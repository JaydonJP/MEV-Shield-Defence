"""Paths, ports, accounts and world parameters shared by every MEV Shield component.

Everything here describes an isolated local devnet. The private keys are Anvil's well-known
public test keys (default mnemonic) and hold no value anywhere.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = ROOT / "contracts"
OUT_DIR = CONTRACTS_DIR / "out"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

DEPLOYMENTS_FILE = DATA_DIR / "deployments.json"
DEVNET_FILE = DATA_DIR / "devnet.json"  # PIDs of the anvil processes *we* started
DB_FILE = DATA_DIR / "mevshield.db"
MODEL_FILE = DATA_DIR / "model.pkl"
RESULTS_CSV = DATA_DIR / "results.csv"

MAIN_PORT = int(os.environ.get("MEVSHIELD_MAIN_PORT", 8545))
SHADOW_PORT = int(os.environ.get("MEVSHIELD_SHADOW_PORT", 8546))
MAIN_RPC = f"http://127.0.0.1:{MAIN_PORT}"
SHADOW_RPC = f"http://127.0.0.1:{SHADOW_PORT}"
CHAIN_ID = 31337

E = 10**18
GWEI = 10**9


def find_foundry_bin(name: str) -> str:
    """Locate a Foundry binary (anvil/forge) on PATH or in ~/.foundry/bin."""
    portable = ROOT / ".tools" / "foundry" / (f"{name}.exe" if os.name == "nt" else name)
    if portable.exists():
        return str(portable)
    found = shutil.which(name)
    if found:
        return found
    for candidate in (Path.home() / ".foundry" / "bin" / f"{name}.exe", Path.home() / ".foundry" / "bin" / name):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(f"{name} not found — install Foundry (see README)")


# Anvil default-mnemonic test accounts (public, devnet only).
ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",
    "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",
    "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97",
    "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6",
]

# role -> account index
ROLES = {
    "admin": 0,  # deployer, protocol owner, liquidity provider
    "alice": 1,  # protected user
    "bob": 2,  # benign traffic
    "sandwich_bot": 3,  # TEST HARNESS attacker (T1)
    "oracle_attacker": 4,  # TEST HARNESS attacker (T2)
    "arbitrageur": 5,  # benign flash-loan arbitrage (T3)
    "guardian": 6,  # MEV Shield guardian / responder
    "relay": 7,  # private relay operator
    "carol": 8,  # benign traffic / borrower
    "gov_attacker": 9,  # TEST HARNESS attacker (T4)
}

# ---------------------------------------------------------------- world state
POOL_A = (100 * E, 200_000 * E)  # WETH, USDC — the pool the lending markets read
POOL_B = (100 * E, 206_000 * E)  # dislocated second venue (arbitrage opportunity)
POOL_FEE_BPS = 5  # 0.05 % tier
FLASH_LIQUIDITY = {"USDC": 5_000_000 * E, "WETH": 5_000 * E, "GOV": 600_000 * E}
LENDING_LIQUIDITY = 1_000_000 * E
DAO_TREASURY = 500_000 * E
GOV_SUPPLY_HOLDERS = 400_000 * E
SAFE_MAX_DEVIATION_BPS = 500
TWAP_MIN_PERIOD = 60
USER_FUNDS = {"USDC": 1_000_000 * E, "WETH": 500 * E}
BOT_INVENTORY = {"USDC": 2_000_000 * E, "WETH": 1_000 * E}

# ------------------------------------------------------------ user-side model
DEFAULT_SLIPPAGE = 0.02  # 2 %
DEFAULT_TIP = 1 * GWEI
SWAP_GAS = 250_000  # router swap (measured 219k–331k incl. token checkpoints)
SANDWICH_GAS = 420_000  # frontrun + backrun (measured)
COMMIT_GAS = 70_000

# Defense selector J(d) = Harm + λ1·Gas + λ2·Latency + λ3·RevertRisk   (all in USDC)
LAMBDA_GAS = 1.0
LAMBDA_LATENCY_PER_BLOCK = 0.50
RETRY_COST = 5.0
RELAY_FEE_USD = 1.0
SIGMA_PER_BLOCK = 0.001  # benign per-block price volatility (one-sided adverse moves cause reverts)

# ---------------------------------------------------------- guardian scoring
PRICE_DEVIATION_ALERT = 0.10
