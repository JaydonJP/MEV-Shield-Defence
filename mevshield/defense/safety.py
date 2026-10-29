"""State-bound previews for supported LOCAL actions, with receipt reconciliation.

No arbitrary calldata, mainnet signing, or 'safe transaction' verdict. A preview is
evidence about one simulated state, not a promise about later ordering.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import secrets
import time
from decimal import Decimal

from web3 import Web3

from .. import config, store
from ..chain import account, fn_simtx


def fingerprint(transaction: dict) -> str:
    """Canonical, lossless binding, including fees, nonce and calldata."""
    canonical = {k: str(v).lower() for k, v in sorted(transaction.items())}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def reconcile(predicted: dict, observed: dict) -> dict:
    differences = {k: {"predicted": str(v), "observed": str(observed.get(k))}
                   for k, v in predicted.items() if v != observed.get(k)}
    return {"matches": not differences, "differences": differences,
            "scope": "token and lending accounting changes; native gas is reported separately"}


class SafetyReports:
    def __init__(self, chain, shadow, ttl: float = 120):
        self.chain, self.shadow, self.ttl = chain, shadow, ttl
        self.reports = {}

    def _verify_local(self):
        if self.chain.w3.eth.chain_id != config.CHAIN_ID:
            raise ValueError("Safety execution supports only the local lab chain")
        if self.chain.txpool():
            raise ValueError("Pending transactions exist; finish or reset them before previewing")
        if store.relay_pending_count():
            raise ValueError("Private relay queue is not empty; finish or reset it first")

    def _state(self, ch, user, market):
        data = {"USDC": ch.balance("USDC", user), "WETH": ch.balance("WETH", user)}
        if market:
            m = ch.c(market)
            data.update(collateral=m.functions.collateralOf(user).call(), debt=m.functions.debtOf(user).call())
        return data

    @staticmethod
    def _delta(before, after):
        return {k: after[k] - v for k, v in before.items()}

    def prepare(self, action="swap", role="alice", amount=1000, pool="ammA", token_in="USDC",
                market="safe", slippage=0.02):
        if action not in ("swap", "deposit", "borrow", "repay", "withdraw") \
                or role not in ("alice", "bob", "carol") or pool not in ("ammA", "ammB") \
                or market not in ("safe", "firewall") or token_in not in ("USDC", "WETH"):
            raise ValueError("Unsupported action, user, pool, market or token")
        if not math.isfinite(amount) or not 0 < amount <= 100000 or not math.isfinite(slippage) \
                or not 0 < slippage <= 0.2:
            raise ValueError("Invalid amount or slippage")
        self._verify_local()
        c, d, acct = self.chain, self.chain.deployments["contracts"], account(role)
        block = c.w3.eth.get_block("latest")
        units = int(Decimal(str(amount)) * config.E)
        if units <= 0:
            raise ValueError("Amount is below one token base unit")
        # Anvil does not mine while idle. Inclusion can use wall time far beyond
        # the last block's timestamp, so a last-block-only deadline may already expire.
        deadline = c.deadline()
        chosen_market = None if action == "swap" else market
        if action == "swap":
            quote = c.c(pool).functions.quote(d[token_in], units).call(block_identifier=block["number"])
            minimum = int(Decimal(quote) * (1 - Decimal(str(slippage))))
            fn = c.c("router").functions.swapExactIn(d[pool], d[token_in], units, minimum, deadline)
            spend_token, spender = token_in, d["router"]
        else:
            fn = getattr(c.c(market).functions, action)(units)
            spend_token = "WETH" if action == "deposit" else "USDC" if action == "repay" else None
            spender = d[market]
            minimum = None
        transaction = c.build_tx(acct, fn, gas=1_000_000)
        # Runtime hash binds the known deployed recipient, not just a friendly label.
        code_hash = Web3.keccak(c.w3.eth.get_code(fn.address)).to_0x_hex()
        allowance = None if spend_token is None else c.c(spend_token).functions.allowance(
            acct.address, spender).call(block_identifier=block["number"])
        self.shadow.sync(block["number"])
        result = self.shadow.simulate([fn_simtx(acct.address, fn, gas=transaction["gas"])],
            before=lambda ch: self._state(ch, acct.address, chosen_market),
            after=lambda ch: self._state(ch, acct.address, chosen_market))
        receipt = result.receipts[0]
        passed = receipt["status"] == 1
        reason = None
        if not passed:
            try:
                c.w3.eth.call({k: transaction[k] for k in ("from", "to", "data", "value", "gas")}, "pending")
            except Exception as exc:
                reason = str(exc)[:240]
        report = {"id": secrets.token_hex(16), "status": "previewed" if passed else "blocked",
                  "simulation_passed": passed, "created_at": time.time(), "expires_at": time.time() + self.ttl,
                  "chain_id": config.CHAIN_ID, "block": block["number"],
                  "block_hash": block["hash"].to_0x_hex(), "code_hash": code_hash,
                  "action": action, "role": role, "market": chosen_market,
                  "pool": pool if action == "swap" else None,
                  "token_in": token_in if action == "swap" else None,
                  "binding": fingerprint(transaction), "transaction": transaction,
                  "predicted_changes": self._delta(result.before, result.after),
                  "gas_estimate": receipt["gasUsed"],
                  "estimated_gas_usd": c.gas_cost_usd(receipt["gasUsed"]),
                  "approval": None if allowance is None else {"token": spend_token, "spender": spender,
                      "allowance": str(allowance), "sufficient": allowance >= units,
                      "unlimited": allowance == 2**256 - 1, "new_approval_requested": False},
                  "min_out": minimum, "deadline": deadline if action == "swap" else None,
                  "reason": reason or (None if passed else "Contract policy rejected this action"),
                  "warnings": ["Local mock assets; preview simulates this action alone, without intervening trades",
                               "Simulation success does not guarantee future execution or MEV protection"],
                  "receipt": None}
        if allowance == 2**256 - 1:
            report["warnings"].append("The lab seeded an unlimited allowance for this known spender")
        self.reports = {k: v for k, v in self.reports.items() if v["expires_at"] > time.time()}
        while len(self.reports) >= 100:
            self.reports.pop(next(iter(self.reports)))
        self.reports[report["id"]] = report
        self._save(report)
        return self._public(report)

    @staticmethod
    def _public(report):
        out = copy.deepcopy(report)
        # Browser numbers cannot represent uint256. Preserve every integer losslessly.
        out["transaction"] = {k: hex(v) if isinstance(v, int) else v for k, v in out["transaction"].items()}
        out["predicted_changes"] = {k: str(v) for k, v in out["predicted_changes"].items()}
        if out["min_out"] is not None:
            out["min_out"] = str(out["min_out"])
        return out

    def execute(self, report_id, binding):
        self._verify_local()
        r = self.reports.get(report_id)
        if r is None:
            raise ValueError("Preview not found or expired; create another preview")
        if r["status"] != "previewed" or not r["simulation_passed"]:
            raise ValueError("Only an unused successful preview can execute")
        if binding != r["binding"] or fingerprint(r["transaction"]) != binding:
            raise ValueError("Transaction binding changed; create another preview")
        c, tx = self.chain, r["transaction"]
        block = c.w3.eth.get_block("latest")
        if time.time() >= r["expires_at"] or block["hash"].to_0x_hex() != r["block_hash"] \
                or c.w3.eth.get_transaction_count(tx["from"], "pending") != tx["nonce"] \
                or Web3.keccak(c.w3.eth.get_code(tx["to"])).to_0x_hex() != r["code_hash"]:
            raise ValueError("Preview state is stale; simulate again")
        if r["deadline"] is not None and c.w3.eth.get_block("pending")["timestamp"] > r["deadline"]:
            raise ValueError("Preview deadline is stale; simulate again")
        before = self._state(c, tx["from"], r["market"])
        r["status"] = "submitting"  # one-shot even if the submission result is uncertain
        try:
            h = c.send_raw(c.sign(account(r["role"]), tx))
            r["status"], r["tx_hash"] = "submitted", h
            c.mine()
            receipt = c.receipt(h)
            delta = self._delta(before, self._state(c, tx["from"], r["market"]))
            check = reconcile(r["predicted_changes"], delta)
            r.update(status="included" if receipt["status"] == 1 else "reverted",
                     receipt={"hash": h, "block": receipt["blockNumber"], "status": receipt["status"],
                              "gas_used": receipt["gasUsed"],
                              "gas_paid_wei": str(receipt["gasUsed"] * receipt["effectiveGasPrice"]),
                              "observed_changes": {k: str(v) for k, v in delta.items()},
                              "reconciliation": check, "finality": "local inclusion; not public-chain finality"})
        except Exception:
            r["warnings"].append("Submission or receipt check failed; inspect the hash before retrying")
            raise
        finally:
            self._save(r)
        return self._public(r)

    def _save(self, report):
        store.save_safety_report(report["id"], self._public(report))
