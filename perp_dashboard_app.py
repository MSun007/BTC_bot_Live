#!/usr/bin/env python3
"""
Coinbase Unified BTC Dashboard — v76 position-authority cleanup
- Coinbase-only: no Gemini files, no Gemini API calls
- Preserves graphical Spot Signal Monitor UX
- Adds professional capital, P&L, fee, funding, margin, and position transparency
- Uses Coinbase balances / futures account as accounting source of truth
- Live Coinbase avg entry is source of truth; legacy opening basis is reference-only
- v69: emergency flatten records dashboard-initiated operator exits in Larry ledger with P&L attribution
- v79: Larry equity curve + max drawdown + annualized Sharpe (reconstructed from the realized-trade ledger); trade-map range P&L now shown as a % return (comparable to BTC move %); CRITICAL fix of a v78 JS syntax error (a raw-string backslash-escaped apostrophe broke the entire dashboard script)
- v78: Larry-focused declutter — single performance story (Larry-only equity headline), one Manual Trading Impact footnote (no dual monitoring), execution-quality source-of-truth bug fix, empty placeholders removed, global feed-freshness in header, windowed metrics relabeled
- v76: position-authority panel makes executable bot management explicit; Max Conviction is the sole size cap.
- v75: command-center visual redesign (Bitcoin-terminal identity; CSS/login only, zero route or logic changes)
- v73: every route now requires a PIN-gated login session (previously public/unauthenticated);
  the emergency-flatten request is HMAC-signed so the VM can verify it came from this dashboard.
"""
import os, json, time, uuid, base64, traceback, csv, io, hmac, hashlib
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, List

import gcsfs
import pandas as pd
import pytz
import requests
import jwt as pyjwt
from flask import Flask, jsonify, render_template_string, request, session, redirect
from google.cloud import secretmanager
from coinbase.rest import RESTClient
from cryptography.hazmat.primitives.serialization import (
    load_der_private_key,
    load_pem_private_key,
    Encoding,
    PrivateFormat,
    NoEncryption,
)
from cryptography.hazmat.backends import default_backend
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange

app = Flask(__name__)

PROJECT_ID = os.environ.get("PROJECT_ID", "btc-bot-v1-live")
BUCKET_PREFIX = os.environ.get("BUCKET_PREFIX", "gs://btc_trade_log")
TZ = pytz.timezone("America/New_York")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "8"))


def _load_session_secret() -> str:
    """v73: Flask needs a stable secret_key to sign the login session cookie.

    Self-contained (does not call the module's secret() helper defined further down)
    so it can run at import time. Falls back to a random per-process key so the app
    still boots without it provisioned, but sessions will not survive a restart/redeploy
    and will look like random logouts across multiple Cloud Run instances -- provision
    DASHBOARD_SESSION_SECRET in Secret Manager (or as a Cloud Run env var) for real use.
    """
    env_val = os.environ.get("DASHBOARD_SESSION_SECRET")
    if env_val:
        return env_val
    try:
        sm = secretmanager.SecretManagerServiceClient()
        ref = f"projects/{PROJECT_ID}/secrets/DASHBOARD_SESSION_SECRET/versions/latest"
        return sm.access_secret_version(request={"name": ref}).payload.data.decode("utf-8").strip()
    except Exception as e:
        import sys
        print(
            f"WARNING: DASHBOARD_SESSION_SECRET not available ({e}). Using an ephemeral "
            "random session key: operator sessions will not survive a restart/redeploy and "
            "may behave inconsistently across multiple Cloud Run instances. Provision this "
            "secret for stable logins.",
            file=sys.stderr,
        )
        return os.urandom(32).hex()


app.secret_key = _load_session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    # Strict SameSite means the browser never attaches this cookie to a cross-site
    # request (a bare link, an <img src>, a form on another site) -- this is what
    # closes the GET-based CSRF hole that used to let anyone with the URL flip the
    # kill switch. It also means a login started by following a link from outside
    # the dashboard itself will need one extra navigation; acceptable for an
    # operator-only control plane.
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# v73: simple in-memory rate limiter for PIN checks (login and emergency-flatten).
# Caveat: this resets on a Cloud Run cold start/redeploy and is per-instance, so it
# is not a substitute for a real WAF/Cloud Armor rate limit in front of the service --
# but it is a meaningful improvement over no throttling at all.
_pin_attempts: Dict[str, List[float]] = {}
PIN_MAX_ATTEMPTS = 5
PIN_WINDOW_SECONDS = 900  # 15 minutes


def _pin_rate_limited(bucket_key: str) -> bool:
    now = time.time()
    hits = [t for t in _pin_attempts.get(bucket_key, []) if now - t < PIN_WINDOW_SECONDS]
    _pin_attempts[bucket_key] = hits
    return len(hits) >= PIN_MAX_ATTEMPTS


def _record_pin_attempt(bucket_key: str) -> None:
    _pin_attempts.setdefault(bucket_key, []).append(time.time())


# ─────────────────────────────────────────────────────────────────────────────
# v73: authentication -- every route in this file used to be reachable by anyone
# with the Cloud Run URL, including /api/halt, /api/resume, and the strategy
# config writers. All routes now require a logged-in session; _pin_matches (used
# by the operator PIN check below) is defined further down next to
# _get_emergency_pin, which this references at request time.
# ─────────────────────────────────────────────────────────────────────────────
_PUBLIC_PATHS = {"/login"}

_LOGIN_PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Larry Command Center — Sign in</title>
<style>
  :root{--bg:#07080b;--card:#0e1218;--line:#212a37;--text:#e9eef5;--sub:#94a1b6;--brand:#f7931a}
  *{box-sizing:border-box}
  body{background:radial-gradient(900px 500px at 70% -10%,rgba(247,147,26,.08),transparent 60%),var(--bg);
       color:var(--text);font-family:-apple-system,"Segoe UI",system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  form{position:relative;background:var(--card);border:1px solid var(--line);border-radius:14px;
       padding:36px 30px 30px;width:300px;overflow:hidden;
       box-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 20px 60px rgba(0,0,0,.5)}
  form::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;
       background:linear-gradient(90deg,transparent,var(--brand),transparent)}
  .mark{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;
       font-size:24px;font-weight:800;color:#1c1104;margin-bottom:16px;
       background:linear-gradient(180deg,#ffa838,#f28c0f);box-shadow:0 4px 18px rgba(247,147,26,.35)}
  h1{font-size:17px;margin:0 0 4px;letter-spacing:-.01em}
  .tag{color:var(--sub);font-size:12px;margin-bottom:20px}
  input{width:100%;padding:11px 12px;font-size:16px;border-radius:9px;
        border:1px solid var(--line);background:#0b0e14;color:var(--text);margin-bottom:14px}
  input:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(247,147,26,.15)}
  button{width:100%;padding:11px;border-radius:9px;border:none;
         background:linear-gradient(180deg,#ffa838,#f28c0f);color:#1c1104;
         font-weight:800;font-size:15px;cursor:pointer;letter-spacing:.02em}
  button:hover{filter:brightness(1.08)}
  .err{color:#ffb3b6;font-size:13px;margin-bottom:14px;background:rgba(240,86,92,.1);
       border:1px solid rgba(240,86,92,.35);border-radius:8px;padding:8px 10px}
</style></head><body>
<form method="post" action="/login">
  <div class="mark">₿</div>
  <h1>Larry Command Center</h1>
  <div class="tag">BTC Perp Engine · operator access</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <input type="password" name="pin" placeholder="Operator PIN" autofocus required>
  <button type="submit">Sign in</button>
</form>
</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template_string(_LOGIN_PAGE_HTML, error=None)
    remote = request.remote_addr or "unknown"
    if _pin_rate_limited(f"login:{remote}"):
        app.logger.warning("LOGIN_RATE_LIMITED remote=%s", remote)
        return render_template_string(_LOGIN_PAGE_HTML, error="Too many attempts. Wait 15 minutes and try again."), 429
    pin = (request.form.get("pin") or "").strip()
    _record_pin_attempt(f"login:{remote}")
    if not _pin_matches(pin):
        app.logger.warning("LOGIN_FAILED remote=%s", remote)
        return render_template_string(_LOGIN_PAGE_HTML, error="Incorrect PIN."), 403
    session.clear()
    session["authenticated"] = True
    session.permanent = True
    app.logger.info("LOGIN_OK remote=%s", remote)
    return redirect("/")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.before_request
def _require_login():
    if request.path in _PUBLIC_PATHS or request.path.startswith("/static/"):
        return None
    if session.get("authenticated"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "authentication required", "login_url": "/login"}), 401
    return redirect("/login")


# Coinbase-only GCS paths. These deliberately avoid Gemini legacy files.
GCS_STRATEGY_CONFIG = f"{BUCKET_PREFIX}/strategy_config.json"
GCS_CONFIG_CANDIDATES = [
    GCS_STRATEGY_CONFIG,
    f"{BUCKET_PREFIX}/coinbase_unified_bot_config.json",
    f"{BUCKET_PREFIX}/coinbase_bot_config.json",
    f"{BUCKET_PREFIX}/perp_bot_config.json",
]
GCS_HEARTBEAT_CANDIDATES = [
    f"{BUCKET_PREFIX}/coinbase_unified_heartbeat.json",
    f"{BUCKET_PREFIX}/perp_bridge_heartbeat.json",
    f"{BUCKET_PREFIX}/perp_heartbeat.json",  # Coinbase Larry fallback only; not Gemini
]
GCS_SPOT_STATE = f"{BUCKET_PREFIX}/coinbase_spot_position_state.json"
GCS_SPOT_TRADES = f"{BUCKET_PREFIX}/coinbase_spot_trades_log.csv"
GCS_PERP_STATE = f"{BUCKET_PREFIX}/perp_engine_state.json"
GCS_PERP_TRADES = f"{BUCKET_PREFIX}/coinbase_perp_trades_log.csv"
GCS_PERP_TRADES_LEDGER = f"{BUCKET_PREFIX}/perp_trades_ledger.csv"  # v12 canonical bot ledger
GCS_MANUAL_POSITION_EVENTS = f"{BUCKET_PREFIX}/manual_position_events.csv"
GCS_CAPITAL = f"{BUCKET_PREFIX}/unified_capital_state.json"
GCS_SIGNAL_HISTORY = f"{BUCKET_PREFIX}/coinbase_signal_history.json"
GCS_BOT_HALT = f"{BUCKET_PREFIX}/bot_halt.json"  # v29 kill switch
GCS_EMERGENCY_FLATTEN_REQUEST = f"{BUCKET_PREFIX}/emergency_flatten_request.json"  # v71 PIN-validated dashboard request; VM executes
GCS_SIGNAL_PNL_ROLLUP = f"{BUCKET_PREFIX}/signal_pnl_rollup.csv"  # v12 signal-class P&L rollup

DEFAULT_CONFIG = {
    "BOT_NAME": "Coinbase Unified BTC Bot",
    "SPOT_PRODUCT_ID": "BTC-USDC",
    "SPOT_FALLBACK_PRODUCT_ID": "BTC-USD",
    "PERP_PRODUCT_ID": "BIP-20DEC30-CDE",
    "CONTRACT_SIZE_BTC": 0.01,
    "MAX_CONVICTION_CONTRACTS": 10,
    "CONTRACTS_PER_TRADE": 2,
    "CONTRACTS_PER_TRADE_FULL": 10,
    "CONTRACTS_PER_TRADE_PARTIAL": 4,
    "CONTRACTS_PER_TRADE_PROBE": 2,
    "SCORE4_MACRO_OVERRIDE_ENABLED": True,
    "MACRO_BLOCKED_PROBE_CONTRACTS": 2,
    "TP1_PCT": 0.0075,
    "TP1_FRACTION": 0.5,
    "FUNDING_SIZE_REDUCE_AT": 0.0005,
    "MANUAL_POSITION_MODE": "monitor_only",
    "SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL": True,
    "EMAIL_INCLUDE_RAW_ORDER": False,
    "SIGNAL_GRANULARITY": "FIFTEEN_MINUTE",
    "MACRO_GRANULARITY": "ONE_HOUR",
    "RSI_PERIOD": 14,
    "RSI_BUY_FLOOR": 20,
    "RSI_BUY_THRESHOLD": 28,
    "RSI_EXIT_THRESHOLD": 95,
    "BB_PERIOD": 20,
    "BB_STD": 2,
    "VOLUME_AVG_PERIOD": 20,
    "VOLUME_MULTIPLIER": 1.5,
    "STOCH_RSI_PERIOD": 14,
    "STOCH_RSI_THRESHOLD": 0.10,
    "ATR_PERIOD": 14,
    "ATR_STOP_MULTIPLIER": 1.5,
    "TSL_ACTIVATION_PCT": 0.08,
    "TSL_TRAIL_PCT": 0.03,
    "PHANTOM_EXTENSION_PCT": 0.005,
    "FUNDING_LONG_MAX": 0.001,
    "FUNDING_SHORT_MIN": -0.001,
    "SPOT_TRANCHE_TARGETS_PCT": [25, 33, 50, 90],
    "ENABLE_SPOT_BTC_TRADING": False,
    "ENABLE_SPOT_BRIDGE_PERP_BUYS": False,
    "MAX_EFFECTIVE_LEVERAGE": 3.0,
    "MIN_FUTURES_EQUITY_BUFFER_USD": 1000,
    "DAILY_STOP_LIMIT": 3,
    "LOSS_STREAK_LIMIT": 3,
    "STREAK_PAUSE_HOURS": 24,
    "SPOT_ENTRY_COOLDOWN_SEC": 300,
    "PERP_ENTRY_COOLDOWN_SEC": 300,
    "BRIDGE_ENTRY_COOLDOWN_SEC": 300,
    "SPOT_MAX_LADDER_UNITS": 4,
    "PERP_STRATEGY_SLOTS": 4,
    "HEARTBEAT_STALE_SECONDS": 180,
    "HEARTBEAT_DOWN_SECONDS": 420,
}

_secrets: Optional[Dict[str, str]] = None
_cb: Optional[RESTClient] = None
_fs: Optional[gcsfs.GCSFileSystem] = None
_cache: Dict[str, Any] = {}

# ─────────────────────────────────────────────────────────────────────────────
# Basic helpers
# ─────────────────────────────────────────────────────────────────────────────
def now_et() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S ET")


def safe_float(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def safe_int(x, default=0):
    try:
        if x is None or x == "":
            return default
        return int(float(x))
    except Exception:
        return default


def money_obj_value(obj: Any, default=0.0):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return safe_float(obj.get("value"), default)
    return safe_float(getattr(obj, "value", None), default)


def attr_or_key(obj: Any, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def fs() -> gcsfs.GCSFileSystem:
    global _fs
    if _fs is None:
        _fs = gcsfs.GCSFileSystem(project=PROJECT_ID)
    return _fs


def gcs_exists(path: str) -> bool:
    try:
        return fs().exists(path)
    except Exception:
        return False


def read_json(path: str, default=None):
    try:
        with fs().open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def write_json(path: str, payload: Dict[str, Any]):
    with fs().open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def read_first_json(paths: List[str], default=None) -> Dict[str, Any]:
    for p in paths:
        if p and gcs_exists(p):
            data = read_json(p, None)
            if isinstance(data, dict):
                data["_source"] = p
                return data
    return default if default is not None else {}


def read_csv(path: str) -> pd.DataFrame:
    try:
        if not gcs_exists(path):
            return pd.DataFrame()
        with fs().open(path, "rb") as f:
            return pd.read_csv(f)
    except Exception:
        return pd.DataFrame()

def read_text_gcs(path: str) -> str:
    try:
        if not gcs_exists(path):
            return ""
        with fs().open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_boolish(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "y", "ok")


def liquidity_from_order_payload(raw_order: Any) -> str:
    """Best-effort Maker/Taker classification for dashboard/research display.

    Coinbase fills may include a liquidity_indicator, but Larry's ledger stores
    the order payload. All current Larry executions use market_market_ioc, which
    are taker orders. If future passive/limit execution is added, this helper
    will surface that distinction without breaking older rows.
    """
    txt = raw_order if isinstance(raw_order, str) else json.dumps(raw_order or {})
    low = txt.lower()
    if "liquidity_indicator" in low and "maker" in low:
        return "MAKER"
    if "liquidity_indicator" in low and "taker" in low:
        return "TAKER"
    if "market_market_ioc" in low or "market" in low:
        return "TAKER"
    if "limit_limit_gtc" in low or "post_only" in low:
        return "MAKER?"
    return "—"



def current_blocked_actions(engine_state: Dict[str, Any], risk_gate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return current blockers only, filtering stale monitor-only/target messages.

    The bot may keep last_blocked_action in state for operator context. The dashboard
    should not treat old manual/external or already-at-target messages as current
    blockers after Coinbase is flat and the risk gate is open.
    """
    raw = engine_state.get("last_blocked_action") if isinstance(engine_state.get("last_blocked_action"), dict) else {}
    out = {k: v for k, v in raw.items() if v}
    live = engine_state.get("exchange_position") if isinstance(engine_state.get("exchange_position"), dict) else {}
    manual = engine_state.get("manual_position_status") if isinstance(engine_state.get("manual_position_status"), dict) else {}
    live_signed = safe_float(live.get("signed_contracts"), 0.0)
    manual_active = bool(manual.get("is_manual_or_external")) and abs(live_signed) > 0
    if not manual_active:
        for key in ["manual_position", "perp_entries"]:
            val = str(out.get(key, ""))
            if "Manual/external" in val or "monitor-only" in val:
                out.pop(key, None)
    if abs(live_signed) == 0 and str(out.get("perp", "")) == "already_at_confidence_target_size":
        out.pop("perp", None)
    rg = risk_gate or (engine_state.get("risk_gate") if isinstance(engine_state.get("risk_gate"), dict) else {})
    if isinstance(rg, dict) and rg.get("entries_allowed") is False:
        out["risk_gate"] = rg.get("reason") or "Entries currently disabled by risk gate"
    return out

def larry_trade_ledger_summary(engine_state: Dict[str, Any], tracking_start: Optional[str] = None) -> Dict[str, Any]:
    """v49: Larry strategy P&L source of truth.

    The older dashboard tried to reconstruct clean P&L from Coinbase fills/opening books.
    Larry now writes actual trade economics into perp_trades_ledger.csv and
    last_completed_trade / last_realized_trade, so the dashboard should display those
    directly. This parser is tolerant of old ledger rows with fewer columns.
    """
    legacy_schema = [
        "timestamp", "reason", "signal_class", "action", "contracts",
        "before_signed", "target_signed", "after_signed",
        "mark_at_send", "fill_price", "slippage_bps", "gross_realized_pnl_usd",
        "fees_usd", "net_realized_pnl_usd", "ok", "client_order_id", "raw_order",
    ]
    modern_schema = [
        "timestamp", "reason", "signal_class", "action", "contracts",
        "before_signed", "target_signed", "after_signed",
        "mark_at_send", "fill_price", "slippage_bps", "gross_realized_pnl_usd",
        "fees_usd", "net_realized_pnl_usd", "ok", "client_order_id",
        "trade_intent", "execution_reason", "signal_reason",
        "target_before", "target_after", "sizing_rung_before", "sizing_rung_after",
        "raw_order",
    ]
    rows: List[Dict[str, Any]] = []
    raw = read_text_gcs(GCS_PERP_TRADES_LEDGER)
    if raw.strip():
        start_dt = parse_iso_utc(tracking_start) if tracking_start else None
        for vals in csv.reader(io.StringIO(raw)):
            if not vals or not vals[0] or vals[0].lower() == "timestamp":
                continue
            # Ledger schema evolved over time. Parse current v28+ intent columns when
            # present; otherwise fall back to the older economics schema.
            schema = modern_schema if len(vals) >= len(modern_schema) else legacy_schema
            rec = {schema[i]: vals[i] if i < len(vals) else "" for i in range(len(schema))}
            # Backfill newer research fields for old rows so the dashboard and Larry 2.0
            # datasets remain consistent.
            rec.setdefault("trade_intent", "")
            rec.setdefault("execution_reason", "")
            rec.setdefault("signal_reason", rec.get("reason", ""))
            rec.setdefault("target_before", rec.get("before_signed", ""))
            rec.setdefault("target_after", rec.get("after_signed", ""))
            rec.setdefault("sizing_rung_before", "")
            rec.setdefault("sizing_rung_after", "")
            t = parse_iso_utc(rec.get("timestamp"))
            rec["timestamp_et"] = format_iso_et(rec.get("timestamp"))
            if start_dt and t and t < start_dt:
                continue
            rec["contracts"] = safe_float(rec.get("contracts"), 0.0)
            rec["before_signed"] = safe_float(rec.get("before_signed"), 0.0)
            rec["target_signed"] = safe_float(rec.get("target_signed"), 0.0)
            rec["after_signed"] = safe_float(rec.get("after_signed"), 0.0)
            rec["mark_at_send"] = safe_float(rec.get("mark_at_send"), 0.0)
            rec["fill_price"] = safe_float(rec.get("fill_price"), 0.0)
            rec["slippage_bps"] = safe_float(rec.get("slippage_bps"), None)
            rec["gross_realized_pnl_usd"] = safe_float(rec.get("gross_realized_pnl_usd"), None)
            rec["fees_usd"] = safe_float(rec.get("fees_usd"), 0.0)
            rec["net_realized_pnl_usd"] = safe_float(rec.get("net_realized_pnl_usd"), None)
            rec["ok"] = parse_boolish(rec.get("ok"))
            rec["target_before"] = safe_float(rec.get("target_before"), rec.get("before_signed"))
            rec["target_after"] = safe_float(rec.get("target_after"), rec.get("after_signed"))
            rec["liquidity"] = liquidity_from_order_payload(rec.get("raw_order"))
            rows.append(rec)
    realized_rows = [r for r in rows if r.get("ok") and r.get("net_realized_pnl_usd") is not None]
    successful_rows = [r for r in rows if r.get("ok")]
    failed_rows = [r for r in rows if not r.get("ok")]
    gross = sum(safe_float(r.get("gross_realized_pnl_usd"), 0.0) for r in realized_rows)
    fees = sum(safe_float(r.get("fees_usd"), 0.0) for r in rows if r.get("ok"))
    net_realized = sum(safe_float(r.get("net_realized_pnl_usd"), 0.0) for r in realized_rows)

    # Running trade P&L and strategy statistics. Keep this based only on Larry's
    # ledger economics, not account equity reconstruction.
    running_net = 0.0
    for r in rows:
        if r.get("ok") and r.get("net_realized_pnl_usd") is not None:
            running_net += safe_float(r.get("net_realized_pnl_usd"), 0.0)
        r["running_net_pnl_usd"] = running_net

    wins = [safe_float(r.get("net_realized_pnl_usd"), 0.0) for r in realized_rows if safe_float(r.get("net_realized_pnl_usd"), 0.0) > 0]
    losses = [safe_float(r.get("net_realized_pnl_usd"), 0.0) for r in realized_rows if safe_float(r.get("net_realized_pnl_usd"), 0.0) < 0]
    total_trades = len(realized_rows)
    gross_wins = sum(wins)
    gross_losses_abs = abs(sum(losses))
    win_rate = (len(wins) / total_trades * 100.0) if total_trades else None
    profit_factor = (gross_wins / gross_losses_abs) if gross_losses_abs else (None if gross_wins == 0 else 999.0)
    avg_win = (gross_wins / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    expectancy = (net_realized / total_trades) if total_trades else None

    # Exposure time: approximate from Larry's post-trade signed position in the
    # ledger. This is intentionally simple and transparent. It tells the operator
    # whether under/outperformance came from being long, short, or flat.
    exposure_start = parse_iso_utc(tracking_start) if tracking_start else None
    first_trade_dt = None
    if rows:
        first_trade_dt = parse_iso_utc(rows[0].get("timestamp"))
    if exposure_start is None:
        exposure_start = first_trade_dt
    now_dt = datetime.now(timezone.utc)
    exposure_seconds = {"long": 0.0, "short": 0.0, "flat": 0.0}
    pos = 0.0
    cursor = exposure_start
    for r in rows:
        t = parse_iso_utc(r.get("timestamp"))
        if not t:
            continue
        if cursor and t > cursor:
            bucket = "long" if pos > 0 else ("short" if pos < 0 else "flat")
            exposure_seconds[bucket] += (t - cursor).total_seconds()
        pos = safe_float(r.get("after_signed"), pos)
        cursor = t
    if cursor and now_dt > cursor:
        bucket = "long" if pos > 0 else ("short" if pos < 0 else "flat")
        exposure_seconds[bucket] += (now_dt - cursor).total_seconds()
    total_exposure_seconds = sum(exposure_seconds.values()) or 0.0
    exposure_pct = {k: (v / total_exposure_seconds * 100.0) if total_exposure_seconds else None for k, v in exposure_seconds.items()}

    # BTC benchmark start price: use explicit capital-state value when available;
    # otherwise fall back to the first successful Larry trade mark/fill after the
    # tracking start. This makes the benchmark available without requiring a
    # separate historical price database.
    first_success = successful_rows[0] if successful_rows else None
    first_success_price = None
    if first_success:
        first_success_price = safe_float(first_success.get("mark_at_send"), None) or safe_float(first_success.get("fill_price"), None)

    slippages = [safe_float(r.get("slippage_bps"), None) for r in successful_rows]
    slippages = [x for x in slippages if x is not None]

    # Open bot-managed unrealized should come from the live exchange position, but only
    # if it is not manual/external monitor-only. This prevents manual book pollution.
    live = engine_state.get("exchange_position") if isinstance(engine_state.get("exchange_position"), dict) else {}
    manual = engine_state.get("manual_position_status") if isinstance(engine_state.get("manual_position_status"), dict) else {}
    manual_active = bool(manual.get("is_manual_or_external"))
    open_unrealized = 0.0 if manual_active else safe_float(live.get("unrealized_pnl"), 0.0)
    open_contracts = 0.0 if manual_active else safe_float(live.get("contracts"), 0.0)
    open_side = "MANUAL" if manual_active else (live.get("side") or "FLAT")
    net_total = net_realized + open_unrealized

    last_success = successful_rows[-1] if successful_rows else None
    last_realized = realized_rows[-1] if realized_rows else None
    return {
        "source": "larry_perp_trades_ledger_csv_plus_live_exchange_position",
        "ledger_path": GCS_PERP_TRADES_LEDGER,
        "tracking_start_timestamp": tracking_start,
        "rows_count": len(rows),
        "successful_trade_count": len(successful_rows),
        "failed_trade_count": len(failed_rows),
        "realized_trade_count": len(realized_rows),
        "gross_realized_pnl_usd": gross,
        "fees_usd": fees,
        "net_realized_pnl_usd": net_realized,
        "open_unrealized_pnl_usd": open_unrealized,
        "open_contracts": open_contracts,
        "open_side": open_side,
        "net_total_pnl_usd": net_total,
        "avg_slippage_bps": (sum(slippages) / len(slippages)) if slippages else None,
        "last_slippage_bps": slippages[-1] if slippages else None,
        "last_trade": last_success,
        "last_realized_trade": last_realized,
        "trade_stats": {
            "realized_trades": total_trades,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "avg_winner_usd": avg_win,
            "avg_loser_usd": avg_loss,
            "expectancy_usd": expectancy,
            "gross_wins_usd": gross_wins,
            "gross_losses_usd": sum(losses),
        },
        "execution_quality": {
            "sample_trades": len(successful_rows),
            "avg_slippage_bps": (sum(slippages) / len(slippages)) if slippages else None,
            "best_slippage_bps": max(slippages) if slippages else None,
            "worst_slippage_bps": min(slippages) if slippages else None,
            "maker_count": sum(1 for r in successful_rows if str(r.get("liquidity", "")).upper().startswith("MAKER")),
            "taker_count": sum(1 for r in successful_rows if str(r.get("liquidity", "")).upper().startswith("TAKER")),
            "unknown_count": sum(1 for r in successful_rows if str(r.get("liquidity", "")) in ("", "—")),
            "fees_usd": fees,
            "note": "Maker/Taker is inferred from ledger order payload when fill-level liquidity is unavailable. Current market IOC orders are treated as taker.",
        },
        "exposure_stats": {
            "long_pct": exposure_pct.get("long"),
            "short_pct": exposure_pct.get("short"),
            "flat_pct": exposure_pct.get("flat"),
            "total_hours": (total_exposure_seconds / 3600.0) if total_exposure_seconds else 0.0,
            "method": "Approximated from Larry ledger after_signed position over time",
        },
        "benchmark_start_btc_price_proxy": first_success_price,
        "benchmark_start_btc_price_source": "first_successful_larry_trade_mark_or_fill" if first_success_price else None,
        "recent_trades": list(reversed(successful_rows[-12:])),
        "all_trades": successful_rows[-500:],
        "trade_map_trades": successful_rows[-500:],
        "failed_trades": list(reversed(failed_rows[-5:])),
        "engine_last_completed_trade": engine_state.get("last_completed_trade"),
        "engine_last_realized_trade": engine_state.get("last_realized_trade"),
        "note": "Larry P&L is ledger net realized plus live open unrealized. Coinbase account equity remains a separate reference.",
    }



def trade_map_price_history(product_id: str) -> Dict[str, Any]:
    """Return lightweight BTC price history for dashboard trade-map overlays.

    The dashboard draws its own canvas chart. We provide hourly data for 1D/1W
    and daily data for longer views. Coinbase may return fewer than requested;
    the frontend handles sparse histories gracefully.
    """
    def points_from_df(df: Optional[pd.DataFrame]) -> List[Dict[str, Any]]:
        pts: List[Dict[str, Any]] = []
        if df is None or df.empty:
            return pts
        for _, row in df.iterrows():
            ts = int(row.get("start", 0) or 0)
            if not ts:
                continue
            iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            pts.append({
                "timestamp": iso,
                "timestamp_et": format_iso_et(iso),
                "price": safe_float(row.get("close"), 0.0),
                "high": safe_float(row.get("high"), 0.0),
                "low": safe_float(row.get("low"), 0.0),
                "volume": safe_float(row.get("volume"), 0.0),
            })
        return pts

    hourly = points_from_df(candles_df(product_id, "ONE_HOUR", 300))
    daily = points_from_df(candles_df(product_id, "ONE_DAY", 300))
    return {
        "source": "Coinbase candles via dashboard API",
        "product_id": product_id,
        "hourly": hourly,
        "daily": daily,
        "note": "1D/1W use hourly candles; 1M/YTD/12M use daily candles when available.",
    }

# ─────────────────────────────────────────────────────────────────────────────
# Coinbase auth/client
# ─────────────────────────────────────────────────────────────────────────────
def secret(name: str) -> str:
    sm = secretmanager.SecretManagerServiceClient()
    ref = f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"
    return sm.access_secret_version(request={"name": ref}).payload.data.decode("UTF-8")


def pem_from_secret(raw_key: str) -> str:
    key = (raw_key or "").strip().strip('"').replace("\\n", "\n")
    if "BEGIN" in key and "PRIVATE KEY" in key:
        return key if key.endswith("\n") else key + "\n"
    der_bytes = base64.b64decode(key)
    pk = load_der_private_key(der_bytes, password=None)
    return pk.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("utf-8")


def get_secrets() -> Dict[str, str]:
    global _secrets
    if _secrets:
        return _secrets
    try:
        api_key = secret("COINBASE_DASHBOARD_KEY").strip()
        raw_secret = secret("COINBASE_DASHBOARD_SECRET")
    except Exception:
        api_key = secret("COINBASE_API_KEY").strip()
        raw_secret = secret("COINBASE_SECRET")
    _secrets = {"api_key": api_key, "api_secret_pem": pem_from_secret(raw_secret)}
    return _secrets


def cb() -> RESTClient:
    global _cb
    if _cb is None:
        s = get_secrets()
        _cb = RESTClient(api_key=s["api_key"], api_secret=s["api_secret_pem"])
    return _cb


def jwt_for_rest(method: str, path: str) -> str:
    s = get_secrets()
    now = int(time.time())
    key_obj = load_pem_private_key(s["api_secret_pem"].encode(), password=None, backend=default_backend())
    return pyjwt.encode(
        {"sub": s["api_key"], "iss": "cdp", "nbf": now, "exp": now + 120, "uri": f"{method.upper()} api.coinbase.com{path}"},
        key_obj,
        algorithm="ES256",
        headers={"kid": s["api_key"], "nonce": uuid.uuid4().hex[:16]},
    )


def raw_get(path: str, params=None) -> Dict[str, Any]:
    try:
        token = jwt_for_rest("GET", path)
        r = requests.get(
            "https://api.coinbase.com" + path,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=12,
        )
        if not r.ok:
            return {"_error": f"HTTP {r.status_code}", "_body": r.text[:800]}
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


def jsonable(obj: Any) -> Any:
    """Best-effort conversion for Coinbase SDK response objects."""
    try:
        return json.loads(json.dumps(obj, default=lambda o: getattr(o, "__dict__", str(o))))
    except Exception:
        try:
            if hasattr(obj, "to_dict"):
                return obj.to_dict()
        except Exception:
            pass
        return str(obj)


def send_dashboard_telegram_message(text: str) -> bool:
    try:
        cfg = load_config()
        if cfg.get("SEND_TELEGRAM") is False:
            return False
        token = secret("TELEGRAM_BOT_TOKEN").strip()
        chat_id = secret("TELEGRAM_CHAT_ID").strip()
        if not token or not chat_id:
            return False
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": "true"},
            timeout=12,
        )
        return bool(r.ok)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# v77: ad-hoc performance SNAPSHOT email (operator-triggered, investor-shareable)
# Reuses the same data helpers as /api/data; read-only; behind PIN-gated session.
# Email sending mirrors the bot's SMTP pattern (EMAIL_PASSWORD in Secret Manager).
# ─────────────────────────────────────────────────────────────────────────────
def send_dashboard_email(subject: str, html_body: str, to_addr: Optional[str] = None) -> (bool, str):
    cfg = load_config()
    email_from = str(cfg.get("EMAIL_FROM") or "lockinlarry2@gmail.com")
    email_to = (to_addr or str(cfg.get("EMAIL_TO") or email_from)).strip()
    host = str(cfg.get("SMTP_HOST") or "smtp.gmail.com")
    port = int(safe_float(cfg.get("SMTP_PORT"), 465) or 465)
    try:
        pw = secret("EMAIL_PASSWORD")
    except Exception as e:
        app.logger.error("snapshot email: EMAIL_PASSWORD unavailable: %s", e)
        return False, "Email is not configured (EMAIL_PASSWORD secret missing)."
    if not pw:
        return False, "Email is not configured (EMAIL_PASSWORD empty)."
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Larry BTC Perp <{email_from}>"
    msg["To"] = email_to
    msg.attach(MIMEText("This is a Larry performance snapshot. View it in an HTML-capable email client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=25) as s:
                s.login(email_from, pw)
                s.sendmail(email_from, [email_to], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=25) as s:
                s.starttls()
                s.login(email_from, pw)
                s.sendmail(email_from, [email_to], msg.as_string())
        return True, email_to
    except Exception as e:
        app.logger.exception("snapshot email send failed")
        return False, f"Send failed: {e}"


def _snapshot_status(halt_state, risk_gate, engine_state, macro, open_contracts):
    """Return (label, hex_color, detail) describing whether Larry is primed to trade."""
    if bool((halt_state or {}).get("halt")):
        return "HALTED", "#f0565c", "Kill switch is active — no orders will be placed."
    mstat = (engine_state or {}).get("manual_position_status") or {}
    if mstat.get("is_manual_or_external") and not mstat.get("allow_bot_to_trade_position", True):
        return "MONITORING", "#4da3ff", "A manual position is under monitor-only supervision; the core engine is observing, not managing it."
    if (risk_gate or {}).get("entries_allowed") is False:
        return "PAUSED", "#f0c440", (risk_gate or {}).get("reason") or "Risk gate has paused new entries."
    phantom = str(((engine_state or {}).get("phantom") or {}).get("state") or "").upper()
    if phantom in ("PHANTOM_ARMED", "EXTENSION_CONFIRMED", "COMMITTED_ENTRY"):
        return "ARMED", "#2dd47e", "A setup is armed and awaiting closed-candle confirmation to execute."
    if open_contracts:
        return "IN POSITION", "#2dd47e", "Managing a live position with active ATR / trailing-stop protection."
    if not bool((macro or {}).get("gate_open")):
        return "SCANNING", "#94a1b6", "Risk gate open; macro filter not fully bullish, so long/bridge entries may be restricted."
    return "PRIMED", "#2dd47e", "Risk gate open and scanning for the next qualifying setup."


def build_snapshot() -> Dict[str, Any]:
    cfg = load_config()
    perp_product = cfg["PERP_PRODUCT_ID"]
    spot_product = cfg.get("SPOT_PRODUCT_ID") or "BTC-USDC"
    fallback = cfg.get("SPOT_FALLBACK_PRODUCT_ID") or "BTC-USD"
    perp_meta = product_details(perp_product)
    btc_price = best_mid(spot_product) or best_mid(fallback) or safe_float(perp_meta.get("price"), 0)
    contract_size = safe_float(perp_meta.get("contract_size"), safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01)) or 0.01
    fut_bal = futures_balance()
    spot_bal = spot_accounts(btc_price or 0)
    cap = load_capital_state()
    ex_positions = futures_positions(perp_product, contract_size)
    tracking_start = cap.get("tracking_start_timestamp")
    fills = recent_fills(perp_product, 100, tracking_start)
    current_mark = 0.0
    if ex_positions:
        current_mark = safe_float(ex_positions[0].get("current_price"), 0.0) or safe_float(btc_price, 0.0)
    clean_book = clean_book_from_opening(cap, fills, current_mark, perp_product, ex_positions)
    pnl = pnl_summary(fut_bal, fills, ex_positions, clean_book)
    capital = combined_capital(spot_bal, fut_bal, cap)
    engine_state = read_json(GCS_PERP_STATE, {}) or {}
    if not isinstance(engine_state, dict):
        engine_state = {}
    larry = larry_trade_ledger_summary(engine_state, tracking_start)
    macro_df = candles_df(fallback, cfg.get("MACRO_GRANULARITY", "ONE_HOUR"), 300)
    mac = macro_snapshot(macro_df)
    risk_gate = live_risk_gate_state(engine_state, cfg, mac)
    halt_state = read_json(GCS_BOT_HALT, {}) or {}

    ts = larry.get("trade_stats", {}) or {}
    ex = larry.get("exposure_stats", {}) or {}
    start_capital = safe_float(capital.get("starting_combined_capital"), None)
    larry_total = safe_float(larry.get("net_total_pnl_usd"), 0.0)
    return_pct = (larry_total / start_capital * 100.0) if start_capital not in (None, 0) else None

    # Benchmark vs passive BTC bought with the same starting capital.
    start_btc = (safe_float(cap.get("starting_btc_price"), None)
                 or safe_float(cap.get("benchmark_start_btc_price"), None)
                 or safe_float(larry.get("benchmark_start_btc_price_proxy"), None))
    bench_ret = None
    alpha_pct = None
    if start_capital not in (None, 0) and start_btc not in (None, 0) and btc_price:
        bench_val = (start_capital / start_btc) * btc_price
        bench_ret = (bench_val - start_capital) / start_capital * 100.0
        if return_pct is not None:
            alpha_pct = return_pct - bench_ret

    pos = ex_positions[0] if ex_positions else None
    open_contracts = safe_float((pos or {}).get("contracts"), 0.0) if pos else 0.0
    label, color, detail = _snapshot_status(halt_state, risk_gate, engine_state, mac, open_contracts)

    now = datetime.now(TZ)
    return {
        "as_of": now.strftime("%A, %B %-d, %Y · %-I:%M %p ET"),
        "as_of_short": now.strftime("%b %-d, %Y"),
        "btc_price": btc_price,
        "status_label": label, "status_color": color, "status_detail": detail,
        "net_pnl_usd": larry_total,
        "return_pct": return_pct,
        "equity_usd": (start_capital + larry_total) if start_capital is not None else None,
        "start_capital_usd": start_capital,
        "benchmark_return_pct": bench_ret,
        "alpha_pct": alpha_pct,
        "realized_trades": ts.get("realized_trades"),
        "win_rate_pct": ts.get("win_rate_pct"),
        "profit_factor": ts.get("profit_factor"),
        "expectancy_usd": ts.get("expectancy_usd"),
        "avg_slippage_bps": larry.get("avg_slippage_bps"),
        "long_pct": ex.get("long_pct"), "short_pct": ex.get("short_pct"), "flat_pct": ex.get("flat_pct"),
        "open_side": (pos or {}).get("side") if pos else "FLAT",
        "open_contracts": open_contracts,
        "open_avg_entry": safe_float((pos or {}).get("avg_entry_price"), 0.0) if pos else 0.0,
        "open_mark": current_mark if pos else 0.0,
        "open_unrealized_usd": safe_float((pos or {}).get("book_unrealized_pnl", (pos or {}).get("exchange_unrealized_pnl")), 0.0) if pos else 0.0,
        "macro_state": mac.get("regime") or ("BULLISH" if mac.get("gate_open") else "MIXED"),
        "product": perp_product,
    }


def render_snapshot_email_html(s: Dict[str, Any]) -> str:
    def usd(n):
        if n is None:
            return "—"
        n = float(n)
        return ("-" if n < 0 else "") + "$" + f"{abs(n):,.2f}"
    def pct(n, dp=1):
        return "—" if n is None else f"{float(n):,.{dp}f}%"
    def signed_pct(n, dp=1):
        if n is None:
            return "—"
        return ("+" if float(n) >= 0 else "") + f"{float(n):,.{dp}f}%"
    def numf(n, dp=2):
        return "—" if n is None else f"{float(n):,.{dp}f}"
    def col(n):
        if n is None:
            return "#e9eef5"
        return "#2dd47e" if float(n) >= 0 else "#f0565c"

    lp = max(0.0, min(100.0, safe_float(s.get("long_pct"), 0.0)))
    sp = max(0.0, min(100.0, safe_float(s.get("short_pct"), 0.0)))
    fp = max(0.0, min(100.0, safe_float(s.get("flat_pct"), 0.0)))
    tot = lp + sp + fp or 1.0
    lw, sw, fw = round(lp / tot * 100), round(sp / tot * 100), round(fp / tot * 100)

    open_line = "Flat — no open position"
    if s.get("open_side") and str(s.get("open_side")).upper() not in ("FLAT", ""):
        open_line = (f"{s['open_side']} {numf(s['open_contracts'],0)} contracts &nbsp;·&nbsp; "
                     f"entry {usd(s['open_avg_entry'])} &nbsp;·&nbsp; mark {usd(s['open_mark'])} &nbsp;·&nbsp; "
                     f"<span style=\"color:{col(s['open_unrealized_usd'])}\">unrealized {usd(s['open_unrealized_usd'])}</span>")

    def kpi(label, value, vcolor="#e9eef5", sub=""):
        subhtml = f'<div style="font-size:11px;color:#5f6a7c;margin-top:3px">{sub}</div>' if sub else ""
        return (f'<td width="50%" style="padding:7px">'
                f'<div style="background:#141a23;border:1px solid #212a37;border-radius:12px;padding:14px 16px">'
                f'<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#94a1b6;font-weight:bold">{label}</div>'
                f'<div style="font-size:22px;font-weight:bold;color:{vcolor};margin-top:6px">{value}</div>{subhtml}</div></td>')

    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#07080b">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#07080b">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;font-family:Arial,Helvetica,sans-serif">

  <tr><td style="background:#141a23;border:1px solid #212a37;border-top:3px solid #f7931a;border-radius:16px 16px 0 0;padding:24px 26px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:22px;font-weight:bold;color:#e9eef5">
        <span style="display:inline-block;width:34px;height:34px;background:#f7931a;color:#1c1104;border-radius:9px;text-align:center;line-height:34px;font-size:19px;font-weight:bold;vertical-align:middle;margin-right:9px">&#8383;</span>
        Larry BTC Perp
      </td>
      <td align="right" style="font-size:12px;color:#94a1b6">{s['as_of']}</td>
    </tr></table>
    <div style="margin-top:16px">
      <span style="display:inline-block;background:{s['status_color']};color:#07080b;font-size:12px;font-weight:bold;letter-spacing:.06em;padding:6px 12px;border-radius:7px">{s['status_label']}</span>
      <span style="font-size:13px;color:#94a1b6;margin-left:10px">{s['status_detail']}</span>
    </div>
    <div style="font-size:12px;color:#5f6a7c;margin-top:12px">BTC {usd(s['btc_price'])} &nbsp;·&nbsp; Macro regime: {s['macro_state']} &nbsp;·&nbsp; {s['product']}</div>
  </td></tr>

  <tr><td style="background:#0e1218;border-left:1px solid #212a37;border-right:1px solid #212a37;padding:10px 18px 4px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>{kpi("Net P&amp;L (since inception)", usd(s['net_pnl_usd']), col(s['net_pnl_usd']), "Ledger realized + open unrealized, net of fees")}
          {kpi("Return on capital", signed_pct(s['return_pct']), col(s['return_pct']), f"Alpha vs BTC buy-and-hold: {signed_pct(s['alpha_pct']) if s['alpha_pct'] is not None else '—'}")}</tr>
      <tr>{kpi("Win rate", pct(s['win_rate_pct'],0), "#e9eef5", f"Profit factor {numf(s['profit_factor'],2)} · expectancy {usd(s['expectancy_usd'])}/trade")}
          {kpi("Trades executed", numf(s['realized_trades'],0), "#e9eef5", f"Avg slippage {numf(s['avg_slippage_bps'],1)} bps")}</tr>
    </table>
  </td></tr>

  <tr><td style="background:#0e1218;border-left:1px solid #212a37;border-right:1px solid #212a37;padding:8px 26px 6px">
    <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#94a1b6;font-weight:bold;margin-bottom:8px">Current Position</div>
    <div style="font-size:14px;color:#e9eef5;font-weight:bold">{open_line}</div>
  </td></tr>

  <tr><td style="background:#0e1218;border-left:1px solid #212a37;border-right:1px solid #212a37;padding:14px 26px 18px">
    <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#94a1b6;font-weight:bold;margin-bottom:8px">Market Exposure Since Inception</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-radius:8px;overflow:hidden"><tr>
      <td width="{lw}%" height="26" style="background:#2dd47e"></td>
      <td width="{sw}%" height="26" style="background:#f0565c"></td>
      <td width="{fw}%" height="26" style="background:#212a37"></td>
    </tr></table>
    <div style="font-size:12px;color:#94a1b6;margin-top:8px">
      <span style="color:#2dd47e">&#9632;</span> Long {pct(s['long_pct'],0)} &nbsp;&nbsp;
      <span style="color:#f0565c">&#9632;</span> Short {pct(s['short_pct'],0)} &nbsp;&nbsp;
      <span style="color:#94a1b6">&#9632;</span> Flat {pct(s['flat_pct'],0)}
    </div>
  </td></tr>

  <tr><td style="background:#0e1218;border:1px solid #212a37;border-top:0;border-radius:0 0 16px 16px;padding:16px 26px 20px">
    <div style="font-size:11px;color:#5f6a7c;line-height:1.6">
      Systematic Bitcoin perpetual-futures engine · rule-based, bidirectional, risk-management-first. Figures are drawn live from the exchange-reconciled ledger at the time shown. Trading digital-asset derivatives involves substantial risk of loss; past performance is not indicative of future results. Prepared for discussion purposes only — not an offer or solicitation.
    </div>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    """Generate and email an ad-hoc branded performance snapshot. Session-gated."""
    try:
        body = request.get_json(silent=True) or {}
        to_addr = (body.get("to") or "").strip() or None
        if to_addr and ("@" not in to_addr or " " in to_addr or "." not in to_addr):
            return jsonify({"ok": False, "error": "That doesn't look like a valid email address."}), 400
        snap = build_snapshot()
        html = render_snapshot_email_html(snap)
        ok, info = send_dashboard_email(f"Larry BTC Perp — Performance Snapshot ({snap['as_of_short']})", html, to_addr)
        if not ok:
            return jsonify({"ok": False, "error": info}), 500
        try:
            send_dashboard_telegram_message(f"📸 Snapshot emailed to {info} — status {snap['status_label']}, net P&L {snap['net_pnl_usd']:.2f}.")
        except Exception:
            pass
        return jsonify({"ok": True, "sent_to": info, "status": snap["status_label"]})
    except Exception as e:
        app.logger.exception("snapshot failed")
        return jsonify({"ok": False, "error": str(e)}), 500


def _coinbase_order_success(payload: Any) -> bool:
    """Return True only when Coinbase explicitly reports order success.
    The SDK can return HTTP 200 with success=false for preview/execution failures,
    so emergency flatten must not treat transport success as order success.
    """
    d = jsonable(payload)
    if isinstance(d, dict):
        if d.get("success") is False:
            return False
        if isinstance(d.get("response"), dict) and d["response"].get("success") is False:
            return False
        if d.get("error_response") or (isinstance(d.get("response"), dict) and d["response"].get("error_response")):
            return False
    return True


def _coinbase_order_error(payload: Any) -> str:
    d = jsonable(payload)
    try:
        if isinstance(d, dict):
            err = d.get("error_response") or (d.get("response", {}) or {}).get("error_response")
            if err:
                return json.dumps(err)[:900]
    except Exception:
        pass
    return "Coinbase did not confirm order success"


def place_dashboard_market_order(product_id: str, side: str, contracts: float, reason: str) -> Dict[str, Any]:
    """Emergency dashboard market order helper used only by /api/emergency_flatten.
    It returns ok=True only if Coinbase explicitly confirms success.
    """
    side = str(side or "").upper()
    if side not in ("BUY", "SELL") or not contracts or contracts <= 0:
        return {"ok": True, "skipped": True, "reason": "no_order_needed"}
    client_order_id = f"dashboard-emergency-flat-{int(time.time())}-{side.lower()}-{str(contracts).replace('.', '_')}"
    size_str = str(int(contracts)) if float(contracts).is_integer() else str(contracts)
    try:
        if side == "BUY":
            res = cb().market_order_buy(client_order_id=client_order_id, product_id=product_id, base_size=size_str)
        else:
            res = cb().market_order_sell(client_order_id=client_order_id, product_id=product_id, base_size=size_str)
        if not _coinbase_order_success(res):
            return {"ok": False, "error": _coinbase_order_error(res), "response": jsonable(res), "client_order_id": client_order_id, "product_id": product_id, "side": side, "contracts": contracts}
        return {"ok": True, "response": jsonable(res), "client_order_id": client_order_id, "product_id": product_id, "side": side, "contracts": contracts}
    except TypeError:
        try:
            res = cb().create_order(
                client_order_id=client_order_id,
                product_id=product_id,
                side=side,
                order_configuration={"market_market_ioc": {"base_size": size_str}},
            )
            if not _coinbase_order_success(res):
                return {"ok": False, "error": _coinbase_order_error(res), "response": jsonable(res), "client_order_id": client_order_id, "product_id": product_id, "side": side, "contracts": contracts}
            return {"ok": True, "response": jsonable(res), "client_order_id": client_order_id, "product_id": product_id, "side": side, "contracts": contracts}
        except Exception as e:
            return {"ok": False, "error": str(e), "client_order_id": client_order_id, "product_id": product_id, "side": side, "contracts": contracts}
    except Exception as e:
        return {"ok": False, "error": str(e), "client_order_id": client_order_id, "product_id": product_id, "side": side, "contracts": contracts}

# ─────────────────────────────────────────────────────────────────────────────
# Config / heartbeat / market data
# ─────────────────────────────────────────────────────────────────────────────
def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    live = read_first_json(GCS_CONFIG_CANDIDATES, {})
    aliases = {
        "SPOT_PRODUCT_ID": ["SPOT_PRODUCT_ID", "SPOT_SYMBOL", "SPOT_PRIMARY_PRODUCT_ID"],
        "SPOT_FALLBACK_PRODUCT_ID": ["SPOT_FALLBACK_PRODUCT_ID", "SPOT_BACKUP_PRODUCT_ID"],
        "ENABLE_SPOT_BTC_TRADING": ["ENABLE_SPOT_BTC_TRADING"],
        "ENABLE_SPOT_BRIDGE_PERP_BUYS": ["ENABLE_SPOT_BRIDGE_PERP_BUYS"],
        "PERP_PRODUCT_ID": ["PERP_PRODUCT_ID", "PRODUCT_ID", "SYMBOL"],
        "CONTRACT_SIZE_BTC": ["CONTRACT_SIZE_BTC"],
        "MAX_CONVICTION_CONTRACTS": ["MAX_CONVICTION_CONTRACTS"],
        "CONTRACTS_PER_TRADE": ["CONTRACTS_PER_TRADE"],
        "CONTRACTS_PER_TRADE_FULL": ["CONTRACTS_PER_TRADE_FULL"],
        "CONTRACTS_PER_TRADE_PARTIAL": ["CONTRACTS_PER_TRADE_PARTIAL"],
        "CONTRACTS_PER_TRADE_PROBE": ["CONTRACTS_PER_TRADE_PROBE"],
        "SCORE4_MACRO_OVERRIDE_ENABLED": ["SCORE4_MACRO_OVERRIDE_ENABLED"],
        "MACRO_BLOCKED_PROBE_CONTRACTS": ["MACRO_BLOCKED_PROBE_CONTRACTS"],
        "TP1_PCT": ["TP1_PCT"],
        "TP1_FRACTION": ["TP1_FRACTION"],
        "FUNDING_SIZE_REDUCE_AT": ["FUNDING_SIZE_REDUCE_AT"],
        "MANUAL_POSITION_MODE": ["MANUAL_POSITION_MODE"],
        "SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL": ["SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL"],
        "EMAIL_INCLUDE_RAW_ORDER": ["EMAIL_INCLUDE_RAW_ORDER"],
        "SIGNAL_GRANULARITY": ["SIGNAL_GRANULARITY", "CANDLE_TIMEFRAME"],
        "MACRO_GRANULARITY": ["MACRO_GRANULARITY", "CANDLE_TIMEFRAME_1H"],
        "RSI_PERIOD": ["RSI_PERIOD"],
        "RSI_BUY_FLOOR": ["RSI_BUY_FLOOR", "RSI_LONG_FLOOR"],
        "RSI_BUY_THRESHOLD": ["RSI_BUY_THRESHOLD", "RSI_LONG_THRESHOLD", "BUY_RSI_THRESHOLD"],
        "RSI_EXIT_THRESHOLD": ["RSI_EXIT_THRESHOLD"],
        "BB_PERIOD": ["BB_PERIOD"],
        "BB_STD": ["BB_STD"],
        "VOLUME_AVG_PERIOD": ["VOLUME_AVG_PERIOD"],
        "VOLUME_MULTIPLIER": ["VOLUME_MULTIPLIER"],
        "STOCH_RSI_PERIOD": ["STOCH_RSI_PERIOD"],
        "STOCH_RSI_THRESHOLD": ["STOCH_RSI_THRESHOLD", "STOCH_RSI_LONG_THRESHOLD"],
        "ATR_PERIOD": ["ATR_PERIOD"],
        "ATR_STOP_MULTIPLIER": ["ATR_STOP_MULTIPLIER"],
        "TSL_ACTIVATION_PCT": ["TSL_ACTIVATION_PCT"],
        "TSL_TRAIL_PCT": ["TSL_TRAIL_PCT"],
        "PHANTOM_EXTENSION_PCT": ["PHANTOM_EXTENSION_PCT"],
        "FUNDING_LONG_MAX": ["FUNDING_LONG_MAX", "MAX_LONG_FUNDING"],
        "FUNDING_SHORT_MIN": ["FUNDING_SHORT_MIN", "MIN_SHORT_FUNDING"],
        "SPOT_TRANCHE_TARGETS_PCT": ["SPOT_TRANCHE_TARGETS_PCT"],
        "MAX_EFFECTIVE_LEVERAGE": ["MAX_EFFECTIVE_LEVERAGE"],
        "MIN_FUTURES_EQUITY_BUFFER_USD": ["MIN_FUTURES_EQUITY_BUFFER_USD"],
        "DAILY_STOP_LIMIT": ["DAILY_STOP_LIMIT", "MAX_DAILY_STOPS"],
        "LOSS_STREAK_LIMIT": ["LOSS_STREAK_LIMIT", "MAX_CONSECUTIVE_LOSSES"],
        "STREAK_PAUSE_HOURS": ["STREAK_PAUSE_HOURS"],
        "SPOT_ENTRY_COOLDOWN_SEC": ["SPOT_ENTRY_COOLDOWN_SEC"],
        "PERP_ENTRY_COOLDOWN_SEC": ["PERP_ENTRY_COOLDOWN_SEC"],
        "BRIDGE_ENTRY_COOLDOWN_SEC": ["BRIDGE_ENTRY_COOLDOWN_SEC"],
        "SPOT_MAX_LADDER_UNITS": ["SPOT_MAX_LADDER_UNITS", "MAX_SPOT_POSITIONS"],
        "PERP_STRATEGY_SLOTS": ["PERP_STRATEGY_SLOTS", "MAX_PERP_POSITIONS"],
    }
    if live:
        for target, names in aliases.items():
            for name in names:
                if name in live and live.get(name) not in (None, ""):
                    cfg[target] = live.get(name)
                    break
        cfg["CONFIG_SOURCE"] = live.get("_source")
        cfg["CONFIG_UPDATED_AT"] = live.get("updated_at") or live.get("last_updated")
    else:
        cfg["CONFIG_SOURCE"] = "DEFAULT_CONFIG fallback"
        cfg["CONFIG_UPDATED_AT"] = None

    # Normalize common numeric fields.
    for k in ["CONTRACT_SIZE_BTC", "ATR_STOP_MULTIPLIER", "TSL_ACTIVATION_PCT", "TSL_TRAIL_PCT", "PHANTOM_EXTENSION_PCT", "FUNDING_LONG_MAX", "FUNDING_SHORT_MIN", "FUNDING_SIZE_REDUCE_AT", "TP1_PCT", "TP1_FRACTION", "MAX_EFFECTIVE_LEVERAGE", "MIN_FUTURES_EQUITY_BUFFER_USD", "RSI_BUY_FLOOR", "RSI_BUY_THRESHOLD", "BB_STD", "VOLUME_MULTIPLIER", "STOCH_RSI_THRESHOLD"]:
        cfg[k] = safe_float(cfg.get(k), DEFAULT_CONFIG[k])
    for k in ["MAX_CONVICTION_CONTRACTS", "CONTRACTS_PER_TRADE", "CONTRACTS_PER_TRADE_FULL", "CONTRACTS_PER_TRADE_PARTIAL", "CONTRACTS_PER_TRADE_PROBE", "MACRO_BLOCKED_PROBE_CONTRACTS", "RSI_PERIOD", "BB_PERIOD", "VOLUME_AVG_PERIOD", "STOCH_RSI_PERIOD", "ATR_PERIOD", "SPOT_MAX_LADDER_UNITS", "PERP_STRATEGY_SLOTS", "DAILY_STOP_LIMIT", "LOSS_STREAK_LIMIT", "STREAK_PAUSE_HOURS", "SPOT_ENTRY_COOLDOWN_SEC", "PERP_ENTRY_COOLDOWN_SEC", "BRIDGE_ENTRY_COOLDOWN_SEC"]:
        cfg[k] = safe_int(cfg.get(k), DEFAULT_CONFIG[k])
    if isinstance(cfg.get("SPOT_TRANCHE_TARGETS_PCT"), str):
        cfg["SPOT_TRANCHE_TARGETS_PCT"] = [safe_float(x, 0) for x in cfg["SPOT_TRANCHE_TARGETS_PCT"].replace(";", ",").split(",") if str(x).strip()]
    if not isinstance(cfg.get("SPOT_TRANCHE_TARGETS_PCT"), list):
        cfg["SPOT_TRANCHE_TARGETS_PCT"] = list(DEFAULT_CONFIG["SPOT_TRANCHE_TARGETS_PCT"])
    return cfg


def parse_ts(ts: Optional[str]):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def heartbeat_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    hb = read_first_json(GCS_HEARTBEAT_CANDIDATES, {})
    dt = parse_ts(hb.get("ts"))
    age = None if not dt else int((datetime.now(timezone.utc) - dt).total_seconds())
    stale = int(cfg.get("HEARTBEAT_STALE_SECONDS") or DEFAULT_CONFIG["HEARTBEAT_STALE_SECONDS"])
    down = int(cfg.get("HEARTBEAT_DOWN_SECONDS") or DEFAULT_CONFIG["HEARTBEAT_DOWN_SECONDS"])
    if age is None:
        health, color = "NO SIGNAL", "bad"
    elif age <= stale:
        health, color = "LIVE", "good"
    elif age <= down:
        health, color = "STALE", "warn"
    else:
        health, color = "DOWN", "bad"
    return {
        "health": health,
        "color": color,
        "age_secs": age,
        "ts": hb.get("ts"),
        "state": hb.get("state", "UNKNOWN"),
        "status": hb.get("status"),
        "price": hb.get("price"),
        "has_position": bool(hb.get("has_position")),
        "source": hb.get("_source"),
    }


def product_obj(product_id: str):
    try:
        return cb().get_product(product_id)
    except Exception:
        return None


def product_details(product_id: str) -> Dict[str, Any]:
    p = product_obj(product_id)
    if not p:
        return {"product_id": product_id, "error": "unavailable"}
    fcm = attr_or_key(p, "fcm_trading_session_details", None)
    details = attr_or_key(p, "future_product_details", None)
    return {
        "product_id": attr_or_key(p, "product_id", product_id),
        "display_name": attr_or_key(p, "display_name"),
        "product_type": attr_or_key(p, "product_type"),
        "price": safe_float(attr_or_key(p, "price")),
        "mid_market_price": safe_float(attr_or_key(p, "mid_market_price")),
        "base_increment": attr_or_key(p, "base_increment"),
        "base_min_size": attr_or_key(p, "base_min_size"),
        "trading_disabled": attr_or_key(p, "trading_disabled"),
        "session_open": attr_or_key(fcm, "is_session_open") if fcm else None,
        "session_state": attr_or_key(fcm, "session_state") if fcm else None,
        "funding_rate": safe_float(attr_or_key(details, "funding_rate")) if details else None,
        "funding_time": attr_or_key(details, "funding_time") if details else None,
        "settlement_price": safe_float(attr_or_key(details, "settlement_price")) if details else None,
        "index_price": safe_float(attr_or_key(details, "index_price")) if details else None,
        "contract_size": safe_float(attr_or_key(details, "contract_size"), None) if details else None,
        "intraday_margin_long": safe_float(attr_or_key(attr_or_key(details, "intraday_margin_rate", {}), "long_margin_rate")) if details else None,
        "overnight_margin_long": safe_float(attr_or_key(attr_or_key(details, "overnight_margin_rate", {}), "long_margin_rate")) if details else None,
    }


def best_mid(product_id: str) -> Optional[float]:
    try:
        bba = cb().get_best_bid_ask(product_ids=[product_id])
        pbs = getattr(bba, "pricebooks", None) or []
        if pbs:
            pb = pbs[0]
            bids = attr_or_key(pb, "bids", []) or []
            asks = attr_or_key(pb, "asks", []) or []
            bid = safe_float(attr_or_key(bids[0], "price")) if bids else None
            ask = safe_float(attr_or_key(asks[0], "price")) if asks else None
            if bid and ask:
                return (bid + ask) / 2
            return bid or ask
    except Exception:
        pass
    p = product_details(product_id)
    return p.get("mid_market_price") or p.get("price")


def candles_df(product_id: str, granularity: str, limit: int = 300) -> Optional[pd.DataFrame]:
    try:
        seconds = {
            "ONE_MINUTE": 60,
            "FIVE_MINUTE": 300,
            "FIFTEEN_MINUTE": 900,
            "THIRTY_MINUTE": 1800,
            "ONE_HOUR": 3600,
            "TWO_HOUR": 7200,
            "SIX_HOUR": 21600,
            "ONE_DAY": 86400,
            "15m": 900,
            "1h": 3600,
        }.get(str(granularity), 900)
        end_ts = int(time.time())
        start_ts = end_ts - min(limit, 300) * seconds
        resp = cb().get_candles(product_id=product_id, start=str(start_ts), end=str(end_ts), granularity=granularity)
        candles = getattr(resp, "candles", None) or []
        rows = []
        for c in candles:
            rows.append({
                "start": safe_int(attr_or_key(c, "start")),
                "open": safe_float(attr_or_key(c, "open"), 0),
                "high": safe_float(attr_or_key(c, "high"), 0),
                "low": safe_float(attr_or_key(c, "low"), 0),
                "close": safe_float(attr_or_key(c, "close"), 0),
                "volume": safe_float(attr_or_key(c, "volume"), 0),
            })
        df = pd.DataFrame(rows).dropna()
        if df.empty:
            return None
        return df.sort_values("start").reset_index(drop=True)
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────────────────────
# Account, fills, positions
# ─────────────────────────────────────────────────────────────────────────────
def spot_accounts(btc_price: float) -> Dict[str, Any]:
    """Return Coinbase Spot treasury balances.

    Important: Coinbase accounts can be paginated and USDC may not appear on the
    first SDK page when the account has many historical wallets. Use the raw
    brokerage accounts endpoint with a large page size first, then fall back to
    the SDK. Sum available + hold for USD/USDC/BTC so the treasury view matches
    the account more closely.
    """
    result = {
        "USD": 0.0,
        "USDC": 0.0,
        "BTC": 0.0,
        "BTC_USD_VALUE": 0.0,
        "SPOT_TREASURY_VALUE": 0.0,
        "raw_count": 0,
        "source": "raw_accounts_endpoint",
        "error": None,
    }

    def consume_rows(rows):
        for a in rows or []:
            cur = attr_or_key(a, "currency")
            if not cur:
                bal_obj = attr_or_key(a, "available_balance", {}) or {}
                cur = attr_or_key(bal_obj, "currency")
            cur = str(cur or "").upper()
            bal = money_obj_value(attr_or_key(a, "available_balance"), 0.0)
            hold = money_obj_value(attr_or_key(a, "hold"), 0.0)
            amt = (bal or 0.0) + (hold or 0.0)
            if cur == "USD":
                result["USD"] += amt
            elif cur == "USDC":
                result["USDC"] += amt
            elif cur == "BTC":
                result["BTC"] += amt

    try:
        # Prefer raw endpoint because it lets us request a much larger page and
        # avoids missing USDC when the SDK returns only the first account page.
        cursor = None
        total_rows = 0
        for _ in range(6):
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            raw = raw_get("/api/v3/brokerage/accounts", params=params)
            if raw.get("_error"):
                raise RuntimeError(raw.get("_error"))
            rows = raw.get("accounts", []) or []
            total_rows += len(rows)
            consume_rows(rows)
            cursor = raw.get("cursor") or raw.get("next_cursor")
            if not cursor:
                break
        result["raw_count"] = total_rows
    except Exception as raw_error:
        # SDK fallback.
        result["source"] = "sdk_get_accounts_fallback"
        result["error"] = str(raw_error)
        try:
            try:
                accts = cb().get_accounts(limit=250)
            except TypeError:
                accts = cb().get_accounts()
            rows = getattr(accts, "accounts", None) or attr_or_key(accts, "accounts", []) or []
            result["raw_count"] = len(rows)
            consume_rows(rows)
        except Exception as e:
            result["error"] = f"raw={raw_error}; sdk={e}"

    # v72 safety fix, confirmed correct in v74: the generic brokerage accounts
    # endpoint includes USD/USDC balances that are already represented in the
    # Coinbase futures balance summary for this account (operator-confirmed: futures
    # base equity already includes this cash). Including it here would double-count
    # account equity, so only live BTC spot mark value is included in combined equity.
    result["RAW_USD_USDC_DISCOVERED"] = (result.get("USD") or 0.0) + (result.get("USDC") or 0.0)
    result["USD_DISPLAY_ONLY"] = result.get("USD", 0.0)
    result["USDC_DISPLAY_ONLY"] = result.get("USDC", 0.0)
    result["USD"] = 0.0
    result["USDC"] = 0.0
    result["BTC_USD_VALUE"] = result["BTC"] * (btc_price or 0.0)
    result["SPOT_TREASURY_VALUE"] = result["BTC_USD_VALUE"]
    result["source"] = str(result.get("source") or "") + ": btc_only_no_cash_double_count_v72"
    result["note"] = "Spot equity includes live BTC only. USD/USDC cash is excluded here to avoid double-counting futures cash."
    return result


def futures_balance() -> Dict[str, Any]:
    raw = raw_get("/api/v3/brokerage/cfm/balance_summary")
    if "_error" in raw:
        return {"error": raw.get("_error"), "raw": raw}
    bs = raw.get("balance_summary", raw)
    def v(name): return money_obj_value(bs.get(name), 0.0)
    out = {
        "futures_buying_power": v("futures_buying_power"),
        "total_usd_balance": v("total_usd_balance"),
        "cbi_usd_balance": v("cbi_usd_balance"),
        "cfm_usd_balance": v("cfm_usd_balance"),
        "open_orders_hold": v("total_open_orders_hold_amount"),
        "unrealized_pnl": v("unrealized_pnl"),
        "daily_realized_pnl": v("daily_realized_pnl"),
        "initial_margin": v("initial_margin"),
        "available_margin": v("available_margin"),
        "liquidation_threshold": v("liquidation_threshold"),
        "liquidation_buffer_amount": v("liquidation_buffer_amount"),
        "funding_pnl": v("funding_pnl"),
        "raw": bs,
    }
    im = attr_or_key(bs, "intraday_margin_window_measure", {}) or {}
    om = attr_or_key(bs, "overnight_margin_window_measure", {}) or {}
    out["intraday_initial_margin"] = safe_float(attr_or_key(im, "initial_margin"), None)
    out["intraday_maintenance_margin"] = safe_float(attr_or_key(im, "maintenance_margin"), None)
    out["overnight_initial_margin"] = safe_float(attr_or_key(om, "initial_margin"), None)
    out["overnight_maintenance_margin"] = safe_float(attr_or_key(om, "maintenance_margin"), None)
    return out


def futures_positions(perp_product: str, contract_size: float) -> List[Dict[str, Any]]:
    try:
        resp = cb().list_futures_positions()
        rows = getattr(resp, "positions", None) or attr_or_key(resp, "positions", []) or []
        out = []
        for p in rows:
            pid = attr_or_key(p, "product_id")
            if pid != perp_product:
                continue
            contracts = safe_float(attr_or_key(p, "number_of_contracts"), 0)
            avg = safe_float(attr_or_key(p, "avg_entry_price"), 0)
            cur = safe_float(attr_or_key(p, "current_price"), 0)
            side = attr_or_key(p, "side", "")
            direction_mult = 1 if side == "LONG" else -1
            gross_unr = (cur - avg) * contracts * contract_size * direction_mult if avg and cur else 0
            out.append({
                "product_id": pid,
                "side": side,
                "contracts": contracts,
                "contract_size_btc": contract_size,
                "btc_exposure": contracts * contract_size,
                "avg_entry_price": avg,
                "current_price": cur,
                "exchange_unrealized_pnl": safe_float(attr_or_key(p, "unrealized_pnl"), 0),
                "estimated_gross_unrealized_pnl": gross_unr,
                "daily_realized_pnl": safe_float(attr_or_key(p, "daily_realized_pnl"), 0),
                "expiration_time": attr_or_key(p, "expiration_time"),
            })
        return out
    except Exception:
        return []


def apply_perp_avg_entry_override(positions: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Optionally force the displayed/clean-book avg entry for the live perp position.

    This is a practical control for cases where the Coinbase UI-reported adjusted
    basis differs from a cached/stale API field. It never changes actual exchange
    exposure or order sizing; it only affects dashboard basis/P&L display.
    """
    try:
        enabled = bool(cfg.get("PERP_AVG_ENTRY_OVERRIDE_ENABLED", False))
        override = safe_float(cfg.get("PERP_AVG_ENTRY_OVERRIDE_USD"), 0.0)
        if not enabled or override <= 0 or not positions:
            return positions
        out = []
        for p in positions:
            q = dict(p)
            old = safe_float(q.get("avg_entry_price"), 0.0)
            q["coinbase_api_avg_entry_price"] = old
            q["avg_entry_price"] = override
            q["cost_basis_source"] = "operator_override_coinbase_ui"
            contracts = safe_float(q.get("contracts"), 0.0)
            cur = safe_float(q.get("current_price"), 0.0)
            cs = safe_float(q.get("contract_size_btc"), safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01)) or 0.01
            side = q.get("side", "")
            mult = 1 if side == "LONG" else -1
            if contracts and cur:
                q["estimated_gross_unrealized_pnl"] = (cur - override) * contracts * cs * mult
                q["book_unrealized_pnl"] = q["estimated_gross_unrealized_pnl"]
            out.append(q)
        return out
    except Exception:
        return positions


def parse_iso_utc(ts: Optional[str]):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None




def format_iso_et(ts: Optional[str]) -> str:
    """Human-friendly Eastern Time timestamp for dashboard/operator messages."""
    dt = parse_iso_utc(ts)
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(TZ)
    return local.strftime("%a, %b %-d, %Y at %-I:%M:%S %p ET")


def format_operator_reason_et(reason: Any) -> str:
    """Replace raw ISO/UTC timestamps in status reasons with clean ET text."""
    if not reason:
        return "—"
    text = str(reason)
    import re

    def repl(match):
        raw = match.group(0)
        return format_iso_et(raw)

    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)?", repl, text)
    text = text.replace("Streak pause until", "Streak pause until")
    return text

def recent_fills(product_id: str, limit: int = 100, tracking_start: Optional[str] = None) -> Dict[str, Any]:
    """Return only fills that belong to the clean dashboard book.

    Coinbase's get_fills() can include months of manual/perp trading history. For the
    professional dashboard, trade attribution should start from the explicit
    tracking_start_timestamp in unified_capital_state.json. Account-level equity
    still includes everything; this only scopes the Trade P&L / Cost Transparency view.
    """
    try:
        # Safety first: never let historical Coinbase fills contaminate the clean book.
        # If no tracking_start_timestamp is set, return an empty scoped ledger.
        if not tracking_start:
            return {
                "rows": [],
                "fees_since_reset": 0.0,
                "fees_today_since_reset": 0.0,
                "fees_last_100": 0.0,
                "fees_today": 0.0,
                "fees_current_open_est": 0.0,
                "open_position_fill_count_est": 0,
                "total_fees": 0.0,
                "buy_contracts": 0.0,
                "sell_contracts": 0.0,
                "count": 0,
                "ignored_before_tracking_start": 0,
                "tracking_start_timestamp": None,
                "scope": "clean_book_requires_tracking_start",
            }
        resp = cb().get_fills(product_id=product_id, limit=limit)
        fills = getattr(resp, "fills", None) or attr_or_key(resp, "fills", []) or []
        start_dt = parse_iso_utc(tracking_start)
        rows = []
        ignored_before_start = 0
        total_fees = 0.0
        today_fees = 0.0
        buy_contracts = sell_contracts = 0.0
        today_et = datetime.now(TZ).date()
        for f in fills:
            raw_time = attr_or_key(f, "trade_time")
            t = parse_iso_utc(raw_time)
            if start_dt and t and t < start_dt:
                ignored_before_start += 1
                continue
            side = attr_or_key(f, "side")
            size = safe_float(attr_or_key(f, "size"), 0)
            fee = safe_float(attr_or_key(f, "commission"), 0)
            price = safe_float(attr_or_key(f, "price"), 0)
            total_fees += fee
            try:
                if t and t.astimezone(TZ).date() == today_et:
                    today_fees += fee
            except Exception:
                pass
            if side == "BUY":
                buy_contracts += size
            if side == "SELL":
                sell_contracts += size
            rows.append({
                "trade_time": raw_time,
                "side": side,
                "size": size,
                "price": price,
                "commission": fee,
                "liquidity": attr_or_key(f, "liquidity_indicator"),
                "order_id": attr_or_key(f, "order_id"),
            })
        return {
            "rows": rows,
            "fees_since_reset": total_fees,
            "fees_today_since_reset": today_fees,
            "fees_last_100": 0.0,  # intentionally not shown; historical last-100 fees are no longer part of the clean book
            "fees_today": today_fees,
            "fees_current_open_est": 0.0,
            "open_position_fill_count_est": 0,
            "total_fees": total_fees,
            "buy_contracts": buy_contracts,
            "sell_contracts": sell_contracts,
            "count": len(rows),
            "ignored_before_tracking_start": ignored_before_start,
            "tracking_start_timestamp": tracking_start,
            "scope": "fills_after_tracking_start_only",
        }
    except Exception as e:
        return {"rows": [], "fees_since_reset": 0.0, "fees_today_since_reset": 0.0, "fees_last_100": 0.0, "fees_today": 0.0, "fees_current_open_est": 0.0, "open_position_fill_count_est": 0, "total_fees": 0.0, "buy_contracts": 0.0, "sell_contracts": 0.0, "count": 0, "ignored_before_tracking_start": 0, "tracking_start_timestamp": tracking_start, "scope": "error", "error": str(e)}


def opening_position_from_capital(cap: Dict[str, Any], product_id: str) -> Optional[Dict[str, Any]]:
    op = cap.get("opening_perp_position") if isinstance(cap, dict) else None
    # Backwards compatibility if the JSON was edited with a slightly different key.
    if not isinstance(op, dict) and isinstance(cap, dict):
        op = cap.get("manual_opening_perp_position")
    if not isinstance(op, dict):
        return None
    if op.get("product_id") and op.get("product_id") != product_id:
        return None
    contracts = safe_float(op.get("contracts"), 0)
    avg = safe_float(op.get("avg_entry_price"), 0)
    if not contracts or not avg:
        return None
    return {
        "product_id": op.get("product_id") or product_id,
        "side": (op.get("side") or "LONG").upper(),
        "contracts": contracts,
        "avg_entry_price": avg,
        "contract_size_btc": safe_float(op.get("contract_size_btc"), DEFAULT_CONFIG["CONTRACT_SIZE_BTC"]) or DEFAULT_CONFIG["CONTRACT_SIZE_BTC"],
        "source": op.get("source") or "manual_opening_position",
    }


def apply_opening_position_override(positions: List[Dict[str, Any]], cap: Dict[str, Any], product_id: str) -> List[Dict[str, Any]]:
    """Apply clean-book cost basis WITHOUT overriding live exchange size.

    Important safety rule:
    - opening_perp_position is the STARTING BOOK only.
    - current contracts/side must always come from Coinbase live positions.

    This prevents stale dashboard state from showing LONG 6 after the user manually
    reduced to LONG 4, and it also keeps the netting monitor honest.
    """
    op = opening_position_from_capital(cap, product_id)
    out = []
    for p in positions or []:
        q = dict(p)
        live_contracts = safe_float(q.get("contracts"), 0.0)
        live_side = str(q.get("side") or "FLAT").upper()
        live_avg = safe_float(q.get("avg_entry_price"), 0.0)
        cur = safe_float(q.get("current_price"), 0.0)
        contract_size = safe_float(q.get("contract_size_btc"), DEFAULT_CONFIG["CONTRACT_SIZE_BTC"]) or DEFAULT_CONFIG["CONTRACT_SIZE_BTC"]

        # v16: Live Coinbase position basis is source of truth once the exchange has reconciled.
        # The old opening_perp_position is retained only as a reset/audit reference, never
        # as the displayed current average entry.
        q["exchange_avg_entry_price"] = live_avg
        q["avg_entry_price"] = live_avg
        q["book_avg_entry_price"] = live_avg
        q["contracts"] = live_contracts
        q["side"] = live_side
        q["contract_size_btc"] = contract_size
        q["btc_exposure"] = live_contracts * contract_size
        direction_mult = 1 if live_side == "LONG" else (-1 if live_side == "SHORT" else 0)
        q["book_unrealized_pnl"] = (cur - live_avg) * live_contracts * contract_size * direction_mult if cur and live_avg and live_contracts else q.get("exchange_unrealized_pnl")
        q["estimated_gross_unrealized_pnl"] = q["book_unrealized_pnl"]
        q["cost_basis_source"] = "coinbase_live_api"
        out.append(q)
    return out


def clean_book_from_opening(cap: Dict[str, Any], fills: Dict[str, Any], current_price: float, product_id: str, live_positions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Calculate clean book P&L from opening basis + post-reset fills, reconciled to Coinbase live size.

    The opening position is a cost-basis anchor, not a live-position source.
    Live side/contracts are always read from Coinbase so manual reductions are reflected
    immediately across the dashboard.
    """
    op = opening_position_from_capital(cap, product_id)
    live = (live_positions or [None])[0] if live_positions else None
    if not op:
        return {
            "has_opening_position": False,
            "book_contracts": safe_float((live or {}).get("contracts"), 0.0),
            "book_side": (live or {}).get("side", "FLAT"),
            "book_avg_entry_price": (live or {}).get("avg_entry_price"),
            "book_avg_entry_source": "coinbase_live_api",
            "book_unrealized_pnl": safe_float((live or {}).get("exchange_unrealized_pnl"), 0.0),
            "realized_since_reset": 0.0,
            "fees_since_reset": safe_float(fills.get("fees_since_reset"), 0.0),
            "net_book_impact": safe_float((live or {}).get("exchange_unrealized_pnl"), 0.0),
            "method_note": "No manual opening position override set. Add opening_perp_position to unified_capital_state.json for clean book accounting.",
        }

    contract_size = op["contract_size_btc"]
    avg = op["avg_entry_price"]
    start_pos = op["contracts"] if op["side"] == "LONG" else -op["contracts"]
    pos = start_pos
    realized = 0.0
    fees = 0.0
    rows = list(fills.get("rows") or [])
    rows.sort(key=lambda r: r.get("trade_time") or "")
    for r in rows:
        fside = (r.get("side") or "").upper()
        qty_abs = safe_float(r.get("size"), 0.0)
        price = safe_float(r.get("price"), 0.0)
        fees += safe_float(r.get("commission"), 0.0)
        if not qty_abs or not price:
            continue
        qty = qty_abs if fside == "BUY" else -qty_abs
        if pos == 0 or (pos > 0 and qty > 0) or (pos < 0 and qty < 0):
            new_abs = abs(pos) + abs(qty)
            avg = ((avg * abs(pos)) + (price * abs(qty))) / new_abs if new_abs else price
            pos += qty
        else:
            close_qty = min(abs(pos), abs(qty))
            direction = 1 if pos > 0 else -1
            realized += (price - avg) * close_qty * contract_size * direction
            remaining_qty = abs(qty) - close_qty
            pos += qty
            if remaining_qty > 0:
                avg = price

    live_contracts = safe_float((live or {}).get("contracts"), None)
    live_side = str((live or {}).get("side") or "").upper()
    live_signed = None
    live_avg_entry = safe_float((live or {}).get("avg_entry_price"), None)
    if live_contracts is not None and live_side:
        live_signed = live_contracts if live_side == "LONG" else (-live_contracts if live_side == "SHORT" else 0.0)

    # v19 clean: once Coinbase reports a live cost basis, that becomes the visible and
    # calculation basis for the current open position. The opening override is
    # retained only as a historical reset reference, never as the displayed avg.
    if live_avg_entry not in (None, 0):
        avg = live_avg_entry

    # If fills are missing because reset timestamp was set after a manual reduction,
    # reconcile current quantity to Coinbase but keep the manual opening cost basis.
    reconciled_to_live = False
    implied_closed_contracts = 0.0
    if live_signed is not None and live_signed != pos:
        implied_closed_contracts = max(0.0, abs(start_pos) - abs(live_signed))
        pos = live_signed
        reconciled_to_live = True
        # If Coinbase exposes daily realized after the user's reset/manual sell, use it as
        # a reference fallback rather than showing zero closed P&L. This remains labeled.
        if abs(realized) < 1e-9:
            realized = safe_float((live or {}).get("daily_realized_pnl"), 0.0)

    direction = 1 if pos > 0 else (-1 if pos < 0 else 0)
    book_unr = (current_price - avg) * abs(pos) * contract_size * direction if current_price and avg and pos else 0.0
    net_impact = realized + book_unr - fees
    return {
        "has_opening_position": True,
        "opening_position": op,
        "book_contracts": abs(pos),
        "book_side": "LONG" if pos > 0 else ("SHORT" if pos < 0 else "FLAT"),
        "book_avg_entry_price": avg,
        "book_avg_entry_source": "coinbase_live_api" if live_avg_entry not in (None, 0) else "opening_reset_reference",
        "book_unrealized_pnl": book_unr,
        "realized_since_reset": realized,
        "fees_since_reset": fees,
        "net_book_impact": net_impact,
        "tracking_start_timestamp": cap.get("tracking_start_timestamp"),
        "new_fill_count": len(rows),
        "ignored_before_tracking_start": fills.get("ignored_before_tracking_start", 0),
        "reconciled_to_live_exchange_size": reconciled_to_live,
        "implied_closed_contracts": implied_closed_contracts,
        "method_note": "Clean book uses live Coinbase avg entry for the current open position. The old opening/reset basis is reference-only and not displayed as current cost basis.",
    }

def load_capital_state() -> Dict[str, Any]:
    cap = read_json(GCS_CAPITAL, {})
    return cap if isinstance(cap, dict) else {}


def combined_capital(spot: Dict[str, Any], fut: Dict[str, Any], cap: Dict[str, Any]) -> Dict[str, Any]:
    """Combined account equity reference.

    v72: use Coinbase futures total_usd_balance as the futures equity source and
    add only verified live spot BTC mark value. Do NOT add futures unrealized P&L
    again because Coinbase's balance summary already reflects account equity and
    adding UPL separately can double count.
    """
    spot_value = safe_float(spot.get("SPOT_TREASURY_VALUE"), 0)
    futures_equity = safe_float(fut.get("total_usd_balance"), 0)
    futures_unrealized_pnl = safe_float(fut.get("unrealized_pnl"), 0)
    futures_daily_realized_pnl = safe_float(fut.get("daily_realized_pnl"), 0)
    futures_funding_pnl = safe_float(fut.get("funding_pnl"), 0)

    current = spot_value + futures_equity

    starting = safe_float(cap.get("starting_combined_capital"), None)
    if starting is None:
        manual = safe_float(cap.get("starting_capital"), None)
        starting = manual
    pnl = current - starting if starting is not None else None
    ret = (pnl / starting * 100) if starting not in (None, 0) else None
    return {
        "starting_combined_capital": starting,
        "current_combined_capital": current,
        "net_pnl": pnl,
        "net_return_pct": ret,
        "spot_value": spot_value,
        "futures_equity": futures_equity,
        "futures_base_equity": futures_equity,
        "futures_unrealized_pnl_adjustment": futures_unrealized_pnl,
        "futures_daily_realized_pnl_reference": futures_daily_realized_pnl,
        "futures_funding_pnl_reference": futures_funding_pnl,
        "combined_equity_method": "live_futures_total_usd_balance + verified_live_spot_btc_value",
        "combined_equity_note": "Spot BTC is included. Spot USD/USDC cash is excluded until a dedicated non-futures spot cash filter is added, to avoid double-counting futures cash.",
        "baseline_source": cap.get("source") or cap.get("last_updated") or "not_set",
        "baseline_set": starting is not None,
        "started_at": cap.get("started_at"),
    }


def pnl_summary(fut: Dict[str, Any], fills: Dict[str, Any], positions: List[Dict[str, Any]], clean_book: Dict[str, Any]) -> Dict[str, Any]:
    """Clean Trade P&L / Cost Transparency view.

    Account-level performance remains equity vs baseline. This block is a clean
    strategy/book view: opening position override + post-reset fills only.
    """
    exchange_unrealized = safe_float(fut.get("unrealized_pnl"), 0.0)
    exchange_daily_realized = safe_float(fut.get("daily_realized_pnl"), 0.0)
    exchange_funding = safe_float(fut.get("funding_pnl"), 0.0)
    book_unrealized = safe_float(clean_book.get("book_unrealized_pnl"), 0.0)
    realized_since_reset = safe_float(clean_book.get("realized_since_reset"), 0.0)
    fees_since_reset = safe_float(clean_book.get("fees_since_reset"), safe_float(fills.get("fees_since_reset"), 0.0))
    net_book_impact = safe_float(clean_book.get("net_book_impact"), realized_since_reset + book_unrealized - fees_since_reset)
    return {
        "book_unrealized_pnl": book_unrealized,
        "realized_since_reset": realized_since_reset,
        "fees_since_reset": fees_since_reset,
        "net_book_impact": net_book_impact,
        "book_contracts": clean_book.get("book_contracts"),
        "book_avg_entry_price": clean_book.get("book_avg_entry_price"),
        "book_avg_entry_source": clean_book.get("book_avg_entry_source", "coinbase_live_api"),
        "new_fill_count": clean_book.get("new_fill_count", fills.get("count", 0)),
        "ignored_before_tracking_start": clean_book.get("ignored_before_tracking_start", fills.get("ignored_before_tracking_start", 0)),
        "tracking_start_timestamp": clean_book.get("tracking_start_timestamp") or fills.get("tracking_start_timestamp"),
        "has_opening_position": clean_book.get("has_opening_position", False),
        # Exchange reference only; not used in clean book net impact.
        "exchange_unrealized_pnl": exchange_unrealized,
        "daily_realized_pnl": exchange_daily_realized,
        "funding_pnl": exchange_funding,
        "exchange_trade_pnl": exchange_daily_realized + exchange_unrealized + exchange_funding,
        "fees_last_100": 0.0,
        "fees_today": safe_float(fills.get("fees_today"), 0.0),
        "fees_current_open_est": 0.0,
        "today_fee_adjusted_snapshot": net_book_impact,
        "open_fee_adjusted_snapshot": net_book_impact,
        "method_note": clean_book.get("method_note") or "Clean book uses live Coinbase avg entry for current open position and post-reset fills. Coinbase native daily realized P&L is reference-only and excluded from clean performance.",
        "exchange_reference_excluded_from_clean_pnl": True,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Signal monitor
# ─────────────────────────────────────────────────────────────────────────────
def signal_snapshot(df: Optional[pd.DataFrame], cfg: Dict[str, Any]) -> Dict[str, Any]:
    if df is None or df.empty or len(df) < 60:
        return {"error": "insufficient candles"}
    try:
        close = df["close"]
        volume = df["volume"]
        price = float(close.iloc[-1])
        rsi_series = RSIIndicator(close=close, window=int(cfg["RSI_PERIOD"])).rsi()
        rsi = float(rsi_series.iloc[-1])
        bb = BollingerBands(close=close, window=int(cfg["BB_PERIOD"]), window_dev=float(cfg["BB_STD"]))
        lower = float(bb.bollinger_lband().iloc[-1])
        mid = float(bb.bollinger_mavg().iloc[-1])
        upper = float(bb.bollinger_hband().iloc[-1])
        vol_ma = float(volume.rolling(int(cfg["VOLUME_AVG_PERIOD"])).mean().iloc[-1])
        vol_cur = float(volume.iloc[-1])
        vol_ratio = vol_cur / vol_ma if vol_ma else 0
        period = int(cfg["STOCH_RSI_PERIOD"])
        rsi_low = rsi_series.rolling(period).min()
        rsi_high = rsi_series.rolling(period).max()
        stoch = float(((rsi_series - rsi_low) / (rsi_high - rsi_low + 1e-9)).iloc[-1])
        atr = float(AverageTrueRange(high=df["high"], low=df["low"], close=close, window=int(cfg["ATR_PERIOD"])).average_true_range().iloc[-1])

        rsi_floor = float(cfg["RSI_BUY_FLOOR"])
        rsi_threshold = float(cfg["RSI_BUY_THRESHOLD"])
        stoch_threshold = float(cfg["STOCH_RSI_THRESHOLD"])
        vol_threshold = float(cfg["VOLUME_MULTIPLIER"])
        sig_rsi = rsi_floor <= rsi <= rsi_threshold
        sig_bb = price <= lower
        sig_vol = vol_ratio >= vol_threshold
        sig_stoch = stoch <= stoch_threshold
        score = int(sig_rsi) + int(sig_bb) + int(sig_vol) + int(sig_stoch)
        bb_pct = ((price - lower) / (upper - lower) * 100) if upper > lower else 50
        sig = {
            "price": price,
            "granularity": cfg["SIGNAL_GRANULARITY"],
            "rsi": rsi,
            "rsi_floor": rsi_floor,
            "rsi_threshold": rsi_threshold,
            "rsi_pct": max(0, min(rsi, 100)),
            "bb_lower": lower,
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_pct": max(0, min(bb_pct, 100)),
            "vol_ratio": vol_ratio,
            "vol_threshold": vol_threshold,
            "vol_pct": max(0, min(vol_ratio / max(vol_threshold * 2, 1) * 100, 100)),
            "stoch": stoch,
            "stoch_threshold": stoch_threshold,
            "stoch_pct": max(0, min(stoch * 100, 100)),
            "atr": atr,
            "score": score,
            "sig_rsi": sig_rsi,
            "sig_bb": sig_bb,
            "sig_vol": sig_vol,
            "sig_stoch": sig_stoch,
        }
        update_signal_history(sig)
        return sig
    except Exception as e:
        return {"error": str(e)}


def update_signal_history(sig: Dict[str, Any]) -> None:
    try:
        hist = read_json(GCS_SIGNAL_HISTORY, [])
        if not isinstance(hist, list):
            hist = []
        hist.append({
            "ts": datetime.now(TZ).strftime("%H:%M:%S"),
            "score": sig.get("score"),
            "price": sig.get("price"),
            "rsi": sig.get("rsi"),
            "sig_rsi": sig.get("sig_rsi"),
            "sig_bb": sig.get("sig_bb"),
            "sig_vol": sig.get("sig_vol"),
            "sig_stoch": sig.get("sig_stoch"),
        })
        write_json(GCS_SIGNAL_HISTORY, hist[-12:])
    except Exception:
        pass


def signal_history() -> List[Dict[str, Any]]:
    hist = read_json(GCS_SIGNAL_HISTORY, [])
    return hist[-12:] if isinstance(hist, list) else []


def macro_snapshot(df: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if df is None or df.empty or len(df) < 60:
        return {
            "regime": "UNKNOWN",
            "gate_open": False,
            "gate_text": "UNKNOWN",
            "blocked_reason": "Not enough macro candles returned from Coinbase",
            "fast_period": 50,
            "slow_period": 200,
        }
    close = df["close"]
    fast_period = 50
    slow_period = 200 if len(close) >= 200 else min(100, len(close) - 1)
    fast = float(close.rolling(fast_period).mean().iloc[-1])
    slow = float(close.rolling(slow_period).mean().iloc[-1])
    price = float(close.iloc[-1])
    above_fast = price > fast
    above_slow = price > slow
    fast_above_slow = fast > slow
    gate_open = bool(above_fast and above_slow and fast_above_slow)
    if gate_open:
        regime = "BULL"
        blocked_reason = None
    elif (price < fast) and (price < slow) and (fast < slow):
        regime = "BEAR"
        blocked_reason = "Price below trend and fast SMA below slow SMA"
    else:
        regime = "MIXED"
        failed = []
        if not above_fast:
            failed.append("Price ≤ Fast SMA")
        if not above_slow:
            failed.append("Price ≤ Slow SMA")
        if not fast_above_slow:
            failed.append("Fast SMA ≤ Slow SMA")
        blocked_reason = ", ".join(failed) if failed else "Macro gate closed"
    def dist(a, b):
        return ((a / b) - 1.0) * 100.0 if b else None
    return {
        "regime": regime,
        "gate_open": gate_open,
        "gate_text": "OPEN" if gate_open else "BLOCKED",
        "blocked_reason": blocked_reason,
        "price": price,
        "sma_fast": fast,
        "sma_slow": slow,
        "above_fast": above_fast,
        "above_slow": above_slow,
        "fast_above_slow": fast_above_slow,
        "price_vs_fast_pct": dist(price, fast),
        "price_vs_slow_pct": dist(price, slow),
        "fast_vs_slow_pct": dist(fast, slow),
        "fast_period": fast_period,
        "slow_period": slow_period,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Strategy-state readers
# ─────────────────────────────────────────────────────────────────────────────
def spot_strategy_state() -> Dict[str, Any]:
    state = read_json(GCS_SPOT_STATE, {})
    positions = state.get("positions", {}) if isinstance(state, dict) else {}
    if isinstance(positions, list):
        open_pos = [p for p in positions if str(p.get("status", "")).lower() not in ("closed", "sold")]
    elif isinstance(positions, dict):
        open_pos = [p for p in positions.values() if str(p.get("status", "")).lower() not in ("closed", "sold")]
    else:
        open_pos = []
    ladder_units = [safe_int(p.get("ladder_unit"), None) for p in open_pos if isinstance(p, dict)]
    return {
        "open_count": len(open_pos),
        "max_units": DEFAULT_CONFIG["SPOT_MAX_LADDER_UNITS"],
        "ladder_units": [u for u in ladder_units if u is not None],
        "positions": open_pos[:10],
        "last_updated": state.get("last_updated") if isinstance(state, dict) else None,
        "source": GCS_SPOT_STATE,
        "tranche_targets_pct": [25, 33, 50, 90],
    }


def perp_strategy_state(exchange_positions: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    state = read_json(GCS_PERP_STATE, {})
    bot_positions = state.get("positions", {}) if isinstance(state, dict) else {}
    open_bot = []
    if isinstance(bot_positions, dict):
        open_bot = [p for p in bot_positions.values() if str(p.get("status", "open")).lower() not in ("closed", "flat")]
    elif isinstance(bot_positions, list):
        open_bot = [p for p in bot_positions if str(p.get("status", "open")).lower() not in ("closed", "flat")]
    total_contracts = sum(p.get("contracts", 0) for p in exchange_positions)
    slots = max(int(cfg.get("PERP_STRATEGY_SLOTS") or 4), len(open_bot), 4)
    return {
        "open_bot_legs": len(open_bot),
        "strategy_slots": slots,
        "exchange_contracts": total_contracts,
        "bot_positions": open_bot[:10],
        "last_updated": state.get("last_updated") if isinstance(state, dict) else None,
        "source": GCS_PERP_STATE,
        "note": "Exchange contracts include manual + bot positions; strategy slots show logical bot stack.",
    }



def perp_signal_snapshot(sig: Dict[str, Any], macro: Dict[str, Any], exchange_positions: List[Dict[str, Any]], cfg: Dict[str, Any], perp_state: Dict[str, Any]) -> Dict[str, Any]:
    """Dashboard-only perp intent monitor.

    This does not place trades. It makes the dashboard explicit about:
    - LONG bridge/core intent
    - SHORT/reversal pressure
    - netting math against the live Coinbase position
    - phantom/local-vs-exchange reconciliation risk
    """
    try:
        contracts_per_trade = safe_int(cfg.get("CONTRACTS_PER_TRADE"), 2) or 2
        contract_size = safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01) or 0.01
        exchange_contracts = 0
        exchange_side = "FLAT"
        if exchange_positions:
            p0 = exchange_positions[0]
            exchange_contracts = safe_int(p0.get("contracts"), 0)
            exchange_side = str(p0.get("side") or "FLAT").upper()
        signed_now = exchange_contracts if exchange_side == "LONG" else (-exchange_contracts if exchange_side == "SHORT" else 0)

        rsi = safe_float(sig.get("rsi"), None)
        stoch = safe_float(sig.get("stoch"), None)
        price = safe_float(sig.get("price"), None)
        bb_upper = safe_float(sig.get("bb_upper"), None)
        vol_ratio = safe_float(sig.get("vol_ratio"), 0.0) or 0.0
        vol_threshold = safe_float(sig.get("vol_threshold"), safe_float(cfg.get("VOLUME_MULTIPLIER"), 1.5)) or 1.5
        stoch_threshold = safe_float(sig.get("stoch_threshold"), safe_float(cfg.get("STOCH_RSI_THRESHOLD"), 0.10)) or 0.10
        short_rsi_threshold = safe_float(cfg.get("RSI_SHORT_THRESHOLD"), safe_float(cfg.get("RSI_EXIT_THRESHOLD"), 95)) or 95
        short_stoch_threshold = 1.0 - stoch_threshold

        # LONG monitor largely mirrors the Spot/bridge trigger. The macro gate can block new long entries.
        long_components = {
            "RSI buy band": bool(sig.get("sig_rsi")),
            "Lower Bollinger": bool(sig.get("sig_bb")),
            "Volume spike": bool(sig.get("sig_vol")),
            "Stoch oversold": bool(sig.get("sig_stoch")),
        }
        long_score = sum(1 for v in long_components.values() if v)
        long_allowed = bool(macro.get("gate_open"))
        long_triggered = bool(long_allowed and long_score >= 3)
        long_target = signed_now + contracts_per_trade if long_triggered else signed_now

        # SHORT/reversal monitor is intentionally explicit. It is not allowed to pretend a SELL 2 equals SHORT 2.
        short_components = {
            f"RSI ≥ {short_rsi_threshold:g}": bool(rsi is not None and rsi >= short_rsi_threshold),
            "Upper Bollinger": bool(price is not None and bb_upper is not None and price >= bb_upper),
            "Volume spike": bool(vol_ratio >= vol_threshold),
            f"Stoch ≥ {short_stoch_threshold:.2f}": bool(stoch is not None and stoch >= short_stoch_threshold),
        }
        short_score = sum(1 for v in short_components.values() if v)
        short_signal = bool(short_score >= 3)

        # Default short action monitor assumes a SELL of CONTRACTS_PER_TRADE unless the bot is configured for target shorts.
        # This is the number that would reduce a long, not necessarily create a short.
        sell_ticket = contracts_per_trade if short_signal else 0
        after_sell = signed_now - sell_ticket
        if after_sell > 0:
            net_effect = f"SELL {sell_ticket} would reduce LONG {abs(signed_now)} → LONG {after_sell}"
        elif after_sell == 0 and sell_ticket:
            net_effect = f"SELL {sell_ticket} would flatten the current position"
        elif after_sell < 0 and sell_ticket:
            net_effect = f"SELL {sell_ticket} would flip to SHORT {abs(after_sell)}"
        else:
            net_effect = "No short/reduction ticket indicated"

        # If the strategy wants an actual SHORT 2 target while currently long 6, the required ticket is larger.
        target_short_contracts = contracts_per_trade
        required_to_target_short = max(0, signed_now + target_short_contracts) if signed_now >= 0 else max(0, target_short_contracts - abs(signed_now))
        target_short_effect = f"To target SHORT {target_short_contracts} from current {exchange_side} {exchange_contracts}, required order is SELL {required_to_target_short}."

        bot_open_legs = safe_int(perp_state.get("open_bot_legs"), 0)
        exchange_has_position = exchange_contracts > 0
        local_has_bot_position = bot_open_legs > 0
        if local_has_bot_position and not exchange_has_position:
            phantom_state = "PHANTOM RISK"
            phantom_detail = "Bot/local state shows open perp legs but Coinbase exchange position is flat. Do not auto-cover until reconciled."
        elif exchange_has_position and not local_has_bot_position:
            phantom_state = "EXCHANGE-ONLY / MANUAL POSITION"
            phantom_detail = "Coinbase has a live position not attributed to bot legs. Dashboard treats it as opening/manual book unless bot state is updated."
        else:
            phantom_state = "OK"
            phantom_detail = "Local bot state and exchange exposure are not in conflict."

        return {
            "exchange_side": exchange_side,
            "exchange_contracts": exchange_contracts,
            "signed_now": signed_now,
            "contracts_per_trade": contracts_per_trade,
            "contract_size_btc": contract_size,
            "long_score": long_score,
            "long_components": long_components,
            "long_allowed_by_macro": long_allowed,
            "long_triggered": long_triggered,
            "long_target_contracts": long_target,
            "short_score": short_score,
            "short_components": short_components,
            "short_signal": short_signal,
            "sell_ticket_contracts": sell_ticket,
            "after_sell_signed_contracts": after_sell,
            "net_effect": net_effect,
            "required_sell_to_target_short": required_to_target_short,
            "target_short_effect": target_short_effect,
            "phantom_state": phantom_state,
            "phantom_detail": phantom_detail,
            "note": "Dashboard monitor only. Safe execution should always re-read Coinbase after any order and trade to target net exposure, not assume ticket side equals final position."
        }
    except Exception as e:
        return {"error": str(e), "note": "Perp signal monitor unavailable"}



def iaf_engine_state(cfg: Dict[str, Any], perp_meta: Dict[str, Any], fut_bal: Dict[str, Any], perp_state: Dict[str, Any], perp_monitor: Dict[str, Any], clean_book: Dict[str, Any]) -> Dict[str, Any]:
    """Expose the original WorkingOG / IAF Layer 3 rule stack for dashboard monitoring.

    This is a dashboard/status view only. It does not modify trading behavior.
    It deliberately shows when the currently running unified system differs from the
    original WorkingOG assumptions, so strategy drift is visible rather than hidden.
    """
    contracts_per_trade = safe_int(cfg.get("CONTRACTS_PER_TRADE"), 2) or 2
    contract_size = safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01) or 0.01
    atr_mult = safe_float(cfg.get("ATR_STOP_MULTIPLIER"), 1.5) or 1.5
    tsl_activation = safe_float(cfg.get("TSL_ACTIVATION_PCT"), None)
    tsl_trail = safe_float(cfg.get("TSL_TRAIL_PCT"), None)
    funding_rate = safe_float(perp_meta.get("funding_rate"), None)
    funding_gate_long_blocked = bool(funding_rate is not None and funding_rate > 0.001)
    funding_gate_short_blocked = bool(funding_rate is not None and funding_rate < -0.001)

    # Try several likely state names so older/newer bot files can publish status without breaking the dashboard.
    raw_state = read_json(GCS_PERP_STATE, {})
    if not isinstance(raw_state, dict):
        raw_state = {}
    stop_hits = safe_int(raw_state.get("daily_stop_hits", raw_state.get("stop_hits_today", raw_state.get("sl_hits_today", 0))), 0)
    loss_streak = safe_int(raw_state.get("consecutive_losses", raw_state.get("loss_streak", 0)), 0)
    pause_until = raw_state.get("pause_until") or raw_state.get("streak_pause_until")
    halted_until = raw_state.get("halt_until") or raw_state.get("daily_halt_until")
    phantom = raw_state.get("phantom") or raw_state.get("phantom_state") or raw_state.get("pending_phantom") or {}
    if isinstance(phantom, dict):
        phantom_status = phantom.get("state") or phantom.get("status") or "Not published"
        phantom_detail = phantom.get("detail") or phantom.get("reason") or "Bot has not published detailed phantom-delay state yet."
    else:
        phantom_status = str(phantom) if phantom else "Not published"
        phantom_detail = "Bot has not published detailed phantom-delay state yet."

    bidirectional = True  # Dashboard monitors both long and short pressure; actual execution must be confirmed in bot code.
    original_size_ok = contracts_per_trade == 1
    atr_ok = abs(atr_mult - 1.5) < 1e-9
    tsl_original_ok = (tsl_activation is not None and abs(tsl_activation - 0.08) < 1e-9 and tsl_trail is not None and abs(tsl_trail - 0.03) < 1e-9)

    return {
        "framework": "IAF Layer 3 — Risk Management First / J-curve model",
        "exchange": cfg.get("PERP_PRODUCT_ID", "BIP-20DEC30-CDE"),
        "contract_size_btc": contract_size,
        "contracts_per_trade": contracts_per_trade,
        "fixed_size_status": "ORIGINAL 1 CONTRACT" if original_size_ok else f"MODIFIED: {contracts_per_trade} contracts",
        "atr_stop_multiplier": atr_mult,
        "atr_status": "OK" if atr_ok else "MODIFIED",
        "tsl_activation_pct": tsl_activation,
        "tsl_trail_pct": tsl_trail,
        "tsl_status": "ORIGINAL 8% / 3%" if tsl_original_ok else "MODIFIED FROM ORIGINAL 8% / 3%",
        "daily_stop_hits": stop_hits,
        "daily_umbrella_status": "HALTED" if stop_hits >= 3 or halted_until else "ACTIVE",
        "daily_halt_until": halted_until,
        "loss_streak": loss_streak,
        "streak_pause_status": "PAUSED" if loss_streak >= 3 or pause_until else "ACTIVE",
        "pause_until": pause_until,
        "phantom_delay_status": phantom_status,
        "phantom_delay_detail": phantom_detail,
        "funding_rate": funding_rate,
        "funding_time": perp_meta.get("funding_time"),
        "funding_long_blocked": funding_gate_long_blocked,
        "funding_short_blocked": funding_gate_short_blocked,
        "funding_gate_status": "LONGS BLOCKED" if funding_gate_long_blocked else ("SHORTS BLOCKED" if funding_gate_short_blocked else "OPEN"),
        "bidirectional_status": "MONITORING LONG + SHORT" if bidirectional else "LONG ONLY",
        "safe_execution_rule": "Trade to target NET exposure; re-read Coinbase after every fill. Ticket side is not final position state.",
        "rule_notes": [
            {"rule": "Fixed Size", "current": f"{contracts_per_trade} contract(s)", "status": "OK" if original_size_ok else "MODIFIED"},
            {"rule": "ATR Stop", "current": f"{atr_mult:g}x ATR14", "status": "OK" if atr_ok else "MODIFIED"},
            {"rule": "Late TSL", "current": f"activation {(tsl_activation or 0)*100:.2f}% / trail {(tsl_trail or 0)*100:.2f}%", "status": "OK" if tsl_original_ok else "MODIFIED"},
            {"rule": "Daily Umbrella", "current": f"{stop_hits}/3 stop hits", "status": "HALTED" if stop_hits >= 3 or halted_until else "ACTIVE"},
            {"rule": "Streak Pause", "current": f"{loss_streak}/3 losses", "status": "PAUSED" if loss_streak >= 3 or pause_until else "ACTIVE"},
            {"rule": "Phantom Delay", "current": phantom_status, "status": "PUBLISHED" if phantom_status != "Not published" else "NEEDS BOT STATE"},
            {"rule": "Funding Gate", "current": f"{funding_rate if funding_rate is not None else '—'}", "status": "OPEN" if not funding_gate_long_blocked and not funding_gate_short_blocked else "BLOCKED"},
            {"rule": "Bidirectional", "current": "Long + Short monitor", "status": "MONITORED"},
        ],
    }



def perp_position_risk_state(positions: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini-style visual risk card for the current live Coinbase perp position.

    Current position comes from Coinbase. Risk controls come from the bot-published
    perp_engine_state.json so manual and bot-entered positions are visualized the
    same way.
    """
    raw_state = read_json(GCS_PERP_STATE, {})
    if not isinstance(raw_state, dict):
        raw_state = {}
    controls = raw_state.get("position_controls") if isinstance(raw_state.get("position_controls"), dict) else {}
    manual_status = raw_state.get("manual_position_status") if isinstance(raw_state.get("manual_position_status"), dict) else {}

    contract_size = safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01) or 0.01
    tsl_activation_pct = safe_float(cfg.get("TSL_ACTIVATION_PCT"), 0.0) or 0.0
    tsl_trail_pct = safe_float(cfg.get("TSL_TRAIL_PCT"), 0.0) or 0.0
    atr_mult = safe_float(cfg.get("ATR_STOP_MULTIPLIER"), 1.5) or 1.5

    manual_mode = str(cfg.get("MANUAL_POSITION_MODE") or "monitor_only")
    bot_managed = bool(manual_status.get("bot_managed")) and bool(manual_status.get("allow_bot_to_trade_position"))
    effective_management_mode = "auto_managed" if bot_managed else manual_mode
    if not positions:
        return {
            "has_position": False,
            "side": "FLAT",
            "contracts": 0,
            "management_mode": manual_mode,
            "manual_monitor_only": manual_mode == "monitor_only",
            "note": "No live Coinbase futures position detected.",
            "source": "coinbase_live_position + bot_position_controls",
        }

    pos = positions[0]
    side = pos.get("side") or "FLAT"
    contracts = safe_int(pos.get("contracts"), 0)
    entry = safe_float(pos.get("avg_entry_price"), 0.0) or 0.0
    current = safe_float(pos.get("current_price"), 0.0) or 0.0
    exch_upl = safe_float(pos.get("exchange_unrealized_pnl"), None)
    book_upl = safe_float(pos.get("book_unrealized_pnl"), None)
    upl = exch_upl if exch_upl is not None else book_upl
    btc_exposure = contracts * contract_size
    notional = abs(btc_exposure) * current if current else 0.0

    if side == "LONG" and entry and current:
        upl_pct = ((current / entry) - 1.0) * 100.0
        activation_price = entry * (1.0 + tsl_activation_pct)
    elif side == "SHORT" and entry and current:
        upl_pct = ((entry / current) - 1.0) * 100.0
        activation_price = entry * (1.0 - tsl_activation_pct)
    else:
        upl_pct = None
        activation_price = None

    highest = safe_float(controls.get("highest_price"), None)
    lowest = safe_float(controls.get("lowest_price"), None)
    atr_stop = safe_float(controls.get("atr_stop"), None)
    tsl_active = bool(controls.get("tsl_active"))
    tsl_stop = safe_float(controls.get("tsl_stop"), None)
    active_stop = tsl_stop if tsl_active and tsl_stop else atr_stop
    stop_type = "TSL ACTIVE" if tsl_active and tsl_stop else "ATR HARD STOP"

    distance_to_stop = None
    distance_to_stop_pct = None
    if active_stop and current:
        if side == "LONG":
            distance_to_stop = current - active_stop
            distance_to_stop_pct = (distance_to_stop / current) * 100.0
        elif side == "SHORT":
            distance_to_stop = active_stop - current
            distance_to_stop_pct = (distance_to_stop / current) * 100.0

    activation_gap = None
    activation_gap_pct = None
    if activation_price and current and not tsl_active:
        if side == "LONG":
            activation_gap = activation_price - current
            activation_gap_pct = (activation_gap / current) * 100.0
        elif side == "SHORT":
            activation_gap = current - activation_price
            activation_gap_pct = (activation_gap / current) * 100.0

    progress_to_activation_pct = None
    if entry and activation_price and current and activation_price != entry:
        if side == "LONG":
            progress_to_activation_pct = ((current - entry) / (activation_price - entry)) * 100.0
        elif side == "SHORT":
            progress_to_activation_pct = ((entry - current) / (entry - activation_price)) * 100.0
        progress_to_activation_pct = max(0.0, min(100.0, progress_to_activation_pct))

    if tsl_active:
        status = "TSL active — trailing stop is now the active exit level."
    elif activation_gap is not None and activation_gap <= 0:
        status = "Activation threshold reached; waiting for bot state to publish TSL active."
    else:
        status = "ATR hard stop active; TSL not activated yet."

    return {
        "has_position": True,
        "side": side,
        "contracts": contracts,
        "contract_size_btc": contract_size,
        "btc_exposure": btc_exposure,
        "entry_price": entry,
        "current_price": current,
        "notional": notional,
        "unrealized_pnl": upl,
        "unrealized_pnl_pct": upl_pct,
        "highest_price": highest,
        "lowest_price": lowest,
        "atr_stop": atr_stop,
        "atr_stop_multiplier": atr_mult,
        "tsl_active": tsl_active,
        "tsl_stop": tsl_stop,
        "active_stop": active_stop,
        "stop_type": stop_type,
        "tsl_activation_pct": tsl_activation_pct,
        "tsl_trail_pct": tsl_trail_pct,
        "tsl_activation_price": activation_price,
        "activation_gap": activation_gap,
        "activation_gap_pct": activation_gap_pct,
        "progress_to_activation_pct": progress_to_activation_pct,
        "distance_to_stop": distance_to_stop,
        "distance_to_stop_pct": distance_to_stop_pct,
        "status": ("Manual position monitor-only — Larry will not execute ATR/TSL exits on this exposure. " + status) if effective_management_mode == "monitor_only" else status,
        "management_mode": effective_management_mode,
        "manual_monitor_only": effective_management_mode == "monitor_only",
        "source": "Coinbase live position + v34 engine ownership and position controls",
    }

def portfolio_overlay_state(spot_bal: Dict[str, Any], fut_bal: Dict[str, Any], positions: List[Dict[str, Any]], cfg: Dict[str, Any], capital: Dict[str, Any], clean_book: Dict[str, Any], live_price_hint: float = 0.0) -> Dict[str, Any]:
    """Live portfolio overlay.

    v20: uses LIVE Coinbase position for exposure and avg entry; removes avg-entry override controls.
    Perp notional is contracts * contract_size_btc * live price.
    Effective leverage is perp notional / futures equity, because this measures
    leverage inside the futures margin pool rather than the whole combined account.
    """
    spot_btc = safe_float(spot_bal.get("BTC"), 0.0) or 0.0
    perp_btc = 0.0
    net_contracts = 0.0
    abs_contracts = 0.0
    side = "FLAT"
    avg_entry = None
    live_price = safe_float(clean_book.get("current_mark"), 0.0) or 0.0

    if positions:
        p = positions[0]
        side = str(p.get("side") or "FLAT").upper()
        abs_contracts = safe_float(p.get("contracts"), 0.0) or 0.0
        csize = safe_float(p.get("contract_size_btc"), safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01)) or 0.01
        mult = 1 if side == "LONG" else (-1 if side == "SHORT" else 0)
        net_contracts = abs_contracts * mult
        perp_btc = abs_contracts * csize * mult
        avg_entry = safe_float(p.get("avg_entry_price"), None)
        live_price = safe_float(p.get("current_price"), live_price) or live_price

    # v73 fix: both fallbacks below used to read keys (`clean_book.current_mark`,
    # `capital.btc_price`) that are never populated by their supposed sources -- always
    # resolved to 0. Harmless while flat (perp_btc is also 0), but if a live position's
    # current_price is ever briefly missing, leverage/notional would silently compute
    # as zero instead of using a real price. Use the BTC mark already fetched by the
    # /api/data caller as the real fallback.
    if not live_price:
        live_price = safe_float(live_price_hint, 0.0) or 0.0

    net_btc_delta = spot_btc + perp_btc
    futures_equity = safe_float(fut_bal.get("total_usd_balance"), 0.0) or safe_float(fut_bal.get("cfm_usd_balance"), 0.0) or 0.0
    combined_equity = safe_float(capital.get("current_combined_capital"), 0.0) or 0.0
    gross_notional = abs(perp_btc) * live_price if live_price else 0.0
    effective_leverage = gross_notional / futures_equity if futures_equity else None
    combined_leverage = gross_notional / combined_equity if combined_equity else None
    return {
        "spot_btc": spot_btc,
        "perp_btc": perp_btc,
        "net_btc_delta": net_btc_delta,
        "net_contracts": net_contracts,
        "abs_contracts": abs_contracts,
        "contract_size_btc": safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01) or 0.01,
        "exchange_side": side,
        "exchange_avg_entry": avg_entry,
        "live_price": live_price,
        "gross_perp_notional": gross_notional,
        "futures_equity": futures_equity,
        "effective_leverage": effective_leverage,
        "combined_effective_leverage": combined_leverage,
        "summary": "Live Coinbase exposure only. Displays micro contracts separately from BTC notional exposure.",
    }


def position_reconciler_state(positions: List[Dict[str, Any]], perp_state: Dict[str, Any], cap: Dict[str, Any], perp_monitor: Dict[str, Any]) -> Dict[str, Any]:
    """Live exchange vs bot-target reconciler.

    v14: removes confusing opening/manual contract labels. Coinbase is source of truth.
    """
    exchange_contracts = safe_int(perp_monitor.get("exchange_contracts"), 0) if isinstance(perp_monitor, dict) else 0
    exchange_side = perp_monitor.get("exchange_side", "FLAT") if isinstance(perp_monitor, dict) else "FLAT"
    signed_now = safe_int(perp_monitor.get("signed_now"), 0) if isinstance(perp_monitor, dict) else 0
    local_bot_legs = safe_int(perp_state.get("open_bot_legs"), 0) if isinstance(perp_state, dict) else 0
    target_signed = None
    last_plan = {}
    try:
        engine_state = read_json(GCS_PERP_STATE, {}) or {}
        last_plan = engine_state.get("last_order_plan") or {}
        if isinstance(last_plan, dict):
            target_signed = last_plan.get("target_signed")
    except Exception:
        pass
    drift = None
    if target_signed is not None:
        drift = signed_now - safe_int(target_signed, 0)
    status = "MATCHED"
    detail = "Live Coinbase position is the current source of truth. No stale opening override is used for exposure."
    if local_bot_legs and exchange_contracts == 0:
        status = "PHANTOM RISK"
        detail = "Local/bot state shows open legs but Coinbase is flat. Do not auto-cover until reconciled."
    elif drift not in (None, 0):
        status = "DRIFT WARNING"
        detail = "Last target exposure differs from live Coinbase exposure. Reconcile before any new trade."
    elif exchange_contracts and not local_bot_legs:
        status = "EXCHANGE LIVE / BOT FLAT"
        detail = "Coinbase has live exposure not attributed to bot legs. Treat as live portfolio exposure; bot must calculate from this before any trade."
    return {
        "status": status,
        "detail": detail,
        "exchange_side": exchange_side,
        "exchange_contracts": exchange_contracts,
        "signed_now": signed_now,
        "local_bot_legs": local_bot_legs,
        "target_signed": target_signed,
        "exposure_drift": drift,
        "safe_rule": "Before every order, read live Coinbase net position. Example: LONG 4 target SHORT 2 = SELL 6.",
    }



def live_risk_gate_state(engine_state: Dict[str, Any], cfg: Dict[str, Any], macro: Dict[str, Any]) -> Dict[str, Any]:
    """Dashboard source-of-truth for trading gate.

    v28: risk gate is explicitly separated from phantom/macro states. PHANTOM_ARMED is a pending
    execution state, not a blocked trading gate. Manual positions are monitor-only by default.
    """
    if not isinstance(engine_state, dict):
        engine_state = {}
    risk = engine_state.get("risk") if isinstance(engine_state.get("risk"), dict) else {}
    raw_gate = engine_state.get("risk_gate") if isinstance(engine_state.get("risk_gate"), dict) else {}
    entries_halted = bool(risk.get("entries_halted"))
    pause_until = risk.get("pause_until")
    halt_reason = risk.get("halt_reason")
    loss_streak = safe_int(risk.get("loss_streak"), 0)
    daily_stop_hits = safe_int(risk.get("daily_stop_hits"), 0)
    entries_allowed = raw_gate.get("entries_allowed")
    if entries_allowed is None:
        entries_allowed = not entries_halted and not bool(pause_until)
    entries_allowed = bool(entries_allowed)
    reason = raw_gate.get("reason") or halt_reason or (f"Pause until {pause_until}" if pause_until else "Risk gate open")
    manual_mode = str(cfg.get("MANUAL_POSITION_MODE") or "monitor_only")
    macro_open = bool(macro.get("gate_open")) if isinstance(macro, dict) else None
    if not entries_allowed:
        label = "RISK BLOCKED"
        color = "blocked"
        headline = "🔴 Risk Gate Blocked"
    elif macro_open is False:
        # Macro gate can block Spot/Bridge entries, but it is NOT a global trading halt.
        # Keep the top trading gate open when risk allows entries so PHANTOM/MIXED macro
        # states do not render as a scary global BLOCKED status.
        label = "OPEN"
        color = "enabled"
        headline = "🟢 Trading Gate Open"
        reason = "Risk gate open. Macro filter is not fully bullish, so Spot/Bridge entries may be restricted."
    else:
        label = "OPEN"
        color = "enabled"
        headline = "🟢 Trading Gate Open"
    return {
        "entries_allowed": entries_allowed,
        "entries_halted": entries_halted,
        "pause_until": pause_until,
        "halt_reason": halt_reason,
        "loss_streak": loss_streak,
        "daily_stop_hits": daily_stop_hits,
        "reason": reason,
        "label": label,
        "color": color,
        "headline": headline,
        "manual_position_mode": manual_mode,
        "manual_management_text": "Manual positions are MONITOR ONLY; Larry will not auto-sell/TSL/ATR-stop them." if manual_mode == "monitor_only" else f"Manual position mode: {manual_mode}",
    }



def build_risk_intelligence(cfg: Dict[str, Any], engine_state: Dict[str, Any], heartbeat: Dict[str, Any], fut_bal: Dict[str, Any], spot_bal: Dict[str, Any], capital: Dict[str, Any], pnl: Dict[str, Any], position_risk: Dict[str, Any], portfolio_overlay: Dict[str, Any], fills: Dict[str, Any]) -> Dict[str, Any]:
    """Operational trading-desk intelligence layer.

    This intentionally separates Larry strategy performance from manual monitor-only
    exposure. Combined account equity remains a reference; strategy P&L is bot-only
    where attribution is available.
    """
    if not isinstance(engine_state, dict):
        engine_state = {}
    if not isinstance(position_risk, dict):
        position_risk = {}
    manual_status = engine_state.get("manual_position_status") if isinstance(engine_state.get("manual_position_status"), dict) else {}
    manual_mode = str(cfg.get("MANUAL_POSITION_MODE") or "monitor_only")
    manual_active = bool(manual_status.get("is_manual_or_external")) or bool(position_risk.get("manual_monitor_only"))
    manual_unrealized = safe_float(position_risk.get("unrealized_pnl"), 0.0) if manual_active else 0.0
    manual_notional = safe_float(position_risk.get("notional"), 0.0) if manual_active else 0.0
    manual_contracts = safe_float(position_risk.get("contracts"), 0.0) if manual_active else 0.0
    manual_side = position_risk.get("side") if manual_active else "FLAT"
    manual_entry = safe_float(position_risk.get("entry_price"), 0.0) if manual_active else 0.0
    manual_mark = safe_float(position_risk.get("current_price"), 0.0) if manual_active else 0.0
    manual_pnl_pct = safe_float(position_risk.get("unrealized_pnl_pct"), None) if manual_active else None

    raw_bot_pnl = safe_float(pnl.get("net_book_impact"), 0.0)
    raw_book_unr = safe_float(pnl.get("book_unrealized_pnl"), 0.0)
    # If current live position is manual-monitor-only, remove its open unrealized from Larry clean book.
    larry_strategy_pnl = raw_bot_pnl - raw_book_unr if manual_active else raw_bot_pnl

    # Signal rollup if analyzer has written it.
    signal_rows = []
    try:
        df = read_csv(GCS_SIGNAL_PNL_ROLLUP)
        if not df.empty:
            signal_rows = df.fillna("").to_dict(orient="records")[:25]
    except Exception:
        signal_rows = []

    ledger_rows = []
    slippage_values = []
    try:
        df = read_csv(GCS_PERP_TRADES_LEDGER)
        if not df.empty:
            ledger_rows = df.tail(200).fillna("").to_dict(orient="records")
            for r in ledger_rows:
                try:
                    slippage_values.append(float(r.get("slippage_bps")))
                except Exception:
                    pass
    except Exception:
        ledger_rows = []

    avg_slippage = sum(slippage_values)/len(slippage_values) if slippage_values else None
    worst_slippage = max(slippage_values) if slippage_values else None
    last_slippage = slippage_values[-1] if slippage_values else None

    # Opportunity cost / blocked-action visibility from current bot state only.
    last_blocked = current_blocked_actions(engine_state)
    blocked_count = len([v for v in last_blocked.values() if v])
    opp = {
        "blocked_count_current_cycle": blocked_count,
        "last_blocked_action": last_blocked,
        "macro_gate_open": bool((engine_state.get("macro_regime") or {}).get("gate_open")) if isinstance(engine_state.get("macro_regime"), dict) else None,
        "risk_entries_allowed": bool((engine_state.get("risk_gate") or {}).get("entries_allowed")) if isinstance(engine_state.get("risk_gate"), dict) else None,
    }

    # Drawdown reference: use available strategy P&L snapshots if present, otherwise current clean P&L.
    drawdown = {
        "current_strategy_pnl": larry_strategy_pnl,
        "manual_unrealized_pnl": manual_unrealized,
        "combined_equity_drift": safe_float(capital.get("net_pnl"), None),
        "note": "Full historical drawdown requires periodic equity snapshots; current view separates Larry P&L, manual book P&L, and account equity drift."
    }

    funding = {
        "today_or_session_funding_pnl": safe_float(fut_bal.get("funding_pnl"), 0.0),
        "funding_rate": safe_float((engine_state.get("funding") or {}).get("rate"), None) if isinstance(engine_state.get("funding"), dict) else None,
        "long_gate_open": (engine_state.get("funding") or {}).get("long_gate_open") if isinstance(engine_state.get("funding"), dict) else None,
        "short_gate_open": (engine_state.get("funding") or {}).get("short_gate_open") if isinstance(engine_state.get("funding"), dict) else None,
    }

    health = {
        "heartbeat_health": heartbeat.get("health"),
        "heartbeat_age_secs": heartbeat.get("age_secs"),
        "bot_state": heartbeat.get("state"),
        "gcs_status": "ok_if_dashboard_loaded",
        "kill_switch": engine_state.get("kill_switch") or {},
        "last_updated": engine_state.get("updated_at") or engine_state.get("last_updated"),
    }

    expected_actual = {
        "expected_note": "Expected-vs-actual needs backtest expectancy by signal_class. Once signal_pnl_rollup.csv exists, compare live realized P&L by class against expected value by class.",
        "actual_larry_strategy_pnl": larry_strategy_pnl,
        "actual_manual_pnl": manual_unrealized,
        "actual_combined_equity_drift": safe_float(capital.get("net_pnl"), None),
    }

    return {
        "larry_strategy": {
            "net_pnl": larry_strategy_pnl,
            "raw_clean_book_before_manual_exclusion": raw_bot_pnl,
            "manual_open_unrealized_excluded": manual_unrealized if manual_active else 0.0,
            "note": "Larry strategy P&L excludes manual monitor-only open exposure. Combined account equity still includes everything."
        },
        "manual_book": {
            "active": manual_active,
            "mode": manual_mode,
            "side": manual_side,
            "contracts": manual_contracts,
            "avg_entry_price": manual_entry,
            "current_price": manual_mark,
            "unrealized_pnl": manual_unrealized,
            "unrealized_pnl_pct": manual_pnl_pct,
            "notional": manual_notional,
            "included_in_larry_pnl": False,
            "note": "Manual positions are shown here and in combined equity, but excluded from Larry strategy return."
        },
        "signal_attribution": {"rows": signal_rows, "available": bool(signal_rows), "path": GCS_SIGNAL_PNL_ROLLUP},
        "execution_quality": {"avg_slippage_bps": avg_slippage, "worst_slippage_bps": worst_slippage, "last_slippage_bps": last_slippage, "sample_count": len(slippage_values)},
        "opportunity_cost": opp,
        "risk_exposure": {
            "spot_btc": safe_float(portfolio_overlay.get("spot_btc"), 0.0),
            "perp_btc": safe_float(portfolio_overlay.get("perp_btc"), 0.0),
            "net_btc_delta": safe_float(portfolio_overlay.get("net_btc_delta"), 0.0),
            "manual_perp_notional": manual_notional,
            "gross_perp_notional": safe_float(portfolio_overlay.get("gross_perp_notional"), 0.0),
            "effective_leverage": portfolio_overlay.get("effective_leverage"),
        },
        "drawdown": drawdown,
        "funding": funding,
        "expected_vs_actual": expected_actual,
        "health": health,
    }

# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/data")
def api_data():
    now = time.time()
    if "data" in _cache and now - _cache.get("ts", 0) < CACHE_TTL_SECONDS:
        return jsonify(_cache["data"])
    try:
        cfg = load_config()
        heartbeat = heartbeat_status(cfg)
        spot_product = cfg["SPOT_PRODUCT_ID"] or "BTC-USDC"
        fallback_product = cfg.get("SPOT_FALLBACK_PRODUCT_ID") or "BTC-USD"
        perp_product = cfg["PERP_PRODUCT_ID"]
        btc_price = best_mid(spot_product) or best_mid(fallback_product) or safe_float(heartbeat.get("price"), 0)
        perp_meta = product_details(perp_product)
        spot_meta = product_details(spot_product)
        fut_bal = futures_balance()
        spot_bal = spot_accounts(btc_price or 0)
        contract_size = safe_float(perp_meta.get("contract_size"), safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01)) or 0.01
        cap = load_capital_state()
        ex_positions = futures_positions(perp_product, contract_size)  # live Coinbase position is source of truth
        tracking_start = cap.get("tracking_start_timestamp")
        fills = recent_fills(perp_product, 100, tracking_start)
        current_mark = 0.0
        if ex_positions:
            current_mark = safe_float(ex_positions[0].get("current_price"), 0.0) or safe_float(perp_meta.get("price"), 0.0) or safe_float(btc_price, 0.0)
        clean_book = clean_book_from_opening(cap, fills, current_mark, perp_product, ex_positions)
        pnl = pnl_summary(fut_bal, fills, ex_positions, clean_book)
        capital = combined_capital(spot_bal, fut_bal, cap)
        sig_df = candles_df(fallback_product, cfg["SIGNAL_GRANULARITY"], 300)
        macro_df = candles_df(fallback_product, cfg["MACRO_GRANULARITY"], 300)
        spot_state = spot_strategy_state()
        spot_state["max_units"] = int(cfg.get("SPOT_MAX_LADDER_UNITS") or 4)
        perp_state = perp_strategy_state(ex_positions, cfg)
        sig = signal_snapshot(sig_df, cfg)
        mac = macro_snapshot(macro_df)
        engine_state = read_json(GCS_PERP_STATE, {})
        if not isinstance(engine_state, dict):
            engine_state = {}
        larry_accounting = larry_trade_ledger_summary(engine_state, tracking_start)
        trade_map = trade_map_price_history(fallback_product)
        # v55: benchmark Larry against passive BTC bought with the same starting
        # capital. Prefer explicit start BTC price from capital_state if it exists;
        # otherwise use the first successful Larry trade price as a transparent proxy.
        start_capital = safe_float(capital.get("starting_combined_capital"), None)
        start_btc_price = (safe_float(cap.get("starting_btc_price"), None)
                           or safe_float(cap.get("benchmark_start_btc_price"), None)
                           or safe_float(larry_accounting.get("benchmark_start_btc_price_proxy"), None))
        current_btc_price = safe_float(btc_price, None)
        larry_total_pnl = safe_float(larry_accounting.get("net_total_pnl_usd"), 0.0)
        larry_return_pct = (larry_total_pnl / start_capital * 100.0) if start_capital not in (None, 0) else None
        synthetic_btc = (start_capital / start_btc_price) if start_capital not in (None, 0) and start_btc_price not in (None, 0) else None
        benchmark_value = (synthetic_btc * current_btc_price) if synthetic_btc is not None and current_btc_price is not None else None
        benchmark_pnl = (benchmark_value - start_capital) if benchmark_value is not None and start_capital is not None else None
        benchmark_return_pct = (benchmark_pnl / start_capital * 100.0) if benchmark_pnl is not None and start_capital not in (None, 0) else None
        alpha_pct = (larry_return_pct - benchmark_return_pct) if larry_return_pct is not None and benchmark_return_pct is not None else None
        alpha_usd = ((start_capital + larry_total_pnl) - benchmark_value) if start_capital is not None and benchmark_value is not None else None
        performance_analytics = {
            "start_capital_usd": start_capital,
            "start_btc_price": start_btc_price,
            "start_btc_price_source": "capital_state" if (safe_float(cap.get("starting_btc_price"), None) or safe_float(cap.get("benchmark_start_btc_price"), None)) else larry_accounting.get("benchmark_start_btc_price_source"),
            "current_btc_price": current_btc_price,
            "synthetic_btc_holdings": synthetic_btc,
            "btc_benchmark_value_usd": benchmark_value,
            "btc_benchmark_pnl_usd": benchmark_pnl,
            "btc_benchmark_return_pct": benchmark_return_pct,
            "larry_total_pnl_usd": larry_total_pnl,
            "larry_equity_usd": (start_capital + larry_total_pnl) if start_capital is not None else None,
            "larry_return_pct": larry_return_pct,
            "alpha_pct": alpha_pct,
            "alpha_usd": alpha_usd,
            "trade_stats": larry_accounting.get("trade_stats", {}),
            "exposure_stats": larry_accounting.get("exposure_stats", {}),
            "max_drawdown_pct": None,
            "note": "BTC benchmark uses the same starting capital. Start BTC price comes from capital_state if set, otherwise first successful Larry trade price.",
        }
        # v49: Make Larry's own trade ledger the headline strategy P&L source.
        pnl["ledger_realized_pnl_usd"] = larry_accounting.get("net_realized_pnl_usd", 0.0)
        pnl["ledger_gross_realized_pnl_usd"] = larry_accounting.get("gross_realized_pnl_usd", 0.0)
        pnl["ledger_fees_usd"] = larry_accounting.get("fees_usd", 0.0)
        pnl["ledger_open_unrealized_pnl_usd"] = larry_accounting.get("open_unrealized_pnl_usd", 0.0)
        pnl["ledger_net_total_pnl_usd"] = larry_accounting.get("net_total_pnl_usd", 0.0)
        pnl["net_book_impact"] = larry_accounting.get("net_total_pnl_usd", pnl.get("net_book_impact", 0.0))
        pnl["realized_since_reset"] = larry_accounting.get("net_realized_pnl_usd", pnl.get("realized_since_reset", 0.0))
        pnl["fees_since_reset"] = larry_accounting.get("fees_usd", pnl.get("fees_since_reset", 0.0))
        pnl["book_unrealized_pnl"] = larry_accounting.get("open_unrealized_pnl_usd", pnl.get("book_unrealized_pnl", 0.0))
        pnl["method_note"] = "Larry strategy P&L uses Larry's own perp_trades_ledger.csv: net realized + live open unrealized. Coinbase fills/equity are reference only."
        risk_gate = live_risk_gate_state(engine_state, cfg, mac)
        current_blockers = current_blocked_actions(engine_state, risk_gate)
        perp_monitor = perp_signal_snapshot(sig, mac, ex_positions, cfg, perp_state)
        iaf_state = iaf_engine_state(cfg, perp_meta, fut_bal, perp_state, perp_monitor, clean_book)
        portfolio_overlay = portfolio_overlay_state(spot_bal, fut_bal, ex_positions, cfg, capital, clean_book, live_price_hint=btc_price)
        position_risk = perp_position_risk_state(ex_positions, cfg)
        # v32: if live exposure is manual/external monitor-only, exclude its open UPL from Larry clean strategy P&L.
        manual_status = engine_state.get("manual_position_status") if isinstance(engine_state.get("manual_position_status"), dict) else {}
        manual_active = bool(manual_status.get("is_manual_or_external")) or bool(position_risk.get("manual_monitor_only"))
        if manual_active:
            manual_upl = safe_float(position_risk.get("unrealized_pnl"), 0.0)
            book_unr = safe_float(pnl.get("book_unrealized_pnl"), 0.0)
            pnl["manual_position_excluded_from_clean_pnl"] = True
            pnl["manual_book_unrealized_pnl"] = manual_upl
            pnl["raw_net_book_impact_before_manual_exclusion"] = pnl.get("net_book_impact")
            pnl["net_book_impact"] = safe_float(pnl.get("net_book_impact"), 0.0) - book_unr
            pnl["book_unrealized_pnl"] = 0.0
            pnl["method_note"] = "Larry clean strategy P&L excludes manual monitor-only open positions. Manual performance is shown separately in the Manual Perp Book and included only in combined account equity reference."
        risk_intelligence = build_risk_intelligence(cfg, engine_state, heartbeat, fut_bal, spot_bal, capital, pnl, position_risk, portfolio_overlay, fills)
        reconciler = position_reconciler_state(ex_positions, perp_state, cap, perp_monitor)
        warnings = []
        if not capital["baseline_set"]:
            warnings.append("Capital baseline is not set. Use /api/set_capital_baseline once the account state looks correct.")
        if heartbeat.get("health") != "LIVE":
            warnings.append(f"Bot heartbeat is {heartbeat.get('health')}.")
        # v39: a fresh heartbeat can still carry state=ERROR if the bot's last loop hit
        # an exception. Surface that as an operator warning instead of only showing a
        # cryptic red ERROR pill in the command center.
        if str(heartbeat.get("state") or "").upper() == "ERROR":
            warnings.append("Bot loop heartbeat state is ERROR. Process may be alive, but the last bot cycle hit an exception — check journalctl for the latest traceback.")
        if perp_meta.get("session_open") is False:
            warnings.append("Futures exchange session is closed; exchange mark/P&L fields may use settlement/stale marks.")
        if isinstance(risk_gate, dict) and risk_gate.get("entries_allowed") is False:
            warnings.append(f"Perp entries are currently blocked by risk gate: {format_operator_reason_et(risk_gate.get('reason'))}")
        # v74: removed a v73 warning that fired whenever spot_accounts() zeroed
        # discovered USD/USDC (RAW_USD_USDC_DISCOVERED). Operator confirmed that cash
        # is the same balance already reflected in futures base equity for this
        # account, so it was a false-positive firing every cycle, not a real signal.
        # The underlying exclusion in spot_accounts() is correct and unchanged.
        data = {
            "ok": True,
            "server_time": now_et(),
            "config": cfg,
            "price": btc_price,
            "spot_product": spot_meta,
            "perp_product": perp_meta,
            "heartbeat": heartbeat,
            "macro": mac,
            "risk_gate": risk_gate,
            "current_blocked_actions": current_blockers,
            "halt_state": read_json(GCS_BOT_HALT, {}) or {},
            "engine_state": engine_state,
            "manual_position_mode": cfg.get("MANUAL_POSITION_MODE", "monitor_only"),
            "signals": sig,
            "signal_history": signal_history(),
            "spot_balance": spot_bal,
            "futures_balance": fut_bal,
            "capital": capital,
            "pnl_summary": pnl,
            "larry_trade_accounting": larry_accounting,
            "trade_map": trade_map,
            "performance_analytics": performance_analytics,
            "clean_book": clean_book,
            "futures_positions": ex_positions,
            "authoritative_position_source": "Coinbase live futures_positions/exchange_position only; historical last_execution_result is never a current-position source",
            "fills": fills,
            "spot_strategy": spot_state,
            "perp_strategy": perp_state,
            "perp_signal_monitor": perp_monitor,
            "iaf_engine": iaf_state,
            "portfolio_overlay": portfolio_overlay,
            "position_risk": position_risk,
            "position_reconciler": reconciler,
            "risk_intelligence": risk_intelligence,
            "warnings": warnings,
        }
        _cache.update({"ts": now, "data": data})
        return jsonify(data)
    except Exception as e:
        # v73 fix: full tracebacks used to be returned to the browser (internals
        # disclosure to any visitor). Log server-side only; client gets the message.
        app.logger.exception("api_data failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/set_capital_baseline", methods=["GET", "POST"])
def set_capital_baseline():
    try:
        cfg = load_config()
        spot_product = cfg["SPOT_PRODUCT_ID"] or "BTC-USDC"
        btc_price = best_mid(spot_product) or best_mid(cfg.get("SPOT_FALLBACK_PRODUCT_ID") or "BTC-USD") or 0
        spot_bal = spot_accounts(btc_price)
        fut_bal = futures_balance()
        manual = safe_float(request.args.get("amount"), None)
        starting_spot = safe_float(spot_bal.get("SPOT_TREASURY_VALUE"), 0)
        starting_fut = safe_float(fut_bal.get("total_usd_balance"), 0)
        combined = manual if manual is not None else starting_spot + starting_fut
        existing = load_capital_state()
        payload = {
            "starting_spot_value": starting_spot,
            "starting_futures_equity": starting_fut,
            "starting_combined_capital": combined,
            "manual_override": manual,
            "started_at": now_et(),
            "last_updated": "set_from_professional_dashboard",
            "source": "coinbase_only_account_snapshot" if manual is None else "manual_override",
        }
        # Preserve clean-book accounting controls when resetting the account baseline.
        for k in ("tracking_start_timestamp", "opening_perp_position"):
            if k in existing:
                payload[k] = existing[k]
        write_json(GCS_CAPITAL, payload)
        _cache.clear()
        return jsonify({"ok": True, "capital_state": payload})
    except Exception as e:
        # v73 fix: see api_data -- tracebacks are logged server-side, not returned.
        app.logger.exception("request failed")
        return jsonify({"ok": False, "error": str(e)}), 500



# v73 fix: this allowlist used to live only inside api_strategy_config; the sibling
# /api/update_strategy_param route below wrote ANY key straight into
# strategy_config.json with no allowlist at all. Shared here so both routes agree.
STRATEGY_CONFIG_ALLOWED_KEYS = set(DEFAULT_CONFIG.keys()) | {
    "PHANTOM_EXTENSION_PCT", "FUNDING_LONG_MAX", "FUNDING_SHORT_MIN",
    "MAX_EFFECTIVE_LEVERAGE", "MIN_FUTURES_EQUITY_BUFFER_USD",
    "DAILY_STOP_LIMIT", "LOSS_STREAK_LIMIT", "STREAK_PAUSE_HOURS",
    "SPOT_ENTRY_COOLDOWN_SEC", "PERP_ENTRY_COOLDOWN_SEC", "BRIDGE_ENTRY_COOLDOWN_SEC",
    "SPOT_TRANCHE_TARGETS_PCT", "CONFIG_NOTE", "MANUAL_POSITION_MODE",
    "SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL", "EMAIL_INCLUDE_RAW_ORDER",
    "MAX_CONVICTION_CONTRACTS", "CONTRACTS_PER_TRADE_FULL", "CONTRACTS_PER_TRADE_PARTIAL", "CONTRACTS_PER_TRADE_PROBE", "SCORE4_MACRO_OVERRIDE_ENABLED", "MACRO_BLOCKED_PROBE_CONTRACTS", "TP1_PCT", "TP1_FRACTION", "FUNDING_SIZE_REDUCE_AT", "ENABLE_SPOT_BTC_TRADING", "ENABLE_SPOT_BRIDGE_PERP_BUYS",
    "CORE_SCORE4_IMMEDIATE_ENTRY", "SIGNAL_LOCK_ENABLED", "SIGNAL_VALIDITY_MINUTES", "SIGNAL_CANCEL_SCORE",
    "SIGNAL_HYSTERESIS_ARM_SCORE", "SIGNAL_COMMIT_ON_CLOSED_CANDLE", "FREEZE_CONFIDENCE_ON_ARM",
    "SIGNAL_ARM_SCORE", "SIGNAL_COMMIT_SCORE", "REVERSAL_PROBE_ENABLED", "REVERSAL_PROBE_CONTRACTS",
    "REVERSAL_NEAR_BB_PCT", "REVERSAL_RSI_SOFT_LONG_MAX", "REVERSAL_RSI_SOFT_SHORT_MIN",
}

# v73 fix: /api/strategy_config only ever validated key *names*; any value was
# accepted, including zero/negative/absurd numbers for fields that gate real risk
# (leverage, stop distances, position caps). (min, max) bounds mirror the client-side
# bounds already enforced in saveStrategyControls() on the dashboard's own JS.
STRATEGY_CONFIG_NUMERIC_BOUNDS = {
    "MAX_EFFECTIVE_LEVERAGE": (0.1, 20.0),
    "MIN_FUTURES_EQUITY_BUFFER_USD": (0.0, 1_000_000.0),
    "TSL_ACTIVATION_PCT": (0.0005, 0.5),
    "TSL_TRAIL_PCT": (0.0005, 0.5),
    "ATR_STOP_MULTIPLIER": (0.1, 10.0),
    "TP1_PCT": (0.0005, 0.5),
    "TP1_PROBE_TRIGGER_PCT": (0.0005, 0.5),
    "TP1_PARTIAL_TRIGGER_PCT": (0.0005, 0.5),
    "TP1_STRONG_TRIGGER_PCT": (0.0005, 0.5),
    "TP1_FULL_TRIGGER_PCT": (0.0005, 0.5),
    "MAX_CONVICTION_CONTRACTS": (1, 500),
    "CONTRACT_SIZE_BTC": (0.0001, 10.0),
    "DAILY_STOP_LIMIT": (1, 50),
    "LOSS_STREAK_LIMIT": (1, 50),
    "STREAK_PAUSE_HOURS": (0.0, 168.0),
    "MIN_ENTRY_COOLDOWN_SECONDS": (0, 86400),
    "SPOT_ENTRY_COOLDOWN_SEC": (0, 86400),
    "PERP_ENTRY_COOLDOWN_SEC": (0, 86400),
    "BRIDGE_ENTRY_COOLDOWN_SEC": (0, 86400),
    "SPOT_MIN_ORDER_USD": (0.0, 1_000_000.0),
}


def _validate_strategy_value(key: str, value: Any) -> (bool, Any):
    """Return (ok, value_or_error_message). Enforces numeric sanity for the fields
    in STRATEGY_CONFIG_NUMERIC_BOUNDS; passes everything else through unchanged
    (the allowlist above is still the primary gate on which keys can be set at all).
    """
    bounds = STRATEGY_CONFIG_NUMERIC_BOUNDS.get(key)
    if bounds is None:
        return True, value
    try:
        num_val = float(value)
    except (TypeError, ValueError):
        return False, f"{key} must be numeric"
    if num_val != num_val or num_val in (float("inf"), float("-inf")):
        return False, f"{key} must be a finite number"
    lo, hi = bounds
    if not (lo <= num_val <= hi):
        return False, f"{key} must be between {lo} and {hi}"
    return True, value


@app.route("/api/strategy_config", methods=["GET", "POST"])
def api_strategy_config():
    """Read/update live strategy config in gs://btc_trade_log/strategy_config.json.

    GET returns the active dashboard-normalized config.
    POST accepts JSON with any editable keys and persists them to GCS.
    """
    if request.method == "GET":
        live = read_json(GCS_STRATEGY_CONFIG, {}) or {}
        cfg = load_config()
        return jsonify({"ok": True, "config": cfg, "raw": live, "path": GCS_STRATEGY_CONFIG})
    try:
        incoming = request.get_json(silent=True) or {}
        existing = read_json(GCS_STRATEGY_CONFIG, {}) or {}
        if not isinstance(existing, dict):
            existing = {}
        updates = {}
        errors = {}
        for k, v in incoming.items():
            if k not in STRATEGY_CONFIG_ALLOWED_KEYS:
                continue
            ok, result = _validate_strategy_value(k, v)
            if ok:
                updates[k] = result
            else:
                errors[k] = result
        if errors:
            return jsonify({"ok": False, "error": "One or more values were rejected.", "field_errors": errors}), 400
        existing.update(updates)
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        existing["_source"] = "dashboard_control_plane"
        existing["CONFIG_VERSION"] = existing.get("CONFIG_VERSION") or "v1_control_plane"
        write_json(GCS_STRATEGY_CONFIG, existing)
        return jsonify({"ok": True, "updated": updates, "config": existing, "path": GCS_STRATEGY_CONFIG})
    except Exception as e:
        app.logger.exception("api_strategy_config failed")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/spot_toggle", methods=["GET", "POST"])
def api_spot_toggle():
    """Enable/disable Coinbase Spot BTC trading and Spot->Perp bridge from dashboard."""
    try:
        body = request.get_json(silent=True) or {}
        enabled_raw = request.args.get("enabled", body.get("enabled", None))
        if enabled_raw is None:
            cfg = read_json(GCS_STRATEGY_CONFIG, {}) or {}
            enabled = bool(cfg.get("ENABLE_SPOT_BTC_TRADING", False))
            bridge = bool(cfg.get("ENABLE_SPOT_BRIDGE_PERP_BUYS", False))
            return jsonify({"ok": True, "spot_enabled": enabled, "bridge_enabled": bridge, "config": cfg})
        enabled = str(enabled_raw).lower() in ("1", "true", "yes", "on")
        existing = read_json(GCS_STRATEGY_CONFIG, {}) or {}
        if not isinstance(existing, dict):
            existing = {}
        existing["ENABLE_SPOT_BTC_TRADING"] = enabled
        # When disabling Spot, also disable the Spot->Perp bridge so Perp remains isolated.
        existing["ENABLE_SPOT_BRIDGE_PERP_BUYS"] = enabled
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        existing["_source"] = "dashboard_spot_toggle"
        write_json(GCS_STRATEGY_CONFIG, existing)
        _cache.clear()
        return jsonify({"ok": True, "spot_enabled": enabled, "bridge_enabled": enabled, "config": existing})
    except Exception as e:
        # v73 fix: see api_data -- tracebacks are logged server-side, not returned.
        app.logger.exception("request failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/update_strategy_param", methods=["GET", "POST"])
def api_update_strategy_param():
    """Simple single-key parameter update, useful from browser/curl.

    Example: /api/update_strategy_param?key=TSL_ACTIVATION_PCT&value=0.05

    v73 fix: this used to write ANY key straight into strategy_config.json with no
    allowlist at all (unlike /api/strategy_config, which always filtered key names) --
    combined with the dashboard previously having no authentication, that was an
    unauthenticated arbitrary-field injection point into the file the trading engine
    reloads every cycle. Now shares the same allowlist and value bounds.
    """
    body = request.get_json(silent=True) or {}
    key = request.args.get("key") or body.get("key")
    value = request.args.get("value", body.get("value"))
    if not key:
        return jsonify({"ok": False, "error": "missing key"}), 400
    if key not in STRATEGY_CONFIG_ALLOWED_KEYS:
        app.logger.warning("UPDATE_STRATEGY_PARAM_REJECTED key=%s not in allowlist remote=%s", key, request.remote_addr)
        return jsonify({"ok": False, "error": f"{key} is not an editable strategy parameter"}), 400
    raw = read_json(GCS_STRATEGY_CONFIG, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    # Basic parsing: bool/list/float/int/string
    parsed = value
    if value is not None:
        low = str(value).lower()
        if low in ("true", "false"):
            parsed = low == "true"
        elif "," in str(value):
            parsed = [safe_float(x, 0) for x in str(value).split(",") if x.strip()]
        else:
            try:
                parsed = float(value)
                if parsed.is_integer() and key.endswith(("SEC", "LIMIT", "HOURS", "PERIOD", "CONTRACTS")):
                    parsed = int(parsed)
            except Exception:
                parsed = value
    ok, result = _validate_strategy_value(key, parsed)
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    raw[key] = parsed
    raw["updated_at"] = datetime.now(timezone.utc).isoformat()
    raw["_source"] = "dashboard_control_plane_url"
    raw["CONFIG_VERSION"] = raw.get("CONFIG_VERSION") or "v1_control_plane"
    write_json(GCS_STRATEGY_CONFIG, raw)
    return jsonify({"ok": True, "key": key, "value": parsed, "config": raw})

@app.route("/api/reset_clean_book", methods=["GET", "POST"])
def reset_clean_book():
    """Reset trade-attribution ledger without closing Coinbase positions.

    Example:
      /api/reset_clean_book?contracts=6&avg=79780

    This writes tracking_start_timestamp and opening_perp_position to GCS.
    The account baseline is left untouched unless set_baseline=1 is passed.
    """
    try:
        cfg = load_config()
        product_id = request.args.get("product_id") or cfg.get("PERP_PRODUCT_ID") or "BIP-20DEC30-CDE"
        contracts = safe_float(request.args.get("contracts"), 6.0)
        avg = safe_float(request.args.get("avg") or request.args.get("avg_entry_price"), 79780.0)
        side = (request.args.get("side") or "LONG").upper()
        contract_size = safe_float(request.args.get("contract_size_btc"), safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01)) or 0.01
        existing = load_capital_state()
        payload = dict(existing) if isinstance(existing, dict) else {}
        payload["tracking_start_timestamp"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        payload["opening_perp_position"] = {
            "product_id": product_id,
            "side": side,
            "contracts": contracts,
            "avg_entry_price": avg,
            "contract_size_btc": contract_size,
            "source": "manual_coinbase_ui_reset",
        }
        payload["last_updated"] = "clean_book_reset_from_dashboard"
        payload["source"] = "coinbase_only"
        write_json(GCS_CAPITAL, payload)
        _cache.clear()
        return jsonify({"ok": True, "capital_state": payload})
    except Exception as e:
        # v73 fix: see api_data -- tracebacks are logged server-side, not returned.
        app.logger.exception("request failed")
        return jsonify({"ok": False, "error": str(e)}), 500



def _signed_from_position(pos: Dict[str, Any]) -> float:
    side = str(pos.get("side") or "").upper()
    contracts = safe_float(pos.get("contracts"), 0.0) or 0.0
    if side == "SHORT":
        return -abs(contracts)
    if side == "LONG":
        return abs(contracts)
    return 0.0


def _rung_name_for_abs_contracts(abs_contracts: float, cfg: Dict[str, Any]) -> str:
    maxc = int(safe_float(cfg.get("MAX_CONVICTION_CONTRACTS"), 10) or 10)
    probe_pct = safe_float(cfg.get("PROBE_PCT"), 0.20) if cfg.get("PROBE_PCT") is not None else 0.20
    partial_pct = safe_float(cfg.get("PARTIAL_PCT"), 0.40) if cfg.get("PARTIAL_PCT") is not None else 0.40
    strong_pct = safe_float(cfg.get("STRONG_PCT"), 0.70) if cfg.get("STRONG_PCT") is not None else 0.70
    probe = max(1, round(maxc * probe_pct))
    partial = max(probe + 1, round(maxc * partial_pct))
    strong = max(partial + 1, round(maxc * strong_pct))
    n = int(abs(round(abs_contracts)))
    if n <= 0:
        return "FLAT"
    if n <= probe:
        return "PROBE"
    if n <= partial:
        return "PARTIAL"
    if n <= strong:
        return "STRONG"
    return "FULL"


def _extract_order_id(order: Dict[str, Any]) -> str:
    d = order.get("response") if isinstance(order, dict) else {}
    try:
        return (((d.get("success_response") or {}).get("order_id")) or (((d.get("response") or {}).get("success_response") or {}).get("order_id")) or order.get("order_id") or "")
    except Exception:
        return ""


def _fetch_fill_for_order(product_id: str, order_id: str, client_order_id: str = "") -> Dict[str, Any]:
    """Best-effort fill lookup for dashboard emergency exits."""
    if not order_id and not client_order_id:
        return {"found": False, "avg_price": 0.0, "contracts": 0.0, "commission": 0.0, "liquidity": "TAKER", "fills": []}
    try:
        resp = cb().get_fills(product_id=product_id, limit=100)
        fills = getattr(resp, "fills", None) or attr_or_key(resp, "fills", []) or []
        matched = []
        for f in fills or []:
            oid = str(attr_or_key(f, "order_id", ""))
            coid = str(attr_or_key(f, "client_order_id", ""))
            if (order_id and oid == order_id) or (client_order_id and coid == client_order_id):
                matched.append(f)
        qty = sum(safe_float(attr_or_key(f, "size"), 0.0) or 0.0 for f in matched)
        notional = sum((safe_float(attr_or_key(f, "price"), 0.0) or 0.0) * (safe_float(attr_or_key(f, "size"), 0.0) or 0.0) for f in matched)
        fee = sum(safe_float(attr_or_key(f, "commission"), 0.0) or 0.0 for f in matched)
        liq = "TAKER"
        for f in matched:
            liq = str(attr_or_key(f, "liquidity_indicator", "") or liq).upper()
        return {"found": bool(matched), "avg_price": (notional / qty if qty else 0.0), "contracts": qty, "commission": fee, "liquidity": liq or "TAKER", "fills": [jsonable(x) for x in matched]}
    except Exception as e:
        return {"found": False, "avg_price": 0.0, "contracts": 0.0, "commission": 0.0, "liquidity": "TAKER", "fills": [], "error": str(e)}


def _append_emergency_flatten_ledger_rows(now_z: str, before: List[Dict[str, Any]], orders: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Append successful dashboard emergency flatten orders to Larry's canonical ledger.

    Included in P&L because the dashboard is closing Larry-managed exposure, but
    tagged as MANUAL_OVERRIDE_EXIT so it is excluded from normal signal attribution.
    """
    before_by_pid = {str(x.get("product_id")): x for x in before or []}
    contract_size = safe_float(cfg.get("CONTRACT_SIZE_BTC"), 0.01) or 0.01
    appended, new_rows = [], []
    for order in orders or []:
        if not order.get("ok"):
            continue
        pid = str(order.get("product_id") or cfg.get("PERP_PRODUCT_ID") or "BIP-20DEC30-CDE")
        b = before_by_pid.get(pid) or {}
        before_signed = _signed_from_position(b)
        qty = safe_float(order.get("contracts"), 0.0) or 0.0
        action = str(order.get("side") or "").upper()
        mark = safe_float(b.get("current_price"), 0.0) or 0.0
        avg_entry = safe_float(b.get("avg_entry_price"), 0.0) or 0.0
        client_order_id = str(order.get("client_order_id") or "")
        oid = _extract_order_id(order)
        fill = _fetch_fill_for_order(pid, oid, client_order_id)
        fill_price = safe_float(fill.get("avg_price"), 0.0) or mark
        fees = safe_float(fill.get("commission"), 0.0) or 0.0
        gross = None
        if avg_entry and fill_price and qty:
            if before_signed < 0 and action == "BUY":
                gross = (avg_entry - fill_price) * qty * contract_size
            elif before_signed > 0 and action == "SELL":
                gross = (fill_price - avg_entry) * qty * contract_size
        net = (gross - fees) if gross is not None else None
        slip = None
        if mark and fill_price:
            slip = ((fill_price - mark) / mark * 10000.0) if action == "SELL" else ((mark - fill_price) / mark * 10000.0)
        raw_order = dict(order)
        raw_order["fills"] = fill
        fmt = lambda x: int(x) if isinstance(x, float) and float(x).is_integer() else x
        row = [
            now_z, "OPERATOR_EMERGENCY_FLATTEN", "OPERATOR_EMERGENCY_FLATTEN", action,
            fmt(qty), fmt(before_signed), 0, 0, round(mark, 8), round(fill_price, 8),
            round(slip, 3) if slip is not None else "", round(gross, 8) if gross is not None else "",
            round(fees, 8), round(net, 8) if net is not None else "", True, client_order_id,
            "MANUAL_OVERRIDE_EXIT", "EMERGENCY_FLATTEN_DASHBOARD", "OPERATOR_OVERRIDE",
            fmt(before_signed), 0, _rung_name_for_abs_contracts(abs(before_signed), cfg), "FLAT",
            json.dumps(raw_order, separators=(",", ":")),
        ]
        new_rows.append(row)
        appended.append({"product_id": pid, "action": action, "contracts": qty, "fill_price": fill_price, "fees_usd": fees, "gross_realized_pnl_usd": gross, "net_realized_pnl_usd": net, "client_order_id": client_order_id, "order_id": oid, "fill_found": bool(fill.get("found"))})
    if not new_rows:
        return appended
    try:
        existing = read_text_gcs(GCS_PERP_TRADES_LEDGER)
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        if not existing.strip():
            writer.writerow(["timestamp","reason","signal_class","action","contracts","before_signed","target_signed","after_signed","mark_at_send","fill_price","slippage_bps","gross_realized_pnl_usd","fees_usd","net_realized_pnl_usd","ok","client_order_id","trade_intent","execution_reason","signal_reason","target_before","target_after","sizing_rung_before","sizing_rung_after","raw_order"])
        for row in new_rows:
            writer.writerow(row)
        with fs().open(GCS_PERP_TRADES_LEDGER, "wb") as f:
            f.write(((existing.rstrip("\n") + "\n") if existing.strip() else "").encode("utf-8"))
            f.write(out.getvalue().encode("utf-8"))
    except Exception as e:
        for x in appended:
            x["ledger_append_error"] = str(e)
    return appended




def _get_emergency_pin() -> str:
    """Read the operator PIN from Secret Manager. Used for both dashboard login and
    the emergency-flatten second confirmation, and as the emergency-flatten HMAC key.

    Any state-changing operator action must require this server-side secret. This
    PIN only authorizes a GCS request; Coinbase trading keys remain on the Larry VM.
    """
    try:
        return secret("EMERGENCY_FLATTEN_PIN").strip()
    except Exception as e:
        # v73 fix: an unprovisioned secret and a mistyped PIN used to return the
        # identical client-facing error, giving an operator no way to tell them apart
        # during a real emergency. Client response stays generic (don't help an
        # attacker distinguish misconfiguration from a wrong guess); this log line is
        # the operator's signal to check Cloud Run logs and Secret Manager.
        app.logger.error("EMERGENCY_FLATTEN_PIN secret unavailable: %s", e)
        return ""


def _pin_matches(candidate: str) -> bool:
    expected = _get_emergency_pin()
    return bool(expected) and hmac.compare_digest(str(candidate or "").strip(), expected)


def _latest_position_summary_from_state() -> Dict[str, Any]:
    st = read_json(GCS_PERP_STATE, {}) or {}
    pos = st.get("exchange_position") or st.get("last_exchange_position") or {}
    return {
        "side": pos.get("side"),
        "contracts": pos.get("contracts"),
        "signed_contracts": pos.get("signed_contracts"),
        "avg_entry_price": pos.get("avg_entry_price"),
        "current_price": pos.get("current_price"),
        "unrealized_pnl": pos.get("unrealized_pnl"),
        "source": "perp_engine_state.json",
    }

def _emergency_flatten_signable_string(request_id: str, requested_at: str) -> str:
    # Must match _emergency_flatten_signable_string in larry_perp_v1.py exactly --
    # both sides sign/verify the same canonical string with the same secret.
    return f"EMERGENCY_FLATTEN|{request_id}|{requested_at}"


@app.route("/api/emergency_flatten", methods=["POST"])
def api_emergency_flatten():
    """PIN-validated, HMAC-signed emergency flatten request relay.

    v73 security model:
    - Cloud Run dashboard validates operator PIN and exact confirmation (POST only;
      the PIN is never accepted from a query string, which used to leak it into
      access logs, browser history, and Referer headers).
    - The request written to GCS is HMAC-signed with the same EMERGENCY_FLATTEN_PIN
      secret. The Larry VM verifies that signature before executing -- previously it
      trusted any GCS object it found with status=="REQUESTED", with no cryptographic
      link back to a PIN-verified dashboard action.
    - Cloud Run writes emergency_flatten_request.json and bot_halt.json to GCS.
    - Larry VM reads the request and executes the flatten with its existing trading key.
    - Coinbase trading credentials are not required in this dashboard.
    """
    now_z = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    remote = request.remote_addr or "unknown"
    try:
        if _pin_rate_limited(f"flatten:{remote}"):
            app.logger.warning("EMERGENCY_FLATTEN_RATE_LIMITED remote=%s", remote)
            return jsonify({"ok": False, "error": "Too many attempts. Wait 15 minutes and try again."}), 429
        body = request.get_json(silent=True) or {}
        confirm = str(body.get("confirm") or "").strip().upper()
        pin = str(body.get("pin") or "").strip()
        if confirm != "FLATTEN":
            app.logger.warning("EMERGENCY_FLATTEN_REQUEST_REJECTED missing_confirm remote=%s", remote)
            return jsonify({"ok": False, "error": "Confirmation required. Type FLATTEN."}), 400
        _record_pin_attempt(f"flatten:{remote}")
        if not _pin_matches(pin):
            app.logger.warning("EMERGENCY_FLATTEN_REQUEST_REJECTED bad_pin remote=%s", remote)
            return jsonify({"ok": False, "error": "Invalid emergency PIN."}), 403

        request_id = f"dash-emergency-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        pos_hint = _latest_position_summary_from_state()
        halt_payload = {
            "halt": True,
            "reason": "emergency_flatten_from_dashboard_pending_vm_execution",
            "set_by": remote,
            "set_at": now_z,
            "request_id": request_id,
        }
        signing_secret = _get_emergency_pin()
        signature = hmac.new(signing_secret.encode("utf-8"), _emergency_flatten_signable_string(request_id, now_z).encode("utf-8"), hashlib.sha256).hexdigest() if signing_secret else ""
        request_payload = {
            "status": "REQUESTED",
            "request_id": request_id,
            "requested_at": now_z,
            "requested_by": remote,
            "source": "dashboard_pin_validated_gcs_request",
            "confirm": "FLATTEN",
            "max_age_seconds": 300,
            "position_hint": pos_hint,
            "signature": signature,
            "operator_note": "Dashboard PIN accepted and request signed. Larry VM must verify the signature, execute, and write COMPLETE_FLAT / FAILED_NOT_FLAT.",
        }
        write_json(GCS_BOT_HALT, halt_payload)
        write_json(GCS_EMERGENCY_FLATTEN_REQUEST, request_payload)
        app.logger.warning("EMERGENCY_FLATTEN_REQUEST_WRITTEN request_id=%s remote=%s pos_hint=%s", request_id, remote, pos_hint)
        send_dashboard_telegram_message(
            f"🛑 EMERGENCY FLATTEN REQUESTED\nPIN accepted. Larry VM will verify signature and execute using trading key.\nPosition hint: {pos_hint.get('side')} {pos_hint.get('contracts')}\nRequest: {request_id}\nTime: {now_z}"
        )
        _cache.clear()
        return jsonify({
            "ok": True,
            "status": "REQUESTED_VM_EXECUTION",
            "request_id": request_id,
            "halt_state": halt_payload,
            "position_hint": pos_hint,
            "message": "Emergency flatten request accepted. Larry VM will execute on the next cycle and record ledger/Telegram results.",
        })
    except Exception as e:
        app.logger.exception("EMERGENCY_FLATTEN_REQUEST_FAILED")
        return jsonify({"ok": False, "status": "FAILED_EXCEPTION", "error": str(e)}), 500


def _record_emergency_flatten(now_z: str, halt_payload: Dict[str, Any], before: List[Dict[str, Any]], orders: List[Dict[str, Any]], after_positions: List[Dict[str, Any]], failures: List[str], status: str, ledger_events: Optional[List[Dict[str, Any]]] = None) -> None:
    """Persist the latest emergency flatten attempt for dashboard visibility."""
    try:
        cfg = load_config()
        st = read_json(GCS_PERP_STATE, {}) or {}
        st["emergency_flatten_last"] = {"at": now_z, "status": status, "halt_state": halt_payload, "before": before, "orders": orders, "after_positions": after_positions, "failures": failures, "ledger_events": ledger_events or []}
        if status == "SUCCESS_FLAT" or status == "NO_POSITION_DETECTED":
            flat = {"product_id": cfg.get("PERP_PRODUCT_ID", "BIP-20DEC30-CDE"), "side": "FLAT", "contracts": 0, "signed_contracts": 0, "avg_entry_price": 0.0, "current_price": 0.0, "unrealized_pnl": 0.0, "daily_realized_pnl": 0.0, "raw": None}
            st["exchange_position"] = flat
            st["last_exchange_position"] = flat
            st["bot_managed_position"] = None
            st["manual_position_status"] = {"mode": "monitor_only", "is_manual_or_external": False, "bot_managed": False, "allow_bot_to_trade_position": True, "reason": "emergency_flatten_reconciled_flat", "bot_managed_signed": 0, "live_signed": 0}
            st["last_core_target_plan"] = None
            st["last_order_plan"] = None
            st["last_blocked_action"] = {}
            if ledger_events:
                st["last_completed_trade"] = {"ok": True, "reason": "OPERATOR_EMERGENCY_FLATTEN", "trade_intent": "MANUAL_OVERRIDE_EXIT", "ledger_events": ledger_events, "after": flat, "at": now_z}
                st["last_realized_trade"] = st["last_completed_trade"]
        st["updated_at"] = now_z
        st["last_updated"] = now_z
        write_json(GCS_PERP_STATE, st)
    except Exception:
        pass

@app.route("/api/emergency_flatten_last", methods=["GET"])
def api_emergency_flatten_last():
    """Show last emergency flatten audit record from GCS state."""
    st = read_json(GCS_PERP_STATE, {}) or {}
    return jsonify({"ok": True, "emergency_flatten_last": st.get("emergency_flatten_last"), "exchange_position": st.get("exchange_position"), "kill_switch": st.get("kill_switch")})


@app.route("/api/halt", methods=["POST"])
def api_halt():
    """Activate the kill switch. The bot reads gs://btc_trade_log/bot_halt.json
    on every cycle and skips ALL order placement when halt=true. Telemetry,
    state tracking, and email reconciliation continue normally.

    Requires a logged-in session (see _require_login). POST-only: this used to also
    accept GET with query-string params, which meant a bare link (or an <img src>,
    classic no-JS CSRF) could flip the kill switch for anyone who had the URL.

    Optional JSON body:
        reason: free-text reason recorded with the halt
        set_by: operator identifier (defaults to remote addr)
    """
    try:
        body = request.get_json(silent=True) or {}
        reason = (body.get("reason") or "").strip() or "operator_initiated"
        set_by = (body.get("set_by") or request.remote_addr or "dashboard").strip()
        payload = {
            "halt": True,
            "reason": reason,
            "set_by": set_by,
            "set_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        write_json(GCS_BOT_HALT, payload)
        _cache.clear()
        return jsonify({"ok": True, "halt_state": payload})
    except Exception as e:
        # v73 fix: see api_data -- tracebacks are logged server-side, not returned.
        app.logger.exception("request failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/resume", methods=["POST"])
def api_resume():
    """Clear the kill switch. Bot resumes order placement on its next cycle.

    Requires a logged-in session. POST-only -- see api_halt for why GET was removed.
    """
    try:
        body = request.get_json(silent=True) or {}
        set_by = (body.get("set_by") or request.remote_addr or "dashboard").strip()
        payload = {
            "halt": False,
            "reason": "",
            "set_by": set_by,
            "set_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        write_json(GCS_BOT_HALT, payload)
        _cache.clear()
        return jsonify({"ok": True, "halt_state": payload})
    except Exception as e:
        # v73 fix: see api_data -- tracebacks are logged server-side, not returned.
        app.logger.exception("request failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/halt_status")
def api_halt_status():
    """Return the current kill-switch state (does not contact the exchange)."""
    try:
        state = read_json(GCS_BOT_HALT, {}) or {}
        return jsonify({"ok": True, "halt_state": state, "path": GCS_BOT_HALT})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/fee_audit")
def api_fee_audit():
    """Audit exactly which Coinbase fills are counted in Fees since reset."""
    try:
        cfg = load_config()
        cap = load_capital_state()
        tracking_start = cap.get("tracking_start_timestamp")
        product_id = cfg.get("PERP_PRODUCT_ID", DEFAULT_CONFIG["PERP_PRODUCT_ID"])
        fills = recent_fills(product_id, limit=100, tracking_start=tracking_start)
        return jsonify({
            "ok": True,
            "product_id": product_id,
            "tracking_start_timestamp": tracking_start,
            "scope": fills.get("scope"),
            "fees_since_reset": fills.get("fees_since_reset"),
            "fees_today_since_reset": fills.get("fees_today_since_reset"),
            "fills_counted": fills.get("count"),
            "old_fills_ignored": fills.get("ignored_before_tracking_start"),
            "rows": fills.get("rows", [])[:50],
            "note": "Fees since reset = sum(commission) for Coinbase fills with trade_time >= tracking_start_timestamp. Historical fills are ignored."
        })
    except Exception as e:
        # v73 fix: see api_data -- tracebacks are logged server-side, not returned.
        app.logger.exception("request failed")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/signal_pnl_rollup")
def api_signal_pnl_rollup():
    """Return signal-class P&L rollup CSV as JSON for dashboard transparency.

    The file is created by analyze_signal_pnl.py and stored at
    gs://btc_trade_log/signal_pnl_rollup.csv. If it has not been generated yet,
    this returns an empty table with a helpful note.
    """
    try:
        df = read_csv(GCS_SIGNAL_PNL_ROLLUP)
        if df.empty:
            return jsonify({
                "ok": True,
                "rows": [],
                "path": GCS_SIGNAL_PNL_ROLLUP,
                "note": "No signal_pnl_rollup.csv found yet. Run analyze_signal_pnl.py --write on the VM to generate it."
            })
        return jsonify({"ok": True, "rows": df.fillna("").to_dict(orient="records"), "path": GCS_SIGNAL_PNL_ROLLUP})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "path": GCS_SIGNAL_PNL_ROLLUP}), 500


@app.route("/api/raw")
def api_raw():
    return jsonify({
        "strategy_config": read_json(GCS_STRATEGY_CONFIG, {}),
        "halt_state": read_json(GCS_BOT_HALT, {}),
        "capital": read_json(GCS_CAPITAL, {}),
        "spot_state": read_json(GCS_SPOT_STATE, {}),
        "perp_state": read_json(GCS_PERP_STATE, {}),
        "heartbeat": read_first_json(GCS_HEARTBEAT_CANDIDATES, {}),
        "config": load_config(),
    })

# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────
HTML = r'''
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Larry BTC Perp Command Center</title>

<style>
:root{--bg:#07080b;--card:#0e1218;--card2:#141a23;--panel:#0b0e14;--line:#212a37;--border:#212a37;--text:#e9eef5;--sub:#94a1b6;--muted:#5f6a7c;--brand:#f7931a;--brand-soft:rgba(247,147,26,.13);--green:#2dd47e;--red:#f0565c;--orange:#f0c440;--blue:#4da3ff;--purple:#b191f7;--yellow:#facc15}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:radial-gradient(1200px 600px at 85% -10%,rgba(247,147,26,.07),transparent 60%),radial-gradient(900px 500px at -10% 0,rgba(77,163,255,.05),transparent 55%),var(--bg);color:var(--text);font-family:-apple-system,"SF Pro Text","Segoe UI",system-ui,Roboto,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.app{max-width:1440px;margin:0 auto;padding:16px}
.hero{position:relative;background:linear-gradient(160deg,#141a23 0%,#0c1016 55%,#0a0d12 100%);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-bottom:14px;overflow:hidden}
.hero::before{content:"";position:absolute;inset:0 0 auto 0;height:2px;background:linear-gradient(90deg,transparent,var(--brand) 30%,#ffb964 50%,var(--brand) 70%,transparent);opacity:.85}
.hero::after{content:"";position:absolute;top:-140px;right:-80px;width:420px;height:320px;background:radial-gradient(closest-side,rgba(247,147,26,.15),transparent);pointer-events:none}
.hero-row{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;position:relative}
.brand{font-weight:800;font-size:1.3rem;letter-spacing:-.01em}
.brand::first-letter{color:var(--brand)}
.sub{color:var(--sub);font-size:.8rem;margin-top:4px}
.price{font-size:1.7rem;font-weight:800;text-align:right;font-variant-numeric:tabular-nums;letter-spacing:-.01em;color:#ffedd2;text-shadow:0 0 26px rgba(247,147,26,.3)}
.pill-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;position:relative}
.pill{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;border-radius:8px;font-size:.72rem;font-weight:700;letter-spacing:.03em;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--sub);line-height:1.2}
.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--orange)}.blue{color:var(--blue)}.purple{color:var(--purple)}.muted{color:var(--muted)}
.pill.good{background:rgba(45,212,126,.10);border-color:rgba(45,212,126,.35)}
.pill.bad{background:rgba(240,86,92,.10);border-color:rgba(240,86,92,.35)}
.pill.warn{background:rgba(240,196,64,.10);border-color:rgba(240,196,64,.35)}
.pill.blue{background:rgba(77,163,255,.10);border-color:rgba(77,163,255,.35)}
.pill.purple{background:rgba(177,145,247,.10);border-color:rgba(177,145,247,.35)}
.warnbox,.errbox{display:none;border-radius:12px;padding:11px 13px;margin:12px 0;font-size:.85rem}
.warnbox{background:rgba(240,196,64,.08);border:1px solid rgba(240,196,64,.32);color:#f5dea1}
.errbox{background:rgba(240,86,92,.10);border:1px solid rgba(240,86,92,.35);white-space:pre-wrap;color:#ffc1c4}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
.card{background:linear-gradient(180deg,rgba(255,255,255,.015),transparent 40%),var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:0 1px 0 rgba(255,255,255,.03) inset,0 10px 30px rgba(0,0,0,.35)}
.span3{grid-column:span 3}.span4{grid-column:span 4}.span5{grid-column:span 5}.span6{grid-column:span 6}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}
h2{margin:0 0 12px;font-size:.9rem;letter-spacing:.01em;font-weight:700;display:flex;align-items:center;flex-wrap:wrap;gap:8px}
h2::before{content:"";width:3px;height:14px;border-radius:2px;background:linear-gradient(180deg,var(--brand),#b96c05);flex:none}
h3{margin:0 0 8px;font-size:.78rem;color:var(--sub);font-weight:700}
.big{font-size:1.6rem;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.kv{display:flex;justify-content:space-between;gap:10px;border-top:1px solid rgba(255,255,255,.05);padding:9px 0}
.kv:first-of-type{border-top:0}
.k{color:var(--sub);font-size:.76rem}
.v{font-weight:700;font-size:.8rem;text-align:right;font-variant-numeric:tabular-nums}
.mini{font-size:.71rem;color:var(--muted)}
.metric-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.metric{background:var(--card2);border:1px solid rgba(255,255,255,.05);border-radius:11px;padding:12px;transition:border-color .18s ease}
.metric:hover{border-color:rgba(247,147,26,.3)}
.metric .label{font-size:.64rem;color:var(--sub);font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.metric .val{font-size:1.12rem;font-weight:800;margin-top:6px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.bar{height:10px;background:var(--panel);border-radius:999px;overflow:hidden;border:1px solid rgba(255,255,255,.05)}
.fill{height:100%;width:0%;background:linear-gradient(90deg,var(--blue),#7cc0ff);border-radius:999px}
.gauge{position:relative;height:14px;background:var(--panel);border-radius:999px;border:1px solid rgba(255,255,255,.07);overflow:hidden}
.zone{position:absolute;top:0;height:100%;background:rgba(148,163,184,.20)}
.sig-card.active .zone{background:rgba(45,212,126,.5);box-shadow:0 0 16px rgba(45,212,126,.3)}
.needle{position:absolute;top:-3px;width:3px;height:20px;background:#fff;border-radius:2px;box-shadow:0 0 8px rgba(255,255,255,.7)}
.sig-card.active .needle{background:var(--green);box-shadow:0 0 12px rgba(45,212,126,.9)}
.sig-card{background:var(--card2);border:1px solid rgba(255,255,255,.05);border-radius:12px;padding:12px;margin-bottom:10px}
.sig-card.active{border-color:rgba(45,212,126,.55);background:linear-gradient(180deg,rgba(45,212,126,.10),var(--card2))}
.sig-head{display:flex;justify-content:space-between;margin-bottom:8px}
.sig-name{font-weight:800}
.sig-val{font-weight:800;font-variant-numeric:tabular-nums}
.sig-desc{display:flex;justify-content:space-between;color:var(--sub);font-size:.71rem;margin-top:5px}
.score-pips{display:flex;gap:6px}
.score-pip{height:13px;flex:1;border-radius:999px;background:#1b222d}
.score-pip.on{background:linear-gradient(90deg,var(--green),#8ff0bd);box-shadow:0 0 10px rgba(45,212,126,.35)}
.ladder{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.slot{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:12px;text-align:center}
.slot.on{background:rgba(45,212,126,.12);border-color:rgba(45,212,126,.5)}
.slot.manual{background:rgba(177,145,247,.12);border-color:rgba(177,145,247,.45)}
.slot .num{font-size:1.1rem;font-weight:800;font-variant-numeric:tabular-nums}
.slot .lbl{font-size:.68rem;color:var(--sub);margin-top:3px}
.table-wrap{overflow:auto}
.table{width:100%;border-collapse:collapse;font-size:.76rem}
.table th{text-align:left;color:var(--muted);font-weight:700;font-size:.66rem;text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid var(--line);padding:8px}
.table td{border-bottom:1px solid rgba(255,255,255,.05);padding:8px;white-space:nowrap;font-variant-numeric:tabular-nums}
.table tbody tr:hover{background:rgba(255,255,255,.025)}
.chartbox{height:230px}
.hist-row{display:flex;align-items:center;gap:6px;border-bottom:1px solid rgba(255,255,255,.05);padding:7px 0}
.hist-ts{width:62px;color:var(--sub);font-size:.71rem;font-variant-numeric:tabular-nums}
.hist-score{width:24px;height:22px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-weight:800;background:#1b222d}
.hist-score.s3{background:rgba(240,196,64,.2);color:var(--orange)}
.hist-score.s4{background:rgba(45,212,126,.2);color:var(--green)}
.sig-pip{width:18px;height:18px;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;font-size:.6rem;font-weight:800;margin-right:3px}
.sp-on{background:rgba(45,212,126,.22);color:var(--green)}
.sp-off{background:#1b222d;color:var(--muted)}
.status-banner{display:flex;align-items:center;justify-content:space-between;gap:12px;border-radius:12px;padding:14px 16px;margin-bottom:12px;font-weight:800;border:1px solid rgba(255,255,255,.08)}
.status-banner.enabled{background:rgba(45,212,126,.10);border-color:rgba(45,212,126,.4);color:#9df0c3}
.status-banner.blocked{background:rgba(240,86,92,.10);border-color:rgba(240,86,92,.4);color:#ffc1c4}
.status-banner.unknown{background:rgba(240,196,64,.09);border-color:rgba(240,196,64,.35);color:#f5dea1}
.macro-card{background:linear-gradient(155deg,var(--card2),var(--card))}
.macro-top{display:grid;grid-template-columns:1.2fr 2fr;gap:14px;align-items:stretch}
.macro-state{font-size:2.1rem;font-weight:800;letter-spacing:-.03em;margin:8px 0}
.macro-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:10px 0}
.check.yes{color:var(--green)}.check.no{color:var(--red)}
.pnl-note{font-size:.71rem;color:var(--sub);line-height:1.4;margin-top:8px}
.method-badge{display:inline-block;padding:3px 8px;border-radius:6px;background:var(--brand-soft);color:#ffbf69;font-size:.62rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase;margin-left:0}
.reference-row{background:rgba(148,163,184,.05);color:var(--sub)}
.reference-row td:first-child{color:var(--yellow);font-weight:800}
.risk-hero{display:grid;grid-template-columns:1.1fr 2fr;gap:14px;align-items:stretch}
.risk-main{background:linear-gradient(150deg,rgba(45,212,126,.10),var(--panel));border:1px solid rgba(45,212,126,.28);border-radius:13px;padding:14px}
.risk-main.short{background:linear-gradient(150deg,rgba(240,86,92,.11),var(--panel));border-color:rgba(240,86,92,.35)}
.risk-main.tsl-on{background:linear-gradient(150deg,rgba(45,212,126,.18),var(--panel));border-color:rgba(45,212,126,.6);box-shadow:0 0 0 1px rgba(45,212,126,.08),0 10px 28px rgba(45,212,126,.07)}
.risk-side{font-size:1.75rem;font-weight:800;letter-spacing:-.02em}
.risk-contracts{font-size:.88rem;color:var(--sub);margin-top:2px}
.risk-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.risk-stop-card{margin-top:12px;background:var(--panel);border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:12px}
.stop-line{position:relative;height:16px;background:#141924;border-radius:999px;border:1px solid rgba(255,255,255,.07);overflow:hidden;margin:9px 0}
.stop-progress{height:100%;background:linear-gradient(90deg,var(--blue),var(--green));width:0%;border-radius:999px}
.stop-progress.active{background:linear-gradient(90deg,var(--green),#8ff0bd);box-shadow:0 0 16px rgba(45,212,126,.35)}
.risk-status{font-size:.76rem;color:var(--sub);line-height:1.4}
.stop-badge{display:inline-block;border-radius:7px;padding:5px 8px;font-size:.64rem;font-weight:800;letter-spacing:.05em;text-transform:uppercase;border:1px solid rgba(255,255,255,.1);background:rgba(148,163,184,.1)}
.stop-badge.tsl{color:#9df0c3;background:rgba(45,212,126,.1);border-color:rgba(45,212,126,.35)}
.stop-badge.atr{color:#f5dea1;background:rgba(240,196,64,.1);border-color:rgba(240,196,64,.35)}
.stop-badge.manual{color:#cfe1ff;background:rgba(77,163,255,.12);border-color:rgba(77,163,255,.4)}
@media(max-width:1000px){.risk-hero{grid-template-columns:1fr}.risk-grid{grid-template-columns:repeat(2,1fr)}.span3,.span4,.span5,.span6,.span7,.span8,.span12{grid-column:span 12}.metric-row{grid-template-columns:repeat(2,1fr)}.macro-top{grid-template-columns:1fr}.macro-grid{grid-template-columns:repeat(2,1fr)}.hero-row{flex-direction:column}.price{text-align:left}.ladder{grid-template-columns:repeat(2,1fr)}}
@media(max-width:640px){.app{padding:10px}.risk-hero{display:block}.risk-main{padding:12px;margin-bottom:10px}.risk-side{font-size:1.35rem}.risk-contracts{font-size:.78rem}.risk-grid{grid-template-columns:1fr!important;gap:8px}.risk-grid .metric{padding:10px}.risk-stop-card{padding:10px}.stop-badge{font-size:.62rem;padding:4px 7px}.risk-status{font-size:.72rem}.hero{border-radius:16px;padding:14px}.card{border-radius:16px;padding:12px}h2{font-size:.95rem}.metric .val{font-size:1rem}.kv{font-size:.78rem}.table{font-size:.72rem}}
.monitor-pips{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin:8px 0}
.monitor-pip{padding:7px 8px;border-radius:8px;background:#1b222d;color:var(--muted);font-size:.7rem;font-weight:700;border:1px solid rgba(255,255,255,.05)}
.monitor-pip.on{background:rgba(45,212,126,.14);border-color:rgba(45,212,126,.5);color:#9df0c3}
.monitor-pip.short.on{background:rgba(240,86,92,.14);border-color:rgba(240,86,92,.5);color:#ffc1c4}
.rule-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.rule-card{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:10px}
.rule-title{font-size:.62rem;color:var(--sub);font-weight:700;text-transform:uppercase;letter-spacing:.07em}
.rule-current{font-size:.8rem;font-weight:800;margin-top:4px;font-variant-numeric:tabular-nums}
.rule-status{font-size:.66rem;margin-top:5px;font-weight:800}
.rule-status.ok,.rule-status.active,.rule-status.open{color:var(--green)}
.rule-status.monitored{color:var(--blue)}
.rule-status.modified{color:var(--orange)}
.rule-status.blocked,.rule-status.halted,.rule-status.paused{color:var(--red)}
.rule-status.needs{color:var(--orange)}
@media(max-width:1000px){.rule-grid{grid-template-columns:repeat(2,1fr)}}
.pnl-visual{margin:12px 0;background:var(--panel);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:12px}
.pnl-bar-title{font-size:.66rem;color:var(--sub);font-weight:700;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px}
.pnl-bar-row{display:grid;grid-template-columns:120px 1fr 90px;gap:8px;align-items:center;margin:7px 0}
.pnl-bar-label{font-size:.7rem;color:var(--sub);font-weight:700}
.pnl-bar-track{height:12px;background:#141924;border-radius:999px;overflow:hidden;border:1px solid rgba(255,255,255,.05)}
.pnl-bar-fill{height:100%;width:0%;border-radius:999px;background:var(--green)}
.pnl-bar-fill.neg{background:var(--red)}
.pnl-bar-fill.neu{background:var(--blue)}
.pnl-bar-value{font-size:.71rem;font-weight:800;text-align:right;font-variant-numeric:tabular-nums}
.diagnostic-card{background:rgba(11,14,20,.72);border-style:dashed}
.diagnostic-card h2{color:#aebdd1}
.headline-note{font-size:.71rem;color:var(--muted);margin-top:6px}
.card h2 .method-badge{float:none}

.control-grid{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:12px;align-items:end}
.control-grid label{font-size:10.5px;color:var(--muted);display:flex;flex-direction:column;gap:6px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.control-grid input,.control-grid select{background:var(--panel);border:1px solid #2a3442;color:var(--text);border-radius:9px;padding:10px;font-size:14px;font-variant-numeric:tabular-nums}
.control-grid input:focus,.control-grid select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(247,147,26,.14)}
.btn{background:linear-gradient(180deg,#ffa838,#f28c0f);color:#1c1104;border:0;border-radius:10px;padding:11px 14px;font-weight:800;cursor:pointer;letter-spacing:.02em;box-shadow:0 2px 12px rgba(247,147,26,.22);transition:filter .15s ease,transform .1s ease}
.btn:hover{filter:brightness(1.08)}
.btn:active{transform:translateY(1px)}
.btn-danger{background:linear-gradient(180deg,#ff6f74,#e5484d);color:#fff;box-shadow:0 2px 12px rgba(229,72,77,.25)}
.btn-warn{background:linear-gradient(180deg,#ffd75e,#f0c440);color:#1c1504;box-shadow:0 2px 12px rgba(240,196,64,.2)}
.halt-panel{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:12px;align-items:center}
.halt-status{border-radius:12px;padding:13px;border:1px solid rgba(255,255,255,.09);font-weight:800}
.halt-status.on{background:rgba(240,86,92,.14);border-color:rgba(240,86,92,.55);color:#ffc1c4}
.halt-status.off{background:rgba(45,212,126,.12);border-color:rgba(45,212,126,.45);color:#9df0c3}
.halt-actions{display:flex;gap:10px;flex-wrap:wrap}
.halt-actions .btn{min-width:110px}
@media(max-width:760px){.halt-panel{grid-template-columns:1fr}.halt-actions .btn{width:100%}}
@media(max-width:900px){.control-grid{grid-template-columns:1fr 1fr}.control-grid .btn{grid-column:1/-1;width:100%;padding:13px 14px}}
@media(max-width:560px){.risk-grid{grid-template-columns:1fr}.control-grid{grid-template-columns:1fr}.control-grid label{font-size:10px}.control-grid input,.control-grid select{width:100%;box-sizing:border-box}.control-grid .btn{grid-column:1;width:100%}}

/* v26 mobile hardening for Live Perp Position Risk Card */

.risk-flat-card{border:1px solid rgba(148,163,184,.18);border-radius:18px;padding:16px;background:rgba(15,23,42,.42);display:flex;flex-direction:column;gap:10px;}

.risk-hero{width:100%;max-width:100%;box-sizing:border-box;}
.risk-hero,.risk-main,.risk-grid,.risk-stop-card{min-width:0;}
.risk-grid .metric{min-width:0;overflow:hidden;}
.risk-grid .metric .val{overflow-wrap:anywhere;word-break:break-word;}
.risk-stop-head{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap;}
@media(max-width:760px){
  #perpRiskCard{width:100%;max-width:100%;overflow:hidden;}
  #perpRiskCard .risk-hero{display:flex!important;flex-direction:column!important;gap:10px!important;}
  #perpRiskCard .risk-main{width:100%!important;box-sizing:border-box!important;margin:0!important;padding:12px!important;}
  #perpRiskCard .risk-grid{display:grid!important;grid-template-columns:1fr!important;gap:8px!important;width:100%!important;box-sizing:border-box!important;}
  #perpRiskCard .risk-grid .metric{width:100%!important;box-sizing:border-box!important;padding:10px 11px!important;border-radius:12px!important;}
  #perpRiskCard .risk-grid .metric .label{font-size:.68rem!important;line-height:1.2!important;}
  #perpRiskCard .risk-grid .metric .val{font-size:1.02rem!important;line-height:1.15!important;letter-spacing:-.02em!important;}
  #perpRiskCard .risk-grid .metric .mini{font-size:.68rem!important;line-height:1.25!important;}
  #perpRiskCard .risk-side{font-size:1.35rem!important;line-height:1.05!important;}
  #perpRiskCard .risk-contracts{font-size:.78rem!important;line-height:1.25!important;overflow-wrap:anywhere!important;}
  #perpRiskCard .risk-stop-card{width:100%!important;box-sizing:border-box!important;padding:10px!important;margin-top:10px!important;}
  #perpRiskCard .risk-stop-head{display:flex!important;flex-direction:column!important;align-items:flex-start!important;gap:6px!important;}
  #perpRiskCard .stop-line{height:14px!important;margin:8px 0!important;}
  #perpRiskCard .risk-status{font-size:.70rem!important;line-height:1.3!important;overflow-wrap:anywhere!important;}
  #perpRiskCard .stop-badge{font-size:.62rem!important;padding:4px 7px!important;}
}
@media(max-width:390px){
  #perpRiskCard .risk-grid .metric .val{font-size:.96rem!important;}
  #perpRiskCard .risk-grid .metric{padding:9px 10px!important;}
}


.v12-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.v12-card{background:#0b0e14;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:12px;min-width:0}.v12-title{font-size:.72rem;color:var(--sub);font-weight:900}.v12-val{font-size:1.05rem;font-weight:950;margin-top:5px;overflow-wrap:anywhere}.v12-note{font-size:.70rem;color:var(--muted);line-height:1.3;margin-top:5px}.v12-banner{border-radius:14px;padding:12px;margin-bottom:12px;border:1px solid rgba(59,130,246,.38);background:rgba(59,130,246,.12);color:#bfdbfe;font-size:.82rem;font-weight:850}.v12-banner.manual{border-color:rgba(245,158,11,.45);background:rgba(245,158,11,.12);color:#fed7aa}.v12-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.btn-small{padding:8px 10px;border-radius:10px;font-size:.78rem}.rollup-table{margin-top:10px;max-height:220px;overflow:auto}@media(max-width:1000px){.v12-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:640px){.v12-grid{grid-template-columns:1fr}.v12-card{padding:10px}.v12-val{font-size:.98rem}.v12-actions .btn{width:100%}}

.intel-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.intel-card{background:#0b0e14;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:12px;min-width:0}.intel-title{font-size:.72rem;color:var(--sub);font-weight:900}.intel-val{font-size:1.08rem;font-weight:950;margin-top:5px;overflow-wrap:anywhere}.intel-note{font-size:.70rem;color:var(--muted);line-height:1.3;margin-top:5px}.intel-card.manual{border-color:rgba(245,158,11,.45);background:rgba(245,158,11,.10)}.intel-card.goodborder{border-color:rgba(34,197,94,.35)}.intel-card.warnborder{border-color:rgba(245,158,11,.35)}.intel-card.badborder{border-color:rgba(239,68,68,.35)}.tiny-table{width:100%;border-collapse:collapse;font-size:.72rem}.tiny-table th,.tiny-table td{border-bottom:1px solid rgba(255,255,255,.07);padding:6px;text-align:left;white-space:nowrap}.tiny-scroll{max-height:190px;overflow:auto;margin-top:8px}@media(max-width:1000px){.intel-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:640px){.intel-grid{grid-template-columns:1fr}.intel-card{padding:10px}.intel-val{font-size:.98rem}}


/* v34 BTC Trigger Map */
.trigger-map-wrap{display:grid;grid-template-columns:1.05fr 1.4fr;gap:14px;align-items:stretch}.trigger-hero{background:linear-gradient(135deg,rgba(56,189,248,.15),rgba(15,23,42,.9));border:1px solid rgba(56,189,248,.35);border-radius:18px;padding:16px;position:relative;overflow:hidden}.trigger-hero:before{content:"";position:absolute;inset:-40%;background:radial-gradient(circle,rgba(56,189,248,.18),transparent 45%);animation:pulseGlow 5s ease-in-out infinite}.trigger-hero>*{position:relative}.trigger-price{font-size:2.25rem;font-weight:1000;letter-spacing:-.05em;margin-top:6px}.trigger-next{margin-top:10px;padding:10px 12px;border-radius:14px;background:rgba(2,6,23,.42);border:1px solid rgba(255,255,255,.08);font-size:.82rem;line-height:1.35}.trigger-map{position:relative;height:430px;background:linear-gradient(180deg,rgba(239,68,68,.08),rgba(56,189,248,.07) 48%,rgba(34,197,94,.09));border:1px solid rgba(255,255,255,.08);border-radius:18px;overflow:hidden;padding:12px}.trigger-axis{position:absolute;left:118px;top:18px;bottom:18px;width:2px;background:linear-gradient(180deg,rgba(239,68,68,.65),rgba(56,189,248,.75),rgba(34,197,94,.65));box-shadow:0 0 20px rgba(56,189,248,.25)}.trigger-marker{position:absolute;left:0;right:10px;display:grid;grid-template-columns:108px 1fr;align-items:center;transform:translateY(-50%);gap:10px}.trigger-marker .level{font-size:.72rem;color:var(--sub);text-align:right;font-weight:850}.trigger-marker .line{height:2px;background:rgba(148,163,184,.32);position:relative}.trigger-marker .line:before{content:"";position:absolute;left:0;top:-4px;width:10px;height:10px;border-radius:50%;background:var(--blue);box-shadow:0 0 14px rgba(56,189,248,.65)}.trigger-marker .tag{position:absolute;left:20px;top:-14px;padding:5px 9px;border-radius:999px;background:rgba(15,23,42,.96);border:1px solid rgba(255,255,255,.12);font-size:.68rem;font-weight:900;white-space:nowrap}.trigger-marker.current .line{height:3px;background:rgba(56,189,248,.85);box-shadow:0 0 18px rgba(56,189,248,.45)}.trigger-marker.current .tag{background:rgba(56,189,248,.18);border-color:rgba(56,189,248,.65);color:#bae6fd}.trigger-marker.buy .line:before{background:var(--green);box-shadow:0 0 14px rgba(34,197,94,.7)}.trigger-marker.buy .tag{color:#bbf7d0;border-color:rgba(34,197,94,.42);background:rgba(34,197,94,.13)}.trigger-marker.sell .line:before{background:var(--red);box-shadow:0 0 14px rgba(239,68,68,.7)}.trigger-marker.sell .tag{color:#fecaca;border-color:rgba(239,68,68,.45);background:rgba(239,68,68,.13)}.trigger-marker.warn .line:before{background:var(--orange);box-shadow:0 0 14px rgba(245,158,11,.7)}.trigger-marker.warn .tag{color:#fed7aa;border-color:rgba(245,158,11,.45);background:rgba(245,158,11,.13)}.trigger-marker.manual .line:before{background:var(--purple);box-shadow:0 0 14px rgba(167,139,250,.7)}.trigger-marker.manual .tag{color:#ddd6fe;border-color:rgba(167,139,250,.45);background:rgba(167,139,250,.13)}.trigger-legend{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.legend-pill{border:1px solid rgba(255,255,255,.10);background:rgba(15,23,42,.58);border-radius:999px;padding:6px 9px;font-size:.68rem;font-weight:850;color:var(--sub)}.trigger-table{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:10px}.trigger-tile{background:#0b0e14;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:10px}.trigger-tile .label{font-size:.68rem;color:var(--sub);font-weight:850}.trigger-tile .val{font-weight:950;margin-top:4px}.trigger-tile .note{font-size:.67rem;color:var(--muted);margin-top:4px;line-height:1.25}@keyframes pulseGlow{0%,100%{opacity:.45;transform:scale(1)}50%{opacity:.9;transform:scale(1.06)}}@media(max-width:900px){.trigger-map-wrap{grid-template-columns:1fr}.trigger-map{height:500px}.trigger-axis{left:96px}.trigger-marker{grid-template-columns:86px 1fr}.trigger-marker .tag{font-size:.62rem;max-width:210px;overflow:hidden;text-overflow:ellipsis}.trigger-table{grid-template-columns:1fr}.trigger-price{font-size:1.75rem}}


/* v36 Interactive Trigger Map */
.trigger-control-grid{display:grid;grid-template-columns:repeat(6,auto);gap:7px;margin-top:10px;align-items:center}
.trigger-chip{border:1px solid rgba(255,255,255,.12);background:rgba(15,23,42,.72);color:var(--sub);border-radius:999px;padding:7px 10px;font-size:.68rem;font-weight:950;cursor:pointer;user-select:none}
.trigger-chip.on{color:#e8f1ff;background:rgba(56,189,248,.16);border-color:rgba(56,189,248,.55);box-shadow:0 0 0 1px rgba(56,189,248,.06)}
.trigger-chip.buy.on{background:rgba(34,197,94,.14);border-color:rgba(34,197,94,.5);color:#bbf7d0}
.trigger-chip.sell.on{background:rgba(239,68,68,.14);border-color:rgba(239,68,68,.5);color:#fecaca}
.trigger-chip.manual.on{background:rgba(167,139,250,.14);border-color:rgba(167,139,250,.5);color:#ddd6fe}
.trigger-chip.risk.on{background:rgba(245,158,11,.14);border-color:rgba(245,158,11,.5);color:#fed7aa}
.trigger-mode-card{margin-top:10px;border:1px solid rgba(255,255,255,.08);border-radius:14px;background:rgba(2,6,23,.35);padding:10px}
.trigger-lanes{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}
.trigger-lane-card{border:1px solid rgba(255,255,255,.08);border-radius:12px;background:#0b0e14;padding:9px}
.trigger-lane-card .label{font-size:.65rem;color:var(--sub);font-weight:900}.trigger-lane-card .val{font-size:.82rem;font-weight:950;margin-top:3px}.trigger-lane-card .note{font-size:.64rem;color:var(--muted);margin-top:3px;line-height:1.25}
@media(max-width:900px){.trigger-control-grid{grid-template-columns:repeat(2,1fr)}.trigger-chip{width:100%}.trigger-lanes{grid-template-columns:1fr 1fr}}
@media(max-width:560px){.trigger-lanes{grid-template-columns:1fr}}

/* v35 Trigger Map clustering / overlap handling */
.trigger-map-tools{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.trigger-toggle{border:1px solid rgba(255,255,255,.12);background:rgba(15,23,42,.65);color:var(--text);border-radius:999px;padding:7px 10px;font-size:.68rem;font-weight:900;cursor:pointer}.trigger-toggle.active{background:rgba(56,189,248,.16);border-color:rgba(56,189,248,.55);color:#bae6fd}.trigger-marker.cluster .line:before{width:18px;height:18px;top:-8px;left:-4px;background:linear-gradient(135deg,var(--blue),var(--purple));box-shadow:0 0 20px rgba(167,139,250,.55)}.trigger-marker.cluster .tag{border-radius:14px;white-space:normal;max-width:370px;line-height:1.25}.trigger-marker.cluster.buy .tag{border-color:rgba(34,197,94,.45)}.trigger-marker.cluster.sell .tag{border-color:rgba(239,68,68,.45)}.trigger-marker.cluster.warn .tag{border-color:rgba(245,158,11,.45)}.cluster-title{font-weight:1000}.cluster-items{font-size:.62rem;color:var(--sub);margin-top:3px}.cluster-count{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:20px;border-radius:999px;background:rgba(255,255,255,.10);margin-right:6px;color:#fff;font-size:.66rem}.trigger-marker.lane-left .tag{left:auto;right:22px}.trigger-marker.lane-right .tag{left:20px}.trigger-marker.lane-center .tag{left:50%;transform:translateX(-35%)}.trigger-marker.lane-left .line:before{left:auto;right:0}.trigger-nearest{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}.nearest-card{background:rgba(2,6,23,.42);border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:10px}.nearest-card .label{font-size:.68rem;color:var(--sub);font-weight:900}.nearest-card .val{font-size:.96rem;font-weight:950;margin-top:4px}.nearest-card .note{font-size:.67rem;color:var(--muted);margin-top:4px;line-height:1.25}@media(max-width:900px){.trigger-marker.cluster .tag{max-width:230px}.trigger-marker.lane-left .tag{right:16px}.trigger-nearest{grid-template-columns:1fr}}


.collapsible-controls{padding:0;overflow:hidden}
.collapsible-controls details{padding:18px 20px}
.collapsible-controls summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:10px;justify-content:space-between;flex-wrap:wrap}
.collapsible-controls summary::-webkit-details-marker{display:none}
.collapsible-controls summary:before{content:'▸';font-size:18px;color:var(--muted);transition:transform .18s ease}
.collapsible-controls details[open] summary:before{transform:rotate(90deg)}
.collapsible-controls .summary-title{font-size:18px;font-weight:800;margin-right:auto}
.collapsible-controls .summary-hint{font-size:12px;color:var(--muted);border:1px solid var(--border);border-radius:999px;padding:5px 9px;background:rgba(255,255,255,.04)}
.collapsible-controls details:not([open]){padding-bottom:18px}
.collapsible-controls details[open] .control-grid{margin-top:14px}
@media(max-width:720px){.collapsible-controls details{padding:14px}.collapsible-controls summary{gap:8px}.collapsible-controls .summary-title{font-size:16px}.collapsible-controls .summary-hint{width:100%;text-align:center}}

.score-reconcile-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:10px}.score-reconcile-card{background:#0b0e14;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:12px}.score-reconcile-card .title{font-size:.72rem;color:var(--sub);font-weight:950;text-transform:uppercase;letter-spacing:.04em}.score-reconcile-card .value{font-size:1.1rem;font-weight:1000;margin-top:4px}.score-reconcile-card .note{font-size:.72rem;color:var(--muted);line-height:1.35;margin-top:5px}.opportunity-banner{margin-top:12px;border-radius:14px;padding:12px;border:1px solid rgba(245,158,11,.35);background:rgba(245,158,11,.10);color:#fed7aa;font-size:.82rem;line-height:1.35}.opportunity-banner.ok{border-color:rgba(34,197,94,.4);background:rgba(34,197,94,.12);color:#bbf7d0}.opportunity-banner.blocked{border-color:rgba(239,68,68,.38);background:rgba(239,68,68,.10);color:#fecaca}.opportunity-banner.low{border-color:rgba(148,163,184,.35);background:rgba(148,163,184,.10);color:#e2e8f0}.opportunity-banner.medium{border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.10);color:#fed7aa}.opportunity-banner.high{border-color:rgba(59,130,246,.45);background:rgba(59,130,246,.12);color:#bfdbfe}.opportunity-banner.extreme{border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.14);color:#bbf7d0}.opp-meter{display:flex;gap:6px;margin-top:8px}.opp-dot{height:9px;flex:1;border-radius:999px;background:rgba(148,163,184,.25)}.opp-dot.on.low{background:rgba(148,163,184,.75)}.opp-dot.on.medium{background:rgba(245,158,11,.9)}.opp-dot.on.high{background:rgba(59,130,246,.9)}.opp-dot.on.extreme{background:rgba(34,197,94,.9)}.checkline{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}.checkpill{border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.04);border-radius:999px;padding:3px 8px;font-size:.72rem;color:var(--muted)}.checkpill.yes{color:#bbf7d0;border-color:rgba(34,197,94,.35);background:rgba(34,197,94,.10)}.checkpill.no{color:#fecaca;border-color:rgba(239,68,68,.30);background:rgba(239,68,68,.08)}@media(max-width:900px){.score-reconcile-grid{grid-template-columns:1fr 1fr}}@media(max-width:560px){.score-reconcile-grid{grid-template-columns:1fr}}


/* v60 mobile-first cleanup: keep iPhone view readable and prevent stale/empty panels from dominating. */
@media(max-width:760px){
  .app{padding:10px!important;max-width:100%!important;overflow-x:hidden!important;}
  header{display:block!important;padding:12px!important;border-radius:16px!important;}
  header h1{font-size:1.28rem!important;line-height:1.15!important;margin-bottom:8px!important;}
  header .actions{display:grid!important;grid-template-columns:1fr 1fr!important;gap:8px!important;margin-top:10px!important;}
  header .actions .btn{width:100%!important;text-align:center!important;min-height:40px!important;}
  .grid{display:block!important;}
  .card{margin-bottom:12px!important;padding:12px!important;border-radius:16px!important;overflow:hidden!important;}
  .card h2{font-size:.92rem!important;line-height:1.25!important;margin-bottom:10px!important;}
  .method-badge{display:inline-block!important;margin-top:5px!important;float:none!important;white-space:normal!important;}
  .metric-row,.macro-grid,.v12-grid,.intel-grid,.score-reconcile-grid{grid-template-columns:1fr!important;gap:8px!important;}
  .metric{padding:10px!important;border-radius:12px!important;min-width:0!important;}
  .metric .val,.v12-val,.intel-val,.score-reconcile-card .value{font-size:1.05rem!important;line-height:1.2!important;word-break:break-word!important;}
  .score-reconcile-card,.v12-card,.intel-card{padding:10px!important;border-radius:12px!important;}
  .opportunity-banner{font-size:.82rem!important;line-height:1.35!important;padding:10px!important;border-radius:12px!important;}
  .monitor-pips{grid-template-columns:1fr!important;}
  .monitor-pip{font-size:.78rem!important;line-height:1.25!important;}
  .trigger-map-wrap{grid-template-columns:1fr!important;}
  .trigger-control-grid{grid-template-columns:1fr!important;}
  .trigger-lanes{grid-template-columns:1fr!important;}
  .trigger-toggle,.trigger-chip{width:100%!important;text-align:center!important;}
  .control-grid{grid-template-columns:1fr!important;}
  .control-grid input,.control-grid select,.control-grid button{width:100%!important;min-height:42px!important;}
  .table-wrap{overflow-x:auto!important;-webkit-overflow-scrolling:touch!important;}
  .table{min-width:640px!important;font-size:.78rem!important;}
  .kv{display:grid!important;grid-template-columns:1fr!important;gap:4px!important;align-items:start!important;}
  .kv .v{text-align:left!important;word-break:break-word!important;}
  .pnl-note,.mini,.sub{font-size:.76rem!important;line-height:1.35!important;}
  .diagnostic-card{display:none!important;}
}
@media(max-width:390px){
  header .actions{grid-template-columns:1fr!important;}
  .card{padding:10px!important;}
  .metric .val{font-size:1rem!important;}
  .btn{padding:9px 10px!important;}
}



/* v63 Trade Map & Performance overlay */
.trade-map-controls{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0 12px}.tm-btn{border:1px solid rgba(255,255,255,.12);background:rgba(15,23,42,.72);color:var(--sub);border-radius:999px;padding:7px 11px;font-size:.72rem;font-weight:950;cursor:pointer}.tm-btn.on{background:rgba(56,189,248,.16);border-color:rgba(56,189,248,.55);color:#bae6fd}.trade-map-wrap{display:grid;grid-template-columns:1fr;gap:10px}.trade-map-canvas-box{position:relative;background:#0b0e14;border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:10px;overflow:hidden}.trade-map-canvas{display:block;width:100%;height:330px}.trade-map-pnl{height:170px}.trade-map-legend{display:flex;flex-wrap:wrap;gap:8px;margin-top:9px}.tm-legend-pill{border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.04);border-radius:999px;padding:5px 8px;font-size:.68rem;font-weight:850;color:var(--sub)}.tm-tip{position:absolute;display:none;z-index:20;max-width:260px;background:rgba(2,6,23,.96);border:1px solid rgba(255,255,255,.15);border-radius:12px;padding:9px;color:var(--text);font-size:.72rem;line-height:1.35;box-shadow:0 12px 40px rgba(0,0,0,.35);pointer-events:none}.tm-note{font-size:.72rem;color:var(--muted);line-height:1.35;margin-top:8px}.tm-empty{padding:20px;color:var(--muted);text-align:center}.tm-marker-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}.tm-stat{background:#0b0e14;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:9px}.tm-stat .label{font-size:.66rem;color:var(--sub);font-weight:900}.tm-stat .val{font-size:.95rem;font-weight:950;margin-top:3px}@media(max-width:760px){.trade-map-canvas{height:280px}.trade-map-pnl{height:135px}.tm-marker-summary{grid-template-columns:1fr 1fr}.trade-map-controls{display:grid;grid-template-columns:repeat(3,1fr)}.tm-btn{width:100%;padding:9px 6px}}
.advanced-diagnostics{grid-column:span 12;border:1px solid rgba(255,255,255,.09);border-radius:18px;background:rgba(15,23,42,.42);overflow:hidden}.advanced-diagnostics>summary{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:15px 18px;cursor:pointer;font-weight:900;list-style:none}.advanced-diagnostics>summary::-webkit-details-marker{display:none}.advanced-diagnostics>summary::before{content:"▸";color:#38bdf8;transition:transform .18s ease}.advanced-diagnostics[open]>summary::before{transform:rotate(90deg)}.advanced-diagnostics .advanced-grid{padding:0 10px 10px}.refresh-stamp{margin-top:2px;font-size:.65rem;color:var(--muted);font-variant-numeric:tabular-nums}


/* v75 command-center polish layer */
::selection{background:rgba(247,147,26,.35)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#242c39;border-radius:8px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#2f3a49}
:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
button,input,select{font-family:inherit}
#botHealth{display:inline-flex;align-items:center}
#botHealth::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor;animation:livedot 2.2s ease-in-out infinite;flex:none}
@keyframes livedot{0%,100%{opacity:1}50%{opacity:.3}}
.v12-val,.intel-val,.score-reconcile-card .value,.trigger-price,.tm-stat .val,.nearest-card .val,.trigger-tile .val,.trigger-lane-card .val{font-variant-numeric:tabular-nums}
.v12-title,.intel-title,.tm-stat .label,.trigger-tile .label,.trigger-lane-card .label,.nearest-card .label,.score-reconcile-card .title{text-transform:uppercase;letter-spacing:.07em;font-size:.62rem;font-weight:700}
.mindset-shell{border:1px solid rgba(56,189,248,.28);background:linear-gradient(135deg,rgba(8,47,73,.34),rgba(11,14,20,.88) 48%,rgba(30,41,59,.68));border-radius:18px;padding:14px;overflow:hidden}.mindset-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}.mindset-kicker{font-size:.64rem;color:var(--blue);font-weight:900;text-transform:uppercase;letter-spacing:.09em}.mindset-decision{font-size:1.18rem;font-weight:1000;margin-top:3px;line-height:1.15}.mindset-reason{font-size:.72rem;color:var(--sub);margin-top:5px;line-height:1.35;max-width:760px}.mindset-badge{flex:0 0 auto;padding:7px 10px;border-radius:999px;border:1px solid rgba(148,163,184,.28);background:rgba(148,163,184,.10);font-size:.68rem;font-weight:950;text-transform:uppercase;letter-spacing:.06em}.mindset-badge.good{color:#bbf7d0;border-color:rgba(34,197,94,.42);background:rgba(34,197,94,.12)}.mindset-badge.warn{color:#fed7aa;border-color:rgba(245,158,11,.42);background:rgba(245,158,11,.12)}.mindset-badge.bad{color:#fecaca;border-color:rgba(239,68,68,.42);background:rgba(239,68,68,.12)}.mindset-flow{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.mindset-step{position:relative;min-width:0;border:1px solid rgba(255,255,255,.08);background:rgba(2,6,23,.48);border-radius:14px;padding:10px}.mindset-step:not(:last-child):after{content:'›';position:absolute;right:-8px;top:50%;transform:translate(50%,-50%);z-index:2;color:rgba(56,189,248,.8);font-size:1.15rem;font-weight:1000}.mindset-step.active{border-color:rgba(56,189,248,.62);background:rgba(56,189,248,.12);box-shadow:0 0 18px rgba(56,189,248,.10)}.mindset-step.done{border-color:rgba(34,197,94,.32);background:rgba(34,197,94,.07)}.mindset-label{font-size:.59rem;text-transform:uppercase;letter-spacing:.075em;color:var(--muted);font-weight:850}.mindset-value{font-size:.91rem;font-weight:950;margin-top:4px;overflow-wrap:anywhere}.mindset-note{font-size:.65rem;color:var(--sub);margin-top:4px;line-height:1.28;overflow-wrap:anywhere}.mindset-profit{margin-top:10px;border-top:1px solid rgba(255,255,255,.07);padding-top:10px}.mindset-profit-head{display:flex;justify-content:space-between;gap:10px;align-items:end}.mindset-profit-title{font-size:.65rem;color:var(--sub);font-weight:850;text-transform:uppercase;letter-spacing:.06em}.mindset-profit-value{font-size:.78rem;font-weight:950}.mindset-track{height:10px;border-radius:999px;background:rgba(148,163,184,.18);margin-top:7px;position:relative;overflow:visible}.mindset-fill{height:100%;width:0;border-radius:999px;background:linear-gradient(90deg,var(--green),var(--blue));transition:width .35s ease}.mindset-marker{position:absolute;top:-4px;width:2px;height:18px;background:var(--orange);box-shadow:0 0 8px rgba(245,158,11,.6)}.mindset-profit-foot{display:flex;justify-content:space-between;gap:8px;margin-top:6px;font-size:.61rem;color:var(--muted)}
@media(max-width:760px){.mindset-shell{padding:11px}.mindset-head{display:block}.mindset-badge{display:inline-flex;margin-top:9px}.mindset-decision{font-size:1.04rem}.mindset-flow{grid-template-columns:1fr;gap:7px}.mindset-step{display:grid;grid-template-columns:82px minmax(0,1fr);column-gap:8px;align-items:start;padding:9px}.mindset-step:not(:last-child):after{content:'↓';right:auto;left:39px;top:auto;bottom:-12px;transform:none;font-size:.85rem}.mindset-value{margin-top:0;font-size:.85rem}.mindset-note{grid-column:2;margin-top:2px}.mindset-profit-head{align-items:flex-start}.mindset-profit-foot{font-size:.58rem}}
.defense-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:10px}.defense-cell{border:1px solid rgba(148,163,184,.18);border-radius:12px;padding:9px;background:rgba(2,6,23,.42)}.defense-cell .k{font-size:.58rem;color:var(--muted);font-weight:850;text-transform:uppercase;letter-spacing:.065em}.defense-cell .v{font-size:.88rem;font-weight:950;margin-top:4px}.defense-cell .n{font-size:.61rem;color:var(--sub);line-height:1.3;margin-top:3px}.defense-score{height:6px;background:rgba(148,163,184,.18);border-radius:999px;margin-top:6px;overflow:hidden}.defense-score span{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--green),var(--orange),var(--red));transition:width .3s ease}@media(max-width:760px){.defense-grid{grid-template-columns:1fr 1fr}.defense-cell{padding:8px}}@media(max-width:430px){.defense-grid{grid-template-columns:1fr}}
.management-alert{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:12px;padding:12px 14px;border-radius:14px;border:1px solid rgba(148,163,184,.22);background:rgba(15,23,42,.72)}.management-alert .ma-title{font-size:.82rem;font-weight:1000;letter-spacing:.02em}.management-alert .ma-note{font-size:.72rem;color:var(--sub);margin-top:3px;line-height:1.35}.management-alert .ma-badge{flex:0 0 auto;border-radius:999px;padding:7px 10px;font-size:.66rem;font-weight:1000;letter-spacing:.06em}.management-alert.managed{border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.09)}.management-alert.managed .ma-badge{color:#bbf7d0;background:rgba(34,197,94,.15)}.management-alert.unmanaged{border-color:rgba(239,68,68,.62);background:rgba(127,29,29,.22);box-shadow:0 0 24px rgba(239,68,68,.08)}.management-alert.unmanaged .ma-title{color:#fecaca}.management-alert.unmanaged .ma-badge{color:#fff;background:#b91c1c}.management-alert.flat .ma-badge{color:#cbd5e1;background:rgba(148,163,184,.14)}@media(max-width:760px){.management-alert{align-items:flex-start}.management-alert .ma-note{font-size:.76rem}.management-alert .ma-badge{white-space:nowrap}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style></head><body><div class="app">
<header class="hero"><div class="hero-row"><div><div class="brand">₿ Larry BTC Perp Command Center</div><div class="sub" id="productLine">Spot + Perp · Coinbase-only</div></div><div><div class="price" id="btcPrice">—</div><div class="sub" id="serverTime">—</div><div class="refresh-stamp" id="dashboardRefresh">Dashboard refreshed —</div></div></div><div class="pill-row"><span class="pill blue" id="botHealth">BOT —</span><span class="pill blue" id="botState">STATE —</span><span class="pill blue" id="macroPill">MACRO —</span><span class="pill purple" id="sessionPill">SESSION —</span><button class="pill" style="cursor:pointer;border:1px solid rgba(247,147,26,.5);background:rgba(247,147,26,.12);color:#ffbf69;font-weight:700" onclick="sendSnapshot(event)">📸 Snapshot</button><button class="pill" style="cursor:pointer;border:none" onclick="signOut()">Sign out</button></div></header>
<div class="errbox" id="errorBox"></div><div class="warnbox" id="warnBox"></div>
<main class="grid">
<section class="card span12 authority-card"><h2>Position Authority <span class="method-badge">Coinbase truth · execution permission</span></h2><div class="management-alert flat" id="managementAlert"><div><div class="ma-title" id="managementTitle">Checking position authority…</div><div class="ma-note" id="managementNote">Larry is reconciling the live Coinbase position with persisted bot ownership.</div></div><div class="ma-badge" id="managementBadge">CHECKING</div></div></section>
<section class="card span12"><h2>Larry Decision Pipeline <span class="method-badge">live mindset · triggers · conviction · profit protection</span></h2><div class="mindset-shell"><div class="mindset-head"><div><div class="mindset-kicker">Current decision</div><div class="mindset-decision" id="mindsetDecision">Loading Larry's current state...</div><div class="mindset-reason" id="mindsetReason">Reading the current engine cycle, live position, and exit plan.</div></div><div class="mindset-badge" id="mindsetBadge">WAITING</div></div><div class="mindset-flow"><div class="mindset-step" id="mindsetRegimeStep"><div class="mindset-label">Market regime</div><div class="mindset-value" id="mindsetRegime">—</div><div class="mindset-note" id="mindsetRegimeNote">Macro and funding gates</div></div><div class="mindset-step" id="mindsetTriggerStep"><div class="mindset-label">Trigger score</div><div class="mindset-value" id="mindsetTrigger">—</div><div class="mindset-note" id="mindsetTriggerNote">Arm and commit progress</div></div><div class="mindset-step" id="mindsetConvictionStep"><div class="mindset-label">Conviction</div><div class="mindset-value" id="mindsetConviction">—</div><div class="mindset-note" id="mindsetConvictionNote">Target size from confidence</div></div><div class="mindset-step" id="mindsetPositionStep"><div class="mindset-label">Position plan</div><div class="mindset-value" id="mindsetPosition">—</div><div class="mindset-note" id="mindsetPositionNote">Live exchange position</div></div><div class="mindset-step" id="mindsetExitStep"><div class="mindset-label">Exit plan</div><div class="mindset-value" id="mindsetExit">—</div><div class="mindset-note" id="mindsetExitNote">TP, trailing stop, and ATR protection</div></div></div><div class="mindset-profit"><div class="mindset-profit-head"><div class="mindset-profit-title">Profit-protection progress</div><div class="mindset-profit-value" id="mindsetProfitValue">Flat / waiting</div></div><div class="mindset-track"><div class="mindset-fill" id="mindsetProfitFill"></div><div class="mindset-marker" id="mindsetTpMarker" style="left:50%"></div></div><div class="mindset-profit-foot"><span>Entry</span><span id="mindsetTpLabel">TP threshold</span><span id="mindsetTrailLabel">Trail activation</span></div></div><div class="defense-grid"><div class="defense-cell"><div class="k">Adaptive defence</div><div class="v" id="defenseState">Flat</div><div class="defense-score"><span id="defenseBar"></span></div><div class="n" id="defenseEvidence">Waiting for a position</div></div><div class="defense-cell"><div class="k">Position anchor</div><div class="v" id="positionVersion">Version —</div><div class="n" id="positionAnchor">Exchange average not active</div></div><div class="defense-cell"><div class="k">Market structure</div><div class="v" id="pivotStructure">Unclassified</div><div class="n" id="pivotLevels">Confirmed pivots · shadow</div></div><div class="defense-cell"><div class="k">Post-stop read</div><div class="v" id="stopBlownState">Inactive</div><div class="n" id="stopBlownScores">SB1–SB5 shadow observer</div></div></div></div></section>
<section class="card span12"><h2>Mission Control <span class="method-badge">performance-first · ledger P&L · live risk</span></h2><div class="metric-row"><div class="metric"><div class="label">Starting Capital / Baseline</div><div class="val" id="startingCapital">—</div><div class="mini" id="baselineSource">Baseline source —</div></div><div class="metric"><div class="label">Larry Equity</div><div class="val" id="currentCapital">—</div><div class="mini" id="currentCapitalMini">Baseline + Larry ledger P&L</div></div><div class="metric"><div class="label">Larry Net P&L</div><div class="val" id="netPnl">—</div><div class="mini">Ledger realized + live bot UPL</div></div><div class="metric"><div class="label">Larry Return on Capital</div><div class="val" id="netReturn">—</div><div class="mini">Larry P&L / baseline</div></div></div><div class="pnl-note" id="pnlMethodNote">Every figure on this dashboard measures Larry's strategy only — its own ledger, net of Larry's fees. Manual trading is tracked in a single tile below and never mixed into Larry's numbers.</div></section>
<section class="card span12"><h2>Performance vs BTC Buy & Hold <span class="method-badge">same starting capital · live BTC benchmark</span></h2><div class="metric-row"><div class="metric"><div class="label">Larry Equity</div><div class="val" id="paLarryEquity">—</div><div class="mini" id="paLarryNote">Starting capital + Larry P&L</div></div><div class="metric"><div class="label">BTC Benchmark Value</div><div class="val" id="paBtcValue">—</div><div class="mini" id="paBtcNote">Synthetic BTC bought at inception</div></div><div class="metric"><div class="label">BTC Benchmark Return</div><div class="val" id="paBtcReturn">—</div><div class="mini" id="paBtcPriceNote">—</div></div><div class="metric"><div class="label">Larry Alpha</div><div class="val" id="paAlpha">—</div><div class="mini" id="paAlphaNote">Larry return minus BTC return</div></div></div><div class="metric-row"><div class="metric"><div class="label">Trades</div><div class="val" id="paTrades">—</div><div class="mini" id="paWinRate">Win rate —</div></div><div class="metric"><div class="label">Profit Factor</div><div class="val" id="paProfitFactor">—</div><div class="mini" id="paExpectancy">Expectancy —</div></div><div class="metric"><div class="label">Avg Winner / Loser</div><div class="val" id="paAvgWinLoss">—</div><div class="mini">Realized net trade P&L</div></div><div class="metric"><div class="label">Market Exposure</div><div class="val" id="paExposure">—</div><div class="mini" id="paExposureNote">Long / Short / Flat since start</div></div></div><div class="pnl-visual"><div class="pnl-bar-title">Larry vs BTC Return</div><div class="pnl-bars" id="paReturnBars"><div class="mini">Loading benchmark…</div></div></div><div class="pnl-note" id="paMethodNote">Benchmark uses the same Starting Capital / Baseline. BTC start price uses capital_state if available; otherwise the first successful Larry trade price is used as a transparent proxy.</div></section>

<section class="card span12"><h2>Trade Map & Performance <span class="method-badge">BTC price · Larry trades · trade P&L bars</span></h2><div class="trade-map-controls" id="tradeMapControls"><button class="tm-btn on" data-range="1M">1M</button><button class="tm-btn" data-range="1D">1D</button><button class="tm-btn" data-range="1W">1W</button><button class="tm-btn" data-range="YTD">YTD</button><button class="tm-btn" data-range="12M">12M</button><button class="tm-btn" data-range="ALL">ALL</button></div><div class="tm-marker-summary"><div class="tm-stat"><div class="label">Markers (range view)</div><div class="val" id="tmTradeCount">—</div></div><div class="tm-stat"><div class="label">Larry Return (range)</div><div class="val" id="tmVisiblePnl">—</div></div><div class="tm-stat"><div class="label">BTC move</div><div class="val" id="tmBtcMove">—</div></div><div class="tm-stat"><div class="label">Range</div><div class="val" id="tmRangeLabel">1M</div></div></div><div class="trade-map-wrap"><div class="trade-map-canvas-box"><canvas id="tradeMapPriceCanvas" class="trade-map-canvas"></canvas><div class="tm-tip" id="tradeMapTip"></div></div><div class="trade-map-canvas-box"><canvas id="tradeMapPnlCanvas" class="trade-map-canvas trade-map-pnl"></canvas><div class="tm-tip" id="tradeMapPnlTip"></div></div></div><div class="trade-map-legend"><span class="tm-legend-pill">🟢 BUY / ADD</span><span class="tm-legend-pill">🟡 TP / partial sell</span><span class="tm-legend-pill">🔴 STOP / flatten</span><span class="tm-legend-pill">⚪ Other / failed</span><span class="tm-legend-pill">Line 1: BTC price</span><span class="tm-legend-pill">Chart 2: Trade net P&L bars</span></div><div class="tm-note" id="tradeMapNote">Trade markers come from Larry’s ledger. BTC price uses Coinbase candles. The lower chart shows each realized trade as a green gain or red loss.</div></section>
<section class="card span12"><h2>Larry Equity Curve &amp; Drawdown <span class="method-badge">ledger-reconstructed · risk-adjusted</span></h2><div class="metric-row"><div class="metric"><div class="label">Max Drawdown</div><div class="val" id="eqMaxDD">—</div><div class="mini" id="eqMaxDDNote">Peak-to-trough on Larry equity</div></div><div class="metric"><div class="label">Sharpe (annualized)</div><div class="val" id="eqSharpe">—</div><div class="mini" id="eqSharpeNote">From daily returns</div></div><div class="metric"><div class="label">Peak Equity</div><div class="val" id="eqPeak">—</div><div class="mini">Highest Larry equity reached</div></div><div class="metric"><div class="label">Current Equity</div><div class="val" id="eqCurrent">—</div><div class="mini">Baseline + Larry realized P&L</div></div></div><div class="trade-map-canvas-box" style="margin-top:12px"><canvas id="equityCurveCanvas" class="trade-map-canvas"></canvas><div class="tm-tip" id="equityCurveTip"></div></div><div class="pnl-note" id="eqNote">Reconstructed from Larry’s realized-trade ledger: equity = baseline + cumulative net realized P&L. Drawdown and Sharpe are computed from this curve and update from day one. Intra-trade mark-to-market smoothing will follow once periodic equity snapshots accumulate.</div></section>
<section class="card span12"><h2>Larry Trade P&L Tape <span class="method-badge">ledger source of truth · net trade impact</span></h2><div class="metric-row"><div class="metric"><div class="label">Larry Realized P&L</div><div class="val" id="larryRealizedPnl">—</div><div class="mini">Sum of net realized P&L from Larry ledger</div></div><div class="metric"><div class="label">Open Bot Unrealized</div><div class="val" id="larryOpenUnrealized">—</div><div class="mini" id="larryOpenNote">Live Coinbase open bot position</div></div><div class="metric"><div class="label">Larry Total P&L</div><div class="val" id="larryTotalPnl">—</div><div class="mini">Net realized + open unrealized</div></div><div class="metric"><div class="label">Last Realized Trade</div><div class="val" id="larryLastTradePnl">—</div><div class="mini" id="larryLastTradeNote">—</div></div></div><div class="pnl-visual"><div class="pnl-bar-title">Larry P&L Composition</div><div class="pnl-bars" id="larryPnlBars"><div class="mini">Loading P&L bars…</div></div></div><div class="table-wrap"><table class="table"><thead><tr><th>Time</th><th>Intent</th><th>Reason</th><th>Action</th><th>Contracts</th><th>Fill</th><th>Gross</th><th>Fees</th><th>Net</th><th>Slip</th><th>M/T</th></tr></thead><tbody id="larryTradeTapeBody"><tr><td colspan="11" class="muted">Loading Larry ledger…</td></tr></tbody></table></div><div class="pnl-note" id="larryAccountingNote">Larry P&L comes from Larry’s own trade ledger: realized net trade impacts plus live open bot UPL, net of Larry’s fees. Manual trading is excluded and shown only in the Manual Trading Impact tile.</div><div class="pnl-note" id="larryRiskGateNote">Risk gate status loading…</div></section>
<section class="card span12 macro-card"><h2>Macro Regime / Trading Gate <span class="method-badge">50h / 200h SMA regime filter</span></h2><div class="macro-top"><div><div id="tradeStatus" class="status-banner unknown"><span>Trading Gate</span><strong>—</strong></div><div class="macro-state" id="macroRegime">—</div><div class="sub" id="macroReason">—</div></div><div><div class="macro-grid"><div class="metric"><div class="label">BTC Price</div><div class="val" id="macroPrice">—</div></div><div class="metric"><div class="label">Fast SMA <span id="macroFastPeriod"></span></div><div class="val" id="macroFast">—</div></div><div class="metric"><div class="label">Slow SMA <span id="macroSlowPeriod"></span></div><div class="val" id="macroSlow">—</div></div><div class="metric"><div class="label">Gate</div><div class="val" id="macroGate">—</div></div><div class="metric"><div class="label">Price &gt; Fast</div><div class="val check" id="macroAboveFast">—</div></div><div class="metric"><div class="label">Price &gt; Slow</div><div class="val check" id="macroAboveSlow">—</div></div><div class="metric"><div class="label">Fast &gt; Slow</div><div class="val check" id="macroFastAboveSlow">—</div></div><div class="metric"><div class="label">Fast vs Slow</div><div class="val" id="macroDistSpread">—</div></div></div><div class="metric-row"><div class="metric"><div class="label">Price vs Fast SMA</div><div class="val" id="macroDistFast">—</div></div><div class="metric"><div class="label">Price vs Slow SMA</div><div class="val" id="macroDistSlow">—</div></div><div class="metric"><div class="label">ATR Stop</div><div class="val" id="atrStop">—</div></div><div class="metric"><div class="label">TSL Activation / Trail</div><div class="val"><span id="tslAct">—</span> / <span id="tslTrail">—</span></div></div></div><div class="pnl-note">Config source: <span id="configSource">—</span></div></div></div></section>
<section class="card span12"><h2>Live Perp Position Risk Card <span class="method-badge">Gemini-style risk view · live Coinbase position</span></h2><div id="perpRiskCard"><div class="muted">Loading position risk…</div></div></section>
<section class="card span12"><h2>Operator Kill Switch <span class="method-badge">v12 safety control · no order placement when halted</span></h2><div class="halt-panel"><div id="haltStatusBox" class="halt-status off">Kill switch status loading…</div><div><div class="kv"><span class="k">Reason</span><span class="v" id="haltReason">—</span></div><div class="kv"><span class="k">Set by / at</span><span class="v" id="haltSetBy">—</span></div></div><div class="halt-actions"><button class="btn btn-danger" onclick="setKillSwitch(true)">HALT BOT</button><button class="btn" onclick="setKillSwitch(false)">Resume</button><button class="btn btn-danger" onclick="emergencyFlatten()">EMERGENCY CLOSE FUTURES</button></div></div><div class="pnl-note">HALT means Larry skips new order placement. EMERGENCY CLOSE first halts Larry, then sends market orders to flatten ALL live Coinbase futures exposure -- including manually-entered positions, not just bot-managed ones. Confirm Coinbase UI after use.</div></section>
<section class="card span12"><h2>BTC Perp Strategy Transparency <span class="method-badge">TP1 · ATR lock · confidence sizing · signal lock / reversal probe · why-no-trade diagnostics · progressive add-ons</span></h2><div id="v12ManualBanner" class="v12-banner">Loading strategy controls…</div><div class="v12-grid"><div class="v12-card"><div class="v12-title">TP1 Partial Take Profit</div><div class="v12-val" id="v12Tp1Status">—</div><div class="v12-note" id="v12Tp1Note">—</div></div><div class="v12-card"><div class="v12-title">ATR Locked at Entry</div><div class="v12-val" id="v12AtrLock">—</div><div class="v12-note" id="v12AtrNote">—</div></div><div class="v12-card"><div class="v12-title">Confidence Target Sizing</div><div class="v12-val" id="v12Sizing">—</div><div class="v12-note" id="v12SizingNote">—</div></div><div class="v12-card"><div class="v12-title">Funding Size Modifier</div><div class="v12-val" id="v12Funding">—</div><div class="v12-note" id="v12FundingNote">—</div></div><div class="v12-card"><div class="v12-title">Per-Direction Cooldowns</div><div class="v12-val" id="v12Cooldowns">—</div><div class="v12-note" id="v12CooldownNote">—</div></div><div class="v12-card"><div class="v12-title">Manual Position Mode</div><div class="v12-val" id="v12ManualMode">—</div><div class="v12-note" id="v12ManualNote">—</div></div><div class="v12-card"><div class="v12-title">Phantom Confirmation</div><div class="v12-val" id="v12Phantom">—</div><div class="v12-note" id="v12PhantomNote">—</div></div><div class="v12-card"><div class="v12-title">Signal P&L / Slippage</div><div class="v12-val" id="v12SignalPnl">—</div><div class="v12-note" id="v12SignalPnlNote">—</div><div class="v12-actions"><button class="btn btn-small" onclick="loadSignalPnlRollup()">Load Rollup</button><a class="btn btn-small" href="/api/signal_pnl_rollup" target="_blank">Open API</a></div></div></div><div id="signalPnlRollupBox" class="rollup-table mini"></div></section>
<section class="card span12"><h2>Entry Lifecycle / Signal Lock / Reversal Probe <span class="method-badge">v15 finite-state entry machine</span></h2><div class="v12-grid"><div class="v12-card"><div class="v12-title">Lifecycle State</div><div class="v12-val" id="lifeState">—</div><div class="v12-note" id="lifeStateNote">—</div></div><div class="v12-card"><div class="v12-title">Locked Setup</div><div class="v12-val" id="lifeLockedSetup">—</div><div class="v12-note" id="lifeLockedNote">—</div></div><div class="v12-card"><div class="v12-title">Validity Window</div><div class="v12-val" id="lifeValidity">—</div><div class="v12-note" id="lifeValidityNote">—</div></div><div class="v12-card"><div class="v12-title">Hysteresis / Cancel</div><div class="v12-val" id="lifeHysteresis">—</div><div class="v12-note" id="lifeHysteresisNote">—</div></div><div class="v12-card"><div class="v12-title">Commitment Rule</div><div class="v12-val" id="lifeCommitRule">—</div><div class="v12-note" id="lifeCommitNote">—</div></div><div class="v12-card"><div class="v12-title">Why Waiting?</div><div class="v12-val" id="lifeWhyWaiting">—</div><div class="v12-note" id="lifeWhyNote">—</div></div></div><div class="pnl-note">Lifecycle flow: MONITORING → PHANTOM_ARMED → EXTENSION_CONFIRMED → COMMITTED_ENTRY → EXECUTED. Once armed, Larry freezes the setup for the configured validity window so minor score wobble does not create a moving target.</div></section>

<section class="card span12"><h2>BTC Trigger Map <span class="method-badge">live price ladder · Spot / Perp / Risk levels</span></h2><div id="btcTriggerMap"><div class="muted">Loading BTC trigger levels…</div></div></section>
<section class="card span12"><h2>Attribution / Risk Intelligence <span class="method-badge">manual book · slippage · drawdown · opportunity cost · health</span></h2><div class="intel-grid"><div class="intel-card goodborder"><div class="intel-title">Larry Bot Strategy P&L</div><div class="intel-val" id="intelLarryPnl">—</div><div class="intel-note" id="intelLarryNote">Excludes manual monitor-only exposure.</div></div><div class="intel-card manual"><div class="intel-title">Manual Trading Impact</div><div class="intel-val" id="intelManualBook">—</div><div class="intel-note" id="intelManualNote">The only place manual trading is tracked. Excluded from Larry.</div></div><div class="intel-card"><div class="intel-title">Risk Exposure Heatmap</div><div class="intel-val" id="intelExposure">—</div><div class="intel-note" id="intelExposureNote">Spot + Perp + net BTC delta.</div></div><div class="intel-card"><div class="intel-title">Execution Quality</div><div class="intel-val" id="intelSlippage">—</div><div class="intel-note" id="intelSlippageNote">Signed bps; positive = worse than mark.</div></div><div class="intel-card"><div class="intel-title">Opportunity Cost</div><div class="intel-val" id="intelOppCost">—</div><div class="intel-note" id="intelOppNote">Shows current blocked/skipped reasons.</div></div><div class="intel-card"><div class="intel-title">Drawdown Monitor</div><div class="intel-val" id="intelDrawdown">—</div><div class="intel-note" id="intelDrawdownNote">Current strategy and account drift.</div></div><div class="intel-card"><div class="intel-title">Funding Impact</div><div class="intel-val" id="intelFundingCost">—</div><div class="intel-note" id="intelFundingNote">Funding P&L and current funding gate.</div></div><div class="intel-card"><div class="intel-title">System Health</div><div class="intel-val" id="intelHealth">—</div><div class="intel-note" id="intelHealthNote">Heartbeat, state age, kill switch.</div></div></div></section>
<section class="card span12" id="spotControlCard"><h3>Perp Focus / Spot Trading</h3><div class="cmd-row"><span class="pill" id="spotTradingPill">SPOT —</span><span class="pill" id="spotBridgePill">BRIDGE —</span></div><div class="pnl-note">Use this while debugging Larry Perp. Disabling Spot also disables the Spot→Perp bridge so only the Perp engine can create exposure.</div><div class="btn-row"><button class="btn danger" onclick="toggleSpot(false)">Disable Spot BTC</button><button class="btn" onclick="toggleSpot(true)">Enable Spot BTC</button></div><div class="pnl-note" id="spotToggleStatus">Spot toggle reads/writes strategy_config.json.</div></section>

<section class="card span12"><h2>Why Larry Is Not Trading <span class="method-badge">entry diagnostics · BTC perp only</span></h2>
  <div class="v12-grid">
    <div class="v12-card"><div class="v12-title">Final Tradable Scores</div><div class="v12-val" id="diagScores">—</div><div class="v12-note" id="diagScoreNote">Engine score after Larry filters</div></div>
    <div class="v12-card"><div class="v12-title">Opportunity Meter</div><div class="v12-val" id="oppLevel">—</div><div class="opp-meter" id="oppMeter"><div class="opp-dot"></div><div class="opp-dot"></div><div class="opp-dot"></div><div class="opp-dot"></div></div><div class="v12-note" id="oppNote">Raw setup quality before live order checks.</div></div>
    <div class="v12-card"><div class="v12-title">LONG Setup Checklist</div><div class="v12-val" id="diagLongStatus">—</div><div class="v12-note" id="diagLongMissing">—</div></div>
    <div class="v12-card"><div class="v12-title">SHORT Setup Checklist</div><div class="v12-val" id="diagShortStatus">—</div><div class="v12-note" id="diagShortMissing">—</div></div>
    <div class="v12-card"><div class="v12-title">Reversal Probe</div><div class="v12-val" id="diagProbeStatus">—</div><div class="v12-note" id="diagProbeReason">—</div></div>
    <div class="v12-card"><div class="v12-title">Macro / Funding</div><div class="v12-val" id="diagGates">—</div><div class="v12-note" id="diagGatesNote">—</div></div>
    <div class="v12-card"><div class="v12-title">Next Possible Action</div><div class="v12-val" id="diagNextAction">—</div><div class="v12-note" id="diagNextNote">—</div></div>
  </div>
  <div class="pnl-note" id="diagExplanation">Waiting for bot cycle diagnostics…</div>
</section>

<details class="advanced-diagnostics"><summary><span>Advanced Diagnostics &amp; Controls</span><span class="method-badge">click to expand · detailed balances, signals, execution &amp; accounting</span></summary><div class="grid advanced-grid">
<section class="card span12 collapsible-controls"><details id="strategyControlsDetails"><summary><span class="summary-title">Strategy Controls</span><span class="method-badge">click to expand · live config · no SSH required</span><span class="summary-hint" id="controlSummaryHint">Max / Derived Ladder / Candle Probe / Reversal Probe</span></summary><div class="control-grid"><label>TSL Activation %<input id="ctrlTSLAct" type="number" min="0.10" max="50" step="0.01" inputmode="decimal"></label><label>TSL Trail %<input id="ctrlTSLTrail" type="number" min="0.05" max="25" step="0.01" inputmode="decimal"></label><label>ATR Stop x<input id="ctrlATR" type="number" min="0.1" max="10" step="0.05" inputmode="decimal"></label><label>Phantom Extension %<input id="ctrlPhantom" type="number" min="0.05" max="10" step="0.01" inputmode="decimal"></label><label>Max Conviction Contracts<input id="ctrlMaxConviction" type="number" min="1" max="50" step="1"></label><label>Probe % of Max<input id="ctrlProbePct" type="number" min="5" max="100" step="1" inputmode="decimal"></label><label>Partial % of Max<input id="ctrlPartialPct" type="number" min="5" max="100" step="1" inputmode="decimal"></label><label>Strong % of Max<input id="ctrlStrongPct" type="number" min="5" max="100" step="1" inputmode="decimal"></label><label>Max Adds / Position<input id="ctrlMaxAdds" type="number" min="0" max="10" step="1"></label><label>Signal Lock / Reversal Probe Minutes<input id="ctrlSignalValidity" type="number" min="1" max="120" step="1"></label><label>Cancel Score<input id="ctrlSignalCancel" type="number" min="0" max="4" step="1"></label><label>Signal Lock / Reversal Probe<select id="ctrlSignalLock"><option value="true">On</option><option value="false">Off</option></select></label><label>Email Alerts<select id="ctrlEmail"><option value="true">On</option><option value="false">Off</option></select></label><label>Telegram Alerts<select id="ctrlTelegram"><option value="true">On</option><option value="false">Off</option></select></label><label>Telegram Errors<select id="ctrlTelegramErrors"><option value="true">On</option><option value="false">Off</option></select></label><label>Daily Telegram Summary<select id="ctrlTelegramDaily"><option value="true">On</option><option value="false">Off</option></select></label><label>Daily Summary Hour ET<input id="ctrlTelegramHour" type="number" min="0" max="23" step="1"></label><label>Max Leverage<input id="ctrlMaxLev" type="number" min="0" max="20" step="0.1" inputmode="decimal"></label><label>Futures Buffer $<input id="ctrlBuffer" type="number" min="0" step="25"></label><label>Cooldown Sec<input id="ctrlCooldown" type="number" min="0" step="30"></label><label>Spot Tranches %<input id="ctrlTranches" type="text" placeholder="25,33,50,90"></label><button class="btn save-btn" onclick="saveStrategyControls()">Save</button></div><div class="pnl-note" id="sizingPreview">Sizing preview loads with config.</div><div class="pnl-note" id="controlStatus">Strategy config path: gs://btc_trade_log/strategy_config.json. Bot reloads each cycle. Max Conviction is the only size knob. Probe/partial/strong/full are derived from Max × percentages and rounded to whole contracts. Candle-close confirmation enters the probe; phantom extension upgrades/adds toward partial.</div></details></section>
<section class="card span6"><h2>Live Exposure & Leverage</h2><div class="metric-row" style="grid-template-columns:repeat(2,1fr)"><div class="metric"><div class="label">Net BTC Delta</div><div class="val" id="portNetDelta">—</div><div class="mini" id="portDeltaMini">Spot BTC + signed Perp notional BTC</div></div><div class="metric"><div class="label">Perp Notional</div><div class="val" id="portNotional">—</div><div class="mini" id="portLeverage">—</div></div></div><div class="kv"><span class="k">Spot BTC</span><span class="v" id="portSpotBtc">—</span></div><div class="kv"><span class="k">Signed Perp BTC</span><span class="v" id="portPerpBtc">—</span></div><div class="kv"><span class="k">Exchange side/contracts</span><span class="v" id="portExchange">—</span></div><div class="pnl-note" id="portSummary">—</div></section>
<section class="card span4"><h2>Futures / Perp Equity</h2><div class="big" id="futEquity">—</div><div class="sub">Coinbase futures total USD balance</div><div class="kv"><span class="k">Available margin</span><span class="v" id="availMargin">—</span></div><div class="kv"><span class="k">Buying power</span><span class="v" id="buyingPower">—</span></div><div class="kv"><span class="k">Initial margin</span><span class="v" id="initMargin">—</span></div><div class="kv"><span class="k">Liquidation buffer</span><span class="v" id="liqBuffer">—</span></div></section>
<section class="card span4"><h2>Spot Treasury</h2><div class="big blue" id="spotValue">—</div><div class="sub">BTC mark value; cash excluded to avoid double count</div><div class="kv"><span class="k">USD cash</span><span class="v" id="spotUsd">—</span></div><div class="kv"><span class="k">USDC treasury</span><span class="v" id="spotUsdc">—</span></div><div class="kv"><span class="k">BTC value</span><span class="v" id="spotBtcUsd">—</span></div><div class="kv"><span class="k">BTC amount</span><span class="v" id="spotBtcAmt">—</span></div><div class="kv treasury-total"><span class="k">Accounts scanned</span><span class="v" id="spotAccountCount">—</span></div><div class="mini" id="spotSource">—</div></section>
<section class="card span5"><h2>Spot Signal Monitor</h2><div class="score-pips" id="scorePips"><div class="score-pip"></div><div class="score-pip"></div><div class="score-pip"></div><div class="score-pip"></div></div><div class="sub" style="margin:8px 0 12px" id="scoreText">—</div><div id="signalCards"></div></section>
<section class="card span4"><h2>Signal Score Reconciliation <span class="method-badge">raw monitor → filters → tradable score</span></h2><div class="metric-row" style="grid-template-columns:repeat(2,1fr)"><div class="metric"><div class="label">Exchange Net Position</div><div class="val" id="perpNetPos">—</div><div class="mini" id="perpNetBtc">—</div></div><div class="metric"><div class="label">Lifecycle / Phantom</div><div class="val" id="phantomState">—</div><div class="mini" id="phantomDetail">—</div></div></div><div class="score-reconcile-grid"><div class="score-reconcile-card"><div class="title">Raw LONG Monitor</div><div class="value" id="perpLongScore">—</div><div id="perpLongPips"></div><div class="note" id="perpLongNote">Raw conditions only</div></div><div class="score-reconcile-card"><div class="title">Final LONG Score</div><div class="value" id="diagFinalLongScore">—</div><div class="note" id="diagFinalLongNote">Tradable score used by Larry</div></div><div class="score-reconcile-card"><div class="title">Raw SHORT Monitor</div><div class="value" id="perpShortScore">—</div><div id="perpShortPips"></div><div class="note" id="perpShortNote">Raw conditions only</div></div><div class="score-reconcile-card"><div class="title">Final SHORT Score</div><div class="value" id="diagFinalShortScore">—</div><div class="note" id="diagFinalShortNote">Tradable score used by Larry</div></div></div><div class="opportunity-banner" id="opportunityBanner">Loading opportunity diagnostics…</div>
<div class="score-reconcile-grid" style="margin-top:10px">
  <div class="score-reconcile-card"><div class="title">Current Signal Truth</div><div class="value" id="currentSignalTruth">—</div><div class="note" id="currentSignalTruthNote">Current cycle only</div></div>
  <div class="score-reconcile-card"><div class="title">Last Sizing Decision</div><div class="value" id="lastSizingTruth">—</div><div class="note" id="lastSizingTruthNote">Historical, not a live trigger</div></div>
  <div class="score-reconcile-card"><div class="title">Last Order / Execution</div><div class="value" id="lastExecutionTruth">—</div><div class="note" id="lastExecutionTruthNote">Historical order state</div></div>
  <div class="score-reconcile-card"><div class="title">Live Trigger Status</div><div class="value" id="liveTriggerTruth">—</div><div class="note" id="liveTriggerTruthNote">Uses current score + risk + funding + lifecycle</div></div>
</div>
<div class="pnl-note" id="perpNettingNote">Raw monitor may differ from final tradable score because Larry applies candle, macro, funding, risk-gate, phantom, and reversal-probe filters. Historical sizing/order fields are labelled separately so old 2/4 or 4/4 decisions do not look like current live triggers.</div></section>
<section class="card span12"><h2>Core IAF Perp Engine Monitor <span class="method-badge">WorkingOG / risk-first rule stack</span></h2><div class="metric-row"><div class="metric"><div class="label">Framework</div><div class="val" id="iafFramework">—</div><div class="mini">Institutional J-curve model</div></div><div class="metric"><div class="label">Funding Gate</div><div class="val" id="iafFundingGate">—</div><div class="mini" id="iafFundingDetail">—</div></div><div class="metric"><div class="label">Phantom Delay</div><div class="val" id="iafPhantom">—</div><div class="mini" id="iafPhantomDetail">—</div></div><div class="metric"><div class="label">Safe Execution Rule</div><div class="val">NET TARGET</div><div class="mini" id="iafSafeRule">—</div></div></div><div class="rule-grid" id="iafRules" style="margin-top:10px"></div><div class="pnl-note">This panel does not place trades. It makes strategy drift visible by comparing the live unified setup with the original IAF/WorkingOG rules.</div></section>
<section class="card span6"><h2>Open Perp Exposure</h2><div class="table-wrap"><table class="table"><thead><tr><th>Side</th><th>Contracts</th><th>BTC Exp.</th><th>Avg Entry</th><th>Mark</th><th>Book Unrealized</th><th>Cost Basis</th></tr></thead><tbody id="perpPosBody"></tbody></table></div></section>
<section class="card span4 diagnostic-card"><h2>Legacy Clean Strategy Book <span class="method-badge">diagnostic</span></h2><div class="kv"><span class="k">Book avg entry</span><span class="v" id="bookAvg">—</span></div><div class="kv"><span class="k">Book open contracts</span><span class="v" id="bookContracts">—</span></div><div class="kv"><span class="k">Book unrealized P&L</span><span class="v" id="bookUnr">—</span></div><div class="kv"><span class="k">Closed P&L since reset</span><span class="v" id="realizedReset">—</span></div><div class="kv"><span class="k">Fees since tracking reset</span><span class="v" id="feesReset">—</span></div><div class="kv"><span class="k">Net book impact</span><span class="v" id="netBookImpact">—</span></div><div class="kv"><span class="k">New fills counted</span><span class="v" id="fillCount">—</span></div><div class="kv"><span class="k">Old fills ignored</span><span class="v" id="ignoredFillCount">—</span></div><div class="kv"><span class="k">Fee audit</span><span class="v"><a href="/api/fee_audit" target="_blank">Open</a></span></div><div class="pnl-note" id="tradePnlNote">—</div></section>
<section class="card span6"><h2>Position Reconciler / Phantom Protection</h2><div class="metric-row" style="grid-template-columns:repeat(2,1fr)"><div class="metric"><div class="label">Reconciler Status</div><div class="val" id="recStatus">—</div><div class="mini" id="recDetail">—</div></div><div class="metric"><div class="label">Target / Drift</div><div class="val" id="recOpening">—</div><div class="mini">Target exposure vs live Coinbase</div></div></div><div class="kv"><span class="k">Exchange net position</span><span class="v" id="recExchange">—</span></div><div class="kv"><span class="k">Local bot legs</span><span class="v" id="recBotLegs">—</span></div><div class="kv"><span class="k">Signed net contracts</span><span class="v" id="recSigned">—</span></div><div class="pnl-note" id="recSafeRule">—</div></section>
<section class="card span6"><h2>Execution Quality <span class="method-badge">Larry ledger · costs & slippage</span></h2><div class="metric-row"><div class="metric"><div class="label">Avg Slippage</div><div class="val" id="execAvgSlip">—</div><div class="mini" id="execSlipRange">Best / worst —</div></div><div class="metric"><div class="label">Maker / Taker</div><div class="val" id="execMakerTaker">—</div><div class="mini" id="execMakerTakerNote">Larry orders</div></div><div class="metric"><div class="label">Fees Paid</div><div class="val" id="execFeesPaid">—</div><div class="mini">From Larry ledger</div></div><div class="metric"><div class="label">Execution Score</div><div class="val" id="execScore">—</div><div class="mini" id="execScoreNote">Preliminary</div></div></div><div class="pnl-note" id="execQualityNote">Execution quality summarizes Larry trade fills. Current market IOC orders should appear mostly/all taker.</div></section>
<section class="card span4 diagnostic-card"><h2>Signal History <span class="method-badge">diagnostic</span></h2><div id="sigHistory" class="mini">—</div></section>
<section class="card span12 diagnostic-card"><h2>Detailed Accounting / Reference <span class="method-badge">diagnostic</span></h2><div class="table-wrap"><table class="table"><thead><tr><th>Book</th><th>Current Value / Basis</th><th>Clean Realized</th><th>Clean Unrealized</th><th>Funding</th><th>Fees</th><th>Notes</th></tr></thead><tbody id="acctBody"></tbody></table></div><div class="pnl-note">Clean performance uses the live Coinbase basis for the current position and post-reset fills. Coinbase exchange-native daily realized P&L is shown as a reference only and is not included in clean P&L or return on capital.</div></section>
</div></details>
</main></div>
<script>
// v73: the session cookie can expire (12h) while this page stays open. Wrap fetch
// so any 401 (session gone) sends the operator back to the login page instead of
// every panel silently failing.
(function(){
  const _origFetch = window.fetch.bind(window);
  window.fetch = async function(...args){
    const res = await _origFetch(...args);
    if(res.status === 401){ window.location.href = '/login'; }
    return res;
  };
})();
async function signOut(){ await fetch('/logout',{method:'POST'}); window.location.href='/login'; }
async function sendSnapshot(ev){
  const to = prompt('Email the snapshot to (leave blank to send to the address on file):', '');
  if(to===null) return; // cancelled
  const btn = ev && ev.target; const orig = btn ? btn.innerHTML : '';
  if(btn){ btn.disabled = true; btn.innerHTML = 'Sending…'; }
  try{
    const r = await fetch('/api/snapshot', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({to: to.trim()})});
    const d = await r.json();
    alert(d.ok ? ('✅ Snapshot emailed to '+d.sent_to+'\nStatus: '+(d.status||'—')) : ('Snapshot failed: '+(d.error||'unknown')));
  }catch(e){ alert('Snapshot failed: '+e); }
  finally{ if(btn){ btn.disabled = false; btn.innerHTML = orig || '📸 Snapshot'; } }
}
const $=id=>document.getElementById(id); const num=(n,d=2)=>n==null||isNaN(n)?'—':Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
function fmtET(ts){ if(!ts) return '—'; try { return new Date(ts).toLocaleString('en-US',{timeZone:'America/New_York',weekday:'short',month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true})+' ET'; } catch(e){ return String(ts); } }
const usd=n=>n==null||isNaN(n)?'—':(Number(n)<0?'-':'')+'$'+num(Math.abs(Number(n)),2); const pct=n=>n==null||isNaN(n)?'—':num(n,2)+'%';
const cls=n=>Number(n)>0?'good':Number(n)<0?'bad':'muted'; function set(id,v){const e=$(id); if(e)e.textContent=v} function setClass(id,c){const e=$(id); if(e)e.className=c} function pill(id,t,c){const e=$(id); if(e){e.textContent=t;e.className='pill '+c}}
function warn(items){const e=$('warnBox'); if(!items||!items.length){e.style.display='none';return} e.style.display='block'; e.innerHTML=items.map(x=>'⚠ '+x).join('<br>')}
function gauge(label,valueText,pctVal,zoneLeft,zoneWidth,desc,active=false){return `<div class="sig-card ${active?'active':''}"><div class="sig-head"><span class="sig-name">${label}</span><span class="sig-val ${active?'good':''}">${valueText}</span></div><div class="gauge"><div class="zone" style="left:${zoneLeft}%;width:${zoneWidth}%"></div><div class="needle" style="left:${Math.max(0,Math.min(pctVal,100))}%"></div></div><div class="sig-desc">${desc}</div></div>`}

function renderMonitorPips(obj, shortMode=false){
 const entries=Object.entries(obj||{}); if(!entries.length) return '<div class="muted">No components</div>';
 return `<div class="monitor-pips">${entries.map(([k,v])=>`<div class="monitor-pip ${v?'on':''} ${shortMode?'short':''}">${v?'✓':'·'} ${k}</div>`).join('')}</div>`
}

function renderPositionRisk(pr){
 const box=$('perpRiskCard'); if(!box) return;
 if(!pr||!pr.has_position){ box.innerHTML='<div class="risk-flat-card"><div class="risk-side muted">FLAT 0</div><div class="risk-contracts">No live Coinbase perp position detected.</div><div class="risk-stop-card"><div class="risk-stop-head"><span class="stop-badge atr">NO ACTIVE STOP</span><span class="mini">Risk controls reset</span></div><div class="risk-status">Waiting for next live position. ATR/TSL levels will initialize from the next Coinbase avg entry.</div></div></div>'; return; }
 const side=(pr.side||'FLAT'); const isShort=side==='SHORT'; const pnl=Number(pr.unrealized_pnl||0); const pnlPct=pr.unrealized_pnl_pct;
 const tslActive=!!pr.tsl_active; const stopClass=tslActive?'tsl':'atr'; const progress=tslActive?100:(pr.progress_to_activation_pct==null?0:Math.max(0,Math.min(100,Number(pr.progress_to_activation_pct))));
 const actGap=pr.activation_gap; const stopDist=pr.distance_to_stop;
 box.innerHTML=`
 <div class="risk-hero">
  <div class="risk-main ${isShort?'short':''} ${tslActive?'tsl-on':''}">
   <div class="risk-side ${side==='LONG'?'good':side==='SHORT'?'bad':'muted'}">${side} ${num(pr.contracts,0)}</div>
   <div class="risk-contracts">${num(pr.contracts,0)} micro contracts · ${num(Math.abs(pr.btc_exposure||0),4)} BTC notional exposure</div>
   <div class="risk-stop-card">
    <div class="risk-stop-head"><span class="stop-badge ${stopClass}">${pr.stop_type||'—'}</span>${pr.manual_monitor_only?'<span class="stop-badge manual">MANUAL · MONITOR ONLY</span>':''}<span class="mini">Source: ${pr.source||'—'}</span></div>
    <div class="stop-line"><div class="stop-progress ${tslActive?'active':''}" style="width:${progress}%"></div></div>
    <div class="risk-status">${pr.status||'—'}</div>
   </div>
  </div>
  <div class="risk-grid">
   <div class="metric"><div class="label">Entry Price</div><div class="val">${usd(pr.entry_price)}</div><div class="mini">Live Coinbase avg entry</div></div>
   <div class="metric"><div class="label">Current / Mark</div><div class="val">${usd(pr.current_price)}</div><div class="mini">Latest futures mark</div></div>
   <div class="metric"><div class="label">Unrealized P&L</div><div class="val ${cls(pnl)}">${usd(pnl)}</div><div class="mini">${pnlPct==null?'—':pct(pnlPct)} on price move</div></div>
   <div class="metric"><div class="label">Notional Value</div><div class="val">${usd(pr.notional)}</div><div class="mini">contracts × 0.01 BTC × mark</div></div>
   <div class="metric"><div class="label">ATR Hard Stop</div><div class="val">${usd(pr.atr_stop)}</div><div class="mini">${num(pr.atr_stop_multiplier||1.5,2)}x ATR14</div></div>
   <div class="metric"><div class="label">TSL Activates At</div><div class="val">${usd(pr.tsl_activation_price)}</div><div class="mini">${pct((pr.tsl_activation_pct||0)*100)} activation ${actGap!=null&&!tslActive?' · gap '+usd(actGap):''}</div></div>
   <div class="metric"><div class="label">Current Active Stop</div><div class="val ${tslActive?'good':'warn'}">${usd(pr.active_stop)}</div><div class="mini">${tslActive?'Trailing stop':'ATR stop'} ${stopDist!=null?' · cushion '+usd(stopDist):''}</div></div>
   <div class="metric"><div class="label">TSL Trail / High</div><div class="val">${pct((pr.tsl_trail_pct||0)*100)}</div><div class="mini">High ${pr.highest_price?usd(pr.highest_price):'—'}${pr.lowest_price?' · Low '+usd(pr.lowest_price):''}</div></div>
  </div>
 </div>`;
}

function renderPerpMonitor(pm, d){
 if(!pm||pm.error){set('perpNetPos','—');set('phantomState','Unavailable');set('phantomDetail',pm&&pm.error?pm.error:'—');return}
 const signed=Number(pm.signed_now||0); const side=signed>0?'LONG':signed<0?'SHORT':'FLAT';
 set('perpNetPos',`${side} ${Math.abs(signed)}`); setClass('perpNetPos','val '+(signed>0?'good':signed<0?'bad':'muted'));
 set('perpNetBtc',`${num(Math.abs(signed)*(pm.contract_size_btc||0.01),4)} BTC exposure`);
 const ph=pm.phantom_state||'—'; set('phantomState',ph); const phCls=(ph==='OK'?'good':ph==='PHANTOM RISK'?'bad':String(ph).includes('MANUAL')||String(ph).includes('EXCHANGE-ONLY')?'blue':'warn'); setClass('phantomState','val '+phCls); set('phantomDetail',(pm.phantom_detail||'—')+(pm.phantom_state&&String(pm.phantom_state).includes('MANUAL')?' · Manual positions are monitor-only and do not block the trading gate.':''));
 set('perpLongScore',`${pm.long_score||0}/4`); $('perpLongPips').innerHTML=renderMonitorPips(pm.long_components,false); set('perpLongNote',`Raw monitor only · ${pm.long_triggered?'raw trigger active':'raw trigger not active'} · final score shown to the right`);
 set('perpShortScore',`${pm.short_score||0}/4`); $('perpShortPips').innerHTML=renderMonitorPips(pm.short_components,true); set('perpShortNote',`Raw monitor only · ${pm.net_effect||'—'}`); set('perpNettingNote',`Raw monitor may differ from final tradable score. ${pm.target_short_effect||''} ${pm.note||''}`);
}


function renderIAF(iaf){
 if(!iaf){return}
 set('iafFramework',iaf.framework||'—');
 set('iafFundingGate',iaf.funding_gate_status||'—'); setClass('iafFundingGate','val '+((iaf.funding_gate_status||'')==='OPEN'?'good':'bad'));
 set('iafFundingDetail',`rate ${iaf.funding_rate ?? '—'} · time ${iaf.funding_time || '—'}`);
 set('iafPhantom',iaf.phantom_delay_status||'—'); setClass('iafPhantom','val '+((iaf.phantom_delay_status||'')==='Not published'?'warn':'blue'));
 set('iafPhantomDetail',iaf.phantom_delay_detail||'—');
 set('iafSafeRule',iaf.safe_execution_rule||'—');
 const rules=iaf.rule_notes||[];
 $('iafRules').innerHTML=rules.map(r=>{
   const st=(r.status||'').toLowerCase();
   const stc=st.includes('ok')?'ok':st.includes('active')?'active':st.includes('open')?'open':st.includes('monitor')?'monitored':st.includes('mod')?'modified':st.includes('halt')?'halted':st.includes('pause')?'paused':st.includes('block')?'blocked':st.includes('need')?'needs':'modified';
   return `<div class="rule-card"><div class="rule-title">${r.rule}</div><div class="rule-current">${r.current}</div><div class="rule-status ${stc}">${r.status}</div></div>`
 }).join('') || '<div class="muted">No IAF rule state published</div>'
}
function renderPortfolio(po){
 if(!po){return}
 set('portNetDelta',`${num(po.net_btc_delta,4)} BTC`); setClass('portNetDelta','val '+cls(po.net_btc_delta));
 set('portNotional',usd(po.gross_perp_notional)); set('portLeverage',po.effective_leverage==null?'—':`${num(po.effective_leverage,2)}x futures equity leverage`);
 set('portDeltaMini',`${Math.abs(po.abs_contracts||0)} micro contracts × ${num(po.contract_size_btc||0.01,4)} BTC`);
 set('portSpotBtc',`${num(po.spot_btc,8)} BTC`); set('portPerpBtc',`${po.exchange_side||'FLAT'} ${Math.abs(po.abs_contracts||0)} micro contracts / ${num(po.perp_btc,4)} BTC notional`); set('portExchange',`${po.exchange_side||'—'} ${Math.abs(po.net_contracts||0)} contracts @ avg ${po.exchange_avg_entry?usd(po.exchange_avg_entry):'—'}`); set('portSummary',po.summary||'—');
}
function renderReconciler(rec){
 if(!rec){return}
 const status=rec.status||'—'; set('recStatus',status); setClass('recStatus','val '+(status==='OK'?'good':status.includes('PHANTOM')?'bad':'warn'));
 set('recDetail',rec.detail||'—'); set('recOpening',`Target ${rec.target_signed==null?'—':rec.target_signed} / Drift ${rec.exposure_drift==null?'—':rec.exposure_drift}`);
 set('recExchange',`${rec.exchange_side||'—'} ${rec.exchange_contracts||0}`); set('recBotLegs',rec.local_bot_legs||0); set('recSigned',rec.signed_now||0); set('recSafeRule',rec.safe_rule||'—');
}

function renderSignals(sig){if(!sig||sig.error){$('signalCards').innerHTML='<div class="muted">Signal data unavailable</div>';set('scoreText',sig&&sig.error?sig.error:'—');return} const score=Number(sig.score||0); [...$('scorePips').children].forEach((p,i)=>p.className='score-pip '+(i<score?'on':'')); set('scoreText',`Score ${score}/4 · ${sig.granularity||''}`); const rsiL=Number(sig.rsi_floor||20), rsiT=Number(sig.rsi_threshold||28); $('signalCards').innerHTML=[
 gauge('RSI',num(sig.rsi,2),sig.rsi_pct||0,rsiL,Math.max(1,rsiT-rsiL),`<span>Buy band ${num(rsiL,0)}–${num(rsiT,0)}</span><span class="${sig.sig_rsi?'good':'muted'}">${sig.sig_rsi?'✓ ACTIVE':'not active'}</span>`,!!sig.sig_rsi),
 gauge('Bollinger Band',`$${num(sig.price,2)}`,sig.bb_pct||0,0,8,`<span>Lower band $${num(sig.bb_lower,2)}</span><span class="${sig.sig_bb?'good':'muted'}">${sig.sig_bb?'✓ ACTIVE':'above lower'}</span>`,!!sig.sig_bb),
 gauge('Volume Spike',num(sig.vol_ratio,2)+'x',sig.vol_pct||0,50,50,`<span>Trigger ≥ ${num(sig.vol_threshold,2)}x</span><span class="${sig.sig_vol?'good':'muted'}">${sig.sig_vol?'✓ ACTIVE':'not active'}</span>`,!!sig.sig_vol),
 gauge('StochRSI',num(sig.stoch,3),(sig.stoch_pct||0),0,(Number(sig.stoch_threshold||.1)*100),`<span>Trigger ≤ ${num(sig.stoch_threshold,2)}</span><span class="${sig.sig_stoch?'good':'muted'}">${sig.sig_stoch?'✓ ACTIVE':'not active'}</span>`,!!sig.sig_stoch)
 ].join('')}
function renderHistory(h){$('sigHistory').innerHTML=(h||[]).slice().reverse().map(x=>`<div class="hist-row"><span class="hist-ts">${x.ts||''}</span><span class="hist-score s${x.score||0}">${x.score??'—'}</span><span><span class="sig-pip ${x.sig_rsi?'sp-on':'sp-off'}">R</span><span class="sig-pip ${x.sig_bb?'sp-on':'sp-off'}">B</span><span class="sig-pip ${x.sig_vol?'sp-on':'sp-off'}">V</span><span class="sig-pip ${x.sig_stoch?'sp-on':'sp-off'}">S</span></span><span class="mini">$${num(x.price,0)}</span></div>`).join('')||'<div class="muted">No signal history yet</div>'}
function renderLadder(id,max,onCount,manualContracts){let html=''; for(let i=1;i<=max;i++){const on=i<=onCount; html+=`<div class="slot ${on?'on':''}"><div class="num">${i}</div><div class="lbl">${on?'Active':'Available'}</div></div>`} if(manualContracts>0){html+=`<div class="slot manual"><div class="num">${manualContracts}</div><div class="lbl">Exchange contracts</div></div>`} $(id).innerHTML=html}

function yn(v){return v===true?'YES':v===false?'NO':'—'}
function ynClass(v){return 'val check '+(v===true?'yes':v===false?'no':'')}
function renderMacro(mac, gate){
 const known=mac&&mac.regime&&mac.regime!=='UNKNOWN'; const macroOpen=mac&&mac.gate_open===true;
 const riskOpen = !gate || gate.entries_allowed === true;
 const clsName = gate ? (gate.color||'unknown') : (!known?'unknown':macroOpen?'enabled':'blocked');
 const txt = gate ? (gate.headline||'Trading Gate') : (!known?'UNKNOWN':macroOpen?'🟢 Trading Enabled':'🔴 Trading Blocked');
 const reason = gate ? `${gate.reason||'—'} · ${gate.manual_management_text||''}` : (macroOpen?'Spot entries and Perp bridge are allowed by macro gate.':(mac.blocked_reason||'Macro gate status unknown'));
 const e=$('tradeStatus'); if(e){e.className='status-banner '+clsName; e.innerHTML=`<span>${txt}</span><strong>${gate ? (gate.label||'—') : (mac.gate_text||'—')}</strong>`}
 set('macroRegime',mac.regime||'UNKNOWN'); setClass('macroRegime','macro-state '+(macroOpen?'good':known?'bad':'warn'));
 set('macroReason',reason);
 set('macroPrice',usd(mac.price)); set('macroFast',usd(mac.sma_fast)); set('macroSlow',usd(mac.sma_slow)); set('macroFastPeriod',mac.fast_period?'('+mac.fast_period+')':''); set('macroSlowPeriod',mac.slow_period?'('+mac.slow_period+')':'');
 set('macroAboveFast',yn(mac.above_fast)); setClass('macroAboveFast',ynClass(mac.above_fast));
 set('macroAboveSlow',yn(mac.above_slow)); setClass('macroAboveSlow',ynClass(mac.above_slow));
 set('macroFastAboveSlow',yn(mac.fast_above_slow)); setClass('macroFastAboveSlow',ynClass(mac.fast_above_slow));
 const macroGateText = macroOpen ? 'OPEN' : (known ? 'SPOT/BRIDGE FILTERED' : 'UNKNOWN');
 set('macroGate', macroGateText); setClass('macroGate','val '+(macroOpen?'good':known?'warn':'warn'));
 set('macroDistFast',pct(mac.price_vs_fast_pct)); setClass('macroDistFast','val '+cls(mac.price_vs_fast_pct)); set('macroDistSlow',pct(mac.price_vs_slow_pct)); setClass('macroDistSlow','val '+cls(mac.price_vs_slow_pct)); set('macroDistSpread',pct(mac.fast_vs_slow_pct)); setClass('macroDistSpread','val '+cls(mac.fast_vs_slow_pct));
}


function updateSizingPreview(cfg){
 cfg = cfg || {};
 const max = Number((document.getElementById('ctrlMaxConviction')||{}).value || cfg.MAX_CONVICTION_CONTRACTS || cfg.CONTRACTS_PER_TRADE_FULL || 10);
 const probePct = Number((document.getElementById('ctrlProbePct')||{}).value || ((cfg.PROBE_PCT||0.20)*100));
 const partialPct = Number((document.getElementById('ctrlPartialPct')||{}).value || ((cfg.PARTIAL_PCT||0.40)*100));
 const strongPct = Number((document.getElementById('ctrlStrongPct')||{}).value || ((cfg.STRONG_PCT||0.70)*100));
 const adds = Number((document.getElementById('ctrlMaxAdds')||{}).value || cfg.MAX_POSITION_ADDS || 3);
 const tier=(pct)=>Math.max(1, Math.min(max, Math.round(max*(pct/100))));
 let probe=tier(probePct), partial=tier(partialPct), strong=tier(strongPct);
 if(max>=4){ probe=Math.max(1, Math.min(probe, max-3)); partial=Math.max(probe+1, Math.min(partial, max-2)); strong=Math.max(partial+1, Math.min(strong, max-1)); }
 else if(max===3){ probe=1; partial=2; strong=2; }
 else { probe=1; partial=1; strong=1; }
 const rungs=[...new Set([0,1,probe,partial,strong,max])].sort((a,b)=>a-b);
 const step=(v)=>{ const lower=rungs.filter(x=>x<v); return lower.length?Math.max(...lower):0; };
 const tpLine=`${max}→${step(max)} · ${strong}→${step(strong)} · ${partial}→${step(partial)} · ${probe}→${step(probe)} · 1→0`;
 const el=document.getElementById('sizingPreview');
 if(el){ el.innerHTML = `Sizing ladder from Max <b>${max}</b>: Probe <b>${probe}</b> (${probePct.toFixed(0)}%) → Partial <b>${partial}</b> (${partialPct.toFixed(0)}%) → Strong <b>${strong}</b> (${strongPct.toFixed(0)}%) → Full <b>${max}</b>. Max adds/position: <b>${adds}</b>.<br><span class="mini">TP step-down ladder: ${tpLine}. Whole contracts only; probe keeps a 1-contract runner.</span>`; }
}

function setControlValues(cfg){
 if(!cfg) return;
 const setv=(id,v)=>{const el=$(id); if(el && v!==undefined && v!==null) el.value=v};
 setv('ctrlTSLAct', ((cfg.TSL_ACTIVATION_PCT||0)*100).toFixed(2));
 setv('ctrlTSLTrail', ((cfg.TSL_TRAIL_PCT||0)*100).toFixed(2));
 setv('ctrlATR', cfg.ATR_STOP_MULTIPLIER||1.5);
 setv('ctrlPhantom', ((cfg.PHANTOM_EXTENSION_PCT||0)*100).toFixed(2));
 setv('ctrlMaxConviction', cfg.MAX_CONVICTION_CONTRACTS||cfg.CONTRACTS_PER_TRADE_FULL||10);
 setv('ctrlProbePct', ((cfg.PROBE_PCT||0.20)*100).toFixed(0));
 setv('ctrlPartialPct', ((cfg.PARTIAL_PCT||0.40)*100).toFixed(0));
 setv('ctrlStrongPct', ((cfg.STRONG_PCT||0.70)*100).toFixed(0));
 setv('ctrlMaxAdds', cfg.MAX_POSITION_ADDS||3);
 setv('ctrlSignalValidity', cfg.SIGNAL_VALIDITY_MINUTES||20);
 setv('ctrlSignalCancel', cfg.SIGNAL_CANCEL_SCORE ?? 1);
 setv('ctrlSignalLock', String(cfg.SIGNAL_LOCK_ENABLED!==false));
 updateSizingPreview(cfg);
 const em=$('ctrlEmail'); if(em){ em.value = (cfg.SEND_EMAIL===false?'false':'true'); }
 const tg=$('ctrlTelegram'); if(tg){ tg.value = (cfg.SEND_TELEGRAM===false?'false':'true'); }
 const tge=$('ctrlTelegramErrors'); if(tge){ tge.value = (cfg.TELEGRAM_INCLUDE_ERRORS===false?'false':'true'); }
 const tgd=$('ctrlTelegramDaily'); if(tgd){ tgd.value = (cfg.TELEGRAM_DAILY_SUMMARY_ENABLED===false?'false':'true'); }
 setv('ctrlTelegramHour', cfg.TELEGRAM_DAILY_SUMMARY_HOUR_ET ?? 21);
 setv('ctrlMaxLev', cfg.MAX_EFFECTIVE_LEVERAGE||3);
 setv('ctrlBuffer', cfg.MIN_FUTURES_EQUITY_BUFFER_USD||1000);
 setv('ctrlCooldown', cfg.PERP_ENTRY_COOLDOWN_SEC||cfg.MIN_ENTRY_COOLDOWN_SECONDS||300);
 setv('ctrlTranches', (cfg.SPOT_TRANCHE_TARGETS_PCT||[25,33,50,90]).join(','));
 ['ctrlMaxConviction','ctrlProbeContracts','ctrlPartialContracts','ctrlMaxAdds','ctrlSignalValidity','ctrlSignalCancel','ctrlSignalLock'].forEach(id=>{const el=$(id); if(el && !el.__previewBound){el.addEventListener('input',()=>updateSizingPreview(cfg)); el.__previewBound=true;}});
}


function cooldownLine(cd){
 if(!cd) return '—';
 const active = cd.active===true;
 const rem = cd.remaining_seconds || 0;
 return active ? `cooling ${rem}s` : 'ready';
}
function fundingBucket(direction, rate, cfg){
 rate = Number(rate || 0);
 const reduce = Number(cfg.FUNDING_SIZE_REDUCE_AT || 0.0005);
 const longMax = Number(cfg.FUNDING_LONG_MAX || 0.001);
 const shortMin = Number(cfg.FUNDING_SHORT_MIN || -0.001);
 if(direction==='LONG'){ if(rate > longMax) return 'BLOCK'; if(rate > reduce) return 'PARTIAL'; return 'FULL'; }
 if(direction==='SHORT'){ if(rate < shortMin) return 'BLOCK'; if(rate < -reduce) return 'PARTIAL'; return 'FULL'; }
 return 'FULL';
}



window.TRIGGER_MAP_SETTINGS = window.TRIGGER_MAP_SETTINGS || {zoom:true,layers:{buy:true,sell:true,risk:true,manual:true,macro:true,current:true}};
function triggerLayerFor(item){
 const label=String((item&&item.label)||'').toLowerCase(); const type=String((item&&item.type)||'').toLowerCase();
 if(type==='current') return 'current';
 if(type==='manual'||label.includes('manual')||label.includes('entry')) return 'manual';
 if(label.includes('sma')||label.includes('macro')) return 'macro';
 if(label.includes('atr')||label.includes('tsl')||label.includes('tp1')||label.includes('stop')) return 'risk';
 if(label.includes('short')||type==='sell') return 'sell';
 if(label.includes('buy')||label.includes('long')||type==='buy') return 'buy';
 return 'risk';
}
function triggerVisible(item, price){
 const st=window.TRIGGER_MAP_SETTINGS||{}; const layer=triggerLayerFor(item);
 if(st.layers && st.layers[layer]===false) return false;
 if(st.zoom && item && item.type!=='current'){
   const d=Math.abs(triggerDistancePct(item.level, price)||0);
   if(d>3 && layer!=='manual' && layer!=='risk') return false;
   if(d>6 && (layer==='macro')) return false;
 }
 return true;
}
function setTriggerLayer(layer){
 window.TRIGGER_MAP_SETTINGS=window.TRIGGER_MAP_SETTINGS||{zoom:true,layers:{}};
 window.TRIGGER_MAP_SETTINGS.layers=window.TRIGGER_MAP_SETTINGS.layers||{};
 window.TRIGGER_MAP_SETTINGS.layers[layer]=!(window.TRIGGER_MAP_SETTINGS.layers[layer]!==false);
 if(window.__LAST_DASH_DATA) renderBTCTriggerMap(window.__LAST_DASH_DATA);
}
function toggleTriggerZoom(){
 window.TRIGGER_MAP_SETTINGS=window.TRIGGER_MAP_SETTINGS||{zoom:true,layers:{}};
 window.TRIGGER_MAP_SETTINGS.zoom=!window.TRIGGER_MAP_SETTINGS.zoom;
 if(window.__LAST_DASH_DATA) renderBTCTriggerMap(window.__LAST_DASH_DATA);
}
function triggerChip(layer,label,cls=''){
 const st=window.TRIGGER_MAP_SETTINGS||{layers:{}}; const on=(st.layers||{})[layer]!==false;
 return `<button class="trigger-chip ${cls} ${on?'on':''}" onclick="setTriggerLayer('${layer}')">${on?'✓':'·'} ${label}</button>`;
}
function triggerZoomChip(){
 const st=window.TRIGGER_MAP_SETTINGS||{};
 return `<button class="trigger-chip ${st.zoom?'on':''}" onclick="toggleTriggerZoom()">${st.zoom?'✓ Near Price ±3%':'Full Map'}</button>`;
}
function triggerLevelObj(label, level, type, note, lane, priority){
 return {label, level:Number(level), type:type||'', note:note||'', lane:lane||'right', priority:priority||50};
}
function triggerTop(level,minL,maxL){
 const span=Math.max(1, maxL-minL);
 return Math.max(4, Math.min(96, (1 - ((Number(level)-minL)/span))*100));
}
function triggerDistancePct(level, price){
 if(!level || !price) return null;
 return ((Number(level)/Number(price))-1)*100;
}
function clusterTriggerLevels(items, price){
 const valid=(items||[]).filter(x=>x&&isFinite(x.level)&&x.level>0).sort((a,b)=>b.level-a.level);
 if(!valid.length) return [];
 const threshold=Math.max(price*0.0015, 90); // cluster when within ~15 bps or $90
 const clusters=[];
 for(const item of valid){
   let c=clusters.find(cl=>Math.abs(cl.anchor-item.level)<=threshold);
   if(!c){ c={anchor:item.level, items:[]}; clusters.push(c); }
   c.items.push(item);
   c.anchor=c.items.reduce((sum,x)=>sum+x.level,0)/c.items.length;
 }
 return clusters.map(c=>{ c.items.sort((a,b)=>(a.priority||50)-(b.priority||50)); return c; });
}
function clusterMarker(cluster, minL, maxL, price){
 const items=cluster.items||[]; if(!items.length) return '';
 const primary=items[0]; const count=items.length;
 const type = items.some(x=>x.type==='current')?'current':items.some(x=>x.type==='sell')?'sell':items.some(x=>x.type==='buy')?'buy':items.some(x=>x.type==='manual')?'manual':'warn';
 const lane = items.some(x=>x.lane==='left')?'left':items.some(x=>x.lane==='center')?'center':'right';
 const top=triggerTop(cluster.anchor,minL,maxL);
 const avgLevel=cluster.anchor;
 const dist=triggerDistancePct(avgLevel,price);
 if(count===1){
   const note = primary.note ? ` · ${primary.note}` : '';
   const distTxt = primary.type==='current' ? '' : ` · ${dist==null?'—':(dist>0?'+':'')+num(dist,2)+'%'}`;
   return `<div class="trigger-marker ${type} lane-${lane}" style="top:${top}%"><div class="level">${usd(primary.level)}</div><div class="line"><span class="tag">${primary.label}${note}${distTxt}</span></div></div>`;
 }
 const names=items.slice(0,4).map(x=>x.label).join(' · ')+(items.length>4?' · …':'');
 const notes=items.map(x=>`${x.label}${x.note?' ('+x.note+')':''}`).slice(0,5).join('<br>');
 return `<div class="trigger-marker cluster ${type} lane-${lane}" style="top:${top}%"><div class="level">${usd(avgLevel)}</div><div class="line"><span class="tag"><span class="cluster-count">${count}</span><span class="cluster-title">${names}</span><div class="cluster-items">${notes}<br>${dist==null?'':'Distance: '+(dist>0?'+':'')+num(dist,2)+'%'}</div></span></div></div>`;
}
function nearestTriggers(items, price){
 const valid=(items||[]).filter(x=>x&&isFinite(x.level)&&x.level>0&&x.type!=='current');
 const above=valid.filter(x=>x.level>price).sort((a,b)=>a.level-b.level)[0];
 const below=valid.filter(x=>x.level<price).sort((a,b)=>b.level-a.level)[0];
 const card=(x,title)=>x?`<div class="nearest-card"><div class="label">${title}</div><div class="val ${x.type==='sell'?'bad':x.type==='buy'?'good':x.type==='manual'?'purple':'warn'}">${x.label}</div><div class="note">${usd(x.level)} · ${(triggerDistancePct(x.level,price)>0?'+':'')+num(triggerDistancePct(x.level,price),2)}% ${x.note?`· ${x.note}`:''}</div></div>`:`<div class="nearest-card"><div class="label">${title}</div><div class="val muted">None nearby</div><div class="note">No level in this direction inside the current map range.</div></div>`;
 return `<div class="trigger-nearest">${card(above,'Nearest upside trigger')}${card(below,'Nearest downside trigger')}</div>`;
}
function renderBTCTriggerMap(d){
 const box=$('btcTriggerMap'); if(!box) return;
 const cfg=d.config||{}, sig=d.signals||{}, mac=d.macro||{}, pr=d.position_risk||{}, es=d.engine_state||{}, pc=es.position_controls||{}, pm=d.perp_signal_monitor||{};
 const price=Number(d.price || sig.price || pr.current_price || 0);
 if(!price){ box.innerHTML='<div class="muted">BTC price unavailable.</div>'; return; }
 const lower=Number(sig.bb_lower||0), upper=Number(sig.bb_upper||0), mid=Number(sig.bb_mid||0);
 const entry=Number(pr.entry_price||0), atrStop=Number(pc.atr_stop||pr.atr_stop||0), activeStop=Number(pr.active_stop||pc.tsl_stop||0);
 const tslActivation=Number(pc.tsl_activation_price||pr.tsl_activation_price||0), tp1=Number(pc.tp1_trigger_price||0);
 const fast=Number(mac.sma_fast||0), slow=Number(mac.sma_slow||0);
 const phantom=es.phantom||{}; const phantomExt=Number(phantom.extension_price||0);
 const phPct=Number(cfg.PHANTOM_EXTENSION_PCT||0.005);
 const longPhantomPreview = price * (1-phPct);
 const shortPhantomPreview = price * (1+phPct);
 let rawLevels=[price,lower,upper,mid,entry,atrStop,activeStop,tslActivation,tp1,fast,slow,phantomExt,longPhantomPreview,shortPhantomPreview].filter(x=>x&&isFinite(x));
 let minL=Math.min(...rawLevels), maxL=Math.max(...rawLevels); const pad=Math.max(250,(maxL-minL)*0.18); minL-=pad; maxL+=pad;
 const longScore=Number(sig.score ?? pm.long_score ?? 0); const shortScore=Number(pm.short_score ?? 0); const macroOpen=mac.gate_open===true;
 let next='No immediate action. Monitoring signal score, phantom confirmation, funding, cooldowns, and risk gate.'; let nextClass='blue';
 if(es.kill_switch && es.kill_switch.halt){ next='KILL SWITCH ACTIVE: all order placement is halted.'; nextClass='bad'; }
 else if(pr.manual_monitor_only){ next='Manual perp exposure detected: Larry is monitor-only and will not place ATR/TSL/TP1 exits against it.'; nextClass='purple'; }
 else if(phantom.state==='PHANTOM_ARMED'){ next=`${phantom.direction||''} phantom armed. Waiting for extension level ${usd(phantomExt)} then closed-candle reversal.`; nextClass='warn'; }
 else if(phantom.state==='EXTENSION_CONFIRMED'){ next=`${phantom.direction||''} extension confirmed. Waiting for last closed candle confirmation.`; nextClass='warn'; }
 else if(longScore>=3 && macroOpen){ next=`Spot/Perp LONG setup building: long score ${longScore}/4 and macro gate open.`; nextClass='good'; }
 else if(shortScore>=3){ next=`Perp SHORT/reduction setup building: short score ${shortScore}/4.`; nextClass='bad'; }
 const levels=[];
 levels.push(triggerLevelObj('Current BTC',price,'current','live mark','center',1));
 if(entry) levels.push(triggerLevelObj('Manual / Live Avg Entry',entry,'manual',`${pr.contracts||0} contracts`,'center',3));
 if(activeStop) levels.push(triggerLevelObj(pr.tsl_active?'Active TSL Stop':'Active Stop',activeStop,pr.tsl_active?'sell':'warn',pr.stop_type||'','left',2));
 if(atrStop) levels.push(triggerLevelObj('ATR hard stop',atrStop,'sell',`${num(cfg.ATR_STOP_MULTIPLIER||1.5,2)}x`,'left',4));
 if(tp1) levels.push(triggerLevelObj('TP1 partial',tp1,'sell',`${pct((cfg.TP1_PCT||0.0075)*100)} gain`,'left',5));
 if(tslActivation) levels.push(triggerLevelObj('TSL activates',tslActivation,'warn',`${pct((cfg.TSL_ACTIVATION_PCT||0.015)*100)}`,'left',6));
 if(lower) levels.push(triggerLevelObj('Spot BUY / LONG BB Zone',lower,'buy',`score ${longScore}/4`,'right',20));
 if(longPhantomPreview) levels.push(triggerLevelObj('LONG phantom extension',longPhantomPreview,'buy',`${pct(phPct*100)} below signal`,'right',21));
 if(upper) levels.push(triggerLevelObj('Perp SHORT BB Zone',upper,'sell',`score ${shortScore}/4`,'left',20));
 if(shortPhantomPreview) levels.push(triggerLevelObj('SHORT phantom extension',shortPhantomPreview,'sell',`${pct(phPct*100)} above signal`,'left',21));
 if(fast) levels.push(triggerLevelObj('Fast SMA',fast,'warn',`${cfg.MACRO_FAST_SMA||50}`,'right',40));
 if(slow) levels.push(triggerLevelObj('Slow SMA',slow,'warn',`${cfg.MACRO_SLOW_SMA||200}`,'right',41));
 const visibleLevels=levels.filter(x=>triggerVisible(x,price));
 rawLevels=visibleLevels.map(x=>x.level).concat([price]).filter(x=>x&&isFinite(x));
 minL=Math.min(...rawLevels); maxL=Math.max(...rawLevels); const pad2=Math.max(180,(maxL-minL)*0.22); minL-=pad2; maxL+=pad2;
 const clusters=clusterTriggerLevels(visibleLevels,price);
 const markers=clusters.map(c=>clusterMarker(c,minL,maxL,price)).join('');
 const laneSummary=(layer,title,klass)=>{const arr=visibleLevels.filter(x=>triggerLayerFor(x)===layer&&x.type!=='current'); const nearest=arr.sort((a,b)=>Math.abs(a.level-price)-Math.abs(b.level-price))[0]; return `<div class="trigger-lane-card"><div class="label">${title}</div><div class="val ${klass}">${nearest?nearest.label:'Hidden / none'}</div><div class="note">${nearest?usd(nearest.level)+' · '+((triggerDistancePct(nearest.level,price)>0?'+':'')+num(triggerDistancePct(nearest.level,price),2)+'%'):'Use layer toggles to show more levels.'}</div></div>`};
 const controls=`<div class="trigger-control-grid">${triggerZoomChip()}${triggerChip('buy','Buy / Long','buy')}${triggerChip('sell','Sell / Short','sell')}${triggerChip('risk','Stops / TP / TSL','risk')}${triggerChip('manual','Manual','manual')}${triggerChip('macro','SMA / Macro','')}</div>`;
 box.innerHTML=`<div class="trigger-map-wrap"><div class="trigger-hero"><div class="mini">BTC live price</div><div class="trigger-price">${usd(price)}</div><div class="trigger-next ${nextClass}">${next}</div>${nearestTriggers(visibleLevels,price)}${controls}<div class="trigger-mode-card"><div class="mini">Interactive view</div><div class="trigger-lanes">${laneSummary('buy','Next Buy / Long','good')}${laneSummary('sell','Next Sell / Short','bad')}${laneSummary('risk','Next Risk / Exit','warn')}${laneSummary('manual','Manual Reference','purple')}</div></div><div class="trigger-legend"><span class="legend-pill">🟢 Buy / Long</span><span class="legend-pill">🔴 Sell / Short</span><span class="legend-pill">🟠 Risk / TP / TSL</span><span class="legend-pill">🟣 Manual</span><span class="legend-pill">🔵 Current Price</span></div><div class="trigger-table"><div class="trigger-tile"><div class="label">Visible / Total Levels</div><div class="val good">${visibleLevels.length} / ${levels.length}</div><div class="note">Use toggles to simplify crowded areas. Near-price mode hides distant signal clutter.</div></div><div class="trigger-tile"><div class="label">Overlap handling</div><div class="val good">${clusters.filter(c=>(c.items||[]).length>1).length} clusters</div><div class="note">Nearby levels are grouped into bubbles with priority labels and exact details.</div></div><div class="trigger-tile"><div class="label">Spot BUY trigger view</div><div class="val ${longScore>=3?'good':'muted'}">Score ${longScore}/4</div><div class="note">Uses RSI, lower BB, volume spike and StochRSI. Macro gate: ${macroOpen?'open':'filtered'}.</div></div><div class="trigger-tile"><div class="label">Risk exits</div><div class="val ${pr.tsl_active?'good':'warn'}">${pr.tsl_active?'TSL active':'ATR / TP1 armed'}</div><div class="note">TP1 ${pc.tp1_done?'done':'pending'} · ATR locked ${pc.atr_at_entry?usd(pc.atr_at_entry):'—'}.</div></div></div></div><div class="trigger-map"><div class="trigger-axis"></div>${markers}</div></div>`;
}

function renderRiskIntelligence(d){
 const ri=d.risk_intelligence||{}; const l=ri.larry_strategy||{}, m=ri.manual_book||{}, ex=ri.execution_quality||{}, opp=ri.opportunity_cost||{}, rx=ri.risk_exposure||{}, dd=ri.drawdown||{}, fu=ri.funding||{}, h=ri.health||{}, sva=ri.expected_vs_actual||{};
 const currentBlocked = d.current_blocked_actions || opp.last_blocked_action || {};
 set('intelLarryPnl', usd(l.net_pnl)); setClass('intelLarryPnl','intel-val '+cls(l.net_pnl)); set('intelLarryNote', l.note||'Larry clean strategy P&L excludes manual monitor-only exposure.');
 // v78: the ONE place manual trading is acknowledged. Single footnote, no dual monitoring.
 if(m.active){ set('intelManualBook', `${m.side||'—'} ${num(m.contracts,0)} · ${usd(m.unrealized_pnl)}`); setClass('intelManualBook','intel-val '+cls(m.unrealized_pnl)); set('intelManualNote', `Entry ${usd(m.avg_entry_price)} · Mark ${usd(m.current_price)} · excluded from Larry. Top up to cover any manual losses.`); }
 else { set('intelManualBook','No manual position'); setClass('intelManualBook','intel-val muted'); set('intelManualNote',"Manual trades are excluded from Larry performance. If they lose money, top up funds to keep the account whole — Larry's numbers are unaffected."); }
 set('intelExposure', `${num(rx.net_btc_delta,4)} BTC net`); setClass('intelExposure','intel-val '+cls(rx.net_btc_delta)); set('intelExposureNote', `Spot ${num(rx.spot_btc,4)} BTC · Perp ${num(rx.perp_btc,4)} BTC · Notional ${usd(rx.gross_perp_notional)} · Lev ${rx.effective_leverage==null?'—':num(rx.effective_leverage,2)+'x'}.`);
 // v78 bug fix: this tile used ri.execution_quality.sample_count while the dedicated
 // Execution Quality panel uses larry_trade_accounting.execution_quality.sample_trades —
 // the two disagreed ("Grade A / 143" vs "No samples"). Both now read the ledger source.
 const exq=(d.larry_trade_accounting||{}).execution_quality||{}; const exSamples=Number(exq.sample_trades||ex.sample_count||0); const exAvg=(exq.avg_slippage_bps!=null?exq.avg_slippage_bps:ex.avg_slippage_bps);
 set('intelSlippage', exSamples?`${num(exAvg,2)} bps avg`:'No samples'); setClass('intelSlippage','intel-val '+(exAvg>0?'warn':'good')); set('intelSlippageNote', `Worst ${exq.worst_slippage_bps==null?(ex.worst_slippage_bps==null?'—':num(ex.worst_slippage_bps,2)+' bps'):num(exq.worst_slippage_bps,2)+' bps'} · Samples ${exSamples}.`);
 const blockerCount=Object.keys(currentBlocked).filter(k=>currentBlocked[k]).length; set('intelOppCost', `${blockerCount} current blockers`); setClass('intelOppCost','intel-val '+(blockerCount>0?'warn':'good')); set('intelOppNote', blockerCount?Object.entries(currentBlocked).map(([k,v])=>`${k}: ${v}`).join(' · '):'No current blocked action.');
 set('intelDrawdown', usd(dd.current_strategy_pnl)); setClass('intelDrawdown','intel-val '+cls(dd.current_strategy_pnl)); set('intelDrawdownNote', `Larry strategy P&L since reset. Full drawdown curve pending periodic equity snapshots.`);
 set('intelFundingCost', usd(fu.today_or_session_funding_pnl)); setClass('intelFundingCost','intel-val '+cls(fu.today_or_session_funding_pnl)); set('intelFundingNote', `Rate ${fu.funding_rate==null?'—':Number(fu.funding_rate).toFixed(6)} · Long gate ${fu.long_gate_open===false?'closed':'open/—'} · Short gate ${fu.short_gate_open===false?'closed':'open/—'}.`);
 const hs=h.heartbeat_health||'—'; set('intelHealth', hs); setClass('intelHealth','intel-val '+(hs==='LIVE'?'good':hs==='STALE'?'warn':'bad')); set('intelHealthNote', `Age ${h.heartbeat_age_secs==null?'—':h.heartbeat_age_secs+'s'} · State ${h.bot_state||'—'} · Kill ${h.kill_switch&&h.kill_switch.halt?'HALTED':'clear'} · Updated ${h.last_updated||'—'}.`);
 const rows=(ri.signal_attribution&&ri.signal_attribution.rows)||[];
 const roll=$('intelSignalRollup'); if(roll){ if(rows.length){ roll.innerHTML=`<table class="tiny-table"><thead><tr><th>Class</th><th>Trades</th><th>Closed</th><th>P&L</th><th>Win%</th><th>Slip</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${r.signal_class||r.class||'—'}</td><td>${r.trades||0}</td><td>${r.closing_trades||0}</td><td class="${cls(r.gross_realized_usd)}">${usd(r.gross_realized_usd)}</td><td>${r.win_rate||'—'}</td><td>${r.avg_slippage_bps||'—'}</td></tr>`).join('')}</tbody></table>`; } else { roll.innerHTML='No rollup yet. Upload/run <code>analyze_signal_pnl.py --write</code> on the VM to populate signal_pnl_rollup.csv.'; } }
 const exp=$('intelExpectedActual'); if(exp){ exp.innerHTML=`Actual Larry P&L: <b class="${cls(sva.actual_larry_strategy_pnl)}">${usd(sva.actual_larry_strategy_pnl)}</b><br>Manual P&L: <b class="${cls(sva.actual_manual_pnl)}">${usd(sva.actual_manual_pnl)}</b><br>Combined equity drift: <b class="${cls(sva.actual_combined_equity_drift)}">${usd(sva.actual_combined_equity_drift)}</b><br><span class="muted">${sva.expected_note||''}</span>`; }
}

function renderSignalLifecycle(d){
 const es=d.engine_state||{}, cfg=d.config||{}, ph=es.phantom||{}, life=es.signal_lifecycle||{};
 const st=life.state||ph.state||'MONITORING';
 const dir=life.direction||ph.direction||'—';
 const exp=life.expires_at||ph.expires_at;
 const armed=life.armed_at||ph.armed_at;
 const lockScore=life.locked_score ?? ph.locked_score;
 const lockConf=life.locked_confidence_pct ?? ph.locked_confidence_pct;
 const lockTarget=life.locked_target_contracts ?? ph.locked_target_contracts;
 const nowMs=Date.now(); let remain='—';
 if(exp){ const ms=new Date(exp).getTime()-nowMs; remain=ms>0?`${Math.ceil(ms/60000)}m left`:'expired'; }
 const clsMap={MONITORING:'muted',PHANTOM_ARMED:'warn',EXTENSION_CONFIRMED:'warn',COMMITTED_ENTRY:'good',FUNDING_BLOCKED:'bad'};
 set('lifeState', st); setClass('lifeState','v12-val '+(clsMap[st]||'warn'));
 set('lifeStateNote', `${dir} · ${ph.reason||life.reason||'Waiting for setup'}`);
 set('lifeLockedSetup', lockScore!=null?`${dir} score ${lockScore}/4 · ${lockConf ?? '—'}%`:'No locked setup'); setClass('lifeLockedSetup','v12-val '+(lockScore!=null?'good':'muted'));
 set('lifeLockedNote', `Locked target ${lockTarget ?? '—'} contracts · armed ${fmtET(armed)} · confirmation ${(ph.confirmation_mode||'next_closed_candle')} · extension achieved ${ph.extension_achieved?'YES':'NO'} · confidence freeze ${cfg.FREEZE_CONFIDENCE_ON_ARM===false?'OFF':'ON'}.`);
 set('lifeValidity', `${remain}`); setClass('lifeValidity','v12-val '+(remain==='expired'?'bad':exp?'good':'muted'));
 set('lifeValidityNote', `Configured ${cfg.SIGNAL_VALIDITY_MINUTES||20} minutes. Setup expires instead of constantly re-qualifying forever.`);
 set('lifeHysteresis', `Arm ≥${cfg.SIGNAL_HYSTERESIS_ARM_SCORE||3}/4 · cancel ≤${cfg.SIGNAL_CANCEL_SCORE ?? 1}/4`); setClass('lifeHysteresis','v12-val good');
 set('lifeHysteresisNote', 'Once armed, minor score wobble is ignored. Only score collapse or expiry cancels the setup.');
 set('lifeCommitRule', cfg.SIGNAL_COMMIT_ON_CLOSED_CANDLE===false?'Tick confirm':'Next closed candle'); setClass('lifeCommitRule','v12-val good');
 set('lifeCommitNote', 'v23: probe entries confirm on the next closed candle. Phantom extension is monitored for sizing/add-ons, not required for entry.');
 const sig=d.signals||{};
 let why=ph.reason||life.reason||''; if(!why){ why=`Long ${sig.long_score??'—'}/4 · Short ${sig.short_score??'—'}/4`; }
 set('lifeWhyWaiting', st==='COMMITTED_ENTRY'?'Committed':'Waiting'); setClass('lifeWhyWaiting','v12-val '+(st==='COMMITTED_ENTRY'?'good':'warn'));
 set('lifeWhyNote', why);
}

function renderV12Transparency(d){
 const cfg=d.config||{}, es=d.engine_state||{}, pc=es.position_controls||{}, risk=d.position_risk||{}, mg=es.manual_position_status||{}, fund=es.funding||{}, cds=es.cooldowns||{}, sig=d.signals||{}, ph=es.phantom||{};
 const manualMode = d.manual_position_mode || cfg.MANUAL_POSITION_MODE || mg.mode || 'monitor_only';
 const livePos = es.exchange_position || d.exchange_position || {};
 const liveContracts = Number(livePos.contracts || livePos.number_of_contracts || mg.live_signed || 0);
 // Only show the manual/external warning when there is actual live external exposure.
 // Older dashboard versions also looked at risk.manual_monitor_only / stale blocked-action fields,
 // which caused the banner to remain after the account was flat.
 const isManual = (mg.is_manual_or_external===true) && Math.abs(liveContracts) > 0;
 const banner=$('v12ManualBanner');
 if(banner){ banner.className='v12-banner '+(isManual?'manual':''); banner.textContent = isManual ? '⚠ Manual / external perp position detected. Larry is MONITOR ONLY and will not ATR-stop, TSL-stop, TP1, add, flip, or flatten this exposure.' : '🟢 No unmanaged manual perp exposure blocking bot-managed execution. Manual mode remains monitor-only by default.'; }
 const tp1Done = pc.tp1_done===true;
 const tp1Trig = pc.tp1_trigger_price;
 set('v12Tp1Status', tp1Done ? 'TP1 DONE' : (tp1Trig ? 'TP1 ARMED' : 'WAITING')); setClass('v12Tp1Status','v12-val '+(tp1Done?'good':tp1Trig?'warn':'muted'));
 set('v12Tp1Note', `Trigger ${usd(tp1Trig)} · target ${pc.tp1_target_contracts ?? 'next lower rung'} contracts · active TP ${(Number(pc.tp1_pct_active || cfg.TP1_PCT || 0.0075)*100).toFixed(2)}% · ladder step-down, not a stop.`);
 set('v12AtrLock', pc.atr_at_entry ? `$${num(pc.atr_at_entry,2)}` : 'Not locked yet'); setClass('v12AtrLock','v12-val '+(pc.atr_at_entry?'good':'warn'));
 set('v12AtrNote', `Entry avg used ${usd(pc.atr_entry_avg)} · ATR stop ${usd(pc.atr_stop)} · multiplier ${num(cfg.ATR_STOP_MULTIPLIER||1.5,2)}x.`);
 const sd = es.last_core_sizing_decision || (es.last_core_target_plan && es.last_core_target_plan.sizing_decision) || {};
 const ladder=(sd&&sd.sizing_ladder)||{}; const mx=cfg.MAX_CONVICTION_CONTRACTS||ladder.full||10;
 let probe=ladder.probe||cfg.CONTRACTS_PER_TRADE_PROBE||Math.max(1,Math.round(mx*(cfg.PROBE_PCT||0.20)));
 let part=ladder.partial||cfg.CONTRACTS_PER_TRADE_PARTIAL||Math.max(probe,Math.round(mx*(cfg.PARTIAL_PCT||0.40)));
 let strong=ladder.strong||Math.max(part,Math.round(mx*(cfg.STRONG_PCT||0.70)));
 if(!ladder.probe && mx>=4){ probe=Math.max(1, Math.min(probe, mx-3)); part=Math.max(probe+1, Math.min(part, mx-2)); strong=Math.max(part+1, Math.min(strong, mx-1)); }
 const rungs=[...new Set([0,1,probe,part,strong,mx])].sort((a,b)=>a-b); const step=(v)=>{const lower=rungs.filter(x=>x<v); return lower.length?Math.max(...lower):0};
 const tpLine=`${mx}→${step(mx)} · ${strong}→${step(strong)} · ${part}→${step(part)} · ${probe}→${step(probe)} · 1→0`;
 const trig=(cfg.TP1_DYNAMIC_BY_LADDER===false)?`Fixed ${(Number(cfg.TP1_PCT||0.0075)*100).toFixed(2)}%`:`Probe/Partial ${(Number(cfg.TP1_PROBE_TRIGGER_PCT||0.0075)*100).toFixed(2)}% · Strong ${(Number(cfg.TP1_STRONG_TRIGGER_PCT||0.006)*100).toFixed(2)}% · Full ${(Number(cfg.TP1_FULL_TRIGGER_PCT||0.005)*100).toFixed(2)}%`;
 set('v12Sizing', `Probe ${probe} · Partial ${part} · Strong ${strong} · Full ${mx}`);
 const addState = es.add_on_state || {};
 set('v12SizingNote', `Max conviction ${mx} contracts. Tiers derive from Max and round to whole contracts. TP step-down: ${tpLine}. TP triggers: ${trig}. Progressive add-ons ${cfg.PROGRESSIVE_ADD_ONS_ENABLED===false?'OFF':'ON'}: target size rises only when confidence improves. Last sizing: confidence ${sd.confidence_pct ?? '—'}%, target ${sd.target_abs_contracts ?? sd.final_contracts ?? '—'} contracts, reason ${sd.reason || 'waiting'}. Add state: ${addState.adds_count ?? 0}/${cfg.MAX_POSITION_ADDS||3} adds, last confidence ${addState.last_add_confidence_pct ?? 0}%, last target ${addState.last_target_contracts ?? 0}. Macro-blocked probe: ${cfg.SCORE4_MACRO_OVERRIDE_ENABLED?'ON':'OFF'} / ${probe} contracts. Phantom extension target: ${ph.extension_price?usd(ph.extension_price):'—'}; achieved ${ph.extension_achieved?'YES':'NO'}.`);
 const rate = Number((fund&&fund.rate)!=null?fund.rate:0); const lb=fundingBucket('LONG',rate,cfg), sb=fundingBucket('SHORT',rate,cfg);
 set('v12Funding', `L ${lb} / S ${sb}`); setClass('v12Funding','v12-val '+((lb==='BLOCK'||sb==='BLOCK')?'bad':(lb==='PARTIAL'||sb==='PARTIAL')?'warn':'good'));
 set('v12FundingNote', `Rate ${rate?rate.toFixed(6):'—'} · reduce at ±${Number(cfg.FUNDING_SIZE_REDUCE_AT||0.0005).toFixed(4)} · hard gates +${Number(cfg.FUNDING_LONG_MAX||0.001).toFixed(4)} / ${Number(cfg.FUNDING_SHORT_MIN||-0.001).toFixed(4)}.`);
 const longCd = cooldownLine(cds.perp_long || cds.perp_last_long_entry_at || cds.long || cds.perp);
 const shortCd = cooldownLine(cds.perp_short || cds.perp_last_short_entry_at || cds.short || cds.perp);
 const bridgeCd = cooldownLine(cds.bridge);
 set('v12Cooldowns', `Long ${longCd} · Short ${shortCd}`);
 set('v12CooldownNote', `Bridge ${bridgeCd}. v12 tracks LONG and SHORT cooldowns separately so one side does not suppress the other.`);
 set('v12ManualMode', manualMode.toUpperCase().replace('_',' ')); setClass('v12ManualMode','v12-val '+(manualMode==='monitor_only'?'good':'warn'));
 set('v12ManualNote', mg.reason || 'Manual/external Coinbase positions are observed and emailed, not managed, unless full_management is explicitly enabled.');
 set('v12Phantom', ph.state || 'MONITORING');
 set('v12PhantomNote', `Extension ${(Number(cfg.PHANTOM_EXTENSION_PCT||0.005)*100).toFixed(2)}% is now an add-on/sizing signal. Entries confirm on the next CLOSED candle, not tick noise. ${ph.reason||''}`);
 set('v12SignalPnl', 'Signal class ledger enabled');
 set('v12SignalPnlNote', 'Ledger includes signal_class + signed slippage_bps. Run analyze_signal_pnl.py --write on the VM to refresh rollup.');
 renderSignalLifecycle(d);
}
async function loadSignalPnlRollup(){
 const box=$('signalPnlRollupBox'); if(!box) return;
 box.textContent='Loading signal P&L rollup…';
 try{
  const r=await fetch('/api/signal_pnl_rollup?ts='+Date.now(),{cache:'no-store'}); const d=await r.json();
  if(!d.ok){ box.textContent='Rollup unavailable: '+(d.error||'unknown'); return; }
  if(!d.rows||!d.rows.length){
   box.textContent=(d.note||'No rollup rows yet.');
   const hint=document.createElement('div'); hint.className='muted';
   hint.textContent='VM command: /home/msunderji/bot-env/bin/python3 /home/msunderji/analyze_signal_pnl.py --write';
   box.appendChild(document.createElement('br')); box.appendChild(hint);
   return;
  }
  // v73 fix: rows come from a GCS-hosted CSV (signal_pnl_rollup.csv). Build the table
  // with DOM APIs and textContent instead of string-interpolated innerHTML so a stray
  // value in that file can never be parsed as markup.
  const cols=Object.keys(d.rows[0]);
  const table=document.createElement('table'); table.className='table';
  const thead=document.createElement('thead'); const headRow=document.createElement('tr');
  cols.forEach(c=>{const th=document.createElement('th'); th.textContent=c; headRow.appendChild(th);});
  thead.appendChild(headRow); table.appendChild(thead);
  const tbody=document.createElement('tbody');
  d.rows.forEach(row=>{
   const tr=document.createElement('tr');
   cols.forEach(c=>{const td=document.createElement('td'); td.textContent=row[c]; tr.appendChild(td);});
   tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  box.textContent=''; box.appendChild(table);
 }catch(e){ box.textContent='Rollup load failed: '+e.message; }
}


function boolIcon(v){return v?'✅':'☐'}
function condList(obj){obj=obj||{}; return Object.keys(obj).map(k=>`${boolIcon(obj[k])} ${k.replaceAll('_',' ')}`).join(' · ') || '—'}


let __tradeMapRange='1M';
let __tradeMapMarkers=[];
function parseTimeMs(x){ if(!x) return null; const t=Date.parse(x); return Number.isFinite(t)?t:null; }
function shortTimeLabel(ms, range){ const dt=new Date(ms); const opts=(range==='1D'||range==='1W')?{month:'short',day:'numeric',hour:'numeric'}:{month:'short',day:'numeric'}; return dt.toLocaleString('en-US',opts); }
function tradeMapCutoff(range){ const now=Date.now(); if(range==='1D') return now-24*3600*1000; if(range==='1W') return now-7*24*3600*1000; if(range==='1M') return now-31*24*3600*1000; if(range==='YTD') return new Date(new Date().getFullYear(),0,1).getTime(); if(range==='12M') return now-365*24*3600*1000; return 0; }
function tradeKind(r){ const reason=String(r.reason||'').toUpperCase(); const action=String(r.action||'').toUpperCase(); const net=Number(r.net_realized_pnl_usd||0); if(action==='BUY') return {kind:'buy',label: reason.includes('ADD')?'ADD':'BUY',emoji:'🟢'}; if(reason.includes('TP')) return {kind:'tp',label:'TP',emoji:'🟡'}; if(reason.includes('STOP')||reason.includes('ATR')||reason.includes('TSL')||reason.includes('FLATTEN')) return {kind:'stop',label: reason.includes('ATR')?'ATR':reason.includes('TSL')?'TSL':'EXIT',emoji:'🔴'}; if(action==='SELL') return {kind: net>=0?'tp':'stop',label:'SELL',emoji: net>=0?'🟡':'🔴'}; return {kind:'other',label:action||'TRADE',emoji:'⚪'}; }
function selectTradeMapPrices(d, range){ const tm=d.trade_map||{}; const hist=(range==='1D'||range==='1W')?(tm.hourly||[]):(tm.daily||tm.hourly||[]); const cutoff=tradeMapCutoff(range); return (hist||[]).map(p=>({t:parseTimeMs(p.timestamp), price:Number(p.price||0), label:p.timestamp_et||fmtET(p.timestamp)})).filter(p=>p.t && p.price && p.t>=cutoff).sort((a,b)=>a.t-b.t); }
function selectTradeMapTrades(d, range){ const rows=((d.larry_trade_accounting||{}).trade_map_trades || (d.larry_trade_accounting||{}).all_trades || (d.larry_trade_accounting||{}).recent_trades || []); const cutoff=tradeMapCutoff(range); return (rows||[]).map(r=>({...r,t:parseTimeMs(r.timestamp), fill:Number(r.fill_price||r.mark_at_send||0)})).filter(r=>r.t && r.t>=cutoff).sort((a,b)=>a.t-b.t); }
function drawLineChart(canvas, points, opts){ if(!canvas) return []; const ctx=canvas.getContext('2d'); const dpr=window.devicePixelRatio||1; const rect=canvas.getBoundingClientRect(); const w=Math.max(320, rect.width||canvas.clientWidth||800), h=Math.max(120, rect.height||canvas.clientHeight||300); canvas.width=Math.round(w*dpr); canvas.height=Math.round(h*dpr); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,h); const pad={l:46,r:16,t:18,b:28}; const iw=w-pad.l-pad.r, ih=h-pad.t-pad.b; ctx.fillStyle='rgba(2,6,23,.28)'; ctx.fillRect(0,0,w,h); if(!points||points.length<2){ ctx.fillStyle='rgba(148,163,184,.8)'; ctx.font='13px system-ui'; ctx.fillText('Not enough data for this range yet',pad.l,pad.t+28); return []; } const xs=points.map(p=>p.t), ys=points.map(p=>p.y); const xmin=Math.min(...xs), xmax=Math.max(...xs); let ymin=Math.min(...ys), ymax=Math.max(...ys); if(ymin===ymax){ymin-=1;ymax+=1;} const ypad=(ymax-ymin)*0.08; ymin-=ypad; ymax+=ypad; const x=t=>pad.l+(t-xmin)/(xmax-xmin)*iw; const y=v=>pad.t+(ymax-v)/(ymax-ymin)*ih; ctx.strokeStyle='rgba(148,163,184,.16)'; ctx.lineWidth=1; ctx.font='11px system-ui'; ctx.fillStyle='rgba(148,163,184,.75)'; for(let i=0;i<4;i++){ const yy=pad.t+ih*i/3; ctx.beginPath(); ctx.moveTo(pad.l,yy); ctx.lineTo(w-pad.r,yy); ctx.stroke(); const val=ymax-(ymax-ymin)*i/3; const txt=(opts.valuePrefix||'')+(opts.valueFormat==='usd'?num(val,0):num(val,2)); ctx.fillText(txt,6,yy+4); } ctx.strokeStyle='rgba(56,189,248,.9)'; ctx.lineWidth=2; ctx.beginPath(); points.forEach((p,i)=>{ const xx=x(p.t), yy=y(p.y); if(i===0)ctx.moveTo(xx,yy); else ctx.lineTo(xx,yy); }); ctx.stroke(); ctx.fillStyle='rgba(148,163,184,.75)'; const left=shortTimeLabel(xmin,opts.range), right=shortTimeLabel(xmax,opts.range); ctx.fillText(left,pad.l,h-8); const tw=ctx.measureText(right).width; ctx.fillText(right,w-pad.r-tw,h-8); return [{xmin,xmax,ymin,ymax,x,y,pad,w,h}][0]; }

function drawPnlBarChart(canvas, rows, opts){
 if(!canvas) return null;
 const ctx=canvas.getContext('2d'); const dpr=window.devicePixelRatio||1; const rect=canvas.getBoundingClientRect();
 const w=Math.max(320, rect.width||canvas.clientWidth||800), h=Math.max(150, rect.height||canvas.clientHeight||300);
 canvas.width=Math.round(w*dpr); canvas.height=Math.round(h*dpr); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,h);
 const pad={l:54,r:22,t:22,b:38}; const iw=w-pad.l-pad.r, ih=h-pad.t-pad.b;
 ctx.fillStyle='rgba(2,6,23,.28)'; ctx.fillRect(0,0,w,h);
 const data=(rows||[]).filter(r=>r.net_realized_pnl_usd!==null && r.net_realized_pnl_usd!=='' && r.net_realized_pnl_usd!==undefined).map((r,i)=>({
   ...r, idx:i+1, y:Number(r.net_realized_pnl_usd||0), label:(r.timestamp_et||fmtET(r.timestamp)||('T'+(i+1)))
 }));
 if(!data.length){ ctx.fillStyle='rgba(148,163,184,.8)'; ctx.font='13px system-ui'; ctx.fillText('No realized trades in this range yet',pad.l,pad.t+28); return null; }
 // Keep the bars scoped to the selected range, but preserve the ledger's all-time
 // running P&L for the cumulative line. Falling back to a local sum supports older
 // ledger rows that predate running_net_pnl_usd.
 let running=0; data.forEach(d=>{ running+=d.y; const ledgerRunning=Number(d.running_net_pnl_usd); d.cum=(d.running_net_pnl_usd!==null && d.running_net_pnl_usd!=='' && d.running_net_pnl_usd!==undefined && Number.isFinite(ledgerRunning))?ledgerRunning:running; });
 const barVals=data.map(d=>d.y), cumVals=data.map(d=>d.cum);
 const maxAbs=Math.max(1, ...barVals.map(v=>Math.abs(v)), ...cumVals.map(v=>Math.abs(v)));
 const ymax=maxAbs*1.18, ymin=-maxAbs*1.18;
 const y=v=>pad.t+(ymax-v)/(ymax-ymin)*ih; const zero=y(0);
 ctx.strokeStyle='rgba(148,163,184,.16)'; ctx.lineWidth=1; ctx.font='11px system-ui'; ctx.fillStyle='rgba(148,163,184,.75)';
 for(let i=0;i<5;i++){ const val=ymax-(ymax-ymin)*i/4; const yy=y(val); ctx.beginPath(); ctx.moveTo(pad.l,yy); ctx.lineTo(w-pad.r,yy); ctx.stroke(); ctx.fillText((val>=0?'':'-')+'$'+Math.abs(val).toFixed(0),6,yy+4); }
 ctx.strokeStyle='rgba(226,232,240,.40)'; ctx.lineWidth=1.2; ctx.beginPath(); ctx.moveTo(pad.l,zero); ctx.lineTo(w-pad.r,zero); ctx.stroke();
 const step=iw/data.length; const bw=Math.max(1,Math.min(36,step*.72));
 const bars=[];
 data.forEach((d,i)=>{ const x=pad.l+i*step+(step-bw)/2; const yy=y(d.y); const bh=Math.abs(zero-yy); const top=Math.min(zero,yy); ctx.fillStyle=d.y>=0?'rgba(34,197,94,.88)':'rgba(239,68,68,.88)'; ctx.fillRect(x,top,bw,Math.max(2,bh)); bars.push({x,y:top,w:bw,h:Math.max(2,bh),cx:x+bw/2,r:d}); });
 // Overlay cumulative Larry realized P&L as a bright line with dots.
 const cx=(i)=>pad.l+(i+.5)*step;
 ctx.strokeStyle='rgba(56,189,248,.96)'; ctx.lineWidth=2.4; ctx.beginPath();
 data.forEach((d,i)=>{ const xx=cx(i), yy=y(d.cum); if(i===0) ctx.moveTo(xx,yy); else ctx.lineTo(xx,yy); });
 ctx.stroke();
 data.forEach((d,i)=>{ const xx=cx(i), yy=y(d.cum); ctx.fillStyle='rgba(56,189,248,.98)'; ctx.strokeStyle='rgba(2,6,23,.95)'; ctx.lineWidth=1.5; ctx.beginPath(); ctx.arc(xx,yy,3.5,0,Math.PI*2); ctx.fill(); ctx.stroke(); });
 ctx.fillStyle='rgba(148,163,184,.75)'; ctx.font='11px system-ui'; ctx.fillText(shortTimeLabel(parseTimeMs(data[0].timestamp)||Date.now(),opts.range),pad.l,h-10); const rt=shortTimeLabel(parseTimeMs(data[data.length-1].timestamp)||Date.now(),opts.range); const tw=ctx.measureText(rt).width; ctx.fillText(rt,w-pad.r-tw,h-10);
 const wins=data.filter(d=>d.y>0).length, losses=data.filter(d=>d.y<0).length, net=data[data.length-1].cum; ctx.font='12px system-ui'; ctx.fillStyle='rgba(226,232,240,.84)'; ctx.fillText(`Bars: per-trade P&L · Line: cumulative realized P&L · ${usd(net)}`, pad.l, 15);
 ctx.fillStyle='rgba(34,197,94,.88)'; ctx.fillRect(w-170,8,10,10); ctx.fillStyle='rgba(148,163,184,.82)'; ctx.fillText('Win',w-156,17);
 ctx.fillStyle='rgba(239,68,68,.88)'; ctx.fillRect(w-124,8,10,10); ctx.fillStyle='rgba(148,163,184,.82)'; ctx.fillText('Loss',w-110,17);
 ctx.strokeStyle='rgba(56,189,248,.96)'; ctx.lineWidth=2; ctx.beginPath(); ctx.moveTo(w-66,13); ctx.lineTo(w-42,13); ctx.stroke(); ctx.fillStyle='rgba(148,163,184,.82)'; ctx.fillText('Cum',w-38,17);
 return {bars,pad,w,h,step};
}

function renderTradeMap(d){ const range=__tradeMapRange||'1M'; const priceCanvas=$('tradeMapPriceCanvas'), pnlCanvas=$('tradeMapPnlCanvas'); if(!priceCanvas||!pnlCanvas) return; const pricesRaw=selectTradeMapPrices(d,range); const trades=selectTradeMapTrades(d,range); const pricePts=pricesRaw.map(p=>({t:p.t,y:p.price,label:p.label})); const scale=drawLineChart(priceCanvas,pricePts,{range,valueFormat:'usd',valuePrefix:'$'}); const ctx=priceCanvas.getContext('2d'); __tradeMapMarkers=[]; if(scale && trades.length){ trades.forEach(r=>{ const k=tradeKind(r); const px = r.fill || (pricePts.length?pricePts.reduce((best,p)=>Math.abs(p.t-r.t)<Math.abs(best.t-r.t)?p:best,pricePts[0]).y:0); if(!px) return; const x=scale.x(Math.max(scale.xmin,Math.min(scale.xmax,r.t))); const y=scale.y(Math.max(scale.ymin,Math.min(scale.ymax,px))); let color='rgba(148,163,184,.95)'; if(k.kind==='buy') color='rgba(34,197,94,.98)'; else if(k.kind==='tp') color='rgba(245,158,11,.98)'; else if(k.kind==='stop') color='rgba(239,68,68,.98)'; ctx.fillStyle=color; ctx.strokeStyle='rgba(2,6,23,.95)'; ctx.lineWidth=2; ctx.beginPath(); if(k.kind==='buy'){ ctx.moveTo(x,y-8); ctx.lineTo(x-7,y+7); ctx.lineTo(x+7,y+7); } else if(k.kind==='stop'){ ctx.moveTo(x,y+8); ctx.lineTo(x-7,y-7); ctx.lineTo(x+7,y-7); } else { ctx.arc(x,y,7,0,Math.PI*2); } ctx.closePath(); ctx.fill(); ctx.stroke(); __tradeMapMarkers.push({x,y,r,k,px}); }); }
 const realizedRows=trades.filter(r=>r.net_realized_pnl_usd!==null && r.net_realized_pnl_usd!=='' && r.net_realized_pnl_usd!==undefined);
 const pnlChart=drawPnlBarChart(pnlCanvas,realizedRows,{range,valueFormat:'usd',valuePrefix:'$'});
 const visibleNet=realizedRows.reduce((a,r)=>a+Number(r.net_realized_pnl_usd||0),0);
 // v79: express Larry's range P&L as a % return on baseline so it is directly comparable
 // to the BTC move % over the same window (both are now % returns). $ figure kept in the note.
 const _base=Number((d.capital||{}).starting_combined_capital||0); const visiblePct=_base?visibleNet/_base*100:null;
 const btcMove=(pricePts.length>=2)?((pricePts[pricePts.length-1].y/pricePts[0].y-1)*100):null; set('tmTradeCount', String(trades.length)); set('tmVisiblePnl', visiblePct==null?usd(visibleNet):pct(visiblePct)); setClass('tmVisiblePnl','val '+cls(visibleNet)); set('tmBtcMove', btcMove==null?'—':pct(btcMove)); setClass('tmBtcMove','val '+cls(btcMove)); set('tmRangeLabel', range); set('tradeMapNote', `${range} view · ${pricePts.length} BTC price points · ${trades.length} Larry trade markers · range realized P&L ${usd(visibleNet)}${visiblePct==null?'':' ('+pct(visiblePct)+' on baseline)'}. Larry Return and BTC move are both % over this window. Lower chart shows per-trade net P&L bars plus all-time cumulative realized P&L.`);
 const tip=$('tradeMapTip'); const showTip=(ev)=>{ if(!tip||!__tradeMapMarkers.length) return; const rect=priceCanvas.getBoundingClientRect(); const cx=(ev.touches?ev.touches[0].clientX:ev.clientX)-rect.left; const cy=(ev.touches?ev.touches[0].clientY:ev.clientY)-rect.top; let best=null, bd=9999; __tradeMapMarkers.forEach(m=>{const dd=Math.hypot(m.x-cx,m.y-cy); if(dd<bd){bd=dd; best=m;}}); if(!best||bd>28){ tip.style.display='none'; return; } const r=best.r,k=best.k; const net=r.net_realized_pnl_usd; tip.innerHTML=`<b>${k.emoji} ${k.label}</b><br>${r.timestamp_et||fmtET(r.timestamp)}<br>Reason: ${r.reason||'—'}<br>Contracts: ${num(r.contracts||0,0)}<br>Fill: ${usd(r.fill_price||r.mark_at_send)}<br>Position after: ${num(r.after_signed||0,0)}<br>${net==null||net===''?'Net P&L: —':'Net P&L: '+usd(net)+' · Running: '+usd(r.running_net_pnl_usd||0)}`; tip.style.left=Math.min(rect.width-270, Math.max(8,best.x+12))+'px'; tip.style.top=Math.min(rect.height-120, Math.max(8,best.y-20))+'px'; tip.style.display='block'; };
 priceCanvas.onmousemove=showTip; priceCanvas.ontouchstart=showTip; priceCanvas.onmouseleave=()=>{ if(tip) tip.style.display='none'; };
 const pnlTip=$('tradeMapPnlTip'); const showPnlTip=(ev)=>{ if(!pnlTip||!pnlChart||!pnlChart.bars.length) return; const rect=pnlCanvas.getBoundingClientRect(); const px=(ev.touches?ev.touches[0].clientX:ev.clientX)-rect.left; let best=null,bd=Infinity; pnlChart.bars.forEach(b=>{const dist=Math.abs(b.cx-px);if(dist<bd){bd=dist;best=b;}}); if(!best||bd>Math.max(12,pnlChart.step*.75)){pnlTip.style.display='none';return;} const r=best.r; pnlTip.innerHTML=`<b>${r.y>=0?'Winning':'Losing'} realized trade</b><br>${r.timestamp_et||fmtET(r.timestamp)}<br>Per-trade P&L: ${usd(r.y)}<br>Cumulative realized P&L: ${usd(r.cum)}<br>${r.reason||r.trade_intent||'—'}`; pnlTip.style.left=Math.min(rect.width-245,Math.max(8,best.cx+10))+'px'; pnlTip.style.top='34px'; pnlTip.style.display='block'; };
 pnlCanvas.onmousemove=showPnlTip; pnlCanvas.ontouchstart=showPnlTip; pnlCanvas.onmouseleave=()=>{if(pnlTip)pnlTip.style.display='none';};
}
function initTradeMapControls(){ const c=$('tradeMapControls'); if(!c||c.dataset.ready) return; c.dataset.ready='1'; c.querySelectorAll('.tm-btn').forEach(btn=>btn.onclick=()=>{ __tradeMapRange=btn.dataset.range||'1M'; c.querySelectorAll('.tm-btn').forEach(b=>b.classList.toggle('on',b===btn)); if(window.__LAST_DASH_DATA) renderTradeMap(window.__LAST_DASH_DATA); }); }

// v79: Larry equity curve + max drawdown + annualized Sharpe, reconstructed
// entirely from the realized-trade ledger (no engine change, real data from day one).
let __eqSeries=[], __eqBase=0, __eqScale=null;
function __eqDrawBase(canvas){ return drawLineChart(canvas, __eqSeries.map(p=>({t:p.t,y:p.y})), {range:'ALL',valueFormat:'usd',valuePrefix:'$'}); }
function attachEquityHover(canvas){
 if(canvas.dataset.hoverReady) return; canvas.dataset.hoverReady='1';
 const tip=$('equityCurveTip');
 const move=(ev)=>{
   if(!__eqScale||!__eqScale.x||__eqSeries.length<2){ if(tip) tip.style.display='none'; return; }
   const rect=canvas.getBoundingClientRect(); const cx=(ev.touches?ev.touches[0].clientX:ev.clientX)-rect.left;
   let best=__eqSeries[0], bd=1e9;
   __eqSeries.forEach(p=>{ const px=__eqScale.x(Math.max(__eqScale.xmin,Math.min(__eqScale.xmax,p.t))); const dd=Math.abs(px-cx); if(dd<bd){bd=dd; best=p;} });
   __eqScale=__eqDrawBase(canvas); // redraw clean curve, then overlay crosshair + dot
   const bx=__eqScale.x(Math.max(__eqScale.xmin,Math.min(__eqScale.xmax,best.t))), by=__eqScale.y(Math.max(__eqScale.ymin,Math.min(__eqScale.ymax,best.y)));
   const ctx=canvas.getContext('2d'); ctx.save();
   ctx.strokeStyle='rgba(247,147,26,.5)'; ctx.lineWidth=1; ctx.setLineDash([4,4]); ctx.beginPath(); ctx.moveTo(bx,__eqScale.pad.t); ctx.lineTo(bx,__eqScale.h-__eqScale.pad.b); ctx.stroke(); ctx.setLineDash([]);
   ctx.fillStyle='#f7931a'; ctx.strokeStyle='rgba(7,8,11,.95)'; ctx.lineWidth=2; ctx.beginPath(); ctx.arc(bx,by,4.5,0,Math.PI*2); ctx.fill(); ctx.stroke(); ctx.restore();
   if(tip){ const ret=__eqBase?((best.y/__eqBase-1)*100):null; const retTxt=(ret==null)?'—':((ret>=0?'+':'')+num(ret,2)+'%'); tip.innerHTML=`<b>${new Date(best.t).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'})}</b><br>Equity <b>${usd(best.y)}</b><br>Return <b class="${ret==null?'':(ret>=0?'good':'bad')}">${retTxt}</b>`; tip.style.left=Math.min(rect.width-160,Math.max(8,bx+12))+'px'; tip.style.top=Math.max(8,by-46)+'px'; tip.style.display='block'; }
 };
 canvas.onmousemove=move; canvas.ontouchstart=move; canvas.ontouchmove=move;
 canvas.onmouseleave=()=>{ if(tip) tip.style.display='none'; if(__eqSeries.length>=2) __eqScale=__eqDrawBase(canvas); };
}
function renderEquityCurve(d){
 const canvas=$('equityCurveCanvas'); if(!canvas) return;
 const base=Number((d.capital||{}).starting_combined_capital||0);
 const la=d.larry_trade_accounting||{};
 const rows=(la.trade_map_trades||la.all_trades||la.recent_trades||[])
   .filter(r=>r.net_realized_pnl_usd!==null && r.net_realized_pnl_usd!=='' && r.net_realized_pnl_usd!==undefined)
   .map(r=>({t:parseTimeMs(r.timestamp), net:Number(r.net_realized_pnl_usd||0)}))
   .filter(r=>r.t).sort((a,b)=>a.t-b.t);
 if(!base || rows.length<1){
   __eqSeries=[]; __eqScale=null; drawLineChart(canvas,[],{range:'ALL',valueFormat:'usd',valuePrefix:'$'});
   set('eqMaxDD','—'); set('eqSharpe','—'); set('eqPeak','—'); set('eqCurrent', base?usd(base):'—');
   set('eqNote', base?'Equity curve builds as Larry closes realized trades.':'Set the capital baseline to enable the equity curve.');
   return;
 }
 // Build equity series (start at baseline).
 let eq=base; const series=[{t:rows[0].t-1, y:base}];
 rows.forEach(r=>{ eq+=r.net; series.push({t:r.t, y:eq}); });
 // Max drawdown (peak-to-trough) on the equity series.
 let peak=series[0].y, maxDDpct=0, maxDDusd=0;
 series.forEach(p=>{ if(p.y>peak) peak=p.y; const ddu=peak-p.y; const ddp=peak>0?ddu/peak*100:0; if(ddp>maxDDpct){maxDDpct=ddp; maxDDusd=ddu;} });
 const current=series[series.length-1].y;
 // Annualized Sharpe from daily equity returns (last equity value per UTC day).
 const byDay=new Map();
 series.forEach(p=>{ const dt=new Date(p.t); const key=dt.getUTCFullYear()+'-'+(dt.getUTCMonth()+1)+'-'+dt.getUTCDate(); byDay.set(key,p.y); });
 const dailyEq=[...byDay.values()];
 let sharpe=null, sharpeReady=false;
 if(dailyEq.length>=10){
   const rets=[]; let ok=true;
   for(let i=1;i<dailyEq.length;i++){ if(dailyEq[i-1]<=0){ok=false;break;} rets.push(dailyEq[i]/dailyEq[i-1]-1); }
   if(ok && rets.length>=8){
     const mean=rets.reduce((a,b)=>a+b,0)/rets.length;
     const variance=rets.reduce((a,b)=>a+(b-mean)*(b-mean),0)/(rets.length-1);
     const sd=Math.sqrt(variance);
     if(sd>0){ sharpe=(mean/sd)*Math.sqrt(365); sharpeReady=true; }
   }
 }
 set('eqMaxDD', '-'+num(maxDDpct,2)+'%'); setClass('eqMaxDD','val '+(maxDDpct>0?'bad':'muted')); set('eqMaxDDNote', `Peak-to-trough ${usd(maxDDusd)} on Larry equity`);
 set('eqSharpe', sharpeReady?num(sharpe,2):'Accumulating'); setClass('eqSharpe','val '+(sharpeReady?(sharpe>=1?'good':sharpe>=0?'warn':'bad'):'muted')); set('eqSharpeNote', sharpeReady?`Annualized from ${dailyEq.length} daily points`:`Needs ~10 daily points (have ${dailyEq.length})`);
 set('eqPeak', usd(peak)); set('eqCurrent', usd(current)); setClass('eqCurrent','val '+cls(current-base));
 // Draw the curve and enable the hover crosshair (equity + return % at the cursor).
 __eqSeries=series; __eqBase=base; __eqScale=__eqDrawBase(canvas); attachEquityHover(canvas);
 // Also surface max drawdown in the Attribution Drawdown tile (single source of truth).
 set('intelDrawdown', '-'+num(maxDDpct,2)+'%'); setClass('intelDrawdown','intel-val '+(maxDDpct>0?'bad':'muted')); set('intelDrawdownNote', `Max peak-to-trough ${usd(maxDDusd)} · Sharpe ${sharpeReady?num(sharpe,2):'accumulating'}. Larry strategy only.`);
}

function renderPerformanceAnalytics(d){
 const pa=d.performance_analytics||{}; const ts=pa.trade_stats||{}; const ex=pa.exposure_stats||{};
 set('paLarryEquity', pa.larry_equity_usd==null?'—':usd(pa.larry_equity_usd)); setClass('paLarryEquity','val '+cls(pa.larry_total_pnl_usd));
 set('paLarryNote', `Larry return ${pa.larry_return_pct==null?'—':pct(pa.larry_return_pct)}`);
 set('paBtcValue', pa.btc_benchmark_value_usd==null?'—':usd(pa.btc_benchmark_value_usd)); setClass('paBtcValue','val '+cls(pa.btc_benchmark_pnl_usd));
 set('paBtcNote', pa.synthetic_btc_holdings?`${num(pa.synthetic_btc_holdings,6)} synthetic BTC`:'Benchmark unavailable');
 set('paBtcReturn', pa.btc_benchmark_return_pct==null?'—':pct(pa.btc_benchmark_return_pct)); setClass('paBtcReturn','val '+cls(pa.btc_benchmark_return_pct));
 set('paBtcPriceNote', `Start ${pa.start_btc_price?usd(pa.start_btc_price):'—'} · Now ${pa.current_btc_price?usd(pa.current_btc_price):'—'}`);
 set('paAlpha', pa.alpha_pct==null?'—':pct(pa.alpha_pct)); setClass('paAlpha','val '+cls(pa.alpha_pct));
 set('paAlphaNote', `Excess P&L ${pa.alpha_usd==null?'—':usd(pa.alpha_usd)}`);
 set('paTrades', `${num(ts.realized_trades||0,0)} realized`);
 set('paWinRate', `Wins ${num(ts.winning_trades||0,0)} · Losses ${num(ts.losing_trades||0,0)} · Win rate ${ts.win_rate_pct==null?'—':pct(ts.win_rate_pct)}`);
 set('paProfitFactor', ts.profit_factor==null?'—':(ts.profit_factor>99?'∞':num(ts.profit_factor,2)));
 set('paExpectancy', `Expectancy ${ts.expectancy_usd==null?'—':usd(ts.expectancy_usd)}/trade`);
 set('paAvgWinLoss', `${ts.avg_winner_usd==null?'—':usd(ts.avg_winner_usd)} / ${ts.avg_loser_usd==null?'—':usd(ts.avg_loser_usd)}`);
 set('paExposure', `L ${ex.long_pct==null?'—':num(ex.long_pct,0)+'%'} · S ${ex.short_pct==null?'—':num(ex.short_pct,0)+'%'} · F ${ex.flat_pct==null?'—':num(ex.flat_pct,0)+'%'}`);
 set('paExposureNote', ex.total_hours?`${num(ex.total_hours,1)} tracked hours · ${ex.method||''}`:'Exposure starts after first ledger event');
 set('paMethodNote', pa.note||'Benchmark uses starting capital and BTC price.');
 const bars=$('paReturnBars'); if(bars){
   const vals=[['Larry', Number(pa.larry_return_pct||0)], ['BTC', Number(pa.btc_benchmark_return_pct||0)], ['Alpha', Number(pa.alpha_pct||0)]];
   const maxv=Math.max(1, ...vals.map(x=>Math.abs(x[1])));
   bars.innerHTML=vals.map(([label,val])=>{const w=Math.max(3, Math.min(100, Math.abs(val)/maxv*100)); const klass=val>0?'':(val<0?'neg':'neu'); return `<div class="pnl-bar-row"><div class="pnl-bar-label">${label}</div><div class="pnl-bar-track"><div class="pnl-bar-fill ${klass}" style="width:${w}%"></div></div><div class="pnl-bar-value ${cls(val)}">${pct(val)}</div></div>`}).join('');
 }
}

function renderLarryAccounting(d){
 const la=d.larry_trade_accounting||{};
 set('larryRealizedPnl', usd(la.net_realized_pnl_usd)); setClass('larryRealizedPnl','val '+cls(la.net_realized_pnl_usd));
 set('larryOpenUnrealized', usd(la.open_unrealized_pnl_usd)); setClass('larryOpenUnrealized','val '+cls(la.open_unrealized_pnl_usd));
 set('larryOpenNote', `${la.open_side||'FLAT'} ${num(la.open_contracts||0,0)} contracts`);
 set('larryTotalPnl', usd(la.net_total_pnl_usd)); setClass('larryTotalPnl','val '+cls(la.net_total_pnl_usd));
 const last=la.last_realized_trade||{};
 set('larryLastTradePnl', last.net_realized_pnl_usd!=null?usd(last.net_realized_pnl_usd):'—'); setClass('larryLastTradePnl','val '+cls(last.net_realized_pnl_usd));
 set('larryLastTradeNote', last.reason?`${last.reason} · ${last.action||''} ${num(last.contracts||0,0)} @ ${usd(last.fill_price)}`:'No realized exit yet');
 const body=$('larryTradeTapeBody');
 if(body){
   const rows=la.recent_trades||[];
   body.innerHTML=rows.length?rows.map(r=>{
     const net = r.net_realized_pnl_usd;
     const gross = r.gross_realized_pnl_usd;
     const intent = r.trade_intent || r.execution_reason || '—';
     const liq = r.liquidity || '—';
     const liqCls = String(liq).toUpperCase().startsWith('MAKER') ? 'good' : (String(liq).toUpperCase().startsWith('TAKER') ? 'warn' : 'muted');
     return `<tr><td>${r.timestamp_et||fmtET(r.timestamp)}</td><td>${intent}</td><td>${r.signal_reason||r.reason||'—'}</td><td>${r.action||'—'}</td><td>${num(r.contracts||0,0)}</td><td>${usd(r.fill_price)}</td><td class="${cls(gross)}">${gross==null?'—':usd(gross)}</td><td>${usd(r.fees_usd||0)}</td><td class="${cls(net)}"><strong>${net==null?'—':usd(net)}</strong></td><td>${r.slippage_bps==null?'—':num(r.slippage_bps,2)+' bps'}</td><td class="${liqCls}">${liq}</td></tr>`
   }).join(''):'<tr><td colspan="11" class="muted">No Larry ledger trades found yet.</td></tr>';
 }
 const bars=$('larryPnlBars');
 if(bars){
   const vals=[
     ['Realized', Number(la.net_realized_pnl_usd||0)],
     ['Open UPL', Number(la.open_unrealized_pnl_usd||0)],
     ['Total', Number(la.net_total_pnl_usd||0)]
   ];
   const maxv=Math.max(1, ...vals.map(x=>Math.abs(x[1])));
   bars.innerHTML=vals.map(([label,val])=>{
     const w=Math.max(3, Math.min(100, Math.abs(val)/maxv*100));
     const klass=val>0?'':(val<0?'neg':'neu');
     return `<div class="pnl-bar-row"><div class="pnl-bar-label">${label}</div><div class="pnl-bar-track"><div class="pnl-bar-fill ${klass}" style="width:${w}%"></div></div><div class="pnl-bar-value ${cls(val)}">${usd(val)}</div></div>`;
   }).join('');
 }
 set('larryAccountingNote', la.note || 'Larry ledger accounting active.');
 const eq=la.execution_quality||{};
 set('execAvgSlip', eq.avg_slippage_bps==null?'—':num(eq.avg_slippage_bps,2)+' bps');
 set('execSlipRange', `Best ${eq.best_slippage_bps==null?'—':num(eq.best_slippage_bps,2)+' bps'} · Worst ${eq.worst_slippage_bps==null?'—':num(eq.worst_slippage_bps,2)+' bps'}`);
 const maker=Number(eq.maker_count||0), taker=Number(eq.taker_count||0), sample=Number(eq.sample_trades||0);
 const makerPct=sample?maker/sample*100:null, takerPct=sample?taker/sample*100:null;
 set('execMakerTaker', `${makerPct==null?'—':num(makerPct,0)+'%'} / ${takerPct==null?'—':num(takerPct,0)+'%'}`);
 set('execMakerTakerNote', `${num(maker,0)} maker · ${num(taker,0)} taker · ${num(eq.unknown_count||0,0)} unknown`);
 set('execFeesPaid', usd(eq.fees_usd||0));
 const avgAbs = eq.avg_slippage_bps==null ? null : Math.abs(Number(eq.avg_slippage_bps));
 let score='—'; if(avgAbs!=null){ score = avgAbs<=2?'A':(avgAbs<=5?'B':(avgAbs<=10?'C':'D')); }
 set('execScore', score);
 set('execScoreNote', sample?`${num(sample,0)} successful Larry orders`:'No successful orders yet');
 set('execQualityNote', eq.note || 'Execution quality summarizes Larry trade fills.');
}

function renderLarryMindset(d){
 const es=d.engine_state||{}, diag=es.entry_diagnostics||es.last_entry_diagnostics||d.entry_diagnostics||{}, cfg=d.config||{}, mg=es.manual_position_status||{};
 const pr=d.position_risk||{}, pc=es.position_controls||{}, ph=es.phantom||es.signal_lifecycle||{};
 const pos=es.exchange_position||d.exchange_position||{};
 const ls=Number(diag.long_score||0), ss=Number(diag.short_score||0), best=Math.max(ls,ss);
 const direction=ls>ss?'LONG':ss>ls?'SHORT':(pr.side||pos.side||'NEUTRAL');
 const arm=Number(diag.signal_arm_score||cfg.SIGNAL_ARM_SCORE||2), commit=Number(diag.signal_commit_score||cfg.SIGNAL_COMMIT_SCORE||3);
 const contracts=Math.abs(Number(pr.contracts||pos.contracts||pos.number_of_contracts||0));
 const hasPosition=pr.has_position===true||contracts>0;
 const autoManaged=hasPosition && mg.bot_managed===true && mg.allow_bot_to_trade_position===true;
 const authority=$('managementAlert');
 if(authority) authority.className='management-alert '+(!hasPosition?'flat':autoManaged?'managed':'unmanaged');
 set('managementTitle',!hasPosition?'No open futures position':autoManaged?'Larry owns and manages this position':'Open position is monitor-only');
 set('managementNote',!hasPosition?'Coinbase is flat. Larry may enter when its strategy and risk gates permit.':autoManaged?'Automated ATR, trailing-stop, profit-taking and adaptive exits are authorized for this exact exchange position.':'Larry can display calculated risk levels but will not submit exits, additions or reversals. Use Coinbase or Emergency Close Futures to manage this exposure.');
 set('managementBadge',!hasPosition?'FLAT':autoManaged?'AUTO-MANAGED':'NOT MANAGED');
 const riskOk=(es.risk_gate||{}).entries_allowed!==false, macroOpen=diag.macro_gate_open!==false;
 const fundingOk=direction==='SHORT'?diag.funding_short_ok!==false:diag.funding_long_ok!==false;
 const lifecycle=String(ph.state||'MONITORING').toUpperCase();
 const maxN=Number(cfg.MAX_CONVICTION_CONTRACTS||10), probe=Math.max(1,Math.round(maxN*Number(cfg.PROBE_PCT||.2))), partial=Math.max(probe,Math.round(maxN*Number(cfg.PARTIAL_PCT||.4))), strong=Math.max(partial,Math.round(maxN*Number(cfg.STRONG_PCT||.7)));
 const target=hasPosition?contracts:(best>=4?maxN:best>=3?strong:best>=2?partial:best>=1?probe:0);
 const tier=target>=maxN?'FULL':target>=strong?'STRONG':target>=partial?'PARTIAL':target>0?'PROBE':'WAITING';
 let decision='WATCHING — waiting for a qualified setup', reason=diag.explanation||diag.next_action||'Larry is monitoring the next closed-candle signal.', badge='WATCHING', badgeClass='';
 let activeStep='mindsetTriggerStep';
 if(!riskOk){decision='BLOCKED — risk gate is closed';reason=(es.risk_gate||{}).reason||'New entries are disabled by the active risk gate.';badge='BLOCKED';badgeClass='bad';activeStep='mindsetRegimeStep';}
 else if(hasPosition){
   activeStep='mindsetExitStep';
   if(!autoManaged){decision=`UNMANAGED ${pr.side||direction} — monitor only`;reason='Larry does not have execution authority for this position. Any displayed stop or target is informational and will not execute.';badge='MONITOR ONLY';badgeClass='bad';}
   else if(pr.tsl_active){decision=`PROTECTING ${pr.side||direction} — trailing stop active`;reason=`Larry is managing ${num(contracts,0)} contracts with the trailing stop as the active exit.`;badge='PROTECTING';badgeClass='good';}
   else if(pc.tp1_done===true){decision=`HOLDING ${pr.side||direction} — TP step completed`;reason='The first profit-taking step is complete; Larry is waiting for trailing-stop activation or another exit condition.';badge='AUTO-MANAGED';badgeClass='good';}
   else {decision=`HOLDING ${pr.side||direction} — automatically managed`;reason=pr.status||'ATR protection is active while Larry watches the profit-taking thresholds.';badge='AUTO-MANAGED';badgeClass='good';}
 } else if(!macroOpen||!fundingOk){decision='FILTERED — setup cannot advance';reason=!macroOpen?(diag.macro_reason||'The macro gate is blocking this setup.'):(direction+' funding gate is not open.');badge='FILTERED';badgeClass='warn';activeStep='mindsetRegimeStep';}
 else if(lifecycle.includes('COMMITTED')||best>=commit){decision=`COMMITTED ${direction} — execution checks active`;reason=diag.next_action||'The signal reached commit strength; Larry is checking lifecycle and execution gates.';badge='COMMITTED';badgeClass='good';activeStep='mindsetConvictionStep';}
 else if(lifecycle==='PHANTOM_ARMED'||best>=arm){decision=`ARMED ${direction} — awaiting confirmation`;reason=ph.reason||diag.next_action||'The setup reached the arm threshold and is waiting for its closed-candle confirmation.';badge='ARMED';badgeClass='warn';activeStep='mindsetTriggerStep';}
 set('mindsetDecision',decision); set('mindsetReason',String(reason).replaceAll('_',' ').slice(0,260)); set('mindsetBadge',badge); setClass('mindsetBadge','mindset-badge '+badgeClass);
 set('mindsetRegime',`${macroOpen?'Macro open':'Macro blocked'} · ${fundingOk?'Funding OK':'Funding check'}`); set('mindsetRegimeNote',riskOk?'Risk gate allows entries':'Risk gate blocks entries');
 set('mindsetTrigger',`LONG ${ls}/4 · SHORT ${ss}/4`); set('mindsetTriggerNote',`Arm ${arm}/4 · commit ${commit}/4 · ${String(diag.next_action||'waiting').replaceAll('_',' ')}`);
 set('mindsetConviction',`${tier} · target ${num(target,0)}`); set('mindsetConvictionNote',`Scale: probe ${probe} · partial ${partial} · strong ${strong} · full ${maxN}`);
 set('mindsetPosition',hasPosition?`${pr.side||direction} · ${num(contracts,0)} contracts`:'FLAT · no position'); set('mindsetPositionNote',hasPosition?(es.add_on_state&&es.add_on_state.adds_count?`${es.add_on_state.adds_count}/${cfg.MAX_POSITION_ADDS||3} progressive adds used`:'No progressive add recorded'):'Waiting for a committed entry');
 const exitValue=!hasPosition?'Not armed':!autoManaged?'DISPLAY ONLY — NO AUTO EXIT':pr.tsl_active?'Trailing stop active':pc.tp1_done===true?'TP step done':'ATR stop active';
 set('mindsetExit',exitValue); set('mindsetExitNote',hasPosition?(!autoManaged?`Calculated stop ${pr.active_stop?usd(pr.active_stop):'—'} · Larry will not execute it`:`Executable stop ${pr.active_stop?usd(pr.active_stop):'—'} · trail activates ${pr.tsl_activation_price?usd(pr.tsl_activation_price):'—'}`):'Exit levels initialize at entry');
 ['mindsetRegimeStep','mindsetTriggerStep','mindsetConvictionStep','mindsetPositionStep','mindsetExitStep'].forEach(id=>{const e=$(id);if(e)e.className='mindset-step';});
 const order=['mindsetRegimeStep','mindsetTriggerStep','mindsetConvictionStep','mindsetPositionStep','mindsetExitStep'], ai=order.indexOf(activeStep);
 order.forEach((id,i)=>{const e=$(id);if(!e)return;if(i<ai)e.classList.add('done');else if(i===ai)e.classList.add('active');});
 const uplPct=Number(pr.unrealized_pnl_pct||0), tslPct=Number(pr.tsl_activation_pct||cfg.TSL_ACTIVATION_PCT||.015)*100;
 let tpPct=Number(cfg.TP1_PCT||.0075)*100; if(contracts>=maxN)tpPct=Number(cfg.TP1_FULL_TRIGGER_PCT||.005)*100;else if(contracts>=strong)tpPct=Number(cfg.TP1_STRONG_TRIGGER_PCT||.006)*100;else tpPct=Number(cfg.TP1_PROBE_TRIGGER_PCT||.0075)*100;
 const liveEntry=Number(pr.entry_price||pos.avg_entry_price||0), liveTp=Number(pc.tp1_trigger_price||0); if(hasPosition&&liveEntry&&liveTp)tpPct=Math.abs(liveTp/liveEntry-1)*100;
 const defense=pc.adaptive_defense||{}, ds=Number(defense.score||0), ev=(defense.evidence||[]).map(x=>String(x.factor||'').replaceAll('_',' '));
 set('defenseState',`${String(defense.state||'FLAT').replaceAll('_',' ')} / ${num(ds,0)}/100`); set('defenseEvidence',ev.length?ev.slice(0,2).join(' / '):'No adverse evidence active');
 const db=$('defenseBar'); if(db)db.style.width=Math.max(0,Math.min(100,ds))+'%';
 const reanchor=pc.position_reanchor||{}; set('positionVersion',`Version ${pc.position_version||'--'}${reanchor.verified?' verified':''}`); set('positionAnchor',hasPosition?`Avg ${usd(reanchor.exchange_avg_entry||pr.entry_price)} / ATR ${usd(reanchor.locked_atr)}`:'Exchange average not active');
 const ms=es.market_structure||{}, hi=(ms.last_swing_high||{}).price, lo=(ms.last_swing_low||{}).price; set('pivotStructure',String(ms.structure||'UNCLASSIFIED').replaceAll('_',' ')); set('pivotLevels',`Low ${lo?usd(lo):'--'} / High ${hi?usd(hi):'--'} / shadow`);
 const sb=es.stop_blown||{}, scores=sb.scores||{}; set('stopBlownState',sb.active?`${sb.leader||'OBSERVING'} / SHADOW`:'Inactive / shadow'); set('stopBlownScores',sb.active?`Fish ${num((scores.FISHED||0)*100,0)}% / Save ${num((scores.SAVED||0)*100,0)}% / Extreme ${num((scores.EXTREME||0)*100,0)}%`:'SB1-SB5 observer cannot trade');
 const progress=hasPosition?(pr.tsl_active?100:Math.max(0,Math.min(100,tslPct?uplPct/tslPct*100:0))):0;
 const fill=$('mindsetProfitFill'), marker=$('mindsetTpMarker'); if(fill)fill.style.width=progress+'%'; if(marker)marker.style.left=Math.max(0,Math.min(100,tslPct?tpPct/tslPct*100:50))+'%';
 set('mindsetProfitValue',hasPosition?`${uplPct>=0?'+':''}${num(uplPct,2)}% · ${pc.tp1_done?'TP passed':pr.tsl_active?'trail active':'building protection'}`:'Flat / waiting');
 set('mindsetTpLabel',`TP ${num(tpPct,2)}%${pc.tp1_done?' ✓':''}`); set('mindsetTrailLabel',`Trail ${num(tslPct,2)}%${pr.tsl_active?' ✓':''}`);
}

function renderEntryDiagnostics(d){
 const es=d.engine_state||{}; const diag=es.entry_diagnostics || es.last_entry_diagnostics || d.entry_diagnostics || {}; const rp=es.last_reversal_probe_check||{};
 if(!diag || Object.keys(diag).length===0){
   set('diagScores','No diagnostics yet'); setClass('diagScores','v12-val warn');
   set('diagScoreNote','Deploy clean v19 bot or wait one cycle.');
   return;
 }
 const ls=Number(diag.long_score||0), ss=Number(diag.short_score||0), arm=Number(diag.signal_arm_score||2), commit=Number(diag.signal_commit_score||3);
 set('diagScores',`LONG ${ls}/4 · SHORT ${ss}/4`); setClass('diagScores','v12-val '+((ls>=arm||ss>=arm)?'good':'warn'));
 set('diagScoreNote',`Arm at ${arm}/4 · Commit at ${commit}/4 · checked ${fmtET(diag.checked_at)}`);
 set('diagLongStatus', ls>=arm?'Can arm':'Waiting'); setClass('diagLongStatus','v12-val '+(ls>=arm?'good':'warn'));
 set('diagLongMissing',`${condList(diag.long_conditions)} · Need +${diag.core_long_arm_gap ?? '—'} to arm / +${diag.core_long_commit_gap ?? '—'} to commit`);
 set('diagShortStatus', ss>=arm?'Can arm':'Waiting'); setClass('diagShortStatus','v12-val '+(ss>=arm?'good':'warn'));
 set('diagShortMissing',`${condList(diag.short_conditions)} · Need +${diag.core_short_arm_gap ?? '—'} to arm / +${diag.core_short_commit_gap ?? '—'} to commit`);
 const prDir=diag.reversal_probe_direction || rp.direction || null;
 set('diagProbeStatus', prDir?`${prDir} probe can arm`:'Not qualified'); setClass('diagProbeStatus','v12-val '+(prDir?'good':'warn'));
 set('diagProbeReason', (diag.reversal_probe_reason || rp.reason || '—').slice(0,260));
 const macroOpen=diag.macro_gate_open===true; const longFund=diag.funding_long_ok!==false; const shortFund=diag.funding_short_ok!==false;
 set('diagGates',`${macroOpen?'Macro open':'Macro blocked'} · F:${longFund&&shortFund?'OK':'Check'}`); setClass('diagGates','v12-val '+(macroOpen?'good':'warn'));
 set('diagGatesNote',`${diag.macro_reason||''} · Long funding: ${diag.funding_long_reason||'OK'} · Short funding: ${diag.funding_short_reason||'OK'}`.slice(0,260));
 set('diagNextAction', (diag.next_action||'—').replaceAll('_',' ')); setClass('diagNextAction','v12-val '+((diag.next_action||'').includes('CAN')?'good':'warn'));
 const blockers=(diag.blockers||[]).length ? `Blockers: ${(diag.blockers||[]).join(', ')}` : 'No hard blockers; waiting for score/probe/confirmation.';
 set('diagNextNote', blockers.slice(0,220));
 set('diagFinalLongScore', `LONG ${ls}/4`); setClass('diagFinalLongScore','value '+(ls>=commit?'good':ls>=arm?'warn':'muted'));
 set('diagFinalLongNote', `Arm ${arm}/4 · Commit ${commit}/4 · ${diag.core_long_arm_gap==null?'':('needs +'+diag.core_long_arm_gap+' to arm')}`);
 set('diagFinalShortScore', `SHORT ${ss}/4`); setClass('diagFinalShortScore','value '+(ss>=commit?'bad':ss>=arm?'warn':'muted'));
 set('diagFinalShortNote', `Arm ${arm}/4 · Commit ${commit}/4 · ${diag.core_short_arm_gap==null?'':('needs +'+diag.core_short_arm_gap+' to arm')}`);
 // v67: make current-vs-historical signal state explicit. Do not let old sizing/order state read as a live trigger.
 const sd = es.last_core_sizing_decision || (es.last_core_target_plan && es.last_core_target_plan.sizing_decision) || {};
 const le = es.last_execution_result || es.last_completed_trade || es.last_realized_trade || {};
 const phTruth = es.phantom || es.signal_lifecycle || {};
 const riskOk = (es.risk_gate||{}).entries_allowed !== false;
 const fundingOk = (diag.funding_long_ok !== false && diag.funding_short_ok !== false);
 const position = es.exchange_position || {};
 const currentIsLive = (Math.max(ls, ss) >= commit) || prDir || String(phTruth.state||'').includes('COMMITTED');
 set('currentSignalTruth', `LONG ${ls}/4 · SHORT ${ss}/4`);
 setClass('currentSignalTruth','value '+(currentIsLive?'good':'muted'));
 set('currentSignalTruthNote', `Current cycle checked ${fmtET(diag.checked_at)} · ${diag.next_action||'WAITING'}`);
 const sdSig = sd.signal || '—';
 const sdScore = sd.score==null ? '—' : `${sd.score}/4`;
 set('lastSizingTruth', `${sdSig} ${sdScore}`);
 setClass('lastSizingTruth','value muted');
 set('lastSizingTruthNote', sd.reason ? `Historical sizing: ${sd.reason} · target ${sd.target_abs_contracts ?? sd.final_contracts ?? '—'} · not live` : 'No prior sizing decision recorded');
 const lePlan = le.plan || {};
 const leReason = le.reason || lePlan.explanation || '—';
 const leAction = lePlan.action || (le.order && le.order.client_order_id) || '—';
 set('lastExecutionTruth', leReason==='—'?'—':`${leAction} · ${leReason}`);
 setClass('lastExecutionTruth','value muted');
 set('lastExecutionTruthNote', le.ok===true ? `Historical execution/fill. Current exchange: ${position.side||'FLAT'} ${num(position.contracts||0,0)}` : 'No current order result; historical only');
 let liveTxt='NO LIVE TRIGGER'; let liveNote='Waiting for score/probe confirmation.'; let liveClass='muted';
 if(!riskOk){ liveTxt='BLOCKED'; liveNote=(es.risk_gate||{}).reason || 'Risk gate blocked'; liveClass='bad'; }
 else if(!fundingOk){ liveTxt='FUNDING BLOCK'; liveNote='Funding gate is not open for one side.'; liveClass='warn'; }
 else if(String(phTruth.state||'')==='PHANTOM_ARMED'){ liveTxt='ARMED'; liveNote=`${phTruth.direction||''} armed; awaiting confirmation/candle close.`; liveClass='warn'; }
 else if(String(phTruth.state||'').includes('COMMITTED') || Math.max(ls,ss)>=commit || prDir){ liveTxt='LIVE SETUP'; liveNote=diag.next_action || 'Lifecycle checks are active.'; liveClass='good'; }
 set('liveTriggerTruth', liveTxt); setClass('liveTriggerTruth','value '+liveClass);
 set('liveTriggerTruthNote', liveNote);
 const opp=$('opportunityBanner');
 const rpReason=(diag.reversal_probe_reason || rp.reason || '');
 const reasonText=String(rpReason||'');
 const lc=diag.long_conditions||{}; const sc=diag.short_conditions||{};
 const nearLower = reasonText.includes('near_lower=True') || lc.bb_lower===true;
 const nearUpper = reasonText.includes('near_upper=True') || sc.bb_upper===true;
 const softRsiLong = reasonText.includes('soft_rsi_long=True') || lc.rsi_oversold===true;
 const softRsiShort = reasonText.includes('soft_rsi_short=True') || sc.rsi_overbought===true;
 const stochLong = reasonText.includes('stoch_long=True') || lc.stoch_oversold===true;
 const stochShort = reasonText.includes('stoch_short=True') || sc.stoch_overbought===true;
 const volSpike = lc.volume_spike===true || sc.volume_spike===true;
 const ph=es.phantom||es.signal_lifecycle||{};
 const phState=String(ph.state||'MONITORING');
 let oppPoints=0;
 if(nearLower||nearUpper) oppPoints+=1;
 if(softRsiLong||softRsiShort) oppPoints+=1;
 if(stochLong||stochShort) oppPoints+=1;
 if(volSpike) oppPoints+=1;
 if(phState==='PHANTOM_ARMED') oppPoints+=2;
 if(Math.max(ls,ss)>=commit) oppPoints+=2;
 let level='LOW', levelClass='low';
 if(oppPoints>=6){ level='EXTREME'; levelClass='extreme'; }
 else if(oppPoints>=4){ level='HIGH'; levelClass='high'; }
 else if(oppPoints>=2){ level='MEDIUM'; levelClass='medium'; }
 set('oppLevel', `${level} (${oppPoints} pts)`); setClass('oppLevel','v12-val '+(levelClass==='extreme'||levelClass==='high'?'good':levelClass==='medium'?'warn':'muted'));
 const dots=$('oppMeter'); if(dots){ [...dots.children].forEach((el,i)=>{ const on=(level==='LOW'?i<1:level==='MEDIUM'?i<2:level==='HIGH'?i<3:i<4); el.className='opp-dot '+(on?'on '+levelClass:''); }); }
 const checks=[
   ['Near lower BB', nearLower], ['Near upper BB', nearUpper], ['RSI soft/oversold', softRsiLong||softRsiShort], ['Stoch confirm', stochLong||stochShort], ['Volume spike', volSpike], ['Probe armed', phState==='PHANTOM_ARMED']
 ];
 const missingLong=[];
 if(!nearLower) missingLong.push('near lower BB');
 if(!softRsiLong) missingLong.push('soft RSI');
 if(!stochLong) missingLong.push('Stoch oversold');
 let expected='Keep monitoring';
 if(phState==='PHANTOM_ARMED') expected='Await candle close confirmation; then execute locked probe if still valid';
 else if(prDir) expected=`Arm ${prDir} reversal probe`;
 else if(ls>=commit) expected='Core LONG can commit if lifecycle/risk checks pass';
 else if(ss>=commit) expected='Core SHORT can commit if lifecycle/risk checks pass';
 else if(nearLower) expected=`Watch LONG reversal: need ${missingLong.slice(0,2).join(' + ') || 'confirmation'}${missingLong.length>2?'…':''}`;
 else if(nearUpper) expected='Watch SHORT reversal: need RSI/Stoch confirmation';
 set('oppNote', `Expected next action: ${expected}`);
 const checkHtml='<div class="checkline">'+checks.map(([name,ok])=>`<span class="checkpill ${ok?'yes':'no'}">${ok?'✓':'×'} ${name}</span>`).join('')+'</div>';
 if(opp){
   const blockers=(diag.blockers||[]);
   let msg=`Opportunity Level: ${level}. Final score LONG ${ls}/4, SHORT ${ss}/4. Expected next action: ${expected}.`;
   if(prDir) msg += ` ${prDir} reversal probe is qualified.`;
   else if(blockers.length) msg += ` Blockers: ${blockers.join(', ')}.`;
   else msg += ` ${diag.explanation || rpReason || 'Waiting for score, reversal probe, or confirmation.'}`;
   opp.className='opportunity-banner '+levelClass+(prDir||phState==='PHANTOM_ARMED'?' ok':'');
   // v73 fix: msg is assembled from engine-side diagnostic strings (blockers,
   // explanation). checkHtml has no dynamic data in it (static labels only), so it's
   // still safe as a template; msg gets its own element via textContent so nothing in
   // a future diagnostic string can ever be parsed as markup.
   opp.innerHTML='';
   const msgDiv=document.createElement('div'); const msgB=document.createElement('b');
   msgB.textContent=msg.slice(0,360); msgDiv.appendChild(msgB); opp.appendChild(msgDiv);
   const checksWrap=document.createElement('div'); checksWrap.innerHTML=checkHtml;
   opp.appendChild(checksWrap);
 }
 set('diagExplanation', diag.explanation || `Opportunity ${level}: ${expected}`);
}

function renderKillSwitch(h){
 const box=$('haltStatusBox'); if(!box) return;
 h = h || {};
 const isOn = h.halt === true;
 box.className = 'halt-status ' + (isOn ? 'on' : 'off');
 box.textContent = isOn ? '🛑 HALTED — order placement disabled' : '🟢 LIVE — order placement allowed';
 set('haltReason', h.reason || (isOn ? 'operator halt active' : 'not halted'));
 set('haltSetBy', ((h.set_by || '—') + (h.set_at ? ' · ' + h.set_at : '')));
}
async function setKillSwitch(on){
 const reason = on ? prompt('Reason for HALT?', 'operator_halt_from_dashboard') : '';
 if(on && reason === null) return;
 const url = on ? '/api/halt' : '/api/resume';
 const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({reason: reason || '', set_by:'dashboard'})});
 const d = await r.json();
 if(!d.ok){ alert('Kill switch update failed: ' + (d.error || 'unknown')); return; }
 renderKillSwitch(d.halt_state);
 setTimeout(refresh, 700);
}

async function emergencyFlatten(){
 const msg='EMERGENCY CLOSE FUTURES will HALT Larry and request the VM bot to flatten ALL live Coinbase futures exposure -- including manually-entered positions, not just bot-managed ones. Trading keys stay on the VM. Type FLATTEN to continue.';
 const confirmText = prompt(msg, '');
 if(confirmText !== 'FLATTEN') return;
 const pin = prompt('Enter Emergency PIN to request flatten:', '');
 if(!pin) return;
 const really = confirm('Final confirmation: submit PIN-authorized emergency flatten request and halt Larry?');
 if(!really) return;
 try{
   const r = await fetch('/api/emergency_flatten', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm:'FLATTEN', pin})});
   let d={};
   try{ d = await r.json(); }catch(_){ d = {ok:false, error:'Non-JSON response from dashboard'}; }
   if(!d.ok){
     alert('🚨 EMERGENCY FLATTEN REQUEST FAILED\n' + (d.error || d.message || 'unknown'));
     setTimeout(refresh, 700);
     return;
   }
   alert('✅ ' + (d.message||'Emergency flatten request accepted') + '\nRequest: ' + (d.request_id||'—') + '\nPosition hint: ' + JSON.stringify(d.position_hint||{}));
   setTimeout(refresh, 1500);
 }catch(e){ alert('🚨 Emergency flatten request failed before dashboard response: ' + e); }
}

async function saveStrategyControls(){
 const val=id=>$(id)?$(id).value:null;
 const tslActPct=parseFloat(val('ctrlTSLAct')||0);
 const tslTrailPct=parseFloat(val('ctrlTSLTrail')||0);
 if(isNaN(tslActPct)||tslActPct<0.10||tslActPct>50){ set('controlStatus','TSL Activation must be between 0.10% and 50.00%'); return; }
 if(isNaN(tslTrailPct)||tslTrailPct<0.05||tslTrailPct>25){ set('controlStatus','TSL Trail must be between 0.05% and 25.00%'); return; }
 const maxConviction=parseInt(val('ctrlMaxConviction')||10);
 const probePct=parseFloat(val('ctrlProbePct')||20);
 const partialPct=parseFloat(val('ctrlPartialPct')||40);
 const strongPct=parseFloat(val('ctrlStrongPct')||70);
 const probeContracts=Math.max(1, Math.min(maxConviction, Math.round(maxConviction*(probePct/100))));
 const partialContracts=Math.max(probeContracts, Math.min(maxConviction, Math.round(maxConviction*(partialPct/100))));
 const strongContracts=Math.max(partialContracts, Math.min(maxConviction, Math.round(maxConviction*(strongPct/100))));
 const maxAdds=parseInt(val('ctrlMaxAdds')||3);
 const signalValidity=parseInt(val('ctrlSignalValidity')||20);
 const signalCancel=parseInt(val('ctrlSignalCancel')||1);
 if(!Number.isFinite(maxConviction)||maxConviction<1||maxConviction>50){ set('controlStatus','Max Conviction Contracts must be between 1 and 50.'); return; }
 if(!Number.isFinite(probePct)||probePct<5||probePct>100){ set('controlStatus','Probe % must be between 5% and 100%.'); return; }
 if(!Number.isFinite(partialPct)||partialPct<5||partialPct>100){ set('controlStatus','Partial % must be between 5% and 100%.'); return; }
 if(!Number.isFinite(strongPct)||strongPct<5||strongPct>100){ set('controlStatus','Strong % must be between 5% and 100%.'); return; }
 if(!(probePct<=partialPct && partialPct<=strongPct)){ set('controlStatus','Sizing percentages must be ordered: Probe ≤ Partial ≤ Strong.'); return; }
 if(!Number.isFinite(maxAdds)||maxAdds<0||maxAdds>10){ set('controlStatus','Max Adds / Position must be between 0 and 10.'); return; }
 if(!Number.isFinite(signalValidity)||signalValidity<1||signalValidity>120){ set('controlStatus','Signal Lock / Reversal Probe Minutes must be between 1 and 120.'); return; }
 if(!Number.isFinite(signalCancel)||signalCancel<0||signalCancel>4){ set('controlStatus','Cancel Score must be between 0 and 4.'); return; }
 const payload={
   TSL_ACTIVATION_PCT: tslActPct/100,
   TSL_TRAIL_PCT: tslTrailPct/100,
   ATR_STOP_MULTIPLIER: parseFloat(val('ctrlATR')||1.5),
   PHANTOM_EXTENSION_PCT: parseFloat(val('ctrlPhantom')||0)/100,
   MAX_CONVICTION_CONTRACTS: maxConviction,
   PROBE_PCT: probePct/100,
   PARTIAL_PCT: partialPct/100,
   STRONG_PCT: strongPct/100,
   CONTRACTS_PER_TRADE_FULL: maxConviction,
   CONTRACTS_PER_TRADE: probeContracts,
   CONTRACTS_PER_TRADE_PROBE: probeContracts,
   CONTRACTS_PER_TRADE_PARTIAL: partialContracts,
   STRONG_CONTRACTS: strongContracts,
   MACRO_BLOCKED_PROBE_CONTRACTS: probeContracts,
   REVERSAL_PROBE_CONTRACTS: probeContracts,
   MAX_POSITION_ADDS: maxAdds,
   SIGNAL_LOCK_ENABLED: String(val('ctrlSignalLock')||'true')==='true',
   SIGNAL_VALIDITY_MINUTES: signalValidity,
   SIGNAL_CANCEL_SCORE: signalCancel,
   SIGNAL_HYSTERESIS_ARM_SCORE: 3,
   SIGNAL_COMMIT_ON_CLOSED_CANDLE: true,
   FREEZE_CONFIDENCE_ON_ARM: true,
   SEND_EMAIL: String(val('ctrlEmail')||'true')==='true',
   SEND_TELEGRAM: String(val('ctrlTelegram')||'true')==='true',
   TELEGRAM_INCLUDE_ERRORS: String(val('ctrlTelegramErrors')||'true')==='true',
   TELEGRAM_DAILY_SUMMARY_ENABLED: String(val('ctrlTelegramDaily')||'true')==='true',
   TELEGRAM_DAILY_SUMMARY_HOUR_ET: parseInt(val('ctrlTelegramHour')||21),
   MAX_EFFECTIVE_LEVERAGE: parseFloat(val('ctrlMaxLev')||3),
   MIN_FUTURES_EQUITY_BUFFER_USD: parseFloat(val('ctrlBuffer')||1000),
   SPOT_ENTRY_COOLDOWN_SEC: parseInt(val('ctrlCooldown')||300),
   PERP_ENTRY_COOLDOWN_SEC: parseInt(val('ctrlCooldown')||300),
   BRIDGE_ENTRY_COOLDOWN_SEC: parseInt(val('ctrlCooldown')||300),
   MIN_ENTRY_COOLDOWN_SECONDS: parseInt(val('ctrlCooldown')||300),
   SPOT_TRANCHE_TARGETS_PCT: String(val('ctrlTranches')||'25,33,50,90').split(',').map(x=>parseFloat(x.trim())).filter(x=>!isNaN(x)),
 };
 const r=await fetch('/api/strategy_config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
 const d=await r.json();
 set('controlStatus', d.ok?'Saved. Bot will reload on next cycle.':'Save failed: '+(d.error||'unknown'));
 updateSizingPreview(payload);
 setTimeout(refresh,800);
}
async function toggleSpot(enabled){
 try{
  const r=await fetch('/api/spot_toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
  const d=await r.json();
  set('spotToggleStatus', d.ok ? (enabled?'Spot BTC ENABLED. Bot reloads next cycle.':'Spot BTC DISABLED. Perp-only focus active.') : ('Spot toggle failed: '+(d.error||'unknown')));
  setTimeout(refresh,800);
 }catch(e){ set('spotToggleStatus','Spot toggle failed: '+e); }
}
function renderSpotToggleStatus(cfg){
 const spot=!!(cfg && cfg.ENABLE_SPOT_BTC_TRADING);
 const bridge=!!(cfg && cfg.ENABLE_SPOT_BRIDGE_PERP_BUYS);
 pill('spotTradingPill', spot?'SPOT BTC ENABLED':'SPOT BTC DISABLED', spot?'warn':'good');
 pill('spotBridgePill', bridge?'SPOT→PERP BRIDGE ON':'SPOT→PERP BRIDGE OFF', bridge?'warn':'good');
}
async function refresh(){try{const r=await fetch('/api/data?ts='+Date.now(),{cache:'no-store'}); const d=await r.json(); window.__LAST_DASH_DATA=d; if(!d.ok){$('errorBox').style.display='block';$('errorBox').textContent=d.error+'\n'+(d.trace||'');return} $('errorBox').style.display='none'; warn(d.warnings||[]); const c=d.capital||{}, pnl=d.pnl_summary||{}, sb=d.spot_balance||{}, fb=d.futures_balance||{}, cfg=d.config||{}, hb=d.heartbeat||{}, pm=d.perp_product||{}, mac=d.macro||{}, fills=d.fills||{}, perpMon=d.perp_signal_monitor||{};
 set('btcPrice',d.price?'$'+num(d.price,2):'—');
 // v78: global data-freshness in the header — is the feed live? (was buried in System Health)
 const _age=((d.risk_intelligence||{}).health||{}).heartbeat_age_secs; const _fresh=(_age==null)?'':' · feed '+(_age<=120?'●':'○')+' '+_age+'s';
 set('serverTime',(d.server_time||'—')+_fresh); set('dashboardRefresh','Dashboard refreshed '+new Date().toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',second:'2-digit'})); set('productLine',`${cfg.SPOT_PRODUCT_ID||'BTC-USDC'} · ${cfg.PERP_PRODUCT_ID||'BIP-20DEC30-CDE'}`);
 const es=d.engine_state||{}, xp=es.exchange_position||{}, fp=(d.futures_positions&&d.futures_positions[0])||{};
 const hbState=String(hb.state||'UNKNOWN').toUpperCase();
 const botLoopError = hbState==='ERROR';
 const botLabel = 'BOT '+(hb.health||'—')+(botLoopError?' · LOOP ERROR':'');
 pill('botHealth', botLabel, botLoopError?'bad':(hb.color==='good'?'good':hb.color==='warn'?'warn':'bad'));
 const posSide=(xp.side||fp.side||'FLAT'); const posContracts=(xp.contracts ?? fp.contracts ?? 0);
 pill('botState', posSide==='FLAT'?'POSITION FLAT':`POSITION ${posSide} ${posContracts}`, posSide==='FLAT'?'good':'warn');
 const macroLabel=(mac.regime||'MACRO')+' · '+(mac.gate_open?'MACRO OPEN':'MACRO BLOCK');
 pill('macroPill', macroLabel, mac.gate_open===true?'good':mac.regime==='UNKNOWN'?'warn':'bad');
 pill('sessionPill',pm.session_open===false?'EXCHANGE CLOSED':pm.session_open===true?'EXCHANGE OPEN':'EXCHANGE —',pm.session_open===false?'warn':'good');
 const larryTop=d.larry_trade_accounting||{}; const cleanTopPnl=Number(larryTop.net_total_pnl_usd||0); const cleanTopReturn=(c.baseline_set&&c.starting_combined_capital)?(cleanTopPnl/c.starting_combined_capital*100):null; set('startingCapital',usd(c.starting_combined_capital)); set('baselineSource',(c.baseline_set?('Source: '+(c.baseline_source||'set')+(c.started_at?' · '+c.started_at:'')):'Not set — use /api/set_capital_baseline')); set('currentCapital',usd((c.starting_combined_capital||0)+cleanTopPnl)); set('currentCapitalMini',`Baseline ${usd(c.starting_combined_capital)} + Larry total P&L ${usd(cleanTopPnl)}`); set('netPnl',c.baseline_set?usd(cleanTopPnl):'Baseline not set'); setClass('netPnl','val '+(c.baseline_set?cls(cleanTopPnl):'warn')); set('netReturn',(c.baseline_set&&cleanTopReturn!=null)?pct(cleanTopReturn):'Baseline not set'); setClass('netReturn','val '+(c.baseline_set?cls(cleanTopReturn):'warn'));
 set('spotValue',usd(sb.SPOT_TREASURY_VALUE)); set('spotUsd',usd(sb.USD)); set('spotUsdc',usd(sb.USDC)); set('spotBtcUsd',usd(sb.BTC_USD_VALUE)); set('spotBtcAmt',num(sb.BTC,8)+' BTC'); set('spotAccountCount',(sb.raw_count??'—')); set('spotSource',sb.error?('Source: '+(sb.source||'—')+' · '+sb.error):('Source: '+(sb.source||'—')));
 set('futEquity',usd(fb.total_usd_balance)); set('availMargin',usd(fb.available_margin)); set('buyingPower',usd(fb.futures_buying_power)); set('initMargin',usd(fb.initial_margin)); set('liqBuffer',usd(fb.liquidation_buffer_amount));
 set('bookAvg',pnl.book_avg_entry_price?('$'+num(pnl.book_avg_entry_price,2)):'—'); set('bookContracts',pnl.book_contracts!=null?num(pnl.book_contracts,0):'—'); set('bookUnr',usd(pnl.book_unrealized_pnl)); setClass('bookUnr','v '+cls(pnl.book_unrealized_pnl)); set('realizedReset',usd(pnl.realized_since_reset)); setClass('realizedReset','v '+cls(pnl.realized_since_reset)); set('feesReset',usd(pnl.fees_since_reset)); setClass('feesReset','v '+(pnl.fees_since_reset>0?'bad':'')); set('netBookImpact',usd(pnl.net_book_impact)); setClass('netBookImpact','v '+cls(pnl.net_book_impact)); set('tradePnlNote',pnl.method_note||'—'); set('fillCount',(pnl.new_fill_count||0)+' fills'); set('ignoredFillCount',(pnl.ignored_before_tracking_start||0)+' ignored');
 renderSpotToggleStatus(cfg); initTradeMapControls(); renderPerformanceAnalytics(d); renderTradeMap(d); renderLarryAccounting(d); renderSignals(d.signals); renderBTCTriggerMap(d); renderV12Transparency(d); renderRiskIntelligence(d); renderSignalLifecycle(d); renderLarryMindset(d); renderEntryDiagnostics(d); renderKillSwitch(d.halt_state || (d.engine_state&&d.engine_state.kill_switch) || {}); renderPerpMonitor(perpMon,d); renderPositionRisk(d.position_risk); renderIAF(d.iaf_engine); renderPortfolio(d.portfolio_overlay); renderReconciler(d.position_reconciler); renderHistory(d.signal_history); renderMacro(mac,d.risk_gate); renderEquityCurve(d);
 // v76 fix: this note previously said "Risk gate status loading…" forever -- no code ever populated it.
 const _rg=d.risk_gate||{}; set('larryRiskGateNote', _rg.entries_allowed===false ? ((_rg.headline||'Risk gate blocked')+' — '+(_rg.reason||'see entry diagnostics')) : ('Risk gate open — daily stop hits '+(_rg.daily_stop_hits??0)+', loss streak '+(_rg.loss_streak??0)+'.'));
 set('atrStop',(cfg.ATR_STOP_MULTIPLIER||1.5)+'x ATR'); set('tslAct',pct((cfg.TSL_ACTIVATION_PCT||0)*100)); set('tslTrail',pct((cfg.TSL_TRAIL_PCT||0)*100)); set('configSource',cfg.CONFIG_SOURCE||'—'); setControlValues(cfg);
 const pos=d.futures_positions||[]; $('perpPosBody').innerHTML=pos.length?pos.map(p=>`<tr><td class="${p.side==='LONG'?'good':'bad'}">${p.side}</td><td>${num(p.contracts,0)}</td><td>${num(p.btc_exposure,4)} BTC</td><td>$${num(p.avg_entry_price,2)}</td><td>$${num(p.current_price,2)}</td><td class="${cls(p.book_unrealized_pnl ?? p.exchange_unrealized_pnl)}">${usd(p.book_unrealized_pnl ?? p.exchange_unrealized_pnl)}</td><td>${p.cost_basis_source||'coinbase_api'}</td></tr>`).join(''):'<tr><td colspan="7" class="muted">No open futures position</td></tr>';
 // v60: deprecated Perp Strategy Stack and Spot Entry Ladder cards removed from the main mobile view.
 // Current live exposure is shown only from Coinbase live futures_positions / exchange_position.
 // Spot trading is disabled and spot ladder diagnostics are intentionally hidden to avoid stale/empty panels.
 const cleanBookValue = `${num(pnl.book_contracts||0,0)} contracts @ $${num(pnl.book_avg_entry_price,2)}`;
 $('acctBody').innerHTML=`<tr><td>Spot Treasury</td><td>${usd(sb.SPOT_TREASURY_VALUE)}</td><td>—</td><td>${usd(sb.BTC_USD_VALUE)} BTC mark value</td><td>—</td><td>—</td><td>Spot cash/USDC/BTC only</td></tr><tr><td><strong>Clean Internal Perp Book</strong></td><td>${cleanBookValue}</td><td class="${cls(pnl.realized_since_reset)}">${usd(pnl.realized_since_reset)}</td><td class="${cls(pnl.book_unrealized_pnl)}">${usd(pnl.book_unrealized_pnl)}</td><td>Not included unless scoped after reset</td><td>${usd(pnl.fees_since_reset)} since reset</td><td>Clean book = live Coinbase basis + post-reset fills, reconciled to exchange size</td></tr><tr><td><strong>Clean Book Net Impact</strong></td><td>Internal strategy P&L</td><td colspan="4" class="${cls(pnl.net_book_impact)}">${usd(pnl.net_book_impact)}</td><td>Clean realized + clean unrealized - fees since reset</td></tr><tr><td>Clean Top-Line Performance</td><td>Baseline ${usd(c.starting_combined_capital)}</td><td colspan="4" class="${cls(pnl.net_book_impact)}">${usd(pnl.net_book_impact)} / ${pct((c.baseline_set && c.starting_combined_capital)?(pnl.net_book_impact/c.starting_combined_capital*100):null)}</td><td>Clean return excludes Coinbase native daily realized P&L and uses opening book + post-reset activity only</td></tr>`;
 }catch(e){$('errorBox').style.display='block';$('errorBox').textContent='Frontend error: '+e.message;console.error(e)}}
refresh(); setInterval(refresh,12000);
</script></body></html>
'''

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
