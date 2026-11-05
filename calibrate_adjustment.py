#!/usr/bin/env python3
"""
Stage 2c — calibrate the possession adjustment instead of guessing it.

THE PROBLEM

build_model_v2.py scales volume metrics by a straight ratio:

    defence  x (possession / 50)
    attack   x (50 / possession)

On the real data that flipped the defensive midfielder ranking from
exclusively low-possession teams to exclusively high-possession ones. Going
from one bias to its mirror image is not a fix; it means the correction is
too strong.

THE TEST

If the adjustment were perfectly calibrated, a player's adjusted score would
carry no information about how much possession her team has. So the
correlation between team possession and adjusted percentile should sit near
zero. Before adjusting it is strongly negative for defensive metrics (bad
teams defend more). Over-adjust and it turns positive.

So introduce an exponent and sweep it:

    defence  x (possession / 50) ** alpha
    attack   x (50 / possession) ** alpha

alpha = 0 is no adjustment at all.
alpha = 1 is what build_model_v2.py currently does.
Somewhere in between the correlation crosses zero. That crossing point is
the adjustment the data actually supports, rather than the one that seemed
reasonable when I wrote it.

This script only measures. It changes nothing. It prints the alpha to use,
and then you set ALPHA in build_model_v2.py and re-run.

Run:  python calibrate_adjustment.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("model")
ALPHAS = [0.0, 0.25, 0.4, 0.5, 0.6, 0.75, 1.0]


def corr(a, b) -> float:
    ok = a.notna() & b.notna()
    if ok.sum() < 10:
        return np.nan
    return float(np.corrcoef(a[ok], b[ok])[0, 1])


def main() -> None:
    fp, dp, dm = (OUT / "fact_player_metrics.csv",
                  OUT / "dim_player_season.csv",
                  OUT / "dim_metric.csv")
    for f in (fp, dp, dm):
        if not f.exists():
            sys.exit(f"{f} not found — run build_model_v2.py first")

    fact = pd.read_csv(fp)
    dim = pd.read_csv(dp)
    met = pd.read_csv(dm)

    if "adjustment" not in met.columns:
        sys.exit("dim_metric has no 'adjustment' column — you're still on "
                 "the old build_model.py output. Run build_model_v2.py.")

    f = (fact.merge(met[["metric_key", "adjustment", "metric_group", "direction"]],
                    on="metric_key", how="left")
             .merge(dim[["player_season_key", "position", "possession"]],
                    on="player_season_key", how="left", suffixes=("", "_d")))
    f = f[f["possession"].notna() & f["value"].notna()]

    print(f"{len(f)} fact rows, {f['player_season_key'].nunique()} players")
    print(f"possession {f['possession'].min():.1f}% to {f['possession'].max():.1f}%\n")

    print("Correlation between team possession and a player's mean percentile.")
    print("Zero means the score no longer knows what kind of team she plays for.")
    print("Negative = still favours weak teams.  Positive = now favours strong ones.\n")

    header = "  alpha  " + "".join(f"{s:>14}" for s in ("defence", "attack"))
    print(header)
    print("  " + "-" * (len(header) - 2))

    results = {}
    for alpha in ALPHAS:
        row, scores = [], {}
        for side in ("defence", "attack"):
            sub = f[f["adjustment"] == side].copy()
            ratio = (sub["possession"] / 50.0) if side == "defence" \
                else (50.0 / sub["possession"])
            sub["adj"] = sub["value"] * ratio.pow(alpha)
            sub["pct"] = (sub.groupby(["position", "metric_key"])["adj"]
                             .rank(pct=True).mul(100))
            per_player = (sub.groupby(["player_season_key", "position"], as_index=False)
                             .agg(score=("pct", "mean"),
                                  poss=("possession", "first")))
            # correlate within position, then average, so the four position
            # groups having different possession profiles can't fake a result
            cs = [corr(g["poss"], g["score"])
                  for _, g in per_player.groupby("position") if len(g) >= 15]
            c = float(np.nanmean(cs)) if cs else np.nan
            scores[side] = c
            row.append(f"{c:>14.3f}")
        results[alpha] = scores
        print(f"  {alpha:<5.2f}" + "".join(row))

    print()
    for side in ("defence", "attack"):
        best = min(ALPHAS, key=lambda a: abs(results[a][side])
                   if not np.isnan(results[a][side]) else 9)
        print(f"  {side:<8} closest to zero at alpha = {best:.2f} "
              f"(correlation {results[best][side]:+.3f})")

    print("\nPick the alpha nearest zero for each side, set ALPHA_DEFENCE and")
    print("ALPHA_ATTACK in build_model_v2.py, re-run it, then Refresh Power BI.")
    print("\nA residual correlation under about 0.15 is fine. Chasing it to")
    print("exactly zero would be overfitting to five leagues of one season,")
    print("and some of what's left is real: good clubs buy good players.")


if __name__ == "__main__":
    main()
