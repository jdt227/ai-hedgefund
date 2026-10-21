# Account inputs and manual-execution workflow

The system **never** logs into Investopedia, stores credentials, or places
trades. You type official numbers into three git-ignored files and execute
trades yourself.

```
private/account/              (git-ignored; create it with: python -m src.cli init-account)
├── account_metadata.json     game name, snapshot time/zone, cash, coverage flags
├── holdings.csv              opening holdings snapshot
└── trades.csv                every actual fill after (or before) the snapshot
```

Blank templates and **fictitious** examples are in `templates/account/`.

## account_metadata.json

Use `null` for anything you don't know. Unknown values block only the
operations that need them.

| Field | Meaning | Blocks if unknown |
|---|---|---|
| `game_name` | Exact class-game name. Every trade's `game_name` must match it. | Orders and buy counting |
| `snapshot_captured_at` | Time the holdings were read, e.g. `2026-10-20T20:31:00` | Trade timing |
| `snapshot_timezone` | IANA zone, e.g. `America/New_York` | Fills within ±14 h of the snapshot are "ambiguous" |
| `holdings_complete` | `true` once you confirm the snapshot lists **every** position | Orders, impossible-sell check |
| `cash` | Cash at the snapshot | Orders |
| `reported_account_value` | Account value shown at the snapshot. Checked against cash + holdings. | — |
| `reported_holdings_value`, `reported_unrealized_pnl_total` | Totals shown. Checked against the rows. | — |
| `ledger_complete_after_snapshot` | `true` once `trades.csv` contains every fill since the snapshot | Orders, current cash |
| `buy_history_complete_from` | Earliest time from which **every** BUY is recorded | Monthly buy count is "unknown" until it is on or before the 1st of the month |
| `etf_counts_toward_stock_minimum` | Set this after the professor answers. While `null`, ETFs are **not** counted. | — |
| `fractional_shares_allowed` | `null` or `false` means whole shares | — |
| `commission_per_trade` | Commission charged by the game | Defaults to the config estimate ($0) |

## holdings.csv

The columns are `ticker`, `shares`, `displayed_purchase_price`,
`displayed_current_price`, `market_value`, `reported_unrealized_pnl`, and
`asset_type` (optional). `asset_type` is STOCK or ETF; leave it blank to use
the provider's quote type.

- **`displayed_purchase_price`** is Investopedia's "Purchase Price" column: a
  rounded average cost per share. It is used only to cross-check P&L.
- **`displayed_current_price`** is the price shown on the screenshot. It is
  **not** a timestamped market quote.

Reported values are always kept. Calculated values (`calc_market_value`,
`calc_unrealized_pnl`) are stored next to them with their differences.

**Rounding tolerances:**

- Market value: shares × $0.005 + $0.01. Exceeding it is an error.
- P&L: shares × $0.01 + $0.01. Exceeding it is a warning.

## trades.csv: one row per fill

| Column | Rule |
|---|---|
| `trade_id` | Unique and non-blank. Duplicates are an error. |
| `order_id` | Optional but strongly recommended. Partial fills with the same ID count as **one** buy order. |
| `executed_at` | ISO 8601 **with UTC offset**, e.g. `2026-10-21T10:15:00-04:00` |
| `side` | `BUY` or `SELL` |
| `shares`, `fill_price` | Positive and finite |
| `fees` | Zero or more |
| `game_name` | Must equal `game_name` in metadata |
| `recommendation_run_id` | Optional: which `private/runs/<id>` suggested it |
| `notes` | Free text |

## Monthly BUY compliance

- Executed BUY orders are counted per calendar month in `America/New_York`.
- If fills have no `order_id`, the count is reported as a range. The low end
  merges same-ticker, same-day fills; the high end counts every fill.
- When history is incomplete, the count is a lower bound. The status is
  **UNKNOWN**, never zero and never "compliant", unless the recorded count
  already meets the minimum.
- Recommendations are never counted.

## Daily workflow

```bash
python -m src.cli validate        # check inputs (no network)
python -m src.cli recommend       # live data -> private/runs/<id>/report.md
# ... review the report, decide, place orders manually in the CLASS game ...
# then add each actual fill to private/account/trades.csv and re-run validate
```
