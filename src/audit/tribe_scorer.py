"""Per-tribe trading scorer — central aggregator for shadow-ledger metrics.

Each closed shadow trade + each tick's mark-to-market lands here; the
scorer maintains EWMA (halflife ≈ 300 trades) summaries per tribe AND
per side (long vs short) so downstream consumers can ask: "is this
tribe's long skill real, or is it the short side alone carrying it?"

Metrics tracked per tribe:
  long_skill           EWMA of (realized_bps > 0) on long closes
  short_skill          EWMA of (realized_bps > 0) on short closes
  realized_pnl_bps     EWMA of realized bps over all closes
  unrealized_pnl_bps   latest open-book bps across tribes (marked each tick)
  sharpe_ewma          EWMA(mean) / sqrt(EWMA(var)) of per-trade bps
  sl_discipline        share of SL-hit closes (lower = better cutting)
  tp_capture           share of TP-hit closes
  max_drawdown_pct     running DD from peak equity in %
  avg_hold_s           EWMA of holding time in seconds
  edge_over_cost_bps   EWMA(realized_bps) minus round-trip fees
  net_score            composite sort key in [0, 1] for top-quartile boost

`trading_multiplier(tid, side)` fuses long/short skill into a bounded
multiplier for the aggregator voting layer (PIECE 3 wires this).
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable

from loguru import logger

from ..core.types import Side
from ..execution.shadow import _ShadowTrade


@dataclass(slots=True)
class _SideEwma:
    """Running EWMA of (hit, realized_bps, held_s) on one side."""
    n: int = 0
    hit_ewma: float = 0.5          # fraction-of-wins on this side, 0..1
    bps_mean_ewma: float = 0.0     # mean realized bps
    bps_var_ewma: float = 0.0      # variance proxy for Sharpe denominator
    held_s_ewma: float = 0.0
    sl_hits: int = 0
    tp_hits: int = 0


@dataclass(slots=True)
class _TribeScore:
    tribe_id: str
    # Long / short books tracked separately — the aggregator asks per-side.
    long: _SideEwma
    short: _SideEwma
    # Equity trail from the shadow ledger (mark-driven).
    equity_usd: float = 50_000.0
    start_equity_usd: float = 50_000.0
    peak_equity_usd: float = 50_000.0
    max_drawdown_pct: float = 0.0
    unrealized_pnl_bps: float = 0.0
    # Roll-up across both sides.
    total_trades: int = 0
    total_wins: int = 0
    realized_bps_ewma: float = 0.0
    held_s_ewma: float = 0.0
    fee_bps_ewma: float = 0.0


class TribeTradingScorer:
    """EWMA-based per-tribe trading scorecard fed by TribeShadowLedger.

    Halflife is expressed in trades (not ticks) so fast-trading archetypes
    don't drown slow ones. `halflife_trades=300` means the last ~300 trades
    dominate the estimate; beyond that, contribution halves.
    """

    def __init__(
        self,
        *,
        halflife_trades: int = 300,
        min_trades_for_multiplier: int = 30,
        trading_multiplier_max_boost: float = 1.4,
        trading_multiplier_max_shrink: float = 0.6,
        composite_weights: dict[str, float] | None = None,
    ) -> None:
        self._halflife = max(1, int(halflife_trades))
        self._alpha = 1.0 - 0.5 ** (1.0 / float(self._halflife))
        self._min_trades = max(1, int(min_trades_for_multiplier))
        self._max_boost = max(1.0, float(trading_multiplier_max_boost))
        self._max_shrink = max(0.0, min(1.0, float(trading_multiplier_max_shrink)))
        # Composite net-score weights. Defaults emphasize realized edge and
        # Sharpe, temper drawdown, lightly credit SL discipline and TP
        # capture. All contributions clamp into [0, 1] before weighting.
        cw = composite_weights or {}
        self._w_edge = float(cw.get("edge", 0.40))
        self._w_sharpe = float(cw.get("sharpe", 0.25))
        self._w_dd = float(cw.get("drawdown", 0.15))
        self._w_disc = float(cw.get("discipline", 0.10))
        self._w_cap = float(cw.get("capture", 0.10))
        self._scores: OrderedDict[str, _TribeScore] = OrderedDict()

    # --- ingest -------------------------------------------------------

    def ingest_closed(self, trades: Iterable[_ShadowTrade]) -> None:
        """Fold each closed shadow trade into its tribe's EWMAs. Called by
        the engine each tick with whatever the ledger drained."""
        for tr in trades:
            sc = self._ensure(tr.tribe_id)
            side = sc.long if tr.side is Side.BUY else sc.short
            hit = 1.0 if tr.net_usd > 0 else 0.0
            bps = float(tr.realized_bps)
            held = float(tr.held_s)
            a = self._alpha
            side.n += 1
            side.hit_ewma += a * (hit - side.hit_ewma)
            # Welford-ish streaming variance on EWMA mean — not statistically
            # pure but good enough for a Sharpe rank; the scorer is a sort
            # key, not a p-value.
            prev_mean = side.bps_mean_ewma
            side.bps_mean_ewma += a * (bps - prev_mean)
            resid = bps - side.bps_mean_ewma
            side.bps_var_ewma += a * (resid * resid - side.bps_var_ewma)
            side.held_s_ewma += a * (held - side.held_s_ewma)
            if tr.hit_tp:
                side.tp_hits += 1
            if tr.hit_sl:
                side.sl_hits += 1
            sc.total_trades += 1
            if tr.net_usd > 0:
                sc.total_wins += 1
            sc.realized_bps_ewma += a * (bps - sc.realized_bps_ewma)
            sc.held_s_ewma += a * (held - sc.held_s_ewma)
            fee_bps = (tr.fee_usd / tr.size_usd * 1e4) if tr.size_usd > 0 else 0.0
            sc.fee_bps_ewma += a * (fee_bps - sc.fee_bps_ewma)

    def ingest_mark(
        self,
        tribe_id: str,
        equity_usd: float,
        unrealized_bps: float,
        peak_equity_usd: float,
    ) -> None:
        """Per-tick equity + unrealized mark from the shadow ledger. Keeps
        drawdown + live-pnl numbers fresh even when the tribe hasn't closed
        a trade this tick."""
        sc = self._ensure(tribe_id)
        sc.equity_usd = float(equity_usd)
        sc.peak_equity_usd = max(float(peak_equity_usd), sc.peak_equity_usd)
        sc.unrealized_pnl_bps = float(unrealized_bps)
        if sc.peak_equity_usd > 0:
            dd = (sc.peak_equity_usd - sc.equity_usd) / sc.peak_equity_usd * 100.0
            sc.max_drawdown_pct = max(sc.max_drawdown_pct, max(0.0, dd))

    def seed_start_equity(self, tribe_id: str, start_equity_usd: float) -> None:
        """Stamp the ledger's starting equity onto the score so pnl_bps and
        DD math lock to the real baseline instead of the default 50k."""
        sc = self._ensure(tribe_id)
        sc.start_equity_usd = float(start_equity_usd)
        sc.equity_usd = float(start_equity_usd)
        sc.peak_equity_usd = float(start_equity_usd)

    def _ensure(self, tribe_id: str) -> _TribeScore:
        sc = self._scores.get(tribe_id)
        if sc is None:
            sc = _TribeScore(
                tribe_id=tribe_id,
                long=_SideEwma(),
                short=_SideEwma(),
            )
            self._scores[tribe_id] = sc
        return sc

    # --- query --------------------------------------------------------

    def trading_multiplier(self, tribe_id: str, side: Side) -> float:
        """Side-aware trading-skill multiplier bounded by
        [max_shrink, max_boost]. Uses that side's EWMA hit-rate as the signal,
        not lifetime realized PnL — a tribe that was great last month but
        is losing right now should get down-weighted immediately.

        Returns 1.0 (neutral) when:
          - the tribe is unknown
          - it has fewer than min_trades_for_multiplier trades on this side
          - side is ABSTAIN (no meaningful direction to score)
        """
        if side is Side.ABSTAIN:
            return 1.0
        sc = self._scores.get(tribe_id)
        if sc is None:
            return 1.0
        side_ewma = sc.long if side is Side.BUY else sc.short
        if side_ewma.n < self._min_trades:
            return 1.0
        hit = max(0.0, min(1.0, float(side_ewma.hit_ewma)))
        # Piecewise linear map: 0.0 → max_shrink, 0.5 → 1.0, 1.0 → max_boost.
        if hit >= 0.5:
            return 1.0 + (hit - 0.5) * 2.0 * (self._max_boost - 1.0)
        return self._max_shrink + hit * 2.0 * (1.0 - self._max_shrink)

    def top_quartile_tribes(self) -> set[str]:
        """Tribes whose composite net_score sits in the top 25%. Used by the
        chess harvest-mass boost to double-weight the bloc they cover when
        they land on the contrarian side."""
        ranked = [
            (tid, self._composite_score(sc))
            for tid, sc in self._scores.items()
            if sc.total_trades >= self._min_trades
        ]
        if len(ranked) < 4:
            return set()
        ranked.sort(key=lambda r: -r[1])
        cutoff = max(1, len(ranked) // 4)
        return {tid for tid, _ in ranked[:cutoff]}

    def _composite_score(self, sc: _TribeScore) -> float:
        """Roll long/short + aggregate EWMAs into a single [0, 1] sort key.
        Defined as a weighted sum of normalized components — weights are
        knob-tunable via composite_weights."""
        edge = max(0.0, min(1.0, 0.5 + sc.realized_bps_ewma / 40.0))
        mean_bps = 0.5 * (sc.long.bps_mean_ewma + sc.short.bps_mean_ewma)
        var = max(1e-6, 0.5 * (sc.long.bps_var_ewma + sc.short.bps_var_ewma))
        sharpe_raw = mean_bps / (var ** 0.5)
        sharpe = max(0.0, min(1.0, 0.5 + sharpe_raw / 4.0))
        dd = max(0.0, min(1.0, 1.0 - sc.max_drawdown_pct / 20.0))
        # Discipline = 1 - SL-rate on losing trades (lower SL share = cutting
        # well; high SL share on a winning tribe is just noise). We blend
        # both sides' sl_hits as a global share of closes.
        total_sl = sc.long.sl_hits + sc.short.sl_hits
        total_tp = sc.long.tp_hits + sc.short.tp_hits
        disc = 1.0 - min(1.0, total_sl / max(1, sc.total_trades))
        cap = min(1.0, total_tp / max(1, sc.total_trades))
        return (
            self._w_edge * edge
            + self._w_sharpe * sharpe
            + self._w_dd * dd
            + self._w_disc * disc
            + self._w_cap * cap
        )

    def snapshot(self) -> dict:
        """Serialize everything the UI's Tribe Trading Heatmap renders,
        plus a GLOBAL row aggregating across tribes."""
        rows = []
        for tid, sc in self._scores.items():
            rt_fees_bps = 2.0 * sc.fee_bps_ewma
            rows.append({
                "tribe_id": tid,
                "long_skill": _finite(sc.long.hit_ewma) if sc.long.n else None,
                "short_skill": _finite(sc.short.hit_ewma) if sc.short.n else None,
                "long_trades": sc.long.n,
                "short_trades": sc.short.n,
                "total_trades": sc.total_trades,
                "total_wins": sc.total_wins,
                "realized_pnl_bps": _finite(sc.realized_bps_ewma),
                "unrealized_pnl_bps": _finite(sc.unrealized_pnl_bps),
                "sharpe_ewma": _finite(self._sharpe(sc)),
                "sl_discipline": _finite(self._sl_discipline(sc)),
                "tp_capture": _finite(self._tp_capture(sc)),
                "max_drawdown_pct": _finite(sc.max_drawdown_pct),
                "avg_hold_s": _finite(sc.held_s_ewma),
                "edge_over_cost_bps": _finite(sc.realized_bps_ewma - rt_fees_bps),
                "net_score": _finite(self._composite_score(sc)),
                "equity_usd": _finite(sc.equity_usd),
                "start_equity_usd": _finite(sc.start_equity_usd),
                "peak_equity_usd": _finite(sc.peak_equity_usd),
                "trading_mult_long": self.trading_multiplier(tid, Side.BUY),
                "trading_mult_short": self.trading_multiplier(tid, Side.SELL),
            })
        # GLOBAL row — aggregate across all tribes (trades + PnL).
        total_trades = sum(sc.total_trades for sc in self._scores.values())
        total_wins = sum(sc.total_wins for sc in self._scores.values())
        total_equity = sum(sc.equity_usd for sc in self._scores.values())
        total_start = sum(sc.start_equity_usd for sc in self._scores.values())
        long_n = sum(sc.long.n for sc in self._scores.values())
        short_n = sum(sc.short.n for sc in self._scores.values())
        long_hit = (
            sum(sc.long.hit_ewma * sc.long.n for sc in self._scores.values())
            / long_n
        ) if long_n else None
        short_hit = (
            sum(sc.short.hit_ewma * sc.short.n for sc in self._scores.values())
            / short_n
        ) if short_n else None
        global_realized = (
            sum(sc.realized_bps_ewma * sc.total_trades for sc in self._scores.values())
            / total_trades
        ) if total_trades else 0.0
        global_row = {
            "tribe_id": "GLOBAL",
            "long_skill": _finite(long_hit),
            "short_skill": _finite(short_hit),
            "long_trades": long_n,
            "short_trades": short_n,
            "total_trades": total_trades,
            "total_wins": total_wins,
            "realized_pnl_bps": _finite(global_realized),
            "unrealized_pnl_bps": _finite(
                sum(sc.unrealized_pnl_bps for sc in self._scores.values())
                / max(1, len(self._scores))
            ),
            "sharpe_ewma": None,
            "sl_discipline": None,
            "tp_capture": None,
            "max_drawdown_pct": _finite(
                max((sc.max_drawdown_pct for sc in self._scores.values()), default=0.0)
            ),
            "avg_hold_s": _finite(
                sum(sc.held_s_ewma * sc.total_trades for sc in self._scores.values())
                / max(1, total_trades)
            ) if total_trades else 0.0,
            "edge_over_cost_bps": None,
            "net_score": None,
            "equity_usd": _finite(total_equity),
            "start_equity_usd": _finite(total_start),
            "peak_equity_usd": _finite(total_equity),
            "trading_mult_long": 1.0,
            "trading_mult_short": 1.0,
        }
        return {
            "halflife_trades": self._halflife,
            "min_trades_for_multiplier": self._min_trades,
            "max_boost": self._max_boost,
            "max_shrink": self._max_shrink,
            "top_quartile": sorted(self.top_quartile_tribes()),
            "tribes": rows,
            "global": global_row,
        }

    @staticmethod
    def _sharpe(sc: _TribeScore) -> float:
        mean_bps = 0.5 * (sc.long.bps_mean_ewma + sc.short.bps_mean_ewma)
        var = 0.5 * (sc.long.bps_var_ewma + sc.short.bps_var_ewma)
        if var <= 0:
            return 0.0
        return mean_bps / (var ** 0.5)

    @staticmethod
    def _sl_discipline(sc: _TribeScore) -> float:
        total_sl = sc.long.sl_hits + sc.short.sl_hits
        if sc.total_trades <= 0:
            return 0.0
        return 1.0 - min(1.0, total_sl / sc.total_trades)

    @staticmethod
    def _tp_capture(sc: _TribeScore) -> float:
        total_tp = sc.long.tp_hits + sc.short.tp_hits
        if sc.total_trades <= 0:
            return 0.0
        return total_tp / sc.total_trades


def _finite(x):
    """Guard against NaN / inf leaking into UI JSON."""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if xf != xf or xf in (float("inf"), float("-inf")):
        return None
    return xf
