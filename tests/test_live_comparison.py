"""Controlled slippage comparisons must not switch economics at the ML boundary."""
import time

import pytest
from eth_abi import encode

from mevshield.ml import live
from mevshield.ml.mainnet import POOLS, TOKENS
from mevshield.ml.model import MarketRiskPredictor


@pytest.fixture
def snapshot(monkeypatch):
    class RPC:
        def __init__(self):
            self.calls = []
            self.liquidity = 10_000_000

        def call(self, method, params):
            self.calls.append((method, params))
            return {
                "eth_chainId": "0x1",
                "eth_getBlockByNumber": {"number": "0x123", "hash": "0xabc",
                                         "timestamp": hex(int(time.time()))},
                "eth_gasPrice": hex(2 * 10**9),
            }[method]

        def batch(self, requests):
            self.calls.extend(requests)
            return [
                "0x" + TOKENS["USDC"][2:].rjust(64, "0"),
                "0x" + TOKENS["WETH"][2:].rjust(64, "0"),
                "0x" + encode(["uint112", "uint112", "uint32"],
                              [self.liquidity * 10**6, self.liquidity // 2000 * 10**18, 1]).hex(),
                "0x" + "5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f".rjust(64, "0"),
            ]

    rpc = RPC()
    monkeypatch.setattr(live, "ReadOnlyRPC", lambda *a: rpc)
    return rpc


class ResidualModel(MarketRiskPredictor):
    def __init__(self):
        pass

    def predict(self, *inputs, **kwargs):
        # Reproduce the original 4% positive regression residual / 10% abstention.
        return {"source": "market-ml", "accepted": inputs[2] <= .05,
                "harm_usd": .07 if inputs[2] <= .05 else None,
                "p_profitable": .01, "interval_usd": [0, .3]}


def test_ml_residual_cannot_imply_harm_without_profitable_economics(snapshot):
    report = live.assess_mainnet(next(iter(POOLS)), 1000, .04, model=ResidualModel())
    assert report["source"] == "exact-v2" and report["harm_usd"] == 0
    assert report["model_estimate"]["harm_usd"] == .07
    assert [r["slippage"] for r in report["comparison"]] == [.04, .10]
    assert all(r["harm_usd"] == r["p_profitable"] == 0 for r in report["comparison"])
    assert not report["comparison"][1]["model_estimate"]["accepted"]
    assert report["interval_usd"] is None  # An ML interval must not qualify exact output.


def test_comparison_reads_one_snapshot_and_shares_all_other_assumptions(snapshot):
    snapshot.liquidity = 1_000_000
    report = live.assess_mainnet(next(iter(POOLS)), 10000, .02)
    rows = report["comparison"]
    assert [r["slippage"] for r in rows] == [.02, .04, .10]
    assert rows[0]["harm_usd"] == report["harm_usd"] > 0
    assert len({r["economics"]["benign_out_usd"] for r in rows}) == 1
    assert len({r["economics"]["attacker_gas_usd"] for r in rows}) == 1
    assert rows[0]["harm_usd"] <= rows[1]["harm_usd"] <= rows[2]["harm_usd"]
    assert [m for m, _ in snapshot.calls].count("eth_getBlockByNumber") == 1
    assert [m for m, _ in snapshot.calls].count("eth_gasPrice") == 1
    assert all(p[1] == "0x123" for m, p in snapshot.calls if m == "eth_call")


@pytest.mark.parametrize("amount,liquidity", [(1000,10_000_000), (10000,1_000_000),
                                            (100000,1_000_000)])
def test_relaxing_slippage_preserves_profitable_opportunities(snapshot, amount, liquidity):
    snapshot.liquidity = liquidity
    outcomes = [live.assess_mainnet(next(iter(POOLS)), amount, s)
                for s in [.001,.01,.04,.05,.10,.20]]
    assert [r["p_profitable"] for r in outcomes] == sorted(r["p_profitable"] for r in outcomes)
    harms = [r["harm_usd"] for r in outcomes]
    assert harms == sorted(harms)


def test_broken_ml_does_not_hide_available_economic_calculation(snapshot):
    class BrokenModel(ResidualModel):
        def predict(self, *a, **k):
            raise RuntimeError("model unavailable")

    report = live.assess_mainnet(next(iter(POOLS)), 1000, model=BrokenModel())
    assert report["source"] == "exact-v2" and report["model_estimate"] is None
    assert any("inference unavailable" in reason for reason in report["reasons"])
