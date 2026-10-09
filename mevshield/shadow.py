"""Shadow fork: a second Anvil forked from the main devnet where every "what-if" runs.

`simulate()` executes a bundle of (unsigned, impersonated) transactions in exactly the given order inside
ONE block, lets the caller read state, then reverts — the main devnet and its mempool are never touched.
A cross-process file lock serialises use by the CLI, Guardian, bots and dashboard.
"""
from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from web3 import Web3

from . import config
from .chain import Chain

_LOCK_FILE = config.DATA_DIR / "shadow.lock"
_thread_lock = threading.RLock()
_depth = 0
_fh = None


@contextmanager
def _process_lock(timeout: float = 60.0):
    global _depth, _fh
    with _thread_lock:
        if _depth == 0:
            _fh = open(_LOCK_FILE, "a+")
            deadline = time.time() + timeout
            while True:
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        _fh.seek(0)
                        msvcrt.locking(_fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise TimeoutError("shadow fork is busy")
                    time.sleep(0.01)
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
            if _depth == 0:
                if sys.platform == "win32":
                    import msvcrt
                    _fh.seek(0)
                    msvcrt.locking(_fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(_fh.fileno(), fcntl.LOCK_UN)
                _fh.close()
                _fh = None


@dataclass
class SimTx:
    sender: str
    to: str | None
    data: str
    value: int = 0
    gas: int = 2_000_000
    tag: str = ""


@dataclass
class SimResult:
    receipts: list = field(default_factory=list)
    before: object = None
    after: object = None
    per_tx: list = field(default_factory=list)

    def ok(self, i: int) -> bool:
        return self.receipts[i]["status"] == 1


class ShadowFork:
    def __init__(self, main: Chain | None = None):
        self.main = main or Chain(config.MAIN_RPC)
        self.chain = Chain(config.SHADOW_RPC, deployments=self.main.deployments)
        self._forked_at: int | None = None

    def sync(self, block: int | None = None) -> int:
        """Re-fork at the main devnet's latest block (no-op if already there)."""
        target = self.main.block_number if block is None else block
        with _process_lock():
            if self._forked_at != target or self.chain.block_number < target:
                self.chain.rpc_call("anvil_reset", [{"forking": {"jsonRpcUrl": config.MAIN_RPC, "blockNumber": target}}])
                self._forked_at = target
        return target

    @contextmanager
    def session(self):
        """Exclusive use of the shadow fork; state is reverted on exit."""
        with _process_lock():
            snap = self.chain.snapshot()
            try:
                yield self.chain
            finally:
                self.chain.revert(snap)
                self.chain.set_automine(True)

    def simulate(self, txs: list[SimTx], before=None, after=None, one_block: bool = True,
                 per_tx=None) -> SimResult:
        """Run `txs` in order (in a single block when `one_block`). `before`/`after` are callables(chain).
        `per_tx(chain, i, receipt)` forces one block per tx and is called after each one (state probing)."""
        res = SimResult()
        if per_tx is not None:
            one_block = False
            res.per_tx = []
        with self.session() as ch:
            res.before = before(ch) if before else None
            senders = {Web3.to_checksum_address(t.sender) for t in txs}
            for s in senders:
                ch.rpc_call("anvil_impersonateAccount", [s])
                ch.rpc_call("anvil_setBalance", [s, hex(10**22)])
            ch.set_automine(not one_block)
            hashes = []
            for t in txs:
                tx = {"from": Web3.to_checksum_address(t.sender), "data": t.data or "0x", "value": hex(t.value),
                      "gas": hex(t.gas), "maxFeePerGas": hex(200 * config.GWEI), "maxPriorityFeePerGas": "0x0"}
                if t.to:
                    tx["to"] = Web3.to_checksum_address(t.to)
                hashes.append(ch.rpc_call("eth_sendTransaction", [tx]))
                if per_tx is not None:
                    res.per_tx.append(per_tx(ch, len(hashes) - 1, ch.w3.eth.get_transaction_receipt(hashes[-1])))
            if one_block and txs:
                ch.mine()
            res.receipts = [ch.w3.eth.get_transaction_receipt(h) for h in hashes]
            res.after = after(ch) if after else None
            for s in senders:
                ch.rpc_call("anvil_stopImpersonatingAccount", [s])
        return res

    def call_tx(self, fn_tx: dict) -> SimTx:
        return SimTx(fn_tx["from"], fn_tx.get("to"), fn_tx.get("data", "0x"), fn_tx.get("value", 0))


def fn_simtx(sender: str, fn, tag: str = "", gas: int = 2_000_000) -> SimTx:
    """SimTx from a web3 contract function call."""
    return SimTx(sender, fn.address, fn._encode_transaction_data(), 0, gas, tag)


def pending_to_simtx(tx: dict) -> SimTx:
    """SimTx replaying a pending mempool transaction (txpool_content format)."""
    val = tx.get("value", "0x0")
    gas = tx.get("gas", "0x1e8480")
    return SimTx(tx["from"], tx.get("to"), tx.get("input") or tx.get("data") or "0x",
                 int(val, 16) if isinstance(val, str) else val,
                 max(int(gas, 16) if isinstance(gas, str) else gas, 100_000), tx.get("hash", ""))


def shadow_available() -> bool:
    try:
        return Chain(config.SHADOW_RPC).w3.is_connected()
    except Exception:
        return False

