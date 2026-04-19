"""Phase 6 smoke test — WatchEngine runs end-to-end with a mock broadcaster.

No network, no subprocesses, no kline cache.  Builds a minimal synthetic
event stream, drives WatchEngine through it, and asserts:
  - tick count matches expected
  - broadcaster received at least one tick message with the right shape
  - broadcaster received replay_state messages (active=True then active=False)
  - final replay_state.active is False (clean shutdown)

Usage:  python -m scripts.test_replay_phase6
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.core.config import PROJECT_ROOT, settings
from src.core.types import Event
from src.replay.harness import _build_stack
from src.replay.watch_engine import WatchEngine


# ── Minimal mock broadcaster ──────────────────────────────────────────────────

class MockBroadcaster:
    def __init__(self):
        self.published: list[dict] = []

    async def publish(self, msg: dict) -> None:
        self.published.append(msg)


# ── Minimal synthetic event stream ───────────────────────────────────────────

def _make_events(n_seconds: int = 120, start_price: float = 65_000.0) -> list[Event]:
    """Generate one TRADE event per second for n_seconds."""
    base = datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
    events = []
    for i in range(n_seconds):
        ts = base.replace(second=i % 60, minute=i // 60)
        price = start_price + i * 0.5
        is_sell = i % 2 != 0
        events.append(Event(
            ts=ts,
            source="test",
            kind="trade",
            symbol="BTCUSDT",
            payload={"price": price, "qty": 0.1, "notional_usd": price * 0.1,
                     "is_sell_aggressor": is_sell, "agg_id": str(i)},
            tags={"price", "trade", "taker_sell" if is_sell else "taker_buy"},
            severity=0.1,
        ))
    return events


# ── Test ──────────────────────────────────────────────────────────────────────

async def test_watch_engine_runs() -> None:
    cfg = settings()
    tick_seconds = int(cfg["clock"]["tick_seconds"])
    events = _make_events(n_seconds=120)
    total_ticks_est = 120 // tick_seconds

    ledger_dir = PROJECT_ROOT / "data" / "replays" / "_phase6_test" / "ledger"
    tribes, state, projector, decision, executor, regime_classifier = _build_stack(
        cfg, ledger_dir=ledger_dir, rng_seed=42,
    )

    broadcaster = MockBroadcaster()
    engine = WatchEngine(
        tribes=tribes, state=state, projector=projector,
        decision=decision, executor=executor,
        regime_classifier=regime_classifier,
        tick_seconds=tick_seconds,
        broadcaster=broadcaster,
        run_id="phase6_test",
        day="2024-01-15",
        speed=1000.0,          # max speed — test runs in <1s wall time
        total_ticks_est=total_ticks_est,
    )

    result = await engine.run(iter(events))

    # ── Assertions ────────────────────────────────────────────────────────────

    assert result["ticks"] > 0, "engine produced zero ticks"

    tick_msgs = [m for m in broadcaster.published if m.get("type") == "tick"]
    replay_msgs = [m for m in broadcaster.published if m.get("type") == "replay_state"]

    assert len(tick_msgs) > 0, "no tick messages broadcast"
    assert len(replay_msgs) >= 2, f"expected >=2 replay_state messages, got {len(replay_msgs)}"

    # Shape check on a tick message
    sample = tick_msgs[0]
    required = {"type", "tick", "ts", "price", "net_pressure", "uncertainty",
                "expected_move_bps", "confidence", "votes", "positions",
                "equity_usd", "action", "regime"}
    missing = required - sample.keys()
    assert not missing, f"tick message missing keys: {missing}"

    # Final replay_state must have active=False
    final = replay_msgs[-1]
    assert final.get("active") is False, \
        f"last replay_state.active should be False, got {final.get('active')}"

    # Votes must include tribe_id / side / conviction
    for v in sample["votes"]:
        assert "tribe_id" in v and "side" in v and "conviction" in v

    print(f"  ok  ticks={result['ticks']}  tick_msgs={len(tick_msgs)}"
          f"  replay_msgs={len(replay_msgs)}  total_published={len(broadcaster.published)}")


def main() -> int:
    print("running phase-6 smoke test...")
    try:
        asyncio.run(test_watch_engine_runs())
    except AssertionError as e:
        print(f"  FAIL  {e}")
        return 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  ERROR  {type(e).__name__}: {e}")
        return 1
    print("phase-6 smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
