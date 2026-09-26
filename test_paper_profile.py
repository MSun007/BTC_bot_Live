import json
import pathlib
import tempfile
import unittest
import importlib.util
import os
import sys
from unittest.mock import patch
from paper_profile import profile, CONTRACT_FIELDS, DOLLAR_FIELDS
from paper_account import PaperAccount, PaperStore


class ProfileTests(unittest.TestCase):
    def test_engine_import_accepts_ten_thousand_profile_integrity(self):
        path = pathlib.Path(__file__).with_name("larry_perp_v1.py")
        spec = importlib.util.spec_from_file_location("ten_thousand_paper_engine", path)
        engine = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = engine
        try:
            with patch.dict(os.environ, {"PAPER_PROFILE": "10k"}):
                spec.loader.exec_module(engine)
            from paper_profile import canonical_digest
            base = json.loads(path.with_name("strategy_config.json").read_text())
            cfg, _ = profile(base, "10k")
            self.assertEqual(engine.EXPECTED_CONFIG_VERSION, "v48_paper_10k_x5")
            self.assertEqual(engine.EXPECTED_CONFIG_SHA256, canonical_digest(cfg))
        finally:
            sys.modules.pop(spec.name, None)

    def test_scaling_preserves_percentage_rules_and_scales_all_limits(self):
        base = json.loads(pathlib.Path(__file__).with_name("strategy_config.json").read_text())
        scaled, capital = profile(base, "10k")
        self.assertEqual(capital, 10000)
        self.assertEqual(scaled["POSITION_SIZE_LADDER"], [20, 30, 50, 75, 100])
        for key in CONTRACT_FIELDS + DOLLAR_FIELDS:
            self.assertEqual(scaled[key], base[key] * 5)
        for key in ("MAX_EFFECTIVE_LEVERAGE", "CONTRACT_SIZE_BTC", "ATR_STOP_MULTIPLIER", "ENTRY_MIN_SCORE", "TP1_R_MULTIPLE", "LOSS_STREAK_LIMIT", "MIN_ENTRY_COOLDOWN_SECONDS"):
            self.assertEqual(scaled[key], base[key])
        self.assertEqual(base["MAX_CONVICTION_CONTRACTS"], 20)

    def test_existing_profile_unchanged(self):
        self.assertEqual(profile({"x": [1]}, "2k"), ({"x": [1]}, 2000))

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ValueError):
            profile({}, "live")

    def test_five_times_fills_have_same_return_and_five_times_costs(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = []
            states = []
            try:
                for multiplier in (1, 5):
                    store = PaperStore(pathlib.Path(tmp) / f"{multiplier}.sqlite3")
                    stores.append(store)
                    account = PaperAccount(store, "BIP", fee_bps=6, fee_per_contract=.12, leverage=3, starting_equity=2000 * multiplier)
                    account.observe(80000, 0)
                    store.begin()
                    account.order("BUY", 2 * multiplier, "entry")
                    store.commit()
                    account.observe(79000, 0)
                    store.begin()
                    account.order("SELL", 2 * multiplier, "exit")
                    store.commit()
                    states.append(account.state())
                for key in ("gross", "fees", "funding"):
                    self.assertAlmostEqual(states[1][key], states[0][key] * 5)
                for key in ("gross", "fees"):
                    self.assertAlmostEqual(states[0][key] / 2000, states[1][key] / 10000)
                with self.assertRaisesRegex(RuntimeError, "changed mid-run"):
                    PaperAccount(stores[0], "BIP", fee_bps=6, fee_per_contract=.12, leverage=3, starting_equity=10000)
            finally:
                for store in stores:
                    store.db.close()
