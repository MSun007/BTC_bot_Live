> Historical live-system documentation. Larry is now PAPER ONLY. These archived instructions are not the current deployment runbook. See the repository README and docs/PAPER_RUNBOOK.md.

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

- GitHub production commit: `5d46f85f224de58ed42b6bcf259a38b21c31cf12` on `main` (`03ef9d0` introduced the strategy behavior; `5d46f85` finalized production metadata).
- VM: `btc-perp-bot` in `us-west1-b`; engine `larry_perp_v48_score3_probe` was shut down and the VM was stopped on 2026-08-30 after the final archive was created.
- VM backup: `/home/msunderji/larry_perp_v1.py.backup_pre_v48_20260819_1500`.
- GCS config backup: `gs://btc_trade_log/backups/strategy_config_pre_v48_20260819_1500.json`.
- Final Cloud Run deployment: `perp-bot-dashboard-00179-bmp`, built from commit `5d46f85f224de58ed42b6bcf259a38b21c31cf12` and serving 100% traffic in `us-east1` at archive time.
- Final incident state: no bot-managed position and no order attempted. Repeated GCS write timeouts and Coinbase TLS EOF errors caused degraded-state alerts; this archive preserves the last durable state and logs for diagnosis.
