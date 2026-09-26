import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from paper_account import PaperStore, PaperAccount, PaperClient


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "account.sqlite3"
        self.store = PaperStore(self.path)
        self.account = PaperAccount(self.store, "BIP", fee_bps=6, fee_per_contract=.12, leverage=3)
        self.account.observe(100000, .0001)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def trade(self, side, qty, key):
        self.store.begin()
        try:
            result = self.account.order(side, qty, key)
            self.store.commit()
            return result
        except BaseException:
            self.store.rollback()
            raise

    def test_round_trip_exact_economics_and_all_costs(self):
        self.trade("BUY", 2, "entry")
        self.assertEqual(self.account.state()["average"], 100010)
        self.account.observe(101000, .0001)
        self.trade("SELL", 2, "exit")
        s = self.account.state()
        expected_gross = (100989.9 - 100010) * .02
        expected_fees = (100010 + 100989.9) * .02 * .0006 + .48
        self.assertEqual(s["signed"], 0)
        self.assertAlmostEqual(s["gross"], expected_gross)
        self.assertAlmostEqual(s["fees"], expected_fees)
        equity = float(self.account.balance()["balance_summary"]["total_usd_balance"]["value"])
        self.assertAlmostEqual(equity, 2000 + expected_gross - expected_fees)

    def test_add_partial_reduce_and_reverse(self):
        self.trade("BUY", 2, "a")
        self.account.observe(102000, .0001)
        self.trade("BUY", 1, "b")
        average = (100010 * 2 + 102010.2) / 3
        self.assertAlmostEqual(self.account.state()["average"], average)
        self.trade("SELL", 1, "c")
        self.assertAlmostEqual(self.account.state()["average"], average)
        self.trade("SELL", 4, "d")
        self.assertEqual(self.account.state()["signed"], -2)
        self.assertAlmostEqual(self.account.state()["average"], 101989.8)

    def test_atomic_cycle_rollback_includes_fill_ledger_state_and_alert(self):
        self.store.begin()
        self.account.order("BUY", 1, "rollback")
        self.store.write_json("engine.json", {"position": 1})
        self.store.append_csv_row("ledger.csv", ["id"], ["rollback"])
        self.store.enqueue("telegram", ["trade"], {})
        self.store.rollback()
        self.assertEqual(self.account.state()["signed"], 0)
        self.assertIsNone(self.store.read_json("engine.json"))
        self.assertEqual(self.store.read_text("ledger.csv"), "")
        self.assertEqual(self.store.snapshot()["pending_notifications"], 0)

    def test_restart_and_duplicate_order_are_idempotent(self):
        result = self.trade("BUY", 1, "same")
        self.store.db.close()
        self.store = PaperStore(self.path)
        self.account = PaperAccount(self.store, "BIP", fee_bps=6, fee_per_contract=.12, leverage=3)
        duplicate = self.trade("BUY", 1, "same")
        self.assertEqual(result, duplicate)
        self.assertEqual(len(self.account.state()["fills"]), 1)

    def test_hourly_funding_debit_credit_and_no_duplicate(self):
        s = self.account.state()
        s["last_observation"] = None
        self.store.write_json(self.account.KEY, s)
        self.account.observe(100000, .0001, 3590)
        with patch("paper_account.time.time", return_value=3590):
            self.trade("BUY", 1, "long")
        self.account.observe(100000, .0002, 3610)
        self.account.observe(100000, .0002, 3611)
        self.assertAlmostEqual(self.account.state()["funding"], -.1)
        with patch("paper_account.time.time", return_value=3611):
            self.trade("SELL", 2, "flip")
        self.account.observe(100000, .0002, 7190)
        self.account.observe(100000, .0003, 7210)
        self.assertAlmostEqual(self.account.state()["funding"], .1)

    def test_unresolved_funding_gap_does_not_invent_costs(self):
        self.trade("BUY", 1, "a")
        before = self.account.state()
        with self.assertRaisesRegex(RuntimeError, "Funding data gap"):
            self.account.observe(100000, .0001, time.time() + 7200)
        self.assertEqual(before, self.account.state())

    def test_margin_rejection_does_not_charge_fees(self):
        result = self.trade("BUY", 7, "too-big")
        self.assertFalse(result["success"])
        self.assertEqual(self.account.state()["fees"], 0)

    def test_stale_mark_and_nontransaction_order_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "atomic"):
            self.account.order("BUY", 1, "bad")
        with patch("paper_account.time.time", return_value=time.time() + 121):
            with self.assertRaisesRegex(RuntimeError, "fresh"):
                self.trade("BUY", 1, "stale")

    def test_real_exchange_mutations_cannot_be_forwarded(self):
        class Trap:
            def __getattr__(self, name):
                raise AssertionError("Touched real exchange: " + name)
        client = PaperClient(Trap(), self.account)
        for method in ("create_order", "cancel_orders", "close_position", "transfer", "get_accounts_real"):
            with self.assertRaisesRegex(RuntimeError, "PAPER"):
                getattr(client, method)
        dashboard = PaperClient(Trap(), self.account, read_only=True)
        with self.assertRaises(RuntimeError):
            dashboard.market_order_buy(client_order_id="no", product_id="BIP", base_size="1")
        with self.assertRaises(RuntimeError):
            client.market_order_buy(client_order_id="spot", product_id="BTC-USD", quote_size="100")


if __name__ == "__main__":
    unittest.main()
