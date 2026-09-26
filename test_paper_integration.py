import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from paper_account import PaperAccount, PaperClient, PaperStore

ROOT = pathlib.Path(__file__).resolve().parent


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("paper_pipeline_engine", ROOT / "larry_perp_v1.py")
        cls.engine = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.engine
        spec.loader.exec_module(cls.engine)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PaperStore(pathlib.Path(self.tmp.name) / "paper.sqlite3")
        self.account = PaperAccount(self.store, self.engine.PERP_PRODUCT_ID, fee_bps=6, fee_per_contract=.12, leverage=3)
        self.account.observe(65000, .00001)
        self.client = PaperClient(None, self.account)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_real_execution_pipeline_reconciles_fills_and_alerts(self):
        e = self.engine
        with patch.object(e.time, "sleep"), patch.object(e, "send_trade_telegram") as telegram, patch.object(e, "send_trade_email"), patch.object(e, "ledger_running_totals", return_value={}):
            self.store.begin()
            entry = e.execute_target(self.client, self.store, 2, "CORE_IAF_LONG")
            self.store.commit()
            self.assertTrue(entry["ok"])
            self.assertEqual(entry["execution_status"], "FILLED")
            self.assertEqual(entry["after"]["signed_contracts"], 2)
            self.assertGreater(entry["fees_usd"], 0)
            self.assertEqual(entry["slippage_bps"], 1)
            self.account.observe(66000, .00001)
            self.store.begin()
            exit_result = e.execute_target(self.client, self.store, 0, "ATR_STOP_LONG")
            self.store.commit()
            self.assertTrue(exit_result["ok"])
            self.assertEqual(telegram.call_count, 2)
            self.assertAlmostEqual(exit_result["gross_realized_pnl_usd"], self.account.state()["gross"])
            ledger = self.store.read_text(e.PERP_TRADES_LEDGER_BLOB)
            self.assertIn("CORE_IAF_LONG", ledger)
            self.assertIn("ATR_STOP_LONG", ledger)

    def test_even_dry_run_false_cannot_use_real_client(self):
        e = self.engine
        with patch.object(e, "DRY_RUN", False):
            with self.assertRaisesRegex(RuntimeError, "real exchange client"):
                e.place_market_order(object(), "BUY", 1, "test")


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"DASHBOARD_SESSION_SECRET": "local-test-session-secret-only"}):
            import perp_dashboard_app
        cls.d = perp_dashboard_app
        from paper_dashboard import install_dashboard
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = PaperStore(pathlib.Path(cls.tmp.name) / "paper.sqlite3")
        cls.account = PaperAccount(cls.store, "BIP-20DEC30-CDE", fee_bps=6, fee_per_contract=.12, leverage=3)
        cls.account.observe(65000, .00001)
        cls.store.write_json("strategy_config.json", json.loads((ROOT / "strategy_config.json").read_text()))
        cls.store.write_json("unified_capital_state.json", {"starting_combined_capital": 2000})
        from paper_account import utc
        cls.store.write_json("coinbase_unified_heartbeat.json", {"ts": utc(), "state": "MONITORING", "status": "LIVE"})
        install_dashboard(cls.d, cls.store.snapshot)

    @classmethod
    def tearDownClass(cls):
        cls.store.db.close()
        cls.tmp.cleanup()

    def test_dashboard_capital_uses_paper_balance(self):
        with self.d.app.test_request_context("/api/data"):
            result = self.d.combined_capital({}, self.d.futures_balance(), self.d.load_capital_state())
            self.assertEqual(result["current_combined_capital"], 2000)
            self.assertEqual(result["net_pnl"], 0)

    def test_dashboard_is_labeled_paper(self):
        with self.d.app.test_request_context("/"):
            self.assertIn("NO REAL ORDERS", self.d.index())

    def test_fixed_controls_are_disabled_and_server_rejects_writes(self):
        self.assertNotIn('onclick="toggleSpot(true)"', self.d.HTML)
        self.assertNotIn('onclick="saveStrategyControls()"', self.d.HTML)
        with self.d.app.test_client() as client:
            self.assertEqual(client.post('/api/strategy_config', json={}).status_code, 401)
            with client.session_transaction() as session:
                session['authenticated'] = True
            for path in ('/api/strategy_config', '/api/spot_toggle', '/api/set_capital_baseline', '/api/reset_clean_book', '/api/update_strategy_param'):
                self.assertEqual(client.post(path, json={}).status_code, 409, path)

    def test_public_health_detects_stale_snapshot(self):
        from flask import g
        with self.d.app.test_request_context("/api/paper-health"):
            self.assertEqual(self.d.app.view_functions["paper_health"]()[1], 200)
        with self.d.app.test_request_context("/api/paper-health"):
            stale = self.store.snapshot()
            stale["published_at"] = "2020-01-01T00:00:00+00:00"
            g.paper_snapshot = stale
            self.assertEqual(self.d.app.view_functions["paper_health"]()[1], 503)

    def test_dashboard_cannot_reset_capital_or_call_raw_exchange(self):
        with self.assertRaises(RuntimeError):
            self.d.write_json(self.d.GCS_CAPITAL, {"starting_combined_capital": 100000})
        with self.assertRaises(RuntimeError):
            self.d.raw_get("/api/v3/brokerage/orders")

    def test_zero_contract_position_is_flat(self):
        with self.d.app.test_request_context("/api/data"):
            risk = self.d.perp_position_risk_state([{"side": "FLAT", "contracts": 0}], {})
            self.assertFalse(risk["has_position"])

    def test_funding_cash_events_reconcile_without_creating_trades(self):
        previous = self.account.state()
        updated = dict(previous, funding=-0.27, funding_events={"3600": {"amount": -0.27}})
        self.store.write_json(PaperAccount.KEY, updated)
        try:
            with self.d.app.test_request_context("/api/data"):
                summary = self.d.larry_trade_ledger_summary({})
                self.assertAlmostEqual(sum(r["strategy_net_impact_usd"] for r in summary["cash_events"]), -0.27)
                self.assertEqual(summary["realized_trade_count"], 0)
                self.assertEqual(summary["net_realized_pnl_usd"], -0.27)
        finally:
            self.store.write_json(PaperAccount.KEY, previous)


if __name__ == "__main__":
    unittest.main()
