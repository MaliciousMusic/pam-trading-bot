# PAM Council — Participant Archetype Modeling Trading Bot

> **Paper-trading research platform for BTC/USDT perpetual futures.**
> A council of 24 heterogeneous trading archetypes votes every 5 seconds to produce a weighted consensus price forecast. The bot sizes, enters, and exits positions based on that consensus — and continuously calibrates itself from its own resolved predictions.

---

## Table of Contents

1. [Project Objective](#1-project-objective)
2. [Architecture Overview](#2-architecture-overview)
3. [Ingress Sources](#3-ingress-sources)
4. [The Tribe System](#4-the-tribe-system)
5. [Council Voting Mechanism](#5-council-voting-mechanism)
6. [ChessProjector — Recursive Price Simulation](#6-chessprojector--recursive-price-simulation)
7. [Regime Classifier](#7-regime-classifier)
8. [Decision Engine](#8-decision-engine)
9. [Risk System](#9-risk-system)
10. [Execution & Fee Model](#10-execution--fee-model)
11. [Multi-Layer Exit Engine](#11-multi-layer-exit-engine)
12. [Audit & Backscore](#12-audit--backscore)
13. [Calibration Loops](#13-calibration-loops)
14. [Shadow Ledgers & Tribe Trading Scorer](#14-shadow-ledgers--tribe-trading-scorer)
15. [Replay System](#15-replay-system)
16. [UI Dashboard](#16-ui-dashboard)
17. [Config Reference](#17-config-reference)
18. [Daily Audit & Evolution Scheduler](#18-daily-audit--evolution-scheduler)
19. [Formula Glossary](#19-formula-glossary)
20. [Running the Bot](#20-running-the-bot)

---

## 1. Project Objective

PAM Council models the BTC perpetuals order flow as the aggregate behaviour of **24 participant archetypes** — market makers, arbitrageurs, retail traders, event-driven desks, options hedgers, whales, cascade amplifiers, and more. Each archetype ("tribe") votes on price direction every tick. Their votes are weighted by capital, activity, and measured real-time skill, then fed into a recursive agent-based price simulator (the *ChessProjector*) that produces a forward consensus curve.

The core thesis: **information asymmetry creates a predictable cascade.** Market makers see a liquidity shock immediately; retail traders react 5+ ticks later. By modelling every archetype's perception latency, activation schedule, and behavioural DNA, the council produces a richer signal than any single indicator.

Every forecast is logged, resolved against realized prices, and fed back into two live calibration loops — one that adjusts per-tribe vote multipliers (TribeCalibrator) and one that adjusts the projector's gain (KernelCalibrator). The system learns continuously within a session.

---

## 2. Architecture Overview

```
 INGRESS                    CORE ENGINE (5s tick)               UI
 ─────────────────────      ────────────────────────────────    ──────────────
 BinancePerpsWS  ──┐        1. MarketState.update(events)       WebSocket /ws
 BybitPublicWS   ──┤        2. Each Tribe.vote(ts, state)         │
 OKXPublicWS     ──┤  Bus   3. RegimeClassifier.classify()       Dashboard
 HTFTrendPoller  ──┤  ───►  4. TribeCalibrator.apply_mult()      (index.html)
 OIPoller        ──┤        5. aggregate_votes() → Aggregate
 DeribitOptions  ──┤        6. ChessProjector.project()
 StablecoinPoll  ──┤        7. Executor.evaluate_exits()
 FGIPoller       ──┤        8. Executor.mark_to_market()
 RedditRSSPoller ──┤        9. ShadowLedgers.on_tick()
 RSSFeedPoller   ──┘       10. DecisionEngine.decide()
                            11. Executor.execute()
                            12. ForecastAuditor.record_tick()
 CALIBRATION LOOPS          13. UIBroadcaster.publish()
 ─────────────────────
 TribeCalibrator  ◄── resolved anchors ── ForecastAuditor
 KernelCalibrator ◄── resolved anchors ─┘
 TribeTradingScorer ◄── closed shadow-ledger positions
```

**Key files:**

| Path | Role |
|------|------|
| `src/core/engine.py` | Main tick loop — `TradingEngine` |
| `src/tribes/tribe.py` | Archetype runtime, lifecycle, voting |
| `src/tribes/spec.py` | `TribeSpec` dataclass (parsed from YAML) |
| `src/projector/chess.py` | `ChessProjector` — recursive ABM simulation |
| `src/aggregator/voting.py` | `aggregate_votes()` — council consensus |
| `src/decision/engine.py` | `DecisionEngine` — forecast → `TradeIntent` |
| `src/execution/paper.py` | `PaperExecutor` — sizing, entry, exit, risk |
| `src/execution/shadow.py` | Per-tribe isolated paper accounts |
| `src/audit/backscore.py` | `ForecastAuditor` — anchor create / resolve |
| `src/audit/calibrator.py` | `TribeCalibrator` — EWMA vote-weight multipliers |
| `src/projector/kernel_calibrator.py` | `KernelCalibrator` — live gain fitting |
| `src/audit/tribe_scorer.py` | `TribeTradingScorer` — per-tribe P&L composite |
| `src/regime/classifier.py` | `RegimeClassifier` — chop / trend / cascade labels |
| `src/ui/server.py` | FastAPI app — REST + WebSocket endpoints |
| `src/ui/broadcaster.py` | `UIBroadcaster` — tick broadcast + replay buffer |
| `config/settings.yaml` | All runtime parameters |
| `config/tribes/*.yaml` | Per-archetype specs |
| `scripts/run_bot_ui.py` | Entry point |

---

## 3. Ingress Sources

All sources publish normalized `Event` objects to a shared `EventBus`. No raw exchange payloads reach the engine.

| Source | File | Events | Data |
|--------|------|--------|------|
| **Binance Perps WS** | `src/ingress/binance_ws.py` | `aggTrade`, `forceOrder`, `markPrice`, `depth20` | price, qty, notional_usd, taker side, funding rate, book depth |
| **Bybit Public WS** | `src/ingress/bybit_ws.py` | `aggTrade`, `liquidation`, `funding` | normalized to same schema |
| **OKX Public WS** | `src/ingress/okx_ws.py` | `trade`, `liquidation`, `funding` | normalized to same schema |
| **HTF Trend Poller** | `src/ingress/htf_trend_poller.py` | `htf_trend` every 60 s | 1h EMA-spread/ATR score ∈ [−1, +1]; stale-gated |
| **OI Poller** | `src/ingress/oi_poller.py` | `oi` every 60 s | Binance long/short OI USD |
| **Deribit Options** | `src/ingress/deribit_options.py` | `options` every 120 s | IV surface, put/call ratio, OI-weighted IV |
| **Stablecoin Supply** | `src/ingress/stablecoin_supply.py` | `stablecoin` every 30 min | USDT+USDC circulating supply delta |
| **Fear & Greed Index** | `src/ingress/fgi.py` | `fgi` on poll | score 0–100, label |
| **Reddit RSS** | `src/ingress/reddit_rss.py` | `news` | r/Bitcoin, r/CryptoMarkets headlines |
| **RSS Feeds** | `src/ingress/rss_feeds.py` | `news` every 180 s | 16 curated feeds: SEC, Fed, CFTC, crypto news, Nitter |
| **Event Recorder** | `src/ingress/event_recorder.py` | — | Tapes all events → `data/events/YYYY-MM-DD.jsonl` for replay |

**Event Severity** (`src/ingress/event_classifier.py`):

```
severity = log₁₀(notional_usd + 1) / scale    ∈ [0, 1]
```

Tags: `{price, taker_buy, taker_sell, liq_long, liq_short, funding, news, oi, options, …}`

---

## 4. The Tribe System

### 4.1 The 24 Archetypes

| ID | Strategy Family | Capital Band | Role |
|----|----------------|-------------|------|
| `mm_algo` | mean_reverter_mm | $1M–$100M | Co-located market maker, high-frequency |
| `basis_arb` | cross_venue_arb | $500k–$10M | Funding / spot-perp basis arbitrage |
| `microstructure_prober` | microstructure | $100k–$2M | Reads order-book imbalance, depth events |
| `liquidation_hunter` | event_liquidation | $200k–$5M | Trades into cascading liquidations |
| `news_fader` | news_reactor | $50k–$2M | Fades sentiment overreaction |
| `mev_frontrunner` | mev | $500k–$20M | Front-runs large pending orders |
| `options_hedger_proxy` | options_delta_hedge | $1M–$50M | Delta hedges options book |
| `options_dealer_gamma` | gamma_scalper | $2M–$100M | Gamma scalps around options strikes |
| `passive_whale_twap` | passive_whale | $5M–$500M | Slow TWAP accumulation / distribution |
| `leverage_cycle` | leverage_cycle | $100k–$5M | Rides funding-rate leverage cycles |
| `ta_day_trader` (×3 TZs) | ta_breakout | $10k–$200k | TA breakout/momentum (APAC/EU/US slots) |
| `swing_trend_follower` | trend_follower | $50k–$1M | Multi-day trend signals |
| `contrarian_funding_fader` | funding_fade | $100k–$3M | Fades extreme funding rates |
| `retail_app_newbie` | retail_degen_leveraged | $500–$10k | FOMO / panic retail |
| `retail_grid_bot` | copy_trader | $5k–$50k | Automated grid / copy trading |
| `capitulation_buyer` | contrarian_fade | $50k–$2M | Buys cascading sell-offs |
| `cascade_amplifier` | cascade | $500k–$10M | Amplifies momentum in cascade regimes |
| `put_call_fader` | options_sentiment | $100k–$5M | Fades extreme put/call skew |
| `stablecoin_flow_proxy` | macro_flow | $500k–$20M | Stablecoin supply as bridge-flow signal |

### 4.2 TribeSpec (YAML-driven)

Each tribe is fully defined in `config/tribes/<id>.yaml`:

```yaml
id: mm_algo
name: "Co-located Market Maker"
strategy:
  family: mean_reverter_mm
  params:
    min_spread_bps: 1.2
    max_inventory: 0.8

flow:
  weight: 0.30          # capital × count × activation (pre-computed)
  activation_rate: 1.0  # fraction of time active (0.05 = newbie, 1.0 = colo)
  typical_hold_seconds: [2, 30]   # log-uniform sample per position
  cooldown_seconds: 5

demographics:
  capital_band_usd: [1_000_000, 100_000_000]
  count_estimate: 80
  experience_years: [5, 20]
  jurisdiction: [US, EU, SG]

hardware:
  tier: colo               # phone | laptop | workstation | colo
  typical_latency_ms: 0.5
  uptime: continuous

personality:
  risk_tolerance: 0.85
  fomo: 0.05
  panic: 0.05
  patience: 0.95
  herding: 0.10
  reaction_mu_seconds: 0.1   # latency to process event shocks
  reaction_sigma_seconds: 0.05

circadian:
  presence_by_hour_utc: [1.0, 1.0, 1.0, ...]  # 24 floats; 0 = offline

indicators:
  book_imbalance:
    window_s: 5
    weight: 0.70
  funding_pressure:
    weight: 0.30
```

### 4.3 Tribe Lifecycle

Every tick each tribe runs through a state machine:

```
FLAT  ──► (conviction ≥ 0.10) ──► HOLD ──► (exit trigger) ──► CLOSE ──► COOLDOWN ──► FLAT
```

**FLAT → HOLD (entry):**
- Strategy evaluates MarketState → raw conviction ∈ [0, 1]
- If conviction ≥ 0.10: lock `_sim_side`, sample `_sim_hold_seconds` log-uniformly from `typical_hold_seconds`
- Otherwise: return `idle_bias` vote (tiny lean, no conviction)

**HOLD (in-position vote):**
- **Aligned** (strategy confirms hold): 50% of entry conviction
- **Abstaining** (strategy neutral): 30% of entry conviction
- **Counter** (strategy opposes): 5% of entry conviction
- Exit if `held_s > _sim_hold_seconds` OR a strong counter-signal fires (conviction ≥ 0.15)

**CLOSE:** emit exit vote at 50% entry conviction, opposite side → cooldown

### 4.4 Vote Modifiers

**Shadow Inventory Haircut** (capacity-constrained families: `mm`, `basis_arb`, `grid_bot`):
```
if |inventory| > 0.5 and vote adds to inventory:
    room = (1.0 − |inventory|) / (1.0 − 0.5)
    conviction *= room
# Exits are never haircut — tribe's close signal stays loud
```
Inventory fill: +0.05 per holding tick. Decay on close: ×0.4.

**Retail Tilt Amplifier** (retail families):
```
if 3+ closes within 300 s:
    conviction *= 1.35 × panic_knob
```

**Event Daily Fatigue** (event-fired tribes):
```
budget = 12 fires per UTC day
if fires > budget:
    activation *= decay(fires − budget)  # fades over 8 extra firings
```

### 4.5 Weight Application

```python
effective_weight = spec.flow.weight × presence_factor(ts)   # circadian scaling

buy_frac  = act × (0.5 + conviction / 2)    # BUY vote
sell_frac = act × (0.5 − conviction / 2)    # implicit internal opposition
# SELL vote flips the fracs
# ABSTAIN: buy_frac = sell_frac = 0
```

The aggregator sees **fractional masses, not binary votes.** A tribe at conviction 0.6 contributes 80% of its weight to buy_mass and 20% to sell_mass.

---

## 5. Council Voting Mechanism

**File:** `src/aggregator/voting.py`

```python
buy_mass    = Σ  weight_i × buy_frac_i
sell_mass   = Σ  weight_i × sell_frac_i
total_weight = Σ weight_i

net_pressure = (buy_mass − sell_mass) / total_weight   ∈ [−1, +1]
active_mass  = buy_mass + sell_mass
agreement    = |buy_mass − sell_mass| / active_mass    ∈ [0, 1]
uncertainty  = 1 − agreement                           ∈ [0, 1]
```

`net_pressure` is the primary signal: positive = council net bullish, negative = net bearish.

### Dissension Veto (Phase 3b)

A tribe whose internal buy/sell split is nearly equal is filtered out before aggregation:
```python
lo, hi = min(buy_frac, sell_frac), max(buy_frac, sell_frac)
if lo / hi >= 0.82:
    vote → ABSTAIN   # tribe is 45%/55% — noise, not signal
```

---

## 6. ChessProjector — Recursive Price Simulation

**File:** `src/projector/chess.py`

The projector runs **50 steps × 5 s = 250 seconds** of recursive agent-based simulation. Each tribe follows deterministic behavioural DNA while reacting to shared world shocks sampled from a regime-aware schedule.

### Pipeline

#### Step 1 — Per-Horizon Weight Update (ledger scoring)
```python
# Compare tribe i's h-step-ago prediction against realized price
delta = (median_err − tribe_err) / median_err    clamped [−1, +1]
W[tribe][h] *= exp(0.01 × delta)                 # multiplicative EWMA
W[tribe][h] = clamp(W[tribe][h], 0.05, 2.5)
# Normalize each horizon column: Σ W[:,h] = n_tribes
```

#### Step 2 — World Shock Schedule (regime-aware)
```
cascade : 35% liquidity, 30% micro, 20% sentiment, 15% macro
trend   : 30% macro, 35% sentiment, 20% micro, 15% liquidity
chop    : 30% micro, 30% liquidity, 25% macro, 15% sentiment
normal  : 25% each category
```
~4 typed shocks sampled; magnitudes scale with terminal sigma.

#### Step 3 — Cross-Correlation Squasher (Phase 1a)
```python
# 64-step rolling signed-conviction history per tribe
# Pairwise Pearson correlation
if corr > 0.8:
    pool_weight[tribe_i] *= (1 − corr_weight × 0.5)
# Prevents 3 TA tribes from triple-counting the same chart signal
```

#### Step 4 — Regime-Aware Pool Weight Overlay (Phase 3a)

Category multipliers per regime:

| Category | chop | normal | trend | cascade |
|----------|------|--------|-------|---------|
| mm / arb | 1.30 / 1.15 | 1.00 | 0.85 / 1.05 | 0.35 / 1.60 |
| retail | 0.70 | 1.00 | 1.25 | 1.45 |
| ta | 0.75 | 1.00 | 1.35 | 1.15 |
| event | 0.90 | 1.00 | 1.10 | 1.55 |

Regime blend uses a sigmoid smoother over 7.5 s to avoid hard jumps:
```python
α = sigmoid((ts − regime_change_ts) / 7.5)
ω_eff = ω_base × lerp(prev_mult, curr_mult, α) × (1 − redundancy_haircut)
```

#### Step 5 — TA Family Cap (Task 4d)
```python
ta_total = Σ pool_weight[t] for t in TA_TRIBES
if ta_total > 0.20 × grand_total:
    scale each TA tribe down proportionally
```

#### Step 6 — Contrarian Inversion (Task 2)
If the `(tribe, regime, horizon_bucket)` accuracy cube from TribeCalibrator shows the tribe is **reliably-worse-than-chance** in this context:
```python
side ← flip(side)
buy_frac, sell_frac ← swap(buy_frac, sell_frac)
```

#### Step 7 — Harvest Mass Boost (Task 4e)
```python
harvest_mass = Σ pool_w[t] for t in contrarian_tids
ratio = harvest_mass / total_mass
conviction_boost = 1.0 + harvest_scale × ratio   # harvest_scale = 0.5 → max 1.5×

# Top-quartile contrarian tribes (by TribeTradingScorer net_score):
conviction_boost *= 2.0
```

#### Step 8 — Per-Tribe Recursive Walk (DNA)
For each tribe `i` and each step `h`:

```python
elapsed_s = h × tick_seconds
decay = exp(−elapsed_s / tribe_horizon_s)          # horizon decay
base_strength = base_strength_pre × decay

dna_delta = dna_fn(ctx)           # category-specific response function

# Real scheduled shocks (liquidations, news, OI events)
event_bps = step_event_bps(h, state)

# Order-book depth scaling
depth_scale = depth_ref_usd / max(side_depth, depth_floor_usd)

total_bps = (dna_delta + event_bps) × depth_scale

# Ornstein-Uhlenbeck mean reversion
if mean_reversion_rate > 0:
    total_bps −= mean_reversion_rate × cum_bps

cum_bps += total_bps
```

#### Step 9 — Performance-Weighted Consensus
```python
consensus_bps[h] = Σ (path[tribe][h] × W[tribe][h] × pool_w[tribe])
                   ─────────────────────────────────────────────────
                       Σ (W[tribe][h] × pool_w[tribe])
```

Each tribe's path is weighted jointly by:
- **W[tribe][h]** — measured accuracy at horizon `h` (ledger scoring)
- **pool_w[tribe]** — regime-aware, correlation-adjusted pool weight

### Forecast Output

```python
class Forecast:
    steps_bps: list[float]          # cumulative consensus curve (50 steps)
    steps_sigma_bps: list[float]    # per-step stddev (uncertainty fan)
    sample_paths_bps: list[...]     # MC realizations (n = sample_paths_n)

    expected_move_bps: float        # steps_bps[-1] × conviction_boost
    confidence: float               # Σ (decay^h × agreement_h) / norm
    horizon_seconds: int            # n_steps × tick_seconds

    diversity_score: float          # Shannon entropy of category buy/sell mass
    category_mass: dict             # {"retail": {"buy": …, "sell": …}, …}
    avg_voter_horizon_s: float      # weighted-avg holding horizon across votes
    driver_skill_avg: float         # avg skill multiplier of consensus drivers
    harvest_mass_ratio: float
    conviction_boost: float
```

---

## 7. Regime Classifier

**File:** `src/regime/classifier.py`

Reads `MarketState` (5-min rolling) every tick. No ML — pure thresholds calibrated for BTC perps.

```python
vol_bps       = (5m high − low) / mid × 10000
drift_eff     = |Δprice| / vol_bps          ∈ [0, 1]   # how directional is vol?
liq_60s_usd   = long_liq_usd + short_liq_usd over last 60 s

# Decision tree
if liq_60s_usd ≥ 5_000_000 OR vol_bps ≥ 60:
    label = CASCADE_UP / CASCADE_DOWN    (sign of drift)
elif vol_bps ≤ 15 OR drift_eff ≤ 0.30:
    label = CHOP
elif drift_eff ≥ 0.55:
    label = TREND_UP / TREND_DOWN        (sign of drift)
else:
    label = NORMAL
```

**Regime multipliers** (applied to `steps_bps` via projector):

| Label | Multiplier | Meaning |
|-------|-----------|---------|
| `chop` | 0.6 | Dampen — narrow range, reversion dominant |
| `normal` | 1.0 | Neutral |
| `trend_up` / `trend_down` | 1.2 | Amplify — directional momentum |
| `cascade_up` / `cascade_down` | 1.7 | Strong amplification — forced unwinds |

---

## 8. Decision Engine

**File:** `src/decision/engine.py`

Translates the `Forecast` into a `TradeIntent` (or hold). No hard gates beyond minimum confidence.

### Gating
```python
if price <= 0:         → hold("no mark price")
if emove == 0:         → hold("council flat")
if conf < 0.10:        → hold(f"incoherent conf={conf:.3f}")
```

### Multiplier Stack
```python
pct = size_pct(conf)                    # confidence → base % of equity

# Each multiplier shrinks position size toward 0; none can amplify above base
σ_mult     = sigma_multiplier(forecast)           # [0.35, 1.0] — vol-adjusted sizing
agree_mult = directional_agreement(forecast)      # [0.50, 1.0] — MC path consensus
htf_mult   = htf_multiplier(ts, state, side)      # [0.40, 1.0] — counter-trend shrink
crowd_mult = crowded_trade_multiplier(forecast)   # [floor, 1.0] — retail/TA concentration
horizon_mult = horizon_multiplier(forecast)       # [floor, 1.0] — panic-horizon shrink

# Task 4c — Path Integrity Cap
if driver_skill_avg < 0.70:
    # Consensus carried by unreliable tribes — cap all multipliers at 1.0
    agree_mult = min(agree_mult, 1.0)
    crowd_mult = min(crowd_mult, 1.0)
    htf_mult   = min(htf_mult,   1.0)
    horizon_mult = min(horizon_mult, 1.0)

size_usd = equity × pct/100 × leverage
           × σ_mult × agree_mult × htf_mult × crowd_mult × horizon_mult
```

### Sizing Curve (`settings.yaml → sizing.curve`)
```
linear:    size% = min_pct + (max_pct − min_pct) × conf
quadratic: size% = min_pct + (max_pct − min_pct) × conf²
default:   1.0% – 6.0% across conf ∈ [0, 1]
```

### HTF Multiplier (counter-trend shrink)
```python
# 1h EMA-spread / ATR score ∈ [−1, +1]; staleness-gated
aligned = (side == BUY and score > 0) or (side == SELL and score < 0)
if aligned: return 1.0
return 1.0 − htf_counter_strength × min(1.0, |score|)
# htf_counter_strength = 0.6 → max 60% position shrink when fully counter-trend
```

### Crowded Trade Multiplier (Phase 1c)
```python
if conf < 0.85: return 1.0          # only fires at high confidence
crowded_share = (retail_mass + ta_mass)[side] / total_mass[side]
if crowded_share ≤ 0.50: return 1.0
# Shrink toward floor as crowded share overshoots 0.50
return 1.0 − (1.0 − shrink_floor) × overshoot_ratio
```

### Horizon Multiplier (Phase 3c)
```python
avg_ratio = avg_voter_horizon_s / nominal_horizon_s
if avg_ratio ≥ 0.50: return 1.0     # council thinking at reasonable horizon
# Panic-trading: avg voter holding 10 s in a 250 s window → shrink
return 1.0 − (1.0 − horizon_shrink_floor) × (1 − avg_ratio / 0.50)
```

### Peak Favorable Seconds (Task 3)
```python
# Stamps each opened position with when the forecast peaks in its favor
peak_step = argmax(steps_bps)   (long) or argmin(steps_bps)   (short)
peak_favorable_s = (peak_step + 1) × tick_seconds
# Used by Layer 2c of the exit engine (shape-aware exit)
```

---

## 9. Risk System

**File:** `src/execution/paper.py`

All thresholds are config-driven (`settings.yaml → risk`).

| Guard | Default | Behavior |
|-------|---------|----------|
| **Drawdown from Peak** | 8% | Halt new entries when `equity ≤ peak_equity × 0.92`; auto-clears on recovery |
| **Daily Loss** | 5% | Halt when `equity ≤ day_open_equity × 0.95`; auto-clears on UTC midnight roll |
| **Session DD (hard)** | 8% | Close ALL open positions + permanent halt until `POST /api/risk/unlock` |
| **Per-Position SL** | 1% | Force-close any position with `unrealized_loss ≥ 1% × equity` |
| **Black Swan** | 5σ | Emergency close-all + 180 s cooldown (see below) |
| **Concurrent Positions** | 20 | Hard cap; new entries rejected when full |
| **Total Notional** | 500% equity | Cap on `Σ position_size_usd / equity_usd` |

### Black Swan Detection
```python
# Every tick (after warmup_s)
delta_bps = |log(price_now / price_30s_ago)| × 10000

# 5-min realized σ from per-second log returns
ref_sigma = max(stddev(returns_bps, window=300s), min_ref_sigma_bps=5)

if delta_bps > black_swan_sigma × ref_sigma:   # default 5σ
    close_all_positions(current_price)
    cooldown_until = now + black_swan_cooldown_s  # 180 s
    # Post-cooldown: additional warmup_s (30 s) before detection re-arms
```

---

## 10. Execution & Fee Model

**File:** `src/execution/paper.py`

### Entry
```python
# Size-scaled slippage
slippage_effective = slippage_bps × (1 + slippage_size_k × size_usd / slippage_ref_notional)
slippage_effective = min(slippage_effective, slippage_cap_bps)   # cap 30 bps

entry_cost_bps = fee_rate_bps + slippage_effective    # typically 4–5 bps

# Half-spread adjustment
entry_cost_bps += half_spread_bps

# Adjusted entry price
entry_price_adj = mark_price × (1 + entry_cost_bps / 10000)   # long
entry_price_adj = mark_price × (1 − entry_cost_bps / 10000)   # short

# Forecast TP bracket
tp_price = entry_price_adj ± |emove × conf × tp_factor| / 10000
```

### Round-trip Fee Budget
```
Entry: ~4–5 bps (fee + slippage)
Exit:  ~4–5 bps (symmetric)
Total: ~10 bps base → 30 bps max (at slippage cap)
```

### Ledger
Every execution and tick mark-to-market is appended to `data/ledger/YYYY-MM-DD.jsonl`.

---

## 11. Multi-Layer Exit Engine

**File:** `src/execution/paper.py` — `evaluate_exits()`

Each open position is evaluated against 6 ordered layers every tick:

### Layer 0 — Hard Stop Loss
```python
unrealized_bps = (price / entry_price − 1) × 10000    (signed for side)
if unrealized_bps < −max_position_loss_pct × equity_pct_threshold:
    close(reason="SL")
```

### Layer 1 — Council Sign-Flip Beyond Dead Zone
```python
dead_zone = exit_dead_zone_bps × dead_zone_regime_mult[regime]
# dead_zone_regime_mult: cascade=0.5 (tight), chop=1.5 (wide), normal=1.0

flipped = (position.long AND forecast_emove < −dead_zone) OR
          (position.short AND forecast_emove > +dead_zone)

if flipped:
    if forecast.confidence ≥ strong_flip_conf (0.25):
        close(reason="flip strong")              # immediate
    else:
        _flip_streak[pos.id] += 1
        if _flip_streak[pos.id] ≥ weak_flip_confirm_ticks (2):
            close(reason="flip confirmed")       # need 2 consecutive ticks
else:
    _flip_streak[pos.id] = 0
```

### Layer 2 — Profit Ratchet
```python
if unrealized_bps ≥ ratchet_arm_bps (20):
    peak = max(_peak_bps.get(pos.id, 0), unrealized_bps)
    retrace_level = peak × (1 − ratchet_retrace_frac)   # 65% of peak
    if unrealized_bps ≤ retrace_level:
        close(reason="ratchet")
```

### Layer 2b — Forecast Target TP
```python
tp_target = |forecast_emove| × forecast.confidence
if tp_target ≥ max(forecast_tp_min_bps, round_trip_fees_bps):
    if unrealized_bps ≥ tp_target:
        close(reason="forecast TP")
```

### Layer 2c — Shape-Aware Exit (Task 3)
```python
# peak_favorable_s stamped at entry by DecisionEngine
if held_s > peak_favorable_s + shape_grace_seconds (30):
    if unrealized_bps > 0:              # only take profit, never stop-loss
        close(reason="past peak")
```

### Layer 3 — Edge Exhausted
```python
remaining_move = forecast_emove_bps − unrealized_bps
if remaining_move < edge_exhausted_factor × round_trip_fees_bps:
    close(reason="edge exhausted")
# If the council's remaining forecast is smaller than what it costs to exit,
# there is no edge left — exit now.
```

---

## 12. Audit & Backscore

**File:** `src/audit/backscore.py` — `ForecastAuditor`

### Anchor Creation (every tick)
The engine emits predictions at 4 standard horizons:

| Label | Horizon | Step Index (50-step projector) |
|-------|---------|-------------------------------|
| `15s` | 15 s | step 3 |
| `1m` | 60 s | step 12 |
| `5m` | 300 s | step 60 (capped at 50) |
| `10m` | 600 s | step 120 (capped at 50) |

```python
anchor = PendingAnchor(
    tick_ts       = now,
    target_ts     = now + horizon_s,
    entry_price   = mark_price,
    predicted_price = mark_price × (1 + steps_bps[step] / 10000),
    expected_move_bps, forecast_conf, net_pressure
)
```

### Anchor Resolution
When `now ≥ anchor.target_ts`:
```python
realized_move_bps  = (realized_price / entry_price − 1) × 10000
signed_error_bps   = predicted_price / realized_price − 1) × 10000
abs_error_bps      = |signed_error_bps|
directional_hit    = sign(expected_move) == sign(realized_move) AND both ≠ 0
```

Persisted to `data/forecasts/YYYY-MM-DD.jsonl`. Fed immediately to TribeCalibrator and KernelCalibrator.

### Dashboard Metrics
- **MAE** — Mean absolute error per horizon (in bps)
- **Hit Rate** — Fraction of `directional_hit == True` per horizon
- **Bias** — Mean signed error; positive = systematic over-prediction

---

## 13. Calibration Loops

### 13.1 TribeCalibrator (Phase G + H)

**File:** `src/audit/calibrator.py`

Tracks per-tribe directional accuracy on resolved anchors and converts it into a live vote-weight multiplier.

**EWMA Update:**
```python
α_slow = 1 − 0.5^(1 / halflife_resolved)    # halflife ≈ 500 resolved anchors
α_fast = 1 − 0.5^(1 / fast_halflife)        # halflife ≈ 50 resolved anchors

hit = 1.0 if tribe_vote_sign == realized_move_sign else 0.0
# (skips votes with conviction < 0.05 — unfalsifiable micro-votes)

ewma_slow += α_slow × (hit − ewma_slow)
ewma_fast += α_fast × (hit − ewma_fast)
```

**Skill → Multiplier mapping** (`max_shrink=0.6`, `max_boost=1.4`):
```
ewma ≤ 0.5  →  mult = 0.6 + (1.0 − 0.6) × (2 × ewma)     # below chance → shrink
ewma > 0.5  →  mult = 1.0 + (1.4 − 1.0) × 2 × (ewma − 0.5) # above chance → boost
```

**Granularity options:**
- **Global** — single EWMA per tribe (default, requires ≥ 30 samples)
- **Regime-conditional** — separate EWMA per `(tribe, regime)` (requires ≥ 15 samples per cell)
- **Horizon-bucket** — separate EWMA per `(tribe, bucket)` where `bucket ∈ {short ≤60s, medium 60–300s, long >300s}`

**Phase H — Within-Session Regime Shift Detection:**
```
if |ewma_fast − ewma_slow| > regime_shift_threshold (0.12):
    flag tribe as "regime shifted" → fast EWMA takes precedence temporarily
    converge back to slow EWMA as gap closes
```

**3D Accuracy Mask (Task 2 — TA Cemetery / Contrarian Inversion):**
Per `(tribe, regime, horizon_bucket)` cell: if the tribe's accuracy in that specific context is *reliably below 0.5*, the projector flips its vote in that context rather than silencing it — turning a reliable loser into a contrarian signal.

### 13.2 KernelCalibrator (Phase A)

**File:** `src/projector/kernel_calibrator.py`

Fits `bps_per_unit_pressure` (the projector's gain parameter) live:

```python
ratio = realized_move_bps / predicted_move_bps   # if |predicted| > 1 bps

# Rolling median of last `window` (2000) ratios — robust to outliers
gain = clamp(median(ratio_window), 1.0, 40.0)

# EWMA smoothing
α = 1 − 0.5^(1 / halflife_resolved)   # halflife ≈ 300 anchors
gain_ewma += α × (ratio − gain_ewma)

if apply:
    projector.bps_per_unit_pressure = gain
```

Persisted to `data/calibration/projector_gain.json` so the fit survives restarts. Cleared on hard reset.

---

## 14. Shadow Ledgers & Tribe Trading Scorer

**Files:** `src/execution/shadow.py`, `src/audit/tribe_scorer.py`

Each of the 24 tribes runs its own isolated **$50,000 paper account** in parallel with the main bot. These accounts use the same 6-layer exit engine but driven entirely by the tribe's own vote.

### Shadow Ledger Entry
```python
if vote.side != ABSTAIN and rng.random() < open_probability:
    size_usd = equity × size_base_pct (2%) × conviction / min_conv_epsilon (0.10)
    if total_notional ≤ max_notional:
        create_position(vote.side, size_usd, current_price, ...)
```

### TribeTradingScorer — Composites

For each tribe and each side (long/short):

```python
# Hit rate: fraction of closed trades that were profitable
# EWMA over last `halflife_trades` (300) closed trades

skill = hit_ewma_by_side

# Multiplier (same curve as TribeCalibrator)
if skill ≤ 0.5:
    trading_multiplier = max_shrink + (1 − max_shrink) × 2 × skill
else:
    trading_multiplier = 1.0 + (max_boost − 1.0) × 2 × (skill − 0.5)
```

Net score (composite, drives harvest boost eligibility):
```
net_score = weighted(edge, sharpe_ewma, drawdown_pct, discipline, capture_rate)
```

`calibrator.attach_trading_scorer(tribe_scorer)` wires the `trading_multiplier` into the vote-weight chain automatically each tick.

### Harvest Mass Boost (Task 4e)

Top-quartile tribes (by `net_score`) that are **also flagged as contrarian** (their current vote opposes the majority) receive a **2× pool-weight boost** in the projector, on top of the base conviction boost:
```
conviction_boost = 1.0 + 0.5 × (contrarian_mass / total_mass)
# top-quartile contrarian tribes: conviction_boost × 2.0
```

---

## 15. Replay System

**Files:** `src/replay/`, `src/replay/watch_engine.py`

The replay system enables backtesting, A/B variant testing, and adversarial stress-testing against historical or synthetic event tapes.

| Phase | Script / Module | Purpose |
|-------|----------------|---------|
| **1** | `test_replay_phase1.py` | OHLCV ingest → normalized Event stream |
| **2** | `test_replay_phase2.py` | Tribe vote accuracy audit on historical events |
| **3** | `test_replay_phase3.py` | Forecast calibration: anchor create → resolve |
| **4** | `run_replay.py` | Full replay with live UI feed via WatchController |
| **6** | `run_multi_variant.py` | A/B tournament: N config variants, same event tape |
| **7** | `run_adversarial.py` | Adversarial: synthetic cascade / liquidation storms |
| **8** | `build_report.py` | Week summary: HTML report, P&L curve, accuracy heatmap |
| **9** | `test_replay_phase9.py` | Tribe family breakdown (TA/Retail/MM composite P&L) |

**WatchController** (`src/replay/watch_engine.py`):
- Accepts a `day` + optional `speed` multiplier (default 10×) and `seed`
- Mutes live-tick broadcasts during replay; unmutes on completion
- Supports multi-day chaining with equity carry-over
- Multi-variant mode: runs N configs concurrently on the same tape for fair comparison

**Event tape** stored in `data/events/YYYY-MM-DD.jsonl` by the `EventRecorder` during live sessions.

---

## 16. UI Dashboard

**Server:** `src/ui/server.py` (FastAPI, port 8765)
**Frontend:** `src/ui/static/index.html` (single-file SPA)

### REST Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard SPA |
| `GET /api/tribes` | Tribe metadata (id, name, weight, strategy family, …) |
| `GET /api/backscore` | MAE + hit-rate per horizon (trailing window) |
| `GET /api/calibration` | Per-tribe EWMA accuracy + multipliers |
| `GET /api/tribe_trading` | Shadow-ledger scorecards + open equity per tribe |
| `GET /api/kernel` | KernelCalibrator snapshot (current gain, n samples) |
| `GET /api/regime` | Current regime label + vol/drift stats |
| `GET /api/config` | Live config snapshot |
| `GET /api/risk/snapshot` | Risk guard states (halted?, DD%, session DD%) |
| `POST /api/risk/unlock` | Clear session-DD halt (operator action) |
| `POST /api/replay/start` | Start replay (day, speed, seed, variants) |
| `POST /api/replay/pause` | Toggle replay pause |
| `POST /api/replay/stop` | Stop replay |
| `POST /api/replay/speed` | Adjust replay speed live |
| `GET /api/replay/runs` | List recent replay run directories |
| `GET /api/replay/tick` | Fetch specific tick from completed replay |
| `GET /api/audit/report` | Download full session audit as `.txt` |

### WebSocket `/ws`

Replays recent tick buffer on connect, then streams live ticks. Each message:

```json
{
  "type": "tick",
  "tick": 4320,
  "ts": "2026-04-20T15:30:00Z",
  "price": 76234.40,
  "net_pressure": 0.32,
  "uncertainty": 0.18,
  "expected_move_bps": 8.5,
  "confidence": 0.72,
  "regime": { "label": "trend_up", "multiplier": 1.2, "vol_bps": 28.5 },
  "votes": [{ "tribe_id": "mm_algo", "side": "buy", "conviction": 0.38, "weight": 0.30 }],
  "forecast_steps_bps": [0.2, 0.4, 0.7, 1.1, "...50 steps"],
  "forecast_sigma_bps": [2.0, 2.5, 3.1, "..."],
  "diversity_score": 0.68,
  "category_mass": { "mm": { "buy": 0.25, "sell": 0.15 }, "retail": { "buy": 0.20 } },
  "avg_voter_horizon_s": 285,
  "driver_skill_avg": 0.85,
  "positions": [{ "id": 1, "side": "long", "size_usd": 15000, "entry_price": 76100 }],
  "equity_usd": 52340,
  "trades": 127,
  "wins": 89,
  "action": "open_long",
  "reason": "emove +8.5bps conf=0.72 size=2.50% σ×0.98 agree×0.95 htf×1.00"
}
```

### Dashboard Panels

| Panel | Data |
|-------|------|
| **Price + Forecast Fan** | `forecast_steps_bps` ± `forecast_sigma_bps` overlaid on live price |
| **Council Pressure Gauge** | `net_pressure`, `uncertainty` |
| **Tribe Vote Heatmap** | Per-tribe conviction × side, color-coded by direction |
| **Accuracy Heatmap** | `calibration` — per-tribe EWMA hit-rate with multiplier badge |
| **Tribe Trading Heatmap** | `tribe_trading` — per-tribe P&L, Sharpe, drawdown from shadow ledgers |
| **PnL Radar** | Net score across 5 composite axes per tribe |
| **Regime Badge** | Current regime label + multiplier + reason string |
| **Backscore Table** | MAE + hit-rate at 15s / 1m / 5m / 10m horizons |
| **Positions Table** | Open positions with TP/SL brackets |
| **Risk Panel** | Equity curve, drawdown%, guard states, unlock button |
| **Replay Controls** | Start / pause / speed for historical playback |

---

## 17. Config Reference

**File:** `config/settings.yaml`

```yaml
mode: paper            # paper | live
symbol: BTCUSDT
leverage: 15           # notional multiplier
base_capital_usd: 50000

clock:
  tick_seconds: 5           # vote + project cadence
  projector_steps: 50       # 50 × 5s = 250s horizon

risk:
  max_drawdown_from_peak_pct: 8.0
  max_daily_loss_pct: 5.0
  max_session_dd_pct: 8.0
  max_position_loss_pct: 1.0
  max_concurrent_positions: 20
  max_total_notional_pct: 500.0
  black_swan_sigma: 5.0
  black_swan_window_s: 30.0
  black_swan_sigma_window_s: 300.0
  black_swan_cooldown_s: 180.0
  black_swan_warmup_s: 30.0
  black_swan_min_ref_sigma_bps: 5.0

decision:
  min_conf: 0.10
  exit_dead_zone_bps: 3.0
  sigma_ref_bps: 40.0
  sigma_shrink_floor: 0.35
  htf_counter_strength: 0.6
  crowded_conf_threshold: 0.85
  crowded_share_threshold: 0.50
  crowded_shrink_floor: 0.5
  horizon_ref_ratio: 0.50
  horizon_shrink_floor: 0.5
  edge_exhausted_factor: 1.5
  driver_skill_floor: 0.70

exit_strategy:
  strong_flip_conf: 0.25
  weak_flip_confirm_ticks: 2
  ratchet_arm_bps: 20.0
  ratchet_retrace_frac: 0.35
  forecast_tp_min_bps: 0.0
  shape_grace_seconds: 30
  dead_zone_regime_mult:
    cascade_up: 0.5
    cascade_down: 0.5
    chop: 1.5
    normal: 1.0
    trend_up: 0.8
    trend_down: 0.8

sizing:
  min_pct_equity: 1.0
  max_pct_equity: 6.0
  curve: linear         # linear | quadratic

execution:
  fee_rate_bps: 2.0
  slippage_bps: 2.0
  slippage_size_k: 0.5
  slippage_ref_notional_usd: 100000.0
  slippage_cap_bps: 30.0
  half_spread_bps: 0.5

projector:
  bps_per_unit_pressure: 8.0    # kernel gain (overwritten by KernelCalibrator)
  confidence_decay: 0.98
  mean_reversion_rate: 0.005
  sample_paths_n: 20
  default_horizon_seconds: 600.0
  default_sigma_bps: 8.0

projector_calibration:
  apply: true
  halflife_resolved: 300
  min_samples: 50
  window: 2000

calibration:
  apply: false          # flip to true once ≥ min_samples accumulated
  halflife_resolved: 500
  fast_halflife_resolved: 50
  min_samples: 30
  max_shrink: 0.6
  max_boost: 1.4
  regime_shift_threshold: 0.12
  enable_regime_conditional: false
  regime_min_samples: 15

shadow_ledgers:
  enabled: true
  starting_equity_usd: 50000
  size_base_pct: 2.0
  halflife_trades: 300
  min_trades_for_multiplier: 30
  trading_multiplier_max_boost: 1.4
  trading_multiplier_max_shrink: 0.6
  harvest_top_quartile_boost: 2.0

htf:
  enabled: true
  timeframe: 1h
  lookback_candles: 50
  fast_ema: 8
  slow_ema: 21
  poll_seconds: 60

regime:
  # thresholds and per-label multipliers (see RegimeClassifier)

recorder:
  include_depth: false    # true = full-fidelity replay (large files)

ui:
  host: 127.0.0.1
  port: 8765

logging:
  level: INFO
  dir: logs/
```

---

## 18. Daily Audit & Evolution Scheduler

**Files:** `src/audit/scheduler.py`, `src/audit/hypothesis.py`, `src/audit/evolution.py`, `scripts/spawn_next_generation.py`

Fires once at **00:00 UTC** without interrupting live trading.

### Hypothesis Generation
```python
# Scan session for systematic mispredictions
if conf > 0.80 AND hit_rate < 0.30:
    → "high-confidence council unreliable; reduce confidence_decay"

if tribe.long_skill >> tribe.short_skill by >0.15:
    → f"{tribe_id} directionally biased — check idle_bias and indicators"

if regime==cascade AND proj_sigma < actual_vol × 0.5:
    → "projector underestimates cascade vol; increase cascade multiplier"
```

### Variant Mutation Rules (`src/audit/evolution.py`)

Per strategy family, mutations target specific params:

| Family | Mutable Parameters |
|--------|--------------------|
| `trend_follower` | `min_persistent_move_bps`, `ema_fast`, `ema_slow` |
| `mean_reverter_mm` | `min_spread_bps`, `max_inventory`, `activation_rate` |
| `basis_arb` | `funding_threshold`, `arb_spread_bps` |
| `event_*` | `min_severity`, `cooldown_seconds`, `daily_fire_budget` |

Generated variants are written to `data/audit/YYYY-MM-DD/variants/` and scored in the next tournament run.

---

## 19. Formula Glossary

| Symbol | Formula | Description |
|--------|---------|-------------|
| `net_pressure` | `(buy_mass − sell_mass) / total_weight` | Signed council directional lean ∈ [−1, +1] |
| `uncertainty` | `1 − \|buy_mass − sell_mass\| / active_mass` | Council disagreement ∈ [0, 1] |
| `emove` | `steps_bps[-1] × conviction_boost` | Expected price move in bps at horizon end |
| `confidence` | `Σ(decay^h × agreement_h) / norm` | Confidence in the direction |
| `decay` | `exp(−elapsed_s / tribe_horizon_s)` | Per-tribe horizon fade |
| `regime_blend` | `sigmoid((ts − t_change) / 7.5)` | Smooth regime weight transition |
| `inventory_haircut` | `(1 − \|inv\|) / (1 − 0.5)` if `\|inv\| > 0.5` | Capacity-constrained tribe scaling |
| `skill_mult` (below chance) | `max_shrink + (1 − max_shrink) × 2 × ewma` | Skill → multiplier (shrink side) |
| `skill_mult` (above chance) | `1.0 + (max_boost − 1.0) × 2 × (ewma − 0.5)` | Skill → multiplier (boost side) |
| `α_ewma` | `1 − 0.5^(1/halflife)` | EWMA learning rate from half-life |
| `slippage_eff` | `slip_bps × (1 + k × size / ref)` | Size-scaled slippage |
| `ratchet_close` | `peak_unrealized × (1 − retrace_frac)` | Ratchet TP trigger level |
| `W[t][h]` update | `W *= exp(0.01 × delta)` | Per-(tribe, horizon) ledger weight update |
| `consensus_bps[h]` | `Σ(path_t,h × W_t,h × pool_w_t) / Σ(W_t,h × pool_w_t)` | Performance-weighted forecast |
| `delta_bps` (black swan) | `\|log(p_now / p_30s)\| × 10000` | 30 s price change in bps |
| `conviction_boost` | `1 + 0.5 × (harvest_mass / total_mass)` | Contrarian harvest amplification |

---

## 20. Running the Bot

### Prerequisites
```bash
pip install -r requirements.txt
```

### Paper Trading (live market, no real money)
```bash
python -m scripts.run_bot_ui
# Dashboard: http://127.0.0.1:8765
```

### Replay Historical Session
```bash
python -m scripts.run_replay
# or POST /api/replay/start  {"day": "2026-04-19", "speed": 10}
```

### Multi-Variant Tournament
```bash
python -m scripts.run_multi_variant
```

### Adversarial Stress Test
```bash
python -m scripts.run_adversarial
```

### Build Weekly Report
```bash
python -m scripts.build_report
```

### Hard Reset (clear all accumulated state)
Stop the process, remove calibration files, restart:
```bash
# Backups preserved automatically by Claude Code hard-reset helper
# Or manually:
rm data/calibration/tribe_skill.json data/calibration/projector_gain.json
python -m scripts.run_bot_ui
```

### Data Directories

| Path | Contents |
|------|----------|
| `data/events/` | Event tapes for replay (`YYYY-MM-DD.jsonl`) |
| `data/forecasts/` | Resolved forecast anchors |
| `data/ledger/` | Trade and mark-to-market records |
| `data/calibration/` | Persisted tribe skill + projector gain (survive restarts) |
| `data/audit/` | Daily audit reports + hypothesis variants |
| `data/replays/` | Completed replay runs (manifests + decisions) |
| `logs/` | Rotating daily bot logs |

---

*PAM Council is a research platform. All trading is paper-mode by default. No financial advice implied.*
