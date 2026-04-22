## 1. Exit-Reason Distribution

| reason               |   N |    % | realized_bps mean | median |   p25 |   p75 | hold_s mean | wins | win_rate |
|:---------------------|----:|-----:|------------------:|-------:|------:|------:|------------:|-----:|---------:|
| edge_exhausted       | 236 | 100.0 |            -10.04 | -10.33 | -16.2 |  -5.2 |        20.0 |   30 |    12.7% |

**Total closes: 236**

## 2. Edge-Exhausted 'Remaining Edge' Distribution

- Edge-exhausted remaining-bps distribution (N=236):
  - mean=+0.90bps, median=+0.30bps
  - p10=-0.10, p90=+2.50
  - % with remaining <= 0 (forecast says REVERSE): 26.7%
  - Context: friction threshold ~8bps under Merciless; this fires when |remaining| < threshold.

## 3. Exit Reason × Regime Label

| reason \\ regime       |     cascade_down |       cascade_up |             chop |           normal |       trend_down |         trend_up |
|:----------------------|:---------------:|:---------------:|:---------------:|:---------------:|:---------------:|:---------------:|
| edge_exhausted       | N=  8  μ=-16.1bps | N= 46  μ= -9.8bps | N= 27  μ=-13.0bps | N= 32  μ=-12.2bps | N= 55  μ=-11.3bps | N= 68  μ= -6.3bps |

## 4. Exit Reason × Hold-Seconds Bucket (peak_favorable_s proxy)

| reason \\ hold bucket |        short ≤60s |    medium 60-180s |        long >180s |
|:----------------------|:-----------------:|:-----------------:|:-----------------:|
| edge_exhausted       | N=236  μ=-10.0bps |         —         |         —         |

## 5. Council Signal Strength (mark->mark hit by forecast magnitude)

- Sample: 236 closed positions with matched opens
- **Mark→mark directional hit (frictionless):** 105/236 = 44.5%
- Mean mark-move in predicted direction: -0.21bps  (median -0.51bps)
- Mean realized_net (post-friction): -10.04bps  (median -10.33bps)

Slice by council |expected_move_bps_raw| at entry:
| bucket             |   N | mark-hit | mark μ bps | net μ bps | conf μ |
|:-------------------|----:|---------:|-----------:|----------:|-------:|
| mild 1-5           | 107 |    44.9% |      +0.35 |     -9.53 |   0.15 |
| trivial <1         | 129 |    44.2% |      -0.67 |    -10.46 |   0.13 |

## 6. Counterfactual: Mark-Move at Longer Hold Horizons

For each closed position, compute price move in predicted direction
at counterfactual hold horizons (what if the exit didn't fire at 20s?):

| hold   |   N | direction-hit % | mean mark-move bps | median | p25    | p75   | net vs 8bps fees |
|:-------|----:|----------------:|-------------------:|-------:|-------:|------:|-----------------:|
|  20s   | 236 |           44.5% |              -0.21 |  -0.51 |  -6.33 |  +4.8 |            -8.21 |
|  60s   | 236 |           45.8% |              -2.97 |  -2.23 | -14.65 | +10.4 |           -10.97 |
| 120s   | 236 |           31.4% |              -4.97 |  -4.99 | -13.44 |  +2.2 |           -12.97 |
| 180s   | 236 |           39.0% |              -4.68 |  -6.60 | -15.73 |  +9.6 |           -12.68 |
| 250s   | 236 |           40.7% |              -3.76 |  -4.40 | -13.80 |  +5.4 |           -11.76 |

**Interpretation:** if 'mean mark-move' increases with hold, exit truncates real edge (A).
If it stays flat/negative, holding longer doesn't help — signal is weak (B).

## 6b. Counterfactual by Regime Label

| regime       |   N | mean mv @ 20s | @ 60s  | @ 250s | skill persists? |
|:-------------|----:|--------------:|-------:|-------:|:----------------|
| trend_up     |  68 |         +3.50 |  +7.48 |  +5.01 | YES (rises) |
| trend_down   |  55 |         -1.49 |  -2.33 |  -7.66 | NO (reverts) |
| cascade_up   |  46 |         +0.20 | -18.97 |  -9.28 | NO (reverts) |
| normal       |  32 |         -2.43 |  -1.83 |  -5.70 | PARTIAL |
| chop         |  27 |         -3.18 |  -4.96 |  -9.50 | NO (reverts) |
| cascade_down |   8 |         -6.24 |  -2.05 |  +7.52 | YES (rises) |

## 6c. Mark-Move Skill by Council Confidence Bucket

| conf bucket    |   N | mark-hit % | mean mark-move bps |
|:---------------|----:|-----------:|-------------------:|
| low 0.10-0.25  | 236 |      44.5% |              -0.21 |

## 7. Per-Horizon MAE & Hit-Rate (ForecastAuditor)

| label | horizon_s |      N | MAE bps | median abs err | directional hit | |expected| μ | |realized| μ | conf μ |
|:------|----------:|-------:|--------:|---------------:|----------------:|--------------:|--------------:|-------:|
| 15s   |         5 |  10753 |    1.29 |           0.74 |          43.72% |          0.12 |          1.22 |  0.011 |
| 1m    |        60 |  10636 |    4.34 |           3.13 |          49.68% |          0.64 |          4.21 |  0.011 |
