# MEV Shield — architecture

```
                         ┌──────────────────────── Anvil main devnet ─────────────────────────┐
  users / CLI ──public──►│  mempool (--no-mining, --order fees)  ────────────────►  block      │
        │                │  Contracts: Token×3 · SimpleAMM×2 · FlashLender · TWAPOracle ·       │
        │  private-relay │             Vulnerable/SafeLending · ProtectedRouter ·              │
        └───────────────►│             CommitRevealSwap · Vulnerable/SnapshotDAO               │
                         │  (relay injects tx at block-build time, skipping the public pool)   │
                         └──────▲─────────────────────────────▲──────────────────────▲─────────┘
                                │ txpool_content              │ txpool_content        │ pause() w/ higher tip
                        ┌───────┴────────┐            ┌────────┴────────┐      ┌───────┴────────┐
                        │ Attacker bots  │            │    Guardian     │─────►│   Responder    │
                        │ (test harness) │            │  detect T1/T2   │      └────────────────┘
                        │ sandwich·oracle│            └───────┬─────────┘
                        │ ·arb·gov       │                    │ replay pending tx (snapshot/revert)
                        └────────────────┘                    ▼
  user swap ─► Risk Engine ─► Defense Selector     ┌────────────────────────────┐
                 │  counterfactual sims            │ Shadow fork (anvil --fork) │
                 └────────────────────────────────►│  cross-process file lock   │
                                                   └────────────────────────────┘
                                 all events ──► SQLite (store.py) + WebSocket ──► web dashboard (FastAPI)
```

## Components (file → role)

| File | Role |
|---|---|
| `devnet.py` | start/stop main Anvil (`--no-mining --order fees`) + shadow fork (`--fork-url … --order fifo`); only stops PIDs it recorded |
| `chain.py` | web3 connection, ABIs, accounts, EIP-1559 signing, snapshot/revert, mining control, txpool |
| `deploy.py` | deploy + seed the world; save `deployments.json` + a revert snapshot |
| `shadow.py` | re-fork to latest block; run a bundle in one block in a given order; revert; cross-process lock |
| `builder.py` | build a block = searcher window → inject private-relay txs → mine → summarise & store |
| `amm_math.py` | exact integer AMM math + closed-form sandwich / oracle / arbitrage formulas |
| `risk.py` | counterfactual Risk Engine (user side), leakage levels, closed-form cross-check |
| `defense.py` | defenses + selector `J(d)`; executes the chosen defense |
| `relay.py` | private relay queue |
| `guardian.py` | mempool watcher → shadow-fork replay → feature extraction → explainable score → Responder |
| `predictor.py` | ML fast predictor (gradient boosting) train/load/predict |
| `benchmark.py` | defense sweep, guardian evaluation, predictor evaluation → `results.csv` + SQLite |
| `harness/` | test-harness attackers (sandwich bot, oracle/arb/governance) + benign traffic |
| `store.py` | SQLite for alerts, predictions, blocks, swaps, relay queue, experiments |
| `cli.py` / `demo.py` | the `mevshield` command chain |

## Ordering — why the attacks and defenses land where they do

The main devnet runs with `--no-mining --order fees`. Transactions wait in the mempool; the block builder
mines them ordered by **priority fee** (highest first). This is what makes the demo possible:

- **Sandwich**: bot sends front-run at `victim_tip + 1 gwei` and back-run at `victim_tip / 2` →
  block order `[front-run, victim, back-run]`.
- **Guardian circuit-breaker**: Responder sends `pause()` at `attack_tip + 5 gwei` → ordered *before* the
  attack → the attack hits a paused protocol and reverts.
- **Private relay**: injected at seal time, so it never appears in `txpool_content` the bot watches.
