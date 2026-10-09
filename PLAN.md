# MEV Shield — Implementation Plan

**Goal:** a system that **predicts** flash-loan and MEV attacks *before* they execute and **prevents** them —
demonstrated end-to-end on an isolated local Ethereum devnet with a CLI command chain and a live web dashboard.

Based on: *Flash Loan Arbitrage & MEV Defense System — Research Landscape, Open Problems, and Publishable Research Agenda*
(core idea: "Adaptive Counterfactual MEV Defense" + "Flash-Loan-Aware Attack Synthesis", scaled down to a course project).

---

## 1. The idea in one paragraph

Every attack we care about has to pass through two places before it does damage: **the mempool** (where pending
transactions wait) and **the EVM** (where they execute). MEV Shield sits at both. It keeps a private *shadow fork*
of the chain and, for every transaction of interest, runs it there first — **counterfactually**: "what happens if an
attacker reacts to this?" (to protect users) and "what happens to the protocol if this executes?" (to protect protocols).
The simulated outcome is the **prediction**. The **prevention** is then chosen automatically: re-route or re-shape the
user's transaction so the attack is impossible or unprofitable, or — for an attack already sitting in the mempool —
have a Guardian pause the protocol in the same block, ordered *before* the attack, so the attack reverts.

The attacker code in this repo exists only as a **test harness** to prove the shield works. Everything runs on a local
devnet; nothing touches mainnet or real protocols.

---

## 2. Threats covered

| ID | Attack | How it works | Victim |
|----|--------|--------------|--------|
| **T1** | **Sandwich (MEV)** | Bot sees a big swap in the public mempool → buys first (higher gas tip) → victim buys at a worse price → bot sells after | A user |
| **T2** | **Flash-loan oracle manipulation** (bZx / Harvest pattern) | Flash-borrow a huge amount → dump it into an AMM to distort the spot price → a lending protocol that reads that spot price lets the attacker over-borrow → repay flash loan, keep the loot | A protocol (bad debt) |
| **T3** | **Flash-loan arbitrage** (benign baseline) | Borrow → buy cheap pool → sell expensive pool → repay | Nobody — this is *legitimate* MEV and must **not** be flagged (false-positive test) |
| T4 *(stretch)* | Flash-loan governance attack (Beanstalk pattern) | Flash-borrow governance tokens → vote + execute a malicious proposal in one tx | A DAO treasury |

T3 matters: the paper stresses that flash loans and arbitrage are *not* attacks by themselves. A good detector
must separate T2 (harmful) from T3 (useful).

---

## 3. How we PREDICT

### 3a. User side — "Is my transaction going to get sandwiched, and how badly?"
Before a user's swap is sent, the **Risk Engine** runs on the shadow fork:

1. `snapshot` → execute the swap alone → `out_benign`
2. `revert` → simulate a **rational attacker** who has unlimited flash-loan capital and sees the transaction
   (leakage level = *public*). Search the front-run size `f` (binary/golden-section search) that maximises
   attacker profit **subject to** the victim's `minAmountOut` (if the victim reverts, the attacker earns nothing)
   and **subject to** profit > gas + flash-loan fee (attacks that don't pay won't happen).
3. **Predicted harm** `H(tx) = out_benign − out_attacked` (the paper's counterfactual-harm definition), plus attacker profit
   and `P(attack)` (1 if a profitable attack exists under the leakage model, else 0; softened by the ML model below).
4. Repeat for leakage levels **public / hint (pool only, amount hidden) / private** → harm per channel.
5. Cross-check against the **closed-form constant-product sandwich formula** (validation table for the report).

**Fast predictor (ML):** simulation costs ~100s of ms. We generate a few thousand simulated scenarios
(trade size / liquidity, slippage tolerance, pool fee, gas price) and train a small scikit-learn model
(gradient boosting) that predicts harm in < 1 ms. Report its accuracy vs the simulator (MAE, R², calibration).
Used for the dashboard's live mempool risk labels; the simulator stays the source of truth for decisions.

### 3b. Protocol side — "Is this pending transaction an attack on my protocol?"
The **Guardian** watches the mempool (`txpool_content`). For each new pending tx it replays it on the shadow fork
(`anvil_impersonateAccount` + snapshot/revert) and extracts **attack features**:

| Feature | Source |
|---|---|
| Flash loan taken? amount vs pool liquidity | `FlashLoan` event logs |
| Max intra-tx price deviation of each AMM pool vs its TWAP | `Sync`/`Swap` events, reserves |
| Protected protocol's net value change (TVL delta, new bad debt) | balances & positions before/after |
| Sender's net profit | balances before/after |
| Did the sender borrow against collateral priced at a manipulated spot price? | lending-protocol events |

**Score** = rule-based (explainable for the demo) e.g. *flash loan* ∧ *price deviation > 10 %* ∧ *protocol loss > 0* ∧
*sender profit > 0* → **ATTACK (critical)**. Flash-loan arbitrage (T3) has a flash loan and profit but **no protocol loss**
and moves prices *toward* each other → **benign**. Sandwich pairs are detected as two txs from the same sender, same pool,
opposite directions, bracketing a victim by gas tip.

---

## 4. How we PREVENT

### 4a. User-side defenses (chosen automatically per transaction)

| Defense | What it does | Harm | Cost / downside |
|---|---|---|---|
| `public` | Send normally | high for large swaps | none |
| `adaptive-slippage` | Engine computes the **largest slippage tolerance `s*` at which the best sandwich is no longer profitable** and sets `minAmountOut` to it | ≈ 0 | slightly higher revert risk if the market moves |
| `private-relay` | Send through our relay → goes straight into the block, never visible in the public mempool | ≈ 0 | relay fee, trust in relay |
| `commit-reveal` | Block N: commit `hash(order, salt)`; block N+1: reveal & execute. Attacker can't see the trade in time | ≈ 0 | 2 txs of gas, +1 block latency |
| `split` | Break into `n` chunks across blocks (sandwich profit grows super-linearly with size) | lower | n× gas, n blocks |

**Defense Selector:** `J(d) = Harm_d + λ₁·GasCost_d + λ₂·Latency_d + λ₃·RevertRisk_d` → pick `argmin J`.
Expected (and demo-able) behaviour: small swaps → `public` (no defense is worth paying for);
medium → `adaptive-slippage`; large → `private-relay` / `commit-reveal`.

### 4b. Protocol-side defenses

| Defense | Where | What it does |
|---|---|---|
| **Guardian circuit-breaker** (active) | off-chain Guardian + `pause()` in protocol | When a critical attack is detected in the mempool, the Guardian sends `pause()` with a **higher priority fee** → ordered *before* the attack in the same block → attack reverts. (Forta / Hypernative-style pre-execution defense.) |
| **TWAP oracle** (by design) | `SafeLending` | Prices collateral with a time-weighted average instead of spot → single-tx manipulation barely moves it → attack becomes unprofitable. |
| **Price-deviation guard** (by design) | `SafeLending` | Reverts borrows if spot deviates from TWAP by > X % in the current block. |

We will show the **same** T2 attack against three targets: `VulnerableLending` (succeeds) →
`VulnerableLending` + Guardian (detected & blocked) → `SafeLending` (fails by design).

---

## 5. Architecture

```
                         ┌──────────────────────────── Anvil devnet (main) ───────────────────────────┐
  users / CLI ──public──►│  mempool (no automine, ordered by priority fee)  ──►  block                 │
        │                │  Contracts: Tokens · SimpleAMM×2 · FlashLender · Vulnerable/SafeLending ·   │
        │  private-relay │             TWAPOracle · ProtectedRouter · CommitRevealSwap                 │
        └───────────────►│  (relay injects tx at block-build time, skipping the public pool)           │
                         └──────▲─────────────────────────────▲───────────────────────────────▲────────┘
                                │ txpool_content              │ txpool_content                │ pause() w/ higher tip
                        ┌───────┴────────┐            ┌───────┴─────────┐             ┌───────┴────────┐
                        │ Attacker bots  │            │    Guardian     │────────────►│   Responder    │
                        │ (test harness) │            │ detect T1/T2    │             └────────────────┘
                        └────────────────┘            └───────┬─────────┘
                                                              │ replay pending tx
  user swap ──► Risk Engine ──► Defense Selector              ▼
                 │  counterfactual sims           ┌────────────────────────────┐
                 └───────────────────────────────►│ Shadow fork (anvil --fork) │  snapshot / revert
                                                  └────────────────────────────┘
                                 all events ──► SQLite ──► Streamlit dashboard
```

The **shadow fork** is a second Anvil instance forked from the main devnet. All "what-if" simulations run there,
so predictions never disturb the real mempool.

---

## 6. Tech stack

| Part | Choice |
|---|---|
| Local chain | **Anvil** (Foundry), main: `--no-mining --order fees`; shadow: `--fork-url <main>` |
| Contracts | Solidity 0.8.x + **Forge** tests (own minimal AMM, not real Uniswap — same `x·y=k` math, no legacy-compiler pain) |
| Off-chain | **Python 3.12** venv: `web3.py`, `typer` + `rich` (CLI), `scikit-learn`, `pandas`, `streamlit` + `plotly` |
| Storage | SQLite (alerts, predictions, experiment results) |
| Platform | Native Windows (Foundry Windows binaries, Python 3.12 already installed); **fallback: WSL2** |

---

## 7. Repository layout

```
Blockchain Project/
├── PLAN.md
├── README.md                     setup + demo commands
├── contracts/                    Foundry project
│   ├── src/
│   │   ├── Token.sol             mintable ERC20
│   │   ├── SimpleAMM.sol         constant-product pool, 0.3 % fee, Sync/Swap events, cumulative price for TWAP
│   │   ├── FlashLender.sol       Aave-style flashLoan(), 0.09 % fee, FlashLoan event
│   │   ├── TWAPOracle.sol
│   │   ├── VulnerableLending.sol spot-price collateral, pause() by guardian
│   │   ├── SafeLending.sol       TWAP + deviation guard
│   │   ├── ProtectedRouter.sol   minAmountOut + deadline
│   │   ├── CommitRevealSwap.sol
│   │   └── harness/              FlashArbitrage.sol · SandwichAttacker.sol · OracleAttacker.sol
│   └── test/                     forge tests for every contract + attack/defense scenarios
├── mevshield/                    Python package
│   ├── chain.py                  web3 connection, ABIs, accounts, snapshot/revert, mining control
│   ├── devnet.py                 start/stop main Anvil + shadow fork
│   ├── deploy.py                 deploy + seed pools/lending, write deployments.json
│   ├── amm_math.py               closed-form AMM + sandwich formulas
│   ├── risk.py                   counterfactual Risk Engine (user side)
│   ├── predictor.py              ML fast predictor (train / load / predict)
│   ├── defense.py                defenses + selector J(d)
│   ├── relay.py                  private relay
│   ├── guardian.py               mempool watcher, shadow-fork replay, feature extraction, scoring
│   ├── responder.py              circuit-breaker (pause with higher tip)
│   ├── harness/                  attacker bots (sandwich, oracle attack, arbitrage) + benign traffic generator
│   ├── benchmark.py              mixed-workload experiments → results
│   ├── store.py                  SQLite
│   └── cli.py                    `mevshield` command
├── dashboard/app.py              Streamlit
├── data/                         deployments.json, results.csv, model.pkl, mevshield.db
└── docs/                         report, architecture diagram, slides outline
```

---

## 8. Demo — the command chain for the teacher (~10 min)

```bash
mevshield up                                 # start devnet + shadow fork, deploy & seed everything

# Act 1 — flash loans are powerful (benign)
mevshield arb                                # attacker balance 0 → flash loan → profit; Guardian: BENIGN

# Act 2 — sandwich: predict & prevent (user side)
mevshield swap 500 --public                  # sandwich bot strikes; block shows [front, victim, back]; victim loss shown
mevshield predict 500                        # BEFORE sending: predicted loss, attacker profit, harm per defense, recommendation
mevshield swap 500 --protect                 # selector picks a defense → bot sees nothing / attack unprofitable → loss ≈ 0
mevshield predict 5                          # tiny swap → "public is fine" (no over-protection)

# Act 3 — flash-loan oracle attack: predict & prevent (protocol side)
mevshield attack oracle --target vulnerable  # protocol drained, bad debt shown
mevshield reset
mevshield guardian start                     # (2nd terminal) live mempool monitor
mevshield attack oracle --target vulnerable  # Guardian: CRITICAL → pause() ordered first → attack reverts, funds safe
mevshield attack oracle --target safe        # TWAP lending: attack unprofitable by design

# Act 4 — evidence
mevshield benchmark                          # 500-tx mixed workload: detection precision/recall/FPR,
                                             # harm with vs without shield, adaptive vs baselines, regret
mevshield dashboard                          # live mempool w/ risk labels, block ordering, alerts, charts

mevshield demo                               # runs Acts 1–4 automatically with "press Enter" pauses (backup)
```

---

## 9. Evaluation (what goes in the report)

**Prediction quality**
- Risk Engine vs closed-form sandwich formula: error ≤ 1 %
- ML predictor vs simulator: MAE, R², latency
- Guardian on a mixed workload (benign swaps, arbitrage, sandwiches, oracle attacks): precision, recall, false-positive
  rate (arbitrage must not be flagged), detection latency

**Prevention quality**
- User harm with no protection vs each fixed defense vs adaptive selector, across trade sizes
- Baselines (from the paper): *always-public*, *always-private*, *static 0.5 % slippage*, *best-in-hindsight* → **regret**
- Protocol losses: vulnerable vs Guardian-protected vs SafeLending
- Costs: extra gas, added latency (blocks), revert rate

**Honest limitations** (report section): local devnet, single builder, simplified relay trust model, rational-attacker
assumptions are explicit, no cross-chain/CEX MEV.

---

## 10. Build phases (implementation order, each ends with a verification)

| # | Phase | Deliverable | Verified by |
|---|---|---|---|
| 0 | Environment | Foundry installed, Python 3.12 venv, `git init` | `anvil --version`, `forge --version`, `pip install -e .` |
| 1 | Core contracts | Token, SimpleAMM, FlashLender, TWAPOracle | forge unit tests |
| 2 | Victim protocols + harness contracts | Vulnerable/SafeLending, FlashArbitrage, SandwichAttacker, OracleAttacker | forge scenario tests: attacks succeed/fail where expected |
| 3 | Python core | devnet, chain, deploy, `mevshield up` | contracts deployed, pools priced correctly |
| 4 | Mempool + attack harness | sandwich bot, oracle attack, arbitrage, `swap --public` | sandwich lands in correct order, victim loss > 0 |
| 5 | **Prediction (user side)** | shadow fork, Risk Engine, closed-form check, `predict` | predicted harm ≈ realised harm from Phase 4 |
| 6 | **Prevention (user side)** | relay, router, commit-reveal, split, selector, `swap --protect` | loss ≈ 0 under attack bot; small swaps stay public |
| 7 | **Prediction + prevention (protocol side)** | Guardian, responder, SafeLending demo | oracle attack reverted; arbitrage not flagged |
| 8 | Evidence | benchmark, ML predictor, results.csv, plots | metrics table generated |
| 9 | Dashboard + demo | Streamlit app, `mevshield demo`, README, report outline | full demo runs clean from scratch |
| 10 | *(stretch)* | T4 governance attack + snapshot-voting defense | forge + CLI |

### Team split (3–4 people, for ownership/presentation)
- **A — Contracts:** Phases 1–2 (+ T4)
- **B — Chain, harness, Guardian:** Phases 3–4, 7
- **C — Risk Engine, defenses, ML, benchmark:** Phases 5–6, 8
- **D — Dashboard, report, slides, demo rehearsal:** Phase 9 (also the team's tester)

---

## 11. Technical risks & fallbacks

| Risk | Fallback |
|---|---|
| Foundry on native Windows misbehaves | Run Anvil/Forge inside WSL2; Python talks to it over `localhost:8545` |
| `anvil --order fees` doesn't order the way the demo needs | Our own Python **block builder**: pull `txpool_content`, drop & re-submit in priority order (also a nice PBS teaching point) |
| Shadow-fork replay of pending txs is slow | Cache fork per block; only replay txs touching watched contracts; ML predictor for the live dashboard |
| web3.py incompatibility | Pin versions in `pyproject.toml`; use Python 3.12 (not 3.14) |

---

## 12. Decisions taken (defaults — change any of them)
- Oracle-manipulation attack (T2) is **core**, not stretch — it's the flash-loan half of "predict & prevent".
- Governance attack (T4) is stretch.
- Commit-reveal **kept**; `split` is the first defense to drop if time is short.
- Native Windows first, WSL2 as fallback.
- Project name: **MEV Shield** (`mevshield` CLI).
