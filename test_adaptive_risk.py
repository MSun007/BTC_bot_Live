import importlib.util
import pathlib
import sys
import unittest


PATH = pathlib.Path(__file__).with_name("larry_perp_v1.py")
SPEC = importlib.util.spec_from_file_location("larry", PATH)
larry = importlib.util.module_from_spec(SPEC)
sys.modules["larry"] = larry
SPEC.loader.exec_module(larry)


def candle(start, o, h, lo, c, volume=100):
    return {"start": start, "open": o, "high": h, "low": lo, "close": c, "volume": volume}


class AdaptiveRiskTests(unittest.TestCase):
    def test_confirmed_pivots_do_not_use_newest_bar(self):
        bars = [
            candle(1, 100, 102, 99, 101), candle(2, 101, 105, 100, 104),
            candle(3, 104, 103, 98, 99), candle(4, 99, 101, 95, 100),
            candle(5, 100, 104, 99, 103), candle(6, 103, 108, 102, 107),
            candle(7, 107, 106, 101, 102), candle(8, 102, 103, 97, 98),
            candle(9, 98, 200, 1, 150),
        ]
        result = larry.classify_swing_pivots(bars)
        self.assertNotEqual((result.get("last_swing_high") or {}).get("price"), 200)
        self.assertNotEqual((result.get("last_swing_low") or {}).get("price"), 1)

    def test_position_version_changes_with_exchange_average(self):
        controls = {}
        larry.update_position_version(controls, {"signed_contracts": 4, "avg_entry_price": 100}, 2)
        first = controls["position_version"]
        larry.update_position_version(controls, {"signed_contracts": 8, "avg_entry_price": 102}, 2.2)
        self.assertEqual(controls["position_version"], first + 1)
        self.assertEqual(controls["position_reanchor"]["exchange_avg_entry"], 102)

    def test_adaptive_reduction_targets_lower_rung(self):
        controls = {"adaptive_defense": {"state": "REDUCE_ONE_RUNG"}}
        target, reason = larry.risk_exit_target_if_needed({"signed_contracts": 8}, controls, 100)
        self.assertLess(target, 8)
        self.assertEqual(reason, "ADAPTIVE_DEFENSE_REDUCE_LONG")

    def test_firm_atr_stop_has_priority(self):
        controls = {"atr_stop": 95, "adaptive_defense": {"state": "REDUCE_ONE_RUNG"}}
        target, reason = larry.risk_exit_target_if_needed({"signed_contracts": 8}, controls, 94)
        self.assertEqual(target, 0)
        self.assertEqual(reason, "ATR_STOP_LONG")

    def test_stop_blown_burned_score_uses_repeated_same_side_fishes(self):
        now = larry.iso_utc()
        state = {
            "stop_blown": {"active": True, "anchor": 100, "atr": 10, "stopped_side": "LONG"},
            "stop_blown_history": [
                {"at": now, "side": "LONG", "leader": "FISHED"},
                {"at": now, "side": "LONG", "leader": "FISHED"},
                {"at": now, "side": "LONG", "leader": "FISHED"},
            ],
        }
        larry.update_stop_blown_shadow(state, 103, 10)
        self.assertEqual(state["stop_blown"]["scores"]["BURNED"], 1.0)

    def test_r_multiple_profit_target_uses_locked_atr(self):
        state = larry.default_engine_state()
        sig = larry.SignalSnapshot(100, 50, .5, 90, 100, 110, 4, 1, 0, 0, {}, {})
        controls = larry.update_position_risk_controls(
            state, {"signed_contracts": 2, "avg_entry_price": 100, "current_price": 100}, sig, []
        )
        self.assertAlmostEqual(controls["tp1_trigger_price"], 104.5)

    def test_max_conviction_is_the_only_absolute_position_limit(self):
        previous = larry.MAX_CONVICTION_CONTRACTS
        try:
            larry.MAX_CONVICTION_CONTRACTS = 20
            self.assertEqual(larry.clamp_target(50), 20)
            self.assertEqual(larry.clamp_target(-50), -20)
        finally:
            larry.MAX_CONVICTION_CONTRACTS = previous

    def test_management_requires_matching_exchange_fingerprint(self):
        state = {
            "bot_managed_position": {
                "signed_contracts": -4,
                "product_id": "PERP",
                "avg_entry_price": 100.0,
            }
        }
        exact = larry.live_position_management_status(
            state, {"signed_contracts": -4, "product_id": "PERP", "avg_entry_price": 100.0}
        )
        changed_average = larry.live_position_management_status(
            state, {"signed_contracts": -4, "product_id": "PERP", "avg_entry_price": 101.0}
        )
        self.assertTrue(exact["allow_bot_to_trade_position"])
        self.assertFalse(changed_average["allow_bot_to_trade_position"])

    def test_ledger_recovery_fails_closed_without_prior_bot_continuity(self):
        class NeverReadLedger:
            def read_text(self, *_args, **_kwargs):
                raise AssertionError("ledger must not be consulted without continuity")

        state = {"manual_position_status": {"bot_managed": False}, "last_exchange_position": {}}
        recovered = larry.recover_bot_managed_position_from_ledger(
            NeverReadLedger(), state,
            {"signed_contracts": -4, "product_id": "PERP", "avg_entry_price": 100.0},
        )
        self.assertFalse(recovered)
        self.assertEqual(
            state["ownership_recovery"]["reason"],
            "persisted_bot_management_continuity_not_proven",
        )


if __name__ == "__main__":
    unittest.main()
