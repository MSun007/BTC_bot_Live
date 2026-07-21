import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import patch


MODULE_PATH = pathlib.Path(__file__).with_name("larry_perp_v1.py")
SPEC = importlib.util.spec_from_file_location("larry_safety_candidate", MODULE_PATH)
larry = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = larry
SPEC.loader.exec_module(larry)


class FakeGCS:
    def __init__(self, ledger=""):
        self.ledger = ledger
        self.rows = []

    def read_text(self, *args, **kwargs):
        return self.ledger

    def append_csv_row(self, blob_name, header, row):
        self.rows.append((blob_name, header, row))


class LarrySafetyTests(unittest.TestCase):
    def setUp(self):
        larry.SEND_EMAIL = False
        larry.SEND_TELEGRAM = False

    def test_coinbase_response_requires_explicit_order_id(self):
        accepted = larry.normalize_order_response(
            {"success": True, "success_response": {"order_id": "order-1"}},
            "client-1",
        )
        rejected = larry.normalize_order_response(
            {"success": False, "error_response": {"message": "rejected"}},
            "client-2",
        )
        self.assertTrue(accepted["ok"])
        self.assertFalse(rejected["ok"])

    def test_partial_fill_uses_actual_position_delta(self):
        before = {"signed_contracts": 0, "side": "FLAT", "contracts": 0, "current_price": 64000}
        after = {"signed_contracts": 4, "side": "LONG", "contracts": 4, "current_price": 64010}
        positions = iter([before, after])
        order = {
            "ok": True,
            "response": {"success": True, "success_response": {"order_id": "order-1"}},
            "client_order_id": "larry-v32-test",
        }
        fills = {"found": True, "avg_price": 64005, "contracts": 4, "commission": 1.0, "fills": []}
        gcs = FakeGCS()
        with (
            patch.object(larry, "get_live_net_position", side_effect=lambda cb: next(positions)),
            patch.object(larry, "place_market_order", return_value=order),
            patch.object(larry, "get_recent_fills_for_order", return_value=fills),
            patch.object(larry.time, "sleep", return_value=None),
        ):
            result = larry.execute_target(object(), gcs, 10, "CORE_IAF_LONG_PHANTOM_CONFIRMED")
        self.assertTrue(result["ok"])
        self.assertTrue(result["partial_fill"])
        self.assertEqual(result["execution_status"], "PARTIALLY_FILLED")
        self.assertEqual(result["requested_contracts"], 10)
        self.assertEqual(result["position_delta_contracts"], 4)

    def test_ownership_recovery_requires_exact_signed_match(self):
        ledger = (
            "timestamp,reason,action,before_signed,target_signed,after_signed,ok,client_order_id\n"
            "2026-07-17T19:01:57Z,CORE_IAF_SHORT,SELL,0,-4,-4,True,larry-v2-test-sell-4\n"
        )
        live = {"signed_contracts": -4, "side": "SHORT", "contracts": 4, "avg_entry_price": 64182.5}
        state = {}
        self.assertTrue(larry.recover_bot_managed_position_from_ledger(FakeGCS(ledger), state, live))
        self.assertEqual(state["bot_managed_position"]["signed_contracts"], -4)

        mismatch_state = {}
        mismatch = dict(live, signed_contracts=-8, contracts=8)
        self.assertFalse(larry.recover_bot_managed_position_from_ledger(FakeGCS(ledger), mismatch_state, mismatch))
        self.assertNotIn("bot_managed_position", mismatch_state)


if __name__ == "__main__":
    unittest.main()
