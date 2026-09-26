"""Paper-only account and atomic cycle storage. No exchange write API is exposed."""
import csv
import io
import json
import math
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utc():
    return datetime.now(timezone.utc).isoformat()


def plain(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


class PaperStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS objects (name TEXT PRIMARY KEY, value TEXT NOT NULL, generation INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)")

    def begin(self):
        self.db.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.db.execute("COMMIT")

    def rollback(self):
        self.db.execute("ROLLBACK")

    def begin_cycle_budget(self, *args):
        pass  # Local durable I/O replaces per-object subprocess calls.

    def read_text(self, name, default=""):
        row = self.db.execute("SELECT value FROM objects WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def write_text(self, name, text, content_type=None):
        self.db.execute("INSERT INTO objects VALUES (?, ?, 1) ON CONFLICT(name) DO UPDATE SET value=excluded.value, generation=generation+1", (name, text))

    def read_json(self, name, default=None):
        text = self.read_text(name, None)
        return json.loads(text) if text is not None else default

    def write_json(self, name, value):
        self.write_text(name, json.dumps(value, default=str, allow_nan=False))

    def read_json_with_generation(self, name, default=None):
        row = self.db.execute("SELECT value, generation FROM objects WHERE name=?", (name,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (default, None)

    def write_json_cas(self, name, value, if_generation_match):
        # Called inside the engine's single cycle transaction.
        _, generation = self.read_json_with_generation(name)
        if generation != if_generation_match:
            return False
        self.write_json(name, value)
        return True

    def append_csv_row(self, name, header, row):
        buf = io.StringIO()
        existing = self.read_text(name)
        writer = csv.writer(buf)
        if not existing:
            writer.writerow(header)
        writer.writerow(row)
        self.write_text(name, existing + buf.getvalue())

    def enqueue(self, kind, args, kwargs):
        event_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO outbox VALUES (?, ?, ?)", (event_id, kind, json.dumps([args, kwargs], default=str)))
        return event_id

    def snapshot(self):
        return {"mode": "PAPER", "published_at": utc(), "objects": dict(self.db.execute("SELECT name, value FROM objects")), "pending_notifications": self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]}


class PaperAccount:
    KEY = "paper_account.json"

    def __init__(self, store, product_id, contract_size=0.01, fee_bps=5.0,
                 fee_per_contract=0.15, slippage_bps=1.0, leverage=10.0, starting_equity=2000.0):
        self.store = store
        self.product_id = product_id
        self.contract_size = contract_size
        self.fee_bps = fee_bps
        self.fee_per_contract = fee_per_contract
        self.slippage_bps = slippage_bps
        self.leverage = leverage
        for x in (contract_size, fee_bps, fee_per_contract, slippage_bps, leverage):
            if not math.isfinite(x) or x < 0:
                raise ValueError("Invalid paper execution assumption")
        if contract_size <= 0 or leverage <= 0:
            raise ValueError("Contract size and leverage must be positive")
        if not math.isfinite(starting_equity) or starting_equity <= 0:
            raise ValueError("Starting paper equity must be positive and finite")
        if store.read_json(self.KEY) is None:
            store.write_json(self.KEY, {"mode": "PAPER", "starting_equity": starting_equity,
                "started_at": utc(), "signed": 0, "average": 0.0, "mark": 0.0,
                "gross": 0.0, "fees": 0.0, "funding": 0.0, "fills": [],
                "funding_events": {}, "last_observation": None,
                "assumptions": {"fee_bps": fee_bps, "fee_per_contract": fee_per_contract,
                    "slippage_bps": slippage_bps, "contract_size": contract_size,
                    "fee_source": "provisional conservative assumption; verify account tier before launch"}})
        state = self.state()
        if state["starting_equity"] != starting_equity:
            raise RuntimeError("Paper capital changed mid-run: use a new database and bucket")
        assumptions = state["assumptions"]
        for key, value in (("fee_bps", fee_bps), ("fee_per_contract", fee_per_contract),
                           ("slippage_bps", slippage_bps), ("contract_size", contract_size)):
            if assumptions[key] != value:
                raise RuntimeError("Paper assumptions changed mid-run: " + key)

    def state(self):
        return self.store.read_json(self.KEY)

    def observe(self, mark, rate, timestamp=None):
        """Accrue at hourly boundaries, using the most recent observed rate/mark.

        Never invent missed historical rates after an outage. An open position
        crossing a boundary without a recent observation blocks the cycle until
        the gap is resolved. First observation while flat establishes the clock.
        """
        timestamp = time.time() if timestamp is None else timestamp
        if not math.isfinite(mark) or mark <= 0:
            raise ValueError("Missing or invalid current mark")
        if rate is None or not math.isfinite(rate):
            raise ValueError("Funding rate unavailable")
        s = self.state()
        previous = s.get("last_observation")
        if previous and timestamp < previous["timestamp"]:
            raise RuntimeError("Paper clock moved backwards")
        if previous:
            boundary = (int(previous["timestamp"] // 3600) + 1) * 3600
            while boundary <= timestamp:
                if s["signed"] and (timestamp - previous["timestamp"] > 180):
                    raise RuntimeError("Funding data gap: historical rate reconciliation required")
                key = str(boundary)
                if key not in s["funding_events"]:
                    amount = -s["signed"] * self.contract_size * previous["mark"] * previous["rate"]
                    s["funding"] += amount
                    s["funding_events"][key] = {"amount": amount, "rate": previous["rate"], "mark": previous["mark"], "signed": s["signed"]}
                boundary += 3600
        s["mark"] = mark
        s["last_observation"] = {"timestamp": timestamp, "mark": mark, "rate": rate}
        self.store.write_json(self.KEY, s)

    def position(self):
        s = self.state()
        q = s["signed"]
        return {"product_id": self.product_id, "side": "LONG" if q > 0 else "SHORT" if q < 0 else "FLAT",
            "number_of_contracts": str(abs(q)), "avg_entry_price": str(s["average"]),
            "current_price": str(s["mark"]), "unrealized_pnl": str(q * self.contract_size * (s["mark"] - s["average"])),
            "daily_realized_pnl": "0", "mode": "PAPER"}

    def balance(self):
        s = self.state()
        unrealized = float(self.position()["unrealized_pnl"])
        equity = s["starting_equity"] + s["gross"] - s["fees"] + s["funding"] + unrealized
        margin = abs(s["signed"]) * self.contract_size * s["mark"] / self.leverage
        values = {"total_usd_balance": equity, "cfm_usd_balance": equity,
            "available_margin": max(0, equity - margin), "initial_margin": margin,
            "futures_buying_power": max(0, equity - margin) * self.leverage,
            "unrealized_pnl": unrealized, "funding_pnl": s["funding"],
            "daily_realized_pnl": 0.0}
        return {"balance_summary": {k: {"value": str(v), "currency": "USD"} for k, v in values.items()}}

    def order(self, side, quantity, client_order_id):
        if not self.store.db.in_transaction:
            raise RuntimeError("Paper orders require an atomic engine cycle")
        if side not in ("BUY", "SELL") or quantity <= 0 or int(quantity) != quantity:
            raise ValueError("Invalid paper order")
        s = self.state()
        for f in s["fills"]:
            if f["client_order_id"] == client_order_id:
                return self.response(f)
        observation = s.get("last_observation")
        if not observation or time.time() - observation["timestamp"] > 120:
            raise RuntimeError("Paper fill requires a fresh market observation")
        sign = 1 if side == "BUY" else -1
        fill = s["mark"] * (1 + sign * self.slippage_bps / 10000)
        qty = int(quantity)
        delta = sign * qty
        before = s["signed"]
        after = before + delta
        closed = min(abs(before), qty) if before * delta < 0 else 0
        gross = closed * self.contract_size * (fill - s["average"]) * (1 if before > 0 else -1)
        fees = qty * (fill * self.contract_size * self.fee_bps / 10000 + self.fee_per_contract)
        equity = float(self.balance()["balance_summary"]["total_usd_balance"]["value"])
        if (abs(after) > abs(before) or before * after < 0) and abs(after) * self.contract_size * fill > max(0, equity - fees) * self.leverage:
            return {"success": False, "error_response": {"message": "Insufficient virtual margin"}}
        if after == 0:
            average = 0.0
        elif before == 0 or before * after < 0:
            average = fill
        elif before * delta > 0:
            average = (abs(before) * s["average"] + qty * fill) / abs(after)
        else:
            average = s["average"]
        f = {"order_id": "paper-" + uuid.uuid4().hex, "client_order_id": client_order_id,
            "product_id": self.product_id, "side": side, "size": str(qty), "price": str(fill),
            "commission": str(fees), "trade_time": utc(), "liquidity_indicator": "TAKER",
            "before_signed": before, "after_signed": after, "gross": gross, "mode": "PAPER"}
        s.update(signed=after, average=average, gross=s["gross"] + gross, fees=s["fees"] + fees)
        s["fills"].append(f)
        self.store.write_json(self.KEY, s)
        return self.response(f)

    @staticmethod
    def response(fill):
        return {"success": True, "success_response": {"order_id": fill["order_id"],
            "client_order_id": fill["client_order_id"]}, "mode": "PAPER"}


class PaperClient:
    """Explicit allowlist: unknown methods never reach the real client."""
    READS = frozenset({"get_product", "get_candles", "get_best_bid_ask"})

    def __init__(self, market_client, account, read_only=False):
        self._market = market_client
        self.account = account
        self.read_only = read_only

    def __getattr__(self, name):
        if name in self.READS:
            return getattr(self._market, name)
        raise RuntimeError("Exchange operation unavailable in PAPER: " + name)

    def list_futures_positions(self):
        return {"positions": [self.account.position()]}

    def get_futures_balance_summary(self):
        return self.account.balance()

    def get_accounts(self, **kwargs):
        return {"accounts": [], "has_next": False}

    def get_fills(self, product_id=None, limit=100, **kwargs):
        fills = self.account.state()["fills"]
        return {"fills": list(reversed([f for f in fills if not product_id or f["product_id"] == product_id]))[:limit]}

    def _order(self, side, client_order_id, product_id, base_size=None, **kwargs):
        if self.read_only or product_id != self.account.product_id or kwargs:
            raise RuntimeError("Only engine-owned paper futures orders are supported")
        return self.account.order(side, float(base_size), client_order_id)

    def market_order_buy(self, **kwargs):
        return self._order("BUY", **kwargs)

    def market_order_sell(self, **kwargs):
        return self._order("SELL", **kwargs)
