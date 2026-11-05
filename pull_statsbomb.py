#!/usr/bin/env python3
"""
Stage 1 — pull StatsBomb open data and aggregate events to player-per-match.

Written against the actual field names from explore_statsbomb.py rather than
assumed ones.

Two things that are easy to get wrong and are handled explicitly:

  pass_outcome is only recorded when a pass FAILS. A completed pass has no
  outcome at all. So completion = outcome is null, not outcome == 'Complete'.

  under_pressure is True or absent, never False. Treat null as not pressured.

Events are aggregated per match as they arrive, so memory stays flat even
across 700 matches — only the summary rows are kept.

Run:  python pull_statsbomb.py            # WSL only, 132 matches, validate first
      python pull_statsbomb.py --all      # all five leagues, ~700 matches
Out:  raw/player_match_stats.csv
"""

import argparse
import sys
import time
import traceback
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")      # statsbombpy's no-credentials notice

try:
    from statsbombpy import sb
except ImportError:
    sys.exit("statsbombpy not installed.  pip install statsbombpy")

OUT = Path("raw")

# (competition_id, season_id, label) — confirmed from the explore run
LEAGUES = {
    "WSL":        (37,  281, "FA Women's Super League 2023/24"),
    "Bundesliga": (135, 281, "Frauen Bundesliga 2023/24"),
    "LigaF":      (182, 281, "Liga F 2023/24"),
    "NWSL":       (49,  107, "NWSL 2023"),
    "SerieA":     (131, 281, "Serie A Women 2023/24"),
}

# StatsBomb pitch is 120 long x 80 wide, always attacking left to right
PITCH_LENGTH = 120
FINAL_THIRD  = 80


def clock_to_minutes(s) -> float:
    """'45:00' -> 45.0 ; None -> nan"""
    if not isinstance(s, str) or ":" not in s:
        return np.nan
    mm, ss = s.split(":")[:2]
    try:
        return int(mm) + int(ss) / 60
    except ValueError:
        return np.nan


def minutes_played(lineups: dict, match_end: float) -> dict:
    """player_id -> minutes, from the 'positions' spells in the lineup."""
    out = {}
    for team_df in lineups.values():
        for _, row in team_df.iterrows():
            spells = row.get("positions")
            if not isinstance(spells, list) or not spells:
                continue
            starts = [clock_to_minutes(s.get("from")) for s in spells]
            ends = []
            for s in spells:
                t = clock_to_minutes(s.get("to"))
                ends.append(match_end if np.isnan(t) else t)
            starts = [s for s in starts if not np.isnan(s)]
            if not starts or not ends:
                continue
            out[row["player_id"]] = max(0.0, max(ends) - min(starts))
    return out


def end_x(v):
    return v[0] if isinstance(v, (list, tuple)) and len(v) >= 1 else np.nan


def aggregate_match(ev: pd.DataFrame, mins: dict, meta: dict) -> pd.DataFrame:
    """One row per player for this match."""
    ev = ev[ev["player_id"].notna()].copy()
    if ev.empty:
        return pd.DataFrame()

    # normalise the two trap columns
    ev["is_pressured"] = ev.get("under_pressure").fillna(False).astype(bool) \
        if "under_pressure" in ev else False
    passes = ev[ev["type"] == "Pass"].copy()
    if not passes.empty:
        passes["complete"] = passes["pass_outcome"].isna()      # the trap
        passes["start_x"] = passes["location"].map(end_x)
        passes["finish_x"] = passes["pass_end_location"].map(end_x)
        passes["progressive"] = (passes["finish_x"] - passes["start_x"]) >= 10
        passes["into_third"] = ((passes["finish_x"] >= FINAL_THIRD)
                                & (passes["start_x"] < FINAL_THIRD))

    shots   = ev[ev["type"] == "Shot"]
    carries = ev[ev["type"] == "Carry"].copy()
    if not carries.empty:
        carries["start_x"] = carries["location"].map(end_x)
        carries["finish_x"] = carries["carry_end_location"].map(end_x)
        carries["progressive"] = (carries["finish_x"] - carries["start_x"]) >= 5

    rows = []
    for pid, g in ev.groupby("player_id"):
        p  = passes[passes["player_id"] == pid] if not passes.empty else passes
        s  = shots[shots["player_id"] == pid]
        c  = carries[carries["player_id"] == pid] if not carries.empty else carries
        gk = g[g["type"] == "Goal Keeper"]

        pos = g["position"].mode()
        rows.append({
            "player_id":   pid,
            "player":      g["player"].iloc[0],
            "team":        g["team"].iloc[0],
            "position":    pos.iloc[0] if len(pos) else None,
            "match_id":    meta["match_id"],
            "league":      meta["league"],
            "season":      meta["season"],
            "minutes":     round(mins.get(pid, np.nan), 1) if pid in mins else np.nan,

            # passing
            "passes":            len(p),
            "passes_complete":   int(p["complete"].sum()) if len(p) else 0,
            "pass_len_total":    float(p["pass_length"].sum()) if len(p) else 0.0,
            "passes_pressured":  int(p["is_pressured"].sum()) if len(p) else 0,
            "passes_pressured_complete":
                int((p["is_pressured"] & p["complete"]).sum()) if len(p) else 0,
            "passes_progressive": int(p["progressive"].sum()) if len(p) else 0,
            "passes_into_third":  int(p["into_third"].sum()) if len(p) else 0,
            "crosses":    int(p["pass_cross"].notna().sum()) if len(p) and "pass_cross" in p else 0,
            "through_balls": int(p["pass_through_ball"].notna().sum()) if len(p) and "pass_through_ball" in p else 0,
            "key_passes": int(p["pass_shot_assist"].notna().sum()) if len(p) and "pass_shot_assist" in p else 0,
            "assists":    int(p["pass_goal_assist"].notna().sum()) if len(p) and "pass_goal_assist" in p else 0,

            # shooting
            "shots":  len(s),
            "goals":  int((s["shot_outcome"] == "Goal").sum()) if len(s) else 0,
            "xg":     float(s["shot_statsbomb_xg"].sum()) if len(s) else 0.0,

            # carrying
            "carries":             len(c),
            "carries_progressive": int(c["progressive"].sum()) if len(c) else 0,

            # defending
            "pressures":     int((g["type"] == "Pressure").sum()),
            "counterpress":  int(g["counterpress"].notna().sum()) if "counterpress" in g else 0,
            "interceptions": int((g["type"] == "Interception").sum()),
            "clearances":    int((g["type"] == "Clearance").sum()),
            "blocks":        int((g["type"] == "Block").sum()),
            "duels":         int((g["type"] == "Duel").sum()),
            "ball_recoveries": int((g["type"] == "Ball Recovery").sum()),

            # goalkeeping
            "gk_actions":  len(gk),
            "gk_shots_faced": int((gk["goalkeeper_type"] == "Shot Faced").sum()) if len(gk) and "goalkeeper_type" in gk else 0,
            "gk_sweeper":  int(gk["goalkeeper_type"].isin(
                ["Sweeper Keeper", "Smother", "Keeper Sweeper"]).sum()) if len(gk) and "goalkeeper_type" in gk else 0,
        })
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="all five leagues")
    ap.add_argument("--limit", type=int, default=0, help="stop after N matches (testing)")
    args = ap.parse_args()

    chosen = LEAGUES if args.all else {"WSL": LEAGUES["WSL"]}
    OUT.mkdir(exist_ok=True)

    print(f"Leagues: {', '.join(v[2] for v in chosen.values())}\n")

    collected, failed = [], []
    t0 = time.time()

    for name, (cid, sid, label) in chosen.items():
        try:
            matches = sb.matches(competition_id=cid, season_id=sid)
        except Exception as exc:                            # noqa: BLE001
            print(f"{name}: could not list matches — {type(exc).__name__}: {exc}")
            continue

        ids = matches["match_id"].tolist()
        if args.limit:
            ids = ids[:args.limit]
        print(f"{label}: {len(ids)} matches")

        for i, mid in enumerate(ids, 1):
            try:
                ev = sb.events(match_id=mid)
                lu = sb.lineups(match_id=mid)
                match_end = float(ev["minute"].max()) + 1
                mins = minutes_played(lu, match_end)
                rows = aggregate_match(ev, mins, {"match_id": mid, "league": name,
                                                  "season": label})
                if not rows.empty:
                    collected.append(rows)
            except Exception as exc:                        # noqa: BLE001
                failed.append((mid, f"{type(exc).__name__}: {exc}"))

            if i % 10 == 0 or i == len(ids):
                el = time.time() - t0
                print(f"   {i:>4}/{len(ids)}   {el/60:>5.1f} min elapsed", flush=True)

        # write after each league so a crash costs one league
        if collected:
            pd.concat(collected, ignore_index=True).to_csv(
                OUT / "player_match_stats.csv", index=False)

    if not collected:
        sys.exit("nothing collected — send me the errors above")

    df = pd.concat(collected, ignore_index=True)
    df.to_csv(OUT / "player_match_stats.csv", index=False)

    print("\n" + "=" * 62)
    print(f"raw/player_match_stats.csv   {len(df)} rows, {df.shape[1]} columns")
    print(f"matches: {df['match_id'].nunique()}   players: {df['player_id'].nunique()}")
    print(f"elapsed: {(time.time()-t0)/60:.1f} min")
    if failed:
        print(f"\n{len(failed)} matches failed:")
        for mid, err in failed[:5]:
            print(f"   {mid}  {err}")

    print("\nminutes sanity (should mostly be 1-100):")
    print(df["minutes"].describe().to_string())

    print("\npass completion sanity (should be roughly 0.6-0.9 for most):")
    ok = df[df["passes"] >= 20]
    print((ok["passes_complete"] / ok["passes"]).describe().to_string())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception:                                       # noqa: BLE001
        traceback.print_exc()
        print("\nSend me the traceback.")
        sys.exit(1)
