#!/usr/bin/env python3
"""
Larry Perp v12 Clean — Coinbase Spot + Perp Portfolio Engine
==========================================================
Exchange  : Coinbase Advanced Trade / FCM INTX-style BTC Perp product
Product   : BIP-20DEC30-CDE
Framework : IAF Layer 3 — Risk Management First / Target Net Exposure Engine

CHANGELOG v11 -> v12
--------------------
1.  ATR_AT_ENTRY     - ATR stop is now LOCKED at entry, not recomputed each cycle.
2.  TP1_LADDER_STEPDOWN - Ladder-based take-profit. Trigger percentage tightens as
                       size grows; reduction steps down to the next lower whole-contract
                       ladder rung, keeping a one-contract runner where possible.
3.  CANDLE_CLOSE     - Phantom EXTENSION_CONFIRMED -> CONFIRMED_ENTRY now requires
                       the latest CLOSED candle to confirm the armed probe; phantom extension is tracked as a sizing/add-on signal
                       price (no more tick-noise confirmations).
4.  SCORE_SIZED      - Score 4 => CONTRACTS_PER_TRADE_FULL (default 4),
                       Score 3 => CONTRACTS_PER_TRADE_PARTIAL (default 2).
5.  FUNDING_SIZED    - If funding is adverse but below gate, target size is reduced
                       by one tier (e.g. 4 -> 2) instead of binary block.
6.  PARAM_RECONCILED - short post-stop risk cooldown; STREAK_PAUSE_MINUTES=15 by default.
7.  GCS_CAS          - strategy_config.json writes use If-Generation-Match (CAS).
8.  PER_DIR_COOLDOWN - perp_last_long_entry_at and perp_last_short_entry_at are
                       tracked separately; opposite-direction signals are not
                       suppressed by same-side cooldown.
9.  EMAIL_KEY_WIRED  - SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL now configurable.
10. KILL_SWITCH      - Bot reads gs://btc_trade_log/bot_halt.json each cycle. If
                       {"halt": true} all order placement is skipped.
11. CANDLE_CACHE     - Candles cached by (product, granularity, bucket); fetched
                       only when the latest bucket has rolled.
12. SIGNAL_PNL       - Trade ledger gains signal_class + slippage_bps columns for
                       per-signal-class P&L rollups (see analyze_signal_pnl.py).

CHANGELOG v29 -> v30 (security/correctness audit fixes)
--------------------------------------------------------
1.  CONFIG_WIRING_FIX - apply_strategy_config() declared 13 dashboard-configurable
                       globals (signal arm/commit/cancel scores, reversal probe
                       tuning, signal lock settings) but never assigned most of them;
                       dashboard edits to these were silently dropped. Fixed.
2.  FLATTEN_HMAC      - Emergency flatten requests from the dashboard are now
                       HMAC-signed with the EMERGENCY_FLATTEN_PIN secret and verified
                       here before execution. Previously any GCS object found with
                       status=="REQUESTED" was trusted with no cryptographic link
                       back to a PIN-verified dashboard action.
3.  FLATTEN_CAS       - The flatten request claim (REQUESTED -> IN_PROGRESS) now uses
                       the existing (previously unused) write_json_cas/If-Generation-
                       Match helpers so two processes can't both execute one request.
4.  FLATTEN_RESUME    - A flatten stuck at IN_PROGRESS for >90s (VM restarted mid-
                       execution) is now detected and resumed on the next cycle
                       instead of being stuck forever.
5.  FLATTEN_FAIL_CLOSED - A missing/unparsable requested_at used to *skip* the expiry
                       check (fail-open); now treated as an immediate rejection.
6.  NAIVE_DATETIME_FIX - parse_dt()/_parse_iso_utc_seconds() now assume UTC for a
                       timestamp with no offset, instead of local-VM-timezone.
7.  SPOT_COOLDOWN_FIX - maybe_handle_spot_entry() called cooldown_status() with a
                       throwaway dict instead of the real engine state, so the spot
                       ladder's entry cooldown never actually applied. Fixed.
8.  LOSS_STREAK_RESET - loss_streak was only ever incremented, never reset, so once
                       LOSS_STREAK_LIMIT stop-outs had ever occurred, every future
                       stop re-triggered the pause. Now resets daily and on a
                       profit-take exit.
9.  CORE_SCORE4_TOGGLE - CORE_SCORE4_IMMEDIATE_ENTRY was read via globals().get(...)
                       with no real variable behind it (always True, unconfigurable).
                       Now a first-class, dashboard-editable setting.
10. STREAK_PAUSE_SSOT - STREAK_PAUSE_HOURS is now the single source of truth for
                       pause length; STREAK_PAUSE_MINUTES is always derived from it
                       (previously editing only "hours" on the dashboard could have
                       no effect on the actual pause duration).

CHANGELOG v30 -> v31
--------------------
1.  GUARD_RESIZE      - portfolio_guard_resize_target() replaces the binary
                       leverage guard. Oversized targets are clamped to the largest
                       whole-contract size within MAX_EFFECTIVE_LEVERAGE instead of
                       blocking the entire trade (previously a full-conviction signal
                       on a small account traded NOTHING). Equity buffer remains a
                       hard block; an add is refused when the live position already
                       sits at/over the cap (entry paths never force-reduce).
2.  DECISION_LOG      - signal_decision_log_YYYYMMDD.jsonl (GCS, daily-partitioned):
                       every phantom FSM transition (ARMED / EXTENSION_ACHIEVED /
                       COMMITTED / CANCELLED / FUNDING_BLOCKED) plus core EXECUTED /
                       EXECUTION_FAILED / EXECUTION_BLOCKED events, each with the
                       full indicator/regime/parameter context and a setup_id that
                       joins arm -> commit -> execution. This is the training and
                       backtest dataset for the self-learning roadmap. All capture
                       paths are fail-soft and can never block trading.

IMPORTANT
---------
Coinbase futures positions are NETTED. A SELL while LONG reduces the long; it
does not create a separate short. The bot always trades toward a TARGET NET
POSITION after reading live Coinbase position state.

Core IAF Rules Preserved
------------------------
1. SIZE FIXED-STEP — partial (2) or full (4) contracts per signal-conviction tier.
2. ATR STOP       — Stop = entry +/- ATR_at_entry * 1.5. Fixed at entry.
3. LATE TSL       — Default activation +1.5%, trail 0.5%. Configurable.
4. DAILY UMBRELLA — 3 stop-loss hits per day halts new entries.
5. STREAK PAUSE   — 3 consecutive losses pauses entries for 12h.
6. CANDLE PROBE  — signal -> arm -> next closed candle confirms probe; extension upgrades sizing.
7. FUNDING GATE   — Skip longs if funding > +0.1%; skip shorts if funding < -0.1%.
                    Reduce size by one tier if funding is half-way adverse.
8. BIDIRECTIONAL  — Long oversold and Short overbought.
9. KILL SWITCH    — bot_halt.json (GCS) immediately suspends order placement.

Dashboard Outputs
-----------------
- gs://btc_trade_log/perp_engine_state.json
- gs://btc_trade_log/coinbase_unified_heartbeat.json
- gs://btc_trade_log/perp_heartbeat.json  (legacy compatibility)
- gs://btc_trade_log/perp_position_state.json

Environment / Secrets
---------------------
Expected Secret Manager secrets:
- COINBASE_API_KEY
- COINBASE_SECRET
- EMAIL_PASSWORD optional

Dry Run
-------
Set DRY_RUN = True below for paper execution / dashboard telemetry only.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import logging
import math
import os
import smtplib
import statistics
import subprocess
import tempfile
import time
import uuid
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

try:
    from google.cloud import secretmanager, storage
except Exception:  # keeps py_compile friendly if packages unavailable outside bot env
    secretmanager = None
    storage = None

try:
    from coinbase.rest import RESTClient
except Exception:
    RESTClient = None

try:
    from cryptography.hazmat.primitives.serialization import (
        load_der_private_key,
        Encoding,
        PrivateFormat,
        NoEncryption,
    )
except Exception:
    load_der_private_key = None
    Encoding = PrivateFormat = NoEncryption = None

# =============================================================================
# CONFIG
# =============================================================================

PROJECT_ID = os.getenv("PROJECT_ID", "btc-bot-v1-live")
BUCKET_NAME = os.getenv("BTC_LOG_BUCKET", "btc_trade_log")

PERP_PRODUCT_ID = os.getenv("PERP_PRODUCT_ID", "BIP-20DEC30-CDE")
SPOT_PRODUCT_ID = os.getenv("SPOT_PRODUCT_ID", "BTC-USDC")
SPOT_FALLBACK_PRODUCT_ID = os.getenv("SPOT_FALLBACK_PRODUCT_ID", "BTC-USD")

CONTRACT_SIZE_BTC = float(os.getenv("CONTRACT_SIZE_BTC", "0.01"))
# Confidence-weighted sizing (v13). Full conviction is operator-defined.
# User's current full-size risk unit: 10 micro contracts = 0.10 BTC notional.
MAX_CONVICTION_CONTRACTS = int(os.getenv("MAX_CONVICTION_CONTRACTS", "10"))
PROBE_PCT = float(os.getenv("PROBE_PCT", "0.20"))
PARTIAL_PCT = float(os.getenv("PARTIAL_PCT", "0.40"))
STRONG_PCT = float(os.getenv("STRONG_PCT", "0.70"))

def derived_contract_tier(max_contracts: int, pct: float) -> int:
    try:
        return max(1, int(round(float(max_contracts) * float(pct))))
    except Exception:
        return 1

def derived_sizing_ladder(max_contracts: int) -> Dict[str, int]:
    """Return whole-contract probe/partial/strong/full ladder from Max Conviction."""
    max_c = max(1, int(max_contracts or MAX_CONVICTION_CONTRACTS))
    probe = derived_contract_tier(max_c, PROBE_PCT)
    partial = derived_contract_tier(max_c, PARTIAL_PCT)
    strong = derived_contract_tier(max_c, STRONG_PCT)
    if max_c >= 4:
        probe = max(1, min(probe, max_c - 3))
        partial = max(probe + 1, min(partial, max_c - 2))
        strong = max(partial + 1, min(strong, max_c - 1))
    elif max_c == 3:
        probe, partial, strong = 1, 2, 2
    else:
        probe = partial = strong = 1
    return {"probe": int(probe), "partial": int(partial), "strong": int(strong), "full": int(max_c)}

CONTRACTS_PER_TRADE_FULL = int(os.getenv("CONTRACTS_PER_TRADE_FULL", str(MAX_CONVICTION_CONTRACTS)))
CONTRACTS_PER_TRADE_PARTIAL = int(os.getenv("CONTRACTS_PER_TRADE_PARTIAL", str(derived_contract_tier(MAX_CONVICTION_CONTRACTS, PARTIAL_PCT))))
CONTRACTS_PER_TRADE_PROBE = int(os.getenv("CONTRACTS_PER_TRADE_PROBE", str(derived_contract_tier(MAX_CONVICTION_CONTRACTS, PROBE_PCT))))
# Backwards-compat constant: bridge layer default add size.
CONTRACTS_PER_TRADE = int(os.getenv("CONTRACTS_PER_TRADE", str(CONTRACTS_PER_TRADE_PROBE)))
SCORE4_MACRO_OVERRIDE_ENABLED = os.getenv("SCORE4_MACRO_OVERRIDE_ENABLED", "true").lower() in ("1", "true", "yes")
MACRO_BLOCKED_PROBE_CONTRACTS = int(os.getenv("MACRO_BLOCKED_PROBE_CONTRACTS", str(CONTRACTS_PER_TRADE_PROBE)))
# v14 progressive add-on / pyramiding controls. The bot trades to a confidence-derived
# TARGET size, so a probe can become partial/full without repeatedly adding the
# same signal. Example: current LONG 2, confidence target LONG 4 => BUY 2.
PROGRESSIVE_ADD_ONS_ENABLED = os.getenv("PROGRESSIVE_ADD_ONS_ENABLED", "true").lower() in ("1", "true", "yes")
MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD = int(os.getenv("MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD", "10"))
MAX_POSITION_ADDS = int(os.getenv("MAX_POSITION_ADDS", "3"))
# v15 signal commitment controls: prevent a moving-target setup from never executing.
SIGNAL_LOCK_ENABLED = os.getenv("SIGNAL_LOCK_ENABLED", "true").lower() in ("1", "true", "yes")
SIGNAL_VALIDITY_MINUTES = int(os.getenv("SIGNAL_VALIDITY_MINUTES", "20"))
SIGNAL_CANCEL_SCORE = int(os.getenv("SIGNAL_CANCEL_SCORE", "1"))
SIGNAL_HYSTERESIS_ARM_SCORE = int(os.getenv("SIGNAL_HYSTERESIS_ARM_SCORE", "3"))
SIGNAL_COMMIT_ON_CLOSED_CANDLE = os.getenv("SIGNAL_COMMIT_ON_CLOSED_CANDLE", "true").lower() in ("1", "true", "yes")
FREEZE_CONFIDENCE_ON_ARM = os.getenv("FREEZE_CONFIDENCE_ON_ARM", "true").lower() in ("1", "true", "yes")
# v16: let setups arm earlier but only commit after phantom + closed-candle confirmation.
# Reversal probes are capped at small size and tracked separately from CORE entries.
SIGNAL_ARM_SCORE = int(os.getenv("SIGNAL_ARM_SCORE", "2"))
SIGNAL_COMMIT_SCORE = int(os.getenv("SIGNAL_COMMIT_SCORE", "3"))
# v30: was previously read via globals().get(...) with no real module variable behind
# it, so it could never be anything but True. Now a first-class, config-editable toggle.
CORE_SCORE4_IMMEDIATE_ENTRY = os.getenv("CORE_SCORE4_IMMEDIATE_ENTRY", "true").lower() in ("1", "true", "yes")
REVERSAL_PROBE_ENABLED = os.getenv("REVERSAL_PROBE_ENABLED", "true").lower() in ("1", "true", "yes")
REVERSAL_PROBE_CONTRACTS = int(os.getenv("REVERSAL_PROBE_CONTRACTS", str(CONTRACTS_PER_TRADE_PROBE)))
REVERSAL_NEAR_BB_PCT = float(os.getenv("REVERSAL_NEAR_BB_PCT", "0.003"))
REVERSAL_RSI_SOFT_LONG_MAX = float(os.getenv("REVERSAL_RSI_SOFT_LONG_MAX", "40"))
REVERSAL_RSI_SOFT_SHORT_MIN = float(os.getenv("REVERSAL_RSI_SOFT_SHORT_MIN", "60"))

ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_MULTIPLIER = float(os.getenv("ATR_MULTIPLIER", "1.5"))
# v12 defaults reflect the live config: 1.5% activation, 0.5% trail.
TSL_ACTIVATION_PCT = float(os.getenv("TSL_ACTIVATION_PCT", "0.015"))
TSL_TRAIL_PCT = float(os.getenv("TSL_TRAIL_PCT", "0.005"))
# v24 TP1: ladder-based profit taking.
# TP1 no longer sells a percentage that can create fractional contract math.
# It steps exposure down to the next lower rounded ladder rung. Trigger % gets
# faster as position size increases.
TP1_PCT = float(os.getenv("TP1_PCT", "0.0075"))  # legacy fallback / probe-tier default
TP1_FRACTION = float(os.getenv("TP1_FRACTION", "0.5"))  # legacy only; retained for config compatibility
TP1_DYNAMIC_BY_LADDER = os.getenv("TP1_DYNAMIC_BY_LADDER", "true").lower() in ("1", "true", "yes")
TP1_PROBE_TRIGGER_PCT = float(os.getenv("TP1_PROBE_TRIGGER_PCT", "0.0075"))
TP1_PARTIAL_TRIGGER_PCT = float(os.getenv("TP1_PARTIAL_TRIGGER_PCT", "0.0075"))
TP1_STRONG_TRIGGER_PCT = float(os.getenv("TP1_STRONG_TRIGGER_PCT", "0.0060"))
TP1_FULL_TRIGGER_PCT = float(os.getenv("TP1_FULL_TRIGGER_PCT", "0.0050"))
# v33 adaptive exit architecture. The 1.5x ATR stop remains the firm, final
# protection. These controls can reduce risk sooner when several independent
# pieces of adverse evidence agree. Pivot and stop-blown classification starts
# in shadow mode; it cannot re-enter or reverse a position.
TP1_USE_R_MULTIPLE = os.getenv("TP1_USE_R_MULTIPLE", "true").lower() in ("1", "true", "yes")
TP1_R_MULTIPLE = float(os.getenv("TP1_R_MULTIPLE", "0.75"))
ADAPTIVE_DEFENSE_ENABLED = os.getenv("ADAPTIVE_DEFENSE_ENABLED", "true").lower() in ("1", "true", "yes")
ADAPTIVE_REDUCE_SCORE = int(os.getenv("ADAPTIVE_REDUCE_SCORE", "65"))
ADAPTIVE_EXIT_SCORE = int(os.getenv("ADAPTIVE_EXIT_SCORE", "85"))
ADAPTIVE_CONFIRM_CYCLES = int(os.getenv("ADAPTIVE_CONFIRM_CYCLES", "2"))
ADAPTIVE_REENTRY_COOLDOWN_MINUTES = int(os.getenv("ADAPTIVE_REENTRY_COOLDOWN_MINUTES", "15"))
ADAPTIVE_FRESH_SETUP_REQUIRED = os.getenv("ADAPTIVE_FRESH_SETUP_REQUIRED", "true").lower() in ("1", "true", "yes")
ADAPTIVE_REENTRY_PROBE_ONLY = os.getenv("ADAPTIVE_REENTRY_PROBE_ONLY", "true").lower() in ("1", "true", "yes")
ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND = os.getenv(
    "ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND", "true"
).lower() in ("1", "true", "yes")
SWING_PIVOT_ENABLED = os.getenv("SWING_PIVOT_ENABLED", "true").lower() in ("1", "true", "yes")
SWING_PIVOT_LEFT_BARS = int(os.getenv("SWING_PIVOT_LEFT_BARS", "2"))
SWING_PIVOT_RIGHT_BARS = int(os.getenv("SWING_PIVOT_RIGHT_BARS", "2"))
STOP_BLOWN_SHADOW_MODE = os.getenv("STOP_BLOWN_SHADOW_MODE", "true").lower() in ("1", "true", "yes")
PHANTOM_EXTENSION_PCT = float(os.getenv("PHANTOM_EXTENSION_PCT", "0.005"))
FUNDING_LONG_MAX = float(os.getenv("FUNDING_LONG_MAX", "0.001"))
FUNDING_SHORT_MIN = float(os.getenv("FUNDING_SHORT_MIN", "-0.001"))
# Half-way-adverse funding: above this absolute value but below the gate,
# the bot still trades but at the PARTIAL tier (one tier down).
FUNDING_SIZE_REDUCE_AT = float(os.getenv("FUNDING_SIZE_REDUCE_AT", "0.0005"))

DAILY_STOP_LIMIT = int(os.getenv("DAILY_STOP_LIMIT", "3"))
LOSS_STREAK_LIMIT = int(os.getenv("LOSS_STREAK_LIMIT", "3"))
STREAK_PAUSE_HOURS = float(os.getenv("STREAK_PAUSE_HOURS", "0.25"))
STREAK_PAUSE_MINUTES = float(os.getenv("STREAK_PAUSE_MINUTES", str(STREAK_PAUSE_HOURS * 60)))

CANDLE_GRANULARITY = os.getenv("CANDLE_GRANULARITY", "ONE_HOUR")
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "300"))
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "60"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "60"))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
ENABLE_CORE_PERP_ENTRIES = os.getenv("ENABLE_CORE_PERP_ENTRIES", "true").lower() in ("1", "true", "yes")
ENABLE_SPOT_BRIDGE_PERP_BUYS = os.getenv("ENABLE_SPOT_BRIDGE_PERP_BUYS", "true").lower() in ("1", "true", "yes")
# Safety default: the bot observes manual/external perp positions but does NOT
# place ATR/TSL exits, add-ons, flips, or flattening trades against them.
# Allowed values: monitor_only, full_management
MANUAL_POSITION_MODE = os.getenv("MANUAL_POSITION_MODE", "monitor_only").strip().lower()

# Portfolio/risk orchestration
MIN_ENTRY_COOLDOWN_SECONDS = int(os.getenv("MIN_ENTRY_COOLDOWN_SECONDS", "300"))  # original 5-minute cooldown
SPOT_TRANCHE_TARGETS_PCT = [25, 33, 50, 90]  # original Spot ladder cumulative deployment targets
ENABLE_SPOT_BTC_TRADING = os.getenv("ENABLE_SPOT_BTC_TRADING", "true").lower() in ("1", "true", "yes")
SPOT_MIN_ENTRY_SCORE = int(os.getenv("SPOT_MIN_ENTRY_SCORE", "3"))
SPOT_FULL_SCORE = int(os.getenv("SPOT_FULL_SCORE", "4"))
SPOT_MIN_ORDER_USD = float(os.getenv("SPOT_MIN_ORDER_USD", "10"))
MACRO_FAST_SMA = int(os.getenv("MACRO_FAST_SMA", "50"))
MACRO_SLOW_SMA = int(os.getenv("MACRO_SLOW_SMA", "200"))
MIN_FUTURES_EQUITY_BUFFER_USD = float(os.getenv("MIN_FUTURES_EQUITY_BUFFER_USD", "1000"))
MAX_EFFECTIVE_LEVERAGE = float(os.getenv("MAX_EFFECTIVE_LEVERAGE", "3.0"))

# Signal thresholds for core bidirectional perp engine
RSI_LONG_MAX = float(os.getenv("PERP_RSI_LONG_MAX", "30"))
RSI_SHORT_MIN = float(os.getenv("PERP_RSI_SHORT_MIN", "70"))
STOCH_LONG_MAX = float(os.getenv("PERP_STOCH_LONG_MAX", "0.2"))
STOCH_SHORT_MIN = float(os.getenv("PERP_STOCH_SHORT_MIN", "0.8"))
VOL_SPIKE_MIN = float(os.getenv("PERP_VOL_SPIKE_MIN", "1.2"))

# Optional email
EMAIL_FROM = os.getenv("EMAIL_FROM", "lockinlarry2@gmail.com")
EMAIL_TO = os.getenv("EMAIL_TO", "lockinlarry2@gmail.com")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SEND_EMAIL = os.getenv("SEND_EMAIL", "true").lower() in ("1", "true", "yes")
EMAIL_INCLUDE_RAW_ORDER = os.getenv("EMAIL_INCLUDE_RAW_ORDER", "false").lower() in ("1", "true", "yes")
FILL_LOOKBACK_LIMIT = int(os.getenv("FILL_LOOKBACK_LIMIT", "75"))
SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL = os.getenv(
    "SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL", "true"
).lower() in ("1", "true", "yes")

# Optional Telegram push alerts
SEND_TELEGRAM = os.getenv("SEND_TELEGRAM", "true").lower() in ("1", "true", "yes")
TELEGRAM_INCLUDE_ERRORS = os.getenv("TELEGRAM_INCLUDE_ERRORS", "true").lower() in ("1", "true", "yes")
TELEGRAM_DAILY_SUMMARY_ENABLED = os.getenv("TELEGRAM_DAILY_SUMMARY_ENABLED", "true").lower() in ("1", "true", "yes")
TELEGRAM_DAILY_SUMMARY_HOUR_ET = int(os.getenv("TELEGRAM_DAILY_SUMMARY_HOUR_ET", "21"))

# GCS object names
ENGINE_STATE_BLOB = "perp_engine_state.json"
UNIFIED_HEARTBEAT_BLOB = "coinbase_unified_heartbeat.json"
LEGACY_HEARTBEAT_BLOB = "perp_heartbeat.json"
PERP_POSITION_STATE_BLOB = "perp_position_state.json"
PERP_TRADES_LEDGER_BLOB = "perp_trades_ledger.csv"
SPOT_POSITION_STATE_BLOB = "coinbase_spot_position_state.json"
SPOT_TRADES_LEDGER_BLOB = "coinbase_spot_trades_ledger.csv"
MANUAL_POSITION_EVENTS_BLOB = "manual_position_events.csv"
UNIFIED_CAPITAL_STATE_BLOB = "unified_capital_state.json"
STRATEGY_CONFIG_BLOB = "strategy_config.json"
BOT_HALT_BLOB = "bot_halt.json"  # v12 kill switch (operator-toggleable)
EMERGENCY_FLATTEN_REQUEST_BLOB = "emergency_flatten_request.json"  # v29 dashboard PIN -> VM execution request


DEFAULT_STRATEGY_CONFIG = {
    "CONFIG_VERSION": "v17_telegram_alerts",
    "CONFIG_NOTE": "v17/v25: ladder TP profit-taking plus Telegram trade/system alerts, daily summary, and dashboard-controlled alert toggles.",
    "CONTRACT_SIZE_BTC": CONTRACT_SIZE_BTC,
    "MAX_CONVICTION_CONTRACTS": MAX_CONVICTION_CONTRACTS,
    "PROBE_PCT": PROBE_PCT,
    "PARTIAL_PCT": PARTIAL_PCT,
    "STRONG_PCT": STRONG_PCT,
    "CONTRACTS_PER_TRADE": CONTRACTS_PER_TRADE,
    "CONTRACTS_PER_TRADE_FULL": CONTRACTS_PER_TRADE_FULL,
    "CONTRACTS_PER_TRADE_PARTIAL": CONTRACTS_PER_TRADE_PARTIAL,
    "CONTRACTS_PER_TRADE_PROBE": CONTRACTS_PER_TRADE_PROBE,
    "SCORE4_MACRO_OVERRIDE_ENABLED": SCORE4_MACRO_OVERRIDE_ENABLED,
    "MACRO_BLOCKED_PROBE_CONTRACTS": MACRO_BLOCKED_PROBE_CONTRACTS,
    "PROGRESSIVE_ADD_ONS_ENABLED": PROGRESSIVE_ADD_ONS_ENABLED,
    "MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD": MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD,
    "MAX_POSITION_ADDS": MAX_POSITION_ADDS,
    "SIGNAL_LOCK_ENABLED": SIGNAL_LOCK_ENABLED,
    "SIGNAL_VALIDITY_MINUTES": SIGNAL_VALIDITY_MINUTES,
    "SIGNAL_CANCEL_SCORE": SIGNAL_CANCEL_SCORE,
    "SIGNAL_HYSTERESIS_ARM_SCORE": SIGNAL_HYSTERESIS_ARM_SCORE,
    "SIGNAL_COMMIT_ON_CLOSED_CANDLE": SIGNAL_COMMIT_ON_CLOSED_CANDLE,
    "FREEZE_CONFIDENCE_ON_ARM": FREEZE_CONFIDENCE_ON_ARM,
    "SIGNAL_ARM_SCORE": SIGNAL_ARM_SCORE,
    "SIGNAL_COMMIT_SCORE": SIGNAL_COMMIT_SCORE,
    "CORE_SCORE4_IMMEDIATE_ENTRY": CORE_SCORE4_IMMEDIATE_ENTRY,
    "REVERSAL_PROBE_ENABLED": REVERSAL_PROBE_ENABLED,
    "REVERSAL_PROBE_CONTRACTS": REVERSAL_PROBE_CONTRACTS,
    "REVERSAL_NEAR_BB_PCT": REVERSAL_NEAR_BB_PCT,
    "REVERSAL_RSI_SOFT_LONG_MAX": REVERSAL_RSI_SOFT_LONG_MAX,
    "REVERSAL_RSI_SOFT_SHORT_MIN": REVERSAL_RSI_SOFT_SHORT_MIN,
    "ATR_PERIOD": ATR_PERIOD,
    "ATR_STOP_MULTIPLIER": ATR_MULTIPLIER,
    "TSL_ACTIVATION_PCT": TSL_ACTIVATION_PCT,
    "TSL_TRAIL_PCT": TSL_TRAIL_PCT,
    "TP1_PCT": TP1_PCT,
    "TP1_FRACTION": TP1_FRACTION,
    "TP1_DYNAMIC_BY_LADDER": TP1_DYNAMIC_BY_LADDER,
    "TP1_PROBE_TRIGGER_PCT": TP1_PROBE_TRIGGER_PCT,
    "TP1_PARTIAL_TRIGGER_PCT": TP1_PARTIAL_TRIGGER_PCT,
    "TP1_STRONG_TRIGGER_PCT": TP1_STRONG_TRIGGER_PCT,
    "TP1_FULL_TRIGGER_PCT": TP1_FULL_TRIGGER_PCT,
    "TP1_USE_R_MULTIPLE": TP1_USE_R_MULTIPLE,
    "TP1_R_MULTIPLE": TP1_R_MULTIPLE,
    "ADAPTIVE_DEFENSE_ENABLED": ADAPTIVE_DEFENSE_ENABLED,
    "ADAPTIVE_REDUCE_SCORE": ADAPTIVE_REDUCE_SCORE,
    "ADAPTIVE_EXIT_SCORE": ADAPTIVE_EXIT_SCORE,
    "ADAPTIVE_CONFIRM_CYCLES": ADAPTIVE_CONFIRM_CYCLES,
    "ADAPTIVE_REENTRY_COOLDOWN_MINUTES": ADAPTIVE_REENTRY_COOLDOWN_MINUTES,
    "ADAPTIVE_FRESH_SETUP_REQUIRED": ADAPTIVE_FRESH_SETUP_REQUIRED,
    "ADAPTIVE_REENTRY_PROBE_ONLY": ADAPTIVE_REENTRY_PROBE_ONLY,
    "ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND": ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND,
    "SWING_PIVOT_ENABLED": SWING_PIVOT_ENABLED,
    "SWING_PIVOT_LEFT_BARS": SWING_PIVOT_LEFT_BARS,
    "SWING_PIVOT_RIGHT_BARS": SWING_PIVOT_RIGHT_BARS,
    "STOP_BLOWN_SHADOW_MODE": STOP_BLOWN_SHADOW_MODE,
    "PHANTOM_EXTENSION_PCT": PHANTOM_EXTENSION_PCT,
    "FUNDING_LONG_MAX": FUNDING_LONG_MAX,
    "FUNDING_SHORT_MIN": FUNDING_SHORT_MIN,
    "FUNDING_SIZE_REDUCE_AT": FUNDING_SIZE_REDUCE_AT,
    "DAILY_STOP_LIMIT": DAILY_STOP_LIMIT,
    "LOSS_STREAK_LIMIT": LOSS_STREAK_LIMIT,
    "STREAK_PAUSE_HOURS": STREAK_PAUSE_HOURS,
    "STREAK_PAUSE_MINUTES": STREAK_PAUSE_MINUTES,
    "SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL": SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL,
    "SPOT_ENTRY_COOLDOWN_SEC": MIN_ENTRY_COOLDOWN_SECONDS,
    "PERP_ENTRY_COOLDOWN_SEC": MIN_ENTRY_COOLDOWN_SECONDS,
    "BRIDGE_ENTRY_COOLDOWN_SEC": MIN_ENTRY_COOLDOWN_SECONDS,
    "MIN_ENTRY_COOLDOWN_SECONDS": MIN_ENTRY_COOLDOWN_SECONDS,
    "SPOT_TRANCHE_TARGETS_PCT": SPOT_TRANCHE_TARGETS_PCT,
    "ENABLE_SPOT_BTC_TRADING": ENABLE_SPOT_BTC_TRADING,
    "SPOT_MIN_ENTRY_SCORE": SPOT_MIN_ENTRY_SCORE,
    "SPOT_FULL_SCORE": SPOT_FULL_SCORE,
    "SPOT_MIN_ORDER_USD": SPOT_MIN_ORDER_USD,
    "MACRO_FAST_SMA": MACRO_FAST_SMA,
    "MACRO_SLOW_SMA": MACRO_SLOW_SMA,
    "MIN_FUTURES_EQUITY_BUFFER_USD": MIN_FUTURES_EQUITY_BUFFER_USD,
    "MAX_EFFECTIVE_LEVERAGE": MAX_EFFECTIVE_LEVERAGE,
    "RSI_LONG_MAX": RSI_LONG_MAX,
    "RSI_SHORT_MIN": RSI_SHORT_MIN,
    "STOCH_LONG_MAX": STOCH_LONG_MAX,
    "STOCH_SHORT_MIN": STOCH_SHORT_MIN,
    "VOL_SPIKE_MIN": VOL_SPIKE_MIN,
    "ENABLE_CORE_PERP_ENTRIES": ENABLE_CORE_PERP_ENTRIES,
    "ENABLE_SPOT_BRIDGE_PERP_BUYS": ENABLE_SPOT_BRIDGE_PERP_BUYS,
    "MANUAL_POSITION_MODE": MANUAL_POSITION_MODE,
    "DRY_RUN": DRY_RUN,
    "SEND_EMAIL": SEND_EMAIL,
    "EMAIL_FROM": EMAIL_FROM,
    "EMAIL_TO": EMAIL_TO,
    "EMAIL_INCLUDE_RAW_ORDER": EMAIL_INCLUDE_RAW_ORDER,
    "SEND_TELEGRAM": SEND_TELEGRAM,
    "TELEGRAM_INCLUDE_ERRORS": TELEGRAM_INCLUDE_ERRORS,
    "TELEGRAM_DAILY_SUMMARY_ENABLED": TELEGRAM_DAILY_SUMMARY_ENABLED,
    "TELEGRAM_DAILY_SUMMARY_HOUR_ET": TELEGRAM_DAILY_SUMMARY_HOUR_ET,
}

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("larry_perp_v21_reporting_only")

# =============================================================================
# UTILITIES
# =============================================================================


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc() -> str:
    return now_utc().isoformat()


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None or x == "":
            return default
        return int(float(x))
    except Exception:
        return default


def obj_to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: obj_to_dict(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)


def side_to_signed(side: str, contracts: int) -> int:
    s = (side or "").upper()
    if s == "LONG":
        return abs(contracts)
    if s == "SHORT":
        return -abs(contracts)
    return 0


def signed_to_side_contracts(signed: int) -> Tuple[str, int]:
    if signed > 0:
        return "LONG", signed
    if signed < 0:
        return "SHORT", abs(signed)
    return "FLAT", 0

# =============================================================================
# SECRETS / CLIENTS
# =============================================================================


def load_secret(name: str) -> str:
    env_val = os.getenv(name)
    if env_val:
        return env_val
    if secretmanager is None:
        raise RuntimeError("google-cloud-secret-manager not available and env secret not set")
    sm = secretmanager.SecretManagerServiceClient()
    ref = f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"
    return sm.access_secret_version(request={"name": ref}).payload.data.decode("utf-8")


def fix_coinbase_secret(raw: str) -> str:
    key = raw.strip().strip('"').replace("\\n", "\n")
    if "BEGIN" in key and "PRIVATE KEY" in key:
        return key if key.endswith("\n") else key + "\n"
    if load_der_private_key is None:
        return key
    pk = load_der_private_key(base64.b64decode(key), password=None)
    return pk.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("utf-8")


def build_coinbase_client() -> Any:
    if RESTClient is None:
        raise RuntimeError("coinbase.rest.RESTClient not available")
    api_key = load_secret("COINBASE_API_KEY").strip()
    api_secret = fix_coinbase_secret(load_secret("COINBASE_SECRET"))
    return RESTClient(api_key=api_key, api_secret=api_secret)


def build_storage_client() -> Any:
    if storage is None:
        raise RuntimeError("google-cloud-storage not available")
    return storage.Client(project=PROJECT_ID)

# =============================================================================
# GCS HELPERS
# =============================================================================

class GCS:
    """GCS helper with a gcloud-storage subprocess fallback.

    The earlier v2 used google-cloud-storage directly. On this VM that can fail while
    trying to discover ADC through the metadata server with an SSL certificate error.
    The bot uses the Google Cloud CLI when the Python storage client is not explicitly
    enabled. ``gcloud storage`` shares gcloud credentials and supersedes gsutil.
    """

    def __init__(self, bucket_name: str):
        self.bucket_name = bucket_name
        self.prefix = f"gs://{bucket_name}"
        self.use_python_storage = os.getenv("USE_PYTHON_GCS_CLIENT", "false").lower() in ("1", "true", "yes")
        self.client = None
        self.bucket = None
        if self.use_python_storage:
            try:
                self.client = build_storage_client()
                self.bucket = self.client.bucket(bucket_name)
                log.info("GCS mode: python google-cloud-storage client")
            except Exception as e:
                log.warning("Python GCS client unavailable, falling back to gcloud storage: %s", e)
                self.use_python_storage = False
        if not self.use_python_storage:
            log.info("GCS mode: gcloud storage subprocess")

    def _uri(self, blob_name: str) -> str:
        return f"{self.prefix}/{blob_name}"

    def _run(self, cmd: List[str], input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )

    def _run_with_retry(
        self,
        cmd: List[str],
        input_text: Optional[str] = None,
        attempts: int = 3,
    ) -> subprocess.CompletedProcess:
        """Retry transient gcloud storage failures without hiding the final exception."""
        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                res = self._run(cmd, input_text=input_text)
                if res.returncode == 0:
                    return res
                last_error = RuntimeError(res.stderr.strip() or res.stdout.strip() or f"gcloud storage exit {res.returncode}")
            except subprocess.TimeoutExpired as exc:
                last_error = exc
            if attempt < attempts:
                delay = min(8.0, (2 ** (attempt - 1)) + (time.time() % 0.75))
                log.warning("Transient GCS command failure attempt %s/%s; retrying in %.2fs: %s", attempt, attempts, delay, last_error)
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def read_text(self, blob_name: str, default: str = "") -> str:
        if self.use_python_storage and self.bucket is not None:
            try:
                blob = self.bucket.blob(blob_name)
                if not blob.exists():
                    return default
                return blob.download_as_text()
            except Exception as e:
                log.warning("Python GCS read failed for %s: %s", blob_name, e)
        res = self._run(["gcloud", "storage", "cat", self._uri(blob_name)])
        if res.returncode != 0:
            return default
        return res.stdout

    def write_text(self, blob_name: str, text: str, content_type: str = "text/plain") -> None:
        if self.use_python_storage and self.bucket is not None:
            try:
                self.bucket.blob(blob_name).upload_from_string(text, content_type=content_type)
                return
            except Exception as e:
                log.warning("Python GCS write failed for %s: %s", blob_name, e)
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(text)
            tmp = f.name
        try:
            self._run_with_retry(["gcloud", "storage", "cp", "--content-type", content_type, tmp, self._uri(blob_name)])
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def read_json(self, blob_name: str, default: Any = None) -> Any:
        try:
            txt = self.read_text(blob_name, default="")
            if not txt.strip():
                return default
            return json.loads(txt)
        except Exception as e:
            log.warning("GCS read_json failed for %s: %s", blob_name, e)
            return default

    def write_json(self, blob_name: str, payload: Any) -> None:
        self.write_text(blob_name, json.dumps(payload, indent=2, default=str), content_type="application/json")

    def read_json_with_generation(self, blob_name: str, default: Any = None) -> Tuple[Any, Optional[int]]:
        """v12: returns (payload, generation) for compare-and-set writes.

        Falls back to (default, None) when running through gcloud storage where we cannot
        reliably surface the generation number. Concurrency protection is only
        active when USE_PYTHON_GCS_CLIENT=true.
        """
        if not (self.use_python_storage and self.bucket is not None):
            return self.read_json(blob_name, default=default), None
        try:
            blob = self.bucket.blob(blob_name)
            if not blob.exists():
                return default, None
            blob.reload()
            txt = blob.download_as_text()
            return (json.loads(txt) if txt.strip() else default), getattr(blob, "generation", None)
        except Exception as e:
            log.warning("read_json_with_generation failed for %s: %s", blob_name, e)
            return default, None

    def write_json_cas(self, blob_name: str, payload: Any, if_generation_match: Optional[int]) -> bool:
        """Compare-and-set write. Returns True on success, False on generation mismatch."""
        if not (self.use_python_storage and self.bucket is not None):
            # The CLI path has no CAS support here; fall through to unconditional write.
            self.write_json(blob_name, payload)
            return True
        try:
            from google.api_core.exceptions import PreconditionFailed
            blob = self.bucket.blob(blob_name)
            blob.upload_from_string(
                json.dumps(payload, indent=2, default=str),
                content_type="application/json",
                if_generation_match=if_generation_match if if_generation_match is not None else 0,
            )
            return True
        except Exception as e:
            log.warning("CAS write failed for %s (likely concurrent edit): %s", blob_name, e)
            return False

    def append_csv_row(self, blob_name: str, header: List[str], row: List[Any]) -> None:
        existing = self.read_text(blob_name, default="")
        out = []
        if not existing.strip():
            out.append(",".join(header))
        else:
            out.append(existing.rstrip("\n"))
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(row)
        out.append(buf.getvalue().rstrip("\n"))
        self.write_text(blob_name, "\n".join(out) + "\n", content_type="text/csv")


# =============================================================================
# LIVE STRATEGY CONFIG CONTROL PLANE
# =============================================================================

def _bool_from_any(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if x is None:
        return default
    return str(x).strip().lower() in ("1", "true", "yes", "on")


def _pct_list(value: Any, default: List[float]) -> List[float]:
    try:
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
            return [float(p) for p in parts]
        if isinstance(value, list):
            return [float(v) for v in value]
    except Exception:
        pass
    return list(default)


def load_strategy_config(gcs: GCS) -> Dict[str, Any]:
    """Load live strategy configuration from GCS.

    Dashboard writes gs://btc_trade_log/strategy_config.json. The bot reloads this
    each cycle, so parameters like TSL activation can be changed without SSH or a
    code deployment. Missing/invalid keys fall back to safe defaults.
    """
    live = gcs.read_json(STRATEGY_CONFIG_BLOB, default={}) or {}
    cfg = dict(DEFAULT_STRATEGY_CONFIG)
    if isinstance(live, dict):
        for k, v in live.items():
            if v not in (None, ""):
                cfg[k] = v
        cfg["CONFIG_SOURCE"] = f"gs://{BUCKET_NAME}/{STRATEGY_CONFIG_BLOB}"
        cfg["CONFIG_UPDATED_AT"] = live.get("updated_at") or live.get("last_updated")
    else:
        cfg["CONFIG_SOURCE"] = "DEFAULT_STRATEGY_CONFIG"
        cfg["CONFIG_UPDATED_AT"] = None

    # Normalize typed values.
    float_keys = [
        "CONTRACT_SIZE_BTC", "ATR_STOP_MULTIPLIER", "TSL_ACTIVATION_PCT",
        "TSL_TRAIL_PCT", "TP1_PCT", "TP1_FRACTION",
        "TP1_PROBE_TRIGGER_PCT", "TP1_PARTIAL_TRIGGER_PCT", "TP1_STRONG_TRIGGER_PCT", "TP1_FULL_TRIGGER_PCT", "TP1_R_MULTIPLE",
        "PHANTOM_EXTENSION_PCT", "FUNDING_LONG_MAX",
        "FUNDING_SHORT_MIN", "FUNDING_SIZE_REDUCE_AT",
        "PROBE_PCT", "PARTIAL_PCT", "STRONG_PCT",
        "REVERSAL_NEAR_BB_PCT", "REVERSAL_RSI_SOFT_LONG_MAX", "REVERSAL_RSI_SOFT_SHORT_MIN",
        "MIN_FUTURES_EQUITY_BUFFER_USD",
        "MAX_EFFECTIVE_LEVERAGE", "RSI_LONG_MAX", "RSI_SHORT_MIN",
        "STOCH_LONG_MAX", "STOCH_SHORT_MIN", "VOL_SPIKE_MIN", "SPOT_MIN_ORDER_USD",
    ]
    int_keys = [
        "MAX_CONVICTION_CONTRACTS", "CONTRACTS_PER_TRADE", "CONTRACTS_PER_TRADE_FULL", "CONTRACTS_PER_TRADE_PARTIAL",
        "CONTRACTS_PER_TRADE_PROBE", "MACRO_BLOCKED_PROBE_CONTRACTS",
        "ATR_PERIOD",
        "DAILY_STOP_LIMIT", "LOSS_STREAK_LIMIT",
        "SIGNAL_ARM_SCORE", "SIGNAL_COMMIT_SCORE", "SIGNAL_CANCEL_SCORE", "SIGNAL_HYSTERESIS_ARM_SCORE",
        "SIGNAL_VALIDITY_MINUTES", "REVERSAL_PROBE_CONTRACTS",
        "SPOT_ENTRY_COOLDOWN_SEC", "PERP_ENTRY_COOLDOWN_SEC",
        "BRIDGE_ENTRY_COOLDOWN_SEC", "MIN_ENTRY_COOLDOWN_SECONDS",
        "SPOT_MIN_ENTRY_SCORE", "SPOT_FULL_SCORE", "MACRO_FAST_SMA", "MACRO_SLOW_SMA",
        "TELEGRAM_DAILY_SUMMARY_HOUR_ET",
        "ADAPTIVE_REDUCE_SCORE", "ADAPTIVE_EXIT_SCORE", "ADAPTIVE_CONFIRM_CYCLES", "ADAPTIVE_REENTRY_COOLDOWN_MINUTES",
        "SWING_PIVOT_LEFT_BARS", "SWING_PIVOT_RIGHT_BARS",
    ]
    # STREAK_PAUSE_MINUTES is normalized for display only; apply_strategy_config always
    # derives the effective pause length from STREAK_PAUSE_HOURS (single source of truth,
    # see v30 fix note there) so editing only the hours field on the dashboard takes effect.
    float_keys += ["STREAK_PAUSE_HOURS", "STREAK_PAUSE_MINUTES"]
    bool_keys = ["ENABLE_CORE_PERP_ENTRIES", "ENABLE_SPOT_BRIDGE_PERP_BUYS", "ENABLE_SPOT_BTC_TRADING", "DRY_RUN", "SEND_EMAIL", "SEND_TELEGRAM", "TELEGRAM_INCLUDE_ERRORS", "TELEGRAM_DAILY_SUMMARY_ENABLED", "SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL", "SCORE4_MACRO_OVERRIDE_ENABLED", "PROGRESSIVE_ADD_ONS_ENABLED", "SIGNAL_LOCK_ENABLED", "SIGNAL_COMMIT_ON_CLOSED_CANDLE", "FREEZE_CONFIDENCE_ON_ARM", "REVERSAL_PROBE_ENABLED", "CORE_SCORE4_IMMEDIATE_ENTRY", "TP1_DYNAMIC_BY_LADDER", "TP1_USE_R_MULTIPLE", "ADAPTIVE_DEFENSE_ENABLED", "ADAPTIVE_FRESH_SETUP_REQUIRED", "ADAPTIVE_REENTRY_PROBE_ONLY", "ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND", "SWING_PIVOT_ENABLED", "STOP_BLOWN_SHADOW_MODE"]
    for k in float_keys:
        cfg[k] = safe_float(cfg.get(k), safe_float(DEFAULT_STRATEGY_CONFIG.get(k), 0.0))
    for k in int_keys:
        cfg[k] = safe_int(cfg.get(k), safe_int(DEFAULT_STRATEGY_CONFIG.get(k), 0))
    for k in bool_keys:
        cfg[k] = _bool_from_any(cfg.get(k), bool(DEFAULT_STRATEGY_CONFIG.get(k)))
    cfg["SPOT_TRANCHE_TARGETS_PCT"] = _pct_list(cfg.get("SPOT_TRANCHE_TARGETS_PCT"), SPOT_TRANCHE_TARGETS_PCT)
    return cfg


def apply_strategy_config(cfg: Dict[str, Any]) -> None:
    """Apply live config into module globals used by the current engine.

    This keeps the current file structure stable while making parameters live
    configurable. Product IDs and exchange plumbing remain code/env controlled.
    """
    global CONTRACT_SIZE_BTC, MAX_CONVICTION_CONTRACTS, PROBE_PCT, PARTIAL_PCT, STRONG_PCT, CONTRACTS_PER_TRADE, CONTRACTS_PER_TRADE_FULL, CONTRACTS_PER_TRADE_PARTIAL, CONTRACTS_PER_TRADE_PROBE, SCORE4_MACRO_OVERRIDE_ENABLED, PROGRESSIVE_ADD_ONS_ENABLED, MACRO_BLOCKED_PROBE_CONTRACTS, MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD, MAX_POSITION_ADDS, SIGNAL_LOCK_ENABLED, SIGNAL_VALIDITY_MINUTES, SIGNAL_CANCEL_SCORE, SIGNAL_HYSTERESIS_ARM_SCORE, SIGNAL_COMMIT_ON_CLOSED_CANDLE, FREEZE_CONFIDENCE_ON_ARM, SIGNAL_ARM_SCORE, SIGNAL_COMMIT_SCORE, CORE_SCORE4_IMMEDIATE_ENTRY, REVERSAL_PROBE_ENABLED, REVERSAL_PROBE_CONTRACTS, REVERSAL_NEAR_BB_PCT, REVERSAL_RSI_SOFT_LONG_MAX, REVERSAL_RSI_SOFT_SHORT_MIN
    global ATR_PERIOD, ATR_MULTIPLIER, TSL_ACTIVATION_PCT, TSL_TRAIL_PCT, TP1_PCT, TP1_FRACTION, TP1_DYNAMIC_BY_LADDER, TP1_PROBE_TRIGGER_PCT, TP1_PARTIAL_TRIGGER_PCT, TP1_STRONG_TRIGGER_PCT, TP1_FULL_TRIGGER_PCT, TP1_USE_R_MULTIPLE, TP1_R_MULTIPLE, PHANTOM_EXTENSION_PCT
    global ADAPTIVE_DEFENSE_ENABLED, ADAPTIVE_REDUCE_SCORE, ADAPTIVE_EXIT_SCORE, ADAPTIVE_CONFIRM_CYCLES, ADAPTIVE_REENTRY_COOLDOWN_MINUTES, ADAPTIVE_FRESH_SETUP_REQUIRED, ADAPTIVE_REENTRY_PROBE_ONLY, ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND, SWING_PIVOT_ENABLED, SWING_PIVOT_LEFT_BARS, SWING_PIVOT_RIGHT_BARS, STOP_BLOWN_SHADOW_MODE
    global FUNDING_LONG_MAX, FUNDING_SHORT_MIN, FUNDING_SIZE_REDUCE_AT, DAILY_STOP_LIMIT, LOSS_STREAK_LIMIT, STREAK_PAUSE_HOURS, STREAK_PAUSE_MINUTES
    global MIN_ENTRY_COOLDOWN_SECONDS, SPOT_TRANCHE_TARGETS_PCT, MIN_FUTURES_EQUITY_BUFFER_USD, MAX_EFFECTIVE_LEVERAGE
    global RSI_LONG_MAX, RSI_SHORT_MIN, STOCH_LONG_MAX, STOCH_SHORT_MIN, VOL_SPIKE_MIN
    global ENABLE_CORE_PERP_ENTRIES, ENABLE_SPOT_BRIDGE_PERP_BUYS, ENABLE_SPOT_BTC_TRADING, MANUAL_POSITION_MODE, DRY_RUN, SEND_EMAIL, EMAIL_FROM, EMAIL_TO, EMAIL_INCLUDE_RAW_ORDER, SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL, SEND_TELEGRAM, TELEGRAM_INCLUDE_ERRORS, TELEGRAM_DAILY_SUMMARY_ENABLED, TELEGRAM_DAILY_SUMMARY_HOUR_ET
    global SPOT_MIN_ENTRY_SCORE, SPOT_FULL_SCORE, SPOT_MIN_ORDER_USD, MACRO_FAST_SMA, MACRO_SLOW_SMA

    CONTRACT_SIZE_BTC = safe_float(cfg.get("CONTRACT_SIZE_BTC"), CONTRACT_SIZE_BTC)
    MAX_CONVICTION_CONTRACTS = max(1, safe_int(cfg.get("MAX_CONVICTION_CONTRACTS"), MAX_CONVICTION_CONTRACTS))
    PROBE_PCT = safe_float(cfg.get("PROBE_PCT"), PROBE_PCT)
    PARTIAL_PCT = safe_float(cfg.get("PARTIAL_PCT"), PARTIAL_PCT)
    STRONG_PCT = safe_float(cfg.get("STRONG_PCT"), STRONG_PCT)
    # v24: sizing tiers derive from Max Conviction by default. Legacy explicit contract
    # fields are accepted only if present, but the clean config controls pct-based sizing.
    derived_probe = derived_contract_tier(MAX_CONVICTION_CONTRACTS, PROBE_PCT)
    derived_partial = derived_contract_tier(MAX_CONVICTION_CONTRACTS, PARTIAL_PCT)
    derived_strong = derived_contract_tier(MAX_CONVICTION_CONTRACTS, STRONG_PCT)
    # v24 clean rule: Max Conviction is the single size knob. Probe/partial/strong/full
    # are derived from percentages and rounded to whole contracts every cycle.
    CONTRACTS_PER_TRADE = derived_probe
    CONTRACTS_PER_TRADE_PROBE = derived_probe
    CONTRACTS_PER_TRADE_PARTIAL = derived_partial
    CONTRACTS_PER_TRADE_FULL = MAX_CONVICTION_CONTRACTS
    SCORE4_MACRO_OVERRIDE_ENABLED = _bool_from_any(cfg.get("SCORE4_MACRO_OVERRIDE_ENABLED"), SCORE4_MACRO_OVERRIDE_ENABLED)
    PROGRESSIVE_ADD_ONS_ENABLED = _bool_from_any(cfg.get("PROGRESSIVE_ADD_ONS_ENABLED"), PROGRESSIVE_ADD_ONS_ENABLED)
    MACRO_BLOCKED_PROBE_CONTRACTS = safe_int(cfg.get("MACRO_BLOCKED_PROBE_CONTRACTS"), CONTRACTS_PER_TRADE_PROBE)
    MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD = safe_int(cfg.get("MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD"), MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD)
    MAX_POSITION_ADDS = safe_int(cfg.get("MAX_POSITION_ADDS"), MAX_POSITION_ADDS)
    # v30 fix: these 13 fields were declared `global` above (signaling intent to make
    # them dashboard-configurable) but were never actually assigned here, so edits made
    # via strategy_config.json were silently dropped and the engine kept running on
    # process-start defaults. Restored to match every other field's pattern.
    SIGNAL_LOCK_ENABLED = _bool_from_any(cfg.get("SIGNAL_LOCK_ENABLED"), SIGNAL_LOCK_ENABLED)
    SIGNAL_VALIDITY_MINUTES = safe_int(cfg.get("SIGNAL_VALIDITY_MINUTES"), SIGNAL_VALIDITY_MINUTES)
    SIGNAL_CANCEL_SCORE = safe_int(cfg.get("SIGNAL_CANCEL_SCORE"), SIGNAL_CANCEL_SCORE)
    SIGNAL_HYSTERESIS_ARM_SCORE = safe_int(cfg.get("SIGNAL_HYSTERESIS_ARM_SCORE"), SIGNAL_HYSTERESIS_ARM_SCORE)
    SIGNAL_COMMIT_ON_CLOSED_CANDLE = _bool_from_any(cfg.get("SIGNAL_COMMIT_ON_CLOSED_CANDLE"), SIGNAL_COMMIT_ON_CLOSED_CANDLE)
    FREEZE_CONFIDENCE_ON_ARM = _bool_from_any(cfg.get("FREEZE_CONFIDENCE_ON_ARM"), FREEZE_CONFIDENCE_ON_ARM)
    SIGNAL_ARM_SCORE = safe_int(cfg.get("SIGNAL_ARM_SCORE"), SIGNAL_ARM_SCORE)
    SIGNAL_COMMIT_SCORE = safe_int(cfg.get("SIGNAL_COMMIT_SCORE"), SIGNAL_COMMIT_SCORE)
    CORE_SCORE4_IMMEDIATE_ENTRY = _bool_from_any(cfg.get("CORE_SCORE4_IMMEDIATE_ENTRY"), CORE_SCORE4_IMMEDIATE_ENTRY)
    REVERSAL_PROBE_ENABLED = _bool_from_any(cfg.get("REVERSAL_PROBE_ENABLED"), REVERSAL_PROBE_ENABLED)
    REVERSAL_PROBE_CONTRACTS = safe_int(cfg.get("REVERSAL_PROBE_CONTRACTS"), REVERSAL_PROBE_CONTRACTS)
    REVERSAL_NEAR_BB_PCT = safe_float(cfg.get("REVERSAL_NEAR_BB_PCT"), REVERSAL_NEAR_BB_PCT)
    REVERSAL_RSI_SOFT_LONG_MAX = safe_float(cfg.get("REVERSAL_RSI_SOFT_LONG_MAX"), REVERSAL_RSI_SOFT_LONG_MAX)
    REVERSAL_RSI_SOFT_SHORT_MIN = safe_float(cfg.get("REVERSAL_RSI_SOFT_SHORT_MIN"), REVERSAL_RSI_SOFT_SHORT_MIN)
    ATR_PERIOD = safe_int(cfg.get("ATR_PERIOD"), ATR_PERIOD)
    ATR_MULTIPLIER = safe_float(cfg.get("ATR_STOP_MULTIPLIER"), ATR_MULTIPLIER)
    TSL_ACTIVATION_PCT = safe_float(cfg.get("TSL_ACTIVATION_PCT"), TSL_ACTIVATION_PCT)
    TSL_TRAIL_PCT = safe_float(cfg.get("TSL_TRAIL_PCT"), TSL_TRAIL_PCT)
    TP1_PCT = safe_float(cfg.get("TP1_PCT"), TP1_PCT)
    TP1_FRACTION = safe_float(cfg.get("TP1_FRACTION"), TP1_FRACTION)
    TP1_DYNAMIC_BY_LADDER = _bool_from_any(cfg.get("TP1_DYNAMIC_BY_LADDER"), TP1_DYNAMIC_BY_LADDER)
    TP1_PROBE_TRIGGER_PCT = safe_float(cfg.get("TP1_PROBE_TRIGGER_PCT"), TP1_PROBE_TRIGGER_PCT)
    TP1_PARTIAL_TRIGGER_PCT = safe_float(cfg.get("TP1_PARTIAL_TRIGGER_PCT"), TP1_PARTIAL_TRIGGER_PCT)
    TP1_STRONG_TRIGGER_PCT = safe_float(cfg.get("TP1_STRONG_TRIGGER_PCT"), TP1_STRONG_TRIGGER_PCT)
    TP1_FULL_TRIGGER_PCT = safe_float(cfg.get("TP1_FULL_TRIGGER_PCT"), TP1_FULL_TRIGGER_PCT)
    TP1_USE_R_MULTIPLE = _bool_from_any(cfg.get("TP1_USE_R_MULTIPLE"), TP1_USE_R_MULTIPLE)
    TP1_R_MULTIPLE = max(0.25, safe_float(cfg.get("TP1_R_MULTIPLE"), TP1_R_MULTIPLE))
    ADAPTIVE_DEFENSE_ENABLED = _bool_from_any(cfg.get("ADAPTIVE_DEFENSE_ENABLED"), ADAPTIVE_DEFENSE_ENABLED)
    ADAPTIVE_REDUCE_SCORE = max(1, min(100, safe_int(cfg.get("ADAPTIVE_REDUCE_SCORE"), ADAPTIVE_REDUCE_SCORE)))
    ADAPTIVE_EXIT_SCORE = max(ADAPTIVE_REDUCE_SCORE, min(100, safe_int(cfg.get("ADAPTIVE_EXIT_SCORE"), ADAPTIVE_EXIT_SCORE)))
    ADAPTIVE_CONFIRM_CYCLES = max(1, safe_int(cfg.get("ADAPTIVE_CONFIRM_CYCLES"), ADAPTIVE_CONFIRM_CYCLES))
    ADAPTIVE_REENTRY_COOLDOWN_MINUTES = max(1, safe_int(cfg.get("ADAPTIVE_REENTRY_COOLDOWN_MINUTES"), ADAPTIVE_REENTRY_COOLDOWN_MINUTES))
    ADAPTIVE_FRESH_SETUP_REQUIRED = _bool_from_any(cfg.get("ADAPTIVE_FRESH_SETUP_REQUIRED"), ADAPTIVE_FRESH_SETUP_REQUIRED)
    ADAPTIVE_REENTRY_PROBE_ONLY = _bool_from_any(cfg.get("ADAPTIVE_REENTRY_PROBE_ONLY"), ADAPTIVE_REENTRY_PROBE_ONLY)
    ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND = _bool_from_any(
        cfg.get("ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND"),
        ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND,
    )
    SWING_PIVOT_ENABLED = _bool_from_any(cfg.get("SWING_PIVOT_ENABLED"), SWING_PIVOT_ENABLED)
    SWING_PIVOT_LEFT_BARS = max(1, safe_int(cfg.get("SWING_PIVOT_LEFT_BARS"), SWING_PIVOT_LEFT_BARS))
    SWING_PIVOT_RIGHT_BARS = max(1, safe_int(cfg.get("SWING_PIVOT_RIGHT_BARS"), SWING_PIVOT_RIGHT_BARS))
    STOP_BLOWN_SHADOW_MODE = _bool_from_any(cfg.get("STOP_BLOWN_SHADOW_MODE"), STOP_BLOWN_SHADOW_MODE)
    PHANTOM_EXTENSION_PCT = safe_float(cfg.get("PHANTOM_EXTENSION_PCT"), PHANTOM_EXTENSION_PCT)
    FUNDING_LONG_MAX = safe_float(cfg.get("FUNDING_LONG_MAX"), FUNDING_LONG_MAX)
    FUNDING_SHORT_MIN = safe_float(cfg.get("FUNDING_SHORT_MIN"), FUNDING_SHORT_MIN)
    FUNDING_SIZE_REDUCE_AT = safe_float(cfg.get("FUNDING_SIZE_REDUCE_AT"), FUNDING_SIZE_REDUCE_AT)
    DAILY_STOP_LIMIT = safe_int(cfg.get("DAILY_STOP_LIMIT"), DAILY_STOP_LIMIT)
    LOSS_STREAK_LIMIT = safe_int(cfg.get("LOSS_STREAK_LIMIT"), LOSS_STREAK_LIMIT)
    STREAK_PAUSE_HOURS = safe_float(cfg.get("STREAK_PAUSE_HOURS"), STREAK_PAUSE_HOURS)
    # v30 fix: STREAK_PAUSE_HOURS is the single source of truth for pause length.
    # Previously STREAK_PAUSE_MINUTES was read independently from cfg and, once any
    # value had ever been written for it, took precedence over a freshly-edited
    # STREAK_PAUSE_HOURS -- an operator editing only "hours" on the dashboard saw no
    # change in the actual pause duration. Always derive minutes from hours instead.
    STREAK_PAUSE_MINUTES = STREAK_PAUSE_HOURS * 60
    MIN_ENTRY_COOLDOWN_SECONDS = safe_int(cfg.get("MIN_ENTRY_COOLDOWN_SECONDS", cfg.get("PERP_ENTRY_COOLDOWN_SEC")), MIN_ENTRY_COOLDOWN_SECONDS)
    SPOT_TRANCHE_TARGETS_PCT = _pct_list(cfg.get("SPOT_TRANCHE_TARGETS_PCT"), SPOT_TRANCHE_TARGETS_PCT)
    MIN_FUTURES_EQUITY_BUFFER_USD = safe_float(cfg.get("MIN_FUTURES_EQUITY_BUFFER_USD"), MIN_FUTURES_EQUITY_BUFFER_USD)
    MAX_EFFECTIVE_LEVERAGE = safe_float(cfg.get("MAX_EFFECTIVE_LEVERAGE"), MAX_EFFECTIVE_LEVERAGE)
    RSI_LONG_MAX = safe_float(cfg.get("RSI_LONG_MAX"), RSI_LONG_MAX)
    RSI_SHORT_MIN = safe_float(cfg.get("RSI_SHORT_MIN"), RSI_SHORT_MIN)
    STOCH_LONG_MAX = safe_float(cfg.get("STOCH_LONG_MAX"), STOCH_LONG_MAX)
    STOCH_SHORT_MIN = safe_float(cfg.get("STOCH_SHORT_MIN"), STOCH_SHORT_MIN)
    VOL_SPIKE_MIN = safe_float(cfg.get("VOL_SPIKE_MIN"), VOL_SPIKE_MIN)
    ENABLE_CORE_PERP_ENTRIES = _bool_from_any(cfg.get("ENABLE_CORE_PERP_ENTRIES"), ENABLE_CORE_PERP_ENTRIES)
    ENABLE_SPOT_BRIDGE_PERP_BUYS = _bool_from_any(cfg.get("ENABLE_SPOT_BRIDGE_PERP_BUYS"), ENABLE_SPOT_BRIDGE_PERP_BUYS)
    ENABLE_SPOT_BTC_TRADING = _bool_from_any(cfg.get("ENABLE_SPOT_BTC_TRADING"), ENABLE_SPOT_BTC_TRADING)
    MANUAL_POSITION_MODE = str(cfg.get("MANUAL_POSITION_MODE") or MANUAL_POSITION_MODE).strip().lower()
    if MANUAL_POSITION_MODE not in ("monitor_only", "full_management"):
        MANUAL_POSITION_MODE = "monitor_only"
    SPOT_MIN_ENTRY_SCORE = safe_int(cfg.get("SPOT_MIN_ENTRY_SCORE"), SPOT_MIN_ENTRY_SCORE)
    SPOT_FULL_SCORE = safe_int(cfg.get("SPOT_FULL_SCORE"), SPOT_FULL_SCORE)
    SPOT_MIN_ORDER_USD = safe_float(cfg.get("SPOT_MIN_ORDER_USD"), SPOT_MIN_ORDER_USD)
    MACRO_FAST_SMA = safe_int(cfg.get("MACRO_FAST_SMA"), MACRO_FAST_SMA)
    MACRO_SLOW_SMA = safe_int(cfg.get("MACRO_SLOW_SMA"), MACRO_SLOW_SMA)
    DRY_RUN = _bool_from_any(cfg.get("DRY_RUN"), DRY_RUN)
    SEND_EMAIL = _bool_from_any(cfg.get("SEND_EMAIL"), SEND_EMAIL)
    SEND_TELEGRAM = _bool_from_any(cfg.get("SEND_TELEGRAM"), SEND_TELEGRAM)
    TELEGRAM_INCLUDE_ERRORS = _bool_from_any(cfg.get("TELEGRAM_INCLUDE_ERRORS"), TELEGRAM_INCLUDE_ERRORS)
    TELEGRAM_DAILY_SUMMARY_ENABLED = _bool_from_any(cfg.get("TELEGRAM_DAILY_SUMMARY_ENABLED"), TELEGRAM_DAILY_SUMMARY_ENABLED)
    TELEGRAM_DAILY_SUMMARY_HOUR_ET = safe_int(cfg.get("TELEGRAM_DAILY_SUMMARY_HOUR_ET"), TELEGRAM_DAILY_SUMMARY_HOUR_ET)
    EMAIL_INCLUDE_RAW_ORDER = _bool_from_any(cfg.get("EMAIL_INCLUDE_RAW_ORDER"), EMAIL_INCLUDE_RAW_ORDER)
    EMAIL_FROM = str(cfg.get("EMAIL_FROM") or EMAIL_FROM)
    EMAIL_TO = str(cfg.get("EMAIL_TO") or EMAIL_TO)
    SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL = _bool_from_any(
        cfg.get("SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL"),
        SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL,
    )

# =============================================================================
# INDICATORS
# =============================================================================


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) <= period:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def bollinger(values: List[float], period: int = 20, mult: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    mid = sum(window) / period
    sd = statistics.pstdev(window)
    return mid - mult * sd, mid, mid + mult * sd


def stoch_rsi(values: List[float], rsi_period: int = 14, stoch_period: int = 14) -> Optional[float]:
    if len(values) < rsi_period + stoch_period + 1:
        return None
    rsis = []
    for end in range(len(values) - stoch_period, len(values) + 1):
        sub = values[:end]
        val = rsi(sub, rsi_period)
        if val is not None:
            rsis.append(val)
    if not rsis:
        return None
    lo, hi = min(rsis), max(rsis)
    if hi == lo:
        return 0.5
    return (rsis[-1] - lo) / (hi - lo)

# =============================================================================
# DATA FETCHING
# =============================================================================


def get_product(cb: Any, product_id: str) -> Dict[str, Any]:
    return obj_to_dict(cb.get_product(product_id))


def get_btc_price(cb: Any) -> float:
    for pid in (SPOT_PRODUCT_ID, SPOT_FALLBACK_PRODUCT_ID):
        try:
            p = get_product(cb, pid)
            price = safe_float(p.get("price"))
            if price > 0:
                return price
        except Exception:
            continue
    return 0.0


def get_perp_mark(cb: Any) -> Tuple[float, Dict[str, Any]]:
    p = get_product(cb, PERP_PRODUCT_ID)
    details = p.get("future_product_details") or {}
    mark = (
        safe_float(p.get("mid_market_price"))
        or safe_float(details.get("index_price"))
        or safe_float(p.get("price"))
        or safe_float(details.get("settlement_price"))
    )
    return mark, p


def get_funding_rate(product: Dict[str, Any]) -> float:
    details = product.get("future_product_details") or {}
    return safe_float(details.get("funding_rate"))


def get_live_net_position(cb: Any) -> Dict[str, Any]:
    positions = obj_to_dict(cb.list_futures_positions())
    rows = positions.get("positions", []) if isinstance(positions, dict) else []
    for p in rows:
        if p.get("product_id") == PERP_PRODUCT_ID:
            side = (p.get("side") or "FLAT").upper()
            contracts = safe_int(p.get("number_of_contracts"), 0)
            signed = side_to_signed(side, contracts)
            return {
                "product_id": PERP_PRODUCT_ID,
                "side": side if contracts else "FLAT",
                "contracts": contracts,
                "signed_contracts": signed,
                "avg_entry_price": safe_float(p.get("avg_entry_price")),
                "current_price": safe_float(p.get("current_price")),
                "unrealized_pnl": safe_float(p.get("unrealized_pnl")),
                "daily_realized_pnl": safe_float(p.get("daily_realized_pnl")),
                "raw": p,
            }
    return {
        "product_id": PERP_PRODUCT_ID,
        "side": "FLAT",
        "contracts": 0,
        "signed_contracts": 0,
        "avg_entry_price": 0.0,
        "current_price": 0.0,
        "unrealized_pnl": 0.0,
        "daily_realized_pnl": 0.0,
        "raw": None,
    }


_CANDLE_CACHE: Dict[Tuple[str, str], Tuple[int, List[Dict[str, float]]]] = {}


def get_candles(cb: Any, product_id: str, granularity: str = CANDLE_GRANULARITY, limit: int = CANDLE_LIMIT) -> List[Dict[str, float]]:
    """v12: cache candles by (product, granularity) and refetch only when the
    most recent candle bucket has rolled. Saves ~50x API calls when running on
    ONE_HOUR granularity with a 60s loop.
    """
    # Coinbase rejects requests above ~350 candles. Keep the request safely below that.
    limit = max(50, min(int(limit or CANDLE_LIMIT), 300))
    granularity_seconds = {
        "ONE_MINUTE": 60,
        "FIVE_MINUTE": 300,
        "FIFTEEN_MINUTE": 900,
        "THIRTY_MINUTE": 1800,
        "ONE_HOUR": 3600,
        "TWO_HOUR": 7200,
        "SIX_HOUR": 21600,
        "ONE_DAY": 86400,
    }.get(str(granularity), 3600)
    end = int(time.time())
    current_bucket = end - (end % granularity_seconds)
    cache_key = (product_id, str(granularity))
    cached = _CANDLE_CACHE.get(cache_key)
    if cached and cached[0] == current_bucket and len(cached[1]) >= limit - 1:
        # The newest closed candle hasn't rolled yet. Reuse the cache.
        return cached[1]

    start = end - (limit * granularity_seconds)
    try:
        res = cb.get_candles(product_id=product_id, start=str(start), end=str(end), granularity=granularity)
        data = obj_to_dict(res)
        raw = data.get("candles", data if isinstance(data, list) else [])
    except Exception as e:
        log.warning("get_candles failed: %s", e)
        # Fall back to whatever is in cache if any, otherwise empty.
        return cached[1] if cached else []

    candles = []
    for c in raw:
        try:
            candles.append({
                "start": safe_float(c.get("start")),
                "low": safe_float(c.get("low")),
                "high": safe_float(c.get("high")),
                "open": safe_float(c.get("open")),
                "close": safe_float(c.get("close")),
                "volume": safe_float(c.get("volume")),
            })
        except Exception:
            continue
    candles.sort(key=lambda x: x.get("start", 0))
    out = candles[-limit:]
    if out:
        _CANDLE_CACHE[cache_key] = (current_bucket, out)
    return out

# =============================================================================
# SIGNALS / PHANTOM STATE
# =============================================================================

@dataclass
class SignalSnapshot:
    price: float
    rsi: Optional[float]
    stoch_rsi: Optional[float]
    lower_bb: Optional[float]
    mid_bb: Optional[float]
    upper_bb: Optional[float]
    atr: Optional[float]
    volume_ratio: float
    long_score: int
    short_score: int
    long_conditions: Dict[str, bool]
    short_conditions: Dict[str, bool]


def calculate_signals(candles: List[Dict[str, float]]) -> SignalSnapshot:
    closes = [c["close"] for c in candles if c.get("close")]
    vols = [c["volume"] for c in candles if c.get("volume") is not None]
    price = closes[-1] if closes else 0.0
    r = rsi(closes)
    srsi = stoch_rsi(closes)
    lb, mb, ub = bollinger(closes)
    a = atr(candles, ATR_PERIOD)
    vol_ratio = 0.0
    if len(vols) >= 21 and sum(vols[-21:-1]) > 0:
        vol_ratio = vols[-1] / (sum(vols[-21:-1]) / 20)

    long_cond = {
        "rsi_oversold": r is not None and r <= RSI_LONG_MAX,
        "bb_lower": lb is not None and price <= lb,
        "stoch_oversold": srsi is not None and srsi <= STOCH_LONG_MAX,
        "volume_spike": vol_ratio >= VOL_SPIKE_MIN,
    }
    short_cond = {
        "rsi_overbought": r is not None and r >= RSI_SHORT_MIN,
        "bb_upper": ub is not None and price >= ub,
        "stoch_overbought": srsi is not None and srsi >= STOCH_SHORT_MIN,
        "volume_spike": vol_ratio >= VOL_SPIKE_MIN,
    }
    return SignalSnapshot(
        price=price,
        rsi=r,
        stoch_rsi=srsi,
        lower_bb=lb,
        mid_bb=mb,
        upper_bb=ub,
        atr=a,
        volume_ratio=vol_ratio,
        long_score=sum(bool(v) for v in long_cond.values()),
        short_score=sum(bool(v) for v in short_cond.values()),
        long_conditions=long_cond,
        short_conditions=short_cond,
    )


def load_engine_state(gcs: GCS) -> Dict[str, Any]:
    return gcs.read_json(ENGINE_STATE_BLOB, default={}) or {}


def save_engine_state(gcs: GCS, state: Dict[str, Any]) -> None:
    state["last_updated"] = iso_utc()
    gcs.write_json(ENGINE_STATE_BLOB, state)


def default_engine_state() -> Dict[str, Any]:
    return {
        "version": "larry_perp_v35_fresh_setup_guard",
        "phantom": {
            "state": "MONITORING",
            "direction": None,
            "armed_price": None,
            "extension_price": None,
            "extension_achieved": False,
            "extension_achieved_at": None,
            "armed_candle_start": None,
            "confirmation_mode": None,
            "armed_at": None,
            "expires_at": None,
            "locked_score": None,
            "locked_confidence_pct": None,
            "locked_target_contracts": None,
            "locked_funding_bucket": None,
            "confirm_close": None,
            "committed_at": None,
            "reason": None,
        },
        "risk": {
            "daily_stop_hits": 0,
            "daily_stop_date": now_utc().date().isoformat(),
            "loss_streak": 0,
            "pause_until": None,
            "entries_halted": False,
            "halt_reason": None,
        },
        "position_controls": {
            "highest_price": None,
            "lowest_price": None,
            "atr_stop": None,
            "tsl_active": False,
            "tsl_stop": None,
            "phantom_extension_add_done": False,
            "phantom_extension_target_price": None,
            "phantom_extension_target_contracts": None,
            "position_version": 0,
            "position_fingerprint": None,
            "adaptive_defense": {},
        },
        "market_structure": {},
        "adaptive_reentry_guard": {
            "active": False,
            "side": None,
            "signal_cleared": False,
            "recovery_seen": False,
        },
        "stop_blown": {"active": False, "mode": "SHADOW"},
        "cooldowns": {
            "spot_last_entry_at": None,
            "perp_last_entry_at": None,  # back-compat: latest of long/short
            "perp_last_long_entry_at": None,
            "perp_last_short_entry_at": None,
            "bridge_last_entry_at": None,
            "min_seconds": MIN_ENTRY_COOLDOWN_SECONDS,
        },
        "spot_treasury": {
            "tranche_targets_pct": SPOT_TRANCHE_TARGETS_PCT,
            "enable_spot_btc_trading": ENABLE_SPOT_BTC_TRADING,
            "spot_min_entry_score": SPOT_MIN_ENTRY_SCORE,
            "spot_full_score": SPOT_FULL_SCORE,
            "note": "Original Spot ladder cumulative targets retained: 25%, 33%, 50%, 90%."
        },
        "macro_regime": None,
        "perp_equity_protection": {
            "min_futures_equity_buffer_usd": MIN_FUTURES_EQUITY_BUFFER_USD,
            "max_effective_leverage": MAX_EFFECTIVE_LEVERAGE,
        },
        "last_signal": None,
        "last_order_plan": None,
        "last_exchange_position": None,
        "bot_managed_position": None,
        "manual_position_status": {
            "mode": MANUAL_POSITION_MODE,
            "is_manual_or_external": False,
            "reason": None,
        },
        "add_on_state": {
            "position_id": None,
            "direction": None,
            "adds_count": 0,
            "last_add_confidence_pct": 0,
            "last_target_contracts": 0,
            "last_add_at": None,
            "note": "Progressive add-ons trade to a higher confidence target size, not repeated same-size adds.",
        },
    }


def reset_daily_risk_if_needed(state: Dict[str, Any]) -> None:
    risk = state.setdefault("risk", {})
    today = now_utc().date().isoformat()
    if risk.get("daily_stop_date") != today:
        risk["daily_stop_date"] = today
        risk["daily_stop_hits"] = 0
        # v30 fix: loss_streak was only ever incremented (record_exit_risk_result),
        # never reset, so once the bot had accumulated LOSS_STREAK_LIMIT stop-outs
        # over its entire lifetime, every subsequent stop -- even months later, even
        # after many winning trades in between -- re-triggered the streak pause. That
        # contradicts the documented "N *consecutive* losses" behavior. Bounding the
        # streak to reset daily keeps it from becoming a permanent one-way ratchet;
        # it also resets immediately on a profitable exit, see record_exit_risk_result.
        risk["loss_streak"] = 0
        if risk.get("halt_reason") == "daily_stop_limit":
            risk["entries_halted"] = False
            risk["halt_reason"] = None


def risk_allows_entry(state: Dict[str, Any]) -> Tuple[bool, str]:
    reset_daily_risk_if_needed(state)
    risk = state.setdefault("risk", {})
    if safe_int(risk.get("daily_stop_hits")) >= DAILY_STOP_LIMIT:
        risk["entries_halted"] = True
        risk["halt_reason"] = "daily_stop_limit"
        return False, "Daily umbrella active"
    pause_until = risk.get("pause_until")
    if pause_until:
        try:
            dt = datetime.fromisoformat(pause_until.replace("Z", "+00:00"))
            if now_utc() < dt:
                return False, f"Streak pause until {pause_until}"
            risk["pause_until"] = None
        except Exception:
            risk["pause_until"] = None
    return True, "OK"


def funding_allows(direction: str, funding_rate: float) -> Tuple[bool, str]:
    if direction == "LONG" and funding_rate > FUNDING_LONG_MAX:
        return False, f"Funding gate blocks LONG: {funding_rate:.6f} > {FUNDING_LONG_MAX:.6f}"
    if direction == "SHORT" and funding_rate < FUNDING_SHORT_MIN:
        return False, f"Funding gate blocks SHORT: {funding_rate:.6f} < {FUNDING_SHORT_MIN:.6f}"
    return True, "OK"



def _iso_plus_minutes(minutes: int) -> str:
    return (now_utc() + timedelta(minutes=max(1, int(minutes or 1)))).isoformat()


def _is_expired_iso(value: Any) -> bool:
    dt = parse_dt(value) if 'parse_dt' in globals() else None
    if not dt:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return False
    return now_utc() > dt


def reset_phantom_with_reason(state: Dict[str, Any], reason: str) -> None:
    p = default_engine_state()["phantom"]
    p["reason"] = reason
    state["phantom"] = p


def locked_context_from_phantom(state: Dict[str, Any], direction: str, sig: SignalSnapshot, funding_rate: float, macro_gate_open: bool) -> Dict[str, Any]:
    p = state.get("phantom") or {}
    score = safe_int(p.get("locked_score"), sig.long_score if direction == "LONG" else sig.short_score)
    fb = p.get("locked_funding_bucket") or funding_size_modifier(direction, funding_rate) if 'funding_size_modifier' in globals() else "FULL"
    conf = safe_int(p.get("locked_confidence_pct"), confidence_pct_for_signal(score, fb, macro_gate_open, direction) if 'confidence_pct_for_signal' in globals() else 0)
    target = safe_int(p.get("locked_target_contracts"), 0)
    return {"direction": direction, "score": score, "funding_bucket": fb, "confidence_pct": conf, "target_abs_contracts": target}


def build_entry_diagnostics(state: Dict[str, Any], sig: SignalSnapshot, macro: Dict[str, Any], funding_rate: float) -> Dict[str, Any]:
    """Operator-facing why-no-trade diagnostics.

    This is deliberately redundant with the trade logic so the dashboard can show
    exactly which stage is missing without requiring journalctl. It does not
    place orders or change the trading decision.
    """
    long_score = safe_int(sig.long_score, 0)
    short_score = safe_int(sig.short_score, 0)
    arm = safe_int(globals().get("SIGNAL_ARM_SCORE", 2), 2)
    commit = safe_int(globals().get("SIGNAL_COMMIT_SCORE", 3), 3)
    macro_open = bool((macro or {}).get("gate_open"))
    manual = state.get("manual_position_status") or {}
    kill = state.get("kill_switch") or {}
    funding_long_ok, funding_long_reason = funding_allows("LONG", funding_rate)
    funding_short_ok, funding_short_reason = funding_allows("SHORT", funding_rate)
    rp_dir, rp_score, rp_reason = reversal_probe_candidate(sig)

    long_conditions = dict(sig.long_conditions or {})
    short_conditions = dict(sig.short_conditions or {})
    long_missing = [k for k, v in long_conditions.items() if not v]
    short_missing = [k for k, v in short_conditions.items() if not v]

    blockers = []
    if kill.get("halt"):
        blockers.append(f"kill_switch: {kill.get('reason') or 'halt=true'}")
    if manual.get("is_manual_or_external") and not manual.get("allow_bot_to_trade_position", False):
        blockers.append("manual_position_monitor_only")
    if not macro_open:
        blockers.append("macro_blocked_for_core_long")
    if not funding_long_ok:
        blockers.append(f"long_funding_blocked: {funding_long_reason}")
    if not funding_short_ok:
        blockers.append(f"short_funding_blocked: {funding_short_reason}")

    # This describes what would be needed next. Core can arm at arm score; reversal
    # can arm independently if reversal_probe_candidate returns a side.
    core_long_arm_gap = max(0, arm - long_score)
    core_short_arm_gap = max(0, arm - short_score)
    core_long_commit_gap = max(0, commit - long_score)
    core_short_commit_gap = max(0, commit - short_score)

    if rp_dir:
        next_action = f"REVERSAL_PROBE_{rp_dir}_CAN_ARM"
    elif long_score >= arm or short_score >= arm:
        next_action = "CORE_CAN_ARM"
    else:
        next_action = "WAITING_FOR_SCORE_OR_REVERSAL_PROBE"

    return {
        "checked_at": iso_utc(),
        "price": safe_float(sig.price, 0.0),
        "long_score": long_score,
        "short_score": short_score,
        "signal_arm_score": arm,
        "signal_commit_score": commit,
        "long_conditions": long_conditions,
        "short_conditions": short_conditions,
        "long_missing": long_missing,
        "short_missing": short_missing,
        "core_long_arm_gap": core_long_arm_gap,
        "core_short_arm_gap": core_short_arm_gap,
        "core_long_commit_gap": core_long_commit_gap,
        "core_short_commit_gap": core_short_commit_gap,
        "macro_gate_open": macro_open,
        "macro_reason": (macro or {}).get("reason"),
        "funding_rate": funding_rate,
        "funding_long_ok": funding_long_ok,
        "funding_long_reason": funding_long_reason,
        "funding_short_ok": funding_short_ok,
        "funding_short_reason": funding_short_reason,
        "manual_position_block": bool(manual.get("is_manual_or_external")),
        "kill_switch_halt": bool(kill.get("halt")),
        "reversal_probe_direction": rp_dir,
        "reversal_probe_score": rp_score,
        "reversal_probe_reason": rp_reason,
        "blockers": blockers,
        "next_action": next_action,
        "explanation": (
            f"Long {long_score}/4 needs +{core_long_arm_gap} to arm, +{core_long_commit_gap} to commit. "
            f"Short {short_score}/4 needs +{core_short_arm_gap} to arm, +{core_short_commit_gap} to commit. "
            f"Reversal probe: {rp_reason}."
        ),
    }

def reversal_probe_candidate(sig: SignalSnapshot) -> Tuple[Optional[str], int, str]:
    """v17 small opportunistic reversal probe with explicit diagnostics.

    This does NOT create full-size trades. It only arms a probe-size setup.
    Phantom extension + closed-candle confirmation are still required before any order.
    """
    if not REVERSAL_PROBE_ENABLED:
        return None, 0, "reversal_probe_disabled"
    price = safe_float(sig.price, 0.0)
    lower = safe_float(sig.lower_bb, 0.0)
    upper = safe_float(sig.upper_bb, 0.0)
    # Use a slightly more forgiving near-BB test for probes. This is still only an ARM, not an order.
    probe_band = max(REVERSAL_NEAR_BB_PCT, 0.0075)
    near_lower = bool(lower and price <= lower * (1 + probe_band))
    near_upper = bool(upper and price >= upper * (1 - probe_band))
    soft_rsi_long = bool(sig.rsi is not None and sig.rsi <= REVERSAL_RSI_SOFT_LONG_MAX)
    soft_rsi_short = bool(sig.rsi is not None and sig.rsi >= REVERSAL_RSI_SOFT_SHORT_MIN)
    stoch_long = bool(sig.long_conditions.get("stoch_oversold"))
    stoch_short = bool(sig.short_conditions.get("stoch_overbought"))
    long_ok = bool(stoch_long and (near_lower or soft_rsi_long))
    short_ok = bool(stoch_short and (near_upper or soft_rsi_short))
    reason = (
        f"probe_check price={price:.2f} lower={lower:.2f} upper={upper:.2f} "
        f"band={probe_band:.4f} stoch_long={stoch_long} soft_rsi_long={soft_rsi_long} near_lower={near_lower} "
        f"stoch_short={stoch_short} soft_rsi_short={soft_rsi_short} near_upper={near_upper}"
    )
    if long_ok and not short_ok:
        return "LONG", 2, "reversal_probe_long " + reason
    if short_ok and not long_ok:
        return "SHORT", 2, "reversal_probe_short " + reason
    if long_ok and short_ok:
        # Rare conflict: prefer the side with stronger score; otherwise stay out.
        if sig.long_score > sig.short_score:
            return "LONG", 2, "reversal_probe_long_conflict_resolved " + reason
        if sig.short_score > sig.long_score:
            return "SHORT", 2, "reversal_probe_short_conflict_resolved " + reason
        return None, 0, "reversal_probe_conflict_no_trade " + reason
    return None, 0, "no_reversal_probe " + reason


def update_phantom_state(state: Dict[str, Any], sig: SignalSnapshot, funding_rate: float, candles: Optional[List[Dict[str, float]]] = None) -> Optional[str]:
    """v20 clean hard finite-state machine.

    Rules:
      - Existing PHANTOM_ARMED / EXTENSION_CONFIRMED / COMMITTED_ENTRY state is handled FIRST.
      - Once armed, the setup cannot be overwritten back to MONITORING by fresh score noise.
      - Reversal probes are independent of the core 2/4 score and persist until expiry / funding block / execution.
      - Score-4 core setups can commit immediately if CORE_SCORE4_IMMEDIATE_ENTRY is enabled.
    """
    phantom = state.setdefault("phantom", default_engine_state()["phantom"])
    pstate = phantom.get("state", "MONITORING")
    price = safe_float(sig.price, 0.0)
    long_score = safe_int(sig.long_score, 0)
    short_score = safe_int(sig.short_score, 0)

    def active_score(direction: str) -> int:
        return long_score if direction == "LONG" else short_score

    def last_closed_price() -> float:
        if candles and len(candles) >= 2:
            return safe_float(candles[-2].get("close"), 0.0)
        return 0.0

    def last_closed_start() -> float:
        if candles and len(candles) >= 2:
            return safe_float(candles[-2].get("start"), 0.0)
        return 0.0

    # ------------------------------------------------------------------
    # 1) Handle existing locked states FIRST. Do not recalculate candidate
    #    and do not allow score wobble to force MONITORING.
    # ------------------------------------------------------------------
    if pstate in ("PHANTOM_ARMED", "EXTENSION_CONFIRMED", "COMMITTED_ENTRY"):
        direction = phantom.get("direction")
        if not direction:
            reset_phantom_with_reason(state, "Reset: missing locked direction")
            return None

        # Expiry is the normal way an armed setup dies.
        if phantom.get("expires_at") and _is_expired_iso(phantom.get("expires_at")):
            reset_phantom_with_reason(state, "Signal lock expired before execution")
            return None

        allowed, reason = funding_allows(direction, funding_rate)
        if not allowed:
            phantom.update({"state": "FUNDING_BLOCKED", "reason": reason})
            return None

        is_reversal_probe = bool(phantom.get("is_reversal_probe") or phantom.get("signal_class") == "REVERSAL_PROBE")

        # v19 safety/alignment: do not let a CORE phantom stay armed/committed
        # if the sizing engine would produce target 0. This was confusing on
        # macro-blocked LONG setups: the FSM showed COMMITTED_ENTRY while the
        # target-net plan correctly said action=NONE. Reversal probes are the
        # permitted macro-blocked path and are handled below.
        if not is_reversal_probe:
            macro_open_now = bool((state.get("macro_regime") or {}).get("gate_open"))
            locked_target_now = safe_int(phantom.get("locked_target_contracts"), 0)
            if direction == "LONG" and not macro_open_now and locked_target_now <= 0:
                reset_phantom_with_reason(state, "Core LONG phantom cleared: macro blocked and target size is 0; waiting for qualified reversal probe")
                return None

        # For reversal probes, DO NOT cancel on core score 1. Only score 0 + no reversal evidence cancels.
        if is_reversal_probe:
            rp_dir, rp_score, rp_reason = reversal_probe_candidate(sig)
            state["last_reversal_probe_check"] = {"direction": rp_dir, "score": rp_score, "qualified": bool(rp_dir), "reason": rp_reason, "checked_at": iso_utc()}
            state["reversal_probe_diagnostics"] = state["last_reversal_probe_check"]
            if active_score(direction) <= 0 and rp_dir != direction:
                reset_phantom_with_reason(state, f"Reversal probe cancelled: no active evidence. {rp_reason}")
                return None
        else:
            if active_score(direction) <= SIGNAL_CANCEL_SCORE:
                reset_phantom_with_reason(state, f"Core signal cancelled: {direction} score fell to {active_score(direction)}/4")
                return None

        extension_price = safe_float(phantom.get("extension_price"), 0.0)
        if extension_price <= 0:
            reset_phantom_with_reason(state, "Reset: invalid extension price")
            return None

        if pstate == "PHANTOM_ARMED":
            # v24: phantom extension is now a sizing/add-on signal, not the entry trigger.
            extension_hit = (direction == "LONG" and price <= extension_price) or (direction == "SHORT" and price >= extension_price)
            if extension_hit and not phantom.get("extension_achieved"):
                ladder = sizing_ladder_contracts()
                phantom["extension_achieved"] = True
                phantom["extension_achieved_at"] = iso_utc()
                phantom["extension_target_contracts"] = ladder.get("partial")
                if FREEZE_CONFIDENCE_ON_ARM:
                    phantom["locked_target_contracts"] = max(safe_int(phantom.get("locked_target_contracts"), 0), safe_int(ladder.get("partial"), 0))
                    phantom["locked_confidence_pct"] = max(safe_int(phantom.get("locked_confidence_pct"), 0), 50)

            closed = last_closed_price()
            closed_start = last_closed_start()
            armed_candle_start = safe_float(phantom.get("armed_candle_start"), 0.0)
            candle_confirmed = bool(closed and (not armed_candle_start or closed_start > armed_candle_start))
            evidence_ok = active_score(direction) >= SIGNAL_ARM_SCORE
            if is_reversal_probe and not evidence_ok:
                rp_dir, rp_score, rp_reason = reversal_probe_candidate(sig)
                evidence_ok = (rp_dir == direction)
            if candle_confirmed and evidence_ok:
                phantom["state"] = "COMMITTED_ENTRY"
                phantom["reason"] = f"{direction} entry committed by next closed candle; extension_achieved={bool(phantom.get('extension_achieved'))}"
                phantom["confirm_close"] = closed
                phantom["committed_at"] = iso_utc()
                return direction
            phantom["reason"] = (
                f"{phantom.get('signal_class','CORE')} {direction} remains ARMED; "
                f"waiting next closed candle confirmation; price={price:.2f} extension={extension_price:.2f} "
                f"extension_achieved={bool(phantom.get('extension_achieved'))} expires_at={phantom.get('expires_at')}"
            )
            return None

        if pstate == "EXTENSION_CONFIRMED":
            closed = last_closed_price()
            confirm_price = closed if (SIGNAL_COMMIT_ON_CLOSED_CANDLE and closed) else price
            if direction == "LONG" and confirm_price > extension_price:
                phantom["state"] = "COMMITTED_ENTRY"
                phantom["reason"] = "Long entry committed by reversal confirmation; executing locked setup"
                phantom["confirm_close"] = confirm_price
                phantom["committed_at"] = iso_utc()
                return "LONG"
            if direction == "SHORT" and confirm_price < extension_price:
                phantom["state"] = "COMMITTED_ENTRY"
                phantom["reason"] = "Short entry committed by reversal confirmation; executing locked setup"
                phantom["confirm_close"] = confirm_price
                phantom["committed_at"] = iso_utc()
                return "SHORT"
            phantom["reason"] = f"Extension confirmed; waiting reversal close. confirm={confirm_price:.2f} extension={extension_price:.2f}"
            return None

        if pstate == "COMMITTED_ENTRY":
            return direction

    # ------------------------------------------------------------------
    # 2) FUNDING_BLOCKED can clear back to MONITORING only after funding clears.
    # ------------------------------------------------------------------
    if pstate == "FUNDING_BLOCKED":
        direction = phantom.get("direction")
        if direction and funding_allows(direction, funding_rate)[0]:
            reset_phantom_with_reason(state, "Funding block cleared; monitoring for new setup")
        return None

    # ------------------------------------------------------------------
    # 3) Fresh candidate selection only when MONITORING.
    # ------------------------------------------------------------------
    candidate = None
    candidate_score = 0
    candidate_class = "CORE"

    # Always evaluate the reversal probe in parallel so diagnostics and the FSM
    # agree. If a core LONG is macro-blocked and would size to zero, but a
    # reversal probe qualifies, use the reversal-probe path rather than arming a
    # non-executable CORE phantom.
    rp_dir, rp_score, rp_reason = reversal_probe_candidate(sig)
    _rp_diag = {"direction": rp_dir, "score": rp_score, "qualified": bool(rp_dir), "reason": rp_reason, "checked_at": iso_utc()}
    state["last_reversal_probe_check"] = _rp_diag
    state["reversal_probe_diagnostics"] = _rp_diag

    macro_open_now = bool((state.get("macro_regime") or {}).get("gate_open"))

    if long_score >= SIGNAL_ARM_SCORE and long_score >= short_score:
        core_decision = sizing_decision_for_signal("LONG", long_score, funding_rate, macro_open_now) if 'sizing_decision_for_signal' in globals() else {}
        core_target = safe_int(core_decision.get("target_abs_contracts"), 0)
        if core_target > 0:
            candidate, candidate_score, candidate_class = "LONG", long_score, "CORE"
        elif rp_dir == "LONG":
            candidate, candidate_score, candidate_class = "LONG", rp_score, "REVERSAL_PROBE"
            state.setdefault("last_blocked_action", {})["core_long"] = "Core LONG blocked by macro/target=0; using qualified reversal probe path"
        else:
            state.setdefault("last_blocked_action", {})["core_long"] = "Core LONG blocked by macro/target=0; no qualified reversal probe"
    elif short_score >= SIGNAL_ARM_SCORE:
        candidate, candidate_score, candidate_class = "SHORT", short_score, "CORE"
    elif rp_dir:
        candidate, candidate_score, candidate_class = rp_dir, rp_score, "REVERSAL_PROBE"

    if not candidate:
        rp = state.get("last_reversal_probe_check") or {}
        phantom.update(default_engine_state()["phantom"])
        phantom["reason"] = f"Waiting: core score too low long={long_score}/4 short={short_score}/4; {rp.get('reason', 'reversal probe not qualified')}"
        return None

    allowed, reason = funding_allows(candidate, funding_rate)
    if not allowed:
        phantom.update({"state": "FUNDING_BLOCKED", "direction": candidate, "reason": reason})
        return None

    fb = funding_size_modifier(candidate, funding_rate) if 'funding_size_modifier' in globals() else "FULL"
    macro_open = bool((state.get("macro_regime") or {}).get("gate_open"))
    decision = sizing_decision_for_signal(candidate, candidate_score, funding_rate, macro_open) if 'sizing_decision_for_signal' in globals() else {}

    # Score-4 core setup: do not make it jump through phantom if enabled. This matches operator expectation:
    # all 4 long/short triggers = committed entry, subject to risk/portfolio guards in caller.
    if candidate_class == "CORE" and candidate_score >= 4 and bool(globals().get("CORE_SCORE4_IMMEDIATE_ENTRY", True)):
        phantom.update({
            "state": "COMMITTED_ENTRY",
            "direction": candidate,
            "armed_price": price,
            "extension_price": price,
            "extension_achieved": False,
            "extension_achieved_at": None,
            "armed_candle_start": last_closed_start(),
            "confirmation_mode": "immediate_score4",
            "armed_at": iso_utc(),
            "expires_at": _iso_plus_minutes(SIGNAL_VALIDITY_MINUTES),
            "locked_score": candidate_score,
            "locked_confidence_pct": decision.get("confidence_pct"),
            "locked_target_contracts": decision.get("target_abs_contracts"),
            "locked_funding_bucket": fb,
            "signal_class": "CORE_SCORE4_IMMEDIATE",
            "is_reversal_probe": False,
            "reason": f"CORE {candidate} score-4 immediate commit; executing locked setup",
            "committed_at": iso_utc(),
        })
        return candidate

    extension_price = price * (1 - PHANTOM_EXTENSION_PCT) if candidate == "LONG" else price * (1 + PHANTOM_EXTENSION_PCT)
    phantom.update({
        "state": "PHANTOM_ARMED",
        "direction": candidate,
        "armed_price": price,
        "extension_price": extension_price,
        "extension_achieved": False,
        "extension_achieved_at": None,
        "armed_candle_start": last_closed_start(),
        "confirmation_mode": "next_closed_candle",
        "armed_at": iso_utc(),
        "expires_at": _iso_plus_minutes(SIGNAL_VALIDITY_MINUTES),
        "locked_score": candidate_score if FREEZE_CONFIDENCE_ON_ARM else None,
        "locked_confidence_pct": decision.get("confidence_pct") if FREEZE_CONFIDENCE_ON_ARM else None,
        "locked_target_contracts": decision.get("target_abs_contracts") if FREEZE_CONFIDENCE_ON_ARM else None,
        "locked_funding_bucket": fb if FREEZE_CONFIDENCE_ON_ARM else None,
        "signal_class": candidate_class,
        "is_reversal_probe": candidate_class == "REVERSAL_PROBE",
        "reason": f"{candidate_class} {candidate} setup ARMED and hard-locked for {SIGNAL_VALIDITY_MINUTES}m; waiting next closed candle. Extension {PHANTOM_EXTENSION_PCT*100:.2f}% is monitored for sizing/add-ons, not required for entry.",
    })
    return None


# =============================================================================
# EMAIL NOTIFICATIONS
# =============================================================================

_EMAIL_PASSWORD_CACHE = None


def get_email_password() -> str:
    """Load EMAIL_PASSWORD lazily so the bot can run even if email is disabled."""
    global _EMAIL_PASSWORD_CACHE
    if _EMAIL_PASSWORD_CACHE is not None:
        return _EMAIL_PASSWORD_CACHE
    try:
        _EMAIL_PASSWORD_CACHE = load_secret("EMAIL_PASSWORD")
    except Exception as e:
        log.warning("EMAIL_PASSWORD not available; trade emails disabled until configured: %s", e)
        _EMAIL_PASSWORD_CACHE = ""
    return _EMAIL_PASSWORD_CACHE


def send_email(subject: str, body: str) -> bool:
    if not SEND_EMAIL:
        log.info("Email disabled by config; skipping: %s", subject)
        return False
    if not EMAIL_FROM or not EMAIL_TO:
        log.warning("Email not sent; EMAIL_FROM or EMAIL_TO missing")
        return False
    password = get_email_password()
    if not password:
        log.warning("Email not sent; EMAIL_PASSWORD missing")
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        if int(SMTP_PORT) == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, int(SMTP_PORT), timeout=20) as srv:
                srv.login(EMAIL_FROM, password)
                srv.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT), timeout=20) as srv:
                srv.starttls()
                srv.login(EMAIL_FROM, password)
                srv.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Email sent: %s", subject)
        return True
    except Exception as e:
        log.exception("Email send failed: %s", e)
        return False




_TELEGRAM_BOT_TOKEN_CACHE = None
_TELEGRAM_CHAT_ID_CACHE = None


def get_telegram_secret(name: str) -> str:
    try:
        return load_secret(name).strip()
    except Exception as e:
        log.warning("Telegram secret %s unavailable: %s", name, e)
        return ""


def get_telegram_credentials() -> Tuple[str, str]:
    global _TELEGRAM_BOT_TOKEN_CACHE, _TELEGRAM_CHAT_ID_CACHE
    if _TELEGRAM_BOT_TOKEN_CACHE is None:
        _TELEGRAM_BOT_TOKEN_CACHE = get_telegram_secret("TELEGRAM_BOT_TOKEN")
    if _TELEGRAM_CHAT_ID_CACHE is None:
        _TELEGRAM_CHAT_ID_CACHE = get_telegram_secret("TELEGRAM_CHAT_ID")
    return _TELEGRAM_BOT_TOKEN_CACHE or "", _TELEGRAM_CHAT_ID_CACHE or ""


def send_telegram_message(text: str, *, event_type: str = "INFO") -> bool:
    """Send a free Telegram push alert to the operator's chat.

    Secrets required in GCP Secret Manager:
      - TELEGRAM_BOT_TOKEN
      - TELEGRAM_CHAT_ID
    """
    if not SEND_TELEGRAM:
        log.info("Telegram disabled by config; skipping %s", event_type)
        return False
    token, chat_id = get_telegram_credentials()
    if not token or not chat_id:
        log.warning("Telegram not sent; bot token or chat id missing")
        return False
    try:
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text[:3900],
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=12) as resp:
            ok = 200 <= int(resp.status) < 300
        if ok:
            log.info("Telegram sent: %s", event_type)
        return ok
    except Exception as e:
        log.warning("Telegram send failed for %s: %s", event_type, e)
        return False


def fmt_signed_usd(value: Any) -> str:
    try:
        v = float(value)
        return ("+" if v >= 0 else "-") + f"${abs(v):,.2f}"
    except Exception:
        return "—"


def et_timestamp_short() -> str:
    now = datetime.now(timezone.utc)
    if ZoneInfo:
        now = now.astimezone(ZoneInfo("America/New_York"))
        return now.strftime("%a %b %-d, %-I:%M %p ET")
    return now.isoformat()




def ladder_rung_for_contracts(contracts: int, ladder: Optional[Dict[str, Any]] = None) -> str:
    """Return the closest named sizing rung for an absolute contract count."""
    try:
        c = abs(int(contracts or 0))
    except Exception:
        c = 0
    ladder = ladder or derived_sizing_ladder(MAX_CONVICTION_CONTRACTS)
    rung_values = {
        "flat": 0,
        "runner": 1,
        "probe": safe_int(ladder.get("probe"), 0),
        "partial": safe_int(ladder.get("partial"), 0),
        "strong": safe_int(ladder.get("strong"), 0),
        "full": safe_int(ladder.get("full"), 0),
    }
    for name, value in rung_values.items():
        if c == value:
            return name.upper()
    return f"CUSTOM_{c}"


def classify_trade_intent(plan: Dict[str, Any], signal_reason: str) -> Dict[str, Any]:
    """Separate execution intent from signal reason for cleaner alerts/research data.

    signal_reason answers: what signal/risk module produced this target?
    trade_intent answers: what did the order actually do to exposure?
    """
    before_signed = safe_int(plan.get("current_signed"), 0)
    target_signed = safe_int(plan.get("target_signed"), 0)
    action = str(plan.get("action") or "NONE").upper()
    before_abs = abs(before_signed)
    target_abs = abs(target_signed)
    direction_after = "LONG" if target_signed > 0 else "SHORT" if target_signed < 0 else "FLAT"
    direction_before = "LONG" if before_signed > 0 else "SHORT" if before_signed < 0 else "FLAT"
    r = (signal_reason or "").upper()

    intent = "NO_CHANGE"
    execution_reason = signal_reason or "TRADE"

    if action == "NONE" or before_signed == target_signed:
        intent = "NO_CHANGE"
        execution_reason = "NO_CHANGE"
    elif before_signed == 0 and target_signed != 0:
        intent = "NEW_ENTRY"
        execution_reason = f"NEW_{direction_after}_ENTRY"
    elif target_signed == 0 and before_signed != 0:
        if "TP" in r:
            intent = "TAKE_PROFIT_FINAL"
            execution_reason = f"FINAL_TP_{direction_before}"
        elif "TSL" in r:
            intent = "TRAILING_STOP_EXIT"
            execution_reason = f"TSL_EXIT_{direction_before}"
        elif "ATR" in r:
            intent = "ATR_STOP_EXIT"
            execution_reason = f"ATR_EXIT_{direction_before}"
        elif "EMERGENCY" in r or "OPERATOR" in r or "MANUAL_OVERRIDE" in r:
            intent = "MANUAL_OVERRIDE_EXIT"
            execution_reason = "EMERGENCY_FLATTEN_DASHBOARD" if "DASHBOARD" in r or "EMERGENCY" in r else "OPERATOR_OVERRIDE_EXIT"
        else:
            intent = "FULL_EXIT"
            execution_reason = f"FULL_EXIT_{direction_before}"
    elif before_signed * target_signed < 0:
        intent = "REVERSAL_FLIP"
        execution_reason = f"REVERSAL_{direction_before}_TO_{direction_after}"
    elif before_signed * target_signed > 0:
        if target_abs > before_abs:
            intent = "TARGET_UPSIZE"
            if "PHANTOM_EXTENSION" in r:
                execution_reason = f"EXTENSION_UPSIZE_{direction_after}"
            else:
                execution_reason = f"TARGET_UPSIZE_{direction_after}"
        elif target_abs < before_abs:
            if "TP" in r:
                intent = "TAKE_PROFIT_STEPDOWN"
                execution_reason = f"TP_STEPDOWN_{direction_before}"
            elif "TSL" in r or "ATR" in r or "STOP" in r:
                intent = "RISK_REDUCTION"
                execution_reason = f"RISK_REDUCTION_{direction_before}"
            else:
                intent = "TARGET_STEPDOWN"
                execution_reason = f"TARGET_STEPDOWN_{direction_before}"
    else:
        intent = "TARGET_REBALANCE"
        execution_reason = f"TARGET_REBALANCE_{direction_after}"

    ladder = derived_sizing_ladder(MAX_CONVICTION_CONTRACTS)
    return {
        "trade_intent": intent,
        "signal_reason": signal_reason or "TRADE",
        "execution_reason": execution_reason,
        "target_before": before_signed,
        "target_after": target_signed,
        "sizing_rung_before": ladder_rung_for_contracts(before_abs, ladder),
        "sizing_rung_after": ladder_rung_for_contracts(target_abs, ladder),
    }

def trade_emoji(reason: str, action: str, is_exit: bool) -> str:
    r = (reason or "").upper()
    a = (action or "").upper()
    if "EMERGENCY" in r:
        return "🚨"
    if "ATR" in r or "TSL" in r or "STOP" in r:
        return "🔴"
    if "TP" in r or is_exit:
        return "🟡"
    if a == "BUY":
        return "🟢"
    if a == "SELL":
        return "🔵"
    return "ℹ️"


def send_trade_telegram(result: Dict[str, Any]) -> None:
    try:
        plan = result.get("plan") or {}
        order = result.get("order") or {}
        before = result.get("before") or {}
        after = result.get("after") or {}
        fills = result.get("fills") or {}
        if not result.get("ok") or not order or plan.get("action") in (None, "NONE"):
            return
        action = str(plan.get("action") or "—").upper()
        qty = safe_int(plan.get("contracts_needed"), 0)
        signal_reason = result.get("signal_reason") or result.get("reason") or "TRADE"
        execution_reason = result.get("execution_reason") or signal_reason
        trade_intent = result.get("trade_intent") or "TRADE"
        fill_price = safe_float(fills.get("avg_price"), 0.0) or safe_float(before.get("current_price"), 0.0)
        fees = safe_float(result.get("fees_usd"), safe_float(fills.get("commission"), 0.0))
        net = result.get("net_realized_pnl_usd")
        gross = result.get("gross_realized_pnl_usd")
        running = result.get("running_pnl_summary") or {}
        running_net = running.get("net_realized_pnl_usd")
        is_exit = bool(result.get("is_exit_trade"))
        emoji = trade_emoji(execution_reason, action, is_exit)
        before_line = f"{before.get('side','—')} {before.get('contracts',0)}"
        after_line = f"{after.get('side','—')} {after.get('contracts',0)}"
        ladder = derived_sizing_ladder(MAX_CONVICTION_CONTRACTS)
        ladder_line = f"{ladder.get('probe')} → {ladder.get('partial')} → {ladder.get('strong')} → {ladder.get('full')}"
        parts = [
            f"{emoji} LARRY {action}",
            "",
            f"Intent: {trade_intent}",
            f"Execution reason: {execution_reason}",
            f"Signal reason: {signal_reason}",
            f"Order: {action} {qty} BTC perp contracts",
            f"Fill: {fmt_usd(fill_price)}" + (" actual" if fills.get("found") else " estimate"),
            "",
            f"Position: {before_line} → {after_line}",
            f"Avg entry now: {fmt_usd(after.get('avg_entry_price')) if safe_int(after.get('contracts'),0) else 'Flat'}",
            f"Open UPL now: {fmt_signed_usd(after.get('unrealized_pnl')) if safe_int(after.get('contracts'),0) else 'Flat'}",
            f"Ladder: {ladder_line} (Max {MAX_CONVICTION_CONTRACTS})",
        ]
        if net is not None or gross is not None:
            parts += [
                "",
                f"Gross realized: {fmt_signed_usd(gross)}" if gross is not None else "Gross realized: —",
                f"Fees: {fmt_usd(fees)}",
                f"Net trade P&L: {fmt_signed_usd(net)}" if net is not None else "Net trade P&L: —",
            ]
        if running_net is not None:
            parts.append(f"Running Larry P&L: {fmt_signed_usd(running_net)}")
        parts += ["", f"Time: {et_timestamp_short()}"]
        send_telegram_message("\n".join(parts), event_type=f"TRADE_{execution_reason}")
    except Exception as e:
        log.warning("Trade Telegram construction failed: %s", e)


def maybe_send_daily_telegram_summary(gcs: 'GCS', state: Dict[str, Any], live_pos: Dict[str, Any]) -> None:
    if not TELEGRAM_DAILY_SUMMARY_ENABLED or not SEND_TELEGRAM:
        return
    try:
        now_utc = datetime.now(timezone.utc)
        if ZoneInfo:
            now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
        else:
            now_et = now_utc
        if now_et.hour < int(TELEGRAM_DAILY_SUMMARY_HOUR_ET):
            return
        today = now_et.strftime("%Y-%m-%d")
        if state.get("last_telegram_daily_summary_date") == today:
            return
        running = ledger_running_totals(gcs)
        pos_line = f"{live_pos.get('side','—')} {live_pos.get('contracts',0)}"
        msg = "\n".join([
            "📊 Larry Daily Report",
            "",
            f"Position: {pos_line}",
            f"Avg entry: {fmt_usd(live_pos.get('avg_entry_price')) if safe_int(live_pos.get('contracts'),0) else 'Flat'}",
            f"Open UPL: {fmt_signed_usd(live_pos.get('unrealized_pnl')) if safe_int(live_pos.get('contracts'),0) else 'Flat'}",
            "",
            f"Running realized P&L: {fmt_signed_usd(running.get('net_realized_pnl_usd'))}",
            f"Realized exit trades: {running.get('realized_trade_count', 0)}",
            f"Fees in ledger: {fmt_usd(running.get('fees_usd'))}",
            "",
            f"Risk gate: {(state.get('risk_gate') or {}).get('reason', 'OK')}",
            f"Time: {et_timestamp_short()}",
        ])
        if send_telegram_message(msg, event_type="DAILY_SUMMARY"):
            state["last_telegram_daily_summary_date"] = today
            state["last_telegram_daily_summary_at"] = now_utc.isoformat()
    except Exception as e:
        log.warning("Daily Telegram summary failed: %s", e)

def fmt_usd(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "—"


def fmt_num(value: Any, decimals: int = 2) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except Exception:
        return "—"


def fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return "—"


def order_id_from_response(order: Dict[str, Any]) -> Optional[str]:
    resp = order.get("response") or {}
    if isinstance(resp, dict):
        sr = resp.get("success_response") or {}
        return sr.get("order_id") or resp.get("order_id")
    return None


def normalize_order_response(response: Any, client_order_id: str) -> Dict[str, Any]:
    """Require Coinbase's explicit success response; a non-throwing SDK call is not enough."""
    payload = obj_to_dict(response)
    if not isinstance(payload, dict):
        return {"ok": False, "response": payload, "client_order_id": client_order_id, "error": "Coinbase returned a non-dict order response"}
    success_response = payload.get("success_response") or {}
    error_response = payload.get("error_response") or {}
    order_id = success_response.get("order_id") or payload.get("order_id")
    explicit_success = payload.get("success")
    ok = bool(order_id) and explicit_success is not False and not error_response
    return {
        "ok": ok,
        "response": payload,
        "client_order_id": client_order_id,
        "order_id": order_id,
        "error": None if ok else (error_response or payload.get("message") or "Coinbase did not explicitly confirm order success"),
    }


def get_recent_fills_for_order(cb: Any, order_id: Optional[str]) -> Dict[str, Any]:
    """Best-effort fill lookup for cleaner realized P&L / fee emails.

    Coinbase order responses do not always include fill economics immediately. We
    query recent fills and match by order_id. If unavailable, callers fall back to
    before/current price estimates and clearly label the values as estimates.
    """
    out = {"found": False, "fills": [], "avg_price": None, "contracts": 0.0, "commission": 0.0}
    if not order_id:
        return out
    try:
        raw = obj_to_dict(cb.get_fills(product_id=PERP_PRODUCT_ID, limit=FILL_LOOKBACK_LIMIT))
        rows = raw.get("fills", []) if isinstance(raw, dict) else []
        matched = [f for f in rows if str(f.get("order_id")) == str(order_id)]
        if not matched:
            return out
        qty = sum(safe_float(f.get("size"), 0.0) for f in matched)
        notional_px = sum(safe_float(f.get("price"), 0.0) * safe_float(f.get("size"), 0.0) for f in matched)
        comm = sum(safe_float(f.get("commission"), 0.0) for f in matched)
        out.update({
            "found": True,
            "fills": matched,
            "avg_price": (notional_px / qty) if qty else None,
            "contracts": qty,
            "commission": comm,
        })
        return out
    except Exception as e:
        log.warning("get_recent_fills_for_order failed: %s", e)
        return out


def estimated_gross_realized(before: Dict[str, Any], plan: Dict[str, Any], fill_price: float) -> Optional[float]:
    """Estimate gross realized P&L for reducing/closing existing exposure.

    Contract size is 0.01 BTC per contract. For entries/adds this returns None.
    """
    before_signed = safe_int(before.get("signed_contracts"), 0)
    action = (plan.get("action") or "").upper()
    qty = safe_float(plan.get("contracts_needed"), 0.0)
    avg = safe_float(before.get("avg_entry_price"), 0.0)
    if not before_signed or not qty or not avg or not fill_price:
        return None
    # SELL while long realizes on min(qty, long contracts). BUY while short realizes.
    if before_signed > 0 and action == "SELL":
        closed = min(qty, abs(before_signed))
        return (fill_price - avg) * closed * CONTRACT_SIZE_BTC
    if before_signed < 0 and action == "BUY":
        closed = min(qty, abs(before_signed))
        return (avg - fill_price) * closed * CONTRACT_SIZE_BTC
    return None


def ledger_running_totals(gcs: GCS) -> Dict[str, Any]:
    """Reporting-only ledger summary for emails/state.

    Uses Larry's own perp_trades_ledger.csv, not Coinbase account equity. This is
    deliberately non-trading logic: it only helps the operator see running net P&L.
    """
    schema = [
        "timestamp", "reason", "signal_class", "action", "contracts",
        "before_signed", "target_signed", "after_signed", "mark_at_send",
        "fill_price", "slippage_bps", "gross_realized_pnl_usd", "fees_usd",
        "net_realized_pnl_usd", "ok", "client_order_id", "raw_order",
    ]
    raw = gcs.read_text(PERP_TRADES_LEDGER_BLOB, default="")
    realized = []
    successes = []
    fees_total = 0.0
    if raw.strip():
        for vals in csv.reader(raw.splitlines()):
            if not vals or vals[0].lower() == "timestamp":
                continue
            rec = {schema[i]: vals[i] if i < len(vals) else "" for i in range(len(schema))}
            ok = str(rec.get("ok", "")).strip().lower() in ("true", "1", "yes")
            if ok:
                successes.append(rec)
                fees_total += safe_float(rec.get("fees_usd"), 0.0)
            net = rec.get("net_realized_pnl_usd")
            if ok and net not in (None, ""):
                rec["net_realized_pnl_usd"] = safe_float(net, 0.0)
                realized.append(rec)
    net_realized_total = sum(safe_float(r.get("net_realized_pnl_usd"), 0.0) for r in realized)
    last_realized = realized[-1] if realized else None
    return {
        "trade_count": len(successes),
        "realized_trade_count": len(realized),
        "fees_usd": fees_total,
        "net_realized_pnl_usd": net_realized_total,
        "last_realized_net_pnl_usd": safe_float(last_realized.get("net_realized_pnl_usd"), None) if last_realized else None,
        "last_realized_reason": last_realized.get("reason") if last_realized else None,
        "source": PERP_TRADES_LEDGER_BLOB,
    }

def send_trade_email(result: Dict[str, Any]) -> None:
    """Clean operator email for every actual perp order.

    The old email dumped raw JSON and showed post-flat avg/current as 0.0, which
    was technically true but operationally confusing. This version emphasizes:
    action, reason, before/after exposure, fill estimate/actual fill lookup,
    estimated realized P&L, fees, and the active risk configuration.
    """
    try:
        plan = result.get("plan") or {}
        order = result.get("order") or {}
        before = result.get("before") or {}
        after = result.get("after") or {}
        fills = result.get("fills") or {}
        if not result.get("ok") or not order or plan.get("action") in (None, "NONE"):
            return

        action = plan.get("action")
        qty = safe_float(plan.get("contracts_needed"), 0)
        signal_reason = result.get("signal_reason") or result.get("reason") or "TRADE"
        execution_reason = result.get("execution_reason") or signal_reason
        trade_intent = result.get("trade_intent") or "TRADE"
        reason = execution_reason
        order_id = order_id_from_response(order)
        client_order_id = order.get("client_order_id")
        fill_price = safe_float(fills.get("avg_price"), 0.0) or safe_float(before.get("current_price"), 0.0)
        fill_label = "actual avg fill" if fills.get("found") else "estimated using pre-trade mark"
        fees = safe_float(fills.get("commission"), 0.0)
        gross_realized = estimated_gross_realized(before, plan, fill_price)
        net_realized = result.get("net_realized_pnl_usd")
        if net_realized is None:
            net_realized = (gross_realized - fees) if gross_realized is not None else None
        running = result.get("running_pnl_summary") or {}
        running_net = running.get("net_realized_pnl_usd")
        running_count = running.get("realized_trade_count")

        before_line = f"{before.get('side', '—')} {before.get('contracts', 0)} contracts"
        after_line = f"{after.get('side', '—')} {after.get('contracts', 0)} contracts"
        exposure_btc_before = safe_int(before.get("signed_contracts"), 0) * CONTRACT_SIZE_BTC
        exposure_btc_after = safe_int(after.get("signed_contracts"), 0) * CONTRACT_SIZE_BTC

        pnl_suffix = ""
        if net_realized is not None:
            sign = "+" if net_realized >= 0 else ""
            pnl_suffix = f" | Net P&L {sign}${net_realized:,.2f}"
        subject = f"Larry Perp: {reason} — {action} {int(qty)} contracts{pnl_suffix}"
        body = f"""
Larry Perp Trade Confirmation

Trade Intent
  {trade_intent}

Execution Reason
  {execution_reason}

Signal Reason
  {signal_reason}

Order
  Action: {action} {int(qty)} contracts
  Plan: {plan.get('explanation')}
  Product: {PERP_PRODUCT_ID}

Position Change
  Before: {before_line} | Avg Entry {fmt_usd(before.get('avg_entry_price'))} | Mark {fmt_usd(before.get('current_price'))} | BTC exposure {fmt_num(exposure_btc_before, 4)} BTC
  After:  {after_line} | Avg Entry {fmt_usd(after.get('avg_entry_price')) if after.get('contracts') else 'Flat'} | Mark {fmt_usd(after.get('current_price')) if after.get('contracts') else 'Flat'} | BTC exposure {fmt_num(exposure_btc_after, 4)} BTC

Execution Economics
  Fill price: {fmt_usd(fill_price)} ({fill_label})
  Gross realized P&L on closed contracts: {fmt_usd(gross_realized) if gross_realized is not None else 'N/A — entry/add trade'}
  Estimated/actual execution fees: {fmt_usd(fees) if fills.get('found') else 'Pending fill lookup'}
  Net realized trade impact: {fmt_usd(net_realized) if net_realized is not None else 'N/A — no contracts closed'}
  Open/unrealized after trade: {fmt_usd(after.get('unrealized_pnl')) if after.get('contracts') else 'Flat'}
  Remaining position after trade: {after_line}

Running Larry P&L
  Running net realized P&L: {fmt_usd(running_net) if running_net is not None else 'Pending ledger summary'}
  Realized exit trades counted: {running_count if running_count is not None else '—'}
  Ledger source: {running.get('source', PERP_TRADES_LEDGER_BLOB)}

Risk Settings Active
  ATR stop multiple: {fmt_num(ATR_MULTIPLIER, 2)}x
  TSL activation: {fmt_pct(TSL_ACTIVATION_PCT)}
  TSL trail: {fmt_pct(TSL_TRAIL_PCT)}
  Dry run: {DRY_RUN}

IDs
  Coinbase order ID: {order_id or 'pending'}
  Client order ID: {client_order_id or 'pending'}

Timestamp
  {iso_utc()}
""".strip()

        if EMAIL_INCLUDE_RAW_ORDER:
            body += "\n\nRaw Order Debug\n" + json.dumps(order.get('response') or order, indent=2, default=str)[:3000]
        send_email(subject, body)
    except Exception as e:
        log.warning("Trade email construction failed: %s", e)


# =============================================================================
# TARGET NET EXPOSURE + EXECUTION
# =============================================================================


def clamp_target(target: int) -> int:
    """Clamp every desired net position to the operator's Max Conviction setting."""
    return max(-MAX_CONVICTION_CONTRACTS, min(MAX_CONVICTION_CONTRACTS, int(target)))


def safe_target_order_plan(current_signed: int, target_signed: int) -> Dict[str, Any]:
    target_signed = clamp_target(target_signed)
    delta = target_signed - current_signed
    if delta == 0:
        action = "NONE"
        contracts = 0
    elif delta > 0:
        action = "BUY"
        contracts = delta
    else:
        action = "SELL"
        contracts = abs(delta)
    cur_side, cur_contracts = signed_to_side_contracts(current_signed)
    tgt_side, tgt_contracts = signed_to_side_contracts(target_signed)
    post_side, post_contracts = signed_to_side_contracts(target_signed)
    return {
        "current_signed": current_signed,
        "current_side": cur_side,
        "current_contracts": cur_contracts,
        "target_signed": target_signed,
        "target_side": tgt_side,
        "target_contracts": tgt_contracts,
        "action": action,
        "contracts_needed": contracts,
        "expected_post_side": post_side,
        "expected_post_contracts": post_contracts,
        "explanation": f"{cur_side} {cur_contracts} -> {tgt_side} {tgt_contracts}: {action} {contracts}",
    }


def place_market_order(cb: Any, side: str, contracts: int, reason: str) -> Dict[str, Any]:
    if contracts <= 0 or side == "NONE":
        return {"ok": True, "skipped": True, "reason": "no_order_needed"}
    if DRY_RUN:
        log.warning("DRY_RUN: would %s %s contracts for %s", side, contracts, reason)
        return {"ok": True, "dry_run": True, "side": side, "contracts": contracts, "reason": reason}

    client_order_id = f"larry-v32-{int(time.time())}-{uuid.uuid4().hex[:10]}-{side.lower()}-{contracts}"
    # SDK helpers differ across versions. Prefer generic market_order if available.
    try:
        if side == "BUY":
            res = cb.market_order_buy(client_order_id=client_order_id, product_id=PERP_PRODUCT_ID, base_size=str(contracts))
        else:
            res = cb.market_order_sell(client_order_id=client_order_id, product_id=PERP_PRODUCT_ID, base_size=str(contracts))
        return normalize_order_response(res, client_order_id)
    except TypeError:
        # Fall back to generic create_order shape if SDK expects order_configuration.
        cfg_side = "BUY" if side == "BUY" else "SELL"
        try:
            res = cb.create_order(
                client_order_id=client_order_id,
                product_id=PERP_PRODUCT_ID,
                side=cfg_side,
                order_configuration={"market_market_ioc": {"base_size": str(contracts)}},
            )
            return normalize_order_response(res, client_order_id)
        except Exception as e:
            return {"ok": False, "error": str(e), "client_order_id": client_order_id}
    except Exception as e:
        return {"ok": False, "error": str(e), "client_order_id": client_order_id}


def execute_target(cb: Any, gcs: GCS, target_signed: int, reason: str, signal_class: Optional[str] = None) -> Dict[str, Any]:
    """v12: ledger now includes signal_class + slippage_bps.

    signal_class is a coarse tag (CORE_IAF_LONG, CORE_IAF_SHORT, SPOT_BRIDGE_*,
    ATR_STOP_*, TSL_STOP_*, TP1_LADDER_STEPDOWN_*) used by analyze_signal_pnl.py to
    aggregate realised P&L per sub-strategy.
    """
    before = get_live_net_position(cb)
    plan = safe_target_order_plan(before["signed_contracts"], target_signed)
    log.info("Net position plan: %s", plan["explanation"])

    if plan["contracts_needed"] <= 0:
        return {"ok": True, "plan": plan, "before": before, "after": before, "order": None}

    mark_at_send = safe_float(before.get("current_price"), 0.0)
    _CYCLE_CONTEXT.update({
        "phase": "ORDER_SUBMITTING",
        "order_attempted": True,
        "client_order_id": None,
        "order_status": "SUBMITTING",
    })
    order = place_market_order(cb, plan["action"], plan["contracts_needed"], reason)
    _CYCLE_CONTEXT["client_order_id"] = order.get("client_order_id")
    if not order.get("ok"):
        _CYCLE_CONTEXT["order_status"] = "REJECTED_OR_UNCONFIRMED"
        after = before
        fills = {"found": False, "fills": [], "avg_price": None, "contracts": 0.0, "commission": 0.0}
    else:
        _CYCLE_CONTEXT["order_status"] = "ACKNOWLEDGED"
        order_id = order_id_from_response(order)
        fills = {"found": False, "fills": [], "avg_price": None, "contracts": 0.0, "commission": 0.0}
        after = before
        deadline = time.monotonic() + max(5, int(os.getenv("ORDER_RECONCILE_TIMEOUT_SECONDS", "20")))
        while time.monotonic() < deadline:
            time.sleep(2)
            current_fills = get_recent_fills_for_order(cb, order_id)
            if current_fills.get("found"):
                fills = current_fills
            try:
                after = get_live_net_position(cb)
            except Exception as exc:
                _CYCLE_CONTEXT["order_status"] = "UNKNOWN_RECONCILIATION_REQUIRED"
                _CYCLE_CONTEXT["reconcile_error"] = str(exc)
                continue
            if safe_int(after.get("signed_contracts"), 0) != safe_int(before.get("signed_contracts"), 0):
                break

    before_signed_actual = safe_int(before.get("signed_contracts"), 0)
    after_signed_actual = safe_int(after.get("signed_contracts"), 0)
    actual_delta = abs(after_signed_actual - before_signed_actual)
    requested_qty = safe_int(plan.get("contracts_needed"), 0)
    if not order.get("ok"):
        execution_status = "REJECTED_OR_UNCONFIRMED"
    elif actual_delta <= 0:
        execution_status = "UNKNOWN_NO_POSITION_CHANGE"
    elif actual_delta < requested_qty:
        execution_status = "PARTIALLY_FILLED"
    elif after_signed_actual == safe_int(plan.get("target_signed"), 0):
        execution_status = "FILLED"
    else:
        execution_status = "FILLED_POSITION_MISMATCH"
    _CYCLE_CONTEXT["order_status"] = execution_status

    # v12: slippage in basis points (signed) -- positive means we paid worse than the pre-trade mark.
    fill_price = safe_float(fills.get("avg_price"), 0.0)
    slippage_bps = None
    if fill_price > 0 and mark_at_send > 0:
        direction_sign = 1 if plan["action"] == "BUY" else -1
        slippage_bps = round(direction_sign * (fill_price - mark_at_send) / mark_at_send * 10000.0, 3)

    gross_realized = estimated_gross_realized(before, plan, fill_price)
    fees = safe_float(fills.get("commission"), 0.0)
    net_realized = (gross_realized - fees) if gross_realized is not None else None
    intent_meta = classify_trade_intent(plan, reason)
    result = {
        "ok": bool(order.get("ok")) and actual_delta > 0, "plan": plan, "before": before, "after": after,
        "order": order, "fills": fills, "reason": reason, "mark_at_send": mark_at_send,
        "slippage_bps": slippage_bps, "gross_realized_pnl_usd": gross_realized,
        "fees_usd": fees, "net_realized_pnl_usd": net_realized,
        "is_exit_trade": gross_realized is not None,
        "execution_status": execution_status,
        "requested_contracts": requested_qty,
        "filled_contracts": safe_float(fills.get("contracts"), actual_delta) or actual_delta,
        "position_delta_contracts": actual_delta,
        "partial_fill": execution_status == "PARTIALLY_FILLED",
        **intent_meta,
    }
    ledger_header = [
        "timestamp", "reason", "signal_class", "action", "contracts",
        "before_signed", "target_signed", "after_signed",
        "mark_at_send", "fill_price", "slippage_bps", "gross_realized_pnl_usd", "fees_usd", "net_realized_pnl_usd",
        "ok", "client_order_id", "trade_intent", "execution_reason", "signal_reason",
        "target_before", "target_after", "sizing_rung_before", "sizing_rung_after", "raw_order"
    ]
    gcs.append_csv_row(PERP_TRADES_LEDGER_BLOB, ledger_header, [
        iso_utc(), reason, signal_class or reason, plan["action"], plan["contracts_needed"],
        before["signed_contracts"], plan["target_signed"], after["signed_contracts"],
        mark_at_send, fill_price, slippage_bps, gross_realized, fees, net_realized,
        result.get("ok"), order.get("client_order_id"),
        result.get("trade_intent"), result.get("execution_reason"), result.get("signal_reason"),
        result.get("target_before"), result.get("target_after"),
        result.get("sizing_rung_before"), result.get("sizing_rung_after"),
        json.dumps({
            **order,
            "execution_status": execution_status,
            "requested_contracts": requested_qty,
            "filled_contracts": result.get("filled_contracts"),
            "position_delta_contracts": actual_delta,
            "partial_fill": result.get("partial_fill"),
        }, default=str)
    ])
    before_signed = safe_int(before.get("signed_contracts"), 0)
    after_signed = safe_int(after.get("signed_contracts"), 0)
    confirmed_change = after_signed != before_signed
    should_email = result.get("ok") and plan.get("contracts_needed", 0) > 0 and (
        confirmed_change or not SEND_TRADE_EMAIL_ONLY_AFTER_CONFIRMED_FILL
    )
    if should_email:
        send_trade_email(result)
        send_trade_telegram(result)
    elif order.get("ok") and not confirmed_change:
        log.warning("Order acknowledged but position did not change after reconciliation; suppressing trade confirmation email. client_order_id=%s", order.get("client_order_id"))
    return result

# =============================================================================
# RISK MANAGEMENT FOR OPEN POSITION
# =============================================================================


def classify_swing_pivots(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    """Return only confirmed, non-repainting pivots from completed bars."""
    if not SWING_PIVOT_ENABLED or len(candles) < 8:
        return {"enabled": SWING_PIVOT_ENABLED, "status": "INSUFFICIENT_DATA"}
    left, right = SWING_PIVOT_LEFT_BARS, SWING_PIVOT_RIGHT_BARS
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []
    # Exclude the newest bar because the exchange may still be building it.
    bars = candles[:-1]
    for i in range(left, len(bars) - right):
        window = bars[i-left:i+right+1]
        h, lo = safe_float(bars[i].get("high"), 0), safe_float(bars[i].get("low"), 0)
        if h and h == max(safe_float(x.get("high"), 0) for x in window):
            highs.append({"price": h, "start": bars[i].get("start"), "index": i})
        if lo and lo == min(safe_float(x.get("low"), float("inf")) for x in window):
            lows.append({"price": lo, "start": bars[i].get("start"), "index": i})
    last_high, prev_high = (highs[-1] if highs else None), (highs[-2] if len(highs) > 1 else None)
    last_low, prev_low = (lows[-1] if lows else None), (lows[-2] if len(lows) > 1 else None)
    structure = "UNCLASSIFIED"
    if last_high and prev_high and last_low and prev_low:
        if last_high["price"] > prev_high["price"] and last_low["price"] > prev_low["price"]:
            structure = "BULLISH_HH_HL"
        elif last_high["price"] < prev_high["price"] and last_low["price"] < prev_low["price"]:
            structure = "BEARISH_LH_LL"
        else:
            structure = "RANGE_OR_TRANSITION"
    return {
        "enabled": True, "status": "SHADOW", "structure": structure,
        "last_swing_high": last_high, "previous_swing_high": prev_high,
        "last_swing_low": last_low, "previous_swing_low": prev_low,
        "confirmed_at": iso_utc(), "left_bars": left, "right_bars": right,
    }


def update_position_version(controls: Dict[str, Any], live_pos: Dict[str, Any], atr_locked: float) -> None:
    signed = safe_int(live_pos.get("signed_contracts"), 0)
    avg = safe_float(live_pos.get("avg_entry_price"), 0.0)
    fingerprint = f"{signed}:{avg:.8f}"
    if controls.get("position_fingerprint") != fingerprint:
        previous = controls.get("position_fingerprint")
        controls["position_version"] = safe_int(controls.get("position_version"), 0) + 1
        controls["position_fingerprint"] = fingerprint
        controls["position_reanchor"] = {
            "version": controls["position_version"], "previous_fingerprint": previous,
            "new_fingerprint": fingerprint, "signed_contracts": signed,
            "exchange_avg_entry": avg, "locked_atr": atr_locked,
            "reanchored_at": iso_utc(), "verified": bool(signed and avg and atr_locked),
        }


def adaptive_defense_snapshot(state: Dict[str, Any], live_pos: Dict[str, Any], sig: SignalSnapshot,
                              candles: List[Dict[str, float]], structure: Dict[str, Any]) -> Dict[str, Any]:
    """Score independent evidence that the open-position thesis is deteriorating."""
    signed = safe_int(live_pos.get("signed_contracts"), 0)
    if not signed:
        return {"enabled": ADAPTIVE_DEFENSE_ENABLED, "score": 0, "state": "FLAT", "evidence": []}
    side = "LONG" if signed > 0 else "SHORT"
    evidence: List[Dict[str, Any]] = []
    score = 0
    closes = [safe_float(c.get("close"), 0) for c in candles[-6:] if safe_float(c.get("close"), 0) > 0]
    last_open = safe_float(candles[-1].get("open"), sig.price) if candles else sig.price
    adverse_candle = sig.price < last_open if side == "LONG" else sig.price > last_open
    if len(closes) >= 4:
        moves = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        adverse_moves = sum(1 for m in moves[-3:] if (m < 0 if side == "LONG" else m > 0))
        if adverse_moves >= 2:
            score += 20; evidence.append({"factor": "adverse_momentum", "points": 20})
        if len(moves) >= 2 and abs(moves[-1]) > abs(moves[-2]) and (moves[-1] < 0 if side == "LONG" else moves[-1] > 0):
            score += 10; evidence.append({"factor": "momentum_accelerating", "points": 10})
    if adverse_candle and sig.volume_ratio >= VOL_SPIKE_MIN:
        score += 20; evidence.append({"factor": "adverse_volume_expansion", "points": 20, "volume_ratio": sig.volume_ratio})
    pivot = (structure.get("last_swing_low") or {}).get("price") if side == "LONG" else (structure.get("last_swing_high") or {}).get("price")
    if pivot and (sig.price < pivot if side == "LONG" else sig.price > pivot):
        score += 35; evidence.append({"factor": "confirmed_structure_break", "points": 35, "pivot": pivot})
    if sig.mid_bb and (sig.price < sig.mid_bb if side == "LONG" else sig.price > sig.mid_bb):
        score += 10; evidence.append({"factor": "wrong_side_of_mid_band", "points": 10})
    if sig.rsi is not None and ((side == "LONG" and sig.rsi < 40) or (side == "SHORT" and sig.rsi > 60)):
        score += 10; evidence.append({"factor": "adverse_rsi_regime", "points": 10, "rsi": sig.rsi})
    score = min(100, score)
    prior = ((state.get("position_controls") or {}).get("adaptive_defense") or {})
    cycles = safe_int(prior.get("confirm_cycles"), 0) + 1 if score >= ADAPTIVE_REDUCE_SCORE else 0
    action = "HOLD"
    if score >= ADAPTIVE_EXIT_SCORE and cycles >= ADAPTIVE_CONFIRM_CYCLES:
        action = "EXIT"
    elif score >= ADAPTIVE_REDUCE_SCORE and cycles >= ADAPTIVE_CONFIRM_CYCLES:
        action = "REDUCE_ONE_RUNG"
    elif score >= ADAPTIVE_REDUCE_SCORE:
        action = "CONFIRMING"
    return {"enabled": ADAPTIVE_DEFENSE_ENABLED, "score": score, "state": action,
            "confirm_cycles": cycles, "required_cycles": ADAPTIVE_CONFIRM_CYCLES,
            "evidence": evidence, "side": side, "evaluated_at": iso_utc()}


def start_adaptive_reentry_guard(state: Dict[str, Any], side: str, reason: str) -> Dict[str, Any]:
    """Latch a same-side block until the old signal disappears and recovery is observed.

    The v33 cooldown was time-only, so a persistent oversold/overbought reading
    could be treated as a fresh setup as soon as 15 minutes elapsed. This guard
    restores the older FSM's intended separation between setups: the triggering
    signal must first clear, then a later setup may arm.
    """
    prior_phantom = state.get("phantom") or {}
    guard = {
        "active": bool(ADAPTIVE_FRESH_SETUP_REQUIRED),
        "side": side,
        "started_at": iso_utc(),
        "reason": reason,
        "exited_setup_id": prior_phantom.get("setup_id"),
        "signal_cleared": False,
        "signal_cleared_at": None,
        "recovery_seen": False,
        "recovery_seen_at": None,
        "eligible": False,
        "first_reentry_probe_only": bool(ADAPTIVE_REENTRY_PROBE_ONLY),
        "note": "Same-side entry/adds blocked until the prior signal clears and recovery is observed.",
    }
    state["adaptive_reentry_guard"] = guard
    return guard


def update_adaptive_reentry_guard(state: Dict[str, Any], sig: SignalSnapshot,
                                  structure: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    guard = state.setdefault("adaptive_reentry_guard", {
        "active": False, "side": None, "signal_cleared": False, "recovery_seen": False,
    })
    if not guard.get("active"):
        return guard
    side = str(guard.get("side") or "").upper()
    score = sig.long_score if side == "LONG" else sig.short_score
    if not guard.get("signal_cleared") and score <= SIGNAL_CANCEL_SCORE:
        guard["signal_cleared"] = True
        guard["signal_cleared_at"] = iso_utc()

    structure_name = str((structure or {}).get("structure") or "UNCLASSIFIED").upper()
    supportive_mid = bool(
        sig.mid_bb and (
            (side == "LONG" and sig.price >= sig.mid_bb)
            or (side == "SHORT" and sig.price <= sig.mid_bb)
        )
    )
    non_adverse_structure = (
        side == "LONG" and structure_name != "BEARISH_LH_LL"
    ) or (
        side == "SHORT" and structure_name != "BULLISH_HH_HL"
    )
    recovery_now = supportive_mid or non_adverse_structure
    if guard.get("signal_cleared") and recovery_now and not guard.get("recovery_seen"):
        guard["recovery_seen"] = True
        guard["recovery_seen_at"] = iso_utc()
        guard["recovery_basis"] = "mid_band_reclaim" if supportive_mid else "structure_no_longer_adverse"
    guard["current_score"] = score
    guard["current_structure"] = structure_name
    guard["supportive_mid_band"] = supportive_mid
    guard["eligible"] = bool(
        guard.get("signal_cleared")
        and (
            guard.get("recovery_seen")
            or not ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND
        )
    )
    guard["updated_at"] = iso_utc()
    return guard


def adaptive_reentry_allows(state: Dict[str, Any], side: str,
                            setup_started_at: Any = None) -> Tuple[bool, str]:
    guard = state.get("adaptive_reentry_guard") or {}
    if not guard.get("active") or str(guard.get("side") or "").upper() != side.upper():
        return True, "OK"
    if not guard.get("signal_cleared"):
        return False, f"Fresh {side.upper()} setup required: prior signal has not cleared"
    if ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND and not guard.get("recovery_seen"):
        return False, f"Fresh {side.upper()} setup required: waiting for structure or mid-band recovery"
    cleared_at = parse_dt(guard.get("signal_cleared_at"))
    setup_at = parse_dt(setup_started_at)
    if not setup_at or (cleared_at and setup_at <= cleared_at):
        return False, f"Fresh {side.upper()} setup required: waiting for a new post-clear setup"
    return True, "Fresh setup verified"


def resolve_adaptive_reentry_guard(state: Dict[str, Any], side: str, setup_id: Any) -> None:
    guard = state.get("adaptive_reentry_guard") or {}
    if not guard.get("active") or str(guard.get("side") or "").upper() != side.upper():
        return
    guard.update({
        "active": False,
        "eligible": False,
        "resolved_at": iso_utc(),
        "resolved_by_setup_id": setup_id,
        "resolution": "fresh_same_side_probe_executed",
    })


def update_stop_blown_shadow(state: Dict[str, Any], price: float, atr_now: float) -> None:
    sb = state.setdefault("stop_blown", {"active": False, "mode": "SHADOW"})
    if not sb.get("active") or price <= 0:
        return
    anchor, direction = safe_float(sb.get("anchor"), 0), sb.get("stopped_side")
    envelope = safe_float(sb.get("atr"), 0) or atr_now
    if not anchor or not envelope:
        return
    adverse = (anchor - price) if direction == "LONG" else (price - anchor)
    recovery = (price - anchor) if direction == "LONG" else (anchor - price)
    scores = {
        "FISHED": max(0.0, min(1.0, recovery / (0.25 * envelope))),
        "SAVED": max(0.0, min(1.0, adverse / (0.75 * envelope))),
        "EXTREME": max(0.0, min(1.0, (adverse / envelope - 0.75) / 0.75)),
    }
    scores["UNCLEAR"] = max(0.0, 1.0 - max(scores.values()))
    recent_fished = 0
    for event in (state.get("stop_blown_history") or [])[-20:]:
        event_dt = parse_dt(event.get("at"))
        if (event.get("side") == direction and event.get("leader") == "FISHED" and event_dt
                and now_utc() - event_dt <= timedelta(minutes=30)):
            recent_fished += 1
    scores["BURNED"] = min(1.0, recent_fished / 3.0)
    sb.update({"scores": scores, "leader": max(scores, key=scores.get), "last_price": price,
               "updated_at": iso_utc(), "mode": "SHADOW", "trading_action": "NONE"})


def update_position_risk_controls(state: Dict[str, Any], live_pos: Dict[str, Any], sig: SignalSnapshot,
                                  candles: Optional[List[Dict[str, float]]] = None) -> Dict[str, Any]:
    controls = state.setdefault("position_controls", default_engine_state()["position_controls"])
    signed = live_pos.get("signed_contracts", 0)
    price = sig.price or live_pos.get("current_price") or 0.0
    avg = live_pos.get("avg_entry_price") or 0.0
    a = sig.atr or 0.0
    structure = classify_swing_pivots(candles or [])
    state["market_structure"] = structure

    if signed == 0 or price <= 0 or avg <= 0:
        controls.update({
            "highest_price": None, "lowest_price": None,
            "atr_stop": None, "atr_at_entry": None, "atr_entry_avg": None,
            "tsl_active": False, "tsl_stop": None,
            "tp1_done": False, "tp1_trigger_price": None, "tp1_pct_active": None, "tp1_target_contracts": None,
            "phantom_extension_add_done": False, "phantom_extension_target_price": None, "phantom_extension_target_contracts": None,
            "adaptive_defense": {"enabled": ADAPTIVE_DEFENSE_ENABLED, "score": 0, "state": "FLAT", "evidence": []},
        })
        return controls

    # v12: lock ATR at entry. The "entry" is defined as the first cycle the bot
    # observes this position (or a position whose avg_entry_price has changed,
    # which indicates a new entry rather than the same trade aging).
    snapshotted_avg = safe_float(controls.get("atr_entry_avg"), 0.0)
    if a and (controls.get("atr_at_entry") is None or snapshotted_avg != avg):
        controls["atr_at_entry"] = a
        controls["atr_entry_avg"] = avg
        controls["tp1_done"] = False  # reset on any new entry

    atr_locked = safe_float(controls.get("atr_at_entry"), 0.0) or a
    update_position_version(controls, live_pos, atr_locked)
    controls["adaptive_defense"] = adaptive_defense_snapshot(state, live_pos, sig, candles or [], structure)

    side = "LONG" if signed > 0 else "SHORT"
    if side == "LONG":
        controls["highest_price"] = max(safe_float(controls.get("highest_price"), avg), price)
        controls["lowest_price"] = None
        controls["atr_stop"] = avg - atr_locked * ATR_MULTIPLIER if atr_locked else None
        # v24: dynamic ladder TP1. Larger positions lock gains faster.
        tp_pct = tp1_trigger_pct_for_position(abs(signed))
        controls["tp1_pct_active"] = tp_pct
        controls["tp1_target_contracts"] = next_lower_ladder_target(abs(signed))
        controls["tp1_trigger_price"] = (avg + atr_locked * ATR_MULTIPLIER * TP1_R_MULTIPLE) if (TP1_USE_R_MULTIPLE and atr_locked) else avg * (1 + tp_pct)
        # Activate TSL based on the BEST price seen since this position was detected,
        # not only the current tick. This is critical for manual/exchange positions:
        # if price traded above activation and then pulled back before the next cycle,
        # the TSL must still arm as soon as highest_price crosses the activation level.
        high = safe_float(controls.get("highest_price"), avg)
        gain_pct = (high / avg) - 1 if avg else 0.0
        activation_price = avg * (1 + TSL_ACTIVATION_PCT)
        controls["tsl_activation_price"] = activation_price
        if gain_pct >= TSL_ACTIVATION_PCT:
            if not controls.get("tsl_active"):
                logging.info(
                    "TSL ACTIVATED LONG: avg=%.2f high=%.2f activation=%.2f trail=%.4f",
                    avg, high, activation_price, TSL_TRAIL_PCT,
                )
            controls["tsl_active"] = True
        if controls.get("tsl_active"):
            controls["tsl_stop"] = high * (1 - TSL_TRAIL_PCT)
    else:
        controls["lowest_price"] = min(safe_float(controls.get("lowest_price"), avg) or avg, price)
        controls["highest_price"] = None
        controls["atr_stop"] = avg + atr_locked * ATR_MULTIPLIER if atr_locked else None
        tp_pct = tp1_trigger_pct_for_position(abs(signed))
        controls["tp1_pct_active"] = tp_pct
        controls["tp1_target_contracts"] = next_lower_ladder_target(abs(signed))
        controls["tp1_trigger_price"] = (avg - atr_locked * ATR_MULTIPLIER * TP1_R_MULTIPLE) if (TP1_USE_R_MULTIPLE and atr_locked) else avg * (1 - tp_pct)
        # For shorts, activate TSL based on the LOWEST price seen since detection.
        low = safe_float(controls.get("lowest_price"), avg) or avg
        gain_pct = (avg / low) - 1 if low else 0.0
        activation_price = avg * (1 - TSL_ACTIVATION_PCT)
        controls["tsl_activation_price"] = activation_price
        if gain_pct >= TSL_ACTIVATION_PCT:
            if not controls.get("tsl_active"):
                logging.info(
                    "TSL ACTIVATED SHORT: avg=%.2f low=%.2f activation=%.2f trail=%.4f",
                    avg, low, activation_price, TSL_TRAIL_PCT,
                )
            controls["tsl_active"] = True
        if controls.get("tsl_active"):
            controls["tsl_stop"] = low * (1 + TSL_TRAIL_PCT)
    return controls


def risk_exit_target_if_needed(live_pos: Dict[str, Any], controls: Dict[str, Any], price: float) -> Tuple[Optional[int], Optional[str]]:
    """Return (target_signed_contracts, reason).

    v24: TP1_LADDER_STEPDOWN_* returns a non-zero whole-contract target that steps down to the next lower ladder rung rather than flattening. The caller must execute the returned target exactly as given.
    """
    signed = safe_int(live_pos.get("signed_contracts"), 0)
    if signed == 0 or price <= 0:
        return None, None
    side = "LONG" if signed > 0 else "SHORT"
    atr_stop = controls.get("atr_stop")
    tsl_stop = controls.get("tsl_stop") if controls.get("tsl_active") else None
    tp1_trigger = controls.get("tp1_trigger_price")
    tp1_done = bool(controls.get("tp1_done"))
    defense = controls.get("adaptive_defense") or {}

    if side == "LONG":
        if atr_stop and price <= atr_stop:
            return 0, "ATR_STOP_LONG"
        if tsl_stop and price <= tsl_stop:
            return 0, "TSL_STOP_LONG"
        if ADAPTIVE_DEFENSE_ENABLED and defense.get("state") == "EXIT":
            return 0, "ADAPTIVE_DEFENSE_EXIT_LONG"
        if ADAPTIVE_DEFENSE_ENABLED and defense.get("state") == "REDUCE_ONE_RUNG":
            keep = next_lower_ladder_target(abs(signed))
            if keep < abs(signed):
                return keep, "ADAPTIVE_DEFENSE_REDUCE_LONG"
        if (not tp1_done) and tp1_trigger and price >= tp1_trigger:
            keep = next_lower_ladder_target(abs(signed))
            if keep < abs(signed):
                return keep, "TP1_LADDER_STEPDOWN_LONG"
    else:
        if atr_stop and price >= atr_stop:
            return 0, "ATR_STOP_SHORT"
        if tsl_stop and price >= tsl_stop:
            return 0, "TSL_STOP_SHORT"
        if ADAPTIVE_DEFENSE_ENABLED and defense.get("state") == "EXIT":
            return 0, "ADAPTIVE_DEFENSE_EXIT_SHORT"
        if ADAPTIVE_DEFENSE_ENABLED and defense.get("state") == "REDUCE_ONE_RUNG":
            keep = next_lower_ladder_target(abs(signed))
            if keep < abs(signed):
                return -keep, "ADAPTIVE_DEFENSE_REDUCE_SHORT"
        if (not tp1_done) and tp1_trigger and price <= tp1_trigger:
            keep = next_lower_ladder_target(abs(signed))
            if keep < abs(signed):
                return -keep, "TP1_LADDER_STEPDOWN_SHORT"
    return None, None


def record_exit_risk_result(state: Dict[str, Any], reason: str) -> None:
    risk = state.setdefault("risk", {})
    if "STOP" in reason:
        risk["daily_stop_hits"] = safe_int(risk.get("daily_stop_hits")) + 1
        risk["loss_streak"] = safe_int(risk.get("loss_streak")) + 1
        if safe_int(risk.get("loss_streak")) >= LOSS_STREAK_LIMIT:
            risk["pause_until"] = (now_utc() + timedelta(minutes=STREAK_PAUSE_MINUTES)).isoformat()
            risk["halt_reason"] = "post_stop_cooldown"


def recover_bot_managed_position_from_ledger(gcs: GCS, state: Dict[str, Any], live_pos: Dict[str, Any]) -> bool:
    """Recover ownership only when persisted bot-management continuity is provable.

    A historical ledger row is supporting evidence, never sufficient evidence by
    itself. If the prior cycle did not already identify this exact exchange
    position as bot-managed, fail closed and leave it monitor-only.
    """
    live_signed = safe_int(live_pos.get("signed_contracts"), 0)
    if live_signed == 0 or state.get("bot_managed_position"):
        return False
    prior_status = state.get("manual_position_status") or {}
    prior_live = state.get("last_exchange_position") or {}
    prior_signed = safe_int(prior_live.get("signed_contracts"), 0)
    same_product = str(prior_live.get("product_id") or "") == str(live_pos.get("product_id") or "")
    prior_avg = safe_float(prior_live.get("avg_entry_price"), 0.0)
    live_avg = safe_float(live_pos.get("avg_entry_price"), 0.0)
    same_average = bool(prior_avg and live_avg and abs(prior_avg - live_avg) <= 0.01)
    continuity_proven = (
        bool(prior_status.get("bot_managed"))
        and bool(prior_status.get("allow_bot_to_trade_position"))
        and prior_signed == live_signed
        and same_product
        and same_average
    )
    if not continuity_proven:
        state["ownership_recovery"] = {
            "recovered": False,
            "at": iso_utc(),
            "reason": "persisted_bot_management_continuity_not_proven",
            "live_signed": live_signed,
            "prior_signed": prior_signed,
            "same_product": same_product,
            "same_average": same_average,
        }
        return False
    try:
        raw = gcs.read_text(PERP_TRADES_LEDGER_BLOB, default="")
        if not raw.strip():
            return False
        rows = list(csv.DictReader(raw.splitlines()))
        last = next((r for r in reversed(rows) if str(r.get("ok", "")).strip().lower() in ("true", "1", "yes")), None)
        if not last:
            return False
        ledger_after = safe_int(last.get("after_signed"), 0)
        client_order_id = str(last.get("client_order_id") or "")
        if ledger_after != live_signed or not client_order_id.startswith(("larry-v2-", "larry-v32-")):
            return False
        state["bot_managed_position"] = {
            "signed_contracts": live_signed,
            "side": live_pos.get("side"),
            "contracts": live_pos.get("contracts"),
            "avg_entry_price": live_pos.get("avg_entry_price"),
            "current_price": live_pos.get("current_price"),
            "unrealized_pnl": live_pos.get("unrealized_pnl"),
            "daily_realized_pnl": live_pos.get("daily_realized_pnl"),
            "product_id": live_pos.get("product_id"),
            "marked_at": iso_utc(),
            "source_reason": last.get("reason"),
            "ownership_source": "recovered_from_canonical_larry_ledger",
            "recovered_client_order_id": client_order_id,
            "recovered_trade_timestamp": last.get("timestamp"),
        }
        state["ownership_recovery"] = {
            "recovered": True,
            "at": iso_utc(),
            "live_signed": live_signed,
            "ledger_after_signed": ledger_after,
            "client_order_id": client_order_id,
        }
        log.error("SAFETY RECOVERY: restored bot ownership from Larry ledger for live signed position %s", live_signed)
        try:
            send_telegram_message(
                f"⚠️ LARRY OWNERSHIP RECOVERED\nLive position: {live_pos.get('side')} {live_pos.get('contracts')}\n"
                f"Matched Larry order: {client_order_id}\nATR/TSL management restored after ledger verification.\nTime: {et_timestamp_short()}",
                event_type="OWNERSHIP_RECOVERED",
            )
        except Exception as notify_exc:
            log.warning("Ownership recovery notification failed: %s", notify_exc)
        return True
    except Exception as exc:
        log.warning("Bot ownership recovery check failed closed: %s", exc)
        return False


def live_position_management_status(state: Dict[str, Any], live_pos: Dict[str, Any]) -> Dict[str, Any]:
    """Classify whether the current live exchange position is bot-managed.

    In monitor_only mode, any live position that was not explicitly created by
    this bot in the current lifecycle is treated as manual/external. The bot may
    still display and email-reconcile it, but it must not place ATR/TSL exits,
    adds, flips, or flattening trades against it.
    """
    signed = safe_int(live_pos.get("signed_contracts"), 0)
    bot_pos = state.get("bot_managed_position") or {}
    bot_signed = safe_int(bot_pos.get("signed_contracts"), 0)
    same_product = str(bot_pos.get("product_id") or "") == str(live_pos.get("product_id") or "")
    bot_avg = safe_float(bot_pos.get("avg_entry_price"), 0.0)
    live_avg = safe_float(live_pos.get("avg_entry_price"), 0.0)
    same_average = bool(bot_avg and live_avg and abs(bot_avg - live_avg) <= 0.01)
    mode = MANUAL_POSITION_MODE if MANUAL_POSITION_MODE in ("monitor_only", "full_management") else "monitor_only"

    if signed == 0:
        return {
            "mode": mode,
            "is_manual_or_external": False,
            "bot_managed": False,
            "allow_bot_to_trade_position": True,
            "reason": "flat",
            "bot_managed_signed": bot_signed,
            "live_signed": signed,
        }

    if mode == "full_management":
        return {
            "mode": mode,
            "is_manual_or_external": False,
            "bot_managed": True,
            "allow_bot_to_trade_position": True,
            "reason": "manual_position_mode_full_management",
            "bot_managed_signed": bot_signed,
            "live_signed": signed,
        }

    # monitor_only default: only positions explicitly recorded as bot-managed
    # can be modified by ATR/TSL/core exits. If the user adds or reduces manually,
    # the signed exposure diverges and the whole live position becomes monitor-only.
    if bot_signed != 0 and bot_signed == signed and same_product and same_average:
        return {
            "mode": mode,
            "is_manual_or_external": False,
            "bot_managed": True,
            "allow_bot_to_trade_position": True,
            "reason": "live_position_matches_bot_managed_position",
            "bot_managed_signed": bot_signed,
            "live_signed": signed,
        }

    return {
        "mode": mode,
        "is_manual_or_external": True,
        "bot_managed": False,
        "allow_bot_to_trade_position": False,
        "reason": "position_ownership_not_proven_monitor_only",
        "bot_managed_signed": bot_signed,
        "live_signed": signed,
        "same_product": same_product,
        "same_average": same_average,
    }


def mark_bot_managed_position_from_result(state: Dict[str, Any], result: Optional[Dict[str, Any]], reason: str) -> None:
    """Record bot-managed exposure only for successful bot entry/add actions."""
    if not result or not result.get("ok"):
        return
    after = result.get("after") or {}
    after_signed = safe_int(after.get("signed_contracts"), 0)
    if after_signed == 0:
        state["bot_managed_position"] = None
        return
    if reason.startswith("CORE_IAF_") or reason.startswith("SPOT_BRIDGE_"):
        state["bot_managed_position"] = {
            "signed_contracts": after_signed,
            "side": after.get("side"),
            "contracts": after.get("contracts"),
            "avg_entry_price": after.get("avg_entry_price"),
            "product_id": after.get("product_id"),
            "marked_at": iso_utc(),
            "source_reason": reason,
        }


def reset_tsl_after_position_increase(state: Dict[str, Any], result: Dict[str, Any], reason: str) -> None:
    """v26 safety fix: reset trailing-stop state after any Larry add-on/increase.

    Bug fixed: a small runner could carry an old high/low watermark. If Larry later
    added to that position, the enlarged position inherited the stale TSL watermark
    and could be stopped immediately. After any same-direction position increase,
    TSL must restart from the post-fill live price/average. ATR and TP are allowed
    to re-snapshot on the next risk-control cycle.
    """
    plan = result.get("plan") or {}
    before = result.get("before") or {}
    after = result.get("after") or {}
    before_signed = safe_int(before.get("signed_contracts"), 0)
    after_signed = safe_int(after.get("signed_contracts"), 0)
    if before_signed == 0 or after_signed == 0:
        return
    if (before_signed > 0) != (after_signed > 0):
        return
    if abs(after_signed) <= abs(before_signed):
        return

    side = "LONG" if after_signed > 0 else "SHORT"
    current_price = safe_float(after.get("current_price"), 0.0)
    if current_price <= 0:
        current_price = safe_float((result.get("fills") or {}).get("avg_price"), 0.0)
    avg = safe_float(after.get("avg_entry_price"), current_price)

    pc = state.setdefault("position_controls", default_engine_state()["position_controls"])
    # Reset trailing state so the resized position cannot inherit an old watermark.
    pc["highest_price"] = current_price if side == "LONG" and current_price > 0 else None
    pc["lowest_price"] = current_price if side == "SHORT" and current_price > 0 else None
    pc["tsl_active"] = False
    pc["tsl_stop"] = None
    pc["tsl_activation_price"] = None
    # Re-lock ATR and TP1 from the new blended position on the next cycle.
    pc["atr_at_entry"] = None
    pc["atr_entry_avg"] = avg if avg > 0 else None
    pc["tp1_done"] = False
    pc["tp1_trigger_price"] = None
    pc["tp1_pct_active"] = None
    pc["tp1_target_contracts"] = None
    pc["tsl_reset_after_add"] = {
        "reset_at": iso_utc(),
        "reason": reason,
        "before_signed": before_signed,
        "after_signed": after_signed,
        "side": side,
        "reference_price": current_price,
        "avg_entry_price": avg,
    }
    logging.warning(
        "TSL reset after position increase: reason=%s %s %s -> %s ref=%.2f avg=%.2f",
        reason, side, before_signed, after_signed, current_price, avg,
    )


def sync_bot_managed_position_after_trade(state: Dict[str, Any], result: Optional[Dict[str, Any]], reason: str) -> None:
    """v20 clean: keep bot-managed state aligned after entries, TP1 partials, ATR/TSL exits.

    Larry only calls this for Larry-initiated trades. Therefore the post-trade
    exchange position is the bot-managed position unless it is flat. This fixes
    stale state after partial exits where Coinbase showed LONG 1 but local
    bot_managed_position still showed LONG 2.
    """
    if not result or not result.get("ok"):
        return
    plan = result.get("plan") or {}
    if plan.get("action") in (None, "NONE"):
        return
    after = result.get("after") or {}
    after_signed = safe_int(after.get("signed_contracts"), 0)
    if after_signed == 0:
        state["bot_managed_position"] = None
    else:
        state["bot_managed_position"] = {
            "signed_contracts": after_signed,
            "side": after.get("side"),
            "contracts": after.get("contracts"),
            "avg_entry_price": after.get("avg_entry_price"),
            "current_price": after.get("current_price"),
            "unrealized_pnl": after.get("unrealized_pnl"),
            "daily_realized_pnl": after.get("daily_realized_pnl"),
            "product_id": after.get("product_id"),
            "marked_at": iso_utc(),
            "source_reason": reason,
        }
    # v26: if this trade increased an existing same-direction position, clear stale TSL watermarks.
    reset_tsl_after_position_increase(state, result, reason)

    # v24: after a fresh entry, carry the armed setup's extension target into open-position controls.
    ph = state.get("phantom") or {}
    if after_signed != 0 and ph.get("extension_price"):
        pc = state.setdefault("position_controls", default_engine_state()["position_controls"])
        pc["phantom_extension_add_done"] = bool(ph.get("extension_achieved"))
        pc["phantom_extension_target_price"] = ph.get("extension_price")
        pc["phantom_extension_target_contracts"] = (ph.get("extension_target_contracts") or sizing_ladder_contracts().get("partial"))
        pc["phantom_extension_source"] = ph.get("signal_class")

    state["last_position_sync"] = {
        "synced_at": iso_utc(),
        "reason": reason,
        "after_signed": after_signed,
        "source": "larry_initiated_trade_result",
    }


# =============================================================================
# COOLDOWN / PORTFOLIO GUARDS
# =============================================================================

def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # v30 fix: a timestamp with no UTC offset used to be treated as naive-local,
        # which skewed cooldown/expiry math depending on the VM's local timezone.
        # All timestamps this bot writes are UTC; assume the same on read.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def cooldown_status(state: Dict[str, Any], key: str) -> Dict[str, Any]:
    cds = state.setdefault("cooldowns", {})
    min_seconds = safe_int(cds.get("min_seconds"), MIN_ENTRY_COOLDOWN_SECONDS)
    last = parse_dt(cds.get(key))
    if not last:
        return {"key": key, "active": False, "remaining_seconds": 0, "last_entry_at": None, "min_seconds": min_seconds}
    elapsed = (now_utc() - last).total_seconds()
    remaining = max(0, int(min_seconds - elapsed))
    return {"key": key, "active": remaining > 0, "remaining_seconds": remaining, "last_entry_at": cds.get(key), "min_seconds": min_seconds}


def mark_cooldown(state: Dict[str, Any], key: str) -> None:
    cds = state.setdefault("cooldowns", {})
    cds.setdefault("min_seconds", MIN_ENTRY_COOLDOWN_SECONDS)
    cds[key] = iso_utc()


def futures_equity_summary(cb: Any) -> Dict[str, Any]:
    try:
        raw = obj_to_dict(cb.get_futures_balance_summary())
        # SDK shapes vary. Try common fields.
        def money(path, default=0.0):
            obj = raw
            for part in path.split('.'):
                if isinstance(obj, dict):
                    obj = obj.get(part)
                else:
                    return default
            if isinstance(obj, dict):
                return safe_float(obj.get('value'), default)
            return safe_float(obj, default)
        total = money('balance_summary.total_usd_balance') or money('total_usd_balance') or money('cfm_usd_balance')
        available = money('balance_summary.available_margin') or money('available_margin')
        return {"ok": True, "total_usd_balance": total, "available_margin": available, "raw": raw}
    except Exception as e:
        return {"ok": False, "error": str(e), "total_usd_balance": 0.0, "available_margin": 0.0}


def portfolio_guard_resize_target(cb: Any, target_signed: int, current_signed: int, price: float) -> Tuple[int, bool, str, Dict[str, Any]]:
    """v31: leverage guard RESIZES oversized targets instead of blocking them.

    The old guard was binary: a score-4 signal targeting 20 contracts on a small
    account exceeded MAX_EFFECTIVE_LEVERAGE and the ENTIRE trade was blocked --
    the strongest signals were exactly the ones that traded nothing. Now the
    target is clamped to the largest whole-contract size that stays within the
    leverage cap, and only genuinely un-executable situations block:
      - equity below MIN_FUTURES_EQUITY_BUFFER_USD (hard floor, never resized)
      - leverage cap allows zero contracts
      - a same-direction add when the live position already sits at/over the cap
        (an entry path must never force-reduce an existing position)

    Returns (resized_target_signed, ok, reason, info).
    """
    bal = futures_equity_summary(cb)
    equity = safe_float(bal.get("total_usd_balance"), 0.0)
    target_notional = abs(target_signed) * CONTRACT_SIZE_BTC * price if price else 0.0
    lev = target_notional / equity if equity else None
    info: Dict[str, Any] = {"balance": bal, "target_notional": target_notional, "projected_leverage": lev}
    if equity and equity < MIN_FUTURES_EQUITY_BUFFER_USD:
        return target_signed, False, f"Futures equity buffer too low: ${equity:,.2f} < ${MIN_FUTURES_EQUITY_BUFFER_USD:,.2f}", info
    if not equity or not price or price <= 0 or CONTRACT_SIZE_BTC <= 0:
        # Leverage not computable (missing equity or price). Preserve the old
        # behavior for this case: allow unchanged rather than block on bad data.
        return target_signed, True, "OK (leverage not computable; equity or price unavailable)", info
    max_allowed = int((MAX_EFFECTIVE_LEVERAGE * equity) / (CONTRACT_SIZE_BTC * price))
    info["max_contracts_at_leverage_cap"] = max_allowed
    if abs(target_signed) <= max_allowed:
        return target_signed, True, "OK", info
    if max_allowed < 1:
        return target_signed, False, f"Leverage cap allows 0 contracts: equity ${equity:,.2f} at price ${price:,.2f}", info
    sign = 1 if target_signed > 0 else -1
    if current_signed * sign > 0 and abs(current_signed) >= max_allowed:
        return target_signed, False, f"Live position already at leverage cap ({abs(current_signed)} >= {max_allowed} contracts); add skipped", info
    resized = sign * max_allowed
    resized_lev = (abs(resized) * CONTRACT_SIZE_BTC * price) / equity
    info.update({"resized_from": target_signed, "resized_to": resized, "projected_leverage": resized_lev})
    reason = (
        f"Target resized {target_signed} -> {resized}: {MAX_EFFECTIVE_LEVERAGE:.2f}x cap allows "
        f"{max_allowed} contracts at equity ${equity:,.2f} / price ${price:,.2f}"
    )
    return resized, True, reason, info

# =============================================================================
# OPTIONAL SPOT BRIDGE HOOKS
# =============================================================================



# =============================================================================
# SPOT TREASURY + COINBASE SPOT EXECUTION
# =============================================================================

def calculate_macro_regime_from_candles(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    closes = [c["close"] for c in candles if c.get("close")]
    price = closes[-1] if closes else 0.0
    fast = sma(closes, MACRO_FAST_SMA)
    slow = sma(closes, MACRO_SLOW_SMA)
    gate_open = bool(price and fast and slow and price > fast and price > slow and fast > slow)
    return {
        "price": price,
        "fast_sma": fast,
        "slow_sma": slow,
        "fast_period": MACRO_FAST_SMA,
        "slow_period": MACRO_SLOW_SMA,
        "gate_open": gate_open,
        "state": "BULLISH" if gate_open else "BLOCKED",
        "reason": "Price > fast SMA, price > slow SMA, fast SMA > slow SMA" if gate_open else "Macro gate blocked: requires price > fast SMA, price > slow SMA, and fast SMA > slow SMA",
    }


def get_spot_accounts(cb: Any) -> Dict[str, float]:
    out = {"USD": 0.0, "USDC": 0.0, "BTC": 0.0}
    try:
        accts = obj_to_dict(cb.get_accounts())
        rows = accts.get("accounts", []) if isinstance(accts, dict) else []
        for a in rows:
            cur = a.get("currency") or (a.get("available_balance") or {}).get("currency")
            bal = safe_float((a.get("available_balance") or {}).get("value"))
            if cur in out:
                out[cur] += bal
    except Exception as e:
        log.warning("get_spot_accounts failed: %s", e)
    return out


def get_spot_treasury(cb: Any, btc_price: float) -> Dict[str, float]:
    accts = get_spot_accounts(cb)
    btc_value = accts.get("BTC", 0.0) * btc_price
    total = accts.get("USD", 0.0) + accts.get("USDC", 0.0) + btc_value
    cash = accts.get("USD", 0.0) + accts.get("USDC", 0.0)
    return {
        "usd": accts.get("USD", 0.0),
        "usdc": accts.get("USDC", 0.0),
        "btc_qty": accts.get("BTC", 0.0),
        "btc_value_usd": btc_value,
        "cash_usd_equiv": cash,
        "total_spot_value": total,
    }


def load_spot_state(gcs: GCS) -> Dict[str, Any]:
    st = gcs.read_json(SPOT_POSITION_STATE_BLOB, default={}) or {}
    st.setdefault("ladder_level", 0)
    st.setdefault("last_spot_entry_at", None)
    st.setdefault("last_spot_order", None)
    st.setdefault("trade_history", [])
    return st


def save_spot_state(gcs: GCS, state: Dict[str, Any]) -> None:
    state["last_updated"] = iso_utc()
    state["source"] = "coinbase_unified_spot_engine"
    gcs.write_json(SPOT_POSITION_STATE_BLOB, state)


def place_spot_market_buy(cb: Any, quote_size: float, reason: str) -> Dict[str, Any]:
    if quote_size < SPOT_MIN_ORDER_USD:
        return {"ok": True, "skipped": True, "reason": f"quote_size below minimum: {quote_size:.2f}"}
    if DRY_RUN:
        log.warning("DRY_RUN: would BUY spot %s quote_size=%.2f for %s", SPOT_PRODUCT_ID, quote_size, reason)
        return {"ok": True, "dry_run": True, "product_id": SPOT_PRODUCT_ID, "quote_size": quote_size, "reason": reason}
    client_order_id = f"larry-spot-{int(time.time())}-buy-{int(quote_size*100)}"
    try:
        res = cb.market_order_buy(client_order_id=client_order_id, product_id=SPOT_PRODUCT_ID, quote_size=str(round(quote_size, 2)))
        return {"ok": True, "response": obj_to_dict(res), "client_order_id": client_order_id, "product_id": SPOT_PRODUCT_ID, "quote_size": quote_size}
    except TypeError:
        try:
            res = cb.create_order(
                client_order_id=client_order_id,
                product_id=SPOT_PRODUCT_ID,
                side="BUY",
                order_configuration={"market_market_ioc": {"quote_size": str(round(quote_size, 2))}},
            )
            return {"ok": True, "response": obj_to_dict(res), "client_order_id": client_order_id, "product_id": SPOT_PRODUCT_ID, "quote_size": quote_size}
        except Exception as e:
            return {"ok": False, "error": str(e), "client_order_id": client_order_id, "product_id": SPOT_PRODUCT_ID, "quote_size": quote_size}
    except Exception as e:
        return {"ok": False, "error": str(e), "client_order_id": client_order_id, "product_id": SPOT_PRODUCT_ID, "quote_size": quote_size}


def send_spot_trade_email(result: Dict[str, Any], treasury: Dict[str, Any], sig: SignalSnapshot, macro: Dict[str, Any]) -> None:
    if not result.get("ok") or result.get("skipped"):
        return
    subject = f"Larry Spot Trade: BUY ${result.get('quote_size', 0):,.2f} BTC"
    body = f"""
Larry Spot Trade Executed

Reason: {result.get('reason')}
Product: {result.get('product_id')}
Quote Size: ${safe_float(result.get('quote_size')):,.2f}

Signal Score: {sig.long_score}/4
RSI: {sig.rsi}
Stoch RSI: {sig.stoch_rsi}
Volume Ratio: {sig.volume_ratio}
Price: ${sig.price:,.2f}

Macro Gate: {macro.get('state')}
Fast SMA {macro.get('fast_period')}: {macro.get('fast_sma')}
Slow SMA {macro.get('slow_period')}: {macro.get('slow_sma')}

Treasury Before/At Decision:
USD: ${treasury.get('usd', 0):,.2f}
USDC: ${treasury.get('usdc', 0):,.2f}
BTC Qty: {treasury.get('btc_qty', 0):.8f}
Spot Total: ${treasury.get('total_spot_value', 0):,.2f}

Client Order ID: {result.get('client_order_id')}
Raw Order: {json.dumps(result.get('response') or result, indent=2, default=str)[:3000]}

Timestamp: {iso_utc()}
""".strip()
    send_email(subject, body)


def maybe_handle_spot_entry(cb: Any, gcs: GCS, state: Dict[str, Any], sig: SignalSnapshot, macro: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Coinbase Spot BTC ladder. Original cumulative tranche targets: 25/33/50/90%."""
    if not ENABLE_SPOT_BTC_TRADING:
        state.setdefault("last_blocked_action", {})["spot"] = "Spot BTC trading disabled by config"
        return None
    spot_state = load_spot_state(gcs)
    treasury = get_spot_treasury(cb, sig.price or get_btc_price(cb))
    spot_state["treasury"] = treasury
    spot_state["macro_regime"] = macro
    spot_state["last_signal_score"] = sig.long_score

    # v30 fix: this used to call cooldown_status() with a throwaway one-key dict
    # instead of the real engine `state`. cooldown_status() looks for a nested
    # state["cooldowns"][key] structure; the throwaway dict never had that nesting,
    # so a fresh empty cooldown table was created on every call and the cooldown was
    # always reported inactive. MIN_ENTRY_COOLDOWN_SECONDS pacing never actually
    # applied to spot ladder buys -- a persistent qualifying score could step through
    # the entire 25/33/50/90% treasury ladder in a few loop cycles. Check directly
    # against spot_state's own persisted timestamp instead.
    last_spot_entry_dt = parse_dt(spot_state.get("last_spot_entry_at"))
    if last_spot_entry_dt:
        remaining = max(0, int(MIN_ENTRY_COOLDOWN_SECONDS - (now_utc() - last_spot_entry_dt).total_seconds()))
        cd = {
            "key": "spot_last_entry_at", "active": remaining > 0, "remaining_seconds": remaining,
            "last_entry_at": spot_state.get("last_spot_entry_at"), "min_seconds": MIN_ENTRY_COOLDOWN_SECONDS,
        }
    else:
        cd = {"key": "spot_last_entry_at", "active": False, "remaining_seconds": 0, "last_entry_at": None, "min_seconds": MIN_ENTRY_COOLDOWN_SECONDS}
    state.setdefault("cooldown_status", {})["spot"] = cd
    if cd.get("active"):
        spot_state["last_blocked_reason"] = f"Spot cooldown active: {cd['remaining_seconds']}s remaining"
        save_spot_state(gcs, spot_state)
        return None
    if not macro.get("gate_open"):
        spot_state["last_blocked_reason"] = macro.get("reason")
        save_spot_state(gcs, spot_state)
        return None
    if sig.long_score < SPOT_MIN_ENTRY_SCORE:
        spot_state["last_blocked_reason"] = f"Spot score {sig.long_score}/4 below threshold {SPOT_MIN_ENTRY_SCORE}/4"
        save_spot_state(gcs, spot_state)
        return None

    level = safe_int(spot_state.get("ladder_level"), 0)
    if level >= len(SPOT_TRANCHE_TARGETS_PCT):
        spot_state["last_blocked_reason"] = "Spot ladder already at max tranche"
        save_spot_state(gcs, spot_state)
        return None

    # Score 4 is allowed to jump to max target; otherwise step one tranche.
    target_index = len(SPOT_TRANCHE_TARGETS_PCT) - 1 if sig.long_score >= SPOT_FULL_SCORE else level
    target_pct = float(SPOT_TRANCHE_TARGETS_PCT[target_index]) / 100.0
    target_spot_value = treasury["total_spot_value"] * target_pct
    current_btc_value = treasury["btc_value_usd"]
    buy_usd = max(0.0, min(treasury["cash_usd_equiv"], target_spot_value - current_btc_value))
    if buy_usd < SPOT_MIN_ORDER_USD:
        spot_state["last_blocked_reason"] = f"Calculated spot buy ${buy_usd:.2f} below minimum ${SPOT_MIN_ORDER_USD:.2f}"
        save_spot_state(gcs, spot_state)
        return None

    order = place_spot_market_buy(cb, buy_usd, f"SPOT_LADDER_SCORE_{sig.long_score}_TARGET_{int(target_pct*100)}pct")
    result = {"ok": bool(order.get("ok")), "order": order, "quote_size": buy_usd, "target_pct": target_pct, "score": sig.long_score, "reason": f"SPOT_LADDER_SCORE_{sig.long_score}"}
    ledger_header = ["timestamp", "reason", "score", "target_pct", "quote_size", "ok", "client_order_id", "raw_order"]
    gcs.append_csv_row(SPOT_TRADES_LEDGER_BLOB, ledger_header, [iso_utc(), result["reason"], sig.long_score, target_pct, buy_usd, order.get("ok"), order.get("client_order_id"), json.dumps(order, default=str)])
    if order.get("ok"):
        spot_state["ladder_level"] = max(level + 1, target_index + 1)
        spot_state["last_spot_entry_at"] = iso_utc()
        spot_state["last_spot_order"] = result
        spot_state.setdefault("trade_history", []).append(result)
        spot_state["trade_history"] = spot_state["trade_history"][-50:]
        send_spot_trade_email({**order, "reason": result["reason"]}, treasury, sig, macro)
        # Write bridge trigger for the long-only perp bridge layer.
        if ENABLE_SPOT_BRIDGE_PERP_BUYS:
            trigger = "spot_score_4" if sig.long_score >= SPOT_FULL_SCORE else "spot_ladder_1"
            gcs.write_json("coinbase_spot_bridge_signal.json", {"active": True, "consumed": False, "trigger": trigger, "created_at": iso_utc(), "spot_quote_size": buy_usd, "score": sig.long_score})
    else:
        spot_state["last_blocked_reason"] = order.get("error") or "Spot order failed"
    save_spot_state(gcs, spot_state)
    return result


def funding_size_modifier(direction: str, funding_rate: float) -> str:
    """v12: bucket the funding rate into FULL / PARTIAL / BLOCK tiers.

    LONG  : funding > FUNDING_LONG_MAX               -> BLOCK
            FUNDING_SIZE_REDUCE_AT < funding <= MAX  -> PARTIAL
            otherwise                                -> FULL
    SHORT : funding < FUNDING_SHORT_MIN              -> BLOCK
            MIN <= funding < -FUNDING_SIZE_REDUCE_AT -> PARTIAL
            otherwise                                -> FULL
    """
    if direction == "LONG":
        if funding_rate > FUNDING_LONG_MAX:
            return "BLOCK"
        if funding_rate > FUNDING_SIZE_REDUCE_AT:
            return "PARTIAL"
        return "FULL"
    if direction == "SHORT":
        if funding_rate < FUNDING_SHORT_MIN:
            return "BLOCK"
        if funding_rate < -FUNDING_SIZE_REDUCE_AT:
            return "PARTIAL"
        return "FULL"
    return "FULL"


def sizing_ladder_contracts(max_conviction: Optional[int] = None) -> Dict[str, int]:
    """v24: derive all managed sizing tiers from Max Conviction using rounded whole contracts.

    The ladder is monotonic whenever Max Conviction is large enough. This means changing
    only MAX_CONVICTION_CONTRACTS cleanly rescales entries, add-ons, and TP reductions.
    Example Max=14 => probe 3, partial 6, strong 10, full 14.
    """
    m = max(1, safe_int(max_conviction if max_conviction is not None else MAX_CONVICTION_CONTRACTS, MAX_CONVICTION_CONTRACTS))
    probe = min(m, derived_contract_tier(m, PROBE_PCT))
    partial = min(m, derived_contract_tier(m, PARTIAL_PCT))
    strong = min(m, derived_contract_tier(m, STRONG_PCT))

    # Force sensible ordering when possible, while never exceeding full max.
    if m >= 4:
        probe = max(1, min(probe, m - 3))
        partial = max(probe + 1, min(partial, m - 2))
        strong = max(partial + 1, min(strong, m - 1))
    elif m == 3:
        probe, partial, strong = 1, 2, 2
    elif m == 2:
        probe, partial, strong = 1, 1, 1
    else:
        probe = partial = strong = 1

    tp_stepdown = tp_stepdown_ladder_from_values(probe, partial, strong, m)
    return {
        "probe": probe,
        "partial": partial,
        "strong": strong,
        "full": m,
        "probe_pct": PROBE_PCT,
        "partial_pct": PARTIAL_PCT,
        "strong_pct": STRONG_PCT,
        "tp_stepdown": tp_stepdown,
        "tp1_triggers": {
            "probe": TP1_PROBE_TRIGGER_PCT,
            "partial": TP1_PARTIAL_TRIGGER_PCT,
            "strong": TP1_STRONG_TRIGGER_PCT,
            "full": TP1_FULL_TRIGGER_PCT,
        },
    }


def tp_stepdown_ladder_from_values(probe: int, partial: int, strong: int, full: int) -> Dict[str, int]:
    """Map each ladder rung to its next lower whole-contract target.

    We keep a one-contract runner after a probe/low position rather than forcing an
    immediate full exit. ATR/TSL/signal reversal manages the final runner.
    """
    vals = sorted({0, 1, safe_int(probe, 1), safe_int(partial, 1), safe_int(strong, 1), safe_int(full, 1)})
    out = {}
    for v in vals:
        if v <= 0:
            continue
        lower = [x for x in vals if x < v]
        out[str(v)] = max(lower) if lower else 0
    return out


def next_lower_ladder_target(current_abs_contracts: int) -> int:
    """Return the next lower whole-contract TP target for the current position size."""
    cur = abs(safe_int(current_abs_contracts, 0))
    if cur <= 1:
        return cur  # leave the runner; do not TP1 a one-lot
    ladder = sizing_ladder_contracts()
    rungs = sorted({0, 1, ladder["probe"], ladder["partial"], ladder["strong"], ladder["full"]})
    lower = [x for x in rungs if x < cur]
    return max(lower) if lower else max(1, cur - 1)


def tp1_trigger_pct_for_position(current_abs_contracts: int) -> float:
    """Dynamic TP1 trigger: larger positions lock gains sooner."""
    if not TP1_DYNAMIC_BY_LADDER:
        return TP1_PCT
    cur = abs(safe_int(current_abs_contracts, 0))
    ladder = sizing_ladder_contracts()
    if cur >= ladder["full"]:
        return TP1_FULL_TRIGGER_PCT
    if cur >= ladder["strong"]:
        return TP1_STRONG_TRIGGER_PCT
    if cur >= ladder["partial"]:
        return TP1_PARTIAL_TRIGGER_PCT
    return TP1_PROBE_TRIGGER_PCT


def confidence_pct_for_signal(score: int, funding_bucket: str, macro_gate_open: bool = True, direction: str = "LONG") -> int:
    """v13: convert discrete signal quality into an operator-readable confidence %.

    This is deliberately simple and transparent:
      score 3 = moderate confidence
      score 4 = full conviction candidate
      adverse-but-allowed funding reduces confidence
      macro-blocked LONGs can only trade as small override probes.
    """
    score = int(score or 0)
    if funding_bucket == "BLOCK":
        return 0
    if score < 2:
        return 0
    if score == 2:
        base = 35
    else:
        base = 92 if score >= 4 else 58
    if funding_bucket == "PARTIAL":
        base = min(base, 48 if score < 4 else 68)
    if direction == "LONG" and not macro_gate_open:
        if score >= 4 and SCORE4_MACRO_OVERRIDE_ENABLED:
            base = min(base, 35)
        elif score == 2 and REVERSAL_PROBE_ENABLED:
            base = min(base, 30)
        else:
            return 0
    return int(max(0, min(100, base)))


def contracts_for_signal(score: int, funding_bucket: str, macro_gate_open: bool = True, direction: str = "LONG") -> int:
    """v24: target size is a rounded tier derived from MAX_CONVICTION_CONTRACTS.

    Probe/partial/strong/full scale automatically when Max Conviction changes.
    Phantom extension is no longer an entry blocker; it can upgrade the target tier.
    """
    conf = confidence_pct_for_signal(score, funding_bucket, macro_gate_open, direction)
    if conf <= 0:
        return 0
    ladder = sizing_ladder_contracts()
    # Macro-blocked long setups are permitted as probes; score-4 can reach partial.
    if direction == "LONG" and not macro_gate_open:
        if score >= 4 and SCORE4_MACRO_OVERRIDE_ENABLED:
            return ladder["partial"]
        return ladder["probe"]
    if score >= 4 or conf >= 85:
        return ladder["full"]
    if score >= 3 or conf >= 65:
        return ladder["strong"]
    if conf >= 45:
        return ladder["partial"]
    return ladder["probe"]


def sizing_decision_for_signal(signal: str, score: int, funding_rate: float = 0.0, macro_gate_open: bool = True) -> Dict[str, Any]:
    funding_bucket = funding_size_modifier(signal, funding_rate)
    confidence = confidence_pct_for_signal(score, funding_bucket, macro_gate_open, signal)
    contracts = contracts_for_signal(score, funding_bucket, macro_gate_open, signal)
    return {
        "signal": signal,
        "score": int(score or 0),
        "confidence_pct": confidence,
        "funding_rate": funding_rate,
        "funding_bucket": funding_bucket,
        "macro_gate_open": bool(macro_gate_open),
        "max_conviction_contracts": MAX_CONVICTION_CONTRACTS,
        "raw_full_size_contracts": sizing_ladder_contracts().get("full"),
        "sizing_ladder": sizing_ladder_contracts(),
        "probe_contracts": sizing_ladder_contracts().get("probe"),
        "partial_contracts": sizing_ladder_contracts().get("partial"),
        "strong_contracts": sizing_ladder_contracts().get("strong"),
        "full_contracts": sizing_ladder_contracts().get("full"),
        "macro_blocked_probe_contracts": sizing_ladder_contracts().get("probe"),
        "final_contracts": contracts,
        "target_abs_contracts": contracts,
        "sizing_model": "progressive_target_size",
        "progressive_add_ons_enabled": PROGRESSIVE_ADD_ONS_ENABLED,
        "min_confidence_improvement_for_add": MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD,
        "max_position_adds": MAX_POSITION_ADDS,
        "reason": (
            "blocked" if contracts <= 0 else
            "macro_blocked_score4_partial" if signal == "LONG" and not macro_gate_open and score >= 4 else
            "macro_blocked_probe" if signal == "LONG" and not macro_gate_open else
            "funding_reduced" if funding_bucket == "PARTIAL" else
            "full_conviction" if contracts >= MAX_CONVICTION_CONTRACTS else
            "confidence_scaled"
        ),
    }


def core_target_for_signal(current_signed: int, signal: str, score: int = 3, funding_rate: float = 0.0, macro_gate_open: bool = True) -> int:
    """v14 progressive target sizing.

    The signal returns a desired TARGET ABSOLUTE net size, not a repeated add size.
    This allows multiple clean add-ons only when the confidence target increases:
      current LONG 2, target LONG 4  => BUY 2
      current LONG 4, target LONG 10 => BUY 6
      current LONG 10, target LONG 10 => no trade

    For opposite direction signals, Coinbase netting still applies:
      current LONG 6, target SHORT 4 => SELL 10
    """
    decision = sizing_decision_for_signal(signal, score, funding_rate, macro_gate_open)
    target_abs = safe_int(decision.get("target_abs_contracts", decision.get("final_contracts", 0)), 0)
    if target_abs <= 0:
        return current_signed
    target_abs = min(target_abs, MAX_CONVICTION_CONTRACTS)
    if not PROGRESSIVE_ADD_ONS_ENABLED:
        # Backwards-compatible behavior: each valid signal adds one tier.
        if signal == "LONG":
            return clamp_target(current_signed + target_abs) if current_signed >= 0 else clamp_target(+target_abs)
        if signal == "SHORT":
            return clamp_target(current_signed - target_abs) if current_signed <= 0 else clamp_target(-target_abs)
        return current_signed
    if signal == "LONG":
        return clamp_target(+target_abs)
    if signal == "SHORT":
        return clamp_target(-target_abs)
    return current_signed


def should_allow_progressive_add(state: Dict[str, Any], current_signed: int, target_signed: int, decision: Dict[str, Any]) -> Tuple[bool, str]:
    """Prevent repeated add spam while still allowing probe->partial->full scaling.

    If target would reduce/flatten/flip exposure, allow it. This guard is only for
    increasing same-direction exposure.
    """
    if not PROGRESSIVE_ADD_ONS_ENABLED:
        return True, "progressive_addons_disabled_backcompat"
    if target_signed == current_signed:
        return False, "already_at_confidence_target_size"
    # Not an add; allow reductions/flips produced by target-net logic.
    if abs(target_signed) <= abs(current_signed) or (current_signed and (target_signed * current_signed) < 0):
        return True, "not_an_add_or_is_flip"
    add_state = state.setdefault("add_on_state", default_engine_state().get("add_on_state", {}))
    confidence = safe_int(decision.get("confidence_pct"), 0)
    last_conf = safe_int(add_state.get("last_add_confidence_pct"), 0)
    adds_count = safe_int(add_state.get("adds_count"), 0)
    if adds_count >= MAX_POSITION_ADDS:
        return False, f"max_position_adds_reached_{adds_count}/{MAX_POSITION_ADDS}"
    if last_conf and confidence < last_conf + MIN_CONFIDENCE_IMPROVEMENT_FOR_ADD:
        return False, f"confidence_improvement_too_small_{confidence}%_vs_last_{last_conf}%"
    return True, "progressive_add_allowed"


def record_progressive_add(state: Dict[str, Any], result: Dict[str, Any], decision: Dict[str, Any]) -> None:
    if not result or not result.get("ok"):
        return
    before = result.get("before") or {}
    after = result.get("after") or {}
    before_signed = safe_int(before.get("signed_contracts"), 0)
    after_signed = safe_int(after.get("signed_contracts"), 0)
    if abs(after_signed) <= abs(before_signed):
        return
    add_state = state.setdefault("add_on_state", default_engine_state().get("add_on_state", {}))
    add_state["position_id"] = f"{PERP_PRODUCT_ID}:{after.get('side')}:{after.get('avg_entry_price')}"
    add_state["direction"] = after.get("side")
    add_state["adds_count"] = safe_int(add_state.get("adds_count"), 0) + 1
    add_state["last_add_confidence_pct"] = safe_int(decision.get("confidence_pct"), 0)
    add_state["last_target_contracts"] = abs(after_signed)
    add_state["last_add_at"] = iso_utc()


def detect_manual_position_change(gcs: GCS, previous: Dict[str, Any], current: Dict[str, Any]) -> None:
    """Email on manual/external position changes detected between cycles.

    Bot-originated changes also reconcile here, but immediate trade emails already go out; this is mainly
    to catch manual Coinbase entries/exits so the operator sees account exposure changes.
    """
    try:
        prev_signed = safe_int(previous.get("signed_contracts"), 0) if previous else 0
        cur_signed = safe_int(current.get("signed_contracts"), 0)
        if prev_signed == cur_signed:
            return
        # Suppress if very recent bot trade probably caused it.
        ledger_header = ["timestamp", "prev_signed", "cur_signed", "prev_side", "cur_side", "avg_entry", "current_price"]
        gcs.append_csv_row(MANUAL_POSITION_EVENTS_BLOB, ledger_header, [iso_utc(), prev_signed, cur_signed, previous.get("side"), current.get("side"), current.get("avg_entry_price"), current.get("current_price")])
        subject = f"Larry Reconciler: Position Changed {prev_signed} -> {cur_signed} contracts"
        body = f"""
Coinbase Perp Position Change Detected

Previous: {previous.get('side')} {previous.get('contracts')} contracts
Current:  {current.get('side')} {current.get('contracts')} contracts

Current Avg Entry: {current.get('avg_entry_price')}
Current Price: {current.get('current_price')}
Unrealized P&L: {current.get('unrealized_pnl')}
Daily Realized P&L: {current.get('daily_realized_pnl')}

This may be a manual Coinbase trade or an external reconciliation event.

Timestamp: {iso_utc()}
""".strip()
        send_email(subject, body)
    except Exception as e:
        log.warning("Manual position change notification failed: %s", e)

def read_optional_spot_bridge_signal(gcs: GCS) -> Dict[str, Any]:
    """Dashboard/bot bridge hook.

    If the unified spot module writes a trigger file, this can read it.
    Expected optional file:
    gs://btc_trade_log/coinbase_spot_bridge_signal.json

    Example:
    {"trigger": "spot_ladder_1", "active": true, "consumed": false}
    {"trigger": "spot_score_4", "active": true, "consumed": false}
    """
    sig = gcs.read_json("coinbase_spot_bridge_signal.json", default={}) or {}
    return sig


def maybe_handle_spot_bridge(cb: Any, gcs: GCS, state: Dict[str, Any], price: float = 0.0) -> Optional[Dict[str, Any]]:
    if not ENABLE_SPOT_BRIDGE_PERP_BUYS:
        return None
    sig = read_optional_spot_bridge_signal(gcs)
    if not sig.get("active") or sig.get("consumed"):
        return None
    trigger = sig.get("trigger")
    if trigger not in ("spot_ladder_1", "spot_score_4"):
        return None

    cd = cooldown_status(state, "bridge_last_entry_at")
    state.setdefault("cooldown_status", {})["bridge"] = cd
    if cd.get("active"):
        state.setdefault("last_blocked_action", {})["bridge"] = f"Bridge cooldown active: {cd['remaining_seconds']}s remaining"
        return None

    live = get_live_net_position(cb)
    target = live["signed_contracts"] + CONTRACTS_PER_TRADE  # bridge can only add LONG exposure
    target, ok, reason, guard = portfolio_guard_resize_target(cb, target, live["signed_contracts"], price or live.get("current_price") or 0.0)
    state["last_portfolio_guard"] = {"ok": ok, "reason": reason, **guard}
    if not ok:
        state.setdefault("last_blocked_action", {})["bridge"] = reason
        return None

    res = execute_target(cb, gcs, target, f"SPOT_BRIDGE_{trigger}", signal_class=f"SPOT_BRIDGE_{trigger}")
    if res.get("ok"):
        mark_cooldown(state, "bridge_last_entry_at")
        mark_cooldown(state, "perp_last_entry_at")
        mark_bot_managed_position_from_result(state, res, f"SPOT_BRIDGE_{trigger}")
    sig["consumed"] = True
    sig["consumed_at"] = iso_utc()
    sig["result"] = res
    gcs.write_json("coinbase_spot_bridge_signal.json", sig)
    return res

# =============================================================================
# HEARTBEAT / TELEMETRY
# =============================================================================



# =============================================================================
# SIGNAL DECISION LOG (v31)
# =============================================================================
# Append-only JSONL capture of every signal state-machine transition and every
# core execution/block, with the full indicator context at the moment of the
# decision. This is the training/backtest dataset for the self-learning roadmap:
# without it we cannot distinguish "good signal, bad sizing" from "bad signal",
# and we cannot replay history against alternative parameters. Daily-partitioned
# blobs keep each file small; a handful of events per day is the expected volume.
# Data capture must NEVER interfere with trading: every entry point is fail-soft.

DECISION_LOG_BLOB_PREFIX = "signal_decision_log"


def _decision_log_blob_name(dt: Optional[datetime] = None) -> str:
    return f"{DECISION_LOG_BLOB_PREFIX}_{(dt or now_utc()).strftime('%Y%m%d')}.jsonl"


def _new_setup_id() -> str:
    return f"setup-{int(time.time())}-{os.urandom(3).hex()}"


def append_decision_event(gcs: GCS, event: Dict[str, Any]) -> None:
    """Append one JSONL row to today's decision log. Never raises."""
    try:
        row = dict(event)
        row.setdefault("ts", iso_utc())
        blob = _decision_log_blob_name()
        existing = gcs.read_text(blob, default="")
        line = json.dumps(row, default=str)
        body = (existing.rstrip("\n") + "\n" if existing.strip() else "") + line + "\n"
        gcs.write_text(blob, body, content_type="application/json")
    except Exception as e:
        log.warning("Decision log append failed (non-fatal): %s", e)


def decision_context(sig: SignalSnapshot, funding_rate: float, macro: Dict[str, Any], live_signed: int) -> Dict[str, Any]:
    """Full indicator + regime + parameter snapshot at the moment of a decision."""
    macro = macro or {}
    return {
        "price": safe_float(sig.price, 0.0),
        "rsi": sig.rsi,
        "stoch_rsi": sig.stoch_rsi,
        "lower_bb": sig.lower_bb,
        "mid_bb": sig.mid_bb,
        "upper_bb": sig.upper_bb,
        "atr": sig.atr,
        "volume_ratio": sig.volume_ratio,
        "long_score": sig.long_score,
        "short_score": sig.short_score,
        "long_conditions": sig.long_conditions,
        "short_conditions": sig.short_conditions,
        "funding_rate": funding_rate,
        "macro_gate_open": bool(macro.get("gate_open")),
        "macro_fast_sma": macro.get("fast_sma"),
        "macro_slow_sma": macro.get("slow_sma"),
        "live_signed_contracts": live_signed,
        "params": {
            "ATR_STOP_MULTIPLIER": ATR_MULTIPLIER,
            "TSL_ACTIVATION_PCT": TSL_ACTIVATION_PCT,
            "TSL_TRAIL_PCT": TSL_TRAIL_PCT,
            "SIGNAL_ARM_SCORE": SIGNAL_ARM_SCORE,
            "SIGNAL_COMMIT_SCORE": SIGNAL_COMMIT_SCORE,
            "SIGNAL_CANCEL_SCORE": SIGNAL_CANCEL_SCORE,
            "REVERSAL_NEAR_BB_PCT": REVERSAL_NEAR_BB_PCT,
            "MAX_CONVICTION_CONTRACTS": MAX_CONVICTION_CONTRACTS,
            "MAX_EFFECTIVE_LEVERAGE": MAX_EFFECTIVE_LEVERAGE,
            "FUNDING_LONG_MAX": FUNDING_LONG_MAX,
            "FUNDING_SHORT_MIN": FUNDING_SHORT_MIN,
        },
    }


def log_phantom_transition(gcs: GCS, before: Dict[str, Any], after: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    """Emit one event when the phantom FSM meaningfully changed this cycle.

    Meaningful = (state, direction, extension_achieved) tuple changed. The reason
    string mutates every cycle while ARMED (it embeds live price), so it must not
    itself trigger events.
    """
    try:
        before = before or {}
        after = after or {}
        b = (str(before.get("state") or "MONITORING"), before.get("direction"), bool(before.get("extension_achieved")))
        a = (str(after.get("state") or "MONITORING"), after.get("direction"), bool(after.get("extension_achieved")))
        if b == a:
            return
        b_state, a_state = b[0], a[0]
        armed_states = ("PHANTOM_ARMED", "EXTENSION_CONFIRMED", "COMMITTED_ENTRY")
        if a_state == "PHANTOM_ARMED" and b_state not in armed_states:
            event = "ARMED"
        elif a[2] and not b[2]:
            event = "EXTENSION_ACHIEVED"
        elif a_state == "COMMITTED_ENTRY":
            event = "COMMITTED"
        elif a_state == "FUNDING_BLOCKED":
            event = "FUNDING_BLOCKED"
        elif a_state == "MONITORING" and b_state in armed_states:
            # Covers cancel, expiry, and macro-clear paths; the reason says which.
            event = "CANCELLED"
        else:
            event = "TRANSITION"
        append_decision_event(gcs, {
            "event": event,
            "from_state": b_state,
            "to_state": a_state,
            "direction": after.get("direction") or before.get("direction"),
            "signal_class": after.get("signal_class") or before.get("signal_class") or "CORE",
            "setup_id": after.get("setup_id") or before.get("setup_id"),
            "locked_score": after.get("locked_score"),
            "locked_confidence_pct": after.get("locked_confidence_pct"),
            "locked_target_contracts": after.get("locked_target_contracts"),
            "extension_achieved": a[2],
            "reason": after.get("reason") or before.get("reason"),
            **ctx,
        })
    except Exception as e:
        log.warning("log_phantom_transition failed (non-fatal): %s", e)


def _safe_request_id(payload: Dict[str, Any]) -> str:
    rid = str((payload or {}).get("request_id") or "").strip()
    return rid[:120] if rid else f"emergency-{int(time.time())}"


def _parse_iso_utc_seconds(ts: str) -> Optional[datetime]:
    # v30 fix: delegate to parse_dt so a missing/naive offset is treated as UTC
    # consistently everywhere, instead of duplicating (and drifting from) the logic.
    return parse_dt(ts)


# v30: emergency-flatten trust boundary. Previously the VM would flatten a live
# position for any GCS object it found with status=="REQUESTED" -- no signature,
# no shared secret, nothing tying it back to a PIN-verified dashboard action.
# Every protection lived in the Cloud Run app's PIN check plus GCS bucket IAM,
# neither of which this file could verify. The dashboard now HMAC-signs the
# request with the same EMERGENCY_FLATTEN_PIN secret (Secret Manager) used for
# the human PIN check, and this file verifies that signature before executing.
EMERGENCY_FLATTEN_ABANDONED_AFTER_SECONDS = 90
_emergency_flatten_secret_cache: Dict[str, str] = {}


def _get_emergency_flatten_secret() -> Optional[str]:
    if "value" in _emergency_flatten_secret_cache:
        return _emergency_flatten_secret_cache["value"]
    try:
        secret = load_secret("EMERGENCY_FLATTEN_PIN").strip()
        _emergency_flatten_secret_cache["value"] = secret
        return secret
    except Exception as e:
        log.error("EMERGENCY_FLATTEN_PIN secret unavailable, cannot verify flatten requests: %s", e)
        return None


def _emergency_flatten_signable_string(request_id: str, requested_at: str) -> str:
    return f"EMERGENCY_FLATTEN|{request_id}|{requested_at}"


def _emergency_flatten_signature_ok(req: Dict[str, Any], request_id: str, requested_at: str) -> Tuple[bool, str]:
    secret = _get_emergency_flatten_secret()
    if not secret:
        return False, "signing secret unavailable on VM (Secret Manager access issue)"
    provided = str(req.get("signature") or "").strip()
    if not provided:
        return False, "request has no signature (dashboard not updated, or tampered)"
    expected = hmac.new(secret.encode("utf-8"), _emergency_flatten_signable_string(request_id, requested_at).encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided, expected):
        return False, "signature mismatch"
    return True, "OK"


def process_emergency_flatten_request(cb: Any, gcs: GCS, state: Dict[str, Any], live_pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """v30: execute dashboard-originated emergency flatten requests from GCS.

    Security model: Cloud Run dashboard does not need Coinbase trading keys. It validates
    the operator PIN, HMAC-signs the request with that same secret, and writes
    emergency_flatten_request.json. The VM bot, which already owns the trading key,
    verifies the signature, executes the flatten, records the canonical ledger row,
    sends alerts, and leaves Larry halted after the operator override.

    Hardening added in v30 (all three were previously missing):
      1. HMAC signature check (see _emergency_flatten_signature_ok) -- the VM no longer
         trusts any object it finds with status=="REQUESTED".
      2. Compare-and-set claim via write_json_cas -- two bot processes briefly alive at
         once (e.g. a rolling redeploy) can no longer both execute the same request.
      3. Abandoned IN_PROGRESS recovery -- if the VM died between claiming the request
         and finishing the flatten, the next cycle detects the stale claim by age and
         resumes it instead of leaving it stuck forever. Re-running execute_target(0) is
         safe/idempotent: if the position is already flat it is a no-op.
    """
    req, generation = gcs.read_json_with_generation(EMERGENCY_FLATTEN_REQUEST_BLOB, default={})
    req = req or {}
    if not isinstance(req, dict):
        return None
    status = str(req.get("status") or "").upper()

    resuming_abandoned = False
    if status == "IN_PROGRESS":
        started_dt = parse_dt(req.get("processing_started_at"))
        age = (now_utc() - started_dt).total_seconds() if started_dt else None
        if age is None or age < EMERGENCY_FLATTEN_ABANDONED_AFTER_SECONDS:
            # Still within a normal single-cycle execution window; another process
            # (or this one, mid-cycle) owns it. Do nothing this cycle.
            return None
        log.warning("EMERGENCY_FLATTEN_RESUMING_ABANDONED request_id=%s age=%.1fs -- VM likely restarted mid-flatten", req.get("request_id"), age)
        # Re-claim via CAS with a fresh processing_started_at so a second process
        # racing to resume the same abandoned request stands down instead of both
        # executing the flatten concurrently.
        reclaim = dict(req)
        reclaim["processing_started_at"] = iso_utc()
        reclaim["resumed_at"] = iso_utc()
        if not gcs.write_json_cas(EMERGENCY_FLATTEN_REQUEST_BLOB, reclaim, generation):
            log.warning("Emergency flatten resume lost race (concurrent resume); standing down. request_id=%s", req.get("request_id"))
            return None
        req = reclaim
        resuming_abandoned = True
    elif status != "REQUESTED":
        return None

    request_id = _safe_request_id(req)
    requested_at = req.get("requested_at") or req.get("created_at") or ""

    if not resuming_abandoned:
        # Fail-closed signature check -- only for a fresh request. A resumed
        # IN_PROGRESS request was already verified when first claimed below.
        sig_ok, sig_reason = _emergency_flatten_signature_ok(req, request_id, requested_at)
        if not sig_ok:
            req.update({"status": "REJECTED_BAD_SIGNATURE", "processed_at": iso_utc(), "error": sig_reason})
            gcs.write_json(EMERGENCY_FLATTEN_REQUEST_BLOB, req)
            log.error("EMERGENCY_FLATTEN_REJECTED request_id=%s reason=%s", request_id, sig_reason)
            send_telegram_message(
                f"🚨 LARRY: emergency flatten request REJECTED ({sig_reason}).\n"
                f"Larry was NOT halted or flattened by this request. If you intended this, "
                f"flatten manually in the Coinbase app and check the VM logs.\nTime: {et_timestamp_short()}",
                event_type="EMERGENCY_FLATTEN",
            )
            return None

        # v30 fix: previously, a missing/unparsable requested_at *skipped* the
        # expiry check entirely (fail-open) instead of being treated as expired.
        requested_dt = _parse_iso_utc_seconds(requested_at)
        max_age_sec = safe_int(req.get("max_age_seconds"), 300)
        if requested_dt is None:
            req.update({"status": "REJECTED_INVALID_TIMESTAMP", "processed_at": iso_utc(), "error": "requested_at missing or unparsable"})
            gcs.write_json(EMERGENCY_FLATTEN_REQUEST_BLOB, req)
            log.error("Emergency flatten request rejected: invalid requested_at. request_id=%s", request_id)
            return None
        age = (now_utc() - requested_dt).total_seconds()
        if age > max_age_sec:
            req.update({"status": "EXPIRED", "processed_at": iso_utc(), "error": f"request older than {max_age_sec}s", "live_position_at_expiry": live_pos})
            gcs.write_json(EMERGENCY_FLATTEN_REQUEST_BLOB, req)
            log.warning("Emergency flatten request expired: request_id=%s age=%.1fs", request_id, age)
            return None

        # Compare-and-set claim: if another process already moved this past
        # REQUESTED, our generation is stale and the write is rejected.
        req.update({"status": "IN_PROGRESS", "processing_started_at": iso_utc(), "live_position_before": live_pos, "signature_verified": True})
        if not gcs.write_json_cas(EMERGENCY_FLATTEN_REQUEST_BLOB, req, generation):
            log.warning("Emergency flatten claim lost race (concurrent claim); standing down. request_id=%s", request_id)
            return None

    current_signed = safe_int(live_pos.get("signed_contracts"), 0)
    log.warning("EMERGENCY_FLATTEN_REQUEST_%s request_id=%s live_signed=%s", "RESUMED" if resuming_abandoned else "RECEIVED", request_id, current_signed)

    halt_payload = {"halt": True, "reason": "emergency_flatten_from_dashboard", "set_by": req.get("requested_by") or "dashboard", "set_at": iso_utc(), "request_id": request_id}
    gcs.write_json(BOT_HALT_BLOB, halt_payload)
    state["kill_switch"] = halt_payload

    if current_signed == 0:
        req.update({"status": "NO_POSITION", "processed_at": iso_utc(), "live_position_after": live_pos, "result": None})
        gcs.write_json(EMERGENCY_FLATTEN_REQUEST_BLOB, req)
        state["emergency_flatten_last"] = {"at": iso_utc(), "status": "NO_POSITION", "request": req, "before": live_pos, "after": live_pos}
        send_telegram_message(f"🛑 LARRY EMERGENCY FLATTEN\nNo live futures position detected.\nLarry halted.\nTime: {et_timestamp_short()}", event_type="EMERGENCY_FLATTEN")
        return None

    target_signed = 0
    result = execute_target(cb, gcs, target_signed, "OPERATOR_EMERGENCY_FLATTEN_DASHBOARD", signal_class="OPERATOR_EMERGENCY_FLATTEN")
    after = result.get("after") or get_live_net_position(cb)
    after_signed = safe_int(after.get("signed_contracts"), 0)
    ok_flat = bool(result.get("ok")) and after_signed == 0
    status = "COMPLETE_FLAT" if ok_flat else "FAILED_NOT_FLAT"
    req.update({"status": status, "processed_at": iso_utc(), "live_position_after": after, "result": result})
    gcs.write_json(EMERGENCY_FLATTEN_REQUEST_BLOB, req)
    state["emergency_flatten_last"] = {"at": iso_utc(), "status": status, "request_id": request_id, "before": live_pos, "after": after, "result": result}
    state["last_completed_trade"] = result
    state["last_execution_result"] = result
    if result.get("is_exit_trade"):
        state["last_realized_trade"] = result
    sync_bot_managed_position_after_trade(state, result, "OPERATOR_EMERGENCY_FLATTEN")
    if ok_flat:
        state["position_controls"] = default_engine_state()["position_controls"]
        state["bot_managed_position"] = None
    return result


def read_kill_switch(gcs: GCS) -> Dict[str, Any]:
    """v12: returns {halt: bool, reason: str, set_by: str, set_at: str}.

    Read on every cycle. If halt is true, run_once skips all order placement
    after persisting telemetry so the operator can still observe the bot.
    """
    payload = gcs.read_json(BOT_HALT_BLOB, default={}) or {}
    if not isinstance(payload, dict):
        return {"halt": False}
    return {
        "halt": bool(payload.get("halt")),
        "reason": payload.get("reason") or "",
        "set_by": payload.get("set_by") or "",
        "set_at": payload.get("set_at") or "",
    }


def write_heartbeat(gcs: GCS, price: float, state_name: str, live_pos: Dict[str, Any], status: str = "LIVE") -> None:
    # STARTUP is deliberately not marked LIVE. A bot is only LIVE after a full
    # successful run_once() cycle writes telemetry, engine state, and position state.
    if str(state_name).upper() == "STARTUP" and status == "LIVE":
        status = "STARTING"
    payload = {
        "ts": iso_utc(),
        "price": price,
        "status": status,
        "state": state_name,
        "bot": "Larry Perp v12 Unified",
        "has_position": bool(live_pos.get("contracts", 0)),
        "position_side": live_pos.get("side"),
        "position_contracts": live_pos.get("contracts"),
        "dry_run": DRY_RUN,
    }
    gcs.write_json(UNIFIED_HEARTBEAT_BLOB, payload)
    gcs.write_json(LEGACY_HEARTBEAT_BLOB, payload)


def write_position_state(gcs: GCS, live_pos: Dict[str, Any], state: Dict[str, Any]) -> None:
    payload = {
        "positions": {
            PERP_PRODUCT_ID: {
                "source": "coinbase_live_reconciled",
                "side": live_pos.get("side"),
                "contracts": live_pos.get("contracts"),
                "signed_contracts": live_pos.get("signed_contracts"),
                "avg_entry_price": live_pos.get("avg_entry_price"),
                "current_price": live_pos.get("current_price"),
                "unrealized_pnl": live_pos.get("unrealized_pnl"),
                "daily_realized_pnl": live_pos.get("daily_realized_pnl"),
                "risk_controls": state.get("position_controls"),
            }
        },
        "last_updated": iso_utc(),
        "source": "larry_perp_v12_unified",
    }
    gcs.write_json(PERP_POSITION_STATE_BLOB, payload)


def build_dashboard_engine_state(state: Dict[str, Any], sig: SignalSnapshot, live_pos: Dict[str, Any], product: Dict[str, Any], last_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    funding = get_funding_rate(product)
    risk_ok, risk_reason = risk_allows_entry(state)
    long_funding_ok, long_funding_reason = funding_allows("LONG", funding)
    short_funding_ok, short_funding_reason = funding_allows("SHORT", funding)
    return {
        **state,
        "version": "larry_perp_v35_fresh_setup_guard",
        "strategy_config": state.get("active_strategy_config", {}),
        "product_id": PERP_PRODUCT_ID,
        "contract_size_btc": CONTRACT_SIZE_BTC,
        "fixed_contracts_per_trade": CONTRACTS_PER_TRADE,
        "max_conviction_contracts": MAX_CONVICTION_CONTRACTS,
        "max_position_contracts": MAX_CONVICTION_CONTRACTS,
        "last_core_sizing_decision": state.get("last_core_sizing_decision"),
        "iaf_rules": {
            "atr_stop_multiplier": ATR_MULTIPLIER,
            "tsl_activation_pct": TSL_ACTIVATION_PCT,
            "tsl_trail_pct": TSL_TRAIL_PCT,
            "phantom_extension_pct": PHANTOM_EXTENSION_PCT,
            "funding_long_max": FUNDING_LONG_MAX,
            "funding_short_min": FUNDING_SHORT_MIN,
            "daily_stop_limit": DAILY_STOP_LIMIT,
            "loss_streak_limit": LOSS_STREAK_LIMIT,
        },
        "signal": asdict(sig),
        "funding": {
            "rate": funding,
            "long_gate_open": long_funding_ok,
            "long_gate_reason": long_funding_reason,
            "short_gate_open": short_funding_ok,
            "short_gate_reason": short_funding_reason,
        },
        "risk_gate": {"entries_allowed": risk_ok, "reason": risk_reason},
        "signal_lifecycle": {
            "state": (state.get("phantom") or {}).get("state"),
            "direction": (state.get("phantom") or {}).get("direction"),
            "armed_at": (state.get("phantom") or {}).get("armed_at"),
            "expires_at": (state.get("phantom") or {}).get("expires_at"),
            "locked_score": (state.get("phantom") or {}).get("locked_score"),
            "locked_confidence_pct": (state.get("phantom") or {}).get("locked_confidence_pct"),
            "locked_target_contracts": (state.get("phantom") or {}).get("locked_target_contracts"),
            "reason": (state.get("phantom") or {}).get("reason"),
            "signal_lock_enabled": SIGNAL_LOCK_ENABLED,
            "validity_minutes": SIGNAL_VALIDITY_MINUTES,
            "cancel_score": SIGNAL_CANCEL_SCORE,
            "hysteresis_arm_score": SIGNAL_HYSTERESIS_ARM_SCORE,
            "freeze_confidence_on_arm": FREEZE_CONFIDENCE_ON_ARM,
        },
        "cooldowns": {
            "spot": cooldown_status(state, "spot_last_entry_at"),
            "perp": cooldown_status(state, "perp_last_entry_at"),
            "bridge": cooldown_status(state, "bridge_last_entry_at"),
        },
        "spot_treasury": {
            "tranche_targets_pct": SPOT_TRANCHE_TARGETS_PCT,
            "enable_spot_btc_trading": ENABLE_SPOT_BTC_TRADING,
            "spot_min_entry_score": SPOT_MIN_ENTRY_SCORE,
            "spot_full_score": SPOT_FULL_SCORE,
            "note": "Original Spot ladder cumulative targets retained: 25%, 33%, 50%, 90%."
        },
        "macro_regime": state.get("macro_regime"),
        "perp_equity_protection": {
            "min_futures_equity_buffer_usd": MIN_FUTURES_EQUITY_BUFFER_USD,
            "max_effective_leverage": MAX_EFFECTIVE_LEVERAGE,
            "last_guard": state.get("last_portfolio_guard"),
        },
        "exchange_position": live_pos,
        "last_execution_result": last_result,
        "last_completed_trade": state.get("last_completed_trade"),
        "last_reported_trade_pnl_summary": state.get("last_reported_trade_pnl_summary"),
        "last_realized_trade": state.get("last_realized_trade"),
        "updated_at": iso_utc(),
        "dry_run": DRY_RUN,
    }

# =============================================================================
# MAIN LOOP
# =============================================================================


_CYCLE_CONTEXT: Dict[str, Any] = {
    "phase": "IDLE",
    "order_attempted": False,
    "client_order_id": None,
    "order_status": "NONE",
}


def run_once(cb: Any, gcs: GCS) -> None:
    _CYCLE_CONTEXT.update({
        "phase": "LOADING_STATE",
        "order_attempted": False,
        "client_order_id": None,
        "order_status": "NONE",
    })
    state = load_engine_state(gcs)
    if not state:
        state = default_engine_state()

    strategy_cfg = load_strategy_config(gcs)
    apply_strategy_config(strategy_cfg)
    state["active_strategy_config"] = strategy_cfg
    # v21 reporting-only cleanup: blocked-action messages are per-cycle diagnostics,
    # not persistent state. Clearing them here prevents stale manual/target-size
    # warnings from staying on the dashboard after Larry is flat.
    state["last_blocked_action"] = {}

    # v12 kill switch -- evaluated before any order-placing branch.
    kill = read_kill_switch(gcs)
    state["kill_switch"] = kill

    _CYCLE_CONTEXT["phase"] = "READING_EXCHANGE_POSITION"
    live_pos = get_live_net_position(cb)
    _CYCLE_CONTEXT["phase"] = "EVALUATING_RISK"
    recover_bot_managed_position_from_ledger(gcs, state, live_pos)
    # v29: dashboard emergency flatten requests are executed by the VM bot, not Cloud Run.
    # Process before normal kill-switch handling so a halt set by the dashboard does not
    # prevent the requested flatten from executing.
    emergency_result = process_emergency_flatten_request(cb, gcs, state, live_pos)
    if emergency_result is not None:
        live_pos = get_live_net_position(cb)
    mark, product = get_perp_mark(cb)
    candles = get_candles(cb, SPOT_FALLBACK_PRODUCT_ID) or get_candles(cb, SPOT_PRODUCT_ID)
    sig = calculate_signals(candles)
    # v17: indicators are calculated from candles, but trigger/proximity/phantom checks
    # should use the freshest live perp mark when available. This avoids hourly candle
    # close staleness making setups look like they never quite trigger.
    live_trigger_price = mark or get_btc_price(cb)
    if live_trigger_price > 0:
        sig.price = live_trigger_price
    elif sig.price <= 0:
        sig.price = get_btc_price(cb)

    macro = calculate_macro_regime_from_candles(candles)
    reset_daily_risk_if_needed(state)
    previous_live = state.get("last_exchange_position") or {}
    detect_manual_position_change(gcs, previous_live, live_pos)
    state["macro_regime"] = macro
    state["last_exchange_position"] = live_pos
    state["last_signal"] = asdict(sig)
    # v18: write explicit entry diagnostics every cycle so the dashboard can
    # explain why Larry is waiting even when no lifecycle transition happens.
    try:
        _funding_for_diag = get_funding_rate(product)
    except Exception:
        _funding_for_diag = 0.0
    _entry_diag = build_entry_diagnostics(state, sig, macro, _funding_for_diag)
    state["last_entry_diagnostics"] = _entry_diag
    state["entry_diagnostics"] = _entry_diag
    # v20 clean: populate the exact dashboard fields every cycle, even before arming.
    _rp_dir, _rp_score, _rp_reason = reversal_probe_candidate(sig)
    _rp_diag = {
        "direction": _rp_dir,
        "score": _rp_score,
        "qualified": bool(_rp_dir),
        "reason": _rp_reason,
        "checked_at": iso_utc(),
    }
    state["last_reversal_probe_check"] = _rp_diag
    state["reversal_probe_diagnostics"] = _rp_diag

    last_result = emergency_result

    # Always calculate risk controls for dashboard visibility, but only allow
    # automated ATR/TSL exits when the live position is bot-managed. Manual
    # Coinbase positions are monitor-only by default and must not be flattened
    # by Larry.
    controls = update_position_risk_controls(state, live_pos, sig, candles)
    update_adaptive_reentry_guard(state, sig, state.get("market_structure") or {})
    update_stop_blown_shadow(state, sig.price, sig.atr or 0.0)
    mgmt = live_position_management_status(state, live_pos)
    state["manual_position_status"] = mgmt
    exit_target, exit_reason = (None, None)
    if mgmt.get("allow_bot_to_trade_position"):
        exit_target, exit_reason = risk_exit_target_if_needed(live_pos, controls, sig.price)
    elif safe_int(live_pos.get("signed_contracts"), 0) != 0:
        state.setdefault("last_blocked_action", {})["manual_position"] = (
            "Manual/external perp position is monitor-only; ATR/TSL exits are disabled."
        )

    if kill.get("halt"):
        log.warning("KILL SWITCH ACTIVE -- skipping all order placement. reason=%s set_by=%s", kill.get("reason"), kill.get("set_by"))
        state.setdefault("last_blocked_action", {})["kill_switch"] = (
            f"halt=true reason={kill.get('reason')} set_by={kill.get('set_by')}"
        )
        # Telemetry still flows; we simply do not trade.
        exit_target = None
        if emergency_result is None:
            last_result = None

    if exit_target is not None:
        log.warning("Risk exit triggered: %s", exit_reason)
        last_result = execute_target(cb, gcs, exit_target, exit_reason, signal_class=exit_reason)
        # Only count risk stats after a confirmed position reduction/flatten.
        before_signed = safe_int((last_result.get("before") or {}).get("signed_contracts"), 0)
        after_signed = safe_int((last_result.get("after") or {}).get("signed_contracts"), 0)
        # TP1 ladder step-downs do NOT count as stops; mark tp1_done so we only fire once per position average.
        is_tp1 = (exit_reason or "").startswith(("TP1_PARTIAL_", "TP1_LADDER_STEPDOWN_"))
        if last_result.get("ok") and abs(after_signed) < abs(before_signed):
            if (exit_reason or "").startswith("ADAPTIVE_DEFENSE_"):
                start_adaptive_reentry_guard(
                    state,
                    "LONG" if before_signed > 0 else "SHORT",
                    exit_reason or "ADAPTIVE_DEFENSE",
                )
                state.setdefault("risk", {})["pause_until"] = (
                    now_utc() + timedelta(minutes=ADAPTIVE_REENTRY_COOLDOWN_MINUTES)
                ).isoformat()
                state["risk"]["halt_reason"] = "adaptive_defense_cooldown"
            if is_tp1:
                state.setdefault("position_controls", {})["tp1_done"] = True
                # v30 fix: a profit-take is clear evidence any prior losing streak
                # broke; don't let stale stop-outs from an earlier winning cycle
                # keep counting toward LOSS_STREAK_LIMIT.
                state.setdefault("risk", {})["loss_streak"] = 0
            else:
                record_exit_risk_result(state, exit_reason or "RISK_EXIT")
            if after_signed == 0 and ("STOP" in (exit_reason or "") or "ADAPTIVE_DEFENSE_EXIT" in (exit_reason or "")):
                previous_sb = state.get("stop_blown") or {}
                history = state.setdefault("stop_blown_history", [])
                if previous_sb.get("active") and previous_sb.get("leader"):
                    history.append({"at": iso_utc(), "side": previous_sb.get("stopped_side"),
                                    "leader": previous_sb.get("leader"), "anchor": previous_sb.get("anchor")})
                    del history[:-20]
                state["stop_blown"] = {
                    "active": True, "mode": "SHADOW", "trading_action": "NONE",
                    "stopped_side": "LONG" if before_signed > 0 else "SHORT",
                    "anchor": sig.price, "atr": safe_float(controls.get("atr_at_entry"), sig.atr or 0.0),
                    "reason": exit_reason, "started_at": iso_utc(), "scores": {},
                    "note": "Observation only; cannot place a re-entry order.",
                }
        # Reset phantom and position controls only on a full flatten (not TP1).
        if not is_tp1:
            state["phantom"] = default_engine_state()["phantom"]
        if last_result and last_result.get("ok") and last_result.get("plan", {}).get("action") not in (None, "NONE"):
            state["last_completed_trade"] = last_result
            state["last_reported_trade_pnl_summary"] = last_result.get("running_pnl_summary")
            if last_result.get("is_exit_trade"):
                state["last_realized_trade"] = last_result
            sync_bot_managed_position_after_trade(state, last_result, exit_reason or "RISK_EXIT")
        if safe_int((last_result.get("after") or {}).get("signed_contracts"), 0) == 0:
            state["position_controls"] = default_engine_state()["position_controls"]
            state["bot_managed_position"] = None
    elif not kill.get("halt"):
        # Coinbase Spot BTC ladder entry engine. If it buys spot, it writes a bridge trigger.
        spot_result = maybe_handle_spot_entry(cb, gcs, state, sig, macro)
        if spot_result:
            last_result = {"ok": spot_result.get("ok"), "reason": spot_result.get("reason"), "spot_result": spot_result}

        # Optional spot bridge / core perp entries. In monitor_only mode, do not
        # add/flip/flatten while a manual/external perp position exists.
        live_for_entries = get_live_net_position(cb)
        entry_mgmt = live_position_management_status(state, live_for_entries)
        if not entry_mgmt.get("allow_bot_to_trade_position"):
            state["manual_position_status"] = entry_mgmt
            state.setdefault("last_blocked_action", {})["perp_entries"] = (
                "Manual/external perp position is monitor-only; bot perp entries are disabled until flat or full_management mode."
            )
            bridge_result = None
        else:
            bridge_result = maybe_handle_spot_bridge(cb, gcs, state, sig.price)

        # v24: if a bot-managed probe is already open, a later phantom extension upgrades
        # the target to the partial tier. This makes extension an add-on signal instead of
        # an entry blocker.
        extension_add_result = None
        if (not bridge_result) and ENABLE_CORE_PERP_ENTRIES and entry_mgmt.get("allow_bot_to_trade_position"):
            pc = state.setdefault("position_controls", default_engine_state()["position_controls"])
            live_signed_for_ext = safe_int(live_for_entries.get("signed_contracts"), 0)
            ext_target_price = safe_float(pc.get("phantom_extension_target_price"), 0.0)
            ext_target_contracts = safe_int(pc.get("phantom_extension_target_contracts"), 0)
            ext_done = bool(pc.get("phantom_extension_add_done"))
            side_ok = (live_signed_for_ext > 0 and sig.price <= ext_target_price) or (live_signed_for_ext < 0 and sig.price >= ext_target_price)
            if live_signed_for_ext != 0 and ext_target_price > 0 and ext_target_contracts > abs(live_signed_for_ext) and side_ok and not ext_done:
                entries_allowed_ext, reason_ext = risk_allows_entry(state)
                ext_side = "LONG" if live_signed_for_ext > 0 else "SHORT"
                # An extension belongs to the pre-exit setup, so it can never
                # satisfy a guard that explicitly requires a new post-clear setup.
                fresh_ext_ok, fresh_ext_reason = adaptive_reentry_allows(state, ext_side, None)
                if entries_allowed_ext and not fresh_ext_ok:
                    entries_allowed_ext, reason_ext = False, fresh_ext_reason
                if entries_allowed_ext:
                    target_ext = clamp_target((1 if live_signed_for_ext > 0 else -1) * min(ext_target_contracts, MAX_CONVICTION_CONTRACTS))
                    cd_key_ext = "perp_last_long_entry_at" if live_signed_for_ext > 0 else "perp_last_short_entry_at"
                    cd_ext = cooldown_status(state, cd_key_ext)
                    if cd_ext.get("active"):
                        state.setdefault("last_blocked_action", {})["perp_extension_add"] = f"Extension add cooldown active: {cd_ext['remaining_seconds']}s remaining"
                    else:
                        # v31: resize BEFORE building the plan so the plan reflects the
                        # leverage-capped target rather than the aspirational one.
                        target_ext, ok_ext, guard_reason_ext, guard_ext = portfolio_guard_resize_target(cb, target_ext, live_signed_for_ext, sig.price or live_for_entries.get("current_price") or 0.0)
                        plan_ext = safe_target_order_plan(live_signed_for_ext, target_ext)
                        decision_ext = sizing_decision_for_signal("LONG" if live_signed_for_ext > 0 else "SHORT", SIGNAL_COMMIT_SCORE, get_funding_rate(product), bool((state.get("macro_regime") or {}).get("gate_open")))
                        decision_ext["reason"] = "phantom_extension_add_on"
                        decision_ext["target_abs_contracts"] = abs(target_ext)
                        decision_ext["final_contracts"] = abs(target_ext)
                        if guard_ext.get("resized_to") is not None:
                            decision_ext["leverage_resized"] = True
                            decision_ext["resize_note"] = guard_reason_ext
                        plan_ext["sizing_decision"] = decision_ext
                        state["last_core_target_plan"] = plan_ext
                        state["last_portfolio_guard"] = {"ok": ok_ext, "reason": guard_reason_ext, **guard_ext}
                        if ok_ext and plan_ext.get("action") not in (None, "NONE"):
                            extension_add_result = execute_target(cb, gcs, target_ext, "PHANTOM_EXTENSION_ADD_ON", signal_class="PHANTOM_EXTENSION_ADD_ON")
                            if extension_add_result.get("ok"):
                                pc["phantom_extension_add_done"] = True
                                state["last_completed_trade"] = extension_add_result
                                mark_cooldown(state, "perp_last_entry_at")
                                mark_cooldown(state, cd_key_ext)
                                sync_bot_managed_position_after_trade(state, extension_add_result, "PHANTOM_EXTENSION_ADD_ON")
                                record_progressive_add(state, extension_add_result, decision_ext)
                        else:
                            state.setdefault("last_blocked_action", {})["perp_extension_add"] = guard_reason_ext
                else:
                    state.setdefault("last_blocked_action", {})["perp_extension_add"] = reason_ext

        if bridge_result:
            last_result = bridge_result
        elif extension_add_result:
            last_result = extension_add_result
        elif ENABLE_CORE_PERP_ENTRIES and entry_mgmt.get("allow_bot_to_trade_position"):
            entries_allowed, reason = risk_allows_entry(state)
            if entries_allowed:
                # v31 decision log: snapshot the FSM around the update so every
                # arm/extension/commit/cancel transition is captured with full
                # indicator context. Fail-soft: logging never blocks trading.
                _ph_before = dict(state.get("phantom") or {})
                _funding_now = get_funding_rate(product)
                confirmed = update_phantom_state(state, sig, _funding_now, candles)
                _ph_after = state.get("phantom") or {}
                if str(_ph_after.get("state") or "").upper() in ("PHANTOM_ARMED", "EXTENSION_CONFIRMED", "COMMITTED_ENTRY") and not _ph_after.get("setup_id"):
                    _ph_after["setup_id"] = _new_setup_id()
                _dctx = decision_context(sig, _funding_now, state.get("macro_regime") or {}, safe_int((state.get("last_exchange_position") or {}).get("signed_contracts"), 0))
                log_phantom_transition(gcs, _ph_before, _ph_after, _dctx)
                if confirmed:
                    fresh_ok, fresh_reason = adaptive_reentry_allows(
                        state, confirmed, (state.get("phantom") or {}).get("armed_at")
                    )
                    if not fresh_ok:
                        reset_phantom_with_reason(state, fresh_reason)
                        state.setdefault("last_blocked_action", {})["adaptive_reentry"] = fresh_reason
                        confirmed = None
                if confirmed:
                    # v12: check the per-direction cooldown, not the merged one.
                    cd_key = "perp_last_long_entry_at" if confirmed == "LONG" else "perp_last_short_entry_at"
                    cd = cooldown_status(state, cd_key)
                    state.setdefault("cooldown_status", {})["perp"] = cd
                    state["cooldown_status"]["perp_direction"] = confirmed
                    if cd.get("active"):
                        state.setdefault("last_blocked_action", {})["perp"] = f"Perp cooldown active: {cd['remaining_seconds']}s remaining"
                    else:
                        # Target net exposure, not ticket side.
                        #
                        # IMPORTANT SAFETY RULE:
                        # A core IAF LONG/SHORT signal expresses the desired FINAL
                        # net exposure, not a blind BUY 2 / SELL 2 ticket.
                        # Coinbase futures are netted, so:
                        #   current LONG 4, target SHORT 2 => SELL 6
                        #   current LONG 2, target SHORT 2 => SELL 4
                        #   current SHORT 2, target LONG 2 => BUY 4
                        # Bridge entries are handled separately and can add long
                        # exposure. Core LONG signals add to existing longs; core SHORT
                        # signals flip to net short target when currently long.
                        live_now = get_live_net_position(cb)
                        _funding = get_funding_rate(product)
                        _macro_open = bool((state.get("macro_regime") or {}).get("gate_open"))
                        _ctx = locked_context_from_phantom(state, confirmed, sig, _funding, _macro_open)
                        _score = safe_int(_ctx.get("score"), sig.long_score if confirmed == "LONG" else sig.short_score)
                        sizing_decision = sizing_decision_for_signal(confirmed, _score, _funding, _macro_open)
                        # If confidence was frozen when the setup armed, preserve the locked values for execution.
                        if FREEZE_CONFIDENCE_ON_ARM and _ctx.get("target_abs_contracts"):
                            sizing_decision["confidence_pct"] = _ctx.get("confidence_pct")
                            sizing_decision["target_abs_contracts"] = _ctx.get("target_abs_contracts")
                            sizing_decision["final_contracts"] = _ctx.get("target_abs_contracts")
                            sizing_decision["reason"] = "locked_signal_commitment"
                        target = core_target_for_signal(live_now["signed_contracts"], confirmed, _score, _funding, _macro_open)
                        if FREEZE_CONFIDENCE_ON_ARM and _ctx.get("target_abs_contracts"):
                            target = clamp_target((+1 if confirmed == "LONG" else -1) * safe_int(_ctx.get("target_abs_contracts"), 0))
                        _adaptive_guard = state.get("adaptive_reentry_guard") or {}
                        _guarded_reentry = bool(
                            _adaptive_guard.get("active")
                            and str(_adaptive_guard.get("side") or "").upper() == confirmed
                        )
                        if _guarded_reentry and ADAPTIVE_REENTRY_PROBE_ONLY:
                            probe_abs = max(1, min(sizing_ladder_contracts().get("probe", 1), MAX_CONVICTION_CONTRACTS))
                            target = clamp_target((1 if confirmed == "LONG" else -1) * probe_abs)
                            sizing_decision["adaptive_reentry_probe_cap"] = True
                            sizing_decision["target_abs_contracts"] = probe_abs
                            sizing_decision["final_contracts"] = probe_abs
                            sizing_decision["reason"] = "fresh_post_adaptive_setup_probe"
                        # v31: resize BEFORE building the plan / progressive-add check so
                        # everything downstream sees the leverage-capped target. A strong
                        # signal now executes at the largest safe size instead of being
                        # blocked outright when full conviction exceeds the leverage cap.
                        target, ok, guard_reason, guard = portfolio_guard_resize_target(cb, target, live_now["signed_contracts"], sig.price or live_now.get("current_price") or 0.0)
                        if guard.get("resized_to") is not None:
                            sizing_decision["leverage_resized"] = True
                            sizing_decision["target_abs_contracts"] = abs(target)
                            sizing_decision["final_contracts"] = abs(target)
                            sizing_decision["resize_note"] = guard_reason
                        plan_preview = safe_target_order_plan(live_now["signed_contracts"], target)
                        plan_preview["sizing_decision"] = sizing_decision
                        add_ok, add_reason = should_allow_progressive_add(state, live_now["signed_contracts"], target, sizing_decision)
                        plan_preview["progressive_add_allowed"] = add_ok
                        plan_preview["progressive_add_reason"] = add_reason
                        log.info("Core IAF target-net plan: signal=%s score=%s confidence=%s funding=%s macro_open=%s current=%s target=%s action=%s qty=%s",
                                 confirmed, _score, sizing_decision.get("confidence_pct"), sizing_decision.get("funding_bucket"), _macro_open, live_now["signed_contracts"], target,
                                 plan_preview.get("action"), plan_preview.get("contracts_needed"))
                        state["last_core_sizing_decision"] = sizing_decision
                        state["last_core_target_plan"] = plan_preview
                        if not add_ok:
                            ok = False
                            guard_reason = add_reason
                        state["last_portfolio_guard"] = {"ok": ok, "reason": guard_reason, **guard}
                        _setup_id = (state.get("phantom") or {}).get("setup_id")
                        if ok:
                            _locked_class = ((state.get("phantom") or {}).get("signal_class") or "CORE")
                            core_reason = f"REVERSAL_PROBE_{confirmed}_PHANTOM_CONFIRMED" if _locked_class == "REVERSAL_PROBE" else f"CORE_IAF_{confirmed}_PHANTOM_CONFIRMED"
                            last_result = execute_target(cb, gcs, target, core_reason, signal_class=core_reason)
                            append_decision_event(gcs, {
                                "event": "EXECUTED" if last_result.get("ok") else "EXECUTION_FAILED",
                                "setup_id": _setup_id,
                                "signal_class": core_reason,
                                "direction": confirmed,
                                "target_signed": target,
                                "leverage_resized": bool(guard.get("resized_to") is not None),
                                "resized_from": guard.get("resized_from"),
                                "client_order_id": (last_result.get("order") or {}).get("client_order_id"),
                                "fill_price": (last_result.get("fills") or {}).get("avg_price"),
                                "before_signed": safe_int((last_result.get("before") or {}).get("signed_contracts"), 0),
                                "after_signed": safe_int((last_result.get("after") or {}).get("signed_contracts"), 0),
                                **_dctx,
                            })
                            if last_result.get("ok"):
                                if last_result.get("plan", {}).get("action") not in (None, "NONE"):
                                    state["last_completed_trade"] = last_result
                                    state["last_reported_trade_pnl_summary"] = last_result.get("running_pnl_summary")
                                    if last_result.get("is_exit_trade"):
                                        state["last_realized_trade"] = last_result
                                # v12: mark both the generic and the per-direction cooldown.
                                mark_cooldown(state, "perp_last_entry_at")
                                if confirmed == "LONG":
                                    mark_cooldown(state, "perp_last_long_entry_at")
                                else:
                                    mark_cooldown(state, "perp_last_short_entry_at")
                                sync_bot_managed_position_after_trade(state, last_result, core_reason)
                                record_progressive_add(state, last_result, sizing_decision)
                                if _guarded_reentry:
                                    resolve_adaptive_reentry_guard(state, confirmed, _setup_id)
                            state["phantom"] = default_engine_state()["phantom"]
                        else:
                            state.setdefault("last_blocked_action", {})["perp"] = guard_reason
                            append_decision_event(gcs, {
                                "event": "EXECUTION_BLOCKED",
                                "setup_id": _setup_id,
                                "direction": confirmed,
                                "target_signed": target,
                                "reason": guard_reason,
                                **_dctx,
                            })
            else:
                state.setdefault("phantom", {})["reason"] = reason

    # Reconcile after potential action.
    live_pos_after = get_live_net_position(cb)
    if safe_int(live_pos_after.get("signed_contracts"), 0) == 0:
        state["position_controls"] = default_engine_state()["position_controls"]
        state["bot_managed_position"] = None
        state["add_on_state"] = default_engine_state()["add_on_state"]
        # v20 clean: when flat, stale target/order plans should not imply Larry
        # still wants to sell or that a position is open. Preserve the completed
        # trade separately in last_completed_trade / last_realized_trade.
        if not last_result:
            state["last_core_target_plan"] = None
            state["last_order_plan"] = None
    # v20 clean: final reconciliation also keeps bot-managed open size in sync with Coinbase
    # after partial fills/exits. If Larry is flat, clear; if Larry has a bot-managed record,
    # refresh it to the live Coinbase position.
    if safe_int(live_pos_after.get("signed_contracts"), 0) != 0 and state.get("bot_managed_position"):
        state["bot_managed_position"].update({
            "signed_contracts": safe_int(live_pos_after.get("signed_contracts"), 0),
            "side": live_pos_after.get("side"),
            "contracts": live_pos_after.get("contracts"),
            "avg_entry_price": live_pos_after.get("avg_entry_price"),
            "current_price": live_pos_after.get("current_price"),
            "unrealized_pnl": live_pos_after.get("unrealized_pnl"),
            "daily_realized_pnl": live_pos_after.get("daily_realized_pnl"),
            "product_id": live_pos_after.get("product_id"),
            "synced_at": iso_utc(),
        })
    state["manual_position_status"] = live_position_management_status(state, live_pos_after)
    state["last_exchange_position"] = live_pos_after
    if last_result:
        state["last_order_plan"] = last_result.get("plan")

    maybe_send_daily_telegram_summary(gcs, state, live_pos_after)

    _CYCLE_CONTEXT["phase"] = "SAVING_ENGINE_STATE"
    dashboard_state = build_dashboard_engine_state(state, sig, live_pos_after, product, last_result)
    save_engine_state(gcs, dashboard_state)
    _CYCLE_CONTEXT["phase"] = "SAVING_POSITION_STATE"
    write_position_state(gcs, live_pos_after, dashboard_state)
    try:
        write_heartbeat(gcs, sig.price or mark, state.get("phantom", {}).get("state", "MONITORING"), live_pos_after)
    except Exception as e:
        log.warning("Non-fatal heartbeat write failure: %s", e)

    log.info(
        "Cycle: price=$%.2f long_score=%s short_score=%s phantom=%s live=%s %s avg=%.2f upl=%.2f",
        sig.price,
        sig.long_score,
        sig.short_score,
        dashboard_state.get("phantom", {}).get("state"),
        live_pos_after.get("side"),
        live_pos_after.get("contracts"),
        live_pos_after.get("avg_entry_price"),
        live_pos_after.get("unrealized_pnl"),
    )
    _CYCLE_CONTEXT["phase"] = "COMPLETE"


def main() -> None:
    log.info("Loading Coinbase client and GCS...")
    cb = build_coinbase_client()
    gcs = GCS(BUCKET_NAME)

    # Startup verification: live exchange state is source of truth.
    live = get_live_net_position(cb)
    log.info("Startup exchange reconciliation: %s", live)
    write_heartbeat(gcs, live.get("current_price") or 0.0, "STARTUP", live)
    send_telegram_message(f"🟢 Larry Perp started\nPosition: {live.get('side')} {live.get('contracts')}\nTime: {et_timestamp_short()}", event_type="BOT_STARTED")

    while True:
        try:
            run_once(cb, gcs)
        except Exception as e:
            log.exception("Main loop error: %s", e)
            if TELEGRAM_INCLUDE_ERRORS:
                attempted = bool(_CYCLE_CONTEXT.get("order_attempted"))
                action = (
                    "Order activity occurred; verify client order ID and Coinbase position before any retry."
                    if attempted
                    else "No order was attempted before this failure; retrying next loop."
                )
                send_telegram_message(
                    f"🚨 LARRY ERROR\n{type(e).__name__}: {str(e)[:800]}\n"
                    f"Phase: {_CYCLE_CONTEXT.get('phase')}\n"
                    f"Order attempted: {'YES' if attempted else 'NO'}\n"
                    f"Order status: {_CYCLE_CONTEXT.get('order_status')}\n"
                    f"Client order ID: {_CYCLE_CONTEXT.get('client_order_id') or 'none'}\n"
                    f"Action: {action}\nTime: {et_timestamp_short()}",
                    event_type="ERROR",
                )
            try:
                # Write a DOWN/ERROR-ish heartbeat while service is still alive.
                gcs = GCS(BUCKET_NAME)
                err_payload = {"ts": iso_utc(), "status": "ERROR", "state": "ERROR", "error": str(e), "bot": "Larry Perp v12 Unified"}
                gcs.write_json(UNIFIED_HEARTBEAT_BLOB, err_payload)
                gcs.write_json(LEGACY_HEARTBEAT_BLOB, err_payload)
            except Exception:
                pass
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
