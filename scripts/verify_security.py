"""Run reproducible stateful verification and two guard-removal mutation checks.

Usage: python scripts/verify_security.py [--seed 0x5eed] [--skip-mutations]
Temporary contract variants are retained under data/verification-work-* for inspection.
Only those copies are mutated. Production Solidity files are never edited.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mevshield import config
from mevshield.verification import contract_fingerprint


def run(forge, cwd, args):
    command = [forge, "test", "--fuzz-seed", args.seed, "--summary"]
    if cwd != config.CONTRACTS_DIR:
        command += ["--match-contract", args.contract]
    started = time.time()
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return {"command": command, "exit_code": -1, "seconds": 240,
                "output": "", "diagnostics": "Verification command timed out"}
    return {"command": command, "exit_code": result.returncode, "seconds": round(time.time() - started, 2),
            "output": result.stdout[-14000:], "diagnostics": result.stderr[-2000:]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default="0x5eed")
    parser.add_argument("--skip-mutations", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", args.seed):
        parser.error("seed must be a hexadecimal uint256")
    forge = config.find_foundry_bin("forge")
    report = {"schema": 1, "created_at": time.time(), "seed": args.seed,
              "source_fingerprint": contract_fingerprint(),
              "scope": "local contract tests; not a proof or production audit", "mutations": []}
    print("Running contract examples, fuzz properties and stateful invariants…", flush=True)
    report["baseline"] = run(forge, config.CONTRACTS_DIR, args)
    if report["baseline"]["exit_code"] == 0 and not args.skip_mutations:
        workspace = (config.DATA_DIR / ("verification-work-" + uuid.uuid4().hex)).resolve()
        if workspace.parent != config.DATA_DIR.resolve():
            raise RuntimeError("Verification workspace escaped data directory")
        workspace.mkdir()
        mutations = [
            ("vault-cooldown", "src/ShieldVault.sol",
             'require(block.timestamp >= unlockAt[msg.sender], "Vault: cooldown active");',
             "VaultInvariantTest", "test_handlerExercisesSuccessfulAndRejectedActions"),
            ("global-borrow-budget", "src/FirewallLending.sol",
             'require(borrowedAt[block.number] + amount <= perBlock, "Firewall: block budget");',
             "LendingInvariantTest", "test_multiActorBudgetRejectsSecondBorrow"),
        ]
        for name, source, guard, contract, test in mutations:
            print(f"Checking deliberately removed protection: {name}…", flush=True)
            dest = workspace / name
            shutil.copytree(config.CONTRACTS_DIR, dest,
                            ignore=shutil.ignore_patterns("out", "cache", "broadcast", ".git"))
            path = dest / source
            text = path.read_text(encoding="utf-8")
            if text.count(guard) != 1:
                raise RuntimeError(f"Mutation target changed: {source}")
            path.write_text(text.replace(guard, "// Deliberately removed for mutation verification."), encoding="utf-8")
            args.contract = contract
            result = run(forge, dest, args)
            # Compilation failure is not a killed mutant: require the specific regression to fail.
            result.update(name=name, killed=result["exit_code"] != 0 and
                          bool(re.search(r"\[FAIL[^\n]*" + re.escape(test), result["output"])))
            report["mutations"].append(result)
    good = report["baseline"]["exit_code"] == 0
    good = good and len(report["mutations"]) == 2 and all(m["killed"] for m in report["mutations"])
    report["status"] = "passed" if good else "partial" if args.skip_mutations else "failed"
    if report["source_fingerprint"] != contract_fingerprint():
        report["status"] = "stale"
        good = False
    destination = config.DATA_DIR / "verification.json"
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Verification {report['status']}: {destination}", flush=True)
    return 0 if good or (args.skip_mutations and report["baseline"]["exit_code"] == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
