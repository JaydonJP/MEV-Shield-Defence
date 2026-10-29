"""FastAPI backend: serves the single-page dashboard and streams engine events over a WebSocket.

A single DemoEngine drives the devnet. Actions arrive as POSTs, run in a worker thread (web3 is blocking),
and emit events that are broadcast to every connected browser in real time.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .engine import DemoEngine
from ..chain.public import PublicNetwork

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
        if event.get("kind") == "reset" or (event.get("kind") == "run" and event.get("busy")):
            self.history = []
        if event.get("kind") != "status":
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


class VaultAction(BaseModel):
    action: Literal["deploy", "deposit", "withdraw", "pause", "unpause"]
    account: str = Field(max_length=42)
    address: str | None = Field(default=None, max_length=42)
    amount_wei: str = Field(default="0", max_length=24, pattern=r"^[0-9]+$")


class SafetyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    action: Literal["swap", "deposit", "borrow", "repay", "withdraw"] = "swap"
    role: Literal["alice", "bob", "carol"] = "alice"
    amount: float = Field(default=1000, gt=0, le=100000)
    pool: Literal["ammA", "ammB"] = "ammA"
    token_in: Literal["USDC", "WETH"] = "USDC"
    market: Literal["safe", "firewall"] = "safe"
    slippage: float = Field(default=.02, gt=0, le=.2)


class SafetyExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    binding: str = Field(pattern=r"^[a-f0-9]{64}$")


class RiskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    amount: float = Field(default=1000, gt=0, le=100000)
    slippage: float = Field(default=.02, gt=0, le=.2)


def create_app() -> FastAPI:
    bus = Broadcaster()
    engine = DemoEngine(emit=bus.emit)
    network = PublicNetwork()
    action_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        bus.bind_loop(asyncio.get_running_loop())
        yield

    app = FastAPI(title="MEV Shield", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.getenv(
        "MEVSHIELD_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"
    ).split(","))
    app.state.engine = engine
    app.state.bus = bus
    app.state.network = network

    @app.middleware("http")
    async def browser_security(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"error": "Cross-origin actions are not allowed."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self' ws: wss:; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    async def _run(fn, *args) -> dict:
        if action_lock.locked():
            raise HTTPException(409, "A lab experiment is already running. Please wait.")
        async with action_lock:
            scenario = {"run_sandwich": "sandwich", "predict": "sandwich", "run_oracle": "oracle",
                        "run_governance": "governance", "run_arbitrage": "arbitrage"}.get(fn.__name__)
            protected = (bool(args[1]) if fn.__name__ == "run_sandwich" else
                         (args[0] == "safe" or bool(args[1])) if fn.__name__ == "run_oracle" else
                         args[0] == "safe" if fn.__name__ == "run_governance" else False)
            engine.emit("run", busy=True, scenario=scenario, protected=protected,
                        amount=args[0] if fn.__name__ in ("run_sandwich", "predict") else None,
                        target=args[0] if fn.__name__ in ("run_oracle", "run_governance") else None,
                        defense_choice=args[2] if fn.__name__ == "run_sandwich" else None)
            try:
                if scenario:
                    engine.emit("baseline", snapshot=await asyncio.to_thread(engine.status))
                return await asyncio.to_thread(fn, *args) or {}
            except Exception as exc:
                engine.emit("log", level="critical", text=f"Experiment stopped: {exc}")
                raise HTTPException(400, str(exc)) from exc
            finally:
                engine.emit("run", busy=False)

    @app.get("/api/status")
    async def status():
        if action_lock.locked():
            return JSONResponse({"busy": True})
        return JSONResponse(await asyncio.to_thread(engine.status))

    @app.get("/api/network")
    async def network_status():
        return await asyncio.to_thread(network.status)

    @app.get("/api/network/vault")
    async def vault(address: str = Query(max_length=42), account: str = Query(max_length=42)):
        try:
            return await asyncio.to_thread(network.vault_state, address, account)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "Unable to read the vault from Sepolia.") from exc

    @app.post("/api/network/prepare")
    async def prepare(action: VaultAction):
        try:
            tx = await asyncio.to_thread(network.prepare, action.action, action.account, action.address, action.amount_wei)
            return {"transaction": tx}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "Sepolia is unavailable. No transaction was submitted.") from exc

    @app.get("/api/network/receipt/{tx_hash}")
    async def receipt(tx_hash: str):
        try:
            return await asyncio.to_thread(network.receipt, tx_hash)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "Could not check this transaction. Check its explorer link.") from exc

    @app.post("/api/reset")
    async def reset():
        return JSONResponse(await _run(engine.reset))

    @app.post("/api/sandwich")
    async def sandwich(amount: float = Query(500, gt=0, le=100_000, allow_inf_nan=False), protect: bool = False,
                       defense: Literal["auto", "adaptive-slippage", "private-relay", "commit-reveal", "split"] = "auto"):
        return JSONResponse(await _run(engine.run_sandwich, amount, protect, defense))

    @app.post("/api/predict")
    async def predict(amount: float = Query(500, gt=0, le=100_000, allow_inf_nan=False)):
        return JSONResponse(await _run(engine.predict, amount))

    @app.post("/api/oracle")
    async def oracle(target: Literal["vulnerable", "safe"] = "vulnerable", guard: bool = False):
        return JSONResponse(await _run(engine.run_oracle, target, guard))

    @app.post("/api/arbitrage")
    async def arbitrage():
        return JSONResponse(await _run(engine.run_arbitrage))

    @app.post("/api/governance")
    async def governance(target: Literal["vulnerable", "safe"] = "vulnerable"):
        return JSONResponse(await _run(engine.run_governance, target))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
            await websocket.close(code=1008)
            return
        await bus.connect(websocket)
        try:
            # replay current status + recent narrative so a late joiner is in sync
            snapshot = {"busy": True} if action_lock.locked() else await asyncio.to_thread(engine.status)
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

    @app.get("/safety")
    async def safety_page():
        return FileResponse(STATIC / "safety.html")

    @app.post("/api/safety/preview")
    async def safety_preview(body: SafetyInput):
        return await _run(engine.safety_preview, body.model_dump())

    @app.post("/api/safety/execute")
    async def safety_execute(body: SafetyExecution):
        return await _run(engine.safety_execute, body.report_id, body.binding)

    @app.get("/api/safety/history")
    async def safety_history():
        from ..store import safety_history
        return await asyncio.to_thread(safety_history)

    @app.post("/api/risk")
    async def risk_report(body: RiskInput):
        return await _run(engine.risk_preview, body.amount, body.slippage)

    @app.get("/api/verification")
    async def verification():
        from .. import config
        from ..verification import contract_fingerprint
        path = config.DATA_DIR / "verification.json"
        if not path.exists():
            return {"status": "not_run", "message": "Run python scripts/verify_security.py to produce evidence"}
        report = json.loads(path.read_text())
        if report.get("source_fingerprint") != contract_fingerprint():
            report.update(status="stale", message="Contract sources or configuration changed; rerun verification")
        return report

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
