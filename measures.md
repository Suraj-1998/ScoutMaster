# ScoutMaster — model and measures reference

Current as of 5 November 2026. Everything below is built and tested.

---

## The model

| Table | Grain | Key columns |
|---|---|---|
| `fact_player_metrics` | one row per player-season-metric | `player_season_key`, `metric_key`, `value`, `value_padj`, `percentile`, `percentile_league`, `percentile_padj`, `percentile_league_padj` |
| `dim_player_season` | one row per player-season | `player_season_key`, `player`, `team`, `league`, `season`, `position`, `minutes`, `nineties`, `possession` |
| `dim_metric` | one row per metric (27) | `metric_key`, `metric_name`, `metric_group`, `direction`, `adjustment` |
| `dim_league` | one row per league (5) | `league` |
| `Basis` | disconnected, 2 rows | `Basis`, `Order` |

Relationships: all one-to-many, single direction, dimension → fact.
`Basis` connects to nothing — it exists to be read by measures.

`direction` is `higher` (more is better) or `style` (describes how a player
plays, not how well — average pass length, share of passes under pressure,
xG per shot). Style metrics never enter a score.

`adjustment` is `attack`, `defence` or `none`, and controls which possession
correction was applied when the data was built.

Source files are in `model/`, built by `build_model_v2.py`.

---

## Possession adjustment — the idea

Every volume metric was originally divided by 90 minutes. That is the wrong
denominator, because it assumes both players had the same opportunity.

A Barcelona midfielder's team has the ball 70.9% of the match, so she spends
about 26 minutes defending. An Eibar midfielder spends about 58. The Eibar
player gets more than twice as long to make an interception, so she makes
roughly twice as many — without being a better defender.

Unadjusted, the tool answered "who plays for a bad team" when you weighted
defending and "who plays for a good team" when you weighted passing. A scout
already knows which teams are good.

So volume metrics are scaled to what they would have been at 50% possession:

```
defence  x (possession / 50) ** ALPHA_DEFENCE
attack   x (50 / possession) ** ALPHA_ATTACK
```

Ratios — pass completion %, completion under pressure, xG per shot — are not
adjusted. They carry their own denominator already.

### Calibrating alpha instead of guessing it

If the correction is right, a player's score should carry no information
about how much possession her team has. So the test is the correlation
between team possession and adjusted percentile: it should be near zero.

`calibrate_adjustment.py` sweeps alpha and measures it. On this data:

| alpha | defence | attack |
|---|---|---|
| 0.00 | −0.316 | +0.404 |
| 0.50 | **+0.005** | +0.236 |
| 0.75 | +0.159 | +0.133 |
| 1.00 | +0.304 | **+0.015** |

**Defence needs 0.5, attack needs 1.0.** No single value de-biases both —
0.5 leaves attacking metrics favouring Barcelona, 1.0 leaves defensive
metrics doing the same. Forcing them to match would be the biased choice.

The reason they differ is the finding. Possession converts almost one-to-one
into passing opportunity, so passing needs the full correction. It converts
only partly into defensive opportunity, because how many defensive actions
you log also depends on whether your side presses high or sits deep. So
defensive actions scale with roughly the *square root* of the possession
ratio.

Residual correlation under about 0.15 is fine. Chasing zero would be
overfitting to five leagues of one season, and some of what remains is real:
good clubs buy good players.

---

## Measures

All created in `fact_player_metrics`.

### Metric Value

```dax
Metric Value =
IF(
    HASONEVALUE( dim_metric[metric_key] ),
    IF(
        SELECTEDVALUE( Basis[Basis], "Per 90" ) = "Possession adjusted",
        SUM( fact_player_metrics[value_padj] ),
        SUM( fact_player_metrics[value] )
    ),
    BLANK()
)
```

The `HASONEVALUE` guard matters because the fact table is long. With two
metrics selected, a plain `SUM` would add a pass completion percentage to a
goals-per-90 figure and return a real-looking number that means nothing.
Returning blank is better than returning a silent lie.

### Percentile Rank

```dax
Percentile Rank =
IF(
    SELECTEDVALUE( Basis[Basis], "Per 90" ) = "Possession adjusted",
    AVERAGE( fact_player_metrics[percentile_padj] ),
    AVERAGE( fact_player_metrics[percentile] )
)
```

No guard needed here, and that contrast is the lesson. Percentiles share a
0–100 scale whatever metric produced them, so averaging four of them means
something. Raw values share no scale, so summing them does not. The right
function follows from what the numbers mean, not from habit.

Note the name: a measure cannot share a name with a column in its own table,
and `percentile` is taken.

### Percentile Rank in League

```dax
Percentile Rank in League =
IF(
    SELECTEDVALUE( Basis[Basis], "Per 90" ) = "Possession adjusted",
    AVERAGE( fact_player_metrics[percentile_league_padj] ),
    AVERAGE( fact_player_metrics[percentile_league] )
)
```

### Minutes, Possession, Players in Scope

```dax
Minutes = SUM( dim_player_season[minutes] )
```
```dax
Possession = AVERAGE( dim_player_season[possession] )
```
```dax
Players in Scope = DISTINCTCOUNT( dim_player_season[player_season_key] )
```

`Minutes` and `Possession` live in `fact_player_metrics` deliberately — the
source columns are in `dim_player_season` and the names would collide there.

`Players in Scope` on a card catches the filter combination that has quietly
reduced everything to nothing, which otherwise looks identical to "no
results".

### Weighted Score

```dax
Weighted Score =
VAR UseAdj =
    SELECTEDVALUE( Basis[Basis], "Per 90" ) = "Possession adjusted"
VAR GroupWeights =
    UNION(
        ROW( "grp", "Passing",        "w", 'W Passing'[W Passing Value] ),
        ROW( "grp", "Under pressure", "w", 'W Pressure'[W Pressure Value] ),
        ROW( "grp", "Creation",       "w", 'W Creation'[W Creation Value] ),
        ROW( "grp", "Shooting",       "w", 'W Shooting'[W Shooting Value] ),
        ROW( "grp", "Carrying",       "w", 'W Carrying'[W Carrying Value] ),
        ROW( "grp", "Defending",      "w", 'W Defending'[W Defending Value] ),
        ROW( "grp", "Goalkeeping",    "w", 'W Goalkeeping'[W Goalkeeping Value] )
    )
VAR Resolved =
    ADDCOLUMNS(
        GroupWeights,
        "pct",
            VAR g = [grp]
            RETURN
                CALCULATE(
                    IF( UseAdj,
                        AVERAGE( fact_player_metrics[percentile_padj] ),
                        AVERAGE( fact_player_metrics[percentile] ) ),
                    REMOVEFILTERS( dim_metric ),
                    dim_metric[metric_group] = g,
                    dim_metric[direction] = "higher"
                )
    )
VAR Scoring =
    FILTER( Resolved, [w] > 0 && NOT ISBLANK( [pct] ) )
RETURN
    DIVIDE(
        SUMX( Scoring, [pct] * [w] ),
        SUMX( Scoring, [w] )
    )
```

Five things in there worth being able to explain, because between them they
cover most of what PL-300 asks about filter context:

**Weights at group level, not metric level.** Twenty-seven sliders is not a
product. Seven is, and it matches how the question gets asked out loud: a
midfielder who passes progressively and holds up under pressure.

**`VAR g = [grp]` before the `CALCULATE`.** `CALCULATE` performs context
transition, which turns the current row into a filter. Reading `[grp]`
directly inside it is ambiguous. Capturing it first resolves it in the row
context where it makes sense and passes a plain value in.

**`REMOVEFILTERS( dim_metric )`.** Without it, a metric slicer on the page
fights the group filter inside the measure, and selecting one passing metric
blanks every other group. The score is governed by the weights, not by
whatever the user last clicked.

**`dim_metric[direction] = "higher"`.** Keeps style metrics out. A player is
not better for having a longer average pass.

**`FILTER( ..., [w] > 0 && NOT ISBLANK([pct]) )`.** Both halves matter. A
group with zero weight must contribute nothing to the numerator *and nothing
to the denominator*. A group with no data for this player — an outfielder has
no goalkeeping metrics — must also drop out of the denominator, or every
outfielder is silently penalised for not being a goalkeeper. Dividing by the
weight actually used rather than the weight requested is the difference
between a ranking that works and one that looks plausible and is wrong.

### The `Basis` toggle

`Basis` is a disconnected table. It filters nothing through relationships;
measures read it with `SELECTEDVALUE` and change behaviour accordingly. The
second argument is the fallback when nothing or several things are selected.

That harvester pattern is how nearly every "let the user choose" feature in
Power BI is built, and it is the same mechanism behind the seven weight
parameters — Power BI generated `W Passing Value = SELECTEDVALUE('W
Passing'[W Passing], 0)` for each one.

---

## Validation

Each of these was checked and passed.

**Completion % under pressure, midfielders** → Yui Hasegawa, Manchester City,
90.35%, 100th percentile. Then Fran Kirby, Maite Oroz, Patricia Guijarro.
Confirms event parsing, minutes filter, per-90 rates and percentile ranking.

**Weighted passing, midfielders** → Guijarro, Keira Walsh, Georgia Stanway.
Correct names, but two of three are Barcelona — the team-bias problem
surfacing.

**Weighted defending, midfielders** → Huelva, Eibar, Louisville. The same
bias in reverse. This is what prompted the possession work.

**Both weighted 50/50** → Erin Cuthbert, Guijarro, Sarah Zadrazil. Cuthbert
and Zadrazil appear in neither single-metric list. That is the composite
doing something no single-metric sort can.

**Toggle test** → Per 90 gives Castelló Oliver, Kakounan, Howell. Possession
adjusted gives Cuthbert, Diehm, Oberdorf. Identical to what
`build_model_v2.py` printed, which means the DAX and the Python agree.

Note that the top of any percentile list is exactly 100.0, because
`rank(pct=True)` gives the best value rank n of n. It means "nobody above
her", not "better than 100% of players".

---

## Still to do

- `Score Rank` with `RANKX` over the filtered player set
- A comparison view: two players side by side on the same percentile scale
- Position-dependent metric lists, so selecting GK offers goalkeeping metrics
- Page layout and formatting
- README and write-up for the portfolio

## Known limitations, worth stating before someone else finds them

- Possession is proxied by share of passes, not time of control. Correlates
  closely, needs no extra data, but is not identical to the broadcast figure.
- Possession is clipped to 25–75% so one freak match cannot produce an
  extreme multiplier. Nothing in the real data reaches those bounds.
- One season, five leagues, women's football only. The men's Big 5 FBref
  dataset is also pulled and validated but has thinner metrics.
- No adjustment for strength of opposition. A player facing Barcelona every
  week is not facing the same test as one who never does.
