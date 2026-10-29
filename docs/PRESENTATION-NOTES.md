# MEV Shield: presentation and viva notes

This guide covers the original system, the six imported extensions, the retained Sepolia vault, and upgrades 8, 9 and 10. It describes the actual implementation rather than a future roadmap.

## Opening explanation: about one minute

“Ma'am, MEV Shield studies two related problems: users receiving worse prices because of transaction ordering, and protocols losing money because a transaction manipulates their assumptions. We first reproduce those situations on a local Ethereum chain. A shadow fork lets us test an action without changing the main demonstration chain. We compare protections, execute supported examples, and show transaction receipts and economic outcomes.

“We extended the system in three areas. Trading mechanisms include routing across two pools, a cashback backrun auction and direct matching of opposite orders. Protocol defenses include oracle checks, outflow budgets and coordinated transaction analysis. Reliability features include stateful invariant testing, transaction previews with receipt reconciliation, and ML estimates that can admit uncertainty and fall back to simulation.

“Most experiments use local mock tokens. The separate Sepolia vault demonstrates real wallet signing and on-chain controls with test ETH. The project does not claim automatic protection of every public-chain transaction.”

## Architecture to draw on the board

```text
Browser / CLI
    |
    +--> risk estimate --> uncertainty checks --> shadow-fork fallback
    |
    +--> transaction preview --> review exact action --> local execution --> receipt comparison
    |
    +--> trading experiments --> router / auction / batch settlement
    |
    +--> Guardian --> transaction or sequence simulation --> alerts / supported pause

Main Anvil chain <--> snapshot-based shadow Anvil fork
    |
    +--> mock tokens, pools, lending, governance and settlement contracts
    +--> SQLite experiment/report evidence --> web dashboard

Foundry --> multi-actor handlers --> invariants --> deliberate guard-removal checks

Separate Sepolia path: browser wallet --> ShieldVault --> public receipt
```

“The shadow fork is a disposable copy for what-if execution. It is not another consensus chain or an oracle that knows the future.”

## Terminology before the demo

| Term | Explain it simply |
|---|---|
| AMM | A contract that exchanges tokens using reserves and a pricing rule. |
| Slippage limit | The worst output a user permits relative to the quote. It is a bound, not an extra fee. |
| Sandwich | An actor trades before and after a victim to worsen the victim's execution. |
| Flash loan | A loan that must be repaid, with its fee, within the same transaction or the transaction reverts. |
| Arbitrage | Trading between differing prices. It can restore prices and is not automatically an attack. |
| TWAP | A price averaged over time, intended to reduce the influence of a short manipulation. |
| Invariant | A property that should hold after every relevant sequence of allowed actions. |
| Calibration | Checking and adjusting how uncertainty or probabilities correspond to held-out outcomes. |
| Abstention | The model explicitly declines to provide an authoritative label. |
| Atomic settlement | Every step succeeds together, or the transaction rolls back. |

## Original features

### 1. Sandwich attack and counterfactual harm prediction

**Say:** “We execute the same victim swap alone and with a modeled front-run/backrun. The difference in received output measures the additional harm.”

The attacker size is optimized under the victim's minimum output and an assumed gas/profit model. The integer constant-product model is cross-checked with EVM simulation. Large flash loans alone are not proof of malicious intent.

**Show:** Attack lab → Sandwich attack → unprotected trade, then prediction and protected trade. Point to output, attacker profit and block order. Reset to compare identical starting conditions.

**Code:** `defense/risk.py`, `defense/amm_math.py`, `harness/sandwich_bot.py`.

**Limit:** The result depends on the specified attacker, market state and ordering assumptions; it is not a prediction of every real bot.

### 2. Adaptive defense selection

**Say:** “Instead of always applying the same protection, we compare modeled harm, gas, delay and failure costs.”

Options are ordinary public submission, tighter adaptive slippage, the local private relay, commit-reveal and splitting a trade over blocks. Small trades may not justify extra protection costs.

**Explain each:** Tight slippage reduces exploitable room but can revert; private routing hides the order from the lab's public searchers but trusts the relay; commit-reveal hides order details until reveal and requires being the pool's first trade in that block; time splitting changes exposure and adds transactions/delay.

**Code:** `defense/selector.py`, `defense/relay.py`, `CommitRevealSwap.sol`.

**Limit:** The original benchmark's legacy regret calculation is harm-only. Use the extensions replay's completed-trade/cost columns for the richer comparison, and do not claim the original benchmark already measures every real cost. Commit-reveal can lose availability under pool contention. Local private routing assumes no leakage.

### 3. Flash-loan oracle manipulation and SafeLending

**Say:** “The vulnerable lender trusts a spot price that can be moved inside an attack. SafeLending instead uses a TWAP, a deviation check, an earlier-block collateral requirement and a borrowing cap.”

**Show:** Oracle scenario against vulnerable lending, then SafeLending. Explain that the failed transaction rolls back.

**Code:** `VulnerableLending.sol`, `SafeLending.sol`, `TWAPOracle.sol`, `harness/OracleAttacker.sol`.

**Limit:** This implementation has a simplified TWAP observation design, not a comprehensive production oracle. `minPeriod` controls updates; it is not a minimum horizon enforced on every `twap()` read. Staleness and independent-feed validation remain future work.

### 4. Guardian detection and response

**Say:** “The Guardian simulates a pending transaction, extracts features and gives an explainable verdict. In our fee-ordered lab it can send a pause before a supported attack.”

**Show:** Oracle attack with Guardian enabled. Inspect the pause and attack receipt order.

**Code:** `guardian/monitor.py`, `guardian/responder.py`, `chain/builder.py`.

**Limit:** Public builders need not follow fee order, and private attacks may be invisible beforehand. The legacy single-transaction score also uses cash outflow as a loss proxy; that can misclassify ordinary loans. Do not claim universal economic-damage detection or perfect precision. This task adds reliability around selected features, not the accounting-aware Guardian from proposal 1.

### 5. Benign arbitrage baseline

**Say:** “A flash loan can fund useful arbitrage. Our arbitrage scenario earns profit without draining lending, so it serves as a false-positive check.”

**Show:** Arbitrage scenario, converging pool prices and the Guardian verdict.

**Code:** `harness/FlashArbitrage.sol`, `harness/attacks.py`.

**Limit:** Passing this example is evidence for this example, not a measured zero false-positive rate across real DeFi.

### 6. Governance attack and historical voting power

**Say:** “Borrowing voting tokens for one transaction should not let someone seize the treasury. Snapshot voting uses earlier balances, and the delay prevents immediate proposal execution.”

**Show:** Governance scenario against vulnerable and snapshot DAOs.

**Code:** `Governance.sol`, `harness/GovernanceAttacker.sol`.

**Limit:** Simplified DAO model. It does not cover compromised administrators, long-term vote buying or all governance attacks.

### 7. Live dashboard, event evidence and storage

**Say:** “The interface turns contract events, receipts and experiment measurements into an explanation. WebSockets stream progress; SQLite retains alerts and reports.”

Animated story replay repeats an already observed narrative. It does not resubmit transactions. The separate research replay is a mathematical experiment with a saved configuration.

**Code:** `web/engine.py`, `web/server.py`, `store.py`.

### 8. Real Sepolia wallet and ShieldVault

**Say:** “This path demonstrates an actual public testnet transaction. The browser wallet signs; the backend prepares an unsigned chain-bound transaction and never takes the user's key.”

**Show if funded:** Connect wallet on Sepolia, attach/deploy the vault, deposit test ETH and inspect the receipt. Explain the 60-second withdrawal cooldown, per-wallet hourly budget, pause and reentrancy protection.

**Code:** `ShieldVault.sol`, `chain/public.py`, `wallet-connection.js`.

**Limit:** It protects this vault's operations. It does not demonstrate the local AMM defenses operating across Sepolia. A per-wallet limit is not a protocol-wide limit and can be distributed across wallets. Inclusion is distinct from finality. No public-network signatures are required for the local presentation.

## Six imported extensions

### 9. MEV-aware routing across two pools

**Say:** “The earlier selector chooses how to protect a trade. This router also chooses where to execute it, including allocations across our two pools.”

It compares five allocations: 0/100, 25/75, 50/50, 75/25 and 100/0. Expected net output accounts for modeled profitable sandwich harm and estimated gas. Pool fees already enter the AMM quote and are not subtracted twice. The contract checks each leg and a global output limit atomically.

**Show:** Research lab → Route & execute. Compare the best raw quote with the selected allocation and actual output.

**Code:** `extensions/routing.py`, `MEVRouter.sol`.

**Limit:** Two direct mock pools, discrete allocation search and a specified per-leg attacker model. No arbitrary multi-hop search or Uniswap V3 integration. Better execution is scenario-dependent; do not promise the router always wins.

### 10. Cashback backrun auction

**Say:** “After a swap creates a price difference, a backrunner can restore prices. Here two funded local searchers offer rebates; a valid chosen bid shares some value with the user.”

The contract binds a bid to the user's order and executes swap, backrun, profit check and rebate in one transaction. Failed profitability checks revert the whole settlement. Losing escrows are refunded/canceled by the demo.

**Show:** Run backrun auction. Point to both bids, winning rebate, searcher profit and the settlement hash. Quote gas separately from gross rebate.

**Code:** `CashbackAuction.sol`, `extensions/settlement.py`.

**Limit:** Funded local bids with fixed supported actions, not a deployed MEV-Share network or guaranteed positive net cashback. Inspired by [Flashbots MEV-Share](https://docs.flashbots.net/flashbots-mev-share/introduction).

### 11. Batch settlement and direct matching

**Say:** “Opposite trades can exchange some tokens directly. Only the remaining imbalance needs the AMM.”

The actual implementation escrows orders through on-chain placement transactions. A solver chooses a price, but each complete order's minimum output must hold. There is a collection window, expiry, cancellation and a closed-order flag.

**Show:** Settle two-order batch. Explain the matched fraction, residual AMM trade and users' received tokens.

**Code:** `BatchSettlement.sol`, `extensions/settlement.py`.

**Limit:** This is not an off-chain signed-intent marketplace. The price/solver is not claimed to be fair merely because limits pass, and batching does not eliminate every MEV opportunity. Inspired by [CoW's Coincidence of Wants](https://docs.cow.fi/cow-protocol/concepts/how-it-works/coincidence-of-wants).

### 12. On-chain lending firewall

**Say:** “Even if the off-chain Guardian is late, a contract can directly reject borrowing that exceeds its budget.”

The default firewall caps aggregate borrowing at 10,000 mock USDC per block and 25,000 over the last 10 blocks. Budgets are shared across callers; repayment does not restore borrowing allowance. Moderate spot/TWAP disagreement halves the limits before the harder deviation check rejects borrowing.

**Show:** Two 8,000-USDC borrow requests in one block. One succeeds, the other exceeds the shared budget. Then explain the rolling window.

**Code:** `FirewallLending.sol`, `extensions/settlement.py`.

**Limit:** Budgets limit the rate of loss for these supported paths; they do not establish that every allowed borrow is harmless or bound lifetime loss. “Confidence” here is a deviation rule, not a calibrated statistical probability.

### 13. Coordinated sequence detection

**Say:** “A transaction may look ordinary in isolation. We connect relevant transactions through pools, token flows and explicit links, then simulate a bounded ordered sequence against a victim-only baseline.”

**Show:** Analyze coordinated sequence. Point out distinct submitting addresses and the counterfactual victim loss.

**Code:** `extensions/coordinated.py`, Guardian sequence integration.

**Limit:** Shared pool usage alone does not prove coordination. The local demonstration includes explicit actor links; it does not solve general wallet-ownership attribution. Sequence analysis is bounded, not a search of every possible ordering.

### 14. Reproducible experiment replay

**Say:** “Changing one parameter while keeping the seed and initial conditions fixed lets us make a controlled comparison.”

Change liquidity, victim slippage, attacker capital, gas, Guardian delay and tips. Save a scenario, reload it and export results. The comparison includes completion and cost; failed trades are excluded from the winner.

**Show:** Increase slippage or reduce liquidity, compare defenses, save and reload. Add one block of Guardian delay and show the ordering assumption change.

**Code:** `extensions/replay.py`, research-lab UI.

**Limit:** Deterministic integer-AMM replay, not historical incident replay. Gas and private-relay behavior are modeled. Guardian ordering in this panel is a model check, not an executed pause. The isolated suite reverts scenario blocks, so its hashes are experiment evidence rather than persistent canonical transactions.

## Three added reliability features

### 15. Upgrade 8: stateful invariants and mutation checks

**Say:** “Example tests ask whether a known demonstration works. Stateful tests ask whether important properties survive thousands of mixed actions by multiple users.”

Three handler suites cover vault deposits/withdrawals/pauses/time/donations; lending deposits/borrows/repayments/withdrawals/budget expiry; and batch/auction placement, settlement, cancellation and expiry.

**Properties:** vault claims are backed and match ghost accounting; unauthorized policy changes fail; lending cash plus debt reconciles, collateral/debt totals match user accounting, shared budgets hold; every open order and bid remains backed by escrow.

**Why ghost accounting?** “The test maintains an independent record of deposits and withdrawals, so it can compare expected claims with contract state.”

**Why backing is an inequality?** Unsolicited ETH can increase a vault's balance without creating a user claim.

**Why a separate successful-action test?** A fuzzer that only produces reverting calls can pass without exercising useful behavior. Deterministic paths establish real deposits, withdrawal, borrow, repay and settlement.

**Show:** `python scripts/verify_security.py`, then Transaction safety → Verification. The runner uses seed `0x5eed`, 64 runs and depth 48. It weakens only isolated copies by removing the vault cooldown and the firewall's per-block budget. The intended tests must fail; a compile failure does not count as detecting the mutant.

**Code:** `contracts/test/Invariants.t.sol`, `scripts/verify_security.py`.

**Limit:** Finite randomized testing and two deliberate mutation checks are not formal proof or a production audit. Known counterexamples and seeds must be retained and investigated.

### 16. Upgrade 9: transaction previews and receipt reconciliation

**Say:** “Before executing a supported action, we show its predicted token and debt changes, existing approval, gas estimate and policy failure. After execution we compare those predictions with actual accounting changes.”

Supported local actions are swaps through the known router and deposit, borrow, repay and withdraw on SafeLending or FirewallLending. Only Alice, Bob and Carol are accepted; arbitrary targets, calldata and approvals are not exposed.

A preview binds the sender, target, value, calldata, nonce, chain and fee fields. It also records the simulated block hash and recipient code hash, expires after two minutes, rejects intervening chain/pending/relay activity, and can execute once. Changing a form invalidates the displayed execution option. A server restart retains report history but discards executable previews.

**Show:** Preview a 1,000-USDC swap. Point to negative USDC, positive WETH, the known spender and unlimited pre-seeded lab allowance. Execute, then show matching observed changes and actual gas. Next preview a borrow without collateral: it is blocked. Demonstrate deposit 10 WETH followed by borrowing 1,000 USDC in a later block.

**Code:** `defense/safety.py`, `/safety`, safety API routes, SQLite report history.

**Limit:** The preview simulates the action alone; it is not sandwich protection and cannot promise future public-chain ordering. Public transactions use the separate wallet flow. Uncoordinated CLI/external writers must not run during the local experiment. Reset/isolation can remove a historical receipt from canonical local state.

### 17. Upgrade 10: calibrated ML, abstention and fallback

**Say:** “A fast model should also tell us when it lacks enough evidence. We reserve separate samples for training, calibration and final testing.”

The split is 60% training, 20% calibration and 20% test. The gradient-boosted harm model learns synthetic AMM outcomes. A separate logistic calibration maps classifier scores; scaled split-conformal residuals form a nominal 90% harm interval. The test set reports actual interval coverage and Brier score as well as existing accuracy metrics.

The model abstains for inputs outside training support, unsupported fee tiers, wide intervals, uncertain classification or old artifacts without calibration. The live integration uses the actual pool's shadow fork as fallback. If fallback also fails, the result is unknown, not zero risk. The evidence records which source produced the final value.

**Show:** Assess a 10,000-USDC trade, inspect source/status/reasons. An uncertain model should visibly fall back to simulation. Then assess 100,000 USDC, which exceeds the synthetic training trade range. For model-only behavior inspect the unit tests and CLI metrics; do not fabricate a simulator outage during the presentation.

**Code:** `defense/predictor.py`, `DemoEngine._risk_report`, `/api/risk`.

**Example measured run:** With 4,000 synthetic samples and seed 0, R² was about 0.9723, held-out interval coverage 88.8%, and Brier score 0.0163. These are one local run's measurements; retraining can change them. 90% is a nominal target, not a promise of exactly 90% empirical coverage.

**Limit:** These are synthetic same-model holdouts, not historical mainnet validation. Exchangeability can fail under drift. The classifier label concerns modeled profitable attack conditions, not the real-world frequency of an attacker appearing. A larger neural network is not automatically better evidence.

## Suggested 8–10 minute presentation

| Time | Show and explain |
|---|---|
| 0–1 minute | Opening story and architecture. |
| 1–3 minutes | Unprotected sandwich, prediction, protected comparison; mention the four threat scenarios. |
| 3–5 minutes | Research lab routing and cashback; explain batch and firewall from their evidence. |
| 5–6 minutes | Sequence detection and saved replay parameters. |
| 6–8 minutes | Transaction safety preview → execute → receipt comparison; explain blocked borrowing. |
| 8–9 minutes | ML abstention/fallback and stateful verification/mutation evidence. |
| 9–10 minutes | Sepolia boundary, limitations, results and questions. |

For a short demonstration, pre-run the verification script. Keep the full notes available for the mechanisms you explain without executing live.

## Start-up checklist and commands

```powershell
# From the repository root, with Python/Foundry installed:
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python.exe -m mevshield.cli start --build

# Presentation pages:
# http://127.0.0.1:8000/          original lab and Sepolia wallet
# http://127.0.0.1:8000/lab      six extensions
# http://127.0.0.1:8000/safety   preview, risk and verification

# Before presenting, produce verification evidence:
.venv\Scripts\python.exe scripts/verify_security.py

# Optional explicit model retraining:
.venv\Scripts\python.exe -m mevshield.cli train --samples 4000

# EVM integration checks need running main + shadow nodes:
$env:MEVSHIELD_INTEGRATION = "1"
.venv\Scripts\python.exe -m pytest tests -q
node --test tests/wallet-connection.test.cjs
```

Use Reset between demonstrations that need the original reserves. A reset invalidates previews. Do not run isolated suites, independent CLI writers and interactive chain demos simultaneously. On Windows, if pytest's system temporary directory is inaccessible, use a new path under `data/pytest-<unique-name>` with `--basetemp`.

## Likely viva questions

**Why blockchain rather than a Python simulation alone?** The contracts enforce atomicity, balances, access control and output limits in an EVM. Python performs planning and controlled experiments; receipts establish which local operations executed. Offline replay is explicitly mathematical.

**Are all flash loans malicious?** No. They are an atomic financing mechanism. Arbitrage is the benign baseline; harm depends on the surrounding operations.

**What is different about routing and defense selection?** Routing chooses venues/allocations. The original selector chooses a protection mechanism. Time splitting and pool splitting are also different dimensions.

**Does a private relay guarantee safety?** No. The local relay assumes no leakage and immediate builder injection. Real private infrastructure has trust and inclusion constraints.

**Can the Guardian always pause first?** No. It succeeds under the local visibility/reaction/fee-order model. On-chain budgets reject excessive supported borrowing directly during execution.

**Why simulate if ML is accurate?** ML is a fast approximation. Calibration and abstention help decide whether its estimate is usable; simulation checks supported actions against actual local state. Both still depend on their threat and state assumptions.

**What is the difference between an invariant and a unit test?** An invariant checks a property across generated sequences; a unit test checks a selected path. Both are needed, especially to establish non-vacuous successful behavior.

**What does mutation testing add?** It checks that the suite can notice a deliberately removed protection. A passing test suite without that sensitivity would be weaker evidence.

**Can a preview be copied and executed twice?** The server treats execution as one-shot and checks the original binding/state. Replayed, stale or altered previews are rejected. The wallet's public-testnet flow is a separate mechanism.

**Does matching eliminate all MEV?** No. It reduces pool interaction for matched amounts. Residual routing, solver pricing, leakage and inclusion can still matter.

**Is this a novel published algorithm?** The mechanisms have research and industry precedents. The project contribution is integrating, implementing and evaluating them in one explainable lab. Do not claim invention of TWAPs, batch auctions or conformal prediction.

**Which work came from where?** The original project supplies the attack lab and shadow-fork architecture. The friend's commit `157d173` adds the six extensions. This integration preserves the existing Sepolia work and adds upgrades 8/9/10, their tests and these notes. Explain team contributions honestly.

**What would you improve next?** Accounting-aware Guardian verdicts, historical incident datasets, and stronger oracle validity/freshness semantics. These remain distinct from the work completed here.
