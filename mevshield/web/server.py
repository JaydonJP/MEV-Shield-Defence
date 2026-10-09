"""FastAPI backend: serves the single-page dashboard and streams engine events over a WebSocket.

A single DemoEngine drives the devnet. Actions arrive as POSTs, run in a worker thread (web3 is blocking),
and emit events that are broadcast to every connected browser in real time.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .engine import DemoEngine

STATIC = Path(__file__).resolve().parent / "static"


class Broadcaster:
    """Fan-out of engine events to all connected websockets, thread-safe for the worker thread."""

    def __init__(self):
        self._conns: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.history: list[dict] = []  # replayed to a newly-connected client (last reset onward)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._conns.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._conns.discard(ws)

    def emit(self, event: dict) -> None:
        """Called from the worker thread — schedule the async broadcast on the server loop."""
        if event.get("kind") in ("phase", "result") and event.get("state") == "start":
            pass
        if event.get("kind") == "status":
            self.history = [event]  # status is a fresh snapshot; drop stale narrative
        else:
            self.history.append(event)
            self.history = self.history[-400:]
        if self._loop is not None:
            self._loop.call_soon_threadsafe(asyncio.create_task, self._send(event))

    async def _send(self, event: dict) -> None:
        dead = []
        msg = json.dumps(event)
        for ws in list(self._conns):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


def create_app() -> FastAPI:
    app = FastAPI(title="MEV Shield")
    bus = Broadcaster()
    engine = DemoEngine(emit=bus.emit)
    app.state.engine = engine
    app.state.bus = bus

    @app.on_event("startup")
    async def _startup():
        bus.bind_loop(asyncio.get_running_loop())

    async def _run(fn, *args) -> dict:
        try:
            return await asyncio.to_thread(fn, *args) or {}
        except Exception as exc:  # surface engine errors to the UI instead of a 500
            engine.emit("log", level="critical", text=f"Error: {exc}")
            return {"error": str(exc)}

    @app.get("/api/status")
    async def status():
        return JSONResponse(await asyncio.to_thread(engine.status))

    @app.post("/api/reset")
    async def reset():
        return JSONResponse(await _run(engine.reset))

    @app.post("/api/sandwich")
    async def sandwich(amount: float = 500, protect: bool = False):
        return JSONResponse(await _run(engine.run_sandwich, amount, protect))

    @app.post("/api/predict")
    async def predict(amount: float = 500):
        return JSONResponse(await _run(engine.predict, amount))

    @app.post("/api/oracle")
    async def oracle(target: str = "vulnerable", guard: bool = False):
        return JSONResponse(await _run(engine.run_oracle, target, guard))

    @app.post("/api/arbitrage")
    async def arbitrage():
        return JSONResponse(await _run(engine.run_arbitrage))

    @app.post("/api/governance")
    async def governance(target: str = "vulnerable"):
        return JSONResponse(await _run(engine.run_governance, target))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await bus.connect(websocket)
        try:
            # replay current status + recent narrative so a late joiner is in sync
            snapshot = await asyncio.to_thread(engine.status)
            await websocket.send_text(json.dumps({"kind": "status", **snapshot}))
            for ev in bus.history:
                if ev.get("kind") != "status":
                    await websocket.send_text(json.dumps(ev))
            while True:
                await websocket.receive_text()  # keepalive / ignored
        except WebSocketDisconnect:
            bus.disconnect(websocket)
        except Exception:
            bus.disconnect(websocket)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
