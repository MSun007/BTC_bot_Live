import json
import unittest
import tempfile
import pathlib
import types
from unittest.mock import Mock
from datetime import datetime, timezone
from paper_health import assess_snapshot
from paper_runtime import CloudMirror
from paper_account import PaperAccount, PaperClient, PaperStore


class HealthTests(unittest.TestCase):
    def snapshot(self, now=1000, **heartbeat):
        ts = datetime.fromtimestamp(now, timezone.utc).isoformat()
        return {"mode": "PAPER", "published_at": ts, "pending_notifications": 0,
                "objects": {"coinbase_unified_heartbeat.json": json.dumps({"ts": ts, "state": "MONITORING", **heartbeat})}}

    def test_healthy(self):
        self.assertTrue(assess_snapshot(self.snapshot(), now=1050)["healthy"])

    def test_fresh_publication_cannot_hide_engine_error(self):
        self.assertEqual(assess_snapshot(self.snapshot(state="ERROR"), now=1050)["reason"], "engine_error")

    def test_fresh_publication_cannot_hide_old_heartbeat(self):
        s = self.snapshot()
        s["published_at"] = datetime.fromtimestamp(1500, timezone.utc).isoformat()
        self.assertEqual(assess_snapshot(s, now=1500)["reason"], "engine_heartbeat_stale")

    def test_pending_alerts_unhealthy(self):
        s = self.snapshot()
        s["pending_notifications"] = 1
        self.assertEqual(assess_snapshot(s, now=1000)["reason"], "notifications_pending")

    def test_future_clock_and_stale_publication_unhealthy(self):
        for now in (990, 1200):
            self.assertFalse(assess_snapshot(self.snapshot(), now=now)["healthy"])

    def test_missing_heartbeat_unhealthy(self):
        s = self.snapshot()
        s["objects"] = {}
        self.assertFalse(assess_snapshot(s, now=1000)["healthy"])

    def test_malformed_object_container_unhealthy(self):
        s = self.snapshot()
        s['objects'] = []
        self.assertEqual(assess_snapshot(s, now=1000)['reason'], 'invalid_snapshot')


class MirrorTests(unittest.TestCase):
    def test_replacement_during_download_retries_fresh_blob(self):
        from google.api_core.exceptions import NotFound, PreconditionFailed
        for error in (NotFound("old generation removed"), PreconditionFailed("generation changed")):
            first, second = Mock(generation=1), Mock(generation=2)
            first.download_as_text.side_effect = error
            second.download_as_text.return_value = '{"mode":"PAPER"}'
            mirror = object.__new__(CloudMirror)
            mirror.bucket = Mock()
            mirror.bucket.blob.side_effect = [first, second]
            self.assertEqual(mirror.read("snapshot"), ({"mode": "PAPER"}, 2))
            self.assertEqual(mirror.bucket.blob.call_count, 2)

    def test_missing_control_object_remains_missing(self):
        from google.api_core.exceptions import NotFound
        mirror = object.__new__(CloudMirror)
        mirror.bucket = Mock()
        mirror.bucket.blob.return_value.reload.side_effect = NotFound("missing")
        self.assertEqual(mirror.read("control"), (None, None))

    def test_persistent_replacement_fails_after_three_attempts(self):
        from google.api_core.exceptions import PreconditionFailed
        mirror = object.__new__(CloudMirror)
        mirror.bucket = Mock()
        mirror.bucket.blob.return_value.download_as_text.side_effect = PreconditionFailed("changed")
        with self.assertRaises(PreconditionFailed):
            mirror.read("snapshot")
        self.assertEqual(mirror.bucket.blob.call_count, 3)


class FailureDeliveryTests(unittest.TestCase):
    def test_failed_notification_is_retained_and_backed_off(self):
        import larry_perp_v1 as engine
        from paper_runtime import install_engine
        with tempfile.TemporaryDirectory() as tmp:
            store = PaperStore(pathlib.Path(tmp) / "state.sqlite3")
            try:
                account = PaperAccount(store, engine.PERP_PRODUCT_ID)
                e = types.SimpleNamespace(**vars(engine))
                send = Mock(return_value=False)
                e.send_telegram_message, e.SEND_TELEGRAM = send, True
                mirror = Mock()
                install_engine(e, (store, account, mirror))
                for _ in range(5):
                    e.send_telegram_message("test")
                e.publish_paper_status()
                e.publish_paper_status()
                self.assertEqual(send.call_count, 1)
                self.assertEqual(store.snapshot()["pending_notifications"], 5)
                self.assertEqual(mirror.write.call_count, 2)
            finally:
                store.db.close()

    def test_error_outbox_can_drain_without_a_successful_trading_cycle(self):
        import larry_perp_v1 as engine
        from paper_runtime import install_engine
        with tempfile.TemporaryDirectory() as tmp:
            store = PaperStore(pathlib.Path(tmp) / "state.sqlite3")
            try:
                account = PaperAccount(store, engine.PERP_PRODUCT_ID)
                mirror = Mock()
                send = Mock(return_value=True)
                e = types.SimpleNamespace(**vars(engine))
                e.send_telegram_message = send
                e.SEND_TELEGRAM = True
                install_engine(e, (store, account, mirror))
                e.send_telegram_message("Cycle failed", event_type="ERROR")
                self.assertEqual(store.snapshot()["pending_notifications"], 1)
                e.publish_paper_status()
                self.assertEqual(store.snapshot()["pending_notifications"], 0)
                send.assert_called_once_with("[PAPER] Cycle failed", event_type="ERROR")
                e.publish_paper_status()
                self.assertEqual(send.call_count, 1)
            finally:
                store.db.close()

    def test_publication_failure_does_not_undo_committed_fill(self):
        import larry_perp_v1 as engine
        from paper_runtime import install_engine
        with tempfile.TemporaryDirectory() as tmp:
            store = PaperStore(pathlib.Path(tmp) / "state.sqlite3")
            try:
                account = PaperAccount(store, engine.PERP_PRODUCT_ID)
                mirror = Mock()
                mirror.read.return_value = (None, None)
                mirror.write.side_effect = RuntimeError("publication unavailable")
                e = types.SimpleNamespace(**vars(engine))
                e.run_once = lambda cb, gcs: account.order("BUY", 1, "committed")
                store.write_json(e.UNIFIED_CAPITAL_STATE_BLOB, {"starting_combined_capital": 2000})
                market = Mock()
                market.get_product.return_value = {"price": "80000", "future_product_details": {"funding_rate": 0}}
                install_engine(e, (store, account, mirror))
                with self.assertRaisesRegex(RuntimeError, "publication unavailable"):
                    e.run_once(PaperClient(market, account), store)
                self.assertEqual(account.state()["signed"], 1)
                self.assertFalse(store.db.in_transaction)
            finally:
                store.db.close()
