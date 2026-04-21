"""Variant council tournament.

End-of-day pipeline:

    yesterday's ledger
        → daily audit report
        → hypothesis.generate()          (single-param, per-tribe mutations)
        → evolution.spawn_from_hypotheses() + write_hypothesis_variants()
        → replay today's (or yesterday's) event tape through:
              parent council + every variant council
        → leaderboard sorted by equity_delta
        → flag a promotion candidate if winner beats parent by threshold

Nothing is swapped in automatically: a winning variant only writes its
tribes into `data/tournaments/<gen>/promoted/`. Promotion into
`config/tribes/` is an explicit action (script flag or manual copy) so
one bad day can't poison the live council overnight.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..core import serde
import yaml
from loguru import logger

from ..core.config import CONFIG_DIR, PROJECT_ROOT
from ..decision.engine import DecisionEngine
from ..execution.ledger import Ledger
from ..execution.paper import PaperExecutor
from ..projector.chess import ChessProjector
from ..regime.classifier import RegimeClassifier
from ..tribes.derive import apply_and_log
from ..tribes.loader import TRIBES_DIR
from ..tribes.market_state import MarketState
from ..tribes.spec import TribeSpec
from ..tribes.strategies import available as available_strategies
from ..tribes.tribe import Tribe
from .evolution import (
    VARIANTS_DIR,
    spawn_from_hypotheses,
    write_hypothesis_variants,
)
from .daily import audit_day
from .hypothesis import Hypothesis, generate
from .replay import EVENTS_DIR, ReplayEngine, load_events


TOURNAMENTS_DIR = PROJECT_ROOT / "data" / "tournaments"


@dataclass(slots=True)
class LeaderboardRow:
    label: str                     # "parent" | "variant_0" | ...
    variant_dir: str | None        # relative path, or None for parent
    hypothesis: dict | None        # description of the mutation, or None
    ticks: int
    trades: int
    wins: int
    win_rate: float
    start_equity_usd: float
    end_equity_usd: float
    equity_delta_usd: float
    equity_delta_pct: float

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "variant_dir": self.variant_dir,
            "hypothesis": self.hypothesis,
            "ticks": self.ticks,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "start_equity_usd": self.start_equity_usd,
            "end_equity_usd": self.end_equity_usd,
            "equity_delta_usd": self.equity_delta_usd,
            "equity_delta_pct": self.equity_delta_pct,
        }


def _load_specs_from_dir(path: Path) -> list[TribeSpec]:
    """Same validation pass as loader.load_all but from an arbitrary dir.
    Returns specs without weight derivation (caller applies it)."""
    if not path.exists():
        return []
    registered = set(available_strategies())
    specs: list[TribeSpec] = []
    for p in sorted(path.glob("*.yaml")):
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        spec = TribeSpec.model_validate(raw)
        if spec.strategy.family not in registered:
            logger.warning("[tournament] unknown strategy {} in {}",
                           spec.strategy.family, p.name)
            continue
        specs.append(spec)
    return specs


def _build_stack(
    specs: list[TribeSpec],
    settings: dict,
    *,
    ledger: Ledger,
) -> tuple[list[Tribe], ChessProjector, DecisionEngine, PaperExecutor]:
    """Fresh engine components for a single replay. Weight derivation is
    applied per-variant so each council is internally-consistent."""
    apply_and_log(specs)
    tribes = [Tribe(s) for s in specs]
    projector = ChessProjector(
        tribes=tribes,
        steps=int(settings["clock"]["projector_steps"]),
        tick_seconds=int(settings["clock"]["tick_seconds"]),
        bps_per_unit_pressure=float(settings["projector"]["bps_per_unit_pressure"]),
        confidence_decay=float(settings["projector"]["confidence_decay"]),
    )
    decision = DecisionEngine(settings)
    executor = PaperExecutor(
        starting_equity_usd=float(settings.get("base_capital_usd", 5000)),
        fee_rate_bps=float(settings["execution"]["fee_rate_bps"]),
        slippage_bps=float(settings["execution"]["slippage_bps"]),
        max_concurrent_positions=int(settings["risk"]["max_concurrent_positions"]),
        max_total_notional_pct=float(settings["risk"]["max_total_notional_pct"]),
        max_hold_seconds=int(settings["risk"]["max_hold_seconds"]),
        exit_dead_zone_bps=float(settings["decision"].get("exit_dead_zone_bps", 0.5)),
        ledger=ledger,
    )
    return tribes, projector, decision, executor


def _score_council(
    label: str,
    specs: list[TribeSpec],
    settings: dict,
    tape: list,   # materialized once by caller
    out_dir: Path,
    hypothesis: dict | None,
    variant_dir: str | None,
) -> LeaderboardRow:
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(directory=out_dir)
    tribes, projector, decision, executor = _build_stack(specs, settings, ledger=ledger)
    state = MarketState()
    # Phase E — replays must use the same regime overlay as the live engine,
    # otherwise variants get scored against a slightly different forecast
    # than the one they'd see in production.
    regime_classifier = RegimeClassifier(settings.get("regime", {}) or {})
    engine = ReplayEngine(
        tribes=tribes, state=state, projector=projector,
        decision=decision, executor=executor,
        tick_seconds=int(settings["clock"]["tick_seconds"]),
        regime_classifier=regime_classifier,
    )
    result = engine.run(iter(tape))
    row = LeaderboardRow(
        label=label,
        variant_dir=variant_dir,
        hypothesis=hypothesis,
        ticks=result["ticks"],
        trades=result["trades"],
        wins=result["wins"],
        win_rate=result["win_rate"],
        start_equity_usd=result["start_equity_usd"],
        end_equity_usd=result["end_equity_usd"],
        equity_delta_usd=result["equity_delta_usd"],
        equity_delta_pct=result["equity_delta_pct"],
    )
    logger.info(
        "[tournament] {:<20} Δeq=${:+.2f} ({:+.2%})  trades={}  wr={:.1%}",
        label, row.equity_delta_usd, row.equity_delta_pct,
        row.trades, row.win_rate,
    )
    return row


def run_tournament(
    *,
    event_day: str,
    audit_day_str: str,
    settings: dict,
    parent_tribes_dir: Path | None = None,
    max_variants: int = 6,
    promotion_threshold_usd: float = 0.0,
    tag: str | None = None,
) -> dict:
    """End-to-end tournament on one day's tape.

    Args:
      event_day: YYYY-MM-DD of the event tape to replay.
      audit_day_str: YYYY-MM-DD of the ledger to audit (hypothesis source).
        Often the same day.
      settings: full settings.yaml as a dict.
      parent_tribes_dir: directory holding the current "parent" council.
        Defaults to config/tribes/.
      max_variants: upper bound on children spawned from hypotheses.
      promotion_threshold_usd: winning variant must beat parent by more
        than this $ equity delta to be flagged for promotion.
      tag: generation tag; defaults to timestamp.
    """
    parent_dir = parent_tribes_dir or TRIBES_DIR
    tag = tag or datetime.now(timezone.utc).strftime("tourn_%Y%m%d_%H%M%S")
    out_root = TOURNAMENTS_DIR / tag
    out_root.mkdir(parents=True, exist_ok=True)

    tape_path = EVENTS_DIR / f"{event_day}.jsonl"
    if not tape_path.exists():
        logger.error("[tournament] no event tape at {}", tape_path)
        return {"error": "no tape", "path": str(tape_path)}

    # Materialize the tape once — every variant replays the identical
    # ordered list. (The tape is bounded by a day of data.)
    tape = list(load_events(tape_path))
    if not tape:
        logger.error("[tournament] tape is empty: {}", tape_path)
        return {"error": "empty tape", "path": str(tape_path)}
    logger.info("[tournament] loaded {} events from {}", len(tape), tape_path.name)

    parent_specs = _load_specs_from_dir(parent_dir)
    if not parent_specs:
        logger.error("[tournament] parent tribes dir empty: {}", parent_dir)
        return {"error": "no parent tribes"}

    # 1) Audit yesterday to get hypotheses.
    report = audit_day(audit_day_str,
                       horizon_seconds=int(settings.get("audit", {}).get("horizon_seconds", 60)))
    hypotheses: list[Hypothesis] = generate(report, parent_specs)[:max_variants]

    # 2) Spawn + persist variants (reuses existing evolution scaffold).
    if hypotheses:
        pairs = spawn_from_hypotheses(parent_specs, hypotheses)
        write_hypothesis_variants(pairs, tag)
    else:
        logger.info("[tournament] no hypotheses generated — parent-only run")
        pairs = []

    # 3) Replay parent and every variant.
    rows: list[LeaderboardRow] = []
    rows.append(_score_council(
        label="parent",
        specs=[s.model_copy(deep=True) for s in parent_specs],
        settings=settings,
        tape=tape,
        out_dir=out_root / "parent",
        hypothesis=None,
        variant_dir=None,
    ))
    for i, (h, variant_specs) in enumerate(pairs):
        rows.append(_score_council(
            label=f"variant_{i}",
            specs=[s.model_copy(deep=True) for s in variant_specs],
            settings=settings,
            tape=tape,
            out_dir=out_root / f"variant_{i}",
            hypothesis={
                "tribe_id": h.tribe_id,
                "field": h.field,
                "current": h.current,
                "proposed": h.proposed,
                "rationale": h.rationale,
                "priority": h.priority,
            },
            variant_dir=str((VARIANTS_DIR / tag / f"variant_{i}").relative_to(VARIANTS_DIR)),
        ))

    # 4) Leaderboard sorted by equity delta.
    rows.sort(key=lambda r: -r.equity_delta_usd)
    parent_row = next(r for r in rows if r.label == "parent")
    winner = rows[0]
    promote = (
        winner.label != "parent"
        and winner.equity_delta_usd - parent_row.equity_delta_usd > promotion_threshold_usd
    )

    leaderboard = {
        "tag": tag,
        "event_day": event_day,
        "audit_day": audit_day_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parent_equity_delta_usd": parent_row.equity_delta_usd,
        "winner_label": winner.label,
        "winner_beats_parent_usd": winner.equity_delta_usd - parent_row.equity_delta_usd,
        "promote_flagged": promote,
        "promotion_threshold_usd": promotion_threshold_usd,
        "rows": [r.to_dict() for r in rows],
    }
    (out_root / "leaderboard.json").write_bytes(
        serde.dumps_indent(leaderboard)
    )
    logger.info(
        "[tournament] winner={} (Δeq=${:+.2f})  vs parent=${:+.2f}  promote={}",
        winner.label, winner.equity_delta_usd, parent_row.equity_delta_usd, promote,
    )

    # 5) Stage a promotion candidate for explicit user activation.
    if promote and winner.variant_dir:
        src_dir = VARIANTS_DIR / winner.variant_dir
        promoted_dir = out_root / "promoted"
        promoted_dir.mkdir(exist_ok=True)
        for f in src_dir.glob("*.yaml"):
            (promoted_dir / f.name).write_bytes(f.read_bytes())
        (promoted_dir / "_HOW_TO_ACTIVATE.txt").write_text(
            f"Winner {winner.label} beat parent by "
            f"${winner.equity_delta_usd - parent_row.equity_delta_usd:+.2f} "
            f"on tape {event_day}.\n\n"
            "To activate, copy these YAMLs over config/tribes/ — promotion\n"
            "is manual by design so one lucky day can't poison the live\n"
            "council.\n",
            encoding="utf-8",
        )
        logger.info("[tournament] promoted candidate → {}", promoted_dir)

    return leaderboard


def load_tape(event_day: str) -> Iterable:
    """Public helper for scripts to stream a tape without loading tournament."""
    return load_events(EVENTS_DIR / f"{event_day}.jsonl")
