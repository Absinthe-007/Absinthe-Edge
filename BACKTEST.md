# V4.0 backtest (Nov 16, 2026)

Everything here was measured by replaying the season, using only information available before each game.

## Games (closing Vegas lines from ESPN pickcenter)
| | NBA (103 games) | NFL (101 games) |
|---|---|---|
| Vegas margin MAE | 11.08 | 9.16 |
| Old 50/50 blend | 11.45 | 9.34 |
| Old team model alone | 12.67 | 9.66 |
| Opponent-adjusted ratings alone | 11.96 | 9.55 |
| Best blend | ~10% model | ~0-10% model |

NBA winner accuracy: Vegas 77.7%, old blend 74.8%, old model 66.0%, new ratings 71.8%.
Total (O/U) MAE is best at 25-30% model weight for NBA, ~0-10% for NFL.

## NBA player props (1,212 walk-forward projections per stat)
- Existing season/L5/split/vs-opponent weights were already at the optimum (every alternative blend or exponential decay was equal or worse), so they were left alone.
- Spread calibration (RMS z-score): points 1.22, PRA 1.19, assists 0.89 -> SD_MULT in `model.py`.
- Actual finished above projection only 42-47% of the time (right skew) -> MED_SHIFT.
- Blocks/steals are overdispersed vs Poisson -> negative binomial.
- Players on a back-to-back finished at 1.03x projection (n=195), not 0.96x; 3+ days rest was 0.98-0.99x, not 1.01x.

## Kalshi picks (3,218 graded)
Brier: model 0.1937, market price 0.1885. Best blend was 10-20% model (log loss); previous setting was 40%.

## Limits
Samples are one partial season (~4 weeks NBA, ~10 weeks NFL), so these are corrections toward the data, not final truth. NFL player projections were not retuned (only 26-275 graded per stat). The tracker now logs signed error so future bias is measured automatically.
