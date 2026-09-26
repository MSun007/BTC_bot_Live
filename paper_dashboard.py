"""Read the same atomic paper snapshot for every field in a dashboard response."""
import io
import json
import os
import time
from datetime import datetime, timezone
from paper_account import PaperAccount, PaperClient
from paper_runtime import CloudMirror
from paper_health import assess_snapshot


def install_dashboard(d, snapshot_loader=None):
    from flask import g, has_request_context, request

    @d.app.before_request
    def fixed_paper_controls():
        if request.path in {"/api/set_capital_baseline", "/api/reset_clean_book", "/api/update_strategy_param"} or (
            request.path in {"/api/strategy_config", "/api/spot_toggle"} and
            (request.method == "POST" or "enabled" in request.args)
        ):
            return d.jsonify({"ok": False, "error": "Paper baseline and strategy settings are fixed for this run."}), 409

    def snapshot():
        if has_request_context() and hasattr(g, "paper_snapshot"):
            return g.paper_snapshot
        if snapshot_loader:
            value = snapshot_loader()
        else:
            bucket = d.BUCKET_PREFIX.removeprefix("gs://").rstrip("/")
            value, _ = CloudMirror(bucket).read("paper_snapshot.json")
        if not value or value.get("mode") != "PAPER" or not value.get("objects"):
            raise RuntimeError("Paper snapshot unavailable; refusing live-account fallback")
        if has_request_context():
            g.paper_snapshot = value
        return value

    def name(path):
        return path.rsplit("/", 1)[-1]

    def read_text(path):
        return snapshot()["objects"].get(name(path), "")

    def read_json(path, default=None):
        value = read_text(path)
        return json.loads(value) if value else default if default is not None else {}

    class SnapshotStore:
        def read_json(self, key):
            return read_json(key)

    def account():
        result = object.__new__(PaperAccount)
        result.store = SnapshotStore()
        cfg = read_json("strategy_config.json")
        result.product_id = cfg.get("PERP_PRODUCT_ID") or os.getenv("PERP_PRODUCT_ID", "BIP-20DEC30-CDE")
        result.contract_size = cfg["CONTRACT_SIZE_BTC"]
        result.leverage = cfg["MAX_EFFECTIVE_LEVERAGE"]
        return result

    market_client = None
    def client():
        nonlocal market_client
        # The underlying SDK is used exclusively for allowlisted market reads.
        if market_client is None:
            from paper_account import plain
            candidate = d.RESTClient(api_key=d.secret("COINBASE_DASHBOARD_KEY").strip(),
                api_secret=d.pem_from_secret(d.secret("COINBASE_DASHBOARD_SECRET")), timeout=12)
            permissions = plain(candidate.get_api_key_permissions())
            if permissions.get("can_trade") is not False or permissions.get("can_transfer") is not False:
                raise RuntimeError("PAPER dashboard requires a verified read-only Coinbase key")
            market_client = candidate
        return PaperClient(market_client, account(), read_only=True)
    d.cb = client
    d.read_json = read_json
    d.read_text_gcs = read_text
    d.gcs_exists = lambda path: name(path) in snapshot()["objects"]
    d.read_csv = lambda path: d.pd.read_csv(io.StringIO(read_text(path))) if read_text(path).strip() else d.pd.DataFrame()

    def raw_get(path, params=None):
        if path == "/api/v3/brokerage/cfm/balance_summary":
            return account().balance()
        if path == "/api/v3/brokerage/accounts":
            return {"accounts": [], "has_next": False}
        raise RuntimeError("Raw exchange endpoint blocked in PAPER: " + path)
    d.raw_get = raw_get

    def write_json(path, payload):
        key = name(path)
        if key not in {"bot_halt.json", "emergency_flatten_request.json"}:
            raise RuntimeError("Paper baseline and strategy settings are fixed for this run")
        bucket = d.BUCKET_PREFIX.removeprefix("gs://").rstrip("/")
        CloudMirror(bucket).write(key, payload)
        d._cache.clear()
    d.write_json = write_json

    original_summary = d.larry_trade_ledger_summary
    def summary(*args, **kwargs):
        result = original_summary(*args, **kwargs)
        a = account()
        s = a.state()
        result.update(execution_mode="PAPER", funding_pnl_usd=s["funding"],
            gross_realized_pnl_usd=s["gross"], fees_usd=s["fees"],
            net_realized_pnl_usd=s["gross"] - s["fees"] + s["funding"],
            open_unrealized_pnl_usd=float(a.position()["unrealized_pnl"]))
        result["net_total_pnl_usd"] = result["net_realized_pnl_usd"] + result["open_unrealized_pnl_usd"]
        result["source"] = "atomic_paper_account_net_of_fees_and_funding"
        # Keep funding as cash events, never masquerading as trades or wins.
        cash = [{"timestamp": f["trade_time"], "ok": True, "reason": "PAPER_" + f["side"],
                 "strategy_net_impact_usd": f["gross"] - float(f["commission"])} for f in s["fills"]]
        for boundary, event in s["funding_events"].items():
            if event["amount"]:
                cash.append({"timestamp": datetime.fromtimestamp(int(boundary), timezone.utc).isoformat(),
                    "ok": True, "reason": "PAPER_FUNDING", "event_type": "FUNDING",
                    "strategy_net_impact_usd": event["amount"]})
        cash.sort(key=lambda row: row["timestamp"])
        running = 0.0
        for row in cash:
            running += float(row.get("strategy_net_impact_usd") or 0)
            row["running_net_pnl_usd"] = running
        result["cash_events"] = cash
        result["note"] = "Paper P&L includes simulated fees and funding; trade statistics exclude funding."
        return result
    d.larry_trade_ledger_summary = summary

    original_capital = d.combined_capital
    def capital(*args, **kwargs):
        result = original_capital(*args, **kwargs)
        result["combined_equity_method"] = "paper_start_plus_gross_minus_fees_plus_funding_plus_unrealized"
        result["combined_equity_note"] = "Virtual futures account; no real account balances or manual trades are included."
        return result
    d.combined_capital = capital

    original_telegram = d.send_dashboard_telegram_message
    d.send_dashboard_telegram_message = lambda text: original_telegram("[PAPER] " + text)
    original_email = d.send_dashboard_email
    d.send_dashboard_email = lambda subject, body, *args, **kwargs: original_email("[PAPER] " + subject, body, *args, **kwargs)
    d.HTML = d.HTML.replace("<body>", '<body><div style="padding:12px;background:#6b4900;color:white;text-align:center;font-weight:bold">PAPER ACCOUNT · STARTING CAPITAL SHOWN BELOW · SIMULATED FILLS · NO REAL ORDERS</div>')
    for old, new in {
        "ALL live Coinbase futures exposure -- including manually-entered positions, not just bot-managed ones": "the simulated paper futures position",
        "Trading keys stay on the VM.": "No real exchange orders are sent.",
        "BOT LIVE": "BOT PAPER",
        "LIVE — order placement allowed": "PAPER — simulated order placement allowed",
        "Larry P&L is ledger net realized plus live open unrealized. Coinbase account equity remains a separate reference.": "Paper P&L includes simulated fees, funding and open unrealized P&L. No real account balance is used.",
        "Confirm Coinbase UI after use.": "Verify the paper position is flat after the next engine cycle.",
        "Use Coinbase or Emergency Close Futures": "Use Emergency Close Paper Position",
        "EMERGENCY CLOSE FUTURES": "EMERGENCY CLOSE PAPER POSITION",
        "Live Perp Position Risk Card": "Paper Perp Position Risk Card",
        "live Coinbase position": "simulated paper position",
        "Coinbase live position": "simulated paper position",
        "Live Coinbase avg entry": "Simulated average entry",
        "Coinbase truth": "Paper account truth",
        "Coinbase is flat.": "Paper account is flat.",
        "Baseline + Larry realized P&L": "Baseline + realized P&L + funding (excludes open UPL)",
        "Lower chart includes realized P&L and every entry/add fee.": "Lower chart includes realized P&L, every fee, and hourly paper funding.",
        "equity = baseline + cumulative net realized P&L": "realized equity = baseline + net trading P&L + funding; open UPL is excluded",
        "per-order net impact": "cash-event net impact",
        "Per-trade P&L:": "Cash-event P&L:",
        "${r.y>=0?'Winning':'Losing'} realized trade": "${r.event_type==='FUNDING'?'Funding payment':'Simulated fill cash impact'}",
        "No realized trades in this range yet": "No paper cash events in this range yet",
        "Equity curve builds as Larry closes realized trades.": "Realized equity includes fill fees, closed P&L and funding as they occur.",
        "click to expand · live config · no SSH required": "click to expand · fixed paper config · read only",
        "Use this while debugging Larry Perp. Disabling Spot also disables the Spot→Perp bridge so only the Perp engine can create exposure.": "Spot execution is disabled in this paper futures experiment.",
        "Spot toggle reads/writes strategy_config.json.": "Spot controls are unavailable in paper mode.",
        'onclick="saveStrategyControls()"': 'disabled title="Strategy fixed for this paper run"',
        'onclick="toggleSpot(true)"': 'disabled title="Spot disabled in paper futures"',
        'onclick="toggleSpot(false)"': 'disabled title="Spot disabled in paper futures"',
    }.items():
        d.HTML = d.HTML.replace(old, new)

    @d.app.after_request
    def mode_header(response):
        response.headers["X-Larry-Execution-Mode"] = "PAPER"
        return response

    d._PUBLIC_PATHS.add("/api/paper-health")
    def paper_health():
        try:
            result = assess_snapshot(snapshot())
            return d.jsonify(result), 200 if result["healthy"] else 503
        except Exception as exc:
            d.app.logger.warning("PAPER_HEALTH snapshot read failed: %s", type(exc).__name__)
            return d.jsonify({"mode": "PAPER", "healthy": False, "reason": "snapshot_read_failed"}), 503
    if "paper_health" in d.app.view_functions:
        d.app.view_functions["paper_health"] = paper_health
    else:
        d.app.add_url_rule("/api/paper-health", "paper_health", paper_health)
