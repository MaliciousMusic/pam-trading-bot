"""Phase 8 smoke test — tick inspect endpoint + WatchEngine decisions logging.

Three tests:
  1. /api/replay/tick endpoint reads correct row from a synthetic decisions.jsonl
  2. WatchEngine writes decisions.jsonl alongside broadcasting when decisions_path set
  3. Endpoint returns 404 for unknown run_id / missing tick

No network, no subprocesses.

Usage:  python -m scripts.test_replay_phase8
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import orjson
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from fastapi.testclient import TestClient

from src.core.config import PROJECT_ROOT, settings
from src.core.types import Event
from src.replay.harness import _build_stack
from src.replay.watch_engine import WatchEngine
from src.ui.broadcaster import UIBroadcaster
from src.ui.server import create_app


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_decisions(path: Path, n: int = 20) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        for i in range(1, n + 1):
            row = {
                "tick": i,
                "ts": f"1970-01-02T00:{i//60:02d}:{i%60:02d}+00:00",
                "price": 65_000.0 + i * 0.5,
                "regime": "trending_up" if i % 2 == 0 else "chop",
                "votes": [
                    {"tribe_id": "momentum_bot", "side": "buy", "conviction": 0.7, "reason": "test"},
                    {"tribe_id": "retail_app_newbie", "side": "sell", "conviction": 0.3, "reason": "test"},
                ],
                "net_pressure": 0.2,
                "uncertainty": 0.35,
                "expected_move_bps": 5.0,
                "confidence": 0.6,
                "action": "long" if i % 5 == 0 else "hold",
                "action_reason": "momentum",
                "equity_usd": 5000.0 + i * 0.5,
                "positions_open": 0,
                "tribe_disagreement": 0.2,
            }
            f.write(orjson.dumps(row) + b"\n")


def _make_events(n: int = 60) -> list[Event]:
    base = datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
    events = []
    for i in range(n):
        ts = base.replace(second=i % 60, minute=i // 60)
        events.append(Event(
            ts=ts, source="test", kind="trade", symbol="BTCUSDT",
            payload={"price": 65_000.0 + i * 0.5, "qty": 0.1,
                     "notional_usd": 6500.0, "is_sell_aggressor": i % 2 != 0, "agg_id": str(i)},
            tags={"price", "trade"}, severity=0.1,
        ))
    return events


class _MockBroadcaster:
    def __init__(self): self.msgs: list[dict] = []
    async def publish(self, m): self.msgs.append(m)


# ── Tests ─────────────────────────────────────────────────────────────────────

def _patch_root(tmp: str):
    """Return (cfg_mod, orig_root) after patching PROJECT_ROOT to tmp/data/replays parent."""
    import src.core.config as cfg_mod
    orig = cfg_mod.PROJECT_ROOT
    cfg_mod.PROJECT_ROOT = Path(tmp)
    return cfg_mod, orig


def test_endpoint_returns_correct_tick() -> None:
    """GET /api/replay/tick returns the right row from decisions.jsonl."""
    with tempfile.TemporaryDirectory() as tmp:
        run_id = "test_phase8_run"
        decisions = Path(tmp) / "data" / "replays" / run_id / "decisions.jsonl"
        _write_decisions(decisions, n=20)

        cfg_mod, orig = _patch_root(tmp)
        broadcaster = UIBroadcaster()
        app = create_app(broadcaster, [], None)
        client = TestClient(app)
        try:
            r = client.get(f"/api/replay/tick?run_id={run_id}&tick=7")
            assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
            data = r.json()
            assert data["tick"] == 7
            assert abs(data["price"] - (65_000.0 + 7 * 0.5)) < 0.01
            assert data["regime"] == "chop"
            assert len(data["votes"]) == 2
            print(f"  ok  endpoint tick=7 price={data['price']} regime={data['regime']}")
        finally:
            cfg_mod.PROJECT_ROOT = orig


def test_endpoint_variant_id() -> None:
    """Endpoint resolves variants/{variant_id}/decisions.jsonl."""
    with tempfile.TemporaryDirectory() as tmp:
        run_id = "test_phase8_multi"
        vid = "synth_60_seed_42"
        decisions = Path(tmp) / "data" / "replays" / run_id / "variants" / vid / "decisions.jsonl"
        _write_decisions(decisions, n=10)

        cfg_mod, orig = _patch_root(tmp)
        broadcaster = UIBroadcaster()
        app = create_app(broadcaster, [], None)
        client = TestClient(app)
        try:
            r = client.get(f"/api/replay/tick?run_id={run_id}&tick=3&variant_id={vid}")
            assert r.status_code == 200, r.text
            assert r.json()["tick"] == 3
            print(f"  ok  variant endpoint tick=3 variant_id={vid}")
        finally:
            cfg_mod.PROJECT_ROOT = orig


def test_endpoint_404_missing() -> None:
    """404 for unknown run, 404 for missing tick."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg_mod, orig = _patch_root(tmp)
        broadcaster = UIBroadcaster()
        app = create_app(broadcaster, [], None)
        client = TestClient(app)
        try:
            r = client.get("/api/replay/tick?run_id=nonexistent&tick=1")
            assert r.status_code == 404, f"expected 404, got {r.status_code}"

            run_id = "test_404_run"
            _write_decisions(Path(tmp) / "data" / "replays" / run_id / "decisions.jsonl", n=5)
            r2 = client.get(f"/api/replay/tick?run_id={run_id}&tick=999")
            assert r2.status_code == 404
            print("  ok  404 for missing run and out-of-range tick")
        finally:
            cfg_mod.PROJECT_ROOT = orig


async def test_watch_engine_writes_decisions() -> None:
    """WatchEngine writes decisions.jsonl when decisions_path is set."""
    with tempfile.TemporaryDirectory() as tmp:
        decisions_path = Path(tmp) / "decisions.jsonl"
        cfg = settings()
        tick_seconds = int(cfg["clock"]["tick_seconds"])
        broadcaster = _MockBroadcaster()
        ledger_dir = Path(tmp) / "ledger"
        tribes, state, projector, decision, executor, regime_clf = _build_stack(
            cfg, ledger_dir=ledger_dir, rng_seed=42,
        )
        engine = WatchEngine(
            tribes=tribes, state=state, projector=projector,
            decision=decision, executor=executor,
            regime_classifier=regime_clf,
            tick_seconds=tick_seconds,
            broadcaster=broadcaster,
            run_id="phase8_dlog", day="2024-01-15",
            speed=1000.0, total_ticks_est=60 // tick_seconds,
            decisions_path=decisions_path,
        )
        await engine.run(iter(_make_events(n=60)))

        assert decisions_path.exists(), "decisions.jsonl not written"
        rows = [orjson.loads(line) for line in decisions_path.read_bytes().splitlines() if line.strip()]
        assert len(rows) > 0, "decisions.jsonl is empty"
        assert rows[0]["tick"] == 1
        assert "price" in rows[0] and "votes" in rows[0] and "action" in rows[0]
        tick_msgs = [m for m in broadcaster.msgs if m.get("type") == "tick"]
        assert len(tick_msgs) == len(rows), \
            f"tick msgs ({len(tick_msgs)}) ≠ decisions rows ({len(rows)})"
        print(f"  ok  WatchEngine wrote {len(rows)} decisions rows, "
              f"{len(tick_msgs)} tick msgs broadcast")


def main() -> int:
    sync_tests = [
        test_endpoint_returns_correct_tick,
        test_endpoint_variant_id,
        test_endpoint_404_missing,
    ]
    async_tests = [test_watch_engine_writes_decisions]
    print(f"running {len(sync_tests) + len(async_tests)} phase-8 tests...")
    for t in sync_tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERROR  {t.__name__}: {type(e).__name__}: {e}")
            return 1
    for t in async_tests:
        try:
            asyncio.run(t())
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ERROR  {t.__name__}: {type(e).__name__}: {e}")
            return 1
    print("all phase-8 tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
