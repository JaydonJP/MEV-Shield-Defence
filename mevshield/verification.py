"""Bind saved verification evidence to the exact Solidity sources and configuration."""
import hashlib

from . import config


def contract_fingerprint():
    digest = hashlib.sha256()
    paths = [config.CONTRACTS_DIR / "foundry.toml"]
    for folder in ("src", "test"):
        paths.extend(sorted((config.CONTRACTS_DIR / folder).rglob("*.sol")))
    for path in paths:
        digest.update(path.relative_to(config.CONTRACTS_DIR).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
