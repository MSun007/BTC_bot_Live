"""Explicit new-run profiles; the existing $2,000 run is the default."""
import copy
import hashlib
import json

CONTRACT_FIELDS = (
    "MAX_CONVICTION_CONTRACTS", "MACRO_BLOCKED_PROBE_CONTRACTS",
    "SCORE3_PROBE_CONTRACTS", "PULLBACK_MAX_TARGET_CONTRACTS",
    "INITIAL_ENTRY_MAX_CONTRACTS", "ADD_MAX_CONTRACTS",
    "NEUTRAL_REGIME_MAX_CONTRACTS", "REVERSAL_PROBE_CONTRACTS",
)
DOLLAR_FIELDS = ("DAILY_NET_LOSS_LIMIT_USD", "MIN_FUTURES_EQUITY_BUFFER_USD")


def canonical_digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def profile(base, name="2k"):
    if name not in ("2k", "10k"):
        raise ValueError("PAPER_PROFILE must be 2k or 10k")
    cfg = copy.deepcopy(base)
    multiplier = 5 if name == "10k" else 1
    if multiplier != 1:
        for key in CONTRACT_FIELDS + DOLLAR_FIELDS:
            cfg[key] *= multiplier
        cfg["POSITION_SIZE_LADDER"] = [n * multiplier for n in cfg["POSITION_SIZE_LADDER"]]
        cfg["CONFIG_VERSION"] = "v48_paper_10k_x5"
        cfg["CONFIG_NOTE"] = "New $10,000 paper run: 5x quantities and dollar limits; unchanged percentage risk and signal rules."
    return cfg, 2000.0 * multiplier
