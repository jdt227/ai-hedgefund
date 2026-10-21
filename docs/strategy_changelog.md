# Strategy changelog

Record every material strategy change here: the version, the date, what
changed, why, and what evidence supported it. Each run's
`strategy_change_summary.md` lists config differences from the previous run,
so you can copy them here and into class forms.

## quant-baseline-1.0.0 — 2026-10-20

- **What:** first deterministic quant baseline.
  - Universe: 79 liquid U.S. large-cap common stocks.
  - Score: weighted z-scores of 12-1M momentum (0.35), 6-1M momentum (0.25),
    and trend vs. the 200-day average (0.10), plus penalties for 63-day
    volatility (0.15) and 1-year max drawdown (0.15).
  - Order sizing (gated on official account data): 15% position cap, 35%
    sector cap, 8% new-position target, cash buffer, no automatic sells.
- **Why:** momentum and low-risk effects are among the most widely documented
  cross-sectional return patterns. Price data is available reliably and with
  correct timing, unlike free point-in-time fundamentals. This gives an
  explainable benchmark that future changes and AI overlays can be compared
  against.
- **Evidence:** none from this account yet. This is a starting rule set, not
  a result fitted to a backtest.
- **Supersedes:** the legacy hypothetical $100k pipeline (`src/run_pipeline.py`).
  That pipeline is unchanged and still runnable, but it is not account-aware.
