# MEV Shield — Project Report

*Adaptive counterfactual defense against flash-loan and MEV attacks, demonstrated on a local Ethereum devnet.*

---

## 1. Problem

Maximal Extractable Value (MEV) and flash-loan attacks are the two dominant on-chain threats to users and
DeFi protocols. A **sandwich** lets a searcher who sees a pending swap buy before it and sell after,
pushing the victim to a worse price. A **flash-loan oracle manipulation** (bZx, Harvest) lets an attacker
borrow enormous capital for one transaction, distort an AMM's spot price, and drain a lending protocol
that trusts that price. Yet flash loans and arbitrage are *not* inherently malicious — benign arbitrage
keeps prices consistent across venues. A useful defense must therefore **separate harmful from useful**
activity, and act *before* the damage is irreversible.

This project builds **MEV Shield**: a system that *predicts* these attacks counterfactually and *prevents*
them automatically, demonstrated end-to-end on an isolated local devnet. It follows the research agenda of
*Flash Loan Arbitrage & MEV Defense System* — "Adaptive Counterfactual MEV Defense" and "Flash-Loan-Aware
Attack Synthesis" — scaled to a course project.

## 2. Approach

Every attack passes through the **mempool** and the **EVM**. MEV Shield sits at both and keeps a private
**shadow fork** of the chain (a second Anvil forked from the main devnet). For any transaction of interest
it runs the transaction — and a rational attacker's reaction to it — on the shadow fork first. The
simulated outcome is the prediction; the prevention is then chosen automatically.

### 2.1 Prediction, user side (sandwich)

For a swap intent the Risk Engine (`risk.py`):

1. executes the swap alone on the shadow fork → `out_benign`;
2. searches, with a golden-section search over the front-run size `f`, the **profit-maximising rational
   attacker** subject to (a) the victim's `minAmountOut` (a revert earns the attacker nothing) and (b)
   attacker profit > gas → `out_attacked`;
3. reports **predicted harm** `H = out_benign − out_attacked`, attacker profit and `P(attack)`;
4. repeats for leakage levels *public* and *hint* (pool + direction only, amount hidden);
5. cross-checks `H` against the **closed-form constant-product sandwich formula** (`amm_math.py`).

### 2.2 Prevention, user side

The Defense Selector (`defense.py`) scores each defense and picks the minimum of

> `J(d) = Harm_d + λ₁·GasCost_d + λ₂·Latency_d + λ₃·RevertRisk_d`.

| Defense | Mechanism |
|---|---|
| `public` | send normally |
| `adaptive-slippage` | set `minAmountOut` at the largest slippage `s*` where the best sandwich is unprofitable |
| `private-relay` | inject at block-build time, never visible in the public mempool |
| `commit-reveal` | commit `hash(order)` in block N, reveal in N+1; reveal must be the pool's first trade that block |
| `split` | break into `n` chunks across blocks |

### 2.3 Prediction + prevention, protocol side

The Guardian (`guardian.py`) replays each pending transaction on the shadow fork and extracts features:
flash-loan size vs pool liquidity, peak intra-tx spot-vs-TWAP deviation, protected-protocol value change,
sender profit, and whether a borrow was priced off a manipulated (non-TWAP) spot price. An **explainable
rule** decides: *flash loan ∧ deviation > 10 % ∧ protocol loss ∧ sender profit → CRITICAL*. Arbitrage has a
flash loan and profit but **no protocol loss** and converges prices → **BENIGN**. On CRITICAL the Responder
(`responder.py`) sends `pause()` at a higher priority fee, so it is ordered before the attack in the same
block and the attack reverts. `SafeLending` also resists by design (TWAP oracle + deviation guard).

### 2.4 Fast predictor

The shadow-fork simulation costs ~100s of ms. `predictor.py` trains a gradient-boosting model on a few
thousand simulated scenarios (trade size, liquidity, slippage, fee, gas) to predict harm in < 1 ms, used
for the dashboard's live mempool labels; the simulator remains the source of truth for decisions.

## 3. Implementation

Solidity 0.8.24 contracts (Foundry) for an own minimal AMM (`x·y=k`, Uniswap-V2 cumulative price), an
Aave-style flash lender, a TWAP oracle, spot-priced vs TWAP-priced lending markets, a protected router, a
commit-reveal swap and two DAOs; plus a test-harness of attacker contracts. The off-chain system is a
Python 3.12 package using `web3.py`, `typer`/`rich` (CLI), `scikit-learn`, `pandas`, and `streamlit`/
`plotly`. The main devnet runs `anvil --no-mining --order fees`; a custom block builder gives searchers a
window, injects relayed transactions and mines, so block ordering by priority fee drives the whole demo.
SQLite stores alerts, predictions, blocks and experiment results. **22 Forge tests** and **12 Python unit
tests** pass.

## 4. Evaluation

*(Numbers below are produced by `mevshield benchmark`; see `data/results.csv` and the dashboard. Values
are from a representative run — rerun to regenerate.)*

**Prediction quality**
- Risk Engine vs the closed-form sandwich formula: harm error **≈ 0.02 %** (target ≤ 1 %).
- ML fast predictor vs the simulator: **R² ≈ 0.99**, **MAE ≈ \$3–11**, ~**600–700× faster** (< 1 ms vs
  ~100s of ms); classifier F1 ≈ 0.999.
- Guardian on a mixed workload (benign swaps, borrows, arbitrage, oracle attacks): **precision 1.0,
  recall 1.0, false-positive rate 0.0**, **0 arbitrage false positives**, detection latency ~0.3 s.

**Prevention quality**
- Sandwich harm, summed over trade sizes \$5–\$20k: *no protection* ≫ *always-public* ≫ *adaptive selector
  (≈ \$0)* — a ~**100 % harm reduction** vs always-public, at only gas/latency cost.
- Selector behaviour is sensible: small swaps stay `public` (no over-protection); medium swaps pick
  `adaptive-slippage`; large swaps pick `private-relay`.
- Protocol: the same T2 attack **drains `VulnerableLending`** (→ \$0), is **blocked by the Guardian**
  (pause ordered first → revert, funds safe), and **fails by design against `SafeLending`**.
- Governance (T4): drains `VulnerableDAO`; **snapshot voting + timelock defeats it** (flash-borrowed
  tokens have no past balance, and the 1-block timelock blocks same-tx execution).

## 5. Honest limitations

- **Local devnet, single builder.** Real MEV involves competing builders/relays (PBS), reorgs and
  cross-domain MEV; none of that is modelled.
- **Rational-attacker assumption** is explicit: the Risk Engine assumes an attacker with unlimited
  flash-loan capital who maximises profit under a stated leakage model. Irrational or adversarially
  cost-insensitive actors are out of scope.
- **Simplified relay trust model.** The private relay trusts the operator/builder not to leak or front-run.
- **Own minimal AMM**, not real Uniswap; the economics are faithful (`x·y=k`) but liquidity and routing are
  simplified. No CEX/cross-chain MEV.
- **Guardian pre-execution response** relies on fee-ordering in one block; against a private-orderflow
  attacker it would need an equivalent trusted ordering channel.

## 6. Reproducing

```
mevshield up → arb → swap 500 --public → predict 500 → swap 500 --protect →
attack oracle --target vulnerable → (guardian start) → attack oracle --target safe →
train → benchmark → dashboard
```

See `README.md` for the full command chain and `docs/architecture.md` for the component map.
