"""FastAPI app serving the dashboard.

Endpoints:
  GET  /                 → the single-file dashboard
  GET  /api/tribes       → tribe metadata (served once at startup)
  WS   /ws               → tick + event stream; replays recent tick buffer on connect
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from .broadcaster import UIBroadcaster


STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    broadcaster: UIBroadcaster,
    tribe_meta: list[dict],
    config: dict | None = None,
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
