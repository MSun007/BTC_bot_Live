# Larry BTC Perpetual Futures Bot

Larry is a live Coinbase BTC perpetual-futures trading system with conviction-based position sizing, exchange-reconciled risk controls, adaptive loss management, profit protection, market-structure analysis, and a mobile-friendly operations dashboard.

The current production engine is:

```text
larry_perp_v35_fresh_setup_guard
```

> This repository controls a live trading system. Test and review every behavioral change before deployment. Never assume that a successful code deployment means the bot is authorized to trade: the kill switch, exchange position, configuration and service health must all be checked independently.

## System overview

Larry separates the trading process into five layers:

1. **Observe:** calculate indicators, macro regime, funding conditions and confirmed swing structure.
2. **Qualify:** score long and short opportunities and progress them through the phantom-signal lifecycle.
3. **Size:** translate conviction into a target net position, then apply exposure and leverage safeguards.
4. **Manage:** protect an open position with a firm ATR stop, adaptive defence, profit taking and a trailing stop.
5. **Reconcile:** treat Coinbase as the source of truth after every order and publish the resulting state to the dashboard.

Coinbase futures positions are netted. Larry therefore trades toward a **target net exposure** rather than sending blind buy or sell tickets.

## Repository files

| File | Purpose |
|---|---|
| `larry_perp_v1.py` | Production trading engine running on the Compute Engine VM |
| `perp_dashboard_app.py` | Flask dashboard deployed to Cloud Run |
| `strategy_config.json` | Live strategy defaults and operator-controlled thresholds |
| `test_adaptive_risk.py` | Regression tests for adaptive risk, pivots, re-anchoring and ATR-stop priority |
| `Dockerfile` | Cloud Run dashboard container definition |
| `requirements.txt` | Dashboard runtime dependencies |

## Entry and conviction model

Larry scores long and short conditions using RSI, stochastic RSI, Bollinger Bands and volume participation. Signals move through a stateful lifecycle rather than executing from a single transient reading.

Typical lifecycle:

```text
MONITORING → PHANTOM_ARMED → CLOSED-CANDLE CONFIRMATION → COMMITTED ENTRY
```

Position size scales with conviction. The effective ladder is derived from `MAX_CONVICTION_CONTRACTS` and the configured probe, partial and strong percentages. `MAX_CONVICTION_CONTRACTS` is the sole absolute contract limit; the portfolio leverage guard may resize a target lower when account equity cannot safely support it.

Progressive additions trade toward a higher target size only when confidence improves. They are not repeated identical orders.

## Position anchoring and resizing

Coinbase is the source of truth for the live position quantity and average entry price.

Each unique combination of signed contracts and exchange average creates a new **position version**. When Larry detects a new version it records:

- Signed contract quantity
- Coinbase-confirmed average entry
- Locked ATR
- Previous and new position fingerprints
- Re-anchoring timestamp
- Verification status

After a same-direction increase, Larry clears the previous trailing watermark and rebuilds the ATR, TP and trailing controls from the new blended average. This prevents an enlarged position from inheriting a stale stop or high/low watermark.

Position-event rules:

- **Fresh entry:** initialize all controls from the confirmed exchange position.
- **Same-direction increase:** re-anchor to the blended average and restart trailing state.
- **Partial reduction:** synchronize the remaining position without treating it as a new entry thesis.
- **Direction flip:** treat the resulting exposure as a new position.
- **Manual/external change:** in `monitor_only` mode, Larry displays the position but will not modify it.

## Risk and exit architecture

### Firm ATR stop

The non-negotiable protective stop remains:

```text
LONG stop  = average entry − locked ATR × 1.5
SHORT stop = average entry + locked ATR × 1.5
```

ATR is locked when the current position version is established. The firm ATR stop always takes priority over adaptive-defence logic.

### Adaptive defence

Adaptive defence looks for converging evidence that an open-position thesis is deteriorating before the firm ATR stop is reached.

The current evidence model can score:

| Evidence | Points |
|---|---:|
| Adverse momentum across recent candles | 20 |
| Adverse momentum accelerating | 10 |
| Adverse candle with elevated volume | 20 |
| Break of the relevant confirmed swing pivot | 35 |
| Price on the unfavorable side of the Bollinger middle band | 10 |
| Adverse RSI regime | 10 |

The score is capped at 100.

Current live actions:

| Score/state | Response |
|---|---|
| Below 65 | `HOLD` |
| 65–84 for two consecutive cycles | Reduce one conviction rung |
| 85+ for two consecutive cycles | Exit the position |
| Firm ATR stop crossed | Exit immediately |

An adaptive reduction or exit creates a 15-minute minimum re-entry cooldown.
Beginning with v35, elapsed time is not sufficient to authorize the same side
again. Larry latches a same-side re-entry guard that requires:

1. The old directional score to fall to the signal-cancel threshold.
2. Structure to stop being adverse or price to reclaim the Bollinger middle band.
3. A new phantom setup whose arm timestamp is later than the signal-clear event.
4. The first qualified retry to use probe size only.

This prevents a persistent oversold or overbought reading from being recycled as
a supposedly fresh setup every time the cooldown expires. Opposite-side setups
remain eligible under their normal gates.

Adaptive defence can only reduce or close an existing position. It cannot independently open or reverse a trade.

### R-based profit taking

TP1 is based on the trade's defined risk rather than an unrelated fixed percentage:

```text
1R = distance from average entry to the firm ATR stop
TP1 distance = 0.75R
```

At TP1, Larry steps the position down to the next lower conviction rung instead of necessarily flattening it. This preserves a runner while improving the relationship between typical gains and losses.

Legacy percentage TP settings remain available for compatibility and can be restored by disabling `TP1_USE_R_MULTIPLE`.

### Trailing stop

The trailing stop activates only after the configured favorable move. Once active, it follows the best observed price using the configured trail percentage.

The watermark is cleared after a same-direction position increase so a larger position cannot inherit an obsolete trailing stop.

## Swing-pivot classifier

Larry detects confirmed, non-repainting swing highs and lows. The newest incomplete candle is excluded, and a pivot must have the configured number of completed bars on both sides.

Structure classifications include:

- `BULLISH_HH_HL`: higher high and higher low
- `BEARISH_LH_LL`: lower high and lower low
- `RANGE_OR_TRANSITION`: mixed or overlapping structure
- `UNCLASSIFIED`: insufficient confirmed structure

The dashboard displays the latest confirmed swing high, swing low and classification.

Pivot structure is currently **shadow/informational**. A confirmed structural break may contribute to adaptive defence, but the pivot classifier cannot independently enter, reverse or re-enter a position.

## Post-stop classifier

After a qualifying full stop or adaptive exit, Larry can observe how price behaves around the exit anchor.

| State | Interpretation |
|---|---|
| `FISHED` | Price crossed the anchor and reclaimed it; possible liquidity sweep or headfake |
| `SAVED` | Price continued beyond the anchor; the stop protected against a real continuation |
| `EXTREME` | Price extended unusually far beyond the stop envelope |
| `UNCLEAR` | No recovery or continuation signature dominates |
| `BURNED` | Repeated same-direction FISHED events suggest that side is being hunted |

The post-stop system is currently **shadow-only**:

- It publishes scores to the dashboard.
- It records repeated same-side FISHED observations.
- Three recent same-side FISHED observations can produce a full BURNED score.
- It cannot place a re-entry order.

Automatic FISHED re-entry should remain disabled until the shadow dataset is large enough to evaluate expectancy, false-reentry cost and regime dependence.

## Dashboard

The Cloud Run dashboard is designed for desktop and mobile operation. The Position Authority panel leads with the most important operational fact:

- `AUTO-MANAGED`: Larry has verified ownership and may execute exits and adjustments.
- `NOT MANAGED`: Larry can display calculated levels but will not submit orders for the position.
- `FLAT`: Coinbase reports no open futures position.

The Larry Decision Pipeline displays:

- Macro, funding and risk gates
- Long and short trigger scores
- Signal lifecycle and conviction tier
- Current and target position size
- ATR, TP and trailing-stop progress
- Adaptive-defence score, state and evidence
- Position version and verified exchange anchor
- Confirmed swing structure and pivot levels
- Post-stop state and shadow scores

The dashboard also provides Larry-only performance accounting, benchmark comparison, trade maps, realized P&L, drawdown, execution diagnostics, configuration visibility and emergency controls.

## Manual positions and ownership

The production default is:

```text
MANUAL_POSITION_MODE=monitor_only
```

In this mode, Larry will not place exits, additions, flips or flattening trades against an unverified manual/external position. If Coinbase exposure diverges from Larry's recorded bot-managed exposure, the dashboard identifies the position as monitor-only.

Ownership recovery fails closed. A matching historical ledger row is supporting evidence but is not sufficient by itself. Recovery requires the prior persisted cycle to have already classified the position as bot-managed, with matching signed quantity, product and Coinbase average-entry fingerprint. If continuity cannot be proven, the position remains monitor-only.

## Order execution safety

The engine:

- Generates collision-resistant client order IDs.
- Validates Coinbase responses rather than treating every non-exception as success.
- Reconciles orders against the resulting live exchange position.
- Distinguishes confirmed, partial, rejected, unknown and mismatched outcomes.
- Uses actual exchange position change as the filled amount.
- Suppresses trade confirmation messages when an acknowledged order produces no confirmed position change.
- Records requested versus filled contracts and execution status.
- Retries transient GCS writes with bounded exponential backoff.

When an order outcome is ambiguous, verify Coinbase position and client order ID before manually retrying.

## Safety controls

### Kill switch

`gs://btc_trade_log/bot_halt.json` is checked before normal order placement. When `halt=true`, telemetry continues but Larry submits no further orders, including automated exits. An existing position remains open until it is handled in Coinbase or through the separate Emergency Close Futures workflow.

Emergency flatten requests are signed, claimed through generation-matched writes and executed by the VM bot. A stuck in-progress request can be safely reconciled after restart.

Never clear a halt merely because a deployment succeeded. Confirm the exchange position, service health and intended trading status first.

### Daily and streak guards

Larry tracks daily stop hits, consecutive-loss state and pause windows. A profitable TP step resets the prior losing streak. Daily state resets on the next UTC trading date.

### Funding and macro gates

Funding can block or reduce position size when carrying cost becomes adverse. The macro regime provides an additional trend-alignment gate and can restrict full-size signals.

## Important configuration

The production defaults live in `strategy_config.json`. The bot merges the stored GCS configuration over code defaults each cycle.

Key v35 settings:

```json
{
  "ATR_STOP_MULTIPLIER": 1.5,
  "MAX_CONVICTION_CONTRACTS": 20,
  "TP1_USE_R_MULTIPLE": true,
  "TP1_R_MULTIPLE": 0.75,
  "ADAPTIVE_DEFENSE_ENABLED": true,
  "ADAPTIVE_REDUCE_SCORE": 65,
  "ADAPTIVE_EXIT_SCORE": 85,
  "ADAPTIVE_CONFIRM_CYCLES": 2,
  "ADAPTIVE_REENTRY_COOLDOWN_MINUTES": 15,
  "ADAPTIVE_FRESH_SETUP_REQUIRED": true,
  "ADAPTIVE_REENTRY_PROBE_ONLY": true,
  "ADAPTIVE_REENTRY_REQUIRE_STRUCTURE_OR_MID_BAND": true,
  "SWING_PIVOT_ENABLED": true,
  "SWING_PIVOT_LEFT_BARS": 2,
  "SWING_PIVOT_RIGHT_BARS": 2,
  "STOP_BLOWN_SHADOW_MODE": true
}
```

Do not change several major exit thresholds simultaneously. Make one controlled change, preserve the prior configuration and measure the resulting expectancy, average win/loss, drawdown, MAE/MFE and fee impact.

## Data and state

Primary GCS objects include:

```text
gs://btc_trade_log/perp_engine_state.json
gs://btc_trade_log/perp_position_state.json
gs://btc_trade_log/perp_trades_ledger.csv
gs://btc_trade_log/strategy_config.json
gs://btc_trade_log/bot_halt.json
gs://btc_trade_log/coinbase_unified_heartbeat.json
```

Coinbase remains authoritative for live exposure. GCS state supports orchestration, telemetry, recovery and dashboard display; it must not override a conflicting live exchange position.

## Testing

Run the regression suite:

```bash
python -m unittest -v test_adaptive_risk.py
```

The current tests verify:

- ATR-stop priority over adaptive reduction
- Position-version changes after quantity/average changes
- R-based TP calculation from locked ATR
- Confirmed pivots exclude the newest incomplete bar
- Adaptive reduction targets a lower ladder rung
- BURNED scoring recognizes repeated same-side FISHED observations
- Max Conviction is the sole absolute contract clamp
- Position management requires a matching exchange ownership fingerprint
- Ledger recovery fails closed without prior bot-managed continuity
- Adaptive exits require the old same-side signal to clear
- Pre-clear phantom setups cannot be reused after recovery
- The first fresh same-side retry is probe-only
- Opposite-side setups are not blocked by the same-side guard

Also run syntax checks before deployment:

```bash
python -m py_compile larry_perp_v1.py perp_dashboard_app.py
```

## Deployment and verification

The dashboard is deployed to Cloud Run from the trusted GitHub `main` branch. The trading engine runs separately on the production Compute Engine VM as `larry-perp.service`.

A complete release must update **both** surfaces.

Recommended sequence:

1. Confirm Coinbase and the dashboard show Larry flat, or deliberately halt trading.
2. Back up the current local production files.
3. Run syntax and regression tests.
4. Commit and push reviewed changes to `main`.
5. Confirm the new Cloud Run revision becomes healthy.
6. Back up the VM's current `larry_perp_v1.py`.
7. Install the tested commit on the VM and restart `larry-perp.service`.
8. Confirm the service is active and startup reconciliation matches Coinbase.
9. Confirm `perp_engine_state.json` reports the intended engine version.
10. Test the dashboard at desktop and mobile widths.
11. Verify the kill switch separately; do not silently clear it.

Production verification should include:

```text
Service: active
Engine: larry_perp_v34_authority_cleanup
Exchange position: expected side and quantity
Dashboard feed: current
Cloud Run revision: healthy
Kill switch: explicitly understood
```

## Backup and maintenance policy

Before changing production files:

- Preserve the current local files in a timestamped `LIVE PLATFORM/backup_*` directory.
- Keep the newest approved source files directly in `LIVE PLATFORM`.
- Preserve a timestamped copy of the VM engine before replacement.
- Keep commits focused and use the commit hash to identify the deployed release.
- Never overwrite unexplained local changes without reviewing them.

After each material strategy change, update this README, configuration notes, tests and dashboard labels together.

## Current production release

The v35 fresh-setup guard preserves the v34 ownership, re-anchoring, ATR,
profit-taking and adaptive-defence improvements while fixing the churn path
introduced by the time-only adaptive re-entry cooldown.

Production deployment (July 23, 2026):

- Release commit: `4c9a02a`
- Cloud Run dashboard revision: `perp-bot-dashboard-00135-tbk` (100% traffic)
- Engine service: `larry-perp.service` active on `btc-perp-bot`
- Engine state: `larry_perp_v35_fresh_setup_guard`
- Exchange position at deployment and verification: `FLAT`
- Same-side signal clear: mandatory
- Recovery evidence: mandatory
- New post-clear phantom setup: mandatory
- First retry sizing: probe only
- Previous VM engine: `/home/msunderji/larry_perp_v1.py.backup_pre_v35_20260723_1024`
- Previous GCS configuration: `gs://btc_trade_log/backups/strategy_config_pre_v35_20260723_1024.json`
