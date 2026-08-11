# Larry v47 — Progressive Independent-Leg Ladder

Deployment date: 2026-08-11

## Entry trigger

- A new long or short requires a fresh 4/4 score on closed-candle evidence.
- A 3/4 score may arm and lock a setup but cannot place an order.
- Confirmation occurs on the required subsequent closed candle while lifecycle,
  macro, funding, cooldown, ownership, reconciliation, and risk gates remain valid.
- A new flat entry is capped at four contracts regardless of the confidence label.

## Position progression

- Total-position ladder: `4 -> 6 -> 10 -> 15 -> 20` contracts.
- Larry can advance only one rung per decision cycle.
- Every increase requires a fresh same-side 4/4 setup.
- The first `4 -> 6` increase may occur during short-term weakness only if the
  move is adverse, no worse than 0.35 locked ATR, and the setup remains 4/4.
- Later increases require favorable progress after estimated costs.
- Four adds per position are available, corresponding to the four increases.
- Twenty contracts is the absolute maximum; configured leverage remains 3x.

## Independent risk legs

- Coinbase remains netted, but every CORE or ADD is a separate Larry risk leg.
- Each leg owns its confirmed fill, quantity, locked ATR, 1.5 ATR firm stop,
  1.0R TP1, 1.25R trailing-stop activation, watermark, fees, and P&L.
- A leg exit reduces only that leg unless an emergency or position-wide safety
  rule requires a larger reduction.
- Internal signed leg quantity must equal Coinbase signed quantity before Larry
  may add new risk.

## Track record

- Inception: `2026-08-11T16:25:57Z`.
- Verified starting position: `FLAT 0`.
- Starting capital/baseline: `$2,000.00`.
- Starting Larry P&L: `$0.00`.
- Canonical ledger: `gs://btc_trade_log/perp_trades_ledger_v47.csv`.
- The previous ledger remains an immutable pre-inception archive.

## Dashboard

- Header reports Dashboard v47 and Engine v47 with the deployment date.
- Sizing displays the executable `4 -> 6 -> 10 -> 15 -> 20` ladder instead of
  legacy percentage-derived tier sizes.
- Flat accounts no longer show the manual-exposure warning merely because
  manual-position mode is configured as monitor-only.
- Independent Position Legs remains the detailed risk and reconciliation view.

## Validation

- Engine, dashboard, and test syntax compilation.
- 48 unit tests covering entry gating, ladder progression, the bounded first
  pullback, independent anchors, TP1-before-TSL ordering, and fail-closed
  reconciliation.
