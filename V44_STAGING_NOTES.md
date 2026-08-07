# Larry v44 staging notes

Status: deployed and verified in production on August 7, 2026.

## Scope

- Structured pre-order `TRADE_DECISION` records in logs, engine state, ledger metadata, email and concise Telegram notifications.
- Bounded retries for retry-safe Coinbase read failures only; order submissions remain single-shot.
- Exact handling for Coinbase's transient portfolio-access 403 response.
- Client refresh after 401 or the known portfolio-access 403.
- Startup resilience and outage alert deduplication/recovery.
- Runtime/API-health telemetry and normal first-write handling for missing GCS read objects.
- Dashboard `POSITION UNVERIFIED` state when Coinbase cannot confirm futures exposure.

## Explicitly unchanged

- Signal and phantom-confirmation thresholds
- Conviction sizing and maximum exposure
- Leverage guard
- TP1, ATR stop and trailing-stop behavior
- Adaptive Defence thresholds and confirmations
- Same-side post-adaptive fresh-setup guard
- Manual/external position ownership rules
- Emergency-flatten execution rules
- `strategy_config.json`

## Review cleanups applied to the supplied candidate

- A no-op target plan no longer emits a false trade-decision record.
- Decision metadata receives the actual client order ID after the single order submission.
- Generic 403 responses are fail-closed; only the exact portfolio-access 403 is retryable.
- A 401 is not retried against the same client and instead refreshes the client for the next cycle.
- The service survives an initial Coinbase outage and starts in an explicit unverified/fail-closed state.
- A one-cycle outage is cleared correctly after recovery without creating a false persistent degraded state.
- GCS missing-object suppression is restricted to read (`storage cat`) operations.
- The GCS command timeout remains at the established 30-second default rather than changing it incidentally.
- The dashboard cannot convert a failed Coinbase futures-position read into `FLAT`.

## Local validation

- Python source compilation: PASS
- Engine import smoke test: PASS
- Dashboard JavaScript parse: PASS
- Unit tests: 32 PASS
- `git diff --check`: PASS (line-ending notices only)

## Deployment gate

Before deployment, verify Coinbase reports the futures book flat. Then:

1. Create a timestamped backup of the current operator Live Platform files.
2. Copy the reviewed canonical engine and dashboard into the operator Live Platform folder.
3. Commit the reviewed release to trusted repository `MSun007/BTC_bot_Live` and push `main`.
4. Deploy the dashboard to Cloud Run from the pushed commit.
5. Back up and update the VM engine, restart `larry-perp.service`, and verify its loaded file/hash.
6. Confirm exchange position, engine version, heartbeat, API-health state, Cloud Run revision and logs.

## Production result

- Release commit: `117a39afb0fe75cd8488e3a4b0bad00b620470e8`
- Cloud Run: `perp-bot-dashboard-00152-cmd`, 100% traffic, `us-east1`
- VM: `btc-perp-bot`, `larry-perp.service` active/running, PID `1486243`
- VM/GitHub engine SHA-256: `34fad927567efa8ec94951cc5e9516734ec3d2cba90c899203edced9e2c6d228`
- Published engine state: `larry_perp_v44_observability_reliability`
- Startup and first completed cycle position: `FLAT 0`
- API health: `HEALTHY`; zero consecutive failures
- Local backup: `LIVE PLATFORM/backup_pre_v44_observability_reliability_20260807_064808`
- VM backup: `/home/msunderji/larry_perp_v1.py.backup_pre_v44_20260807_0656`
- First completed cycle: `2026-08-07T10:58:23Z`, no error and no order attempted
- Runtime observation: first v44 cycle took 113 seconds; v43 had already logged a 96-second cycle overrun immediately before deployment.

## v44.1 decision-contract hotfix

After the first v44 entry, the operator confirmed that the promised decision
details were not present in Telegram. Review found that v44 stored only a
partial schema: core sizing confidence was not wired into the canonical
decision, Telegram used a generic trade template, macro used the wrong display
field, and the running ledger summary was never populated before email.

v44.1 corrects those reporting defects without changing strategy behavior and
adds regression tests for the canonical decision fields and Telegram contract.
