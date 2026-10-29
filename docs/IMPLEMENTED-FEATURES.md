# What has been implemented

Updated 30 October 2026.

1. **Blockchain environment:** An Anvil Ethereum devnet with Solidity smart contracts and a separate shadow fork for isolated transaction testing.

2. **Attack simulations:** Controlled demonstrations of sandwich attacks, flash-loan oracle manipulation, benign flash-loan arbitrage, and flash-loan governance attacks.

3. **Hybrid risk engine:** ML estimates swap risk first. Uncertain predictions, large trades, close defense-cost rankings, or requests for additional validation trigger the shadow-fork simulator. Unavailable required verification stops the recommendation.

4. **Improved machine learning:** A reproducible pipeline uses 10,503 real Ethereum pool-swap inputs, explicitly declared policy scenarios, and separate synthetic lab cases. Histogram gradient boosting estimates financial harm, while an Extra Trees classifier estimates conditional attack profitability. Chronological testing, calibration, uncertainty intervals, and abstention are included.

5. **Automatic defense selection:** A shared cost model compares public submission, adaptive slippage, private relay, commit-reveal, and trade splitting using predicted loss, gas, fees, delay, and revert risk. Selected active protections are verified, costs are re-ranked if verification changes them, and stale decisions cannot be executed through the selector.

6. **Guardian security system:** Monitors pending transactions, detects harmful oracle manipulation, and can pause the vulnerable lending protocol before the attack executes in the controlled devnet.

7. **Web dashboard:** Interactive pages demonstrate attacks, predicted risks, selected defenses, transaction ordering, price changes, alerts, and realized outcomes. Hybrid and simulation-only modes can be selected in the attack lab.

8. **MEV-aware multi-pool routing:** Compares and executes supported routes across local pools using execution output, estimated MEV loss, and transaction costs.

9. **Backrun cashback auction:** Local searchers compete for supported backrun opportunities, with funded settlement returning a rebate to the user.

10. **Batch settlement and direct matching:** Matches supported opposite-direction orders and settles unmatched amounts through an AMM, subject to pricing and order constraints.

11. **On-chain lending firewall:** Enforces borrowing/outflow budgets and oracle-related operating limits directly in the lending contract to constrain damage.

12. **Coordinated attack detection:** Analyzes supported transaction sequences and shared effects, including suspicious activity spread across separate transactions or addresses.

13. **Interactive attack replay lab:** Saves reproducible scenarios and compares defense behavior as liquidity, trade size, attacker capital, slippage, gas, and response delay change.

14. **Transaction safety reports:** Previews supported local swaps and lending actions, including token/debt changes, allowances, estimated gas, and contract failures. One-shot previews are bound to state, and execution receipts are reconciled with predictions.

15. **Stateful contract verification:** Multi-user invariant tests check vault accounting, lending budgets, and settlement escrow. Mutation checks confirm that deliberately removed protections can be detected.

16. **Sepolia wallet integration:** Supports wallet connection and verified testnet-vault deployment, deposits, withdrawals, cooldowns, limits, and owner pause controls using wallet-signed Sepolia transactions.

17. **Read-only Ethereum mainnet assessment:** Reads four allowlisted Uniswap V2/SushiSwap pools and estimates conditional sandwich economics without signing transactions. Unsupported or uncertain ML inputs fall back to explicit V2 calculations.

18. **Comparative research evaluation:** Reproducible paired benchmarks compare hybrid, simulation-only, analytic, and unprotected execution from identical local states. Evidence records losses, reverts, realized user costs, recommendation agreement, latency, CPU time, and shadow-fork work.

**Scope:** Protection mechanisms and attack execution are demonstrated on the local devnet; wallet operations use Sepolia. Mainnet functionality is read-only. ML labels describe modeled economic vulnerability under declared assumptions, not independently confirmed attack occurrence. Benchmark results establish behavior on the measured local scenarios, not universal or mainnet safety.

See [hybrid design and evaluation](HYBRID-SELECTION.md), [real-context ML methodology](REAL-MARKET-ML.md), and [presentation notes for all features](PRESENTATION-NOTES.md).
