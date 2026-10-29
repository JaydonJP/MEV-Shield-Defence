import pytest
from fastapi.testclient import TestClient

from mevshield.defense.safety import fingerprint, reconcile
from mevshield.web.server import create_app


def test_binding_covers_all_transaction_fields_without_integer_precision_loss():
    tx = {"from": "0xabc", "to": "0xdef", "data": "0x1234", "value": 2**200, "chainId": 31337,
          "nonce": 4, "gas": 100000, "maxFeePerGas": 10, "maxPriorityFeePerGas": 1}
    original = fingerprint(tx)
    assert fingerprint(dict(reversed(list(tx.items())))) == original
    for field in tx:
        changed = {**tx, field: tx[field] + 1 if isinstance(tx[field], int) else tx[field] + "0"}
        assert fingerprint(changed) != original


def test_reconciliation_reports_specific_discrepancy():
    assert reconcile({"USDC": -1000, "debt": 1000}, {"USDC": -1000, "debt": 1000})["matches"]
    result = reconcile({"USDC": 1000}, {"USDC": 999})
    assert not result["matches"] and result["differences"]["USDC"]["observed"] == "999"


@pytest.mark.parametrize("body", [{"role": "admin"}, {"action": "approve"}, {"market": "vulnerable"},
    {"amount": -1}, {"slippage": 0}, {"to": "0x123"}, {"amount": "NaN"}])
def test_unknown_or_unbounded_actions_never_reach_engine(body):
    app = create_app()
    app.state.engine.safety_preview = lambda p: pytest.fail("Invalid input reached engine")
    with TestClient(app, base_url="http://localhost") as client:
        assert client.post("/api/safety/preview", json=body).status_code == 422


def test_api_serves_safety_and_reports_contract_rejection():
    app = create_app()
    app.state.engine.safety_preview = lambda p: {"status": "blocked", "reason": "undercollateralised"}
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/safety").status_code == 200
        assert client.post("/api/safety/preview", json={}).json()["status"] == "blocked"
        assert client.post("/api/safety/execute", json={"report_id": "../x", "binding": "x"}).status_code == 422
        assert client.post("/api/risk", json={"amount": 100001}).status_code == 422
