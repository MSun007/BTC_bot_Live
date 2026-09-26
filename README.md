# Larry BTC Paper Futures Bot

Larry currently runs a **paper-only BTC futures experiment**. Fills, cash, fees, funding and positions are simulated. Coinbase supplies market data through a key that must be verified unable to trade or transfer. The repository name `BTC_bot_Live` is historical.

The fresh **$10,000 run with 5× absolute position sizes** started September 26, 2026 at 17:56:17 UTC. The old $2,000 experiment was archived while flat. The VM, dashboard and both watchdogs now use the paper reliability release described in [deployment evidence](docs/DEPLOYMENT_2026-09-26.md). Before this audit, GitHub contained the August live-system archive and omitted the paper adapters running on the VM. This revision restores those modules and their operational documentation.

## Components

| File | Responsibility |
|---|---|
| `larry_perp_v1.py` | v48 signal, position-leg and risk engine; installs paper execution before startup |
| `paper_account.py` | SQLite account, atomic transactions, simulated fills, fees and funding |
| `paper_runtime.py` | Read-only market client, paper execution, notification outbox, snapshot publication |
| `paper_profile.py` | Explicit $2,000 default / $10,000 new-run configuration profiles |
| `perp_dashboard_app.py`, `paper_dashboard.py` | Authenticated Flask dashboard using a single paper snapshot per request |
| `paper_health.py`, `paper_watchdog.py` | Shared health checks and independent outage/recovery monitoring |
| `strategy_config.json` | Preserved, hash-checked v48 baseline strategy |
| `paper_settings.json` | Recorded simulation cost assumptions |
| `test_*.py` | Offline risk, accounting, integration, health and scaling regressions |
| `pair_scan.py` | Standalone historical correlation research; not part of execution |
| `Dockerfile`, `requirements.txt` | Dashboard/watchdog image and runtime dependency ranges |

## Paper isolation

- `PaperClient` forwards only explicitly allowlisted market reads. Unknown exchange methods are rejected.
- Order placement requires the paper client and an active SQLite transaction. The dashboard client cannot place orders.
- Both clients verify that `can_trade` and `can_transfer` are false before use.
- `DRY_RUN=false` in the archived strategy permits **simulated** fills after the paper runtime is installed. It does not switch this distribution to live execution.
- The cloud bucket must be separate from the historical live bucket and contain `paper` in its name.
- Missing snapshots cannot fall back to real-account balances.
- Spot execution is disabled. Paper baseline and strategy editing through the dashboard are disabled for a fixed experiment.

## Strategy and accounting

An aligned 3/4 signal can enter a two-contract probe after confirmation on a distinct closed candle. Standard entries and additions require 4/4. The baseline target ladder is **4, 6, 10, 15, 20** contracts, subject to the leverage guard and per-leg controls. The probe can therefore be smaller than the first standard ladder rung.

The baseline maximum is 20 contracts, leverage cap 3×, daily net-loss limit $25 and equity buffer $500. Signal scores, ATR-based stops, independent position legs, adaptive defence, and cooldowns remain governed by the checked-in configuration and regression tests. ATR distance uses 1.5× entry ATR. Leg TP is 1R; the legacy position-wide TP setting is 1.25R. Historical release prose that says 0.75R is not the current configuration.

Paper equity is:

```text
starting cash + realized gross trading P&L - commissions + funding + open unrealized P&L
```

Cost assumptions are 6 basis points of notional plus $0.12 per contract per fill, and 1 basis point of adverse slippage. These were fitted to archived fills, **not verified against a current account fee tier**. Funding is accrued at hourly boundaries using the last observed rate/mark. A funding-data gap with an open position requires reconciliation; see the runbook.

Realized equity charts include fees and funding but exclude open unrealized P&L. Trade statistics exclude funding and count realized exit events; they are not a complete round-trip attribution model. Dollar scaling does not improve percentage returns or provide more evidence of profitability.

## $10,000 candidate profile

`PAPER_PROFILE=10k` prepares a **new** experiment:

| Setting | Default $2,000 | New $10,000 |
|---|---:|---:|
| 3/4 probe | 2 contracts | 10 contracts |
| Standard ladder | 4 / 6 / 10 / 15 / 20 | 20 / 30 / 50 / 75 / 100 |
| Daily loss limit | $25 | $125 |
| Equity buffer | $500 | $2,500 |
| Leverage cap | 3× | 3× |

All configured absolute contract caps and dollar risk limits scale 5×. Prices, contract size, percentage stops, indicator thresholds and cooldowns do not. Whole-contract rounding and margin clipping can still affect decisions. Unit tests verify linear simulated costs/P&L for identical 5× fills; this is not an end-to-end market replay equivalence claim.

The default remains `2k`. Changing capital against an existing database raises an error. Do not rewrite old fills or rebase the old account. Use a new database and bucket, record a new start timestamp, and preserve the original run. Launch instructions are in [the paper runbook](docs/PAPER_RUNBOOK.md).

## Validation

Use Python 3.12 in a fresh virtual environment:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -p 'test_*.py'
python -m compileall -q larry_perp_v1.py perp_dashboard_app.py paper_account.py paper_runtime.py paper_dashboard.py paper_health.py paper_watchdog.py paper_profile.py
```

Tests use fake clients, temporary accounts and mocked notifications. Do not start the engine as a test. Dependency ranges are not a lockfile; record and audit the exact deployed package versions when building a release.

## Operations and research

- [Paper runbook](docs/PAPER_RUNBOOK.md): deployment identity, health, backups and recovery.
- [Audit changes and remaining work](docs/AUDIT_2026-09-26.md): evidence and limitations.
- [HMM oversight assessment](docs/HMM_OVERSIGHT.md): proposed shadow research only, no model-driven trading changes.
- [Historical live README](docs/ARCHIVED_LIVE_README.md) and `V*_RELEASE_NOTES.md`: historical references, not current operating instructions.
- [Observed deployed source hashes](docs/DEPLOYED_SOURCE_MANIFEST_2026-09-26.json): the pre-audit VM release manifest, not hashes of this modified source.
