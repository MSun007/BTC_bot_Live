# Larry v45 Control Integrity Release

Release date: 2026-08-09

## Purpose

v45 addresses the fee-heavy churn pattern where marginal setups could enter, add without improved conviction, and then be reduced by sizing or repeated Adaptive Defense evaluations.

## Engine changes

- Requires a core setup to retain the 3/4 commit score on a later closed candle; 2/4 remains arm-only.
- Stores initial-entry confidence as the progressive-add baseline and retains the one-add production limit.
- Prevents entry-sizing logic from trimming same-side exposure.
- Blocks countertrend entries/adds and caps neutral-regime entries at probe size.
- Counts Adaptive Defense confirmation only once per distinct closed candle.
- Latches one Adaptive Defense reduction per deterioration episode while preserving the confirmed 85+ exit path.
- Preserves adaptive age and evidence through quantity-only reductions.
- Adds fee-complete, idempotent daily risk accounting and a $25 daily Larry net-loss entry halt.
- Validates the GCS configuration version and canonical hash; mismatch blocks entries/adds but never disables exits.

## Dashboard changes

- Running P&L and the Trade Map now include entry/add fees as order impacts.
- Cumulative P&L is gross realized P&L less every successful Larry order fee.
- Historical win-rate/profit-factor calculations allocate entry/add fees across completed exits so totals reconcile without changing the underlying ledger.

## Live migration safety

- Back up the prior VM engine and GCS configuration before replacement.
- Compile on the VM before restarting `larry-perp.service`.
- Reconcile the Coinbase position after restart and verify that no order was submitted during deployment.
- Existing positions retain ATR, TSL, and Adaptive Defense protection throughout a configuration-integrity block.

## Validation

- Python compilation for engine, dashboard, and tests.
- 41 engine regression tests.
- Dashboard JavaScript parse check.
- Git whitespace/error check.
