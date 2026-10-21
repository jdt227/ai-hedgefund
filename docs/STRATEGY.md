# Strategy: `quant-baseline-1.0.0`

All parameters live in `config/strategy.py`. Each run saves a copy of them
(`strategy_config.json`) plus a short hash, so you can compare runs.

## Universe

There are 79 hand-picked, liquid, large-cap U.S.-listed common stocks
across 11 sectors (`UNIVERSE`). SPY and other ETFs are **tracked but not
ranked**. They get prices and signals but never enter the candidate list.

**Selection bias.** The list was chosen in 2026 from companies that are
large *today*. That's fine for forward-looking ranking. A historical backtest
on this list would be biased upward, though: it leaves out companies that
shrank, failed, or were acquired. Don't use it to claim past
outperformance.

## Data

- **Provider:** yfinance (unofficial Yahoo Finance). It's replaceable via
  `MarketDataProvider` in `src/market_data/provider.py`.
- **History:** 2 years of daily bars, enough for 252 + 21 sessions.
- **Completed bars only:** a session's bar is used only after 16:30 New York
  time on that date. Rows whose adjusted close has not been published are
  also dropped and listed in the report.
- **Prices.** There are three kinds, and they are used for different jobs:
  - *Adjusted close* (splits and dividends) is used for every return
    calculation.
  - *Close* (splits only) is the **reference price** for share sizing and
    marked values.
  - *Screenshot prices* stay in `holdings.csv` as reported and are never used
    as market quotes.
- **Staleness:** the report compares the last completed session with the most
  recent weekday whose close has passed. Holidays are not modeled. More than 2
  missing weekdays counts as stale and blocks order sizing. A ticker whose
  last bar is older than the market's last bar is ineligible.
- **Fundamentals:** current market cap, name, and sector are fetched for
  labeling only. No fundamental value enters the v1 score, so nothing
  current is mistaken for a historical point-in-time fundamental.

## Signals

`t` is the last completed session. `P` is the adjusted close, and lookbacks
count trading sessions.

| Signal | Formula | Direction | Weight |
|---|---|---|---:|
| `mom_12_1` | P[t-21] / P[t-252] − 1 | higher better | 0.35 |
| `mom_6_1` | P[t-21] / P[t-126] − 1 | higher better | 0.25 |
| `trend_200` | P[t] / mean(P[t-199..t]) − 1 | higher better | 0.10 |
| `vol_63` | stdev(daily returns, 63) × √252 | lower better | 0.15 |
| `max_dd_252` | min(P / running max − 1) over 252 | closer to 0 better | 0.15 |

These two are descriptive only and are not scored:

- `ret_1m`
- `rs_6m_vs_benchmark`

Relative strength versus SPY subtracts the same number from every stock, so
its cross-sectional z-score is identical to `mom_6_1`. Scoring it would
double-count.

**Why skip the latest month?** Over 1-month horizons, stocks tend to partly
reverse ("short-term reversal"). Momentum research therefore usually measures
returns over 12 months *excluding* the most recent month.

## Scoring

1. **Eligibility.** A stock must pass every check below. Each exclusion is
   recorded with its reason.
   - It is in the universe and is not monitor-only.
   - Its quote type is `EQUITY`. If the provider can't classify it, the
     stock stays eligible and is flagged.
   - It has at least 273 sessions of history.
   - Its price is at least $5.
   - Its median 63-day dollar volume is at least $50M.
   - Its data is not stale.
2. **z-scores.** For each signal, z = (x − mean) / std (population std),
   computed across eligible stocks only and clipped to ±3.
3. **Composite.** composite = Σ weight × direction × z.
4. **Ranking.** Rank by composite, highest first. Ties are broken
   alphabetically by ticker, so runs are deterministic.

**Missing data:** a stock missing any required signal is **ineligible** with
the reason "insufficient history". It is never filled in with a value or
scored as if poor.

**Liquidity** is a filter, not a score component. Every name here is
liquid. A liquidity score would just tilt toward the largest companies, which
is a size bet (negative SMB) that the Fama–French evaluation would penalize.

## Outputs, kept distinct

| Output | What it is |
|---|---|
| Top-five research candidates | The five highest-ranked eligible stocks. Each has a status: possible addition, existing holding, or (conditionally) constrained by the sector cap. This is a ranking, not a buy list. |
| Holdings review | Every current position with its marked value, rank, and a review flag. The flag says "bottom quintile → review", "top quintile → supported", or "neutral". |
| Target portfolio / proposed orders | Produced only when official cash, game name, holdings completeness, and ledger completeness are all confirmed. |
| Executed trades | Only what you record in `private/account/trades.csv`. |

## Order sizing (only when inputs allow)

- **Limits:**
  - Max position: 15% of equity.
  - Max sector: 35%.
  - New-position target: 8%.
  - Cash buffer: the larger of 2% of equity or $500.
  - Slippage buffer: 0.5% added to the reference price.
  - Minimum order: $1,000.
- **What gets bought:** only new names ranked 15 or better, in rank order,
  with at most 5 buys per run.
- **Shares:** whole shares unless `fractional_shares_allowed` is true. Every
  limit is checked again after rounding.
- **No automatic sells** (`allow_trim_sells = False`). Positions over a cap
  are reported for manual review.
- **Why 15% / 35% instead of the legacy 8% / 25%?** An 8% cap would force at
  least 13 positions. With a 5-stock minimum and a small account, 15% / 35% is
  a coherent concentration limit. Change it in config and bump the version if
  you disagree.

## Evaluation

`src/evaluation/fama_french.py` runs the FF3 regression on **total** daily
returns. RF is subtracted inside the function. The factors come from Ken
French's daily file, converted from percent to decimal. Output includes:

- Newey–West t-stats.
- Two annualizations: arithmetic (×252) and geometric.
- An "insufficient history" result when there are fewer than 60
  observations.

Each run reports the **ex-ante** factor exposure of today's holdings weights
over the past year. That number is *not* your realized alpha. Realized alpha
needs a time series of official account values (see the README's next steps).
