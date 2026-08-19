# Larry v48 — Confirmed 3/4 Entry Probe

Deployed: 2026-08-19

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

- GitHub production commit: `03ef9d0` on `main`.
- VM: `btc-perp-bot` in `us-west1-b`; engine `larry_perp_v48_score3_probe` active under `larry-perp.service`.
- VM backup: `/home/msunderji/larry_perp_v1.py.backup_pre_v48_20260819_1500`.
- GCS config backup: `gs://btc_trade_log/backups/strategy_config_pre_v48_20260819_1500.json`.
- Cloud Run: `perp-bot-dashboard-00177-dhm`, serving 100% traffic in `us-east1`.
- Post-deployment checks: flat position, config version/hash matched, Coinbase healthy, GCS healthy, risk gate open, kill switch off, live heartbeat, `DRY_RUN=false`.
