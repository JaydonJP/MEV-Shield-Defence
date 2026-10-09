"""web3 connection, contract ABIs, accounts, signing, mining control and snapshots."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from eth_account import Account
from web3 import Web3
from web3.contract import Contract

from . import config

# contract name -> forge artifact path (relative to contracts/out)
ARTIFACTS = {
    "Token": "Token.sol/Token.json",
    "SimpleAMM": "SimpleAMM.sol/SimpleAMM.json",
    "FlashLender": "FlashLender.sol/FlashLender.json",
    "TWAPOracle": "TWAPOracle.sol/TWAPOracle.json",
    "VulnerableLending": "VulnerableLending.sol/VulnerableLending.json",
    "SafeLending": "SafeLending.sol/SafeLending.json",
    "ProtectedRouter": "ProtectedRouter.sol/ProtectedRouter.json",
    "CommitRevealSwap": "CommitRevealSwap.sol/CommitRevealSwap.json",
    "VulnerableDAO": "Governance.sol/VulnerableDAO.json",
    "SnapshotDAO": "Governance.sol/SnapshotDAO.json",
    "FlashArbitrage": "FlashArbitrage.sol/FlashArbitrage.json",
    "SandwichAttacker": "SandwichAttacker.sol/SandwichAttacker.json",
    "OracleAttacker": "OracleAttacker.sol/OracleAttacker.json",
    "GovernanceAttacker": "GovernanceAttacker.sol/GovernanceAttacker.json",
}

# deployment key -> contract type
DEPLOYMENT_TYPES = {
    "WETH": "Token", "USDC": "Token", "GOV": "Token",
    "ammA": "SimpleAMM", "ammB": "SimpleAMM",
    "lender": "FlashLender", "oracle": "TWAPOracle",
    "vulnerable": "VulnerableLending", "safe": "SafeLending",
    "router": "ProtectedRouter", "commitReveal": "CommitRevealSwap",
    "vulnerableDAO": "VulnerableDAO", "snapshotDAO": "SnapshotDAO",
    "flashArb": "FlashArbitrage", "sandwich": "SandwichAttacker",
    "oracleAttacker": "OracleAttacker", "govAttacker": "GovernanceAttacker",
}


@lru_cache(maxsize=None)
def artifact(name: str) -> dict:
    path = config.OUT_DIR / ARTIFACTS[name]
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — run `forge build` in contracts/")
    data = json.loads(path.read_text())
    return {"abi": data["abi"], "bytecode": data["bytecode"]["object"]}


@dataclass
class Acct:
    role: str
    key: str

    @property
    def address(self) -> str:
        return Account.from_key(self.key).address


def account(role: str) -> Acct:
    return Acct(role, config.ANVIL_KEYS[config.ROLES[role]])


def role_of(address: str) -> str | None:
    address = address.lower()
    for role in config.ROLES:
        if account(role).address.lower() == address:
            return role
    return None


class Chain:
    """Thin wrapper over a web3 connection to one Anvil node (main devnet or shadow fork)."""

    def __init__(self, rpc: str = config.MAIN_RPC, deployments: dict | None = None):
        self.rpc = rpc
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 60}))
        self._deployments = deployments
        self._contracts: dict[str, Contract] = {}

    # ----------------------------------------------------------- raw RPC
    def rpc_call(self, method: str, params: list | None = None):
        resp = self.w3.provider.make_request(method, params or [])
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error']}")
        return resp.get("result")

    def mine(self, n: int = 1) -> None:
        for _ in range(n):
            self.rpc_call("evm_mine")

    def set_automine(self, on: bool) -> None:
        self.rpc_call("evm_setAutomine", [on])

    def snapshot(self) -> str:
        return self.rpc_call("evm_snapshot")

    def revert(self, snap_id: str) -> bool:
        return self.rpc_call("evm_revert", [snap_id])

    def txpool(self) -> list[dict]:
        """Pending transactions as a flat list (anvil `txpool_content`)."""
        content = self.rpc_call("txpool_content") or {}
        txs = []
        for sender_txs in (content.get("pending") or {}).values():
            for tx in sender_txs.values():
                txs.append(tx)
        return txs

    def drop_all_pending(self) -> None:
        self.rpc_call("anvil_dropAllTransactions")

    @property
    def block_number(self) -> int:
        return self.w3.eth.block_number

    def base_fee(self) -> int:
        return self.w3.eth.get_block("pending").get("baseFeePerGas") or self.w3.eth.get_block("latest")["baseFeePerGas"]

    # ------------------------------------------------------- deployments
    @property
    def deployments(self) -> dict:
        if self._deployments is None:
            if not config.DEPLOYMENTS_FILE.exists():
                raise FileNotFoundError("no deployments.json — run `mevshield up`")
            self._deployments = json.loads(config.DEPLOYMENTS_FILE.read_text())
        return self._deployments

    def c(self, key: str) -> Contract:
        """Contract handle by deployment key (e.g. 'ammA', 'USDC', 'vulnerable')."""
        if key not in self._contracts:
            addr = self.deployments["contracts"][key]
            abi = artifact(DEPLOYMENT_TYPES[key])["abi"]
            self._contracts[key] = self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=abi)
        return self._contracts[key]

    def at(self, name: str, address: str) -> Contract:
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=artifact(name)["abi"])

    def name_of(self, address: str | None) -> str | None:
        if not address:
            return None
        address = address.lower()
        for key, addr in self.deployments["contracts"].items():
            if addr.lower() == address:
                return key
        return role_of(address)

    # ------------------------------------------------------------- txs
    def build_tx(self, acct: Acct, fn=None, *, to: str | None = None, data: str = "0x", value: int = 0,
                 tip: int = config.DEFAULT_TIP, gas: int | None = None, nonce: int | None = None) -> dict:
        sender = acct.address
        if nonce is None:
            nonce = self.w3.eth.get_transaction_count(sender, "pending")
        tx = {
            "from": sender, "nonce": nonce, "chainId": config.CHAIN_ID, "type": 2, "value": value,
            "maxPriorityFeePerGas": tip, "maxFeePerGas": 3 * self.base_fee() + tip,
        }
        if fn is not None:
            tx = fn.build_transaction({**tx, "gas": gas or 1_500_000})
        else:
            tx.update({"to": to, "data": data})
        if gas is None:
            try:
                tx["gas"] = int(self.w3.eth.estimate_gas({k: v for k, v in tx.items() if k != "gas"}) * 1.3) + 20_000
            except Exception:
                tx["gas"] = 1_500_000
        else:
            tx["gas"] = gas
        return tx

    def sign(self, acct: Acct, tx: dict) -> bytes:
        tx = {k: v for k, v in tx.items() if k != "from"}
        return Account.sign_transaction(tx, acct.key).raw_transaction

    def send(self, acct: Acct, fn=None, **kw) -> str:
        """Sign and submit to the mempool (no mining). Returns the tx hash."""
        raw = self.sign(acct, self.build_tx(acct, fn, **kw))
        return self.w3.eth.send_raw_transaction(raw).to_0x_hex()

    def send_raw(self, raw: bytes | str) -> str:
        return self.w3.eth.send_raw_transaction(raw).to_0x_hex()

    def transact(self, acct: Acct, fn=None, **kw) -> dict:
        """Submit and immediately mine a block (admin/setup actions). Raises if the tx reverted."""
        h = self.send(acct, fn, **kw)
        self.mine()
        rcpt = self.w3.eth.get_transaction_receipt(h)
        if rcpt["status"] != 1:
            raise RuntimeError(f"tx {h} reverted: {self.revert_reason(h)}")
        return rcpt

    def receipt(self, h: str):
        return self.w3.eth.get_transaction_receipt(h)

    def revert_reason(self, h: str) -> str:
        tx = self.w3.eth.get_transaction(h)
        call = {"from": tx["from"], "to": tx["to"], "data": tx["input"], "value": tx["value"], "gas": tx["gas"]}
        try:
            self.w3.eth.call(call, tx["blockNumber"] - 1 if tx.get("blockNumber") else "latest")
            return "unknown"
        except Exception as exc:  # web3 surfaces the revert string in the exception
            return _clean_reason(exc)

    # ---------------------------------------------------------- helpers
    def balance(self, token: str, who: str) -> int:
        return self.c(token).functions.balanceOf(Web3.to_checksum_address(who)).call()

    def reserves(self, amm: str = "ammA") -> tuple[int, int]:
        r0, r1, _ = self.c(amm).functions.getReserves().call()
        return r0, r1

    def eth_price(self) -> float:
        """USDC per WETH (≈ ETH) from pool A, used to convert gas costs to USDC."""
        r0, r1 = self.reserves("ammA")
        return r1 / r0

    def gas_cost_usd(self, gas: int, tip: int = config.DEFAULT_TIP) -> float:
        return gas * (self.base_fee() + tip) / config.E * self.eth_price()


def _clean_reason(exc: Exception) -> str:
    msg = str(exc)
    for marker in ("execution reverted: ", "revert: ", "reverted with reason string '"):
        if marker in msg:
            msg = msg.split(marker, 1)[1]
    return msg.strip("'\")( ").split("',")[0][:120]


def fmt(amount: int, decimals: int = 2) -> str:
    return f"{amount / config.E:,.{decimals}f}"
