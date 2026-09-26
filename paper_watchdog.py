"""Independent paper monitor with persistent debounce and actionable alerts."""
import json
import os
import time
from pathlib import Path
from paper_runtime import CloudMirror
from paper_health import assess_snapshot

ADVICE = {
    "engine_error": "Inspect larry-paper service logs; the engine reported a failed cycle.",
    "engine_heartbeat_stale": "Check the VM/service and market-data connectivity.",
    "snapshot_stale_or_clock_skew": "Check engine publication, storage access and system clocks.",
    "notifications_pending": "Check Telegram/email delivery; queued messages remain durable.",
    "snapshot_unavailable": "Check the configured paper bucket and publisher.",
    "invalid_snapshot": "Check snapshot/heartbeat schema before trusting dashboard values.",
}

def transition(previous, assessment, now=None, source="VM"):
    """Two failures/recoveries, immediate already-stale data, hourly reminders.

    Return proposed durable state and optional message. Caller commits the state
    only after successful delivery, so delivery failures are retried next run.
    """
    now = time.time() if now is None else now
    state = dict(previous or {})
    observed = "OK" if assessment.get("healthy") else "ERROR"
    reason = assessment.get("reason", "unknown")
    recent = 0 <= now - state.get("checked_at", now) <= 900
    count = state.get("streak", 0) + 1 if recent and state.get("candidate") == observed else 1
    state.update(candidate=observed, streak=min(count, 1000), checked_at=now, reason=reason, source=source)
    old = state.get("status")
    if old is None and observed == "OK":
        state.update(status="OK", last_alert_at=0)
        return state, None
    urgent = reason in {"engine_heartbeat_stale", "snapshot_stale_or_clock_skew", "invalid_snapshot"}
    confirmed = count >= 2 or (observed == "ERROR" and urgent)
    changed = observed != old and confirmed
    reminder = observed == old == "ERROR" and now - state.get("last_alert_at", state.get("time", now)) >= 3600
    if not changed and not reminder:
        return state, None
    state.update(status=observed, last_alert_at=now)
    if observed == "OK":
        return state, f"[PAPER] Larry {source} monitor RECOVERED\nTwo consecutive healthy checks; snapshot and engine heartbeat are current."
    detail = [f"Reason: {reason}"]
    for key, label in (("age_seconds", "Snapshot age"), ("heartbeat_age_seconds", "Heartbeat age"), ("pending_notifications", "Queued alerts")):
        if key in assessment:
            detail.append(f"{label}: {assessment[key]}" + ("s" if key.endswith("seconds") else ""))
    detail.append(ADVICE.get(reason, "Inspect dashboard/monitor logs and service connectivity."))
    return state, f"[PAPER] Larry {source} monitor {'STILL UNHEALTHY' if reminder else 'ERROR'}\n" + "\n".join(detail)

def inspect(mirror, dashboard_url=None):
    try:
        snapshot, _ = mirror.read("paper_snapshot.json")
        result = assess_snapshot(snapshot)
        if dashboard_url:
            import requests
            response = requests.get(dashboard_url.rstrip('/') + "/api/paper-health", timeout=15)
            try:
                remote = response.json()
            except ValueError:
                remote = {}
            if response.status_code != 200 or not isinstance(remote, dict) or remote.get("healthy") is not True:
                if result.get("healthy"):
                    result = dict(remote) if isinstance(remote, dict) else {}
                    result.update(healthy=False, reason=result.get("reason", f"dashboard_http_{response.status_code}"))
        return result
    except Exception as exc:
        return {"healthy": False, "reason": "monitor_read_failed_" + type(exc).__name__}

def notify(text):
    from google.cloud import secretmanager
    import requests
    sm = secretmanager.SecretManagerServiceClient()
    project = os.environ.get("PROJECT_ID", "btc-bot-v1-live")
    def secret(name):
        return sm.access_secret_version(request={"name": f"projects/{project}/secrets/{name}/versions/latest"}, timeout=10).payload.data.decode().strip()
    token, chat_id = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=12)
    if not response.ok or not response.json().get("ok"):
        raise RuntimeError("Watchdog Telegram delivery failed")

def main():
    cloud_mode = os.getenv("PAPER_WATCHDOG_CLOUD") == "true"
    source = "external" if cloud_mode else "VM"
    try:
        state_path = Path(os.getenv("PAPER_WATCHDOG_STATE", "/var/lib/larry-paper/watchdog.json"))
        mirror = CloudMirror(os.environ["BTC_LOG_BUCKET"])
        previous = (mirror.read("external_watchdog_state.json")[0] or {}) if cloud_mode else json.loads(state_path.read_text()) if state_path.exists() else {}
        assessment = inspect(mirror, os.getenv("PAPER_DASHBOARD_URL"))
        state, message = transition(previous, assessment, source=source)
        if message:
            notify(message)
        if cloud_mode:
            mirror.write("external_watchdog_state.json", state)
        else:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = state_path.with_suffix(".tmp")
            temp.write_text(json.dumps(state))
            temp.replace(state_path)
        print(json.dumps({"severity": "INFO", "event": "paper_monitor_check", "source": source, "observed_healthy": assessment.get("healthy"), "reason": assessment.get("reason"), "alert_status": state.get("status", "PENDING"), "alert_sent": bool(message)}))
    except Exception as exc:
        # Cloud Monitoring observes this outside the monitored bucket. Never log
        # request URLs, tokens, secret payloads or raw exception messages.
        print(json.dumps({"severity": "ERROR", "event": "paper_monitor_failure", "source": source, "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
