"""Guardian — protocol-side prediction: "is this pending transaction an attack on my protocol?"

For each new pending tx the Guardian replays it on the shadow fork and extracts attack features:
  * flash loan taken? amount vs pool liquidity
  * max intra-tx spot-price deviation of each AMM pool vs its TWAP
  * protected protocol's value change (new bad debt / drained liquidity)
  * sender net profit
  * borrow taken against a spot price far from the TWAP
A rule-based, explainable score turns the features into BENIGN / SUSPICIOUS / CRITICAL. Flash-loan arbitrage
(T3) has a flash loan and profit but no protocol loss and converges prices → benign (no false positive).
Sandwiches are flagged structurally (two same-sender txs bracketing a victim on one pool by tip).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from web3 import Web3

from . import config, store
from .builder import Searcher
from .chain import Chain, role_of
from .responder import Responder
from .shadow import ShadowFork, pending_to_simtx

FLASH_TOPIC = Web3.keccak(text="FlashLoan(address,address,address,uint256,uint256)").to_0x_hex()
SYNC_TOPIC = Web3.keccak(text="Sync(uint256,uint256)").to_0x_hex()
BORROW_TOPIC = Web3.keccak(text="Borrow(address,uint256,uint256)").to_0x_hex()


@dataclass
class Features:
    flash_loan: bool = False
    flash_amount: float = 0.0
    flash_vs_pool: float = 0.0  # flash amount / pool reserve of that token
    max_price_deviation: float = 0.0  # max |spot - twap| / twap across pools, intra-tx
    protocol_loss_usd: float = 0.0  # drop in a protected market's debt-token liquidity
    protocol: str | None = None
    sender_profit_usd: float = 0.0
    borrow_at_deviation: bool = False
    reverted: bool = False
    note: str = ""


@dataclass
class Verdict:
    tx_hash: str
    sender: str
    sender_role: str | None
    label: str  # BENIGN / SUSPICIOUS / CRITICAL
    severity: str
    score: float
    reasons: list[str]
    features: Features
    target: str | None = None
    latency_ms: float = 0.0
    extra: dict = field(default_factory=dict)


PROTECTED = ["vulnerable", "safe"]


class Guardian(Searcher):
    name = "guardian"

    def __init__(self, chain: Chain, shadow: ShadowFork, respond: bool = False, log=None):
        self.chain = chain
        self.shadow = shadow
        self.respond = respond
        self.log = log or (lambda *a, **k: None)
        self.seen: set[str] = set()
        self.verdicts: list[Verdict] = []
        self.responder = Responder(chain, log=self.log) if respond else None

    # ------------------------------------------------------- replay + features
    def analyze(self, tx: dict) -> Verdict:
        t0 = time.perf_counter()
        c = self.chain
        d = c.deployments["contracts"]
        usdc, weth = d["USDC"].lower(), d["WETH"].lower()
        self.shadow.sync()
        sim = pending_to_simtx(tx)
        sender = Web3.to_checksum_address(tx["from"])

        twap = c.c("oracle").functions.twap().call()

        def spot(ch, pool):
            r0, r1, _ = ch.c(pool).functions.getReserves().call()
            return (r1 * 10**18 // r0) if r0 and r1 else 0

        def before(ch):
            return {"usdc": ch.balance("USDC", sender), "weth": ch.balance("WETH", sender),
                    "spotA": spot(ch, "ammA"), "spotB": spot(ch, "ammB"),
                    "liq": {m: ch.c(m).functions.liquidity().call() for m in PROTECTED}}

        feats = Features()

        def after(ch):
            return {"usdc": ch.balance("USDC", sender), "weth": ch.balance("WETH", sender),
                    "spotA": spot(ch, "ammA"), "spotB": spot(ch, "ammB"),
                    "liq": {m: ch.c(m).functions.liquidity().call() for m in PROTECTED}}

        res = self.shadow.simulate([sim], before=before, after=after)
        feats.reverted = res.receipts[0]["status"] != 1
        rcpt = res.receipts[0]
        price = c.eth_price()

        # flash loans
        for lg in rcpt["logs"]:
            topic0 = lg["topics"][0].to_0x_hex() if lg["topics"] else ""
            if topic0 == FLASH_TOPIC:
                feats.flash_loan = True
                data = lg["data"] if isinstance(lg["data"], bytes) else bytes.fromhex(lg["data"][2:])
                amt = int.from_bytes(data[0:32], "big") if len(data) >= 32 else 0  # amount (1st non-indexed arg)
                feats.flash_amount = amt / config.E
                token = "0x" + lg["topics"][3].hex()[-40:]
                r0, r1 = c.reserves("ammA")
                pool_res = r1 if token.lower() == usdc else r0
                feats.flash_vs_pool = feats.flash_amount * config.E / pool_res if pool_res else 0
            if topic0 == BORROW_TOPIC:
                borrow_price = int.from_bytes(lg["data"][32:64], "big") if isinstance(lg["data"], bytes) else 0
                tw = twap
                if tw and abs(borrow_price - tw) / tw > config.PRICE_DEVIATION_ALERT:
                    feats.borrow_at_deviation = True

        # peak spot-vs-TWAP deviation the tx leaves on the oracle pool (oracle attacks leave it pumped;
        # arbitrage converges prices and so leaves only a small deviation)
        dev_a = abs(res.after["spotA"] - twap) / twap if twap else 0.0
        dev_a_before = abs(res.before["spotA"] - twap) / twap if twap else 0.0
        feats.max_price_deviation = max(dev_a, dev_a_before)
        # profit & protocol loss
        d_usdc = res.after["usdc"] - res.before["usdc"]
        d_weth = res.after["weth"] - res.before["weth"]
        feats.sender_profit_usd = (d_usdc + d_weth * price) / config.E
        worst = 0.0
        for m in PROTECTED:
            loss = (res.before["liq"][m] - res.after["liq"][m]) / config.E
            if loss > worst:
                worst, feats.protocol = loss, m
        feats.protocol_loss_usd = worst

        v = self.score(tx, feats)
        v.latency_ms = (time.perf_counter() - t0) * 1000
        return v

    # ------------------------------------------------------------- scoring
    def score(self, tx: dict, f: Features) -> Verdict:
        reasons = []
        score = 0.0
        if f.flash_loan:
            reasons.append(f"flash loan {f.flash_amount:,.0f} ({f.flash_vs_pool:.1f}× the pool)")
            score += 0.15
        if f.max_price_deviation > config.PRICE_DEVIATION_ALERT:
            reasons.append(f"intra-tx price deviation {f.max_price_deviation * 100:.0f}% (> {config.PRICE_DEVIATION_ALERT * 100:.0f}%)")
            score += 0.3
        if f.protocol_loss_usd > 1.0:
            reasons.append(f"{f.protocol} loses ${f.protocol_loss_usd:,.0f}")
            score += 0.4
        if f.sender_profit_usd > 1.0:
            reasons.append(f"sender profits ${f.sender_profit_usd:,.0f}")
            score += 0.1
        if f.borrow_at_deviation:
            reasons.append("borrow priced at a manipulated (non-TWAP) spot price")
            score += 0.2

        # decision: oracle-manipulation signature = flash loan ∧ big deviation ∧ protocol loss ∧ sender profit
        critical = f.flash_loan and f.max_price_deviation > config.PRICE_DEVIATION_ALERT \
            and f.protocol_loss_usd > 1.0 and f.sender_profit_usd > 1.0
        if critical:
            label, severity = "CRITICAL", "critical"
        elif f.protocol_loss_usd > 1.0 and f.sender_profit_usd > 1.0:
            label, severity = "CRITICAL", "critical"  # protocol drained without a flash loan
        elif f.flash_loan and f.max_price_deviation > config.PRICE_DEVIATION_ALERT and f.protocol_loss_usd <= 1.0:
            # price moved but nobody lost: this is benign arbitrage (T3) — explicitly NOT an attack
            label, severity = "BENIGN", "info"
            reasons.append("no protocol loss and prices converge → benign arbitrage")
        elif score >= 0.4:
            label, severity = "SUSPICIOUS", "warning"
        else:
            label, severity = "BENIGN", "info"
        if f.reverted:
            reasons.append("reverts in simulation")
        return Verdict(tx_hash=tx["hash"], sender=tx["from"], sender_role=role_of(tx["from"]), label=label,
                       severity=severity, score=round(score, 2), reasons=reasons, features=f, target=f.protocol)

    # ---------------------------------------------------- mempool integration
    def on_pending(self, chain: Chain, pending: list[dict]) -> int:
        responded = 0
        # structural sandwich detection across the pending set
        sandwich_hashes = detect_sandwich(chain, pending)
        for tx in pending:
            h = tx["hash"].lower()
            if h in self.seen or role_of(tx["from"]) == "guardian":
                continue
            self.seen.add(h)
            try:
                v = self.analyze(tx)
            except Exception as exc:
                self.log(f"[guardian] could not analyze {h[:10]}: {exc}")
                store.mark_seen([h])
                continue
            if h in sandwich_hashes and v.label == "BENIGN":
                v.label, v.severity = "SUSPICIOUS", "warning"
                v.reasons.append("part of a sandwich bundle (same sender brackets a victim by tip)")
            self.verdicts.append(v)
            action = ""
            if v.label == "CRITICAL" and self.respond and v.target:
                attack_tip = int(tx.get("maxPriorityFeePerGas", "0x0"), 16)
                h = self.responder.pause(v.target, attack_tip)
                if h:
                    action = f"pause({v.target}) sent (ordered before the attack)"
                    responded += 1
            store.add_alert(v.tx_hash, v.sender, v.label, v.severity, v.score, _feat_dict(v.features),
                            action=action, latency_ms=v.latency_ms, block=chain.block_number)
            store.mark_seen([h])
            self.log(f"[guardian] {v.label} {v.tx_hash[:12]} ({v.sender_role or 'user'}): "
                     f"{'; '.join(v.reasons) or 'ordinary activity'}"
                     + (f"  → {action}" if action else ""))
        return responded


def _feat_dict(f: Features) -> dict:
    return {"flash_loan": f.flash_loan, "flash_amount": round(f.flash_amount, 2),
            "flash_vs_pool": round(f.flash_vs_pool, 2), "max_price_deviation": round(f.max_price_deviation, 4),
            "protocol_loss_usd": round(f.protocol_loss_usd, 2), "sender_profit_usd": round(f.sender_profit_usd, 2),
            "borrow_at_deviation": f.borrow_at_deviation, "protocol": f.protocol, "reverted": f.reverted}


def detect_sandwich(chain: Chain, pending: list[dict]) -> set[str]:
    """Two txs from the same sender, same pool, opposite directions, bracketing by tip → both flagged."""
    from .harness.sandwich_bot import decode_swap
    by_sender: dict[str, list] = {}
    for tx in pending:
        sw = decode_swap(chain, tx)
        if sw:
            by_sender.setdefault(tx["from"].lower(), []).append((tx, sw))
    flagged = set()
    for sender, items in by_sender.items():
        if len(items) >= 2:
            pools = {s["amm"] for _, s in items}
            dirs = {s["token_in"].lower() for _, s in items}
            if len(pools) == 1 and len(dirs) == 2:
                for tx, _ in items:
                    flagged.add(tx["hash"].lower())
    return flagged

