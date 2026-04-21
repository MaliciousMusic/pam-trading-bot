"""FastAPI app serving the dashboard.

Endpoints:
  GET  /                 → the single-file dashboard
  GET  /api/tribes       → tribe metadata (served once at startup)
  GET  /api/backscore    → MAE + hit-rate per forecast horizon (trailing window)
  GET  /api/calibration  → per-tribe skill scores (audit→calibration loop)
  GET  /api/kernel       → projector-gain fit snapshot (Phase A)
  WS   /ws               → tick + event stream; replays recent tick buffer on connect
"""
from __future__ import annotations

from pathlib import Path

from datetime import datetime, timezone

from ..core import serde
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from loguru import logger

from ..audit.backscore import ForecastAuditor
from ..audit.calibrator import TribeCalibrator
from ..projector.kernel_calibrator import KernelCalibrator
from ..regime.classifier import RegimeClassifier
from ..tribes.market_state import MarketState
from .broadcaster import UIBroadcaster


STATIC_DIR = Path(__file__).parent / "static"


class _ReplayStartBody(BaseModel):
    day: str
    speed: float = 10.0
    synth_rate: int = 60
    seed: int = 42
    symbol: str = "BTCUSDT"
    variants: list[dict] | None = None  # [{synth_rate, seed, variant_id?}] for multi-variant
    days: int = 1  # 1 = single-day; >1 chains consecutive UTC days with equity carry-over


class _SpeedBody(BaseModel):
    speed: float


def create_app(
    broadcaster: UIBroadcaster,
    tribe_meta: list[dict],
    config: dict | None = None,
    *,
    auditor: ForecastAuditor | None = None,
    calibrator: TribeCalibrator | None = None,
    kernel_calibrator: KernelCalibrator | None = None,
    regime_classifier: RegimeClassifier | None = None,
    state: MarketState | None = None,
    watch_controller=None,
    executor=None,
    tribe_scorer=None,
    shadow_ledgers: dict | None = None,
) -> FastAPI:
    app = FastAPI(title="PAM Council", docs_url=None, redoc_url=None)

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html",
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/replay")
    async def replay_page():
        return FileResponse(STATIC_DIR / "index.html",
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.post("/api/replay/start")
    async def replay_start(body: _ReplayStartBody):
        if watch_controller is None:
            return JSONResponse({"error": "replay not available"}, status_code=503)
        from datetime import date as _date
        day = _date.fromisoformat(body.day)
        run_id = await watch_controller.start(
            day=day, speed=body.speed, synth_rate=body.synth_rate,
            seed=body.seed, symbol=body.symbol, variants=body.variants,
            days=body.days,
        )
        return JSONResponse({"run_id": run_id, "status": "started"})

    @app.post("/api/replay/speed")
    async def replay_speed(body: _SpeedBody):
        if watch_controller is None:
            return JSONResponse({"error": "replay not available"}, status_code=503)
        watch_controller.set_speed(body.speed)
        return JSONResponse({"speed": body.speed})

    @app.post("/api/replay/pause")
    async def replay_pause():
        if watch_controller is None:
            return JSONResponse({"error": "replay not available"}, status_code=503)
        paused = watch_controller.toggle_pause()
        return JSONResponse({"paused": paused})

    @app.post("/api/replay/stop")
    async def replay_stop():
        if watch_controller is None:
            return JSONResponse({"error": "replay not available"}, status_code=503)
        await watch_controller.stop()
        return JSONResponse({"status": "stopped"})

    @app.post("/api/risk/unlock")
    async def risk_unlock():
        """Phase 10 — operator-driven clear of the session-DD halt. Black-
        swan and peak-DD halts auto-clear; only session-DD requires this."""
        if executor is None or not hasattr(executor, "manual_unlock"):
            return JSONResponse({"error": "executor not available"}, status_code=503)
        was_halted = bool(getattr(executor, "session_halted", False))
        executor.manual_unlock()
        return JSONResponse({
            "status": "unlocked",
            "was_halted": was_halted,
            "session_halted": bool(getattr(executor, "session_halted", False)),
        })

    @app.get("/api/risk/snapshot")
    async def risk_snapshot():
        if executor is None or not hasattr(executor, "risk_snapshot"):
            return JSONResponse({"enabled": False})
        return JSONResponse({"enabled": True, "snapshot": executor.risk_snapshot()})

    @app.get("/api/tribes")
    async def tribes():
        return JSONResponse(tribe_meta)

    @app.get("/api/config")
    async def cfg():
        return JSONResponse(config or {})

    @app.get("/api/backscore")
    async def backscore(window_s: float = 3600.0, limit: int = 200):
        if auditor is None:
            return JSONResponse({"enabled": False})
        return JSONResponse({
            "enabled": True,
            "snapshot": auditor.snapshot(window_s=window_s),
            "recent": auditor.recent(limit=limit),
        })

    @app.get("/api/audit/report")
    async def audit_report(window_s: float = 0):
        """Plain-text session audit report — download as .txt."""
        if auditor is None:
            return PlainTextResponse("Audit disabled.\n")
        # window_s=0 means full session
        snap = auditor.snapshot(window_s=window_s if window_s > 0 else 1e18)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "═" * 60,
            f"  PAM COUNCIL — SESSION AUDIT REPORT",
            f"  Generated: {now}",
            "═" * 60,
            "",
            f"  Total resolved anchors : {snap['total_resolved']}",
            f"  Pending (not yet due)  : {snap['pending']}",
            "",
            f"  {'Horizon':<8}  {'n':>5}  {'Hit%':>6}  {'Bias':>8}  {'MAE':>8}  {'Pred avg':>9}  {'Real avg':>9}",
            "  " + "-" * 56,
        ]
        for h in snap.get("by_horizon", []):
            hit = f"{h['hit_rate']*100:.1f}%" if h['hit_rate'] is not None else "  n/a "
            bias = f"{h['bias_bps']:+.2f}bps" if h['bias_bps'] is not None else "   n/a  "
            mae = f"{h['mae_bps']:.2f}bps" if h['mae_bps'] is not None else "  n/a  "
            pred = f"{h['mean_predicted_mag_bps']:.2f}bps" if h['mean_predicted_mag_bps'] is not None else "  n/a   "
            real = f"{h['mean_realized_mag_bps']:.2f}bps" if h['mean_realized_mag_bps'] is not None else "  n/a   "
            lines.append(f"  {h['label']:<8}  {h['n']:>5}  {hit:>6}  {bias:>8}  {mae:>8}  {pred:>9}  {real:>9}")
        lines += [
            "",
            "  INTERPRETATION",
            "  Hit% < 50% → council directionally wrong more than right",
            "  Bias > 0   → systematic over-prediction (bullish bias)",
            "  Bias < 0   → systematic under-prediction (bearish bias)",
            "  MAE        → mean absolute error in bps",
            "",
            "═" * 60,
        ]
        if calibrator is not None:
            cal = calibrator.snapshot()
            lines += ["", "  PER-TRIBE SKILL (EWMA directional accuracy)", "  " + "-" * 40]
            for info in cal.get("tribes", []):
                tid = info.get("tribe_id", "?")
                mult = info.get("multiplier", 1.0)
                acc = info.get("ewma_hit")
                fast = info.get("ewma_hit_fast")
                n = info.get("n", 0)
                acc_str = f"{acc*100:.1f}%" if acc is not None else " n/a "
                fast_str = f" fast={fast*100:.1f}%" if fast is not None else ""
                shift_flag = "  ◂REGIME-SHIFT▸" if info.get("regime_shift") else ""
                lines.append(f"  {tid:<36}  acc={acc_str}{fast_str}  mult={mult:.2f}x  n={n}{shift_flag}")
            lines.append("")
        content = "\n".join(lines) + "\n"
        filename = f"pam_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.txt"
        return PlainTextResponse(
            content,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/calibration")
    async def calibration():
        if calibrator is None:
            return JSONResponse({"enabled": False})
        return JSONResponse({"enabled": True, **calibrator.snapshot()})

    @app.get("/api/tribe_trading")
    async def tribe_trading():
        """Per-tribe shadow-ledger scorecard. Drives the Tribe Trading
        Heatmap on the dashboard. Returns the scorer snapshot plus the
        live ledger equity rows (so the UI can render both in the same
        table without a second round-trip)."""
        if tribe_scorer is None:
            return JSONResponse({"enabled": False})
        payload = {"enabled": True, **tribe_scorer.snapshot()}
        if shadow_ledgers:
            payload["ledgers"] = [l.snapshot() for l in shadow_ledgers.values()]
        return JSONResponse(payload)

    @app.get("/api/kernel")
    async def kernel():
        if kernel_calibrator is None:
            return JSONResponse({"enabled": False})
        return JSONResponse({"enabled": True, **kernel_calibrator.snapshot()})

    @app.get("/api/replay/runs")
    async def replay_runs():
        """List recent replay run directories (newest first, cap 50)."""
        from ..core.config import PROJECT_ROOT
        replay_dir = PROJECT_ROOT / "data" / "replays"
        if not replay_dir.exists():
            return JSONResponse([])
        runs = []
        for d in sorted(replay_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            meta: dict = {"run_id": d.name}
            for manifest_name in ("multi_manifest.json", "manifest.json"):
                mp = d / manifest_name
                if mp.exists():
                    try:
                        m = serde.loads(mp.read_bytes())
                        meta.update({k: m.get(k) for k in ("day", "symbol") if m.get(k)})
                        meta["type"] = "multi" if manifest_name == "multi_manifest.json" else "single"
                    except Exception:
                        pass
                    break
            runs.append(meta)
            if len(runs) >= 50:
                break
        return JSONResponse(runs)

    @app.get("/api/replay/tick")
    async def replay_tick(run_id: str, tick: int, variant_id: str | None = None):
        """Return the decisions.jsonl row for a specific tick in a completed run.

        Searches: variants/{variant_id}/decisions.jsonl if variant_id given,
        otherwise decisions.jsonl at run root, then any variant subdirectory."""
        from ..core.config import PROJECT_ROOT
        repl_dir = PROJECT_ROOT / "data" / "replays" / run_id
        if not repl_dir.exists():
            return JSONResponse({"error": "run not found", "run_id": run_id}, status_code=404)

        candidates: list[Path] = []
        if variant_id:
            candidates.append(repl_dir / "variants" / variant_id / "decisions.jsonl")
        else:
            candidates.append(repl_dir / "decisions.jsonl")
            vdir = repl_dir / "variants"
            if vdir.exists():
                candidates.extend(
                    p / "decisions.jsonl"
                    for p in sorted(vdir.iterdir())
                    if p.is_dir()
                )

        jsonl = next((p for p in candidates if p.exists()), None)
        if jsonl is None:
            return JSONResponse({"error": "decisions not found", "run_id": run_id}, status_code=404)

        with jsonl.open("rb") as f:
            for line in f:
                if not line.strip():
                    continue
                row = serde.loads(line)
                if row.get("tick") == tick:
                    return JSONResponse(row)

        return JSONResponse({"error": "tick not found", "tick": tick}, status_code=404)

    @app.get("/api/regime")
    async def regime():
        # Classifier is stateless across calls — re-reading MarketState on
        # demand is cheap (a couple of rolling-window queries), so the
        # endpoint always reflects "right now" rather than the last tick.
        if regime_classifier is None or state is None:
            return JSONResponse({"enabled": False})
        if state.last_price is None:
            return JSONResponse({"enabled": True, "warming_up": True})
        r = regime_classifier.classify(state)
        return JSONResponse({"enabled": True, **r.to_dict()})

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        q = broadcaster.subscribe()
        try:
            for msg in broadcaster.recent_ticks():
                await ws.send_json(msg)
            while True:
                msg = await q.get()
                await ws.send_json(msg)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("ws client dropped: {}", e)
        finally:
            broadcaster.unsubscribe(q)

    return app
