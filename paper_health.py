"""Shared, side-effect-free paper health assessment."""
import json
import time
import math
from datetime import datetime


def assess_snapshot(value, now=None):
    now = time.time() if now is None else now
    if not isinstance(value, dict) or value.get("mode") != "PAPER":
        return {"mode": "PAPER", "healthy": False, "reason": "snapshot_unavailable"}
    try:
        age = now - datetime.fromisoformat(value["published_at"]).timestamp()
        heartbeat = json.loads(value["objects"].get("coinbase_unified_heartbeat.json", "{}"))
        heartbeat_age = now - datetime.fromisoformat(heartbeat["ts"]).timestamp()
        pending = int(value.get("pending_notifications", 0))
        if not heartbeat.get("state") or pending < 0 or not all(map(math.isfinite, (age, heartbeat_age))):
            raise ValueError("Invalid health fields")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return {"mode": "PAPER", "healthy": False, "reason": "invalid_snapshot"}
    reason = "ok"
    if not 0 <= age <= 180:
        reason = "snapshot_stale_or_clock_skew"
    elif str(heartbeat.get("state", "")).upper() == "ERROR" or str(heartbeat.get("status", "")).upper() == "ERROR":
        reason = "engine_error"
    elif not 0 <= heartbeat_age <= 180:
        reason = "engine_heartbeat_stale"
    elif pending:
        reason = "notifications_pending"
    return {"mode": "PAPER", "healthy": reason == "ok", "reason": reason,
            "age_seconds": round(age), "heartbeat_age_seconds": round(heartbeat_age),
            "pending_notifications": pending}
