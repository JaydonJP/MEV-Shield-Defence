"""Package the compiled vault for wallet deployments (run after forge build)."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
artifact = json.loads((root / "contracts/out/ShieldVault.sol/ShieldVault.json").read_text())
target = root / "mevshield/web/static/shield-vault.json"
target.write_text(json.dumps({"abi": artifact["abi"], "bytecode": artifact["bytecode"]["object"],
                              "runtime": artifact["deployedBytecode"]["object"]}, indent=2) + "\n")
print(f"Exported {target.name}")
