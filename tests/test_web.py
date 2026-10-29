"""Boundary tests for the browser APIs; no chain or wallet is needed."""
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mevshield.web.server import Broadcaster, create_app


@pytest.fixture
def application():
    return create_app()


@pytest.fixture
def client(application):
    with TestClient(application, base_url="http://localhost") as client:
        yield client


@pytest.mark.parametrize("path", [
    "/api/sandwich?amount=nan", "/api/sandwich?amount=inf", "/api/predict?amount=-1",
    "/api/sandwich?amount=100001", "/api/oracle?target=router", "/api/governance?target=ammA",
    "/api/sandwich?defense=unknown",
])
def test_invalid_lab_actions_never_reach_engine(client, path):
    assert client.post(path).status_code == 422


def test_rejects_cross_origin_actions_and_untrusted_hosts(client):
    assert client.post("/api/reset", headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.get("/", headers={"Host": "rebound.example"}).status_code == 400
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"Origin": "https://unrelated.example"}):
            pass


def test_no_inline_script_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "fonts.googleapis.com" not in response.text


def test_removed_sidebar_content_and_visible_wallet_setup(client):
    html = client.get("/").text
    assert 'data-view="guide"' not in html
    assert 'id="view-guide"' not in html
    assert 'class="sidebar-note"' not in html
    assert 'id="wallet-setup"' in html
    assert "https://metamask.io/download/" in html
    assert client.get("/static/wallet-connection.js").status_code == 200


def test_simultaneous_reset_cannot_interrupt_an_experiment(application, client, monkeypatch):
    entered, finish = threading.Event(), threading.Event()

    def run_arbitrage():
        entered.set()
        assert finish.wait(5)
        return {"benign": True}

    monkeypatch.setattr(application.state.engine, "run_arbitrage", run_arbitrage)
    replies = []
    worker = threading.Thread(target=lambda: replies.append(client.post("/api/arbitrage")))
    worker.start()
    try:
        assert entered.wait(5)
        assert client.post("/api/reset").status_code == 409
        assert client.get("/api/status").json() == {"busy": True}
    finally:
        finish.set()
        worker.join(5)
    assert replies[0].status_code == 200


def test_history_keeps_the_latest_result_after_status():
    bus = Broadcaster()
    bus.emit({"kind": "run", "busy": True})
    bus.emit({"kind": "result", "outcome": "defended"})
    bus.emit({"kind": "status", "ready": True})
    assert any(event["kind"] == "result" for event in bus.history)
    bus.emit({"kind": "reset"})
    assert bus.history == [{"kind": "reset"}]


def test_vault_preparation_requires_validated_action(client):
    response = client.post("/api/network/prepare", json={"action": "transfer", "account": "0x123"})
    assert response.status_code == 422
    assert client.post("/api/network/prepare", json={
        "action": "deposit", "account": "0x123", "amount_wei": "1e18"
    }).status_code == 422
