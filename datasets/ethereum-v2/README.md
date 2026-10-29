# Ethereum V2 real-input sample

10,503 accepted pool swap observations, 9,981 transaction hashes, four pools and 7,059 source blocks, across seven sampling windows from March 2024 through October 2026. `swaps.csv` is the processed **observed-input** dataset. `swaps.manifest.json` contains source windows, rejection/policy counts, assumptions and SHA-256 digests. Derived trade-size stress cases are generated after chronological splitting; they are not additional observed trades in this CSV.

`label_kind=counterfactual_v2_economics`: harm/profitability targets are mathematical what-ifs, not independently observed attack labels. Only 118 rows have decoded router minimums; others name `slippage_source=declared_policy_scenario`. Pool reserves, amounts, gas price, timestamps and transaction/block identities are observed. `eth_price_usd` is a pool-implied reference; stablecoins are assumed at $1. Do not interpret this dataset as a labelled corpus of confirmed MEV attacks.

See [full methodology and limitations](../../docs/REAL-MARKET-ML.md). On-chain facts are identified by public block/transaction hashes. No third-party labelled dataset was copied; third-party code and API services retain their own terms. Raw provider caches are excluded from Git.
