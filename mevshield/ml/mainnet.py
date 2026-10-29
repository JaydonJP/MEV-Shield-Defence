"""Read-only Ethereum V2 swap collection with explicit counterfactual labels.

Inputs are real, mined swaps. Labels answer a declared economic what-if, NOT
whether a transaction was actually sandwiched. No API keys or chain writes.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import time

from eth_abi import decode
from eth_abi.exceptions import DecodingError
import pandas as pd
import requests
from web3 import Web3

from .. import config
from ..defense import amm_math

DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"
DATA = config.DATA_DIR / "mainnet"
VERSION = "v2-counterfactual-2"
POOLS = {
    "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc": {
        "name": "Uniswap V2 USDC/WETH",
        "stable": "USDC",
        "decimals": 6,
        "stable0": True,
        "router": "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
    },
    "0xa478c2975ab1ea89e8196811f51a7b7ade33eb11": {
        "name": "Uniswap V2 DAI/WETH",
        "stable": "DAI",
        "decimals": 18,
        "stable0": True,
        "router": "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
    },
    "0x0d4a11d5eeaac28ec3f61d100daf4d40471f1852": {
        "name": "Uniswap V2 WETH/USDT",
        "stable": "USDT",
        "decimals": 6,
        "stable0": False,
        "router": "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
    },
    "0x397ff1542f962076d0bfe58ea045ffa2d347aca0": {
        "name": "SushiSwap USDC/WETH",
        "stable": "USDC",
        "decimals": 6,
        "stable0": True,
        "router": "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f",
    },
}
TOKENS = {
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "DAI": "0x6b175474e89094c44da98b954eedeac495271d0f",
    "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
}
SYNC = Web3.keccak(text="Sync(uint112,uint112)").to_0x_hex()
SWAP = Web3.keccak(
    text="Swap(address,uint256,uint256,uint256,uint256,address)"
).to_0x_hex()


class ReadOnlyRPC:
    METHODS = {
        "eth_blockNumber",
        "eth_chainId",
        "eth_getLogs",
        "eth_getTransactionByHash",
        "eth_getBlockByNumber",
        "eth_call",
        "eth_getCode",
        "eth_gasPrice",
    }

    def __init__(self, url=DEFAULT_RPC, cache=None):
        self.url, self.cache = url, Path(cache) if cache else None
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "MEVShield-Research/2 read-only"
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)

    def batch(self, calls):
        if not calls:
            return []
        if any(m not in self.METHODS for m, _ in calls):
            raise ValueError("Only allowlisted read-only RPC methods are supported")
        output, missing = [None] * len(calls), []
        for i, (method, params) in enumerate(calls):
            key = hashlib.sha256(
                json.dumps([method, params], sort_keys=True).encode()
            ).hexdigest()
            path = self.cache / (key + ".json") if self.cache else None
            if path and path.exists():
                output[i] = json.loads(path.read_text())
            else:
                missing.append((i, method, params, path))
        for offset in range(0, len(missing), 15):
            chunk = missing[offset : offset + 15]
            payload = [
                {"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                for i, m, p, _ in chunk
            ]
            for attempt in range(4):
                try:
                    response = self.session.post(
                        self.url, json=payload, timeout=(8, 45)
                    )
                    response.raise_for_status()
                    body = response.json()
                    if not isinstance(body, list):
                        raise ValueError("RPC batch returned an invalid response")
                    indexed = {r["id"]: r for r in body}
                    for i, _, _, _ in chunk:
                        if (
                            i not in indexed
                            or "error" in indexed[i]
                            or indexed[i].get("result") is None
                        ):
                            message = (
                                indexed.get(i, {})
                                .get("error", {})
                                .get("message", "Missing RPC result")
                            )
                            raise ValueError(message)
                    break
                except (requests.RequestException, ValueError):
                    if attempt == 3:
                        raise
                    time.sleep(min(4, 0.4 * 2**attempt))
            for i, _, _, path in chunk:
                output[i] = indexed[i]["result"]
                if path:
                    path.write_text(json.dumps(output[i]), encoding="utf-8")
            time.sleep(0.12)  # bounded, polite public-endpoint usage
        return output

    def call(self, method, params):
        return self.batch([(method, params)])[0]


def decode_direct_swap(tx, pool):
    """Only standard exact-input, two-token router calls; never invent a minOut."""
    if (tx.get("to") or "").lower() != pool["router"]:
        raise ValueError("unsupported_router")
    calldata = tx.get("input", "0x")
    selector = calldata[:10].lower()
    raw = bytes.fromhex(calldata[10:])
    if selector in ("0x38ed1739", "0x18cbafe5"):
        amount, minimum, path, _, _ = decode(
            ["uint256", "uint256", "address[]", "address", "uint256"], raw
        )
    elif selector == "0x7ff36ab5":
        minimum, path, _, _ = decode(
            ["uint256", "address[]", "address", "uint256"], raw
        )
        amount = int(tx["value"], 16)
    else:
        raise ValueError("unsupported_selector")
    path = [p.lower() for p in path]
    if len(path) != 2 or set(path) != {TOKENS[pool["stable"]], TOKENS["WETH"]}:
        raise ValueError("unsupported_path")
    return amount, minimum, path[0] == TOKENS[pool["stable"]]


def counterfactual(
    trade_usd,
    liquidity_usd,
    slippage,
    fee_bps,
    gas_gwei,
    eth_price,
    capital_usd=5_000_000,
    attacker_gas=config.SANDWICH_GAS,
):
    """Unit-normalized V2 economics; applies equally to either swap direction."""
    ri = int(liquidity_usd * 10**18)
    # Normalize output into USD units too: the CP rule is invariant under unit changes.
    ro = ri
    amount = int(trade_usd * 10**18)
    benign = amm_math.amount_out(amount, ri, ro, fee_bps)
    minimum = int(benign * (1 - slippage))
    res = amm_math.optimal_sandwich(
        ri, ro, amount, minimum, fee_bps, capital=int(capital_usd * 10**18)
    )
    cost = attacker_gas * gas_gwei * 1e-9 * eth_price
    profit = res.profit / 10**18
    attack = profit > cost
    return {
        "harm_usd": max(0, res.harm / 10**18) if attack else 0.0,
        "attack": int(attack),
        "gross_profit_usd": profit,
        "attacker_gas_usd": cost,
        "benign_out_usd": benign / 10**18,
    }


def swap_rows(logs, transactions, headers, attacker_gas=config.SANDWICH_GAS):
    rows, rejected, syncs = [], Counter(), {}
    counts = Counter(
        (l["transactionHash"], l["address"].lower())
        for l in logs
        if l["topics"][0] == SWAP
    )
    for log in sorted(
        logs, key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16))
    ):
        address, tx_hash = log["address"].lower(), log["transactionHash"]
        key = (tx_hash, address)
        values = [
            int(log["data"][i : i + 64], 16) for i in range(2, len(log["data"]), 64)
        ]
        if log["topics"][0] == SYNC:
            syncs[key] = values
            continue
        if log["topics"][0] != SWAP:
            continue
        try:
            if counts[key] != 1 or key not in syncs:
                raise ValueError("complex_pool_transaction")
            pool, tx = POOLS[address], transactions[tx_hash]
            number = int(log["blockNumber"], 16)
            if (
                headers[number]["hash"] != log["blockHash"]
                or tx["blockHash"] != log["blockHash"]
            ):
                raise ValueError("inconsistent_block_provenance")
            in0, in1, out0, out1 = values
            if not (
                (in0 > 0 and in1 == 0 and out1 > 0 and out0 == 0)
                or (in1 > 0 and in0 == 0 and out0 > 0 and out1 == 0)
            ):
                raise ValueError("unsupported_swap_shape")
            r0, r1 = syncs[key][0] - in0 + out0, syncs[key][1] - in1 + out1
            is0 = in0 > 0
            stable_in = is0 == pool["stable0"]
            amount = in0 if is0 else in1
            r_stable, r_eth = (r0, r1) if pool["stable0"] else (r1, r0)
            eth_price = r_stable / 10 ** pool["decimals"] / (r_eth / 10**18)
            ri, ro = (r0, r1) if is0 else (r1, r0)
            benign = amm_math.amount_out(amount, ri, ro, 30)
            actual_out = out1 if is0 else out0
            if abs(benign - actual_out) > 2:
                raise ValueError("nonstandard_fee_or_token")
            try:
                decoded_amount, minimum, decoded_direction = decode_direct_swap(
                    tx, pool
                )
                if decoded_amount != amount or decoded_direction != stable_in:
                    raise ValueError("input_mismatch")
                slippage = 1 - minimum / benign
                slippage_source = "decoded_router_minimum"
            except (ValueError, DecodingError):
                # One predeclared policy per observed swap; never represent this as actual calldata.
                index = (
                    int(
                        hashlib.sha256((tx_hash + address).encode()).hexdigest()[:8], 16
                    )
                    % 5
                )
                slippage = (0.001, 0.005, 0.01, 0.02, 0.05)[index]
                slippage_source = "declared_policy_scenario"
            if not 1e-6 <= slippage <= 0.2:
                raise ValueError("outside_slippage_scope")
            trade = (
                amount
                / (10 ** pool["decimals"] if stable_in else 10**18)
                * (1 if stable_in else eth_price)
            )
            liquidity = (
                ri
                / (10 ** pool["decimals"] if stable_in else 10**18)
                * (1 if stable_in else eth_price)
            )
            gas = int(tx["gasPrice"], 16) / 1e9
            if not all(
                math.isfinite(x) and x > 0 for x in (trade, liquidity, eth_price, gas)
            ):
                raise ValueError("invalid_economics")
            label = counterfactual(
                trade,
                liquidity,
                slippage,
                30,
                gas,
                eth_price,
                attacker_gas=attacker_gas,
            )
            rows.append(
                {
                    "row_id": f"{tx_hash}:{address}",
                    "block_number": number,
                    "block_hash": log["blockHash"],
                    "timestamp": int(headers[number]["timestamp"], 16),
                    "transaction_hash": tx_hash,
                    "pool": address,
                    "pool_name": pool["name"],
                    "stable_in": stable_in,
                    "trade_usd": trade,
                    "liquidity_usd": liquidity,
                    "slippage": slippage,
                    "fee_bps": 30,
                    "gas_price_gwei": gas,
                    "eth_price_usd": eth_price,
                    "slippage_source": slippage_source,
                    "source": "ethereum-mainnet",
                    "label_kind": "counterfactual_v2_economics",
                    **label,
                }
            )
        except (ValueError, KeyError, OverflowError, ZeroDivisionError) as exc:
            rejected[str(exc)[:100]] += 1
    return rows, rejected


def collect(
    blocks=30000,
    end_block=None,
    rpc=DEFAULT_RPC,
    directory=DATA,
    log=print,
    chunk_size=100,
    transaction_rpc=DEFAULT_RPC,
):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    client = ReadOnlyRPC(rpc, directory / "raw")
    if int(client.call("eth_chainId", []), 16) != 1:
        raise ValueError("Dataset collection requires Ethereum mainnet chain ID 1")
    end = (
        int(end_block)
        if end_block
        else int(ReadOnlyRPC(rpc).call("eth_blockNumber", []), 16) - 64
    )
    start = end - blocks + 1
    if not 100 <= blocks <= 100000 or start <= 0:
        raise ValueError("Collect between 100 and 100000 blocks")
    events = []
    for first in range(start, end + 1, chunk_size):
        last = min(end, first + chunk_size - 1)
        logs = client.call(
            "eth_getLogs",
            [
                {
                    "address": list(POOLS),
                    "fromBlock": hex(first),
                    "toBlock": hex(last),
                    "topics": [[SYNC, SWAP]],
                }
            ],
        )
        events.extend(logs)
        if (first - start) % 1000 == 0:
            log(f"blocks through {last:,}: {len(events):,} pool events")
    hashes = sorted({l["transactionHash"] for l in events if l["topics"][0] == SWAP})
    log(f"Fetching {len(hashes):,} actual transactions (resumable cache)…")
    details = ReadOnlyRPC(transaction_rpc, directory / "raw")
    txs = dict(
        zip(hashes, details.batch([("eth_getTransactionByHash", [h]) for h in hashes]))
    )
    numbers = sorted({int(l["blockNumber"], 16) for l in events})
    headers = dict(
        zip(
            numbers,
            details.batch([("eth_getBlockByNumber", [hex(n), False]) for n in numbers]),
        )
    )
    rows, rejected = swap_rows(events, txs, headers, config.SANDWICH_GAS)
    if not rows:
        raise ValueError(
            "No supported swap rows; inspect source coverage before training"
        )
    frame = (
        pd.DataFrame(rows)
        .drop_duplicates("row_id")
        .sort_values(["block_number", "row_id"])
    )
    output = directory / "swaps.csv"
    frame.to_csv(output, index=False, lineterminator="\n")
    manifest = {
        "schema": VERSION,
        "collected_at": time.time(),
        "chain_id": 1,
        "start_block": start,
        "end_block": end,
        "pools": POOLS,
        "rows": len(frame),
        "source": "public Ethereum JSON-RPC",
        "dataset_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "rejected": dict(rejected),
        "slippage_sources": frame.slippage_source.value_counts().to_dict(),
        "label_kind": "mathematical counterfactual, not observed sandwich occurrence",
        "assumptions": {
            "capital_usd": 5_000_000,
            "attacker_gas_units": config.SANDWICH_GAS,
            "stablecoin_usd": 1,
            "pool_model": "V2 constant product, 30 bps",
            "policy_slippage_if_undecodable": [0.001, 0.005, 0.01, 0.02, 0.05],
        },
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    log(f"Saved {len(frame):,} auditable real swap rows to {output}")
    return manifest
