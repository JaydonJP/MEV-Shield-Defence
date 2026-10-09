"""Private relay: holds signed transactions off the public mempool and hands them to the block builder.

Trust model (explicit, simplified): the relay operator and the builder are trusted not to leak or front-run
the order. Searchers watching `txpool_content` never see relayed transactions, because the builder injects
them at the moment it seals the block (see builder.BlockBuilder.build_block).
"""
from __future__ import annotations

from web3 import Web3

from .. import store
from ..chain import Acct, Chain


class PrivateRelay:
    name = "private-relay"

    def __init__(self, chain: Chain):
        self.chain = chain

    def submit(self, acct: Acct, fn, tip: int, gas: int, nonce: int | None = None) -> str:
        tx = self.chain.build_tx(acct, fn, tip=tip, gas=gas, nonce=nonce)
        raw = self.chain.sign(acct, tx)
        tx_hash = Web3.keccak(raw).to_0x_hex()
        store.relay_push(Web3.to_hex(raw), tx_hash)
        return tx_hash

    @staticmethod
    def pending_count() -> int:
        return store.query("SELECT COUNT(*) AS n FROM relay_queue WHERE sent=0")[0]["n"]
