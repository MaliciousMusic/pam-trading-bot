"""Adversarial replay runner — drives spoof fixtures through the engine
stack and scores per-tribe skepticism.

For each fixture:
  1. Build the base event stream for the chosen day.
  2. Anchor the spoof at `anchor_offset_s` seconds into the day (default
     6h — well past warmup).
  3. Wrap the stream with `inject_spoof()`.
  4. Run a SprintEngine, capturing per-tick decisions to JSONL.
  5. Walk decisions in `[anchor_ts, anchor_ts + scoring_window_s]`,
     compute per-tribe net signed conviction. A tribe is "skeptical"
     iff its net signed conviction has the OPPOSITE sign of the bait.

Aggregated across N fixtures: per-tribe `skepticism ∈ [0, 1]` = mean of
the per-fixture booleans. Feeds `TribeCalibrator.apply_skepticism_prior`
as a starting EWMA — chronically-fooled tribes get a malus, consistently
skeptical ones get a boost.

Output: `data/replays/<run_id>/adversarial_report.json` plus a
`adversarial_report.md` rendering for human review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import orjson
from loguru import logger

from ..core.config import PROJECT_ROOT, settings
from .decision_log import DecisionLogWriter
from .feed import build_event_stream
from .harness import SprintEngine, _build_stack
from .kline_client import CachedHistoricalClient
from .spoof_inject import SpoofFixture, inject_spoof, list_fixtures


@dataclass
class TribeSpoofResult:
    tribe_id: str
    net_signed_conviction: float
    n_buy_votes: int
    n_sell_votes: int
    skeptical: bool


@dataclass
class FixtureResult:
    fixture_id: str
    description: str
    bait_side: str
    real_outcome_bps: float
    anchor_ts: str
    scoring_window_s: int
    n_ticks_in_window: int
    council_signed: float
    council_skeptical: bool
    tribes: list[TribeSpoofResult] = field(default_factory=list)


@dataclass
class AdversarialReport:
    run_id: str
    day: str
    symbol: str
    anchor_offset_s: int
    fixtures: list[FixtureResult]
    skepticism_by_tribe: dict[str, float]


def _bait_sign(bait_side: str) -> int:
    s = bait_side.lower()
    if s == "buy":
        return 1
    if s == "sell":
        return -1
    return 0


def _score_window(
    decisions_path: Path,
    anchor_ts: datetime,
    window_s: int,
    bait_side: str,
) -> tuple[dict[str, dict], float, int]:
    """Walk decisions.jsonl rows whose ts ∈ [anchor, anchor+window].

    Returns:
      per_tribe — {tribe_id: {"signed": float, "buys": int, "sells": int}}
      council   — net signed conviction summed across all tribes/ticks
      n_ticks   — number of decision rows inside the window
    """
    end_ts = anchor_ts + timedelta(seconds=window_s)
    per: dict[str, dict] = {}
    council = 0.0
    n_ticks = 0
    if not decisions_path.exists():
        return per, council, n_ticks
    with decisions_path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = orjson.loads(line)
            except Exception:
                continue
            ts_str = row.get("ts")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < anchor_ts or ts > end_ts:
                continue
            n_ticks += 1
            for v in row.get("votes", []):
                tid = v.get("tribe_id")
                side = v.get("side")
                conv = float(v.get("conviction", 0.0))
                slot = per.setdefault(
                    tid, {"signed": 0.0, "buys": 0, "sells": 0}
                )
                if side == "buy":
                    slot["signed"] += conv
                    slot["buys"] += 1
                    council += conv
                elif side == "sell":
                    slot["signed"] -= conv
                    slot["sells"] += 1
                    council -= conv
    return per, council, n_ticks


def _slice_for_fixture(
    bars: list[dict],
    funding: list[dict],
    anchor_ts: datetime,
    *,
    warmup_min: int,
    cooldown_min: int,
) -> tuple[list[dict], list[dict]]:
    """Trim bars/funding to a window around the spoof anchor.

    Replaying a full UTC day for every fixture is wasteful — the council
    only needs `warmup_min` of context to populate its rolling windows
    before the spoof fires, and `cooldown_min` past the scoring window to
    let positions exit naturally. This keeps each fixture run under a
    minute even at synth_rate=60."""
    start_ts = anchor_ts - timedelta(minutes=int(warmup_min))
    end_ts = anchor_ts + timedelta(minutes=int(cooldown_min))
    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)
    sliced_bars = [b for b in bars
                   if int(b["close_time_ms"]) >= start_ms
                   and int(b["open_time_ms"]) <= end_ms]
    sliced_funding = [f for f in funding
                      if start_ms <= int(f["funding_time_ms"]) <= end_ms]
    return sliced_bars, sliced_funding


async def _run_one_fixture(
    fixture: SpoofFixture,
    *,
    bars: list[dict],
    funding: list[dict],
    cfg: dict,
    run_root: Path,
    seed: int,
    synth_rate: int,
    symbol: str,
    anchor_ts: datetime,
    tribes_dir: Path | None,
    warmup_min: int,
    cooldown_min: int,
) -> FixtureResult:
    fixture_dir = run_root / "fixtures" / fixture.id
    fixture_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = fixture_dir / "decisions.jsonl"
    ledger_dir = fixture_dir / "ledger"

    sliced_bars, sliced_funding = _slice_for_fixture(
        bars, funding, anchor_ts,
        warmup_min=warmup_min,
        cooldown_min=cooldown_min + (fixture.scoring_window_s + 59) // 60,
    )
    tribes, state, projector, decision, executor, regime_clf = _build_stack(
        cfg, ledger_dir=ledger_dir, rng_seed=seed, tribes_dir=tribes_dir,
    )
    # Adversarial clean slate: each fixture is an isolated stress test, not
    # a real account. Disable every risk-halt knob so a single bad spoof
    # outcome can't deactivate the executor for the rest of the fixture's
    # tick stream (and so the per-tribe skepticism score reflects the
    # tribes' reaction to the bait, not the engine's risk plumbing).
    executor.max_drawdown_from_peak_pct = 0.0
    executor.max_daily_loss_pct = 0.0
    executor.max_session_dd_pct = 0.0
    executor.max_position_loss_pct = 0.0
    executor.black_swan_sigma = 0.0
    executor._halt_reason = None
    executor._session_halted = False
    executor._session_halt_ts = None
    executor._session_halt_reason = None
    executor._black_swan_cooldown_until = None
    executor._black_swan_resumed_at = None
    base = build_event_stream(sliced_bars, sliced_funding,
                              synth_rate=synth_rate, symbol=symbol)
    merged = inject_spoof(base, fixture, anchor_ts, symbol=symbol)

    with DecisionLogWriter(decisions_path) as writer:
        engine = SprintEngine(
            tribes=tribes, state=state, projector=projector,
            decision=decision, executor=executor,
            regime_classifier=regime_clf,
            tick_seconds=int(cfg["clock"]["tick_seconds"]),
            log_writer=writer,
        )
        engine.run(merged)

    per, council, n_ticks = _score_window(
        decisions_path, anchor_ts, fixture.scoring_window_s, fixture.bait_side,
    )
    bait = _bait_sign(fixture.bait_side)
    tribe_results = [
        TribeSpoofResult(
            tribe_id=tid,
            net_signed_conviction=slot["signed"],
            n_buy_votes=slot["buys"],
            n_sell_votes=slot["sells"],
            skeptical=(bait != 0 and (slot["signed"] * bait) < 0),
        )
        for tid, slot in sorted(per.items())
    ]
    return FixtureResult(
        fixture_id=fixture.id,
        description=fixture.description,
        bait_side=fixture.bait_side,
        real_outcome_bps=fixture.real_outcome_bps,
        anchor_ts=anchor_ts.isoformat(),
        scoring_window_s=fixture.scoring_window_s,
        n_ticks_in_window=n_ticks,
        council_signed=council,
        council_skeptical=(bait != 0 and (council * bait) < 0),
        tribes=tribe_results,
    )


async def run_adversarial(
    *,
    day: date,
    fixtures: list[SpoofFixture] | None = None,
    fixtures_dir: Path | None = None,
    seed: int = 42,
    synth_rate: int = 60,
    symbol: str = "BTCUSDT",
    cache_root: Path | None = None,
    outputs_root: Path | None = None,
    anchor_offset_s: int = 6 * 3600,
    tribes_dir: Path | None = None,
    run_id: str | None = None,
    warmup_min: int = 30,
    cooldown_min: int = 5,
) -> AdversarialReport:
    cfg = settings()
    repl = cfg.get("replay", {}) or {}
    if outputs_root is None:
        outputs_root = PROJECT_ROOT / repl.get("outputs_dir", "data/replays")
    if fixtures is None:
        fdir = fixtures_dir or (PROJECT_ROOT / "tests" / "fixtures" / "spoofs")
        fixtures = list_fixtures(fdir)
    if not fixtures:
        raise RuntimeError(f"no spoof fixtures found at {fixtures_dir}")

    if run_id is None:
        run_id = f"adversarial_{day.isoformat()}_{datetime.utcnow().strftime('%H%M%S')}"
    run_root = Path(outputs_root) / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    cache_dir = cache_root if cache_root is not None else (
        PROJECT_ROOT / repl.get("cache_dir", "data")
    )
    client = CachedHistoricalClient(cache_root=cache_dir, symbol=symbol)
    bars = await client.get_day_klines(day)
    funding = await client.get_day_funding(day)
    if not bars:
        raise RuntimeError(f"no bars cached for {day} — fetch first")

    anchor_ts = datetime.fromtimestamp(
        bars[0]["open_time_ms"] / 1000, tz=timezone.utc,
    ) + timedelta(seconds=anchor_offset_s)

    fixture_results: list[FixtureResult] = []
    for fx in fixtures:
        logger.info("[adversarial] running fixture {} (bait={})", fx.id, fx.bait_side)
        res = await _run_one_fixture(
            fx,
            bars=bars, funding=funding, cfg=cfg,
            run_root=run_root, seed=seed,
            synth_rate=synth_rate, symbol=symbol,
            anchor_ts=anchor_ts, tribes_dir=tribes_dir,
            warmup_min=warmup_min, cooldown_min=cooldown_min,
        )
        fixture_results.append(res)
        n_skep = sum(1 for t in res.tribes if t.skeptical)
        logger.info(
            "[adversarial] {} council={} ({:+.2f})  tribes_skeptical={}/{}",
            fx.id,
            "OK" if res.council_skeptical else "ERR",
            res.council_signed,
            n_skep, len(res.tribes),
        )

    by_tribe: dict[str, list[bool]] = {}
    for fr in fixture_results:
        for t in fr.tribes:
            by_tribe.setdefault(t.tribe_id, []).append(t.skeptical)
    skepticism_by_tribe = {
        tid: (sum(1 for x in s if x) / len(s)) if s else 0.0
        for tid, s in by_tribe.items()
    }

    report = AdversarialReport(
        run_id=run_id, day=day.isoformat(), symbol=symbol,
        anchor_offset_s=anchor_offset_s,
        fixtures=fixture_results,
        skepticism_by_tribe=skepticism_by_tribe,
    )

    payload = {
        "run_id": run_id,
        "day": day.isoformat(),
        "symbol": symbol,
        "anchor_offset_s": anchor_offset_s,
        "skepticism_by_tribe": skepticism_by_tribe,
        "fixtures": [
            {
                "fixture_id": fr.fixture_id,
                "description": fr.description,
                "bait_side": fr.bait_side,
                "real_outcome_bps": fr.real_outcome_bps,
                "anchor_ts": fr.anchor_ts,
                "scoring_window_s": fr.scoring_window_s,
                "n_ticks_in_window": fr.n_ticks_in_window,
                "council_signed": fr.council_signed,
                "council_skeptical": fr.council_skeptical,
                "tribes": [
                    {
                        "tribe_id": t.tribe_id,
                        "net_signed_conviction": t.net_signed_conviction,
                        "n_buy_votes": t.n_buy_votes,
                        "n_sell_votes": t.n_sell_votes,
                        "skeptical": t.skeptical,
                    }
                    for t in fr.tribes
                ],
            }
            for fr in fixture_results
        ],
    }
    (run_root / "adversarial_report.json").write_bytes(
        orjson.dumps(payload, option=orjson.OPT_INDENT_2)
    )
    (run_root / "adversarial_report.md").write_text(
        render_adversarial_md(report), encoding="utf-8"
    )
    logger.info("[adversarial] wrote report to {}", run_root)
    return report


def render_adversarial_md(report: AdversarialReport) -> str:
    lines = [
        "# Adversarial Replay Report",
        "",
        f"**run_id:** {report.run_id}  ",
        f"**day:** {report.day}  ",
        f"**symbol:** {report.symbol}  ",
        f"**spoof anchor:** +{report.anchor_offset_s}s into day  ",
        f"**fixtures:** {len(report.fixtures)}",
        "",
        "## Council vs spoof",
        "",
        "| fixture | bait | real outcome | council delta | council OK | tribes OK |",
        "|---------|------|--------------|---------------|------------|-----------|",
    ]
    for fr in report.fixtures:
        n_skep = sum(1 for t in fr.tribes if t.skeptical)
        lines.append(
            f"| {fr.fixture_id} | {fr.bait_side} "
            f"| {fr.real_outcome_bps:+.0f} bps "
            f"| {fr.council_signed:+.2f} "
            f"| {'OK' if fr.council_skeptical else 'ERR'} "
            f"| {n_skep}/{len(fr.tribes)} |"
        )
    lines += [
        "",
        "_Council OK means the council's net signed conviction during the "
        "scoring window pointed AGAINST the bait - i.e. the council "
        "wasn't fooled. Tribes OK counts how many individual tribes did "
        "the same._",
        "",
        "## Per-tribe skepticism",
        "",
        "Fraction of fixtures where each tribe voted against the bait. "
        "0.0 = always fooled - 1.0 = always skeptical.",
        "",
        "| tribe | skepticism |",
        "|-------|-----------|",
    ]
    for tid, score in sorted(report.skepticism_by_tribe.items(),
                             key=lambda x: -x[1]):
        bar = "#" * int(round(score * 20))
        lines.append(f"| `{tid}` | {score:.2f}  {bar} |")
    lines += [
        "",
        "## Per-fixture detail",
        "",
    ]
    for fr in report.fixtures:
        lines += [
            f"### {fr.fixture_id}",
            "",
            f"_{fr.description}_",
            "",
            f"- bait side: **{fr.bait_side}**",
            f"- real outcome: **{fr.real_outcome_bps:+.0f} bps** "
            f"over {fr.scoring_window_s}s",
            f"- decision rows in window: {fr.n_ticks_in_window}",
            f"- council net signed conviction: {fr.council_signed:+.2f} "
            f"({'skeptical' if fr.council_skeptical else 'baited'})",
            "",
            "| tribe | signed conviction | buys | sells | result |",
            "|-------|------------------:|-----:|------:|--------|",
        ]
        for t in fr.tribes:
            mark = "OK" if t.skeptical else "ERR"
            lines.append(
                f"| `{t.tribe_id}` | {t.net_signed_conviction:+.2f} "
                f"| {t.n_buy_votes} | {t.n_sell_votes} | {mark} |"
            )
        lines.append("")
    return "\n".join(lines)
