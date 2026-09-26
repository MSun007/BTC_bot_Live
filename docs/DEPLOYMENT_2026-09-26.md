# September 26, 2026 paper deployment

The user authorized deployment, monitor improvements and a fresh $10,000 experiment. The previous $2,000 account was verified flat before stopping its engine. Its original database and bucket remain preserved; a consistent SQLite backup and previous systemd unit definitions are in `/var/lib/larry-paper/archive-2k-20260926`.

## Release identities

- Project: `btc-bot-v1-live`; VM: `larry-paper`, `us-west1-b`.
- Engine source: `/opt/larry-paper/releases/paper-20260926`.
- Engine service started: `2026-09-26 17:56:17 UTC`; zero automatic restarts at verification.
- New database: `/var/lib/larry-paper/paper-10k-20260926.sqlite3`.
- New bucket: `larry-paper-10k-20260926-btc-bot-v1-live`.
- Dashboard revision: `perp-bot-dashboard-00187-luc`, 100% traffic, `us-east1`.
- Dashboard and external watchdog image: `us-east1-docker.pkg.dev/btc-bot-v1-live/cloud-run-source-deploy/perp-bot-dashboard@sha256:a9d08cc086accac9280634261539a46650f828eda036f9a67779f81595f320a9`.
- Successful Cloud Build: `28e134ff-0ebd-4779-8ee6-5667176b8781`.

## Verified behavior

99 offline tests passed locally and on the VM release. Dashboard health returned HTTP 200 with current snapshot and engine heartbeat, reason `ok`, and zero pending notifications. Unauthenticated account data returned HTTP 401; legitimate PIN authentication and account reconciliation passed. The refreshed main dashboard displayed $10,000 starting capital and equity, zero P&L, zero fills, and a flat paper account.

The active target ladder is 20/30/50/75/100 contracts; the score-3 probe is 10, daily loss limit $125, equity buffer $2,500 and leverage cap 3×. Absolute sizes and dollar limits are multiplied by five; percentage limits and signal rules remain unchanged. The code default remains the explicit 2k profile; the deployed unit selects 10k.

## Monitoring

The VM watchdog timer is active and its 18:01:01 UTC check reported healthy, reason `ok`, status `OK`, without sending a startup alert. External execution `larry-paper-watchdog-8r86g` reported the same at 18:01:46 UTC and exited successfully. Both use the new bucket and release. Cloud Scheduler was resumed and verified `ENABLED` on its five-minute schedule; the VM timer checks every minute.

Checks debounce transient failures and recovery across two samples, immediately report stale data, and remind hourly during unresolved outages. Engine notification retries retain messages and back off after failures. A separate Cloud Monitoring policy routes ERROR logs from the external job or scheduler to Larry's existing email destination. Synthetic delivery and complete infrastructure-outage drills were not performed.

## Limits and rollback

This is paper execution only; no live execution was enabled. The funding-gap limitation remains: a missing historical hourly rate with an open paper position blocks the cycle, including exits, until reviewed reconciliation. See the audit and runbook for remaining audit scope and performance-statistics caveats.

Reversible systemd drop-ins select the new source and account. Preserve any newly accumulated fills before rollback. Never restore an old database over new fills or point old code at an incompatible account. Prior dashboard revisions and old source remain available; switching the dashboard alone does not roll back the engine or monitors.

Runtime deployment was verified independently of GitHub publication. The release image and VM retain the candidate VERSION metadata present during build; the repository VERSION and this post-deployment report were updated afterward. Executable source is unchanged by that documentation update.
