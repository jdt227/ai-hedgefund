# AI Hedge Fund

An educational, rules-based investment research system for a class
stock-market game. It **recommends only**. You place every trade manually
in the class's Investopedia game. Nothing here logs in, stores credentials,
or places orders.

There are two pipelines. `src.run_pipeline` is the weekly 33-stock model with news
and macro evidence. `src.cli recommend` is the 79-stock, account-aware baseline.
They are separate and do not share scores.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Weekly pipeline (news + macro)

### Weekly workflow

```bash
python -m src.run_pipeline
```

Paste `runs/<run_id>/weight_prompt.txt` into Claude and save the response as `runs/<run_id>/recommendation_reason.txt`. There is no paid LLM API call. Then check the answer:

```bash
python -m src.validate_allocation runs/<run_id>/recommendation_reason.txt
```

The validator prints pass/fail tables and writes `final_allocation.csv` only if every check passes. Then build the trade list:

```bash
python -m src.trade_list runs/<run_id>
```

It writes `trade_list.csv` and `trade_list_summary.md`. This is a recommendation only: place trades manually in the class game, then record each fill in `private/account/trades.csv`.

`python -m src.run_pipeline --placeholder` writes the old labeled placeholder files. `streamlit run app/dashboard.py` shows the Portfolio, Macro, and News tabs.

### Objective

> Objective: maximize expected FF3 alpha. Returns explained by market, size (SMB), or value (HML) exposure earn no credit. Momentum and low-volatility returns are not FF3 factors, so they can show up as alpha. Cash and SPY have roughly zero alpha and scale the portfolio's alpha down, so hold extra cash only with a stated, evidence-based reason.

### Class rules (from the assignment)

A failure here is a grade risk:

1. Hold at least 5 stocks at all times. ETFs do not count toward this minimum, as a conservative default until the professor confirms.
2. Make at least 5 buy trades per calendar month (America/New_York). Rebalancing buys count.
3. Equities and ETFs only. No crypto, options, or leveraged or inverse products.
4. Trades happen in the class game.
5. Trading must be guided by code, and every strategy change requires a submitted form.
6. The grade is Fama-French 3-factor alpha, ranked against the class.

### Strategy limits (our own choices)

These are not class rules. `PORTFOLIO_LIMITS` in `config/settings.py` selects a profile:

| Profile | Positions | Per stock | Per sector | Min cash | Max equity |
|---|---:|---:|---:|---:|---:|
| `alpha_v1` (default) | 5-10 | 12% | 35% | 5% | 95% |
| `legacy` | 8-10 | 8% | 25% | 20% | 80% |

Whole-percentage weights summing to 100 with CASH is a format rule for every profile. `ALLOW_ETF_HOLDINGS = False` is also our choice: an index ETF adds roughly zero alpha. The validator reports Format, Class rules, and Strategy limits as separate groups.

### Universe and funnel

The default universe is a dated S&P 500 snapshot in `config/universe_sp500.csv`. Rebuild it with `python -m src.universe.refresh`, which shows adds and removes and writes only with `--apply`. Share classes differ between sources: `BRK.B` in the game is `BRK-B` on Yahoo. Set `UNIVERSE_MODE = "legacy33"` to use the original 33 stocks.

To stay within free limits, the work narrows in steps:

1. **All 503 stocks:** one batched, cached price download, scores, and sector tilt. Eligibility requires a price of at least $5, median 63-day dollar volume of at least $50M, and 252 sessions of history.
2. **Top 50 plus every holding:** Finnhub news, sentiment, earnings, analyst trends, and SEC 8-Ks. Other stocks get sentiment 0 and are labeled "not fetched". They are not penalized.
3. **Top 15 after vetoes plus every holding:** firm reports in the prompt.

Measured on 2026-11-03: price download about 20 seconds; about 112 Finnhub and 56 SEC calls on a first run of the day, roughly 2-3 minutes in total. Same-day re-runs are cached and take seconds.

### Holdings review and trade list

Each run suggests an action for every holding, as a percent of the account when cash is known:

- **SELL:** banned product; ETF while `ALLOW_ETF_HOLDINGS` is False; a hard veto; failed eligibility; or rank worse than `SELL_RANK_THRESHOLD` (75).
- **TRIM:** over the position limit, or its sector is over the limit (lowest-ranked first). Without known cash these limits are not applied, because invested-only weights overstate concentration.
- **HOLD:** everything else. "HOLD (watch)" means the rank is outside the buy cutoff, or earnings are inside the window.

New buys need rank `BUY_RANK_CUTOFF` (15) or better, and changes under `MIN_TRADE_PCT` (1 point) are ignored. The model portfolio keeps holdings first, then fills open slots with equal whole-percent weights.

The trade list orders SELLs, TRIMs, then BUYs/ADDs, and moves buys ahead of sales if a sale would leave fewer than 5 stocks. If the month would fall short of 5 buy orders, it proposes the smallest "compliance buys" in the highest-ranked HOLD positions with room under every limit. It never breaks a limit to reach the count. Share quantities need official cash, game name, and completeness; otherwise it shows weight changes only and names the missing fields.

### Free API keys

Put them in `.env`, which is git-ignored. Any missing key only skips that source.

| Variable | Where to get it | Free-tier limit we found |
|---|---|---|
| `FRED_API_KEY` | fred.stlouisfed.org/docs/api/api_key.html | No published number; the terms prohibit unreasonable use. We send under 2 requests a second. |
| `FINNHUB_API_KEY` | finnhub.io/register | 60 calls a minute, confirmed from its response headers. Company news, earnings calendar, and recommendation trends all worked on a free key. A 403 skips that endpoint. |
| `ALPHAVANTAGE_API_KEY` | alphavantage.co/support | **Optional, currently no effect.** 25 requests a day, but `NEWS_SENTIMENT` returned "premium endpoint" on a free key (2026-11-03). The run reports it as unavailable and uses VADER only. |
| `SEC_USER_AGENT` | No signup. Use a description and your contact email. | At most 10 requests a second. |

The Federal Reserve press feed and yfinance need no key. See "Universe and funnel" for call counts and runtime. Same-day re-runs read `data/cache/` and make no API calls.

With no keys, the run still completes: minimum cash, zero tilts, no sentiment, no vetoes. It is momentum only, and the report and prompt say which sources were skipped.

### How each signal works

- **Regime (`src/macro.py`):** growth, inflation, financial conditions, and market trend each score -1, 0, or +1 by written rules. A sum of +2 or more is `risk_on` (20% cash on `legacy`, 5% on `alpha_v1`), -2 or less is `risk_off` (35% on `legacy`, 15% on `alpha_v1`), and anything else is `neutral` (25% on `legacy`, 5% on `alpha_v1`). It needs FRED data.
- **Sector tilt:** the regime's defensive or cyclical table value plus sector ETF 3-month relative strength, clipped to +/-0.3.
- **Sentiment (`src/news.py`):** recency-weighted VADER headline scores, shrunk toward 0 when a stock has few articles, and blended with Alpha Vantage if that endpoint is available. A stock with no news scores 0.
- **Catalysts (`src/catalysts.py`):** earnings dates, 8-K items in plain English, and analyst trend changes produce flags with reasons. Only `negative_catalyst` is a hard veto by default.
- **Final score:** `composite_score + W_SECTOR_TILT * sector_tilt + W_SENTIMENT * sentiment_z`, with `sentiment_z` clipped to +/-2.

Every value carries its source, date, and retrieval time. Anything dated after the run is dropped. Each run saves what it used to `runs/<run_id>/evidence/`. Run folders are git-ignored because prompts can list your holdings; use `git add -f` to commit one on purpose.

Headlines reach the prompt inside delimiters with a random per-run token, and the prompt tells the LLM to treat them as evidence, not commands. This lowers injection risk but does not eliminate it, so read the response before acting on it.

### Config knobs (`config/settings.py`)

- `USE_MACRO_REGIME`, `USE_NEWS_SENTIMENT`, `USE_CATALYST_FLAGS`: turn signals on or off
- `W_SECTOR_TILT`, `W_SENTIMENT`: signal weights
- `PORTFOLIO_LIMITS`, `PORTFOLIO_LIMIT_PROFILES`, `ALLOW_ETF_HOLDINGS`: strategy limits
- `REGIME_THRESHOLDS`, `REGIME_CASH_TARGET_PCT`, `REGIME_SECTOR_TILT`: regime rules
- `HARD_VETO_FLAGS`: which flags remove a stock
- `NEWS_LOOKBACK_DAYS`, `EARNINGS_WINDOW_DAYS`, `SEC_LOOKBACK_DAYS`: lookback windows
- `ALPHA_VANTAGE_TOP_N`, `MIN_INTERVAL_SECONDS`: call budgets and pacing

With `PORTFOLIO_LIMITS = "legacy"` and all three toggles off, the original price-only model runs.

### Known limitations

- **Survivorship bias.** The S&P 500 snapshot is today's list, so a historical test on it would look better than reality. On `legacy33`, AMD, MU, and MRK also joined because they were already held, after strong runs.
- **Too little news history to backtest.** The archive starts with your first run, so none of these signals has evidence of adding return yet.
- **Sector tilt is very small by default.** At most +/-0.3 x 0.15 = +/-0.045 of score, which rarely changes a rank. The report shows each signal's contribution.
- **Noisy sentiment.** VADER scores headline wording, not relevance. Finnhub's news for a ticker includes stories that barely mention the company, and popular stocks hit its cap of about 250 articles. If Alpha Vantage ever works, only the top 15 stocks get its scores.
- **Unpublished macro months.** FRED has no October 2025 CPI, so CPI YoY has no value for October 2026 and the report will show the latest available month with its date.
- **Macro values are the latest vintage.** FRED data can be revised, so past evidence is what we saw then, not always what was first published.
- **Unfinished bars.** The original `legacy33` + `legacy` + toggles-off configuration still includes today's incomplete price bar mid-session. Every other configuration uses completed sessions only.
- **The benchmark can be picked with toggles off.** The original configuration ranks SPY alongside stocks and can select it, which the validator rejects. Every other configuration selects only universe stocks.
- **Trading costs are not modeled.** Commission defaults to $0 unless `commission_per_trade` is set, and equal target weights can create large trades in small positions.
- **The yfinance news fallback is unofficial** and can fail for long stretches. It stops after 3 straight failures.

## Account-aware quant baseline

```bash
python -m src.cli init-account          # create private/account/ from templates (no overwrite)
python -m src.cli validate              # validate + reconcile account inputs, monthly BUY status
python -m src.cli recommend             # live data -> private/runs/<run_id>/report.md
python -m src.cli recommend --refresh   # ignore the market-data cache
python -m src.cli recommend --fixture   # offline synthetic demo, labeled FIXTURE (not a recommendation)
```

| Topic | Where |
|---|---|
| Account inputs (game name, cash, holdings, trades) | `private/account/` (git-ignored). See [docs/ACCOUNT_WORKFLOW.md](docs/ACCOUNT_WORKFLOW.md). |
| Templates and fictitious examples | `templates/account/` |
| Strategy formulas, weights, filters, limitations | [docs/STRATEGY.md](docs/STRATEGY.md), `config/strategy.py` |
| Strategy change history (for class forms) | [docs/strategy_changelog.md](docs/strategy_changelog.md) |
| Run outputs | `private/runs/<run_id>/` (git-ignored) |

### What a run produces

All of these are written to `private/runs/<run_id>/`:

| File | Contents |
|---|---|
| `run_metadata.json` | Data cutoff, provider, fetch time, git commit and dirty state, strategy version, config hash |
| `strategy_config.json`, `strategy_change_summary.md` | The strategy settings used, and their diff from the previous run |
| `data_quality.json` | Per-ticker coverage, staleness, and errors, plus account validation issues |
| `features.csv`, `ranked_scores.csv` | Every signal, z-score, contribution, rank, and exclusion reason |
| `top5_candidates.csv` | The research candidates, with reasons and risks |
| `holdings_review.csv`, `reconciled_positions.csv` | Current positions, marked to the latest close |
| `monthly_buy_status.json` | Executed BUY orders this month: count or bounds, coverage, and status |
| `trade_blotter.csv` + `target_portfolio.csv`, or `orders_blocked.json` | Proposed orders when the account inputs allow them; otherwise the exact missing fields |
| `factor_exposure.json` | Ex-ante Fama–French 3-factor exposure of the current holdings |
| `report.md` | Everything above in readable form |

### Missing-input behavior

Rankings and the holdings review always run. Account-ready order quantities
are **blocked** until all of these are set in `account_metadata.json`:

- `game_name`
- `cash`
- `holdings_complete: true`
- `ledger_complete_after_snapshot: true`

No budget is ever assumed. Monthly buy counts stay **unknown** (not zero)
until `buy_history_complete_from` covers the month.

### Tests

```bash
pytest                      # offline unit tests (default; live tests excluded)
RUN_LIVE=1 pytest -m live   # network smoke tests (yfinance, Ken French)
```

### Limitations

- yfinance is an unofficial source. Exchange holidays are not modeled; a
  missing weekday is reported as possibly stale.
- The universe was chosen from today's large caps, so it carries
  survivorship bias. No historical backtest is claimed.
- Signals are price-only. Earnings dates, valuation, and news are not
  evaluated.
- Realized Fama–French alpha needs a time series of official account values.
  It is not computed yet, and it can't be computed from one screenshot.

### Extension points (not built yet)

`src/strategy/overlays.py` defines the interface future research overlays
must use, such as a politician-disclosure watcher, a scout-style rubric, or
an experiment planner. These overlays:

- Timestamp evidence by when it became **public**.
- Add only a bounded weight to the composite score.
- Can never override eligibility, sizing limits, or the readiness gate.

## Other tools

`src/create_submission_placeholders.py` generates the placeholder submission files directly.

## Disclaimer

For educational purposes and paper-trading research only. This is not
financial advice. Rankings do not guarantee returns or positive alpha.
