# Real-context ML upgrade — 30 October 2026

The shipped model uses **10,503 Ethereum mainnet pool swap inputs** from **9,981 transactions**, four verified Uniswap V2/SushiSwap pairs and 7,059 blocks, sampled in seven windows from **20 March 2024 to 29 October 2026**. It is usable in the local lab and in a **read-only mainnet assessment** at `/safety`.

## What the model predicts

The target is **economic vulnerability to a profitable constant-product sandwich under declared assumptions**, and its resulting token-value loss. It is **not** the probability that a real attacker will actually appear. We do not have independently verified observed sandwich/benign labels for these transactions. Mathematical labels must not be described as observed attacks or production detection accuracy.

Real observations: pool address, transaction/block hashes, timestamp, token input/output, reconstructed pre-swap reserves, executed gas price and pool-implied ETH price. Sync/Swap events are checked against exact 30-bps V2 arithmetic. Complex repeated swaps within a pool, nonstandard transfers/fees, inconsistent state and unsupported slippage are excluded. The normalized output valuation uses the pre-swap pool spot price, not an independent oracle. ETH-in and stablecoin-in observations are supported in training; live quotes currently accept stablecoin → WETH only.

Only **118** inputs have a direct supported router call whose user minimum could be decoded. For the other **10,385**, slippage is an explicitly declared policy drawn deterministically from 0.1%, 0.5%, 1%, 2%, 5%, never presented as actual calldata. The observed inputs contain only **8** profitable cases under the specified threat model. This is too imbalanced to support a meaningful attack-recall claim from ordinary traffic alone.

To test meaningful vulnerability, each real input also supplies four **declared trade-size policies**: a reproducible log-uniform sample in each input/reserve-ratio band 0.001–0.1%, 0.1–0.3%, 0.3–1%, and 1–5%. Reserves, gas and ETH price remain real. These policy cases are **not additional observed trades**. There are also 6,000 separately identified synthetic lab cases, of which 4,200 enter fitting, 900 calibrate intervals and 900 remain a separate lab test.

Fixed economic assumptions: 30-bps real pools, attacker capital capped at $5m, **420,000 attacker gas units** from the lab configuration, stablecoins valued at $1, front/victim/back ordering, no builder competition, flash-loan fee or independent market-price adjustment. Actual gas use, available capital, ordering, private flow, other venues and depegs can change outcomes. The configured gas budget is not an empirical mainnet estimate. Historical victim gas price is used as the attacker gas-price proxy; live assessment uses the RPC gas-price recommendation. Neither includes separately observed builder bids. These are research risk estimates, not a mainnet execution guarantee.

## Architecture and validation

- Thirteen scale-aware features include trade/liquidity ratio, price impact, slippage value versus gas, capital/liquidity ratio and the previously missing ETH-price input. No sender, pool identity, transaction hash, timestamp, future state, realized output or target value enters the learner.
- Harm learns a log-transformed fraction of trade value, clipped to a physical loss bound. Four regressor candidates and two classifiers are compared using validation only. The winner is histogram gradient boosting for harm and Extra Trees for profitability.
- Source blocks split chronologically **60/15/10/15** into training/validation/calibration/test, with a purged boundary block. All swaps and derived policy cases from a block stay together. Policy expansion happens after splitting. Descendants cannot leak between partitions.
- Probability scores receive logistic calibration. Scaled residuals produce a nominal 90% harm interval. Market and synthetic-lab interval calibration/support are separate. Temporal market drift and correlated policy cases mean this is not a distribution-free future-coverage guarantee.
- The candidate was developed using validation only and then evaluated once on the frozen test split. Mainnet promotion gates, set before final evaluation: balanced accuracy ≥90%, recall ≥95%, harm R² ≥0.95, interval coverage ≥85%. Lab qualification additionally requires balanced accuracy ≥90%, recall ≥95%, R² ≥0.95. Both passed.

| Partition | Observed source inputs | Observed + policy cases | Block range |
|---|---:|---:|---|
| train | 6,752 | 33,760 | 19474502–26274409 |
| validation | 1,514 | 7,570 | 26274427–26283615 |
| calibration | 850 | 4,250 | 26283619–26290202 |
| test | 1,384 | 6,920 | 26290208–26300292 |

Three boundary blocks remove three source rows. The model fit also includes the 4,200 synthetic lab cases.

## Frozen holdout results

**The first column evaluates counterfactual labels on held-out real contexts, with observed and declared policy trade sizes. It is not observed-attack accuracy.**

| Metric | Real-context policy/traffic test | Independent synthetic lab test |
|---|---:|---:|
| Cases | 6,920 | 900 |
| Accuracy | 99.90% | 98.56% |
| Balanced accuracy | 99.91% | 97.91% |
| Profitable-case recall | 99.96% | 96.30% |
| Precision | 99.78% | 98.86% |
| ROC-AUC | 0.999997 | 0.999212 |
| Harm MAE | $3.23 | $14.44 |
| Harm RMSE | $29.36 | $100.52 |
| Harm R² | 0.999802 | 0.980679 |

Real-context majority-class baseline: **61.24% accuracy**. Confusion matrix: TN 4,232, FP 6, FN 1, TP 2,681. Nominal 90% interval achieved **89.15%** coverage, mean width **$7.55**. A shuffled-training-label control scored **0.522 ROC-AUC**, close to chance. Whole-block bootstrap 95% ranges: recall **99.89–100%**, harm MAE **$2.45–$3.99**. These intervals describe the sampled policy benchmark, not all Ethereum activity.

The separate **1,384 observed-size test inputs have zero positive labels**. An all-negative baseline gets 100% accuracy there. Recall, balanced accuracy, ROC-AUC and harm R² are therefore reported as undefined, rather than used as evidence of detection quality. Slippage is still a declared policy where undecodable. Wider observed-positive coverage and independent labels are required before claiming observed mainnet attack detection.

One-row harm prediction median: **8.04 ms**; exact V2 calculation: **0.92 ms**, measured on this machine. The ML timing covers the harm regressor only; a full calibrated assessment also evaluates classification, support and intervals. Do not claim the model is faster than direct V2 arithmetic. The EVM shadow-fork workflow performs broader contract checks and is a different workload.

## Run it

The trusted compressed model and model card ship inside `mevshield/ml/artifacts/`, including in wheels. The processed input CSV and checksum/source manifest ship in `datasets/ethereum-v2/`. Loading the bundled artifact verifies its checksum. Scikit-learn is pinned to the training version, 1.9.1. Raw RPC caches remain local and ignored. Only load pickle artifacts from trusted sources.

```powershell
# Read real mainnet reserves, never signs or broadcasts:
.venv\Scripts\python.exe -m mevshield.cli ml quote 10000 --slippage 0.01

# Reproduce on the committed dataset (does not need RPC):
.venv\Scripts\python.exe -m mevshield.cli ml train --dataset datasets/ethereum-v2/swaps.csv

# Publish only after the gates pass:
.venv\Scripts\python.exe -m mevshield.cli ml train --dataset datasets/ethereum-v2/swaps.csv --publish

# Collect a new, distinct window into a new directory; inspect manifest and coverage:
.venv\Scripts\python.exe -m mevshield.cli ml collect --blocks 3000 --directory data/historical/new-window

# Combine collected source windows and retrain:
.venv\Scripts\python.exe -m mevshield.cli ml train --publish

# Explicit legacy synthetic-only training, if needed for comparison:
.venv\Scripts\python.exe -m mevshield.cli train --synthetic --samples 4000
```

`mevshield train` now defaults to the real-context pipeline. It uses collected source windows when available and the committed dataset otherwise. Checksum mismatches and overlapping source swap IDs stop training. Preserve untouched future windows for subsequent candidate testing; do not repeatedly tune on the committed test set and then call it independent. Updating data requires a deliberate collection/training/promotion run, not an automatic promise of continuous learning.

APIs: `GET /api/model` exposes metrics and supported pools; `POST /api/mainnet/risk` accepts `amount`, fractional `slippage`, and an allowlisted `pool`; `/api/risk` remains the local lab endpoint. Mainnet requires no devnet or wallet. Live pool token order, factory, chain ID, reserves and recent block timestamp are validated. Pool reads share a pinned block. Current gas is read separately. The response includes block/hash, inputs, assumptions, source and abstention reasons.

Out-of-support inputs, an unlearned fee, a wide interval or an uncertain probability cause abstention. Local assessments then use the actual shadow fork; mainnet assessments use explicit constant-product economics. If required live reads fail, the API returns unavailable rather than inventing a safe verdict. `p_attack` remains a compatibility alias for the calibrated **conditional profitability score**; prefer `p_profitable`. A zero score does not prove a transaction safe.

## Sources and reproducibility

Collection used public, read-only JSON-RPC: dRPC for small historical log windows, PublicNode for transaction/header lookups. The manifest records seven source ranges, rejection counts, policy assumptions and SHA-256 digests. The data is reproducible from those public blocks; public provider limits and availability can change.

- [Ethereum JSON-RPC documentation](https://ethereum.org/en/developers/docs/apis/json-rpc/) — transactions, logs, block metadata and eth_call.
- [Uniswap V2 pair implementation](https://github.com/Uniswap/v2-core/blob/master/contracts/UniswapV2Pair.sol) — reserve updates, Sync/Swap events and swap invariant.
- [Uniswap V2 library](https://github.com/Uniswap/v2-periphery/blob/master/contracts/libraries/UniswapV2Library.sol) — constant-product quotation and fee arithmetic.
- [Flash Boys 2.0](https://arxiv.org/abs/1904.05234) — ordering competition and MEV; supports why simplified economic risk must be distinguished from actual ordering outcomes.
- [A Gentle Introduction to Conformal Prediction](https://arxiv.org/abs/2107.07511) — calibration intervals and the limits of coverage under distribution shift.

On-chain source facts are identified by public transaction/block hashes; generated annotations retain the explicit counterfactual label distinction. No third-party labelled dataset was copied. Third-party software and hosted API services retain their respective terms.
