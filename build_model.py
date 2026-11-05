#!/usr/bin/env python3
"""
Stage 2b — season model with possession-adjusted volume metrics.

Replaces build_model.py. Writes to the same model/ folder, so Power BI only
needs a Refresh, not a reimport.

WHY THIS EXISTS

The first model ranked midfielders by defensive volume and returned players
from Betis, Eibar and Racing Louisville — all bottom-half sides. Ranked by
passing volume it returned Barcelona, Barcelona, Bayern. Neither list is
wrong exactly, but both are sorted by table position as much as by ability.

The cause is that every volume metric is divided by 90 minutes, when what it
should be divided by is OPPORTUNITY. You cannot make an interception while
your own team has the ball, and you cannot play a progressive pass while
they have it. A midfielder at Betis defends for most of the match, so her
pressures and recoveries per 90 are enormous without her being a better
defender than Guijarro.

So volume metrics get scaled by the share of the match their team spent in
possession:

    defensive volume  x (team possession / 50)
    attacking volume  x (50 / team possession)

A team on 70% possession has its defensive numbers scaled up by 1.4 and its
attacking numbers scaled down to 0.71. A team on 30% gets the reverse. A
team on exactly 50% is unchanged, which is the point of dividing by 50:
the adjusted figure reads as "what this player would do in an even match".

Ratios are NOT adjusted. Pass completion %, completion under pressure and
xG per shot are already normalised by their own denominator; scaling them
would be counting possession twice.

BOTH VERSIONS ARE KEPT. The raw figure answers "how much did she do" and
the adjusted one answers "how much per chance". A scout wants to move
between them, so the fact table carries value and value_padj side by side
and the report toggles which one it reads.

POSSESSION PROXY

Share of passes in the match, per team, from the data already pulled. Not
identical to the possession figure a broadcaster shows — that is measured on
time of control — but it correlates closely and needs no extra data. Each
player's season figure is weighted by the minutes she played in each match,
so a substitute in a match her team dominated is not credited with the whole
match's possession.

Clipped to 25-75%. At season level nothing realistic goes outside that, but
one freak match should not produce a 3x multiplier.

Run:  python build_model_v2.py
Out:  model/fact_player_metrics.csv   (+ value_padj, percentile_padj)
      model/dim_player_season.csv     (+ possession)
      model/dim_metric.csv            (+ adjustment)
      model/dim_league.csv
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

RAW = Path("raw")
OUT = Path("model")
MIN_MINUTES = 900
POSS_FLOOR, POSS_CEIL = 25.0, 75.0

# How hard to push the possession correction.
#   0.0 = no adjustment at all (identical to the raw figure)
#   1.0 = full linear ratio
# Run calibrate_adjustment.py to find the value where a player's score stops
# correlating with how much possession her team has, then set it here.
ALPHA_DEFENCE = 0.5
ALPHA_ATTACK = 1.0

COUNT_COLS = [
    "passes", "passes_complete", "pass_len_total", "passes_pressured",
    "passes_pressured_complete", "passes_progressive", "passes_into_third",
    "crosses", "through_balls", "key_passes", "assists",
    "shots", "goals", "xg", "carries", "carries_progressive",
    "pressures", "counterpress", "interceptions", "clearances", "blocks",
    "duels", "ball_recoveries", "gk_actions", "gk_shots_faced", "gk_sweeper",
]

# metric_key -> (display name, group, direction, adjustment)
#   direction:  higher / style
#   adjustment: attack / defence / none
METRIC_META = {
    "passes_90":              ("Passes per 90",                  "Passing",    "higher", "attack"),
    "pass_completion_pct":    ("Pass completion %",              "Passing",    "higher", "none"),
    "avg_pass_length":        ("Average pass length",            "Passing",    "style",  "none"),
    "progressive_passes_90":  ("Progressive passes per 90",      "Passing",    "higher", "attack"),
    "passes_into_third_90":   ("Passes into final third per 90", "Passing",    "higher", "attack"),

    "pressured_pass_share":   ("Share of passes under pressure", "Under pressure", "style",  "none"),
    "pressured_completion_pct": ("Completion % under pressure",  "Under pressure", "higher", "none"),

    "crosses_90":             ("Crosses per 90",                 "Creation",   "higher", "attack"),
    "through_balls_90":       ("Through balls per 90",           "Creation",   "higher", "attack"),
    "key_passes_90":          ("Key passes per 90",              "Creation",   "higher", "attack"),
    "assists_90":             ("Assists per 90",                 "Creation",   "higher", "attack"),

    "shots_90":               ("Shots per 90",                   "Shooting",   "higher", "attack"),
    "goals_90":               ("Goals per 90",                   "Shooting",   "higher", "attack"),
    "xg_90":                  ("Expected goals per 90",          "Shooting",   "higher", "attack"),
    "xg_per_shot":            ("xG per shot",                    "Shooting",   "style",  "none"),
    "goals_minus_xg":         ("Goals minus xG",                 "Shooting",   "higher", "none"),

    "carries_90":             ("Carries per 90",                 "Carrying",   "higher", "attack"),
    "progressive_carries_90": ("Progressive carries per 90",     "Carrying",   "higher", "attack"),

    "pressures_90":           ("Pressures per 90",               "Defending",  "higher", "defence"),
    "counterpress_90":        ("Counterpressures per 90",        "Defending",  "higher", "defence"),
    "interceptions_90":       ("Interceptions per 90",           "Defending",  "higher", "defence"),
    "clearances_90":          ("Clearances per 90",              "Defending",  "higher", "defence"),
    "blocks_90":              ("Blocks per 90",                  "Defending",  "higher", "defence"),
    "duels_90":               ("Duels per 90",                   "Defending",  "higher", "defence"),
    "recoveries_90":          ("Ball recoveries per 90",         "Defending",  "higher", "defence"),

    "gk_actions_90":          ("Goalkeeper actions per 90",      "Goalkeeping", "higher", "defence"),
    "gk_sweeper_90":          ("Sweeper actions per 90",         "Goalkeeping", "higher", "defence"),
}


def position_group(pos: str) -> str:
    p = str(pos)
    if "Goalkeeper" in p:
        return "GK"
    if "Back" in p:
        return "DF"
    if "Midfield" in p:
        return "MF"
    if any(k in p for k in ("Wing", "Forward", "Striker")):
        return "FW"
    return "Other"


def safe(numer, denom):
    return np.where(denom > 0, numer / np.where(denom > 0, denom, 1), np.nan)


def possession_by_player_season(pm: pd.DataFrame) -> pd.DataFrame:
    """Minutes-weighted mean possession share for each player-season."""
    team_match = (pm.groupby(["match_id", "team"], as_index=False)["passes"]
                    .sum().rename(columns={"passes": "team_passes"}))
    team_match["match_passes"] = (team_match.groupby("match_id")["team_passes"]
                                            .transform("sum"))
    team_match["poss"] = (team_match["team_passes"]
                          / team_match["match_passes"].replace(0, np.nan) * 100)

    # a match with only one team's events recorded would give 100%; drop those
    sides = team_match.groupby("match_id")["team"].transform("size")
    team_match.loc[sides < 2, "poss"] = np.nan

    merged = pm.merge(team_match[["match_id", "team", "poss"]],
                      on=["match_id", "team"], how="left")
    merged["mins"] = merged["minutes"].fillna(0)
    merged["poss_x_min"] = merged["poss"] * merged["mins"]

    agg = (merged.groupby(["player_id", "league"], as_index=False)
                 .agg(poss_x_min=("poss_x_min", "sum"), mins=("mins", "sum")))
    agg["possession"] = safe(agg["poss_x_min"], agg["mins"])
    agg["possession"] = agg["possession"].clip(POSS_FLOOR, POSS_CEIL)
    return agg[["player_id", "league", "possession"]]


def main() -> None:
    path = RAW / "player_match_stats.csv"
    if not path.exists():
        sys.exit(f"{path} not found — run pull_statsbomb.py first")
    OUT.mkdir(exist_ok=True)

    pm = pd.read_csv(path)
    print(f"{len(pm)} player-match rows, {pm['match_id'].nunique()} matches\n")

    pm["pos_group"] = pm["position"].map(position_group)
    poss = possession_by_player_season(pm)

    # ---- roll up to player-season ---------------------------------------
    agg = {c: "sum" for c in COUNT_COLS if c in pm.columns}
    agg["minutes"] = "sum"
    agg["match_id"] = "nunique"

    s = (pm.groupby(["player_id", "player", "league", "season"], as_index=False)
           .agg(agg).rename(columns={"match_id": "appearances"}))

    primary = (pm.groupby(["player_id", "league", "pos_group"]).size()
                 .reset_index(name="n").sort_values("n", ascending=False)
                 .drop_duplicates(["player_id", "league"])
                 [["player_id", "league", "pos_group"]])
    s = s.merge(primary, on=["player_id", "league"], how="left")

    team = (pm.sort_values("match_id").groupby(["player_id", "league"])["team"]
              .last().reset_index())
    s = s.merge(team, on=["player_id", "league"], how="left")
    s = s.merge(poss, on=["player_id", "league"], how="left")

    before = len(s)
    s = s[(s["minutes"] >= MIN_MINUTES) & (s["pos_group"] != "Other")
          & s["possession"].notna()]
    print(f"{before} player-seasons -> {len(s)} at {MIN_MINUTES}+ minutes")
    print(f"possession range: {s['possession'].min():.1f}% "
          f"to {s['possession'].max():.1f}%, median {s['possession'].median():.1f}%\n")
    if s.empty:
        sys.exit("nothing left after filtering")

    n = s["minutes"] / 90

    # ---- raw metrics -----------------------------------------------------
    m = pd.DataFrame(index=s.index)
    m["passes_90"]              = s["passes"] / n
    m["pass_completion_pct"]    = safe(s["passes_complete"], s["passes"]) * 100
    m["avg_pass_length"]        = safe(s["pass_len_total"], s["passes"])
    m["progressive_passes_90"]  = s["passes_progressive"] / n
    m["passes_into_third_90"]   = s["passes_into_third"] / n
    m["pressured_pass_share"]   = safe(s["passes_pressured"], s["passes"]) * 100
    m["pressured_completion_pct"] = safe(s["passes_pressured_complete"],
                                         s["passes_pressured"]) * 100
    m["crosses_90"]             = s["crosses"] / n
    m["through_balls_90"]       = s["through_balls"] / n
    m["key_passes_90"]          = s["key_passes"] / n
    m["assists_90"]             = s["assists"] / n
    m["shots_90"]               = s["shots"] / n
    m["goals_90"]               = s["goals"] / n
    m["xg_90"]                  = s["xg"] / n
    m["xg_per_shot"]            = safe(s["xg"], s["shots"])
    m["goals_minus_xg"]         = s["goals"] - s["xg"]
    m["carries_90"]             = s["carries"] / n
    m["progressive_carries_90"] = s["carries_progressive"] / n
    m["pressures_90"]           = s["pressures"] / n
    m["counterpress_90"]        = s["counterpress"] / n
    m["interceptions_90"]       = s["interceptions"] / n
    m["clearances_90"]          = s["clearances"] / n
    m["blocks_90"]              = s["blocks"] / n
    m["duels_90"]               = s["duels"] / n
    m["recoveries_90"]          = s["ball_recoveries"] / n
    m["gk_actions_90"]          = s["gk_actions"] / n
    m["gk_sweeper_90"]          = s["gk_sweeper"] / n

    # ---- possession-adjusted copies --------------------------------------
    p = s["possession"].values
    m_adj = m.copy()
    for key, (_, _, _, adjustment) in METRIC_META.items():
        if key not in m_adj.columns:
            continue
        if adjustment == "defence":
            m_adj[key] = m[key].values * np.power(p / 50.0, ALPHA_DEFENCE)
        elif adjustment == "attack":
            m_adj[key] = m[key].values * np.power(50.0 / p, ALPHA_ATTACK)
        # 'none' stays identical to raw, so the toggle is safe on any metric

    s["player_season_key"] = (s["player_id"].astype(int).astype(str) + "_"
                              + s["league"].astype(str))

    # ---- dim_player_season ------------------------------------------------
    dim_ps = pd.DataFrame({
        "player_season_key": s["player_season_key"],
        "player_id":   s["player_id"].astype(int),
        "player":      s["player"],
        "team":        s["team"],
        "league":      s["league"],
        "season":      s["season"],
        "position":    s["pos_group"],
        "appearances": s["appearances"],
        "minutes":     s["minutes"].round(0),
        "nineties":    n.round(1),
        "possession":  s["possession"].round(1),
    })
    dim_ps.to_csv(OUT / "dim_player_season.csv", index=False)

    # ---- fact table --------------------------------------------------------
    def to_long(frame, name):
        out = frame.copy()
        out["player_season_key"] = s["player_season_key"].values
        out["league"]   = s["league"].values
        out["position"] = s["pos_group"].values
        return out.melt(id_vars=["player_season_key", "league", "position"],
                        var_name="metric_key", value_name=name)

    fact = to_long(m, "value").merge(
        to_long(m_adj, "value_padj")[["player_season_key", "metric_key", "value_padj"]],
        on=["player_season_key", "metric_key"], how="left")
    fact = fact.dropna(subset=["value"])
    fact["value"] = fact["value"].round(3)
    fact["value_padj"] = fact["value_padj"].round(3)

    grp_pos = ["position", "metric_key"]
    grp_lge = ["league", "position", "metric_key"]
    fact["percentile"] = (fact.groupby(grp_pos)["value"]
                          .rank(pct=True).mul(100).round(1))
    fact["percentile_league"] = (fact.groupby(grp_lge)["value"]
                                 .rank(pct=True).mul(100).round(1))
    fact["percentile_padj"] = (fact.groupby(grp_pos)["value_padj"]
                               .rank(pct=True).mul(100).round(1))
    fact["percentile_league_padj"] = (fact.groupby(grp_lge)["value_padj"]
                                      .rank(pct=True).mul(100).round(1))

    fact.to_csv(OUT / "fact_player_metrics.csv", index=False)

    # ---- dimensions ---------------------------------------------------------
    present = set(fact["metric_key"])
    pd.DataFrame([{"metric_key": k, "metric_name": v[0], "metric_group": v[1],
                   "direction": v[2], "adjustment": v[3]}
                  for k, v in METRIC_META.items() if k in present]
                 ).to_csv(OUT / "dim_metric.csv", index=False)

    pd.DataFrame({"league": sorted(s["league"].unique())}
                 ).to_csv(OUT / "dim_league.csv", index=False)

    # ---- report --------------------------------------------------------------
    print(f"model/dim_player_season.csv   {len(dim_ps):>6} players")
    print(f"model/fact_player_metrics.csv {len(fact):>6} rows")
    print(f"model/dim_metric.csv          {len(present):>6} metrics")
    print(f"alpha: defence {ALPHA_DEFENCE}, attack {ALPHA_ATTACK}\n")

    print("Lowest and highest possession teams (season average):")
    tp = (dim_ps.groupby("team")["possession"].mean().sort_values())
    for t, v in list(tp.items())[:4]:
        print(f"   {t[:34]:<36} {v:>5.1f}%")
    print("   ...")
    for t, v in list(tp.items())[-4:]:
        print(f"   {t[:34]:<36} {v:>5.1f}%")

    # ---- the proof -----------------------------------------------------------
    print("\n" + "=" * 74)
    print("DEFENSIVE VOLUME, MIDFIELDERS — mean percentile across the")
    print("Defending group, before and after possession adjustment")
    print("=" * 74)

    deff = [k for k, v in METRIC_META.items() if v[1] == "Defending"]
    d = fact[(fact.position == "MF") & (fact.metric_key.isin(deff))]
    sc = (d.groupby("player_season_key")
            .agg(raw=("percentile", "mean"), padj=("percentile_padj", "mean"))
            .merge(dim_ps.set_index("player_season_key")[["player", "team", "possession"]],
                   left_index=True, right_index=True))

    print("\nBEFORE  (raw per 90)")
    for _, r in sc.nlargest(6, "raw").iterrows():
        print(f"   {str(r['player'])[:24]:<26} {str(r['team'])[:22]:<24} "
              f"{r['raw']:>5.1f}   poss {r['possession']:>4.1f}%")

    print("\nAFTER   (possession adjusted)")
    for _, r in sc.nlargest(6, "padj").iterrows():
        print(f"   {str(r['player'])[:24]:<26} {str(r['team'])[:22]:<24} "
              f"{r['padj']:>5.1f}   poss {r['possession']:>4.1f}%")

    print("\nIf the AFTER list still contains only low-possession teams, the")
    print("adjustment has not worked. If it now mixes possession levels, it has.")


if __name__ == "__main__":
    main()
