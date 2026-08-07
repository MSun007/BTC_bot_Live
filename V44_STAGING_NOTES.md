# Larry v44 staging notes

Status: reviewed, validated and approved for production deployment on August 7, 2026. The final deployment identifiers and verification results are recorded after rollout.

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
