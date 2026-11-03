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

## Weekly model: news and macro overlays — 2026-11-03

This entry covers `src.run_pipeline`. It does not change `quant-baseline-1.0.0`.

- **What:**
  - Macro regime sets target cash at 20%, 25%, or 35% and adds small sector tilts.
  - News sentiment adds a bounded score adjustment.
  - Catalyst flags report risks, and `negative_catalyst` removes a stock before selection.
  - The base score is unchanged: 6M-ex-1M momentum, 1M momentum, and volatility.
  - The model now selects only universe stocks and uses whole-percent weights.
- **Why:** to inform weekly decisions with dated, sourced macro and company evidence, and to replace the prompt's placeholder sections.
- **Evidence:** none yet. News history is too short to backtest, and the starting weights are judgment calls. Each run now archives its evidence so later comparisons are possible. Toggles in `config/settings.py` allow runs with and without each signal.

## Weekly model: universe expanded to 33 stocks — 2026-11-03

This entry covers `src.run_pipeline`. It also applies when all toggles are off, because the original model reads the same list.

- **What:** added AMD and MU (Technology) and MRK (Health Care).
- **Why:** the account already holds them, after they ranked highly in the separate 79-stock baseline. With the 30-stock list, the validator rejected any allocation that kept them, and the model could not rank them.
- **Evidence:** none for the change itself. This is an account-consistency change, not a tested improvement. It adds selection bias, because these names were picked after strong price runs. It also changes the z-score averages, so scores and picks can differ from the 30-stock list.

## Weekly model: alpha_v1 strategy limits — 2026-11-03

This entry covers `src.run_pipeline`. **File a strategy-change form for this entry.**

- **What:**
  - Separated class rules from our own strategy limits.
  - New default profile `alpha_v1`: 5-10 positions, 12% per stock, 35% per sector, at least 5% cash, at most 95% equity. The previous limits remain available as `legacy`.
  - Regime cash targets are now 5% (risk_on), 5% (neutral), and 15% (risk_off).
  - ETFs are no longer allowed as strategy holdings, and banned products are rejected.
- **Why:** the grade is FF3 alpha. The old 20% cash floor and 8% cap were our own limits, and cash scales alpha toward zero.
- **Evidence:** none from this account. This follows from the grading metric, not a backtest. Higher concentration also raises the risk of a large drawdown.

## Weekly model: S&P 500 universe and funnel — 2026-11-03

This entry covers `src.run_pipeline`. **File a strategy-change form for this entry.**

- **What:**
  - The default universe is now a dated S&P 500 snapshot (`config/universe_sp500.csv`, 503 tickers), with `legacy33` still available.
  - Eligibility filters: price of at least $5, median 63-day dollar volume of at least $50M, at least 252 sessions of history.
  - Scores are measured against eligible stocks, with z-scores capped at +/-3.
  - News and catalysts are fetched only for the top 50 plus holdings. Other stocks are not penalized.
  - Real Estate joined with a neutral regime tilt.
- **Why:** a wider universe gives the momentum and low-volatility signals more stocks to choose from. Capping stops one extreme stock from distorting every score: in a trial, MRNA's volatility z-score reached 15.7.
- **Evidence:** none from this account. The list is today's S&P 500, so any historical test on it has survivorship bias.

## Weekly model: holdings review and sell suggestions — 2026-11-03

This entry covers `src.run_pipeline`. **File a strategy-change form for this entry.**

- **What:**
  - Every holding now gets a suggested HOLD, TRIM, or SELL.
  - New buys need rank 15 or better. Holdings stay until they rank worse than 75 (`SELL_RANK_THRESHOLD`), which is about the top 15% of the universe.
  - The model portfolio keeps those holdings first, then fills open slots with new names, using equal whole-percent weights.
  - The LLM response must include a Holdings actions block, which the validator checks.
- **Why:** the portfolio should evolve by rule instead of ignoring what is held. The gap between the buy and sell lines limits churn on a noisy signal. The sell line was set to 75 instead of 40 because rank 40 would have sold five of eight stocks in one week.
- **Evidence:** none from this account. The thresholds are judgment calls, and turnover is not yet measured.

## Weekly model: trade list and buy compliance — 2026-11-03

This entry covers `src.trade_list`. **File a strategy-change form for this entry.**

- **What:** validated allocations now become an ordered trade list. It keeps at least 5 stocks at every step, tracks the month's buy orders against 5, and proposes the smallest "compliance buys" when the month would fall short. It never breaks a limit to reach the count. Trades are still placed by hand.
- **Why:** to meet the class rules at every step of a rebalance, not just at the end.
- **Evidence:** none needed for rule compliance. Compliance buys may add small trades with no expected alpha.
