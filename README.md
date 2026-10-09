# 🛡️ MEV Shield

**Predict and prevent flash-loan and MEV attacks *before* they execute** — demonstrated end-to-end on an
isolated local Ethereum devnet with a CLI command chain and a live web dashboard.

Every attack we care about passes through two places before it does damage: the **mempool** (where pending
transactions wait) and the **EVM** (where they execute). MEV Shield sits at both. It keeps a private
**shadow fork** of the chain and, for every transaction of interest, runs it there first —
*counterfactually* — to **predict** the outcome, then **prevents** the attack automatically: it re-shapes
the user's transaction so a sandwich is unprofitable, or has a Guardian pause the protocol in the same
block, ordered *before* an attack, so the attack reverts.

> The attacker code in `contracts/src/harness/` and `mevshield/harness/` is a **test harness only** — it
> exists to prove the shield works. Everything runs on a local devnet; nothing touches mainnet or real
> protocols.

See [PLAN.md](PLAN.md) for the full design and [docs/REPORT.md](docs/REPORT.md) for the write-up.

---

## Threats covered

| ID | Attack | Victim | Shielded by |
|----|--------|--------|-------------|
| **T1** | Sandwich (MEV) | a user | Risk Engine + Defense Selector (adaptive slippage / private relay / commit-reveal / split) |
| **T2** | Flash-loan oracle manipulation (bZx / Harvest) | a protocol | Guardian circuit-breaker (active) + TWAP & deviation guard (by design) |
| **T3** | Flash-loan arbitrage (benign baseline) | nobody | must **not** be flagged — false-positive test |
| **T4** | Flash-loan governance attack (Beanstalk) *(stretch)* | a DAO | snapshot voting + timelock (by design) |

---

## Setup (Windows, native)

Prerequisites: **Python 3.12** and **Foundry** (anvil + forge).

```bash
# 1. Foundry — if `anvil --version` fails, install it:
#    PowerShell:  irm https://foundry.paradigm.xyz | iex ; foundryup
#    (this project was built & tested against Foundry 1.8.5)

# 2. Python environment
py -V:Astral/CPython3.12.13 -m venv .venv      # or: python3.12 -m venv .venv
.venv\Scripts\pip install -e .

# 3. (optional) run the test suites
cd contracts && forge test && cd ..            # 22 Solidity tests
.venv\Scripts\python -m pytest tests -q        # 12 Python tests
```

All `mevshield …` commands below assume the venv is active (`.venv\Scripts\activate`), or prefix them with
`.venv\Scripts\mevshield`.

---

## Demo — the 10-minute command chain

```bash
mevshield up                                 # start devnet + shadow fork, deploy & seed everything

# Act 1 — flash loans are powerful (benign)
mevshield arb                                # attacker balance 0 → flash loan → profit; Guardian: BENIGN

# Act 2 — sandwich: predict & prevent (user side)
mevshield swap 500 --public                  # sandwich bot strikes; block shows [front, victim, back]; loss shown
mevshield predict 500                        # BEFORE sending: predicted loss, attacker profit, harm per defense
mevshield swap 500 --protect                 # selector picks a defense → attack unprofitable → loss ≈ 0
mevshield predict 5                           # tiny swap → "public is fine" (no over-protection)

# Act 3 — flash-loan oracle attack: predict & prevent (protocol side)
mevshield attack oracle --target vulnerable  # protocol drained, bad debt shown
mevshield reset
mevshield guardian start                     # (2nd terminal) live mempool monitor, auto-pause on CRITICAL
mevshield attack oracle --target vulnerable  # Guardian: CRITICAL → pause() ordered first → attack reverts
mevshield attack oracle --target safe        # TWAP lending: attack unprofitable by design

# Act 3b (stretch) — governance
mevshield attack governance --target vulnerable   # DAO treasury drained
mevshield attack governance --target safe         # snapshot voting defeats it

# Act 4 — evidence
mevshield train                              # train the ML fast predictor; prints MAE / R² / latency
mevshield benchmark                          # mixed workload: detection P/R/FPR, harm with vs without shield, regret
mevshield dashboard                          # live mempool w/ risk labels, block ordering, alerts, charts

mevshield demo                               # runs Acts 1–3 automatically with "press Enter" pauses (backup)
mevshield down                               # stop the devnet
```

---

## How it works

```
  user swap ─► Risk Engine ─► Defense Selector            Attacker bots      Guardian ─► Responder
                  │  counterfactual sims                  (test harness)     detect T1/T2  pause() w/ higher tip
                  └──────────────► Shadow fork (anvil --fork) ◄── replay pending tx
                                   snapshot / revert
  main devnet: anvil --no-mining --order fees   ·   all events ─► SQLite ─► Streamlit dashboard
```

- **Predict (user side)** — `risk.py` runs the swap alone, then a profit-maximising rational attacker
  (golden-section search over the front-run size, subject to the victim's `minAmountOut` and gas), on the
  shadow fork. Predicted harm = `out_benign − out_attacked`, cross-checked against the closed-form
  constant-product sandwich formula (`amm_math.py`) to < 1 % error.
- **Prevent (user side)** — `defense.py` scores every defense by `J = harm + λ₁·gas + λ₂·latency +
  λ₃·revert-risk` and picks the minimum. Adaptive slippage computes the largest slippage `s*` at which the
  best sandwich is unprofitable.
- **Predict (protocol side)** — `guardian.py` replays each pending tx on the shadow fork, extracts attack
  features (flash loan vs pool size, intra-tx price deviation vs TWAP, protocol value change, sender
  profit) and applies an explainable rule-based score. Arbitrage (T3) has a flash loan and profit but **no
  protocol loss** → benign.
- **Prevent (protocol side)** — `responder.py` sends `pause()` with a higher priority fee so it is ordered
  before the attack in the same block (Forta / Hypernative-style pre-execution defense). `SafeLending`
  additionally resists manipulation by design (TWAP + deviation guard).
- **Fast predictor** — `predictor.py` trains a gradient-boosting model on a few thousand simulated
  scenarios, predicting harm in < 1 ms for the dashboard's live mempool labels (the simulator stays the
  source of truth for decisions).

---

## Repository layout

```
contracts/          Foundry project (Solidity 0.8.24) + forge tests
  src/              Token, SimpleAMM, FlashLender, TWAPOracle, Vulnerable/SafeLending,
                    ProtectedRouter, CommitRevealSwap, Governance, harness/ (attackers)
  test/             22 unit + scenario tests
mevshield/          Python package (web3.py, typer, scikit-learn, streamlit)
  config · devnet · chain · deploy · shadow · builder · amm_math · risk · predictor ·
  defense · relay · guardian · benchmark · store · cli · demo · harness/
dashboard/app.py    Streamlit dashboard
tests/              Python unit tests
data/               deployments.json, results.csv, model.pkl, mevshield.db (generated)
docs/               report, architecture diagram
```

---

## Troubleshooting

- **`anvil` / `forge` not found** — install Foundry (`foundryup`). The code also auto-detects
  `~/.foundry/bin`.
- **"something is already listening on 8545"** — run `mevshield down`, or an old anvil is running; only
  processes started by `mevshield up` are ever stopped.
- **Ports** — override with `MEVSHIELD_MAIN_PORT` / `MEVSHIELD_SHADOW_PORT`.
- **Reset the world** — `mevshield reset` returns to the clean deployed snapshot without restarting anvil.
