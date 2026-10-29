# Integration validation

Validated locally on **29 October 2026**, using Python 3.12, Solidity 0.8.24 and the installed Foundry tools. The friend's extension commit `157d173` is included in the Git history, together with the preserved Sepolia/dashboard work.

## Results

| Check | Result | What it establishes |
|---|---|---|
| Full Python suite, with local integration enabled | **85 passed, 0 skipped** | Math/model checks, typed API boundaries, wallet adapter preparation, six extension EVM integrations, preview/lending/receipt checks, stale state and cache handling |
| Solidity examples, fuzz properties and stateful suites | **Passed** | Foundry summary reports 56 passing entries, grouping related invariant properties per suite |
| Vault cooldown guard-removal variant | **Detected** | Expected successful/rejected-action regression fails when the cooldown is removed |
| Global borrowing-budget guard-removal variant | **Detected** | Expected multi-user budget regression fails when the shared per-block guard is removed |
| Wallet connection JavaScript tests | **8 passed** | Provider discovery, account/network handling, rejected/pending wallet requests |
| Browser verification | **Passed for exercised workflows** | Preview rendering, exact local swap execution and receipt match, input-edit invalidation, visible simulator fallback, extension page and offline replay rendering |
| JS syntax and Git whitespace checks | **Passed** | New scripts parse and the change has no patch whitespace errors |

The stateful runner uses seed `0x5eed`, 64 runs and depth 48. `data/verification.json` retains its output and source fingerprint. The UI marks that evidence stale when the Solidity source/test/config fingerprint changes. Two deliberately weakened source copies remain under ignored `data/verification-work-*` for inspection; production contract files were not modified during mutation checking.

## Commands

With deployed local main/shadow nodes:

```powershell
$env:MEVSHIELD_INTEGRATION = "1"
$env:OMP_NUM_THREADS = "2"
$env:OPENBLAS_NUM_THREADS = "2"
$env:MKL_NUM_THREADS = "2"
.venv\Scripts\python.exe -m pytest tests -q --basetemp data/pytest-new-unique-directory
.venv\Scripts\python.exe scripts/verify_security.py
node --test tests/wallet-connection.test.cjs
node --check mevshield/web/static/safety.js
git diff --check
```

Use a new `--basetemp` path per invocation on this Windows host because its system pytest temporary directory was inaccessible. Without `MEVSHIELD_INTEGRATION=1`, the integration cases intentionally skip; that is not equivalent to the full result above. Limiting numeric-library threads is an optional local performance setting.

## Model example

An explicit 4,000-sample, seed-0 training run produced harm R² **0.9723**, classifier F1 **0.972**, ROC-AUC **0.9976**, Brier score **0.0163**, and **88.8%** held-out coverage for a nominal 90% interval, with 800 calibration and 800 test samples. These are synthetic AMM measurements. Neither these values nor the separate independent synthetic-seed coverage check establish historical/mainnet performance.

## Integration corrections covered

- The imported firewall tests now respect the retained collateral cooldown.
- Imported API tests use an allowed local hostname and the merged HTTP error shape.
- Transaction deadlines account for both idle devnet blocks and Anvil's pending time offset, preventing preflight success followed by an already-expired inclusion.
- Shadow-fork reuse checks block hashes as well as heights, so a replacement block at the same height invalidates its cached state.
- A receipt mismatch is surfaced; gas is recorded separately from token/debt deltas.
- Verification timeout/compile failure cannot be counted as a successfully detected mutant.

There are two upstream deprecation warnings from websockets and the installed Starlette/httpx compatibility path. They did not cause test failures. No public-network transaction, real-wallet signature or mainnet profitability claim is part of this validation. The original Guardian cash-loss proxy and simplified oracle limitations are documented in the presentation notes; this change does not claim to resolve proposal items 1–7 wholesale.
