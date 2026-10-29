"""Public-network adapter tests, including actual EVM execution on an ephemeral LOCAL Anvil.

The test node uses Sepolia's chain ID to exercise transaction encoding. These tests never
send transactions to the public network and do not use a personal wallet or test ETH.
"""
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from hexbytes import HexBytes
from web3 import Web3

from mevshield import config
from mevshield.chain.public import CHAIN_ID, PublicNetwork, vault_artifact


def test_wrong_network_fails_closed():
    network = PublicNetwork("http://unused.invalid")
    network.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=1))
    with pytest.raises(ValueError, match="network mismatch"):
        network.verify()
    assert network.status()["connected"] is False
    with pytest.raises(ValueError, match="network mismatch"):
        network.prepare("deploy", "0x" + "12" * 20, None, "0")


def test_live_status_is_cached_and_does_not_expose_rpc_credentials():
    calls = []
    def get_block(identifier):
        calls.append(identifier)
        return {"number": 200 if identifier == "latest" else identifier, "hash": HexBytes("0x" + "12" * 32),
                "timestamp": 1000, "transactions": [], "gasUsed": 1, "gasLimit": 100, "baseFeePerGas": 10**9}
    network = PublicNetwork("http://secret.invalid/my-api-key")
    network.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=CHAIN_ID, get_block=get_block))
    state = network.status()
    assert state["connected"] and len(state["blocks"]) == 5
    assert "my-api-key" not in str(state)
    assert network.status() == state
    assert len(calls) == 5


@pytest.fixture(scope="module")
def evm_network():
    try:
        anvil = config.find_foundry_bin("anvil")
    except FileNotFoundError:
        pytest.skip("Foundry is required for the local EVM adapter tests")
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    process = subprocess.Popen([anvil, "--port", str(port), "--chain-id", str(CHAIN_ID), "--silent"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    try:
        network = PublicNetwork(f"http://127.0.0.1:{port}")
        for _ in range(80):
            if network.w3.is_connected():
                break
            time.sleep(0.1)
        else:
            pytest.fail("Ephemeral Anvil did not start")
        yield network
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def vault(evm_network):
    network = evm_network
    account = network.w3.eth.accounts[0]
    prepared = network.prepare("deploy", account, None, "0")
    # Only this disposable local node exposes unlocked accounts. Production uses a browser wallet.
    receipt = network.w3.eth.wait_for_transaction_receipt(network.w3.eth.send_transaction(prepared))
    assert receipt["status"] == 1
    return network, account, receipt["contractAddress"], receipt["transactionHash"].to_0x_hex()


def test_wallet_deployment_bytecode_and_real_receipt(vault):
    network, account, address, tx_hash = vault
    assert vault_artifact()["bytecode"].startswith("0x")
    state = network.vault_state(address, account)
    assert state["owner"] == account and state["balance_wei"] == "0"
    receipt = network.receipt(tx_hash)
    assert not receipt["pending"] and receipt["status"] == 1
    assert receipt["contract_address"] == address
    with pytest.raises(ValueError, match="matching ShieldVault"):
        network.vault_state(network.w3.eth.accounts[1], account)


def test_preflight_blocks_cooldown_then_withdraws_and_decodes_events(vault):
    network, account, address, _ = vault
    prepared = network.prepare("deposit", account, address, str(2 * 10**15))
    assert prepared["chainId"] == hex(CHAIN_ID)
    tx_hash = network.w3.eth.send_transaction(prepared).to_0x_hex()
    network.w3.eth.wait_for_transaction_receipt(tx_hash)
    receipt = network.receipt(tx_hash)
    assert receipt["events"][0]["name"] == "Deposited"
    assert receipt["events"][0]["args"]["amount"] == str(2 * 10**15)
    with pytest.raises(ValueError, match="cooldown active"):
        network.prepare("withdraw", account, address, str(10**15))
    network.w3.provider.make_request("evm_increaseTime", [61])
    network.w3.provider.make_request("evm_mine", [])
    withdrawal = network.prepare("withdraw", account, address, str(10**15))
    network.w3.eth.wait_for_transaction_receipt(network.w3.eth.send_transaction(withdrawal))
    assert network.vault_state(address, account)["balance_wei"] == str(10**15)


def test_preflight_blocks_pause_unauthorized_owner_and_excess_amounts(vault):
    network, account, address, _ = vault
    with pytest.raises(ValueError, match="owner only"):
        network.prepare("pause", network.w3.eth.accounts[1], address, "0")
    tx = network.prepare("pause", account, address, "0")
    network.w3.eth.wait_for_transaction_receipt(network.w3.eth.send_transaction(tx))
    assert network.vault_state(address, account)["paused"] is True
    with pytest.raises(ValueError, match="paused"):
        network.prepare("deposit", account, address, "100")
    for amount in ("0", "-1", str(10**18)):
        with pytest.raises(ValueError, match="amount"):
            network.prepare("deposit", account, address, amount)
    with pytest.raises(ValueError, match="hash"):
        network.receipt("not-a-hash")
