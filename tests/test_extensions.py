"""Economic, reproducibility and API boundary checks for the new capabilities (no devnet required)."""
import json
import math

import pytest
from fastapi.testclient import TestClient

from mevshield import config
from mevshield.extensions.coordinated import ordered_pending
from mevshield.extensions.replay import Scenario, list_replays, load_replay, run_replay, save_replay
from mevshield.extensions.routing import PoolView, quote_routes
from mevshield.web.server import create_app

E = config.E
POOLS = [PoolView("A", 100 * E, 200_000 * E), PoolView("B", 100 * E, 206_000 * E)]


def test_route_preserves_dust_and_chooses_highest_net_value():
    amount = 10000 * E + 3
    result = quote_routes(amount, POOLS)
    for candidate in result["candidates"]:
        assert sum(x["amount_in"] for x in candidate["legs"]) == amount
        assert candidate["quoted_out"] >= sum(x["min_out"] for x in candidate["legs"])
        assert all(x["predicted_harm"] >= 0 for x in candidate["legs"])
    assert result["chosen"]["expected_net_usd"] == max(x["expected_net_usd"] for x in result["candidates"])


def test_routing_can_reject_best_raw_quote_when_extra_gas_dominates():
    result = quote_routes(10000 * E, POOLS, gas_per_leg_usd=1000)
    assert len(result["best_quote"]["legs"]) == 2
    assert len(result["chosen"]["legs"]) == 1
    assert result["modeled_improvement_usd"] > 0


def test_zero_attacker_capital_means_no_modeled_mev_loss():
    report = quote_routes(10000 * E, POOLS, capital=0)
    assert all(c["predicted_harm_usd"] == 0 for c in report["candidates"])


def test_reverse_direction_routes_are_supported():
    report = quote_routes(5 * E, POOLS, token_in="WETH")
    assert report["chosen"]["quoted_out"] > 9000 * E


@pytest.mark.parametrize("slippage", [-1, 1, math.nan, math.inf])
def test_invalid_route_limits_rejected(slippage):
    with pytest.raises(ValueError):
        quote_routes(E, POOLS, slippage=slippage)


def test_replay_is_identical_after_save_and_reload(tmp_path):
    s = Scenario(seed=123, liquidity_scale=.2)
    saved = save_replay(s, tmp_path)
    assert save_replay(s, tmp_path)["id"] == saved["id"]
    assert run_replay(s) == run_replay(load_replay(saved["id"], tmp_path))
    assert list_replays(tmp_path)[0]["id"] == saved["id"]
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_replay_rejects_path_traversal_and_wrong_model(tmp_path):
    with pytest.raises(ValueError):
        load_replay("../../private", tmp_path)
    saved = save_replay(Scenario(), tmp_path)
    path = tmp_path / (saved["id"] + ".json")
    data = json.loads(path.read_text())
    data["model_version"] = "future-version"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="model version"):
        load_replay(saved["id"], tmp_path)


def test_edited_replay_cannot_silently_reuse_original_id(tmp_path):
    saved = save_replay(Scenario(), tmp_path)
    path = tmp_path / (saved["id"] + ".json")
    data = json.loads(path.read_text())
    data["scenario"]["amount_usdc"] = 42
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="does not match"):
        load_replay(saved["id"], tmp_path)
    assert list_replays(tmp_path) == []


def test_replay_counts_cost_and_excludes_failed_swaps_from_winner():
    result = run_replay(Scenario(seed=0, slippage=.00001))
    assert any(not row["completed"] for row in result["rows"])
    winner = next(row for row in result["rows"] if row["strategy"] == result["best_completed"])
    assert winner["completed"]
    for row in result["rows"]:
        assert row["gas_usd"] > 0
        if not row["completed"]:
            assert row["total_cost_usd"] >= config.RETRY_COST


def test_delay_and_fee_assumptions_change_guardian_ordering():
    assert run_replay(Scenario())["guardian"]["can_precede_attack"]
    assert not run_replay(Scenario(guardian_delay_blocks=1))["guardian"]["can_precede_attack"]
    assert not run_replay(Scenario(attacker_tip_gwei=10))["guardian"]["can_precede_attack"]


@pytest.mark.parametrize("kwargs", [{"amount_usdc": math.nan}, {"gas_gwei": math.inf},
                                   {"seed": 1.5}, {"seed": True}, {"liquidity_scale": 0},
                                   {"guardian_delay_blocks": 6}])
def test_invalid_scenarios(kwargs):
    with pytest.raises(ValueError):
        Scenario(**kwargs)


def test_pending_order_respects_nonce_before_fee():
    txs = [{"hash": "b", "from": "alice", "nonce": "0x2", "maxPriorityFeePerGas": "0xff"},
           {"hash": "a", "from": "alice", "nonce": "0x1", "maxPriorityFeePerGas": "0x1"},
           {"hash": "c", "from": "bob", "nonce": "0x0", "maxPriorityFeePerGas": "0x2"}]
    assert [t["hash"] for t in ordered_pending(txs)] == ["c", "a", "b"]
    with pytest.raises(ValueError):
        ordered_pending(txs, max_transactions=2)


def test_api_validation_and_offline_replays(tmp_path, monkeypatch):
    import mevshield.extensions.replay as replay
    monkeypatch.setattr(replay, "REPLAY_DIR", tmp_path)
    with TestClient(create_app(), base_url="http://localhost") as client:
        assert client.get("/lab").status_code == 200
        assert client.post("/api/replay", json={"amount_usdc": -1}).status_code == 422
        assert client.post("/api/replay", json={"arbitrary": 1}).status_code == 422
        assert client.post("/api/extensions/unknown", json={}).status_code == 422
        assert client.post("/api/extensions/all", json={"amount": 5}).status_code == 422
        expected = client.post("/api/replay", json={"seed": 123}).json()
        saved = client.post("/api/replays", json={"seed": 123}).json()
        assert client.get("/api/replays/" + saved["id"]).json() == expected
        assert client.get("/api/replays/" + "0" * 20).status_code == 404


def test_feature_api_uses_engine_and_reports_failures():
    app = create_app()
    app.state.engine.run_extension = lambda feature, amount: {"feature": feature, "amount": amount}
    with TestClient(app, base_url="http://localhost") as client:
        assert client.post("/api/extensions/routing", json={"amount": 99}).json()["amount"] == 99
        def fail(*args):
            raise RuntimeError("devnet is not running")
        app.state.engine.run_extension = fail
        response = client.post("/api/extensions/routing", json={})
        assert response.status_code == 400
        assert response.json()["detail"] == "devnet is not running"
