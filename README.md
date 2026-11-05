# ScoutMaster

**A possession-adjusted player scouting model for women's football, built on StatsBomb event data and delivered in Power BI.**

Ask most football databases which midfielder is best at winning the ball back and they will tell you about someone at a relegation-threatened club. Not because she is good, but because her team never has the ball, so she spends ninety minutes defending and racks up enormous numbers doing it.

This project is an attempt to build a scouting tool that does not make that mistake.

---

## What it does

A scout picks a position, decides what matters to them — "progressive passing 50, pressure resistance 30, ball-winning 20" — and the player ranking reorders underneath them. Every metric is a percentile against other players in the same position, so a centre-back is never compared to a striker. Volume metrics are adjusted for how much possession the player's team has, so the ranking is about the player rather than the club.

The question it is designed to answer is not *who had the most interceptions*. It is *who would be good here*.

---

## The data

[StatsBomb Open Data](https://github.com/statsbomb/open-data), pulled with `statsbombpy`.

| | |
|---|---|
| Competitions | FA Women's Super League, Frauen-Bundesliga, Liga F, Serie A Femminile (2023/24), NWSL (2023) |
| Matches | 771 |
| Events aggregated | every on-ball event in those matches |
| Player-match rows | 23,454 |
| Players after a 900-minute filter | 836 |
| Metrics | 27, across seven groups |

Event-level data rather than match summaries, which is what makes metrics like *pass completion percentage while under pressure* possible at all. That one does not exist in any free aggregated source.

Two things in StatsBomb's schema are easy to get wrong and are handled explicitly in `pull_statsbomb.py`:

- **`pass_outcome` is only populated when a pass fails.** A completed pass has no outcome recorded. So completion is `outcome is null`, not `outcome == 'Complete'`. Getting this backwards silently inverts every passing statistic.
- **`under_pressure` is `True` or absent, never `False`.** Null means not pressured.

Minutes come from the per-player position spells in the lineup data, not from assuming ninety.

---

## Why the fact table is long rather than wide

The fact table holds one row per player, per season, per metric — 22,500 rows — instead of one row per player with 27 columns.

A Power BI slicer binds to a column. With a wide table, the list of metrics a user can choose from is fixed in the model, and offering goalkeepers a different list to forwards means rebuilding it. With the metric name held as *data*, the available metrics are just a filtered dimension, and one model serves every position.

The cost is that aggregating across metrics becomes meaningless — summing a pass completion percentage and a goals-per-90 figure produces a real number that means nothing. The `Metric Value` measure guards against it:

```dax
Metric Value =
IF(
    HASONEVALUE( dim_metric[metric_key] ),
    SUM( fact_player_metrics[value] ),
    BLANK()
)
```

Returning blank beats returning a silent lie.

**Schema:** one fact table, three conformed dimensions (`dim_player_season`, `dim_metric`, `dim_league`), all relationships one-to-many and single-direction from dimension to fact, plus one disconnected table driving the raw/adjusted toggle.

---

## The possession problem

The first working version produced two rankings of midfielders:

**Weighted towards passing:** Patricia Guijarro (Barcelona), Keira Walsh (Barcelona), Georgia Stanway (Bayern München).

**Weighted towards defending:** Sandra Castelló Oliver (Sporting Huelva), Bernadette Kakounan (Eibar), Jaelin Howell (Racing Louisville).

Top of the table, then bottom of the table. Neither list is wrong exactly, but both are sorted by club as much as by player.

The cause is the denominator. Every volume metric was divided by 90 minutes, which assumes both players had the same opportunity, and they did not. Barcelona hold 70.9% of possession in this data; Eibar hold 35.5%. Over ninety minutes the Barcelona midfielder spends around 26 minutes defending and the Eibar midfielder around 58. She gets more than twice as long to make an interception, so she makes roughly twice as many, without being a better defender.

Passing is the same problem pointing the other way.

So volume metrics are rescaled to what they would have been at 50% possession:

```
defensive volume  x (team possession / 50) ** alpha
attacking volume  x (50 / team possession) ** alpha
```

Ratios — pass completion %, completion under pressure, xG per shot — are left alone. They already carry their own denominator, and adjusting them would count possession twice.

Possession is proxied by each team's share of passes in the match, weighted by the minutes each player was on the pitch, so a substitute in a match her team dominated is not credited with the whole match.

### Calibrating alpha rather than guessing it

Running the correction at full strength (`alpha = 1`) replaced one bias with its mirror image. Every player in the adjusted top six now came from a team with 56–69% possession, where before they had all come from teams with 36–48%.

Inverting a bias is not fixing it. But "it looks about right now" is not a standard either, so the question needed a test.

**If the adjustment is correct, knowing a player's team possession should tell you nothing about her score.** That makes it measurable: the correlation between team possession and adjusted percentile should sit near zero. `calibrate_adjustment.py` sweeps alpha and reports it, correlating within each position group so that the different possession profiles of defenders and forwards cannot fake the result.

| alpha | defensive metrics | attacking metrics |
|------:|------------------:|------------------:|
| 0.00  | −0.316 | +0.404 |
| 0.25  | −0.162 | +0.327 |
| 0.50  | **+0.005** | +0.236 |
| 0.75  | +0.159 | +0.133 |
| 1.00  | +0.304 | **+0.015** |

**Defensive metrics need `alpha = 0.5`. Attacking metrics need `alpha = 1.0`.**

There is no single value that de-biases both. At 0.5 the attacking metrics still favour possession-dominant teams; at 1.0 the defensive metrics do; at 0.75 both do, mildly. Forcing one exponent onto both — the symmetrical-looking choice — is the one that introduces bias.

The asymmetry is the interesting part, and it is a statement about football rather than about arithmetic. Possession converts almost one-to-one into passing opportunity: twice the ball means roughly twice the passes. It converts only partly into defensive opportunity, because how many defensive actions a player records depends not only on how long the opposition has the ball but on how her team defends while they have it — a high press generates pressures and recoveries, a deep block absorbs attacks without logging many events. **Defensive actions scale with roughly the square root of the possession ratio; attacking actions scale linearly with it.**

Residual correlation under about 0.15 is treated as acceptable. Chasing it to exactly zero would be fitting to five leagues of a single season, and some of what remains is real: good clubs buy good players.

### What changed

The same query, before and after, for defensively-weighted midfielders:

| Per 90 | Possession adjusted |
|---|---|
| Sandra Castelló Oliver — Sporting Huelva | **Erin Cuthbert** — Chelsea |
| Bernadette Kakounan — Eibar | Vanessa Diehm — Hoffenheim |
| Jaelin Howell — Racing Louisville | **Lena Oberdorf** — Wolfsburg |

Oberdorf is widely regarded as the best defensive midfielder in women's football. She did not appear anywhere near the unadjusted list.

Both versions are kept in the model and switchable in the report, because they answer different questions — *how much did she do* and *how much per chance* — and a scout wants to move between them.

---

## The composite score

Weighting all 27 metrics individually would mean 27 sliders, which nobody would use. Weights sit at metric-group level instead — seven dials — which also matches how the question is actually asked out loud: *a midfielder who passes progressively and holds up under pressure.*

The measure builds a virtual table pairing each group with its slider value, resolves each group's mean percentile for the player, and divides by the weight actually used rather than the weight requested. That last distinction matters: a group with no data for a player — an outfielder has no goalkeeping metrics — must drop out of the denominator, or every outfielder is silently penalised for not being a goalkeeper.

Style metrics are excluded from scoring entirely. Average pass length and share of passes under pressure describe *how* a player plays, not how well; there is no good end of the scale, and rewarding whoever hits it longest would be meaningless.

Full DAX and the reasoning behind each line are in [`docs/measures.md`](docs/measures.md).

---

## Validation

Every stage was checked against names a reader of the sport would recognise, rather than against numbers looking plausible.

**Pass completion under pressure, midfielders** → Yui Hasegawa (Manchester City, 90.4%), then Fran Kirby, Maite Oroz, Patricia Guijarro. Confirms event parsing, the minutes filter, per-90 rates and percentile ranking in one query.

**Passing and defending weighted equally** → Erin Cuthbert, Patricia Guijarro, Sarah Zadrazil. Cuthbert and Zadrazil appear in neither single-metric top ten; they rank because they are strong at both rather than extreme at one. That is the entire argument for a composite over a sort.

**Python against DAX** → the report's toggle reproduces the Python pipeline's output exactly, name for name. Two independent implementations agreeing is stronger evidence than either one looking sensible.

---

## Limitations

Stated here rather than waiting for someone to find them.

- **Possession is proxied by share of passes**, not by time of control. It correlates closely and needs no additional data, but it is not the figure a broadcaster shows.
- **Possession is clipped to 25–75%** so that one freak match cannot produce an extreme multiplier. Nothing in the real data reaches those bounds.
- **No adjustment for strength of opposition.** A midfielder facing Barcelona every week is not facing the same test as one who never does. This is the largest remaining source of bias.
- **One season, five leagues, women's football only.** Nothing here has been tested across seasons.
- **A 900-minute minimum** excludes genuine prospects with limited game time, which is exactly the group some scouts care most about.

---

## Running it

```bash
pip install statsbombpy pandas numpy

python pull_statsbomb.py --all      # ~15 min, writes raw/player_match_stats.csv
python build_model.py            # builds the star schema into model/
python calibrate_adjustment.py      # reports the alpha sweep; changes nothing
```

Then open `ScoutMaster.pbix` and refresh. `pull_statsbomb.py` without `--all` does the WSL only, which is faster for a first run.

Alpha values are constants at the top of `build_model.py`.

---

## Built with

Python (pandas, NumPy), `statsbombpy`, Power BI (star schema, DAX, Power Query, what-if parameters).

Data © StatsBomb, used under the terms of their [open data licence](https://github.com/statsbomb/open-data/blob/master/LICENSE.pdf).
