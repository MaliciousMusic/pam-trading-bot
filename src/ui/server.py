"""FastAPI app serving the dashboard.

Endpoints:
  GET  /                 → the single-file dashboard
  GET  /api/tribes       → tribe metadata (served once at startup)
  GET  /api/backscore    → MAE + hit-rate per forecast horizon (trailing window)
  GET  /api/calibration  → per-tribe skill scores (Phase G audit→calibration loop)
  WS   /ws               → tick + event stream; replays recent tick buffer on connect
"""
from __future__ import annotations

from pathlib import Path

from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from loguru import logger

from ..audit.backscore import ForecastAuditor
from ..audit.calibrator import TribeCalibrator
from .broadcaster import UIBroadcaster


STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    broadcaster: UIBroadcaster,
    tribe_meta: list[dict],
    config: dict | None = None,
    *,
    auditor: ForecastAuditor | None = None,
    calibrator: TribeCalibrator | None = None,
) -> FastAPI:
    app = FastAPI(title="PAM Council", docs_url=None, redoc_url=None)

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

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
            for tid, info in cal.get("tribes", {}).items():
                mult = info.get("multiplier", 1.0)
                acc = info.get("ewma_accuracy")
                n = info.get("n_resolved", 0)
                acc_str = f"{acc*100:.1f}%" if acc is not None else " n/a "
                lines.append(f"  {tid:<36}  acc={acc_str}  mult={mult:.2f}x  n={n}")
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
