# Larry v46 — Independent Position Legs

Deployment date: 2026-08-10

## Strategy

- Fresh core entries require 4/4 conviction on a closed candle.
- Core entry size is capped at four contracts.
- One add of at most two contracts is allowed only on a fresh same-side 4/4 setup.
- The existing trade must already be favorable by at least 0.16% and conviction
  must be maintained or improve.
- Adverse phantom-extension adds are disabled.

## Risk ownership

- Coinbase remains a netted position; Larry maintains separate CORE/ADD legs.
- Each leg owns its entry, locked ATR, 1.5x ATR firm stop, 1.25R TP1, 1.0R TSL
  activation, high/low watermark, trail, fees and realized/open P&L.
- A leg trigger closes only that leg's assigned contracts unless an emergency or
  position-wide Adaptive Defense exit requires a larger reduction.
- New risk fails closed whenever internal signed quantity differs from Coinbase.

## Live migration

- A pre-existing Coinbase position with no v46 leg book becomes one migrated CORE
  leg. The exchange average and locked ATR are preserved where available.
- The migration never submits an order.

## Dashboard

- New responsive Independent Position Legs panel for desktop and mobile.
- Reconciliation, entry, conviction, open P&L, stop, TP1, TSL and allocated fees
  are displayed per leg.
- Dashboard v46 and deployment date are visible in the panel.

## Validation

- Python compile for engine and dashboard.
- Dashboard JavaScript parse check.
- 44 unit tests, including three-contract migration, independent anchors and the
  working-position add hurdle.
