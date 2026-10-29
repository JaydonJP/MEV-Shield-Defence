# MEV Shield research extensions

Six additional capabilities build on the original local Anvil world. They are course-project
implementations with explicit scope, not audited production contracts or claims of new mechanisms.
The existing attack and defense demos remain available.

## Start and explore

On the prepared Windows checkout:

```powershell
.venv\Scripts\python.exe -m mevshield.cli up
.venv\Scripts\python.exe -m mevshield.cli serve --no-open-browser
```

Open <http://127.0.0.1:8000/lab>. The original dashboard links to the research lab.
If the devnet is already running, skip `up`. If it predates these extensions, stop it using
`mevshield down`, then deploy again with `mevshield up`. Reset restores the seeded world.
The lab does not require training the ML model. Analytical replay works with the web server alone.

For a new machine, install Python 3.11–3.13 and Foundry, create `.venv`, and install `.[dev]`.
`config.find_foundry_bin` also detects portable binaries in `.tools/foundry/`, which is ignored by Git.
The prepared environment uses Python 3.12 and Foundry 1.8.6 with pinned Solidity 0.8.24.

## Commands

```powershell
.venv\Scripts\python.exe -m mevshield.cli extensions run routing --amount 10000
.venv\Scripts\python.exe -m mevshield.cli extensions run cashback --amount 1000
.venv\Scripts\python.exe -m mevshield.cli extensions run batch --amount 1000
.venv\Scripts\python.exe -m mevshield.cli extensions run firewall --amount 8000
.venv\Scripts\python.exe -m mevshield.cli extensions run coordinated --amount 10000
.venv\Scripts\python.exe -m mevshield.cli extensions scan
.venv\Scripts\python.exe -m mevshield.cli extensions run all
.venv\Scripts\python.exe -m mevshield.cli extensions replay --amount 10000 --liquidity 0.5 --gas 5 --save
.venv\Scripts\python.exe -m mevshield.cli extensions replay --replay-id <saved-id>
```

Individual settlement demos change the local chain. The coordinated demo only simulates the attack
and drops its pending attack transactions afterwards (its account-funding setup remains).
The `all` suite takes a fresh snapshot for each scenario and restores it even when an experiment fails.
Its receipts and hashes are historical experiment evidence; the corresponding blocks are reverted.
It writes `data/extensions-results.json` and records results in SQLite's `experiments` table.
Do not run independent CLI writers or an external Guardian simultaneously with isolated experiments.
Web requests are serialized, but that lock does not coordinate separate processes.

## 1. MEV-aware routing

`extensions/routing.py` evaluates five allocations: 0/100, 25/75, 50/50, 75/25 and 100/0 across the two
existing direct pools. The objective maximizes expected received-token value minus estimated gas.
Pool fees enter the exact integer AMM formula; they are not charged twice in the objective.
Both swap directions are supported by the planner and `MEVRouter`; the demo sells USDC.

The risk model computes a rational sandwich for each active leg and only includes its harm when
the attack exceeds its estimated gas cost. `MEVRouter` executes the allocation atomically and enforces
both per-leg limits and a global output limit. Repeated pools, mismatched pairs, zero legs and expired
orders are rejected. Per-leg limits make the attack model consistent with the submitted order.

The demo preflights a complete bundle on the shadow fork, then executes front-runs, the routed trade,
and backruns on the main devnet. It returns measured user output/gas and every candidate's quote.
The new demo constructs attacks on all chosen legs explicitly; the original generic sandwich bot
does not independently decode arbitrary multi-pool router calls.

**Limits:** two direct venues; discrete allocation search; independent per-leg adversaries and
estimated gas. No concentrated liquidity, arbitrary multi-hop graph, latency forecast or universal
MEV-loss guarantee. The reference valuation comes from the local pool and is an experiment convention.

## 2. Funded backrun auction and cashback

`CashbackAuction` escrows each searcher's USDC capital. The two demo searchers compute a backrun
against the post-user pool state, then bid 50% and 75% of its modeled gross profit as cashback.
The demo chooses the highest rebate. Actual settlement enforces profitability and the user limit.
An opportunity can include an existing pool-price discrepancy as well as the user's price impact;
the demo does not claim all arbitrage profit was newly created by that user.

Each bid binds to a hash of the auction address, chain ID, intended user, user pool, amount, minimum
output and deadline. Another user cannot take that rebate or alter the underlying order. Settlement
executes the user's swap first, followed by the two-pool backrun, cashback and searcher payout, all
in one transaction. If it fails, all changes revert. Searchers can cancel unused or expired bids to
recover capital; the demo releases losing bids and refunds the selected bid after a reverted settlement.

**Limits:** one pair, USDC-in user swaps, two fixed demo bidders, off-chain winner selection and
inventory-funded execution. This is not a Flashbots integration or a permissionless solver network.
Reported searcher profit is gross token profit, before bid/settlement gas; cashback is not guaranteed
to exceed the user's incremental gas. Atomic backrun placement does not hide the outer transaction
from a malicious builder. Users still rely on their output limit and the local builder assumptions.

## 3. Escrowed batch matching

`BatchSettlement` accepts owner-created orders with amount, minimum total output and deadline. Funds
are escrowed immediately, and each owner can cancel an unfilled order at any time. After a collection
window, a permissionless solver settles two opposing orders at one token1/token0 clearing price.
The matched amounts bypass the AMM; residuals use the fixed AMM. Both full-order minimums are checked,
and failure preserves the escrow. Order closure prevents replay and double settlement.

The default demo matches 75% of Alice's USDC with Bob's opposing WETH order. It compares received
amounts with standalone AMM quotes and reports gas including both placement transactions.

**Limits:** two fully consumed orders per settlement, one token pair, 18-decimal mock tokens,
and no optimization over competing solvers. A solver-chosen price must satisfy both users, but
that does not imply best execution or a fair auction. Residual AMM volume remains exposed to MEV.

## 4. On-chain protocol firewall

`FirewallLending` extends `SafeLending` with global outflow budgets. Defaults are 10,000 USDC per
block and 25,000 USDC over the most recent ten blocks. The rolling calculation includes the current
block and the previous nine; it is not a fixed bucket that resets at a boundary. The owner can set
budgets with a bounded 1–64 block window. Historic usage is retained when settings change.

Borrowing at more than half the allowed spot/TWAP deviation halves both budgets, before the existing
hard deviation guard rejects borrowing. Counters update atomically with successful borrowing.
Limits are shared across all accounts; splitting users or repaying does not replenish the budget.
An extension hook in `LendingBase.borrow` supports this without changing ordinary market behavior.

The demo submits two 8,000-USDC requests in one block: the first succeeds and the second exceeds
the 10,000-USDC cap. No off-chain Guardian response is needed to enforce it.

**Limits:** liquidity outflow limits do not establish solvency and do not prevent losses accumulated
over a long period. Legitimate traffic can exhaust budgets. It inherits the original `SafeLending`
oracle assumptions; the earlier recommended oracle hardening is a separate task.

## 5. Coordinated transaction detection

`extensions/coordinated.py` builds transaction nodes and shared-pool/token-delivery edges. It orders
pending transactions by local priority fee while respecting sender nonces. It tests at most 24
candidate front/victim/back triples from a maximum of 32 transactions.

The detector runs the victim alone and the candidate group on the shadow fork. It requires successful
execution, positive victim harm and actor profit above estimated gas before issuing a finding.
Different-sender activity can be `CRITICAL` when there is explicit token delivery between the actors;
a profitable pattern without that linkage is `SUSPICIOUS`. Sharing a pool alone is insufficient.

The demo front-run sends purchased WETH directly to a second account which submits the backrun.
The existing Guardian also calls the sequence detector for bounded pending sets, logs findings and
stores alerts. `extensions scan` analyzes pending transactions without mining or dropping them.

**Limits:** modeled sandwich triples, recognized local calls and a fee-ordering hypothesis. It does
not prove common ownership, support arbitrary multi-step protocol exploitation, or see private
orderflow. Batch/auction/multi-leg settlement calls are not expanded into general trace graphs.

## 6. Reproducible attack replay lab

`Scenario` controls trade size, liquidity, slippage, attacker capital, gas, attacker/Guardian priority
fees, Guardian delay and random seed. Every strategy shares the same seeded benign price move.
The replay compares public submission, adaptive slippage, an ideal private relay, and MEV routing.

The UI separates MEV harm, execution output, gas/fees and completion. A common starting quote makes
route price improvements visible. Failed swaps pay a retry penalty and are excluded from the winner.
The router refreshes its quote at inclusion; this assumption is displayed. Guardian ordering is a
separate analytical check and does not imply that the replay executed a pause transaction.

Saved scenarios are identified by a content hash and model version, written atomically and checked
on load. Replay IDs cannot traverse directories. The same saved scenario reproduces the same result
under its supported model version. Invalid records are omitted from the saved-scenario list.
The browser exports full result JSON, including assumptions. There is no external data dependency.

**Limits:** analytical integer-AMM replay with estimated gas, ideal private submission and a simple
benign-movement model. It is explicitly separate from the EVM-backed feature demos and makes no
mainnet accuracy claim. Replay scenarios are saved under ignored `data/replays/`.

## API

| Method | Path | Input / result |
|---|---|---|
| GET | `/lab` | Research interface |
| POST | `/api/extensions/{feature}` | `{ "amount": 1000 }`; omit amount for defaults |
| POST | `/api/extensions/all` | `{}`; isolated suite |
| POST | `/api/replay` | Scenario JSON; deterministic comparison |
| POST | `/api/replays` | Scenario JSON; persist immutable input |
| GET | `/api/replays` | Up to 100 valid saved scenarios |
| GET | `/api/replays/{id}` | Load, validate and replay |

Feature values are `routing`, `cashback`, `batch`, `firewall`, `coordinated`, and `all`.
Inputs reject unknown fields and out-of-range/nonfinite values. The server binds to loopback by
default and is intended for this local demonstration environment.

## Validation

```powershell
cd contracts
..\.tools\foundry\forge.exe test --summary   # or forge test --summary
cd ..
.venv\Scripts\python.exe -m pytest tests -q
$env:MEVSHIELD_INTEGRATION = '1'
.venv\Scripts\python.exe -m pytest tests/test_extensions_integration.py -q
```

Solidity checks cover settlement rollback, order/bid cancellation and expiry, intent binding,
escrow isolation, per-leg constraints, global/rolling budgets and repayment/account-splitting cases.
Two fuzz properties exercise input conservation and outflow caps. Python checks cover route ranking,
direction and rounding, replay determinism/tamper detection, API validation, nonce ordering, and real
EVM demonstrations. Integration tests restore snapshots after each scenario and include a benign
shared-pool sequence to check against false positives.
