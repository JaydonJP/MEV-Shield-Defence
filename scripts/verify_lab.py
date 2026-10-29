"""Exercise the running local lab through its HTTP API. Never sends public-network transactions.

Start mevshield start --build first. This resets the disposable lab between each scenario.
"""
import requests

ROOT = "http://127.0.0.1:8000"


def post(path, **params):
    response = requests.post(ROOT + "/api/" + path, params=params, timeout=90)
    response.raise_for_status()
    return response.json()


def run():
    checks = [
        ("public sandwich", "sandwich", {"amount": 5000, "protect": False}, lambda r: r["harm_usd"] > 1),
        ("adaptive slippage", "sandwich", {"amount": 5000, "protect": True, "defense": "adaptive-slippage"}, lambda r: r["harm_usd"] < 1),
        ("private relay", "sandwich", {"amount": 5000, "protect": True, "defense": "private-relay"}, lambda r: r["harm_usd"] < 1),
        ("commit reveal", "sandwich", {"amount": 5000, "protect": True, "defense": "commit-reveal"}, lambda r: r["harm_usd"] < 1),
        ("trade splitting", "sandwich", {"amount": 5000, "protect": True, "defense": "split"}, lambda r: "harm_usd" in r),
        ("vulnerable oracle", "oracle", {"target": "vulnerable", "guard": False}, lambda r: r["drained"] > 0),
        ("guardian oracle", "oracle", {"target": "vulnerable", "guard": True}, lambda r: r["reverted"] and r["drained"] == 0),
        ("hardened oracle", "oracle", {"target": "safe", "guard": False}, lambda r: r["reverted"] and r["drained"] == 0),
        ("benign arbitrage", "arbitrage", {}, lambda r: r.get("benign") is True),
        ("vulnerable governance", "governance", {"target": "vulnerable"}, lambda r: not r["reverted"]),
        ("snapshot governance", "governance", {"target": "safe"}, lambda r: r["reverted"]),
    ]
    try:
        for name, path, params, check in checks:
            post("reset")
            result = post(path, **params)
            assert check(result), (name, result)
            print(f"PASS {name}: {result}", flush=True)
    finally:
        post("reset")


if __name__ == "__main__":
    run()
