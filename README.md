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
.venv\Scripts\pip install pytest
.venv\Scripts\python -m pytest tests -q        # 12 Python tests
```

All `mevshield …` commands below assume the venv is active (`.venv\Scripts\activate`), or prefix them with
`.venv\Scripts\mevshield`.

---

## The live web demo — one command

```bash
mevshield start
```

`start` boots the chain + shadow fork, deploys and seeds everything, trains the ML model if needed, then
opens the **web dashboard** (`http://127.0.0.1:8000`). From that single page you drive the whole system and
watch it react in real time:

- **Swap, unprotected** — the sandwich bot front-runs; the block renders live as `[frontrun, victim, backrun]`
  and the victim's loss is shown.
- **Predict** — the counterfactual Risk Engine forecasts the harm and the defense selector ranks every
  option (`J = harm + gas + latency + revert-risk`) before anything is sent.
- **Swap, protected** — the selector's choice is applied; the attack becomes unprofitable, loss ≈ 0.
- **Oracle attack** — drains `VulnerableLending`.
- **Oracle attack · Guardian on** — the Guardian detects it (CRITICAL) and its `pause()` is ordered one slot
  **before** the attack, which reverts; funds intact.
- **Oracle attack · SafeLending** — fails by design (TWAP + deviation guard).
- **Arbitrage** — a flash loan with profit but no protocol loss → correctly ruled **benign** (no false positive).

The event stream, block builder, Guardian alerts, live telemetry and the trained-model panel all update over
a WebSocket as each scenario runs.

---

## CLI — individual commands

```bash
mevshield up                                 # start devnet + shadow fork, deploy & seed
mevshield serve                              # just launch the web dashboard (devnet must be up)

mevshield arb                                # benign flash-loan arbitrage; Guardian: BENIGN
mevshield swap 500 --public                  # sandwich bot strikes; [front, victim, back]; loss shown
mevshield predict 500                        # predicted loss, attacker profit, harm per defense
mevshield swap 500 --protect                 # selector picks a defense → loss ≈ 0
mevshield attack oracle --target vulnerable  # protocol drained
mevshield guardian start                     # (2nd terminal) live mempool monitor, auto-pause on CRITICAL
mevshield attack oracle --target safe        # TWAP lending: unprofitable by design
mevshield attack governance --target safe    # snapshot voting defeats the flash-loan DAO takeover

mevshield train                              # train the ML risk model; prints R² / CV / ROC-AUC / importances
mevshield benchmark                          # mixed workload: detection P/R/FPR, harm with vs without shield
mevshield demo                               # scripted terminal demo (backup for the live web demo)
mevshield down                               # stop the devnet
```

---

## How it works

```
  user swap ─► Risk Engine ─► Defense Selector            Attacker bots      Guardian ─► Responder
                  │  counterfactual sims                  (test harness)     detect T1/T2  pause() w/ higher tip
                  └──────────────► Shadow fork (anvil --fork) ◄── replay pending tx
                                   snapshot / revert
  main devnet: anvil --no-mining --order fees   ·   all events ─► SQLite + WebSocket ─► web dashboard
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
  scenarios, predicting harm in a few milliseconds for the dashboard's live mempool labels (the simulator stays the
  source of truth for decisions).

---

## Repository layout

```
contracts/              Foundry project (Solidity 0.8.24) + 22 forge tests
  src/                  Token, SimpleAMM, FlashLender, TWAPOracle, Vulnerable/SafeLending,
                        ProtectedRouter, CommitRevealSwap, Governance, harness/ (attackers)

mevshield/              Python package, grouped by responsibility
  config.py             world parameters, ports, accounts
  cli.py                the `mevshield` command
  demo.py               scripted terminal demo
  store.py              SQLite (alerts, predictions, blocks, experiments)
  benchmark.py          mixed-workload evaluation → data/results.csv
  chain/                blockchain layer
    connection.py         web3 wrapper, accounts, signing, snapshots
    devnet.py             start/stop anvil main + shadow fork
    deploy.py             deploy & seed the world
    shadow.py             shadow fork — counterfactual bundle simulation
    builder.py            block builder (searcher window, relay injection, fee ordering)
  defense/              user-side predict & prevent
    amm_math.py           exact AMM + closed-form sandwich/oracle/arbitrage math
    risk.py               counterfactual Risk Engine
    selector.py           defenses + the J(d) selector
    relay.py              private relay
    predictor.py          ML risk model (gradient-boosted; train/load/predict)
  guardian/             protocol-side predict & prevent
    monitor.py            mempool monitor, feature extraction, explainable scoring
    responder.py          circuit-breaker (pause ordered before the attack)
  harness/              test-harness attackers + benign traffic
    attacks.py · sandwich_bot.py · traffic.py
  web/                  the live web dashboard
    server.py             FastAPI + WebSocket backend
    engine.py             drives the devnet and emits realtime events
    static/               index.html · styles.css · app.js  (no framework)

tests/                  Python unit tests (12)
data/                   deployments.json, results.csv, model.pkl, mevshield.db (generated)
docs/                   report, architecture diagram, slides outline
```

---

## Troubleshooting

- **`anvil` / `forge` not found** — install Foundry (`foundryup`). The code also auto-detects
  `~/.foundry/bin`.
- **"something is already listening on 8545"** — run `mevshield down`, or an old anvil is running; only
  processes started by `mevshield up` are ever stopped.
- **Ports** — override with `MEVSHIELD_MAIN_PORT` / `MEVSHIELD_SHADOW_PORT`.
- **Reset the world** — `mevshield reset` returns to the clean deployed snapshot without restarting anvil.
