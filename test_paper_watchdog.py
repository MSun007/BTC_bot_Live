import unittest
import tempfile
import pathlib
import json
import os
from paper_watchdog import transition, inspect
from unittest.mock import Mock, patch

class WatchdogTests(unittest.TestCase):
    def test_initial_healthy_is_quiet(self):
        state, message = transition({}, {"healthy": True, "reason": "ok"}, now=100)
        self.assertEqual(state["status"], "OK")
        self.assertIsNone(message)

    def test_transient_failure_and_single_recovery_do_not_flap(self):
        state = {"status": "OK"}
        failure = {"healthy": False, "reason": "dashboard_http_503"}
        state, message = transition(state, failure, now=100)
        self.assertIsNone(message)
        state, message = transition(state, failure, now=160)
        self.assertIn("ERROR", message)
        state, message = transition(state, {"healthy": True, "reason": "ok"}, now=220)
        self.assertIsNone(message)
        state, message = transition(state, {"healthy": True, "reason": "ok"}, now=280)
        self.assertIn("RECOVERED", message)

    def test_stale_engine_alerts_immediately(self):
        state, message = transition({"status": "OK"}, {"healthy": False, "reason": "engine_heartbeat_stale", "heartbeat_age_seconds": 250}, now=100)
        self.assertIn("250s", message)
        self.assertEqual(state["status"], "ERROR")

    def test_hourly_reminder_only(self):
        bad = {"healthy": False, "reason": "engine_error"}
        state = {"status": "ERROR", "last_alert_at": 100}
        state, message = transition(state, bad, now=200)
        self.assertIsNone(message)
        _, message = transition(state, bad, now=3700)
        self.assertIn("STILL UNHEALTHY", message)

    def test_dashboard_200_with_unhealthy_body_is_not_ok(self):
        mirror = Mock()
        mirror.read.return_value = ({}, 1)
        response = Mock(status_code=200)
        response.json.return_value = {"healthy": False, "reason": "engine_error"}
        with patch('paper_watchdog.assess_snapshot', return_value={"healthy": True, "reason": "ok"}), patch('requests.get', return_value=response):
            self.assertFalse(inspect(mirror, 'https://example.test')["healthy"])

    def test_read_failure_does_not_leak_exception_details(self):
        mirror = Mock()
        mirror.read.side_effect = RuntimeError("secret")
        self.assertEqual(inspect(mirror)["reason"], "monitor_read_failed_RuntimeError")

    def test_checks_separated_by_long_outage_are_not_consecutive(self):
        previous = {"status": "OK", "candidate": "ERROR", "streak": 1, "checked_at": 100}
        state, message = transition(previous, {"healthy": False, "reason": "engine_error"}, now=2000)
        self.assertIsNone(message)
        self.assertEqual(state["streak"], 1)

    def test_quiet_check_persists_sample_without_sending(self):
        from paper_watchdog import main
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'watchdog.json'
            with patch.dict(os.environ, {'PAPER_WATCHDOG_CLOUD': 'false', 'PAPER_WATCHDOG_STATE': str(path), 'BTC_LOG_BUCKET': 'paper-test'}), patch('paper_watchdog.CloudMirror'), patch('paper_watchdog.inspect', return_value={'healthy': True, 'reason': 'ok'}), patch('paper_watchdog.notify') as send:
                main()
                send.assert_not_called()
                self.assertEqual(json.loads(path.read_text())['status'], 'OK')

    def test_failed_delivery_does_not_acknowledge_alert(self):
        from paper_watchdog import main
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'watchdog.json'
            path.write_text(json.dumps({'status': 'OK'}))
            with patch.dict(os.environ, {'PAPER_WATCHDOG_CLOUD': 'false', 'PAPER_WATCHDOG_STATE': str(path), 'BTC_LOG_BUCKET': 'paper-test'}), patch('paper_watchdog.CloudMirror'), patch('paper_watchdog.inspect', return_value={'healthy': False, 'reason': 'engine_heartbeat_stale'}), patch('paper_watchdog.notify', side_effect=RuntimeError('secret')):
                with self.assertRaises(SystemExit):
                    main()
                self.assertEqual(json.loads(path.read_text()), {'status': 'OK'})
