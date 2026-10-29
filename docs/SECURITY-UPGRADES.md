# Reliability upgrades 8, 9 and 10

The six trading/security extensions from `bhuvang2026in/MEV-Shield-Defence` commit `157d173` are merged with the existing dashboard and Sepolia vault. This change adds the following previously absent capabilities.

## 8: stateful verification

`contracts/test/Invariants.t.sol` has three multi-actor handler suites covering vault, lending and batch/auction state. Properties check accounting/backing, authorization and global outflow/escrow budgets. Successful-action regression paths prevent a suite of only rejected calls from appearing useful.

Run `python scripts/verify_security.py`. It runs the full Solidity suite with a saved seed and then checks two isolated source variants with intentionally removed protections. Only a failure in the expected regression counts as catching a mutant; compile errors do not. It retains variant sources under ignored `data/verification-work-*` and writes `data/verification.json`. `/api/verification` and `/safety` expose that record. Files in the working production contract tree are never mutated by the runner.

The default invariant configuration is 64 runs, depth 48. Foundry's counterexample cache and the seed make failures reproducible. Saved evidence includes a source/test/config fingerprint; the UI marks it stale after those inputs change. These campaigns are finite tests, not formal verification.

## 9: state-bound transaction reports

`SafetyReports` supports known local-router swaps and collateral deposit/borrow/repay/withdraw on `safe` and `firewall`, using only seeded user roles. The API rejects arbitrary calldata, targets, administrative users and approval actions.

- `POST /api/safety/preview`: typed action, pool, role, amount, market, token and slippage.
- `POST /api/safety/execute`: the returned report ID and SHA-256 transaction binding.
- `GET /api/safety/history`: the latest 30 persisted reports.

Preview executes on a disposable shadow snapshot and reports token/debt changes, current allowance/spender, minimum output/deadline, simulation gas and policy rejection. Integer token amounts are strings and wallet transaction integer fields are hex strings in API output.

Execution checks local chain ID, pending and relay queues, expiry, unused status, exact transaction fingerprint, sender nonce, latest block hash and recipient runtime hash. A successful receipt is reconciled against predicted token and lending deltas, with native gas recorded separately. The report expires in 120 seconds; a server restart discards executable in-memory previews while keeping SQLite history. The UI invalidates the execution option when its form changes and avoids rendering an outdated in-flight result.

The report simulates the action alone, not an adversarial bundle. It does not provide generic transaction safety or public-chain protection. Native gas differs because the shadow runner uses its own fee parameters. Do not run unrelated writers during a local experiment. Historical reports can refer to blocks later removed by snapshot reset. Sepolia still uses the separate unsigned preparation/browser-wallet path.

## 10: calibrated uncertainty and fallback

The existing synthetic AMM dataset now uses disjoint 60/20/20 training/calibration/test splits. The harm model gets a scaled split-conformal interval; classifier probabilities get logistic calibration. Cross-validation uses the training subset. Reported coverage and Brier score use the untouched test subset.

Support checks include all engineered features' observed ranges and known fee tiers. Uncalibrated legacy models, unsupported inputs, wide intervals or probabilities near the decision boundary cause abstention. `predict_with_fallback` accepts a simulator callback. The live engine supplies an actual shadow-fork `RiskEngine` assessment for the current pool. Missing/invalid fallback output becomes `unknown` with null harm/probability, never zero risk. Responses identify source, status and reasons; the ML estimate remains separately inspectable.

`POST /api/risk` assesses USDC-in pool-A intents from the current local state. `/safety` makes fallback visible. `mevshield train` prints interval coverage and calibration metrics; `start` migrates an old uncalibrated model by retraining. Legacy models can still be loaded and explicitly abstain until retrained.

Nominal 90% intervals concern synthetic exchangeable scenarios, not future attack incidence or an unconditional drift guarantee. There is no historical/mainnet accuracy claim. The simulator's profitability label is conditional on the lab attacker model.

## Integration resolutions

The imported firewall fixture now advances a block after depositing, preserving SafeLending's existing cooldown. API tests use the existing trusted local hostname and the merged API's HTTP error contract. The research lab's CSS supplies its missing variables and uses the retained dashboard's light palette. Existing Sepolia protections and the six imported extensions are retained.

Local deadlines now include pending-block timestamps (including Anvil time offsets) and wall time after idle periods. The shadow cache checks block identity as well as height, covering reset/replacement states at the same height.

For a presentation, see [all-feature notes](PRESENTATION-NOTES.md). For exact checked commands and results, see [validation](VALIDATION.md).
