"""Deploy and seed the whole devnet world, then write data/deployments.json."""
from __future__ import annotations

import json
import subprocess
import time

from web3 import Web3

from .. import config
from .connection import Chain, account, artifact

MAX = 2**256 - 1


def forge_build() -> None:
    forge = config.find_foundry_bin("forge")
    subprocess.run([forge, "build"], cwd=config.CONTRACTS_DIR, check=True, capture_output=True)


def _deploy(chain: Chain, acct, name: str, *args) -> str:
    art = artifact(name)
    factory = chain.w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])
    tx = chain.build_tx(acct, factory.constructor(*args), gas=6_000_000)
    rcpt = chain.w3.eth.wait_for_transaction_receipt(chain.send_raw(chain.sign(acct, tx)))
    if rcpt["status"] != 1:
        raise RuntimeError(f"deploy of {name} failed")
    return rcpt["contractAddress"]


def deploy_all(chain: Chain, log=print) -> dict:
    """Deploys with automine on (fast setup), then switches the devnet back to mempool mode."""
    chain.set_automine(True)
    try:
        return _deploy_all(chain, log)
    finally:
        chain.set_automine(False)


def _deploy_all(chain: Chain, log) -> dict:
    admin = account("admin")
    a: dict[str, str] = {}
    a["WETH"] = _deploy(chain, admin, "Token", "Mock Wrapped Ether", "WETH")
    a["USDC"] = _deploy(chain, admin, "Token", "Mock USD Coin", "USDC")
    a["GOV"] = _deploy(chain, admin, "Token", "Shield DAO Governance", "GOV")
    a["ammA"] = _deploy(chain, admin, "SimpleAMM", a["WETH"], a["USDC"], config.POOL_FEE_BPS)
    a["ammB"] = _deploy(chain, admin, "SimpleAMM", a["WETH"], a["USDC"], config.POOL_FEE_BPS)
    a["lender"] = _deploy(chain, admin, "FlashLender")
    chain._deployments = {"contracts": a}
    log("  tokens, AMMs, flash lender deployed")

    weth, usdc, gov = chain.c("WETH"), chain.c("USDC"), chain.c("GOV")
    mint = lambda tok, to, amt: chain.transact(admin, tok.functions.mint(Web3.to_checksum_address(to), amt))

    for key, (w, u) in (("ammA", config.POOL_A), ("ammB", config.POOL_B)):
        mint(weth, admin.address, w)
        mint(usdc, admin.address, u)
        chain.transact(admin, weth.functions.approve(a[key], MAX))
        chain.transact(admin, usdc.functions.approve(a[key], MAX))
        chain.transact(admin, chain.c(key).functions.addLiquidity(w, u))
    mint(usdc, a["lender"], config.FLASH_LIQUIDITY["USDC"])
    mint(weth, a["lender"], config.FLASH_LIQUIDITY["WETH"])
    mint(gov, a["lender"], config.FLASH_LIQUIDITY["GOV"])
    log("  pools seeded: A = 100 WETH / 200k USDC, B = 100 WETH / 206k USDC")

    a["oracle"] = _deploy(chain, admin, "TWAPOracle", a["ammA"], config.TWAP_MIN_PERIOD)
    guardian = account("guardian").address
    a["vulnerable"] = _deploy(chain, admin, "VulnerableLending", a["WETH"], a["USDC"], a["ammA"], guardian)
    a["safe"] = _deploy(chain, admin, "SafeLending", a["WETH"], a["USDC"], a["ammA"], guardian, a["oracle"],
                        config.SAFE_MAX_DEVIATION_BPS)
    mint(usdc, a["vulnerable"], config.LENDING_LIQUIDITY)
    mint(usdc, a["safe"], config.LENDING_LIQUIDITY)
    a["router"] = _deploy(chain, admin, "ProtectedRouter")
    a["commitReveal"] = _deploy(chain, admin, "CommitRevealSwap")
    a["vulnerableDAO"] = _deploy(chain, admin, "VulnerableDAO", a["GOV"], guardian)
    a["snapshotDAO"] = _deploy(chain, admin, "SnapshotDAO", a["GOV"], guardian)
    mint(usdc, a["vulnerableDAO"], config.DAO_TREASURY)
    mint(usdc, a["snapshotDAO"], config.DAO_TREASURY)
    log("  lending markets, router, commit-reveal, DAOs deployed")

    # users
    for role in ("alice", "bob", "carol"):
        acct = account(role)
        mint(usdc, acct.address, config.USER_FUNDS["USDC"])
        mint(weth, acct.address, config.USER_FUNDS["WETH"])
        for spender in ("router", "commitReveal", "vulnerable", "safe", "ammA", "ammB"):
            chain.transact(acct, usdc.functions.approve(a[spender], MAX))
            chain.transact(acct, weth.functions.approve(a[spender], MAX))
    mint(gov, account("bob").address, config.GOV_SUPPLY_HOLDERS)

    # test-harness contracts (owned by their attacker accounts)
    a["flashArb"] = _deploy(chain, account("arbitrageur"), "FlashArbitrage", a["lender"])
    a["sandwich"] = _deploy(chain, account("sandwich_bot"), "SandwichAttacker")
    a["oracleAttacker"] = _deploy(chain, account("oracle_attacker"), "OracleAttacker", a["lender"])
    a["govAttacker"] = _deploy(chain, account("gov_attacker"), "GovernanceAttacker", a["lender"])
    mint(usdc, a["sandwich"], config.BOT_INVENTORY["USDC"])
    mint(weth, a["sandwich"], config.BOT_INVENTORY["WETH"])
    mint(gov, a["govAttacker"], 1_000 * config.E)  # covers the flash fee
    log("  test-harness contracts deployed (sandwich bot, oracle attacker, arbitrage, governance attacker)")

    # let time pass so the TWAP window is non-trivial
    chain.rpc_call("evm_increaseTime", [600])
    chain.mine()

    deployments = {"contracts": a, "deployed_at": int(time.time()), "chain_id": config.CHAIN_ID}
    chain._deployments = deployments
    deployments["snapshot"] = chain.snapshot()
    config.DEPLOYMENTS_FILE.write_text(json.dumps(deployments, indent=2))
    return deployments


def reset(chain: Chain) -> None:
    """Return the devnet to the freshly-deployed state (drops pending txs, reverts to the deploy snapshot)."""
    d = chain.deployments
    chain.drop_all_pending()
    if not chain.revert(d["snapshot"]):
        raise RuntimeError("snapshot revert failed — run `mevshield down && mevshield up`")
    chain.rpc_call("evm_increaseTime", [60])
    chain.mine()
    d["snapshot"] = chain.snapshot()  # anvil consumes snapshots on revert
    config.DEPLOYMENTS_FILE.write_text(json.dumps(d, indent=2))
