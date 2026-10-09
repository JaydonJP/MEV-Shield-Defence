"""Circuit-breaker responder: when the Guardian flags a critical attack sitting in the mempool, send the
targeted protocol's `pause()` with a HIGHER priority fee than the attack, so the block orders it first and
the attack reverts (Forta / Hypernative-style pre-execution defense)."""
from __future__ import annotations

from .. import config
from ..chain import Chain, account

PAUSABLE = ("vulnerable", "safe", "vulnerableDAO", "snapshotDAO")


class Responder:
    def __init__(self, chain: Chain, log=print):
        self.chain = chain
        self.acct = account("guardian")
        self.log = log
        self.responded: dict[str, str] = {}  # protocol -> pause tx hash

    def pause(self, protocol: str, attack_tip: int) -> str | None:
        if protocol not in PAUSABLE:
            return None
        c = self.chain.c(protocol)
        if c.functions.paused().call() or protocol in self.responded:
            return self.responded.get(protocol)
        tip = max(2 * attack_tip, attack_tip + 2 * config.GWEI)
        h = self.chain.send(self.acct, c.functions.pause(), tip=tip, gas=150_000)
        self.responded[protocol] = h
        self.log(f"[responder] pause() on {protocol} with tip {tip / 1e9:.1f} gwei (attack tip "
                 f"{attack_tip / 1e9:.1f} gwei) → ordered before the attack")
        return h

    def unpause_all(self) -> None:
        """Owner-side recovery after an incident (admin key)."""
        admin = account("admin")
        for p in PAUSABLE:
            if self.chain.c(p).functions.paused().call():
                self.chain.transact(admin, self.chain.c(p).functions.unpause())
        self.responded.clear()
