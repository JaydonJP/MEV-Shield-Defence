# MEV Shield — slides outline (~12 slides, ~10 min)

1. **Title** — MEV Shield: predict & prevent flash-loan and MEV attacks. Team + roles.
2. **The problem** — sandwiches rob users; flash-loan oracle manipulation drains protocols (bZx/Harvest).
   But flash loans + arbitrage are *useful* — a defense must tell harmful from benign.
3. **Key idea** — every attack passes through the mempool and the EVM. Sit at both; keep a private
   **shadow fork**; simulate *counterfactually* before anything executes. Prediction = simulated harm;
   prevention = chosen automatically.
4. **Threat model** — T1 sandwich (user), T2 flash-loan oracle manipulation (protocol), T3 arbitrage
   (benign — false-positive test), T4 governance (stretch).
5. **Architecture** — diagram (`docs/architecture.md`): main devnet (`--no-mining --order fees`) + shadow
   fork; attacker bots; Risk Engine + Defense Selector; Guardian + Responder; SQLite → dashboard.
6. **Predict, user side** — rational-attacker search for the worst sandwich on the shadow fork; harm =
   `out_benign − out_attacked`; closed-form cross-check (error ≈ 0.02 %). *Live:* `mevshield predict 500`.
7. **Prevent, user side** — selector `J = harm + λ·gas + λ·latency + λ·revert`; adaptive slippage `s*`.
   *Live:* `swap 500 --public` (loss shown) → `swap 500 --protect` (loss ≈ 0); `predict 5` → "public is fine".
8. **Predict, protocol side** — Guardian replays pending tx, extracts features (flash loan, deviation vs
   TWAP, protocol loss, profit), explainable rule. Arbitrage → BENIGN. *Live:* `mevshield arb`.
9. **Prevent, protocol side** — Responder `pause()` ordered before the attack (higher tip) → revert;
   SafeLending immune by design. *Live:* attack vulnerable (drained) → guardian start → attack (blocked) →
   attack safe (fails).
10. **Fast predictor + dashboard** — ML harm model < 1 ms, R² ≈ 0.99; Streamlit live mempool labels.
11. **Evidence** — `mevshield benchmark`: detection P/R/FPR = 1.0/1.0/0.0, 0 arb false positives;
    harm no-protection vs public vs adaptive (~100 % reduction); regret vs baselines.
12. **Limitations & takeaways** — local devnet, single builder, rational-attacker assumption, simplified
    relay trust. Takeaway: counterfactual, pre-execution defense catches both user- and protocol-side
    attacks while leaving benign MEV untouched.
