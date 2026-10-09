"""Block builder for the main devnet (which runs with --no-mining --order fees).

Building a block = (1) give searchers a window to see the public mempool and react — in-process searchers
are called directly, an external Guardian process is waited for via its heartbeat/seen-set — (2) inject
private-relay transactions, which therefore never sit in the public mempool, (3) mine. Anvil orders the
block by priority fee, which is what makes front-running (and the Guardian's counter-front-run) possible.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from web3 import Web3

from .. import store
from .connection import Chain, role_of


class Searcher:
    """Anything that watches the mempool before a block is built (bots, the Guardian)."""

    name = "searcher"

    def on_pending(self, chain: Chain, pending: list[dict]) -> int:
        """React to the pending txs; return how many new txs were submitted."""
        return 0


@dataclass
class TxSummary:
    hash: str
    sender: str
    sender_role: str | None
    to: str | None
    fn: str
    tip_gwei: float
    status: int
    gas_used: int
    private: bool = False
    labels: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class BlockSummary:
    number: int
    txs: list[TxSummary]

    def tx(self, h: str) -> TxSummary | None:
        h = h.lower()
        return next((t for t in self.txs if t.hash.lower() == h), None)

    def index(self, h: str) -> int:
        h = h.lower()
        return next((i for i, t in enumerate(self.txs) if t.hash.lower() == h), -1)


DECODE_ORDER = ["router", "commitReveal", "ammA", "ammB", "sandwich", "oracleAttacker", "flashArb", "govAttacker",
                "vulnerable", "safe", "vulnerableDAO", "snapshotDAO", "oracle", "USDC", "WETH", "GOV"]


def describe_call(chain: Chain, to: str | None, data: str) -> str:
    name = chain.name_of(to) if to else "create"
    if not to or not data or data == "0x":
        return f"{name}" if to else "contract creation"
    if name in chain.deployments["contracts"]:
        try:
            fn, _ = chain.c(name).decode_function_input(data)
            return f"{name}.{fn.fn_name}"
        except Exception:
            pass
    return f"{name}.?"


class BlockBuilder:
    def __init__(self, chain: Chain, searchers: list[Searcher] | None = None, external_window: float = 4.0,
                 log=None):
        self.chain = chain
        self.searchers = searchers or []
        self.external_window = external_window
        self.log = log or (lambda *a, **k: None)

    def _wait_external_guardian(self) -> None:
        if not store.is_alive("guardian"):
            return
        deadline = time.time() + self.external_window
        while time.time() < deadline:
            pending = [t["hash"] for t in self.chain.txpool()]
            if not pending or len(store.seen(pending)) == len(pending):
                return
            time.sleep(0.05)

    def build_block(self) -> BlockSummary:
        for _ in range(4):  # let searchers react to each other (e.g. guardian reacts to a bot)
            self._wait_external_guardian()
            new = sum(s.on_pending(self.chain, self.chain.txpool()) for s in self.searchers)
            if new == 0:
                break
        self._wait_external_guardian()

        private = set()
        for row in store.relay_pop_all():  # private relay: straight into the block
            try:
                private.add(self.chain.send_raw(row["raw"]).lower())
            except Exception as exc:
                self.log(f"[relay] could not include {row['tx_hash'][:10]}: {exc}")
        self.chain.mine()
        return self.summarize("latest", private)

    def summarize(self, block_id="latest", private: set | None = None) -> BlockSummary:
        blk = self.chain.w3.eth.get_block(block_id, full_transactions=True)
        base = blk.get("baseFeePerGas", 0)
        txs = []
        for tx in blk["transactions"]:
            rcpt = self.chain.receipt(tx["hash"])
            tip = min(tx.get("maxPriorityFeePerGas", 0), tx.get("maxFeePerGas", 0) - base)
            h = tx["hash"].to_0x_hex()
            txs.append(TxSummary(
                hash=h, sender=tx["from"], sender_role=role_of(tx["from"]), to=self.chain.name_of(tx.get("to")),
                fn=describe_call(self.chain, tx.get("to"), Web3.to_hex(tx["input"])), tip_gwei=tip / 1e9,
                status=rcpt["status"], gas_used=rcpt["gasUsed"], private=h.lower() in (private or set()),
            ))
        alerts = {a["tx_hash"].lower(): a for a in store.query(
            "SELECT tx_hash, label, severity FROM alerts ORDER BY id DESC LIMIT 200")}
        for t in txs:
            if t.hash.lower() in alerts:
                t.labels.append(f"{alerts[t.hash.lower()]['label']}")
        summary = BlockSummary(blk["number"], txs)
        store.add_block(blk["number"], [t.as_dict() for t in txs])
        return summary
