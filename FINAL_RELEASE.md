> Historical live-system documentation. Larry is now PAPER ONLY. These archived instructions are not the current deployment runbook. See the repository README and docs/PAPER_RUNBOOK.md.

# Larry v48.0 Final Release

Archived: 2026-08-30

This directory is the final verified, portable Larry distribution. The engine and dashboard files match the source bundle used by the final Cloud Run deployment.

## Release identity

- Distribution: `v48.0-final`
- Engine: `larry_perp_v48_score3_probe`
- Git source: `MSun007/BTC_bot_Live`, branch `main`
- Production commit: `5d46f85f224de58ed42b6bcf259a38b21c31cf12`
- Final dashboard revision: `perp-bot-dashboard-00179-bmp` in `us-east1`
- Trading VM: `btc-perp-bot` in `us-west1-b`

## Included application files

- `larry_perp_v1.py`: trading engine
- `perp_dashboard_app.py`: operations dashboard
- `strategy_config.json`: v48 strategy configuration
- `test_adaptive_risk.py`: regression test suite
- `Dockerfile`, `.dockerignore`, and `requirements.txt`: dashboard deployment assets
- `pair_scan.py`: supporting scanner
- `README.md` and release notes: operating and release documentation

The parent archive also contains `cloud_backup/gcs_full`, which preserves all 105 GCS objects with their original directory structure, and `cloud_backup/cloud_run_source`, which preserves the exact final dashboard build source.

## Shutdown record

Larry was intentionally shut down after repeated GCS write timeouts and Coinbase TLS EOF errors. Alerts stated that no bot-managed position was active and no order was attempted. The final GCS state, position state, ledgers, heartbeats, configuration, and signal logs were downloaded before the VM was stopped. A Compute Engine disk snapshot was also created to preserve files that could not be read over the unresponsive SSH path.

- Snapshot: `btc-perp-bot-final-v48-20260830` (`READY`)
- VM stopped: `2026-08-30T10:36:53.923-07:00`
- Final archive documentation begins at commit `9661a48` on GitHub `main`; later commits only record shutdown metadata.

## Safe redeployment checklist

1. Create a fresh Python environment and install the pinned dependencies in `requirements.txt`.
2. Run `python -m py_compile larry_perp_v1.py perp_dashboard_app.py test_adaptive_risk.py` and `python -m unittest -q test_adaptive_risk.py`.
3. Provision secrets outside the repository. Never commit Coinbase, Telegram, CoinGecko, or Google credentials.
4. Use a new storage bucket or restore only the intended objects from `cloud_backup/gcs_full`; verify `CONFIG_VERSION` and the canonical config hash.
5. Deploy the dashboard from this exact commit/package and confirm its version banner.
6. Start the engine in `DRY_RUN=true` with the kill switch enabled. Confirm exchange reads, storage reads/writes, clock, permissions, and Telegram delivery.
7. Independently verify the Coinbase account is flat and no stale orders exist.
8. Disable the kill switch and live-trading dry run only after an operator signs off on every health gate.

## Commercial handoff cautions

This release is deployable source code, not a promise of profitability or uninterrupted operation. A third-party deployment should add customer-specific secret management, monitoring, terms/risk disclosures, support ownership, data-retention rules, and an explicit incident-response process. Resolve the GCS subprocess timeout path and Coinbase connection resilience before representing the system as production-ready for unattended trading.
