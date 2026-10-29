"""Real EVM previews and receipt checks on the isolated local main/shadow pair."""
import os

import pytest

from mevshield.chain import Chain, ShadowFork
from mevshield.defense.safety import SafetyReports
from mevshield.extensions.service import isolated_world

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("MEVSHIELD_INTEGRATION") != "1", reason="requires deployed local main and shadow Anvil")]


@pytest.fixture
def world():
    c = Chain(); sh = ShadowFork(c)
    with isolated_world(c, sh):
        yield c, SafetyReports(c, sh)


def execute(service, **params):
    r = service.prepare(**params)
    assert r["simulation_passed"], r["reason"]
    return service.execute(r["id"], r["binding"])


def test_preview_changes_no_main_state_and_receipt_matches(world):
    c, service = world
    reserves, block = c.reserves(), c.block_number
    r = service.prepare()
    assert c.reserves() == reserves and c.block_number == block and not c.txpool()
    assert r["approval"]["sufficient"] and r["approval"]["unlimited"]
    assert int(r["predicted_changes"]["USDC"]) == -1000 * 10**18
    result = service.execute(r["id"], r["binding"])
    assert result["status"] == "included" and result["receipt"]["reconciliation"]["matches"]
    assert int(result["receipt"]["gas_paid_wei"]) > 0
    with pytest.raises(ValueError, match="unused"):
        service.execute(r["id"], r["binding"])


def test_stale_tampered_and_expired_previews_cannot_execute(world):
    c, service = world
    r = service.prepare()
    with pytest.raises(ValueError, match="binding"):
        service.execute(r["id"], "0" * 64)
    c.mine()
    with pytest.raises(ValueError, match="stale"):
        service.execute(r["id"], r["binding"])
    r = service.prepare()
    service.reports[r["id"]]["expires_at"] = 0
    with pytest.raises(ValueError, match="stale"):
        service.execute(r["id"], r["binding"])
    assert not c.txpool()


def test_blocked_borrow_and_successful_lending_lifecycle(world):
    _, service = world
    r = service.prepare(action="borrow", amount=1000)
    assert r["status"] == "blocked" and not r["simulation_passed"]
    with pytest.raises(ValueError, match="successful"):
        service.execute(r["id"], r["binding"])
    for params in ({"action": "deposit", "amount": 10}, {"action": "borrow", "amount": 1000},
                   {"action": "repay", "amount": 200}, {"action": "withdraw", "amount": 1}):
        result = execute(service, **params)
        assert result["receipt"]["reconciliation"]["matches"]


def test_pending_transactions_reject_preview(world):
    c, service = world
    from mevshield.harness.traffic import submit_user_swap
    submit_user_swap(c, "alice", 100)
    with pytest.raises(ValueError, match="Pending"):
        service.prepare()


def test_idle_chain_does_not_create_already_expired_swap(world, monkeypatch):
    c, service = world
    import mevshield.defense.safety as safety
    future = c.w3.eth.get_block("latest")["timestamp"] + 3600
    monkeypatch.setattr(safety.time, "time", lambda: future)
    r = service.prepare()
    assert r["deadline"] >= future + 300
    assert r["simulation_passed"]
