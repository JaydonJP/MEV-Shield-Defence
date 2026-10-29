# Visual walkthrough and public-network integration

The website has two views: **Attack lab** and **Live blockchain**. Both use a white
theme, responsive layouts, keyboard-operable controls, live status messages, and reduced-motion support.
There are no external font or JavaScript dependencies.

## Attack lab

Start `mevshield start --build`. The local lab deploys real Solidity contracts on Anvil, and its private
shadow fork evaluates pending transactions. These experiments are intentionally separate from Sepolia.

Choose a scenario, toggle protection, and run. The journey highlights the wallet/flash lender, shield,
protocol and attacker. It explains the mempool (the waiting room for transactions), simulation,
defense, and execution. The block list displays the actual order and success/reversion of each transaction.
Each receipt is decoded for AMM swaps and reserve events, so token amounts and temporary price movements
are measured rather than fabricated for the animation. Reverted calls emit no surviving swap events.

The chart shows Pool A spot prices from reserve events plus block snapshots, alongside its time-weighted
average price. The horizontal axis is observation order; it is not a continuous time chart. The outcome
compares quoted and received WETH for swaps, or protocol liquidity before and after an oracle attack.
Loss figures exclude gas. Defense scores include modeled fees, delay and reversion risk.

**Guided / Fast / Instant** changes presentation pacing without changing chain behavior. **Replay story**
replays captured events without sending transactions. A new experiment captures a fresh baseline; results
from the most recent experiment remain available after a refresh via the WebSocket history. **Reset lab**
restores the clean deployment snapshot. Resetting cannot interrupt a running experiment.

Suggested demonstration:

1. Sandwich attack, 5,000 USDC, without shield: follow front-run → user trade → back-run and inspect the loss.
2. Reset; use the same amount with the shield. Compare the defense score table and realised outcome.
3. Oracle manipulation without protection: observe the lending liquidity loss.
4. Reset; enable Guardian, then try the hardened market. Explain priority ordering vs contract checks.
5. Reset; run benign arbitrage. A flash loan with no protocol harm should pass.
6. Reset between vulnerable and protected governance cases; compare instant voting with snapshots/timelock.

## Ethereum Sepolia

`mevshield serve --no-open-browser` is enough to browse the public network; Foundry and a local chain are
not required for this view. The default public RPC is configurable with `MEVSHIELD_SEPOLIA_RPC`. The backend
checks chain ID 11155111 and caches five recent blocks for ten seconds. The UI polls every twelve seconds,
displays observation times and explorer links, and reports failures without generating replacement data.

Public writes use the browser’s EIP-1193 wallet. The backend prepares only ShieldVault deployment, deposit,
withdrawal and owner pause/unpause calls. It does not hold keys, broadcast signed transactions or expose
an arbitrary RPC proxy. Amounts use integer wei strings; floating-point arithmetic never encodes ETH values.
Before a wallet prompt, the backend checks the chain, validates vault runtime bytecode, and estimates gas.
The wallet’s current chain and account are checked again immediately before submission.

Deployment requires a connected wallet funded with Sepolia test ETH and a user signature. No deployed
Sepolia contract address is shipped or invented. Once the user deploys a vault, its confirmed address is
remembered locally. Another viewer can attach that address after bytecode verification. The deployment
artifact is packaged in `mevshield/web/static/shield-vault.json`; re-export it after recompilation with
`python scripts/export_vault.py`, or use the CLI’s `--build` flag.

The transaction journey shows policy check → wallet signature → network inclusion → receipt. Known policy
failures appear before submission. If a submitted transaction reverts, the receipt reports the failure and
gas consumed. A pending hash is retained in browser storage and rechecked after reload. A receipt proves
inclusion, not finality; the explorer is provided for independent checking. If a wallet replaces/cancels a
transaction, follow the replacement in that wallet or explorer; this app does not resolve replacement hashes.
Use **Stop tracking this hash** to clear a stale local pending record. This does not cancel an on-chain
transaction; the explorer link is retained so its eventual outcome can still be checked.

## Contract protections

| Contract | Enforced defenses | Observable evidence |
| --- | --- | --- |
| SafeLending (local lab) | TWAP, deviation guard, earlier-block deposit, 100,000 USDC aggregate borrow cap per block | Borrow success/reversion; protocol liquidity |
| ShieldVault (wallet deployable on Sepolia) | Reentrancy lock; effects before external ETH transfer; 60-second deposit cooldown; 0.005 ETH per-wallet hourly budget; owner-only pause | `Deposited`, `Withdrawn`, `PauseChanged` receipts; current contract state |

ShieldVault is an educational test-ETH contract, not an upgrade to the local lending markets. Its first
successful withdrawal starts a one-hour window; later withdrawals share the budget until that window
expires. Depositing restarts that wallet’s cooldown. Pause blocks deposits and withdrawals until the owner
unpauses; it is centralized authority for this demonstration. There is no owner function to seize balances.

The local private relay is not a Flashbots integration. Anvil’s controllable ordering is not a guarantee
that a public validator will put a Guardian pause before an attack. Public blocks are not automatically
classified as malicious. Synthetic ML predictions are limited to the lab’s training distribution.

## Verification

`forge test` covers the existing attack scenarios plus cooldown, pause access, aggregate rate limits,
reentrancy attempts, and fuzzed over-withdrawals. `python -m pytest tests -q` adds API validation,
cross-origin/host rejection, operation concurrency, history preservation, chain-ID rejection and RPC-cache
behavior. Its ephemeral local Anvil adapter tests exercise deployment bytecode, actual unsigned
transaction execution, decoded receipts, vault identity checks and preflight policy failures.

Public RPC reads can be verified without a wallet. Public deployment and signing must be completed in
the user’s Ethereum wallet; an embedded browser without wallet extensions cannot perform that step.

If no wallet provider is detected, **Connect wallet** opens a setup dialog immediately. It includes
official MetaMask installation and wallet-creation links, a copyable URL for opening the app in the
same browser as the extension, and Sepolia faucet resources. Connecting itself requires no ETH;
deployment and other transactions require test ETH for gas. Wallet credentials are never entered
into this website. Announced EIP-6963 providers and injected EIP-1193 providers are supported, and
declined or already-pending extension requests get actionable messages.

Run `node --test tests/wallet-connection.test.cjs` for connection tests using mock providers. These
cover an empty wallet balance (no balance call is needed), network switching, unknown-network setup,
account access, and request failures. An actual extension connection and signature still require the
user’s own browser wallet.
