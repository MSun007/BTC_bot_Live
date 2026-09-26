# Paper operations runbook

## Observed deployment, September 26, 2026

- Project: `btc-bot-v1-live` (historical name).
- Active VM: `larry-paper`, `us-west1-b`; systemd service `larry-paper.service`.
- Source directory: `/opt/larry-paper/releases/paper-20260926`; Python: `/opt/larry-paper/venv/bin/python`.
- Active profile: `10k`; database `/var/lib/larry-paper/paper-10k-20260926.sqlite3`.
- Bucket: `larry-paper-10k-20260926-btc-bot-v1-live`.
- Dashboard: `perp-bot-dashboard`, Cloud Run `us-east1`; revision `perp-bot-dashboard-00187-luc`, 100% traffic.
- Archived $2k run: original database `/var/lib/larry-paper/paper.sqlite3`, consistent backup and previous units `/var/lib/larry-paper/archive-2k-20260926`; original bucket `larry-paper-btc-bot-v1-live` preserved.
- VM watchdog: `larry-paper-watchdog.timer`; an inactive oneshot service between timer runs is normal.
- External monitor: Cloud Scheduler → Cloud Run job `larry-paper-watchdog` in `us-east1`.
- Historical trading VMs `btc-perp-bot` and `btc-trading-bot` were stopped. Do not restart them as part of paper maintenance.

These are observations, not an infrastructure-as-code declaration. Verify current revisions before deployment. The runtime uses a read-only Coinbase key for market data; it is disconnected from live execution, not from market-data APIs.

## Read-only checks

```sh
systemctl is-active larry-paper
systemctl show larry-paper --no-pager -p NRestarts -p ActiveEnterTimestamp -p ExecStart
systemctl list-timers --all --no-pager | grep larry
journalctl -u larry-paper --since '24 hours ago' --no-pager
curl --fail https://YOUR_DASHBOARD/api/paper-health
```

Do not print environment variables, tokens, private keys or Secret Manager contents into support logs. Health must check both snapshot age and engine heartbeat state/age. A running process alone is not proof of successful cycles. A successful watchdog job means the check ran; it does not mean it reported OK.

## Backups and release procedure

1. Record the running engine/dashboard/watchdog image identities and source hashes. Run the offline suite first.
2. Identify the configured `PAPER_DB_PATH`. Preserve a transaction-consistent SQLite backup using the SQLite backup API, not a casual copy of only the main file while WAL writes are active. Preserve the bucket snapshot, config and manifests privately.
3. Keep the current ledger, cash and original start timestamp. Deploy compatible source with the same active profile (`PAPER_PROFILE=10k` for the September 26 run) for reliability-only maintenance.
4. Build the dashboard/watchdog image with all paper modules listed in the Dockerfile. `paper_health.py` and `paper_profile.py` are required by this revision.
5. Test a no-traffic dashboard revision against the intended paper bucket. Verify authentication, paper labels, flat/open authority, fees/funding, stale/error health responses, and that writes to fixed controls are rejected. Do not send test Telegram/email messages unless requested.
6. Replace the engine source atomically and restart the paper service only after validation and a backup. Verify two fresh successful cycles, account reconciliation and no duplicate fills. Update both watchdog deployments with matching code.
7. Route dashboard traffic only after preview checks. Preserve the prior compatible source/image for rollback. Never roll back by restoring an old trading database over newer fills.

## New $10,000 experiment

The September 26 experiment is active; do not repeat initialization. For a future explicitly authorized new experiment, preserve the prior experiment and create a separate empty paper bucket and new database location. Set `PAPER_PROFILE=10k`, `BTC_LOG_BUCKET` to that bucket, and `PAPER_DB_PATH` to the new database. Configure its dashboard and watchdogs to the same bucket. Keep read-only market credentials and existing notification destinations.

Before starting: verify the old account is flat or keep its engine independently managing it; do not abandon an open simulated position. Initialize the new experiment only once. Confirm the first snapshot shows starting equity 10000, a new timestamp, zero historical fills, 100 maximum contracts, 10-contract score-3 probes, a 20/30/50/75/100 ladder, a $125 daily loss limit and $2,500 buffer. Check the canonical profile hash in engine diagnostics.

The original v48 config file is preserved; the profile is deterministically derived before the integrity check. A changed profile against an existing account fails without rebasing that account. The new bucket must not contain an old `paper_snapshot.json` when creating a new database.

## Important failure modes

- **Funding gap with an open position:** the present simulator intentionally refuses to invent missed historical rates. It raises before normal strategy evaluation, so automatic exits and emergency requests also wait. Restore/reconcile verified boundary rates through a reviewed accounting repair; do not delete the database or substitute zero funding. This remains a priority engineering limitation.
- **Snapshot replacement race:** older code can read metadata for one generation and then find that generation replaced before downloading content. This revision retries a fresh blob up to three times while retaining the generation precondition.
- **Notification delivery outage:** messages stay in the durable outbox. This revision also drains error alerts on failed cycles. Delivery is at-least-once: a crash after remote acceptance but before local acknowledgement can duplicate a message. There is no exactly-once guarantee.
- **Notification load:** each drain attempts at most three messages, checks a ten-second soft budget between calls, and stops after the first failed delivery. Failed delivery backs off from one minute to fifteen minutes. A single sender call can exceed the soft budget because its network timeout applies separately.
- **Monitor debounce:** fresh transient errors require two consecutive failed checks; recovery requires two healthy checks. Already-stale heartbeat/publication or invalid data alerts immediately. Unresolved incidents remind at most hourly. Healthy startup is quiet; alerts identify VM versus external monitor and include reasons/ages/queue size.
- **Monitor failure:** unreadable monitor state or failed Telegram delivery emits a sanitized `paper_monitor_failure` error and exits unsuccessfully. An independent Cloud Monitoring policy is needed to notify when the monitor itself fails; the bucket-dependent Telegram monitor cannot guarantee this alone.
- **Cloud publication failure:** a committed simulated fill remains committed; restarting must not replay it. The dashboard becomes stale until publication succeeds.
- **HALT:** preserves the existing engine behavior of skipping ordinary order placement, including automatic exits. Emergency close is a separately authenticated request processed by the engine. This is not a universal guarantee that all positions are protected while halted or while data is unavailable.

## Security and deployment limits

Keep authentication/session secrets stable and outside Git. Use dedicated least-privilege service accounts where practical; the inspected dashboard used the project default Compute service account. This audit did not enumerate all inherited IAM grants, firewall rules or historical secrets. Keep a dependency lock and run a vulnerability check on the exact release image. The local scan of newly resolved packages is not certification of the packages already installed on the VM or Cloud Run.
