"""Read Sepolia and prepare wallet-signed transactions. No server-side signing keys.

The local attack harness deliberately never uses this client. All writes are unsigned,
chain-bound transactions returned to an EIP-1193 wallet for the user's signature.
"""
from __future__ import annotations

import json
import os
import threading
import time
from functools import lru_cache
from pathlib import Path

from web3 import Web3
from web3.exceptions import TransactionNotFound

CHAIN_ID = 11155111
RPC_URL = os.getenv("MEVSHIELD_SEPOLIA_RPC", "https://ethereum-sepolia-rpc.publicnode.com")
EXPLORER = "https://sepolia.etherscan.io"
ASSET = Path(__file__).resolve().parents[1] / "web" / "static" / "shield-vault.json"


@lru_cache(maxsize=1)
def vault_artifact() -> dict:
    if not ASSET.exists():
        raise RuntimeError("Vault artifact missing. Run python scripts/export_vault.py after forge build.")
    return json.loads(ASSET.read_text())


class PublicNetwork:
    def __init__(self, rpc: str = RPC_URL):
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        self._snapshot: dict = {}
        self._updated = 0.0
        self._lock = threading.Lock()

    def verify(self):
        if self.w3.eth.chain_id != CHAIN_ID:
            raise ValueError("RPC network mismatch: only Ethereum Sepolia (11155111) is supported.")

    def status(self) -> dict:
        with self._lock:
            if time.monotonic() - self._updated < 10:
                return self._snapshot
            base = {"name": "Ethereum Sepolia", "chain_id": CHAIN_ID, "explorer": EXPLORER,
                    "rpc_url": RPC_URL, "testnet": True}
            try:
                self.verify()
                latest = self.w3.eth.get_block("latest")
                blocks = [latest]
                for i in range(1, 5):
                    blocks.append(self.w3.eth.get_block(latest["number"] - i))
                self._snapshot = {**base, "connected": True, "observed_at": int(time.time()),
                                  "block": latest["number"], "timestamp": latest["timestamp"],
                                  "base_fee_gwei": float(latest.get("baseFeePerGas", 0)) / 1e9,
                                  "blocks": [{"number": b["number"], "hash": b["hash"].to_0x_hex(),
                                              "timestamp": b["timestamp"], "transactions": len(b["transactions"]),
                                              "gas_used": b["gasUsed"], "gas_limit": b["gasLimit"]}
                                             for b in blocks]}
            except Exception:
                # Never present an old block as a live connection, or expose a credential-bearing RPC URL.
                self._snapshot = {**base, "connected": False,
                                  "error": "Sepolia RPC unavailable or wrong network. Check MEVSHIELD_SEPOLIA_RPC."}
            self._updated = time.monotonic()
            # RPC URLs may contain API keys; the browser needs only the public fallback.
            self._snapshot["rpc_url"] = "https://ethereum-sepolia-rpc.publicnode.com"
            return self._snapshot

    def vault(self, address: str):
        self.verify()
        if not Web3.is_address(address):
            raise ValueError("Enter a valid vault address.")
        address = Web3.to_checksum_address(address)
        art = vault_artifact()
        if self.w3.eth.get_code(address).to_0x_hex().lower() != art["runtime"].lower():
            raise ValueError("This address is not a matching ShieldVault deployment on Sepolia.")
        return self.w3.eth.contract(address=address, abi=art["abi"])

    def vault_state(self, address: str, account: str) -> dict:
        if not Web3.is_address(account):
            raise ValueError("Enter a valid wallet address.")
        account = Web3.to_checksum_address(account)
        contract = self.vault(address)
        # Pin every read to the same block so the policy display is internally consistent.
        block = self.w3.eth.get_block("latest")
        read = lambda name, *args: getattr(contract.functions, name)(*args).call(block_identifier=block["number"])
        started = read("windowStart", account)
        used = read("withdrawnInWindow", account)
        limit = read("WITHDRAWAL_LIMIT")
        window = read("WINDOW")
        if block["timestamp"] >= started + window:
            used = 0
        return {"address": contract.address, "account": account, "owner": read("owner"),
                "paused": read("paused"), "balance_wei": str(read("balances", account)),
                "total_wei": str(self.w3.eth.get_balance(contract.address, block["number"])),
                "unlock_at": read("unlockAt", account), "timestamp": block["timestamp"],
                "remaining_wei": str(limit - used), "limit_wei": str(limit),
                "window_resets_at": started + window if used else None, "block": block["number"]}

    def prepare(self, action: str, account: str, address: str | None, amount_wei: str) -> dict:
        self.verify()
        if not Web3.is_address(account) or int(account, 16) == 0:
            raise ValueError("Connect a valid wallet first.")
        account = Web3.to_checksum_address(account)
        tx = {"from": account, "value": 0}
        if action == "deploy":
            tx["data"] = vault_artifact()["bytecode"]
        else:
            contract = self.vault(address or "")
            tx["to"] = contract.address
            if action in ("deposit", "withdraw"):
                try:
                    amount = int(amount_wei)
                except (ValueError, TypeError):
                    raise ValueError("Use a whole number of wei.") from None
                if not 0 < amount <= 10**16:
                    raise ValueError("Choose an amount above 0 and at most 0.01 test ETH.")
                fn = contract.functions.deposit() if action == "deposit" else contract.functions.withdraw(amount)
                tx["value"] = amount if action == "deposit" else 0
            elif action in ("pause", "unpause"):
                fn = contract.functions.setPaused(action == "pause")
            else:
                raise ValueError("Unknown vault action.")
            tx["data"] = fn._encode_transaction_data()
        # A revert is returned to the UI as a blocked policy check, before a wallet prompt/gas spend.
        try:
            gas = self.w3.eth.estimate_gas(tx)
        except Exception as exc:
            message = str(exc)
            for reason in ("cooldown active", "hourly limit", "paused", "owner only", "insufficient balance"):
                if reason in message:
                    raise ValueError(f"Blocked by contract: {reason}.") from None
            raise ValueError("Preflight failed. Check test ETH for gas, vault balance, and contract policy.") from None
        return {**tx, "value": hex(tx["value"]), "gas": hex(gas * 120 // 100), "chainId": hex(CHAIN_ID)}

    def receipt(self, tx_hash: str) -> dict:
        self.verify()
        if len(tx_hash) != 66 or not tx_hash.startswith("0x"):
            raise ValueError("Invalid transaction hash.")
        try:
            int(tx_hash[2:], 16)
        except ValueError:
            raise ValueError("Invalid transaction hash.") from None
        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return {"pending": True}
        events = []
        contract = self.w3.eth.contract(abi=vault_artifact()["abi"])
        for name in ("Deposited", "Withdrawn", "PauseChanged"):
            from web3.logs import DISCARD
            for event in getattr(contract.events, name)().process_receipt(receipt, errors=DISCARD):
                events.append({"name": name, "address": event["address"],
                               "args": {k: str(v) if isinstance(v, int) and not isinstance(v, bool) else v
                                        for k, v in event["args"].items()}})
        return {"pending": False, "status": receipt["status"], "block": receipt["blockNumber"],
                "contract_address": receipt["contractAddress"], "gas_used": receipt["gasUsed"],
                "hash": tx_hash, "events": events}
