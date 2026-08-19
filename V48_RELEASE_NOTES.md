# Larry v48 — Confirmed 3/4 Entry Probe

Prepared: 2026-08-19

## Entry behavior

- While Coinbase futures exposure is flat, an aligned long or short score of exactly 3/4 may arm a `SCORE3_PROBE` setup.
- The setup must remain at least 3/4 on the next distinct closed hourly candle before Larry may place an order.
- The target is capped at two contracts (`0.02 BTC`) regardless of confidence sizing.
- A 4/4 setup retains the standard initial-entry path and four-contract cap.
- Adds, reversals, and entries while a position already exists still require the established 4/4 policy; the 3/4 probe cannot add to or flip a position.
- Macro direction, funding, configuration integrity, cooldown, ownership, portfolio leverage, daily-loss, and kill-switch guards remain unchanged.

## Dashboard

- The entry diagnostics panel identifies whether the 3/4 probe is enabled, its two-contract size, current directional eligibility, and the next-closed-candle confirmation requirement.

## Validation

- Engine, dashboard, and test syntax compilation.
- 52 regression tests pass, including new tests for closed-candle confirmation and flat-only enforcement.

## Deployment status

- Local release candidate only. Not deployed to the VM, GCS configuration, or Cloud Run dashboard.
