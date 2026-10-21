# AI Hedge Fund

An educational, rules-based investment research system for a class
stock-market game. It **recommends only**. You place every trade manually
in the class's Investopedia game. Nothing here logs in, stores credentials,
or places orders.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Quant baseline pipeline (current)

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

## Legacy hypothetical pipeline

These earlier tools still work unchanged:

- `python -m src.run_pipeline` builds a hypothetical $100k, 10-stock paper
  portfolio. It is not account-aware.
- `streamlit run app/dashboard.py` displays that portfolio.
- `src/create_submission_placeholders.py` generates the placeholder
  submission files.

## Disclaimer

For educational purposes and paper-trading research only. This is not
financial advice. Rankings do not guarantee returns or positive alpha.
