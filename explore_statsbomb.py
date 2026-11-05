#!/usr/bin/env python3
"""
Look at StatsBomb's data before writing anything against it.

I can't reach PyPI or GitHub from my side, so I can't test code against this
data myself. Rather than guess at field names again — which cost us four
rewrites this afternoon — this prints exactly what's there, and I'll write the
real pipeline against what it reports.

Nothing is saved. It pulls one match and describes it.

Run:  pip install statsbombpy
      python explore_statsbomb.py
"""

import sys

import pandas as pd

try:
    from statsbombpy import sb
except ImportError:
    sys.exit("statsbombpy not installed.  pip install statsbombpy")

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 50)
pd.set_option("display.max_colwidth", 40)

# ---------------------------------------------------------------- 1. comps

print("=" * 78)
print("1. COMPETITIONS AVAILABLE")
print("=" * 78)
comps = sb.competitions()
print(f"{len(comps)} competition-seasons\n")
print(comps[["competition_id", "season_id", "country_name",
             "competition_name", "season_name", "competition_gender"]]
      .to_string(index=False))

# the five women's leagues from the 2023/24 release
womens = comps[(comps["competition_gender"] == "female")
               & (comps["season_name"].isin(["2023/2024", "2023"]))]
print(f"\n--- women's 2023/24 and 2023 seasons: {len(womens)} ---")
print(womens[["competition_id", "season_id", "competition_name",
              "season_name"]].to_string(index=False))

if womens.empty:
    sys.exit("\nNone found — paste me the full competitions list above.")

# ---------------------------------------------------------------- 2. matches

row = womens.iloc[0]
cid, sid = int(row["competition_id"]), int(row["season_id"])

print("\n" + "=" * 78)
print(f"2. MATCHES — {row['competition_name']} {row['season_name']}")
print("=" * 78)
matches = sb.matches(competition_id=cid, season_id=sid)
print(f"{len(matches)} matches")
print(f"columns: {list(matches.columns)}\n")
print(matches[["match_id", "match_date", "home_team", "away_team",
               "home_score", "away_score"]].head(3).to_string(index=False))

# ---------------------------------------------------------------- 3. events

match_id = int(matches.iloc[0]["match_id"])
print("\n" + "=" * 78)
print(f"3. EVENTS — match {match_id}")
print("=" * 78)
ev = sb.events(match_id=match_id)
print(f"{len(ev)} events, {len(ev.columns)} columns\n")

print("--- event types ---")
print(ev["type"].value_counts().to_string())

print("\n--- all columns ---")
for c in sorted(ev.columns):
    filled = ev[c].notna().sum()
    print(f"  {c:<34} {filled:>5} / {len(ev)}")

# ------------------------------------------------- 4. the fields we need

print("\n" + "=" * 78)
print("4. FIELDS THE SCOUTING METRICS DEPEND ON")
print("=" * 78)

for want in ["player", "position", "minute", "under_pressure",
             "pass_length", "pass_height", "pass_outcome", "pass_type",
             "shot_statsbomb_xg", "shot_outcome", "carry_end_location",
             "goalkeeper_type", "duration", "location"]:
    if want in ev.columns:
        sample = ev[want].dropna().head(3).tolist()
        print(f"  OK      {want:<22} e.g. {sample}")
    else:
        print(f"  MISSING {want}")

print("\n--- one complete pass event ---")
passes = ev[ev["type"] == "Pass"]
if not passes.empty:
    print(passes.iloc[0].dropna().to_string())

print("\n--- goalkeeper events in this match ---")
gk = ev[ev["type"] == "Goal Keeper"] if "Goal Keeper" in set(ev["type"]) else ev.iloc[0:0]
print(f"{len(gk)} found")
if not gk.empty:
    print(gk.iloc[0].dropna().to_string())

# ---------------------------------------------------------------- 5. lineups

print("\n" + "=" * 78)
print("5. LINEUPS — needed for minutes played")
print("=" * 78)
try:
    lu = sb.lineups(match_id=match_id)
    team = list(lu.keys())[0]
    df = lu[team]
    print(f"teams: {list(lu.keys())}")
    print(f"columns: {list(df.columns)}\n")
    print(df.head(3).to_string())
    if "positions" in df.columns:
        print("\n--- 'positions' cell for one player (minutes live here) ---")
        print(df["positions"].iloc[0])
except Exception as exc:                                    # noqa: BLE001
    print(f"lineups failed: {type(exc).__name__}: {exc}")

print("\n" + "=" * 78)
print("Paste all of this back and I'll write the aggregation against it.")
print("=" * 78)
