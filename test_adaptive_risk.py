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

    def test_reversal_resets_trailing_stop_owned_by_prior_side(self):
        controls = {
            "position_version": 1, "position_fingerprint": "-1:65390.00000000",
            "position_signed_contracts": -1, "position_avg_entry": 65390,
            "lowest_price": 63760, "tsl_active": True, "tsl_stop": 64078,
            "tsl_position_version": 1,
        }
        larry.update_position_version(
            controls, {"signed_contracts": 4, "avg_entry_price": 64060}, 300
        )
        self.assertFalse(controls["tsl_active"])
        self.assertIsNone(controls["tsl_stop"])
        self.assertIsNone(controls["lowest_price"])
        self.assertIsNone(controls["tsl_position_version"])

    def test_same_side_reduction_preserves_and_reowns_trailing_stop(self):
        controls = {
            "position_version": 1, "position_fingerprint": "4:64060.00000000",
            "position_signed_contracts": 4, "position_avg_entry": 64060,
            "highest_price": 65100, "tsl_active": True, "tsl_stop": 64774,
            "tsl_position_version": 1,
        }
        larry.update_position_version(
            controls, {"signed_contracts": 2, "avg_entry_price": 64060}, 300
        )
        self.assertTrue(controls["tsl_active"])
        self.assertEqual(controls["tsl_position_version"], 2)
        self.assertEqual(controls["highest_price"], 65100)

    def test_fresh_entry_does_not_consume_add_allowance(self):
        state = larry.default_engine_state()
        larry.record_progressive_add(
            state,
            {"ok": True, "before": {"signed_contracts": 0},
             "after": {"signed_contracts": 4, "side": "LONG", "avg_entry_price": 100}},
            {"confidence_pct": 58},
        )
        self.assertEqual(state["add_on_state"]["adds_count"], 0)

    def test_same_side_increase_counts_as_one_add(self):
        state = larry.default_engine_state()
        larry.record_progressive_add(
            state,
            {"ok": True, "before": {"signed_contracts": 4},
             "after": {"signed_contracts": 8, "side": "LONG", "avg_entry_price": 101}},
            {"confidence_pct": 92},
        )
        self.assertEqual(state["add_on_state"]["adds_count"], 1)

    def test_one_real_add_is_allowed_then_second_is_blocked(self):
        prior_max = larry.MAX_POSITION_ADDS
        prior_improvement = larry.MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD
        try:
            larry.MAX_POSITION_ADDS = 1
            larry.MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD = 25
            state = larry.default_engine_state()
            allowed, _ = larry.should_allow_progressive_add(
                state, 4, 8, {"confidence_pct": 92}
            )
            self.assertTrue(allowed)
            state["add_on_state"].update(
                {"adds_count": 1, "last_add_confidence_pct": 92}
            )
            allowed, reason = larry.should_allow_progressive_add(
                state, 8, 14, {"confidence_pct": 100}
            )
            self.assertFalse(allowed)
            self.assertIn("max_position_adds_reached", reason)
        finally:
            larry.MAX_POSITION_ADDS = prior_max
            larry.MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD = prior_improvement

    def test_flat_position_resets_add_allowance(self):
        state = larry.default_engine_state()
        state["add_on_state"]["adds_count"] = 1
        sig = larry.SignalSnapshot(100, 50, .5, 90, 100, 110, 4, 1, 0, 0, {}, {})
        larry.update_position_risk_controls(
            state, {"signed_contracts": 0, "avg_entry_price": 0}, sig, []
        )
        self.assertEqual(state["add_on_state"]["adds_count"], 0)

    def test_adaptive_reduction_targets_lower_rung(self):
        controls = {"adaptive_defense": {"state": "REDUCE_ONE_RUNG"}}
        target, reason = larry.risk_exit_target_if_needed({"signed_contracts": 8}, controls, 100)
        self.assertLess(target, 8)
        self.assertEqual(reason, "ADAPTIVE_DEFENSE_REDUCE_LONG")

    def test_adaptive_reduction_halves_four_contract_probe(self):
        controls = {"adaptive_defense": {"state": "REDUCE_ONE_RUNG"}}
        target, reason = larry.risk_exit_target_if_needed(
            {"signed_contracts": -4}, controls, 100
        )
        self.assertEqual(target, -2)
        self.assertEqual(reason, "ADAPTIVE_DEFENSE_REDUCE_SHORT")

    def test_adaptive_entry_grace_blocks_ordinary_early_reduction(self):
        state = larry.default_engine_state()
        controls = state["position_controls"]
        controls.update({
            "adaptive_entry_at": larry.iso_utc(),
            "adaptive_entry_price": 100,
            "atr_at_entry": 10,
            "adaptive_entry_baseline": {"score": 0, "factors": []},
        })
        bars = [
            candle(1, 89, 91, 88, 90), candle(2, 90, 92, 89, 91),
            candle(3, 91, 93, 90, 92), candle(4, 92, 94, 91, 93),
            candle(5, 93, 95, 92, 94), candle(6, 94, 102, 93, 98),
        ]
        sig = larry.SignalSnapshot(101, 70, 2.0, 90, 95, 105, 10, 2, 0, 3, {}, {})
        result = larry.adaptive_defense_snapshot(
            state, {"signed_contracts": -4, "avg_entry_price": 100}, sig, bars, {}
        )
        self.assertTrue(result["entry_grace_active"])
        self.assertEqual(result["state"], "HOLD")
        self.assertFalse(result["eligible"])

    def test_adaptive_defense_requires_post_entry_atr_excursion_then_confirms(self):
        state = larry.default_engine_state()
        controls = state["position_controls"]
        controls.update({
            "adaptive_entry_at": (larry.now_utc() - larry.timedelta(minutes=30)).isoformat(),
            "adaptive_entry_price": 100,
            "atr_at_entry": 10,
            "adaptive_entry_baseline": {"score": 0, "factors": []},
        })
        bars = [
            candle(1, 89, 91, 88, 90), candle(2, 90, 92, 89, 91),
            candle(3, 91, 93, 90, 92), candle(4, 92, 94, 91, 93),
            candle(5, 93, 95, 92, 94), candle(6, 94, 104, 93, 98),
        ]
        sig = larry.SignalSnapshot(106, 70, 2.0, 90, 95, 105, 10, 2, 0, 3, {}, {})
        first = larry.adaptive_defense_snapshot(
            state, {"signed_contracts": -4, "avg_entry_price": 100}, sig, bars,
            {"last_swing_high": {"price": 104}},
        )
        self.assertEqual(first["state"], "CONFIRMING")
        self.assertGreaterEqual(first["adverse_atr"], 0.50)
        controls["adaptive_defense"] = first
        second = larry.adaptive_defense_snapshot(
            state, {"signed_contracts": -4, "avg_entry_price": 100}, sig, bars,
            {"last_swing_high": {"price": 104}},
        )
        self.assertEqual(second["state"], "CONFIRMING")
        controls["adaptive_defense"] = second
        third = larry.adaptive_defense_snapshot(
            state, {"signed_contracts": -4, "avg_entry_price": 100}, sig, bars,
            {"last_swing_high": {"price": 104}},
        )
        self.assertEqual(third["state"], "EXIT")
        self.assertEqual(third["confirm_cycles"], 3)

    def test_stale_trailing_stop_cannot_exit_new_position_version(self):
        controls = {
            "position_version": 2, "tsl_position_version": 1,
            "tsl_active": True, "tsl_stop": 99,
            "atr_stop": 90, "adaptive_defense": {"state": "HOLD"},
        }
        target, reason = larry.risk_exit_target_if_needed(
            {"signed_contracts": 4}, controls, 98
        )
        self.assertIsNone(target)
        self.assertIsNone(reason)

    def test_current_position_trailing_stop_still_exits(self):
        controls = {
            "position_version": 2, "tsl_position_version": 2,
            "tsl_active": True, "tsl_stop": 99,
            "atr_stop": 90, "adaptive_defense": {"state": "HOLD"},
        }
        target, reason = larry.risk_exit_target_if_needed(
            {"signed_contracts": 4}, controls, 98
        )
        self.assertEqual(target, 0)
        self.assertEqual(reason, "TSL_STOP_LONG")

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

    def test_adaptive_exit_requires_signal_clear_before_same_side_reentry(self):
        state = larry.default_engine_state()
        larry.start_adaptive_reentry_guard(state, "LONG", "ADAPTIVE_DEFENSE_EXIT_LONG")
        still_long = larry.SignalSnapshot(95, 25, .1, 96, 100, 104, 2, 1.3, 4, 0, {}, {})
        guard = larry.update_adaptive_reentry_guard(
            state, still_long, {"structure": "BEARISH_LH_LL"}
        )
        self.assertFalse(guard["signal_cleared"])
        allowed, reason = larry.adaptive_reentry_allows(state, "LONG", larry.iso_utc())
        self.assertFalse(allowed)
        self.assertIn("has not cleared", reason)

    def test_pre_clear_phantom_cannot_be_reused_as_fresh_setup(self):
        state = larry.default_engine_state()
        guard = larry.start_adaptive_reentry_guard(
            state, "LONG", "ADAPTIVE_DEFENSE_EXIT_LONG"
        )
        old_setup_time = guard["started_at"]
        cleared = larry.SignalSnapshot(101, 50, .5, 96, 100, 104, 2, .8, 0, 0, {}, {})
        larry.update_adaptive_reentry_guard(
            state, cleared, {"structure": "RANGE_OR_TRANSITION"}
        )
        allowed, reason = larry.adaptive_reentry_allows(state, "LONG", old_setup_time)
        self.assertFalse(allowed)
        self.assertIn("new post-clear setup", reason)

    def test_new_post_clear_setup_is_eligible_and_probe_capped(self):
        state = larry.default_engine_state()
        larry.start_adaptive_reentry_guard(state, "LONG", "ADAPTIVE_DEFENSE_EXIT_LONG")
        cleared = larry.SignalSnapshot(101, 50, .5, 96, 100, 104, 2, .8, 0, 0, {}, {})
        guard = larry.update_adaptive_reentry_guard(
            state, cleared, {"structure": "RANGE_OR_TRANSITION"}
        )
        cleared_at = larry.parse_dt(guard["signal_cleared_at"])
        fresh_at = (cleared_at + larry.timedelta(seconds=1)).isoformat()
        allowed, _ = larry.adaptive_reentry_allows(state, "LONG", fresh_at)
        self.assertTrue(allowed)
        self.assertTrue(guard["first_reentry_probe_only"])

    def test_opposite_side_is_not_blocked_by_same_side_guard(self):
        state = larry.default_engine_state()
        larry.start_adaptive_reentry_guard(state, "LONG", "ADAPTIVE_DEFENSE_EXIT_LONG")
        allowed, _ = larry.adaptive_reentry_allows(state, "SHORT", None)
        self.assertTrue(allowed)

    def test_gcs_read_retries_once_then_returns_success(self):
        class RetryReadGCS(larry.GCS):
            def __init__(self):
                self.bucket_name = "test"
                self.prefix = "gs://test"
                self.use_python_storage = False
                self.client = None
                self.bucket = None
                self._cycle_io_remaining_seconds = None
                self.calls = 0

            def _run(self, cmd, input_text=None, timeout_seconds=None):
                self.calls += 1
                if self.calls == 1:
                    return larry.subprocess.CompletedProcess(cmd, 1, "", "transient")
                return larry.subprocess.CompletedProcess(cmd, 0, "payload", "")

        gcs = RetryReadGCS()
        original_sleep = larry.time.sleep
        try:
            larry.time.sleep = lambda _seconds: None
            self.assertEqual(gcs.read_text("critical.json", default="fallback"), "payload")
            self.assertEqual(gcs.calls, 2)
        finally:
            larry.time.sleep = original_sleep

    def test_gcs_cycle_budget_fails_fast_when_exhausted(self):
        gcs = object.__new__(larry.GCS)
        gcs._cycle_io_remaining_seconds = 0
        with self.assertRaises(TimeoutError):
            gcs._run(["gcloud", "storage", "cat", "gs://test/object"])

    def test_non_gcs_time_does_not_consume_gcs_budget(self):
        gcs = object.__new__(larry.GCS)
        gcs.begin_cycle_budget(35)
        before = gcs._remaining_cycle_budget()
        # The budget is a counter charged only by _run/backoff, not a wall-clock deadline.
        self.assertEqual(gcs._remaining_cycle_budget(), before)

    def test_coinbase_read_retries_transient_5xx_then_succeeds(self):
        class Response:
            status_code = 502

        class TransientError(Exception):
            response = Response()

        calls = []

        def operation():
            calls.append(True)
            if len(calls) < 3:
                raise TransientError("Bad Gateway")
            return {"ok": True}

        original_attempts = larry.COINBASE_READ_ATTEMPTS
        original_backoff = larry.COINBASE_READ_BACKOFF_SECONDS
        try:
            larry.COINBASE_READ_ATTEMPTS = 3
            larry.COINBASE_READ_BACKOFF_SECONDS = 0
            self.assertEqual(larry.coinbase_read("test", operation), {"ok": True})
            self.assertEqual(len(calls), 3)
        finally:
            larry.COINBASE_READ_ATTEMPTS = original_attempts
            larry.COINBASE_READ_BACKOFF_SECONDS = original_backoff

    def test_coinbase_read_does_not_retry_non_transient_4xx(self):
        class Response:
            status_code = 401

        class AuthError(Exception):
            response = Response()

        calls = []

        def operation():
            calls.append(True)
            raise AuthError("Unauthorized")

        original_backoff = larry.COINBASE_READ_BACKOFF_SECONDS
        try:
            larry.COINBASE_READ_BACKOFF_SECONDS = 0
            with self.assertRaises(AuthError):
                larry.coinbase_read("test", operation)
            self.assertEqual(len(calls), 1)
        finally:
            larry.COINBASE_READ_BACKOFF_SECONDS = original_backoff

    def test_portfolio_access_403_is_retryable_for_reads_and_rebuilds_client(self):
        class Response:
            status_code = 403

        class PortfolioError(Exception):
            response = Response()

        exc = PortfolioError(
            '{"error":"PERMISSION_DENIED","message":"User does not have access to portfolio"}'
        )
        self.assertTrue(larry.is_transient_coinbase_error(exc))
        self.assertTrue(larry.should_rebuild_coinbase_client(exc))

    def test_unrelated_403_is_not_treated_as_transient(self):
        class Response:
            status_code = 403

        class ForbiddenError(Exception):
            response = Response()

        exc = ForbiddenError("Forbidden by policy")
        self.assertFalse(larry.is_transient_coinbase_error(exc))
        self.assertFalse(larry.should_rebuild_coinbase_client(exc))

    def test_noop_target_does_not_emit_trade_decision(self):
        original_get_position = larry.get_live_net_position
        original_build_decision = larry.build_trade_decision
        calls = []
        try:
            larry.get_live_net_position = lambda _cb: {
                "side": "FLAT", "contracts": 0, "signed_contracts": 0,
                "avg_entry_price": 0, "current_price": 0,
            }
            larry.build_trade_decision = lambda *_args: calls.append(True)
            result = larry.execute_target(object(), object(), 0, "NOOP_TEST")
            self.assertTrue(result["ok"])
            self.assertIsNone(result["order"])
            self.assertEqual(calls, [])
        finally:
            larry.get_live_net_position = original_get_position
            larry.build_trade_decision = original_build_decision

    def test_trade_decision_contains_promised_entry_context(self):
        original_context = dict(larry._CYCLE_CONTEXT)
        try:
            larry._CYCLE_CONTEXT["decision_context"] = {
                "direction": "SHORT", "active_score": 3, "signal_threshold": 3,
                "setup_id": "setup-test", "long_score": 0, "short_score": 3,
                "macro": {"regime": "BULL", "gate_open": True},
                "funding": {"rate": 0.0001, "bucket": "OK"},
                "sizing_decision": {"confidence_pct": 58, "reason": "probe", "final_contracts": 4},
                "atr_stop": 65170, "tsl_active": False, "tsl_stop": None,
                "adaptive_defense": {"score": 0, "entry_age_seconds": 0},
            }
            plan = larry.safe_target_order_plan(0, -4)
            decision = larry.build_trade_decision(
                plan, "CORE_IAF_SHORT_PHANTOM_CONFIRMED",
                {"side": "FLAT", "contracts": 0, "signed_contracts": 0},
            )
            self.assertEqual(decision["trade_intent"], "NEW_ENTRY")
            self.assertEqual(decision["execution_reason"], "NEW_SHORT_ENTRY")
            self.assertEqual(decision["signal_reason"], "CORE_IAF_SHORT_PHANTOM_CONFIRMED")
            self.assertTrue(decision["order"]["required"])
            self.assertEqual(decision["confidence"]["confidence_pct"], 58)
            self.assertEqual(decision["confidence"]["score"], 3)
            self.assertEqual(decision["confidence"]["threshold"], 3)
            self.assertEqual(decision["expected_post_position"], "SHORT 4")
        finally:
            larry._CYCLE_CONTEXT.clear()
            larry._CYCLE_CONTEXT.update(original_context)

    def test_entry_telegram_contains_reason_order_position_and_remaining(self):
        original_send = larry.send_telegram_message
        original_timestamp = larry.et_timestamp_short
        sent = []
        try:
            larry.send_telegram_message = lambda message, event_type=None: sent.append((message, event_type)) or True
            larry.et_timestamp_short = lambda: "test time ET"
            larry.send_trade_telegram({
                "ok": True,
                "trade_intent": "NEW_ENTRY",
                "execution_reason": "NEW_SHORT_ENTRY",
                "signal_reason": "CORE_IAF_SHORT_PHANTOM_CONFIRMED",
                "plan": {"action": "SELL", "contracts_needed": 4},
                "order": {"client_order_id": "test"},
                "before": {"side": "FLAT", "contracts": 0, "signed_contracts": 0, "current_price": 64865},
                "after": {"side": "SHORT", "contracts": 4, "signed_contracts": -4, "unrealized_pnl": 0},
                "fills": {"found": True, "avg_price": 64865},
                "net_realized_pnl_usd": None,
                "is_exit_trade": False,
                "trade_decision": {
                    "trade_intent": "NEW_ENTRY", "signal_reason": "CORE_IAF_SHORT_PHANTOM_CONFIRMED",
                    "confidence": {"direction": "SHORT", "score": 3, "threshold": 3, "confidence_pct": 58},
                },
            })
            message = sent[0][0]
            self.assertIn("LARRY NEW ENTRY", message)
            self.assertIn("Reason: SHORT score 3/4 ≥ 3/4 · confidence 58%", message)
            self.assertIn("Order: SELL 4", message)
            self.assertIn("Position: FLAT 0 → SHORT 4", message)
            self.assertIn("Remaining: SHORT 4", message)
        finally:
            larry.send_telegram_message = original_send
            larry.et_timestamp_short = original_timestamp


if __name__ == "__main__":
    unittest.main()
