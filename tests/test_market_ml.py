"""Real-input provenance, causal splits and read-only production boundary checks."""

import hashlib
import json
import time

import numpy as np
import pandas as pd
import pytest
from eth_abi import encode
from fastapi.testclient import TestClient

from mevshield import config
from mevshield.defense.amm_math import amount_out
from mevshield.defense.predictor import HarmPredictor
from mevshield.ml.mainnet import POOLS, TOKENS, SYNC, SWAP, ReadOnlyRPC, swap_rows
from mevshield.ml.model import INPUTS, features, policy_scenarios, temporal_split
from mevshield.web.server import create_app


def events(stable_in=True, stable0=True, decoded=True):
    address = next(
        p
        for p, m in POOLS.items()
        if m["stable0"] == stable0 and m["stable"] in ("USDC", "USDT")
    )
    metadata = POOLS[address]
    stable = 10_000_000 * 10**6
    eth = 5000 * 10**18
    is0 = stable_in == stable0
    r0, r1 = (stable, eth) if stable0 else (eth, stable)
    amount = 10_000 * 10**6 if stable_in else 5 * 10**18
    ri, ro = (r0, r1) if is0 else (r1, r0)
    output = amount_out(amount, ri, ro)
    inputs = (amount, 0) if is0 else (0, amount)
    outputs = (0, output) if is0 else (output, 0)
    sync = [r0 + inputs[0] - outputs[0], r1 + inputs[1] - outputs[1]]
    txhash = "0x" + "a" * 64
    blockhash = "0x" + "b" * 64
    base = {
        "transactionHash": txhash,
        "address": address,
        "blockNumber": "0x64",
        "blockHash": blockhash,
    }
    logs = [
        {
            **base,
            "topics": [SYNC],
            "logIndex": "0x1",
            "data": "0x" + encode(["uint112", "uint112"], sync).hex(),
        },
        {
            **base,
            "topics": [SWAP],
            "logIndex": "0x2",
            "data": "0x" + encode(["uint256"] * 4, [*inputs, *outputs]).hex(),
        },
    ]
    path = [TOKENS[metadata["stable"]], TOKENS["WETH"]]
    if not stable_in:
        path.reverse()
    calldata = (
        "0x38ed1739"
        + encode(
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [amount, output * 99 // 100, path, "0x" + "1" * 40, 2000000000],
        ).hex()
    )
    tx = {
        "to": metadata["router"] if decoded else "0x" + "2" * 40,
        "input": calldata,
        "value": "0x0",
        "gasPrice": hex(2 * 10**9),
        "blockHash": blockhash,
    }
    return logs, {txhash: tx}, {100: {"hash": blockhash, "timestamp": hex(1800000000)}}


@pytest.mark.parametrize(
    "stable_in,stable0", [(True, True), (False, True), (True, False), (False, False)]
)
def test_real_event_reconstruction_handles_decimals_and_direction(stable_in, stable0):
    rows, rejected = swap_rows(*events(stable_in, stable0))
    assert not rejected and len(rows) == 1
    row = rows[0]
    assert row["trade_usd"] == pytest.approx(10000)
    assert row["liquidity_usd"] == pytest.approx(10_000_000)
    assert row["eth_price_usd"] == pytest.approx(2000)
    assert row["slippage"] == pytest.approx(0.01, abs=1e-10)
    assert row["slippage_source"] == "decoded_router_minimum"


def test_undecodable_router_is_explicit_policy_and_reorg_is_rejected():
    logs, txs, headers = events(decoded=False)
    rows, _ = swap_rows(logs, txs, headers)
    assert rows[0]["slippage_source"] == "declared_policy_scenario"
    assert rows[0]["slippage"] in (0.001, 0.005, 0.01, 0.02, 0.05)
    headers[100]["hash"] = "0x" + "c" * 64
    rows, rejected = swap_rows(logs, txs, headers)
    assert not rows and rejected["inconsistent_block_provenance"] == 1


def test_nonstandard_swaps_are_excluded():
    logs, txs, headers = events()
    data = list(logs[1]["data"])
    data[-2] = "f" if data[-2] != "f" else "e"
    logs[1]["data"] = "".join(data)
    rows, rejected = swap_rows(logs, txs, headers)
    assert not rows and rejected


def test_rpc_rejects_chain_mutation_before_request(monkeypatch):
    rpc = ReadOnlyRPC()
    monkeypatch.setattr(
        rpc.session, "post", lambda *a, **k: pytest.fail("Mutation reached transport")
    )
    with pytest.raises(ValueError, match="read-only"):
        rpc.call("eth_sendRawTransaction", ["0x"])


def test_whole_block_temporal_split_and_policy_descendants():
    frame = pd.DataFrame(
        {
            "row_id": [f"tx{i}" for i in range(600)],
            "block_number": np.repeat(np.arange(200), 3),
        }
    )
    parts = temporal_split(frame)
    for a, b in zip(parts, parts[1:]):
        assert a.block_number.max() + 1 < b.block_number.min()
    row = {
        "row_id": "fixed-id",
        "block_number": 4,
        **dict(zip(INPUTS, [1000, 1_000_000, 0.01, 30, 2, 2000])),
    }
    scenarios = policy_scenarios(pd.DataFrame([row]))
    assert len(scenarios) == 4 and (scenarios.parent_row_id == "fixed-id").all()
    assert (scenarios.block_number == 4).all()
    assert scenarios.trade_source.eq("declared_impact_policy").all()
    pd.testing.assert_frame_equal(scenarios, policy_scenarios(pd.DataFrame([row])))
    with pytest.raises(ValueError, match="Duplicate"):
        temporal_split(pd.concat([frame, frame.iloc[:1]]))


def test_features_ignore_outcomes_and_include_eth_price():
    row = dict(zip(INPUTS, [1000, 1e6, 0.02, 30, 2, 2000]))
    f = pd.DataFrame([row])
    x = features(f)
    f["harm_usd"] = 9e20
    f["block_number"] = 99
    f["transaction_hash"] = "future"
    np.testing.assert_array_equal(x, features(f))
    f["eth_price_usd"] = 4000
    assert not np.array_equal(x, features(f))


def test_live_quote_validates_identity_and_uses_no_write(monkeypatch):
    from mevshield.ml import live

    pool = next(iter(POOLS))
    calls = []

    class FakeRPC:
        def __init__(self, *a, **k):
            pass

        def call(self, method, params):
            calls.append(method)
            return {
                "eth_chainId": "0x1",
                "eth_getBlockByNumber": {
                    "number": "0x123",
                    "hash": "0xabc",
                    "timestamp": hex(int(time.time())),
                },
                "eth_gasPrice": hex(2 * 10**9),
            }[method]

        def batch(self, requests):
            calls.extend(m for m, p in requests)
            return [
                "0x" + TOKENS["USDC"][2:].rjust(64, "0"),
                "0x" + TOKENS["WETH"][2:].rjust(64, "0"),
                "0x"
                + encode(
                    ["uint112", "uint112", "uint32"],
                    [1_000_000 * 10**6, 500 * 10**18, 1],
                ).hex(),
                "0x" + "5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f".rjust(64, "0"),
            ]

    monkeypatch.setattr(live, "ReadOnlyRPC", FakeRPC)
    result = live.assess_mainnet(pool, 10000, 0.02)
    assert result["source"] == "exact-v2" and result["harm_usd"] > 0
    assert result["chain_id"] == 1 and set(calls) <= ReadOnlyRPC.METHODS
    with pytest.raises(ValueError, match="Unsupported pool"):
        live.assess_mainnet("0x" + "0" * 40, 1000)
    monkeypatch.setattr(FakeRPC, "batch", lambda *a: ["0x" + "0" * 64] * 4)
    with pytest.raises(ValueError, match="identity"):
        live.assess_mainnet(pool, 1000)


def test_mainnet_api_does_not_depend_on_devnet_and_rejects_bad_inputs(monkeypatch):
    from mevshield.ml import live

    monkeypatch.setattr(
        live, "assess_mainnet", lambda *a, **k: {"chain_id": 1, "source": "mock"}
    )
    with TestClient(create_app(), base_url="http://localhost") as client:
        assert (
            client.post("/api/mainnet/risk", json={"amount": 1000}).json()["chain_id"]
            == 1
        )
        for body in (
            {"amount": -1},
            {"amount": 1000, "rpc": "http://internal"},
            {"slippage": 0.5},
            {"pool": "bad"},
        ):
            assert client.post("/api/mainnet/risk", json=body).status_code == 422


def test_shipped_model_and_dataset_checksums_and_gates(tmp_path):
    from mevshield.ml.model import BUNDLED, MarketRiskPredictor

    card = json.loads(BUNDLED.with_suffix(".json").read_text())
    assert hashlib.sha256(BUNDLED.read_bytes()).hexdigest() == card["artifact_sha256"]
    dataset = config.ROOT / "datasets" / "ethereum-v2" / "swaps.csv"
    manifest = json.loads(dataset.with_suffix(".manifest.json").read_text())
    assert (
        hashlib.sha256(dataset.read_bytes()).hexdigest()
        == manifest["dataset_sha256"]
        == card["dataset_sha256"]
    )
    model = HarmPredictor.load(BUNDLED)
    assert isinstance(model, MarketRiskPredictor)
    assert model.report["promotion_passed"] and model.report["lab_qualified"]
    assert model.report["test"]["recall"] >= 0.95
    assert model.report["shuffle_control_roc_auc"] < 0.65
    assert model.report["observed_trade_test"]["recall"] is None
    # An unlearned fee must abstain and safely fall back, never silently return low risk.
    params = (10000, 1_000_000, 0.02, 100, 2, 2000)
    assert not model.predict(*params, domain="mainnet")["accepted"]
    unknown = model.predict_with_fallback(*params, domain="mainnet")
    assert unknown["status"] == "unknown" and unknown["harm_usd"] is None
    fallback = model.predict_with_fallback(
        *params, domain="mainnet", simulator=lambda: {"harm_usd": 42, "p_attack": 1}
    )
    assert fallback["source"] == "simulator" and fallback["harm_usd"] == 42
    path = tmp_path / "roundtrip.pkl"
    model.save(path)
    assert HarmPredictor.load(path).predict(*params) == model.predict(*params)
