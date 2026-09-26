"""Install paper account plumbing around the unchanged v48 decision engine."""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from paper_account import PaperStore, PaperAccount, PaperClient, plain, utc
from paper_profile import profile, canonical_digest

ROOT = Path(__file__).resolve().parent


class CloudMirror:
    """One bounded atomic snapshot per cycle; dashboard commands stay separate."""
    def __init__(self, bucket):
        if not bucket or bucket == "btc_trade_log" or "paper" not in bucket.lower():
            raise RuntimeError("PAPER requires a separate bucket containing 'paper' in its name")
        from google.cloud import storage
        self.bucket = storage.Client().bucket(bucket)

    def read(self, name):
        from google.api_core.exceptions import NotFound, PreconditionFailed
        # A publisher may replace the object between metadata and content reads.
        # Retry that race with a NEW Blob (reload pins its generation).
        for attempt in range(3):
            blob = self.bucket.blob(name)
            try:
                blob.reload(timeout=10, retry=None)
            except NotFound:
                return None, None
            try:
                data = blob.download_as_text(timeout=10, retry=None, if_generation_match=blob.generation)
                return json.loads(data), blob.generation
            except (NotFound, PreconditionFailed):
                if attempt == 2:
                    raise

    def write(self, name, value, generation=None):
        kwargs = {} if generation is None else {"if_generation_match": generation}
        self.bucket.blob(name).upload_from_string(json.dumps(value, default=str),
            content_type="application/json", timeout=10, retry=None, **kwargs)


def install_engine(e, resources=None):
    """This distribution is always paper, even if DRY_RUN is false in config."""
    base_config = json.loads((ROOT / "strategy_config.json").read_text())
    if canonical_digest(base_config) != e.EXPECTED_CONFIG_SHA256:
        raise RuntimeError("Archived strategy config does not match v48 integrity hash")
    run_config, starting_equity = profile(base_config, os.getenv("PAPER_PROFILE", "2k"))
    e.EXPECTED_CONFIG_SHA256 = canonical_digest(run_config)
    e.EXPECTED_CONFIG_VERSION = run_config["CONFIG_VERSION"]
    store = None
    account = None
    mirror = None
    lock_file = None

    def initialize():
        nonlocal store, account, mirror, lock_file
        if store is not None:
            return
        if resources is not None:
            store, account, mirror = resources
            return
        db_path = Path(os.getenv("PAPER_DB_PATH", str(ROOT / "state" / "paper.sqlite3")))
        new_database = not db_path.exists()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Hold an OS lock for the whole process, including notification delivery.
        lock_file = open(str(db_path) + ".lock", "a+b")
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0)
            lock_file.write(b"0")
            lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        candidate = PaperStore(db_path)
        cfg = run_config
        settings = json.loads((ROOT / "paper_settings.json").read_text())
        if settings.get("fee_basis") != "historical_live_fills":
            raise RuntimeError("Paper fee assumptions must have a documented basis")
        mirror = CloudMirror(os.getenv("BTC_LOG_BUCKET", ""))
        if new_database and mirror.read("paper_snapshot.json")[0] is not None:
            raise RuntimeError("Existing paper run found: restore its database instead of resetting capital")
        account = PaperAccount(candidate, e.PERP_PRODUCT_ID, cfg["CONTRACT_SIZE_BTC"],
            settings["fee_bps"], settings["fee_per_contract"], settings["slippage_bps"], cfg["MAX_EFFECTIVE_LEVERAGE"], starting_equity)
        candidate.write_json(e.STRATEGY_CONFIG_BLOB, cfg)
        saved_account = account.state()
        saved_account["assumptions"]["fee_source"] = settings["fee_source"]
        candidate.write_json(account.KEY, saved_account)
        if candidate.read_json(e.UNIFIED_CAPITAL_STATE_BLOB) is None:
            candidate.write_json(e.UNIFIED_CAPITAL_STATE_BLOB, {
                "starting_combined_capital": starting_equity, "starting_capital": starting_equity,
                "tracking_start_timestamp": account.state()["started_at"],
                "started_at": account.state()["started_at"], "source": f"PAPER ${starting_equity:,.0f} virtual account"})
        store = candidate

    def build_client():
        initialize()
        market = e.RESTClient(api_key=e.load_secret("COINBASE_DASHBOARD_KEY").strip(),
            api_secret=e.fix_coinbase_secret(e.load_secret("COINBASE_DASHBOARD_SECRET")), timeout=12)
        permissions = plain(market.get_api_key_permissions())
        if permissions.get("can_trade") is not False or permissions.get("can_transfer") is not False:
            raise RuntimeError("PAPER requires a verified read-only Coinbase key")
        return PaperClient(market, account)
    e.build_coinbase_client = build_client
    e.PaperStoreFactory = lambda bucket: (initialize() or store)

    # Execution routes only through our virtual account, independently of mutable DRY_RUN.
    def place_order(cb, side, contracts, reason):
        if not isinstance(cb, PaperClient) or cb.read_only:
            raise RuntimeError("Paper engine refuses a real exchange client")
        if contracts <= 0 or side == "NONE":
            return {"ok": True, "skipped": True}
        client_id = "larry-v32-paper-" + uuid.uuid4().hex
        result = cb._order(side, client_order_id=client_id, product_id=e.PERP_PRODUCT_ID, base_size=str(contracts))
        return e.normalize_order_response(result, client_id)
    e.place_market_order = place_order

    def no_spot(*args, **kwargs):
        raise RuntimeError("Spot execution is disabled in this paper futures distribution")
    e.place_spot_market_buy = no_spot

    original_telegram = e.send_telegram_message
    original_email = e.send_email
    def queue(kind, args, kwargs):
        initialize()
        store.enqueue(kind, args, kwargs)
        return True
    e.send_telegram_message = lambda text, **kw: queue("telegram", ["[PAPER] " + text], kw) if e.SEND_TELEGRAM else False
    e.send_email = lambda subject, body: queue("email", ["[PAPER] " + subject, body], {}) if e.SEND_EMAIL else False

    def drain():
        delivery = store.read_json("paper_notification_delivery.json", {})
        if time.time() < delivery.get("retry_after", 0):
            return
        started = time.monotonic()
        # Soft budget between bounded sender calls; never block on 20 failures.
        for event_id, kind, payload in store.db.execute("SELECT id, kind, payload FROM outbox ORDER BY rowid LIMIT 3").fetchall():
            if time.monotonic() - started >= 10:
                break
            args, kwargs = json.loads(payload)
            sender = original_telegram if kind == "telegram" else original_email
            if sender(*args, **kwargs):
                store.db.execute("DELETE FROM outbox WHERE id=?", (event_id,))
                store.write_json("paper_notification_delivery.json", {"failures": 0, "retry_after": 0})
            else:
                e.log.warning("PAPER notification pending retry: %s", event_id)
                failures = min(5, delivery.get("failures", 0) + 1)
                store.write_json("paper_notification_delivery.json", {"failures": failures, "retry_after": time.time() + min(900, 60 * 2 ** (failures - 1))})
                break

    def publish_status():
        initialize()
        drain()
        mirror.write("paper_snapshot.json", store.snapshot())
    # Error alerts must drain even when the next trading cycle never succeeds.
    e.publish_paper_status = publish_status

    original_totals = e.ledger_running_totals
    def totals(gcs):
        result = original_totals(gcs)
        funding = account.state()["funding"]
        result["funding_pnl_usd"] = funding
        result["net_realized_pnl_usd"] += funding
        return result
    e.ledger_running_totals = totals

    original_reset = e.reset_daily_risk_if_needed
    def reset(state):
        original_reset(state)
        if account is None:
            return
        today = e.now_utc().date()
        from datetime import datetime, timezone
        daily_funding = sum(v["amount"] for k, v in account.state()["funding_events"].items()
                            if datetime.fromtimestamp(int(k), timezone.utc).date() == today)
        risk = state.setdefault("risk", {})
        previous = risk.get("paper_funding_accounted", {})
        accounted = previous.get("amount", 0) if previous.get("day") == str(today) else 0
        risk["daily_net_pnl_usd"] = risk.get("daily_net_pnl_usd", 0) + daily_funding - accounted
        risk["paper_funding_accounted"] = {"day": str(today), "amount": daily_funding}
    e.reset_daily_risk_if_needed = reset

    original_run = e.run_once
    def run(cb, gcs):
        initialize()
        # Commands cannot switch execution mode. Read failures block a new cycle.
        for name in (e.BOT_HALT_BLOB, e.EMERGENCY_FLATTEN_REQUEST_BLOB):
            command, generation = mirror.read(name)
            if command is not None:
                version_key = "control_generation_" + name
                if store.read_json(version_key) != generation:
                    store.write_json(name, command)
                    store.write_json(version_key, generation)
        store.begin()
        try:
            product = plain(e.coinbase_read("paper_funding_product", lambda: cb.get_product(e.PERP_PRODUCT_ID)))
            details = product.get("future_product_details") or {}
            rate = details.get("funding_rate")
            if rate is None:
                raise RuntimeError("Missing funding rate; cannot compute paper costs")
            account.observe(float(product.get("price") or 0), float(rate))
            cap = store.read_json(e.UNIFIED_CAPITAL_STATE_BLOB)
            if not cap.get("starting_btc_price"):
                cap["starting_btc_price"] = account.state()["mark"]
                store.write_json(e.UNIFIED_CAPITAL_STATE_BLOB, cap)
            original_run(cb, gcs)
            for key in (e.ENGINE_STATE_BLOB, e.UNIFIED_HEARTBEAT_BLOB, e.LEGACY_HEARTBEAT_BLOB, e.PERP_POSITION_STATE_BLOB):
                value = store.read_json(key, {})
                value.update(execution_mode="PAPER", real_orders_enabled=False)
                store.write_json(key, value)
            store.commit()
        except BaseException:
            store.rollback()
            raise
        # A failed publication cannot undo an already committed cycle. The next
        # cycle republishes the entire durable state, rather than repeating fills.
        drain()
        mirror.write("paper_snapshot.json", store.snapshot())
    e.run_once = run
