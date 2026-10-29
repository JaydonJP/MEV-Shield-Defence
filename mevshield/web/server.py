"""FastAPI backend: serves the single-page dashboard and streams engine events over a WebSocket.

A single DemoEngine drives the devnet. Actions arrive as POSTs, run in a worker thread (web3 is blocking),
and emit events that are broadcast to every connected browser in real time.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .engine import DemoEngine

STATIC = Path(__file__).resolve().parent / "static"


class FeatureInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    amount: float | None = Field(default=None, gt=0, le=100000)


class ReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    amount_usdc: float = Field(default=10000, ge=1, le=100000)
    liquidity_scale: float = Field(default=1, ge=.05, le=20)
    slippage: float = Field(default=.02, ge=.00001, le=.2)
    attacker_capital_usdc: float = Field(default=50000, ge=0, le=5000000)
    gas_gwei: float = Field(default=2, ge=.01, le=200)
    guardian_delay_blocks: int = Field(default=0, ge=0, le=5, strict=True)
    attacker_tip_gwei: float = Field(default=1, ge=0, le=200)
    guardian_tip_gwei: float = Field(default=2, ge=0, le=200)
    seed: int = Field(default=42, ge=0, le=2**32 - 1, strict=True)


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
    action_lock = asyncio.Lock()

    @app.on_event("startup")
    async def _startup():
        bus.bind_loop(asyncio.get_running_loop())

    async def _run(fn, *args) -> dict:
        try:
            async with action_lock:
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

    @app.get("/lab")
    async def lab():
        return FileResponse(STATIC / "lab.html")

    @app.post("/api/extensions/{feature}")
    async def extension(feature: Literal["routing", "cashback", "batch", "firewall", "coordinated", "all"],
                        body: FeatureInput):
        if feature == "all" and body.amount is not None:
            raise HTTPException(422, "all uses scenario-specific defaults")
        result = await _run(engine.run_extension, feature, body.amount)
        return JSONResponse(result, status_code=400 if "error" in result else 200)

    @app.post("/api/replay")
    async def replay(body: ReplayInput):
        from ..extensions.replay import Scenario, run_replay
        return await asyncio.to_thread(run_replay, Scenario(**body.model_dump()))

    @app.post("/api/replays")
    async def save_scenario(body: ReplayInput):
        from ..extensions.replay import Scenario, save_replay
        return await asyncio.to_thread(save_replay, Scenario(**body.model_dump()))

    @app.get("/api/replays")
    async def saved_scenarios():
        from ..extensions.replay import list_replays
        return await asyncio.to_thread(list_replays)

    @app.get("/api/replays/{replay_id}")
    async def saved_replay(replay_id: str):
        from ..extensions.replay import load_replay, run_replay
        try:
            scenario = await asyncio.to_thread(load_replay, replay_id)
        except FileNotFoundError:
            raise HTTPException(404, "replay not found")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return await asyncio.to_thread(run_replay, scenario)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
