# 🛡️ MEV Shield

**See blockchain attacks and their defenses, one transaction at a time.** MEV Shield combines a visual
Ethereum attack lab with a live connection to **Ethereum Sepolia**, including wallet-signed deployment
and interaction with a protected test-ETH vault. The website uses a responsive white theme.

Every attack we care about passes through two places before it does damage: the **mempool** (where pending
transactions wait) and the **EVM** (where they execute). MEV Shield sits at both. It keeps a private
**shadow fork** of the chain and, for every transaction of interest, runs it there first —
*counterfactually* — to **predict** the outcome, then **prevents** the attack automatically: it re-shapes
the user's transaction so a sandwich is unprofitable, or has a Guardian pause the protocol in the same
block, ordered *before* an attack, so the attack reverts.

> The attacker code in `contracts/src/harness/` and `mevshield/harness/` is a **test harness only** — it
> exists to prove the shield works on the local Anvil lab. The separate Sepolia view reads the public
> network and supports real testnet vault transactions. It never uses the public Anvil keys. Mainnet
> transactions are not supported.

See [PLAN.md](PLAN.md) for the full design and [docs/REPORT.md](docs/REPORT.md) for the write-up.

## Integrated extensions and reliability upgrades

The six extensions from `bhuvang2026in/MEV-Shield-Defence` (commit `157d173`) are integrated with the
Sepolia vault and dashboard: multi-pool MEV routing, cashback auctions, batch matching, firewall
lending, coordinated sequence detection, and saved analytical replays. Explore them at `/lab`.

New at **`/safety`**:

- **Stateful invariants:** multi-user vault, lending and settlement sequences, plus guard-removal
  mutation checks that demonstrate the suite can detect a missing protection.
- **Transaction safety reports:** simulate known local swaps/lending actions, inspect token/debt changes,
  approvals and gas, then execute a one-shot state-bound preview and reconcile the actual receipt.
- **ML uncertainty:** disjoint training/calibration/test sets, nominal harm intervals, calibrated
  classifier scores, abstention and actual shadow-fork fallback. Failed fallback means unknown risk.

Run `python scripts/verify_security.py` to create verification evidence. A trained real-context model now ships with the project and loads in the local lab. At `/safety`, the **read-only Ethereum assessment** also reads four real V2 pools.

The model uses **10,503 real mainnet swap inputs**, declared policy scenarios and separate synthetic lab augmentation. On its chronological counterfactual holdout: **99.91% balanced accuracy, 99.96% recall, $3.23 harm MAE**. These are **modeled economic-risk scores, not observed-attack detection accuracy**; the observed-size holdout has no positive cases. See [data, evaluation and limitations](docs/REAL-MARKET-ML.md), [implementation notes](docs/SECURITY-UPGRADES.md), [validation](docs/VALIDATION.md), and [presentation notes](docs/PRESENTATION-NOTES.md).

## Research extensions

The project now also includes **MEV-aware multi-pool routing, funded backrun cashback auctions,
two-order batch matching, an on-chain lending firewall, coordinated transaction detection, and a
reproducible attack replay lab**. Open **http://127.0.0.1:8000/lab** after starting the server.

```powershell
.venv\Scripts\python.exe -m mevshield.cli extensions run all
.venv\Scripts\python.exe -m mevshield.cli extensions replay --save
```

The first command runs isolated EVM-backed experiments and writes `data/extensions-results.json`;
the second runs an offline analytical comparison. See **[docs/EXTENSIONS.md](docs/EXTENSIONS.md)**
for individual commands, architecture, APIs, tests and the limits of each implementation.

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
.venv\Scripts\pip install -e ".[dev]"

# 3. (optional) run the test suites
cd contracts && forge test && cd ..            # contract examples, fuzz and invariant tests
.venv\Scripts\pip install pytest
.venv\Scripts\python -m pytest tests -q        # offline and adapter tests
```

All `mevshield …` commands below assume the venv is active (`.venv\Scripts\activate`), or prefix them with
`.venv\Scripts\mevshield`.

---

## The live web demo — one command

```bash
mevshield start --build
```

`start` boots the chain + shadow fork, deploys and seeds everything, loads the bundled ML model (or trains a legacy lab fallback if absent), then
opens the **web dashboard** (`http://127.0.0.1:8000`). From that single page you drive the whole system and
watch it react in real time. The main view explains each step with an animated transaction journey,
actual token movements decoded from receipts, transaction ordering, a price chart that includes
intra-block reserve changes, and measured outcomes. Replay repeats the story without sending new
transactions. The four scenario tabs expose sandwich, oracle, benign arbitrage, and governance cases.

Available experiments include:

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

The event stream, block builder, Guardian alerts, and live telemetry update over
a WebSocket as each scenario runs. Technical evidence is available under **Under the hood**; plain-language
explanations lead the interface. Choose an individual swap defense or let the selector recommend one.

### Real blockchain: Ethereum Sepolia

The **Live blockchain** view works even when the local lab is offline:

1. Start the website with `mevshield serve --no-open-browser`, or use `mevshield start --build` for both modes.
2. Open `http://127.0.0.1:8000/#network`. Recent blocks are read from the public RPC, with explorer links.
3. Open the website in a browser with an EIP-1193 Ethereum wallet (for example, MetaMask). Click **Connect wallet**;
   the app requests Sepolia, chain **11155111**. A Codex browser panel may not have a wallet extension.
4. Fund your wallet with Sepolia test ETH using the [Ethereum testnet resources](https://ethereum.org/developers/docs/networks/#sepolia).
5. Click **Deploy ShieldVault**, review the transaction in your wallet, and wait for a receipt. Alternatively,
   attach an existing matching vault address; the backend verifies its deployed runtime bytecode.
6. Deposit e.g. **0.001 ETH**. An immediate withdrawal is rejected by the contract’s **60-second cooldown**
   during preflight. After the cooldown, withdraw and inspect the real receipt and contract events.
7. Test the **0.005 ETH per wallet per hour** withdrawal budget or use the owner-only **pause / unpause** control.

New to wallets? **Connect wallet** opens setup instructions when no extension is found, including
official MetaMask links and a copyable app URL. Create a separate demo wallet, open the app in the same
browser as the extension, and approve the connection. You can connect before getting test ETH; only
deployment and transactions need faucet funds. Pending or declined wallet requests now have visible,
actionable messages. Run `node --test tests/wallet-connection.test.cjs` to check the connection adapter
without installing a wallet.

The server only reads chain state and prepares unsigned, chain-bound transactions. Your wallet holds your
keys and signs each write. The UI tracks a submitted hash across refreshes, reports reversions and gas use,
and distinguishes inclusion from finality. There is no automatic public-network deployment at startup.

The default RPC is `https://ethereum-sepolia-rpc.publicnode.com`. To use your own provider, set
`MEVSHIELD_SEPOLIA_RPC` in the server environment. Chain-ID mismatches fail closed. Credential-bearing
RPC URLs are never sent to the browser. Public endpoints can throttle; failures are displayed explicitly.

**Scope:** the vault’s Solidity protections run on Sepolia. The existing AMMs, attack harness, shadow-fork
simulations, local private relay, and controlled Guardian ordering remain in the labeled local lab. A
higher tip does not guarantee priority on a public chain, and the app does not claim to monitor or protect
all Sepolia transactions. See [docs/WEB-EXPERIENCE.md](docs/WEB-EXPERIENCE.md) for the walkthrough and limits.

### Additional defenses

- **SafeLending:** deposit collateral in an earlier block before borrowing; aggregate borrowing is capped
  at **100,000 lab USDC per block**, in addition to the TWAP and 5% deviation check.
- **ShieldVault:** reentrancy guard, effects before ETH transfers, deposit cooldown, per-wallet hourly
  withdrawal budget, and owner-only emergency pause, with events for observable state changes.
- **Website:** bounded/typed inputs, one lab operation at a time (including reset), same-origin browser
  actions and WebSockets, trusted hosts, and a restrictive content security policy. The server binds to
  localhost by default. For a custom hostname, explicitly set `MEVSHIELD_ALLOWED_HOSTS` to a comma-separated
  allowlist; exposing this educational server publicly also requires deployment-specific authentication.

After changing Solidity, run `forge build` in `contracts/`, then `python scripts/export_vault.py` at the
repository root to update the packaged wallet artifact. `mevshield start --build` also exports it. Restart
the server after recompilation. If the lab was already running older contracts, use `mevshield down` before
starting again; resetting a snapshot does not upgrade deployed bytecode.

Validation: `forge test` in `contracts/` and `python -m pytest tests -q`. Install the
test dependencies with `pip install -e '.[dev]'`. The adapter tests deploy the real vault bytecode on a
temporary local Anvil instance; they never send transactions to Sepolia. They skip EVM checks if Foundry
is missing. A real wallet signature and test ETH are required to complete the public deployment.

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

mevshield ml quote 10000                     # read-only real Ethereum pool assessment
mevshield ml train --publish                 # real-context training; promotion requires held-out gates
mevshield train --synthetic                  # explicit legacy synthetic lab training
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
- **Risk predictor** — the current pipeline in `mevshield/ml/` uses verified real pool inputs and declared policy cases (see the model card). The legacy `predictor.py` also supports simulated
  scenarios, predicting harm in a few milliseconds for the dashboard's live mempool labels (the simulator stays the
  source of truth for decisions).

---

## Repository layout

```
contracts/              Foundry project (Solidity 0.8.24) + example/fuzz/invariant tests
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

tests/                  Python math, uncertainty, web, safety and integration checks; wallet JS tests
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
