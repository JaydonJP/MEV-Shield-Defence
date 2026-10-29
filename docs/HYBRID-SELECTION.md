# Hybrid risk assessment and defense selection

Implemented 30 October 2026.

**Research question:** Can ML-assisted defense selection provide comparable protection to simulation-only selection while reducing decision latency and shadow-fork work?

## Decision flow

1. Read the current local pool, fee, gas reference, attacker inventory and intent. Bind the decision to the block hash, pool reserves, fee, gas price/reference, inventory and complete intent.
2. The real-context model estimates public sandwich harm and conditional profitability using its separately calibrated **lab** domain. Its harm target already accounts for modeled profitability: the selector does **not** multiply harm by the classifier score a second time.
3. If ML abstains, fails, gives invalid values/intervals, or mismatches capital assumptions, run the actual shadow-fork risk assessment. Large/high-impact/high-potential-loss cases and explicit verification requests also require it. If required simulation fails or is unavailable, stop; never relabel analytic arithmetic as a successful shadow simulation.
4. Compare all five defenses using the same objective: harm + weighted gas/fees + weighted waiting time + expected retry cost. ML supplies public risk; adaptive/split candidates still use their explicit constant-product screening models. Private relay and commit-reveal retain the local harness's stated trust/ordering assumptions. This is not a learned model of all defense outcomes.
5. If the leading cost cannot be distinguished from alternatives using the public-harm interval and the configured ranking margin, obtain a shadow-fork report and recompute the ranking. Identical public/adaptive calldata counts as one outcome rather than an ambiguous competing defense.
6. A selected tighter output limit is checked against a shadow-fork attack search. A selected split is checked as a sequential multi-block EVM scenario with bounded rational attack plans and carried pool/inventory state. If verification changes harm, update costs and re-rank. Manual overrides receive their own required active-defense checks.
7. Immediately before execution, check state/intent binding again. Changed state requires a new decision. In this controlled local workflow, independent writers must not modify the chain concurrently.

ML-only reports leave attack trace, attacker profit, attacked token output and unused hint/closed-form fields **null**. They do not fabricate a simulated trace or present uncomputed quantities as zero.

## Fixed verification policy

These defaults were fixed before the paired benchmark, rather than tuned on its results:

| Trigger | Default |
|---|---|
| Trade value requiring verification | At least $25,000 |
| Input/reserve ratio requiring verification | At least 10% |
| ML harm-interval upper bound requiring verification | At least $250 |
| Cost-ranking verification margin | $0.10 |
| Additional checks | Model support/calibration/interval acceptance; actual inventory vs model cap; user balance/allowance; explicit validation request |

`HybridPolicy` is configurable in Python; the command/UI use these defaults. Conservative fallback is expected to reduce the speedup for difficult workloads. Calibrated intervals can fail under distribution shift; checking a cost interval does not turn its nominal coverage into a universal safety guarantee.

## Interfaces

```powershell
# Hybrid is the default for selector-driven local previews and protected swaps:
.venv\Scripts\python.exe -m mevshield.cli predict 1000 --mode hybrid
.venv\Scripts\python.exe -m mevshield.cli swap 1000 --protect --mode hybrid

# Force additional risk validation:
.venv\Scripts\python.exe -m mevshield.cli predict 1000 --mode hybrid --verify

# Simulation-only reference and explicitly analytic baseline:
.venv\Scripts\python.exe -m mevshield.cli predict 1000 --mode sim
.venv\Scripts\python.exe -m mevshield.cli predict 1000 --mode fast

# Eight-case smoke comparison; expanded 20-case workload with two repeats:
.venv\Scripts\python.exe -m mevshield.cli benchmark-hybrid --quick
.venv\Scripts\python.exe -m mevshield.cli benchmark-hybrid --no-quick --repeats 2
```

The attack-lab mode selector exposes hybrid and simulation-only choices. `POST /api/predict?amount=1000&mode=hybrid&verify=false` returns the recommendation, costs and decision evidence. Protected `/api/sandwich` accepts the same mode choice. Evidence identifies risk source, requested mode, fallback reasons, whether risk/active defense was verified, timing, shadow bundle/transaction counts, and state binding. `/safety` and `GET /api/hybrid/benchmark` show the latest comparison; a source fingerprint marks evidence stale after relevant code/model changes.

Python: `RiskEngine(chain, shadow, predictor=model, policy=HybridPolicy())`, `assess(intent, mode="hybrid")`, `DefenseSelector(...).evaluate(intent, mode="hybrid", require_verification=False)`, then the selector's `execute(builder, selection)` method. Direct `execute(...)` remains a low-level harness operation and does not provide decision-state checks by itself.

The legacy `RiskReport.sim_ms` field retains total risk-assessment duration for compatibility; use decision `shadow_ms` and bundle counts to distinguish actual EVM work. The separate `/api/risk` form remains a risk-only ML/uncertainty demonstration. Mainnet assessment remains read-only V2 economics; hybrid automatic defense execution is limited to the known local devnet contracts. No mainnet signing, relay submission or autonomous real-fund protection was added.

## Evaluation design

The benchmark compares simulation-only, hybrid and analytic selection, plus an unprotected control. Every mode begins with the same saved block/state and faces the same rational sandwich bot. Alternating hybrid/simulation order reduces a simple order/cache bias. One explicit model warm-up and model load occur outside timed decisions and are reported separately. Shadow synchronization is warmed consistently before each mode.

Decision timing includes context reads, model prediction, candidate arithmetic, required verification and re-ranking. The recorded timing covers automatic selection, rather than subsequent manual-override checks. Prediction latency is measured separately rather than priced into J; its latency term concerns the protection mechanism’s extra blocks. It excludes user-transaction execution and the bot's execution-time validation in **all** modes. Client process CPU excludes Anvil CPU; shadow bundle/transaction counts are computational-work proxies, not measured cloud cost or dollars.

The benchmark executes each final choice against the attacker and records actual token loss, gas from receipts, relay fee, extra blocks, reverts and realized user cost. A reverted trade is reported separately and is not counted as successful protection merely because its token loss is zero. Comparison uses a predeclared $1 per-trade harm margin and no extra hybrid reverts; this descriptive check is not a statistical non-inferiority proof. Recommendation agreement is reported separately from protection and realized cost.

All decision modes share adaptive-limit handling, bounded attacker inventory, sequential split state and exact remainder allocation. Simulation-only checks the public scenario and the adaptive candidate before final ranking. Hybrid can skip the public search when ML is acceptable and verifies an active candidate when it may be selected. All required checks are included in the reported decision work.

Receipts are actual local EVM executions within isolated snapshots, then reverted to restore the initial world. They are not permanently canonical receipts or mainnet observations. Run this benchmark without concurrent CLI/dashboard chain actions or a separate Guardian process.

## Limits

The workload varies trade size, slippage, swap direction and pool over one local world state. It preserves the current world rather than resetting it to a fresh deployment; the recorded per-row starting-state metadata is authoritative, and results can change after a reset or on another machine. It does not establish performance across arbitrary contracts, liquidity regimes, production gas bidding, relay failures, adversarial model evasion or mainnet private order flow. The trained model's counterfactual labels remain distinct from confirmed historical attack labels. A faster analytic V2 control is included explicitly: this study does not claim ML beats exact constant-product arithmetic, or that the observed local comparison proves general production safety.

For model training/provenance see [REAL-MARKET-ML.md](REAL-MARKET-ML.md). For a concise current feature list see [IMPLEMENTED-FEATURES.md](IMPLEMENTED-FEATURES.md).

## Measured result — 30 October 2026

Expanded workload: **20 scenarios × 2 repeats**, 40 decisions/executions per mode and **160 actual local EVM executions** including the unprotected control. Every paired starting-state hash matched. The final code/model-card source fingerprint matched the saved evidence.

| Measurement | Simulation only | Hybrid | Analytic control |
|---|---:|---:|---:|
| Decisions | 40 | 40 | 40 |
| Total decision time | 178.50 s | 56.33 s | 7.56 s |
| Median decision latency | 4657 ms | 365 ms | 187 ms |
| p95 decision latency | 6158 ms | 3726 ms | 231 ms |
| Client CPU time | 120.81 s | 45.61 s | 6.75 s |
| Shadow scenarios/bundle calls | 1296 | 324 | 0 |
| Shadow transactions | 3600 | 900 | 0 |
| Realized protected token loss | $0 | $0 | $0 |
| Reverted user swaps | 0 | 0 | 0 |
| Total realized user cost | $44.11 | $44.11 | $44.11 |

Hybrid reduced **total** decision time by **68.44%**, shadow bundle calls/transactions by **75%**, and measured client CPU by **62.25%**. It agreed with the simulation-only recommendation in all 40 comparisons. Twenty-four hybrid decisions needed no shadow call, eight required public-risk fallback, and the remaining eight used selected active-defense verification. No paired increase in realized token loss, user cost or reverts was observed.

The unprotected control lost **$5,472.04** over the same 40 scenario instances. These are cumulative counterfactual local losses, not recovered mainnet funds. Only public, adaptive slippage and private relay were selected in this workload; split verification is covered separately by an EVM integration test, and commit-reveal remains an existing tested mechanism rather than a chosen policy in this sample.

One-time model loading took **5.77 s**, followed by **108 ms** warm-up, excluded from all warm decision timing. The cold-start cost matters for one-off use; the dashboard retains the loaded model across decisions. Results depend on hardware/load and should not be treated as a latency service-level guarantee.

**Conclusion limited to this experiment:** hybrid preserved the observed protection of simulation-only selection while reducing its decision work. The analytic control was faster and also matched protection in this narrow V2 sample. This does not prove a general advantage of ML over exact arithmetic, nor production/mainnet safety or statistical non-inferiority.

Full per-decision costs, verification reasons, timings, state bindings and reverted-snapshot receipt records: [saved evaluation evidence](evidence/hybrid-benchmark-2026-10-30.json).
