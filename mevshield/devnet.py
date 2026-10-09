"""Start/stop the main Anvil devnet and the shadow fork.

Only processes started here are ever stopped: their PIDs are recorded in data/devnet.json and
checked (still alive, still an anvil binary) before being terminated. Any other Anvil instance
on the machine is left alone.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import requests

from . import config


def _rpc_alive(url: str) -> bool:
    try:
        r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=1)
        return r.ok
    except requests.RequestException:
        return False


def _load_state() -> dict:
    if config.DEVNET_FILE.exists():
        return json.loads(config.DEVNET_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    config.DEVNET_FILE.write_text(json.dumps(state, indent=2))


def _spawn(args: list[str], log_name: str) -> int:
    log = open(config.DATA_DIR / log_name, "w")
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs).pid


def _wait_rpc(url: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _rpc_alive(url):
            return
        time.sleep(0.2)
    raise RuntimeError(f"anvil at {url} did not come up (see data/*.log)")


def _is_our_anvil(pid: int) -> bool:
    """True only if `pid` is alive and is an anvil binary."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True
        ).stdout
        return "anvil" in out.lower()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return "anvil" in open(f"/proc/{pid}/cmdline").read()
    except OSError:
        return True


def _terminate(pid: int) -> None:
    if not _is_our_anvil(pid):
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        os.kill(pid, 15)


def start_main() -> int:
    if _rpc_alive(config.MAIN_RPC):
        raise RuntimeError(f"something is already listening on {config.MAIN_RPC}; run `mevshield down` first")
    anvil = config.find_foundry_bin("anvil")
    pid = _spawn(
        [anvil, "--port", str(config.MAIN_PORT), "--no-mining", "--order", "fees",
         "--chain-id", str(config.CHAIN_ID), "--accounts", "10", "--balance", "10000"],
        "anvil_main.log",
    )
    _wait_rpc(config.MAIN_RPC)
    state = _load_state()
    state["main_pid"] = pid
    _save_state(state)
    return pid


def start_shadow() -> int:
    """Shadow fork of the main devnet; FIFO ordering so simulated bundles execute in the given order."""
    if _rpc_alive(config.SHADOW_RPC):
        raise RuntimeError(f"something is already listening on {config.SHADOW_RPC}; run `mevshield down` first")
    anvil = config.find_foundry_bin("anvil")
    pid = _spawn(
        [anvil, "--port", str(config.SHADOW_PORT), "--fork-url", config.MAIN_RPC, "--order", "fifo",
         "--chain-id", str(config.CHAIN_ID), "--no-storage-caching"],
        "anvil_shadow.log",
    )
    _wait_rpc(config.SHADOW_RPC)
    state = _load_state()
    state["shadow_pid"] = pid
    _save_state(state)
    return pid


def stop() -> list[int]:
    """Stop the anvil processes recorded in data/devnet.json (and nothing else)."""
    state = _load_state()
    stopped = []
    for key in ("shadow_pid", "main_pid"):
        pid = state.get(key)
        if pid:
            _terminate(pid)
            stopped.append(pid)
    _save_state({})
    return stopped


def status() -> dict:
    state = _load_state()
    return {
        "main": {"pid": state.get("main_pid"), "rpc": config.MAIN_RPC, "alive": _rpc_alive(config.MAIN_RPC)},
        "shadow": {"pid": state.get("shadow_pid"), "rpc": config.SHADOW_RPC, "alive": _rpc_alive(config.SHADOW_RPC)},
    }
