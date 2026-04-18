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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
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
