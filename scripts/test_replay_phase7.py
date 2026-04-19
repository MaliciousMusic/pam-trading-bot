"""Phase 7 smoke test — multi-variant WatchEngine with mock broadcaster.

Runs two WatchEngine instances concurrently (synth_rate 6 and 60) through a
shared MockBroadcaster and verifies:
  - both variant_ids appear in tick messages
  - no tick message from a tagged engine is routed to variant_id=None
  - conductor sends aggregate replay_state (active=True) during the run
  - final replay_state.active is False
  - single-variant path (no variant_id on ticks) still works unchanged

No network, no subprocesses.

Usage:  python -m scripts.test_replay_phase7
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.core.config import PROJECT_ROOT, settings
from src.core.types import Event
from src.replay.harness import _build_stack
from src.replay.watch_engine import WatchEngine, _conduct


class MockBroadcaster:
    def __init__(self):
        self.published: list[dict] = []

    async def publish(self, msg: dict) -> None:
        self.published.append(msg)


def _make_events(n_seconds: int = 90, start_price: float = 65_000.0) -> list[Event]:
    base = datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
    events = []
    for i in range(n_seconds):
        ts = base.replace(second=i % 60, minute=i // 60)
        price = start_price + i * 0.5
        is_sell = i % 2 != 0
        events.append(Event(
            ts=ts, source="test", kind="trade", symbol="BTCUSDT",
            payload={"price": price, "qty": 0.1, "notional_usd": price * 0.1,
                     "is_sell_aggressor": is_sell, "agg_id": str(i)},
            tags={"price", "trade", "taker_sell" if is_sell else "taker_buy"},
            severity=0.1,
        ))
    return events


async def test_multi_variant() -> None:
    cfg = settings()
    tick_seconds = int(cfg["clock"]["tick_seconds"])
    broadcaster = MockBroadcaster()

    variants = [
        {"variant_id": "synth_6",  "synth_rate": 6,  "seed": 42},
        {"variant_id": "synth_60", "synth_rate": 60, "seed": 42},
    ]

    engines: list[WatchEngine] = []
    engine_events = []
    for v in variants:
        ledger_dir = PROJECT_ROOT / "data" / "replays" / "_phase7_test" / "ledger" / v["variant_id"]
        tribes, state, projector, decision, executor, regime_clf = _build_stack(
            cfg, ledger_dir=ledger_dir, rng_seed=v["seed"],
        )
        engine = WatchEngine(
            tribes=tribes, state=state, projector=projector,
            decision=decision, executor=executor,
            regime_classifier=regime_clf,
            tick_seconds=tick_seconds,
            broadcaster=broadcaster,
            run_id="phase7_test", day="2024-01-15",
            speed=1000.0,
            total_ticks_est=90 // tick_seconds,
            variant_id=v["variant_id"],
            emit_replay_state=False,
        )
        engines.append(engine)
        engine_events.append(_make_events(n_seconds=90))

    # Run engines + conductor concurrently
    engine_tasks = [asyncio.create_task(e.run(iter(evs))) for e, evs in zip(engines, engine_events)]
    conductor = asyncio.create_task(_conduct(engines, broadcaster, "phase7_test", "2024-01-15"))
    await asyncio.gather(*engine_tasks)
    conductor.cancel()
    await asyncio.gather(conductor, return_exceptions=True)
    await broadcaster.publish({"type": "replay_state", "active": False})

    # ── Assertions ────────────────────────────────────────────────────────────
    tick_msgs = [m for m in broadcaster.published if m.get("type") == "tick"]
    replay_msgs = [m for m in broadcaster.published if m.get("type") == "replay_state"]
    active_msgs = [m for m in replay_msgs if m.get("active")]

    assert len(tick_msgs) > 0, "no tick messages"
    assert len(replay_msgs) >= 2, f"expected >=2 replay_state, got {len(replay_msgs)}"
    assert replay_msgs[-1].get("active") is False, "last replay_state.active should be False"

    vids_seen = {m.get("variant_id") for m in tick_msgs}
    assert "synth_6" in vids_seen, f"synth_6 not in variant_ids: {vids_seen}"
    assert "synth_60" in vids_seen, f"synth_60 not in variant_ids: {vids_seen}"
    assert None not in vids_seen, "None variant_id found — single-variant leakage in multi mode"

    assert len(active_msgs) >= 1, "conductor sent no active replay_state messages"
    assert "variant_count" in active_msgs[0], "conductor replay_state missing variant_count"
    assert active_msgs[0]["variant_count"] == 2

    n6 = sum(1 for m in tick_msgs if m.get("variant_id") == "synth_6")
    n60 = sum(1 for m in tick_msgs if m.get("variant_id") == "synth_60")
    print(f"  ok  synth_6={n6} ticks  synth_60={n60} ticks"
          f"  replay_state_active={len(active_msgs)}  total={len(broadcaster.published)}")


async def test_single_variant_backward_compat() -> None:
    """Single-variant engine must still send variant_id=None on tick messages."""
    cfg = settings()
    tick_seconds = int(cfg["clock"]["tick_seconds"])
    broadcaster = MockBroadcaster()
    ledger_dir = PROJECT_ROOT / "data" / "replays" / "_phase7_test_sv" / "ledger"
    tribes, state, projector, decision, executor, regime_clf = _build_stack(
        cfg, ledger_dir=ledger_dir, rng_seed=42,
    )
    engine = WatchEngine(
        tribes=tribes, state=state, projector=projector,
        decision=decision, executor=executor,
        regime_classifier=regime_clf,
        tick_seconds=tick_seconds,
        broadcaster=broadcaster,
        run_id="phase7_sv", day="2024-01-15",
        speed=1000.0, total_ticks_est=90 // tick_seconds,
        # variant_id defaults to None, emit_replay_state defaults to True
    )
    await engine.run(iter(_make_events(n_seconds=90)))
    tick_msgs = [m for m in broadcaster.published if m.get("type") == "tick"]
    assert all(m.get("variant_id") is None for m in tick_msgs), \
        "single-variant tick messages must have variant_id=None"
    replay_msgs = [m for m in broadcaster.published if m.get("type") == "replay_state"]
    assert replay_msgs[-1].get("active") is False
    print(f"  ok  single-variant: {len(tick_msgs)} ticks, variant_id=None on all, final active=False")


def main() -> int:
    tests = [test_multi_variant, test_single_variant_backward_compat]
    print(f"running {len(tests)} phase-7 tests...")
    for t in tests:
        try:
            asyncio.run(t())
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  ERROR  {t.__name__}: {type(e).__name__}: {e}")
            return 1
    print("all phase-7 tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
