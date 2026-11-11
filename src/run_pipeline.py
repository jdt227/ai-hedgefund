"""Weekly pipeline: prices, optional macro and news overlays, portfolio, weight prompt.

Each overlay is toggled in config/settings.py and skips itself when its data or
API key is missing. With all toggles off, the picks match the price-only model.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import logging
import os
from pathlib import Path
import sys
import time

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))

from config import settings
from src import http_utils
from src.agents.packets import write_packets
from src.build_weight_prompt import RunEvidence, build_weight_prompt, prepare_sections
from src.catalysts import run_catalysts, vetoes
from src.create_submission_placeholders import create_submission_files
from src.account.inputs import DEFAULT_ACCOUNT_DIR, load_account
from src.account.reconcile import reconcile
from src.data import download_market_data, download_price_data
from src.features import add_final_score, add_standardized_scores, calculate_features
from src.macro import MacroResult, run_macro
from src.market_data.provider import expected_last_completed_session
from src.evaluation.contest_run import compute_contest
from src.holdings_review import HoldingsInputs, incumbents_from_review, review_holdings
from src.news import run_news
from src.overlays.politician import PoliticianOverlay
from src.overlays.scout import ScoutOverlay
from src.politicians import fetch_house_trades
from src.portfolio import build_competition_portfolio, build_portfolio, summarize_sector_weights
from src.reporting.audit_files import create_run_directory
from src.reporting.weekly_sections import overlay_sections
from src.strategy_form import changed_since_last_form
from src.universe import get_universe
from src.untrusted import UntrustedBlocks
from src.universe.eligibility import eligibility_table, extreme_move_flags


PROCESSED_DATA_DIR = Path("data/processed")
REPORTS_DIR = Path("reports")
RUNS_DIR = Path("runs")
# Tests point this elsewhere so they never read the private account.
ACCOUNT_DIR = DEFAULT_ACCOUNT_DIR

logger = logging.getLogger(__name__)


def _guarded(name: str, status: dict[str, str], func, default=None):
    """Run one optional module. A failure skips it and is reported, never raised."""
    try:
        return func()
    except Exception as exc:  # any module failure must leave the price-only run intact
        logger.warning("%s failed: %s", name, exc)
        status[name] = f"unavailable: {type(exc).__name__}"
        return default


def overlays_enabled() -> bool:
    return (settings.USE_MACRO_REGIME or settings.USE_NEWS_SENTIMENT or settings.USE_CATALYST_FLAGS
            or settings.USE_SCOUT_OVERLAY or settings.USE_POLITICIAN_OVERLAY)


def uses_original_model() -> bool:
    """True only for the configuration that must reproduce the original price-only picks."""
    return (not overlays_enabled() and settings.PORTFOLIO_LIMITS == "legacy"
            and settings.UNIVERSE_MODE == "legacy33")


def held_tickers(account_dir: Path | None = None) -> list[str]:
    """Current positions, which are always analyzed even outside the universe."""
    try:
        return sorted(reconcile(load_account(account_dir or ACCOUNT_DIR)).positions["ticker"])
    except Exception as exc:  # holdings are optional for research output
        logger.warning("Holdings unavailable: %s", exc)
        return []


def score_universe(prices: pd.DataFrame, close: pd.DataFrame | None, volume: pd.DataFrame | None,
                   universe: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Eligibility first, then scores measured against eligible stocks only."""
    table = eligibility_table(universe, prices, close, volume)
    features = calculate_features(prices)
    if settings.UNIVERSE_MODE == "legacy33":
        return add_standardized_scores(features), table
    eligible = table.index[table["eligible"]]
    return add_standardized_scores(features, settings.Z_SCORE_CAP, eligible), table


def deep_funnel(scored: pd.DataFrame, table: pd.DataFrame, tilts: dict[str, float], universe: dict[str, str],
                held: list[str]) -> list[str]:
    """Top stocks by price score and sector tilt, plus holdings, get news and catalysts."""
    eligible = [t for t in scored.index if t in table.index and table.at[t, "eligible"]]
    if settings.UNIVERSE_MODE == "legacy33":
        return [t for t in universe if t in scored.index]
    prelim = {t: scored.at[t, "composite_score"] + settings.W_SECTOR_TILT * tilts.get(universe[t], 0.0)
              for t in eligible}
    top = sorted(prelim, key=lambda t: (-prelim[t], t))[: settings.FUNNEL_DEEP_N]
    return top + [t for t in held if t not in top]


def build_run(prices: pd.DataFrame, run_date: date, now_utc: datetime, close: pd.DataFrame | None = None,
              volume: pd.DataFrame | None = None, account_dir: Path | None = None) -> RunEvidence:
    """Compute scores, overlays, and the portfolio from prices and environment keys."""
    account_dir = account_dir or ACCOUNT_DIR
    status: dict[str, str] = {"yfinance_prices": f"ok through {prices.index[-1].date()}"}
    universe = get_universe()
    held = held_tickers(account_dir)
    scored, table = score_universe(prices, close, volume, universe)
    excluded = table[~table["eligible"]]
    status["eligibility"] = f"{int(table['eligible'].sum())}/{len(table)} universe stocks eligible"
    quality = extreme_move_flags(prices[[c for c in prices.columns if c in universe or c in held]],
                                 settings.EXTREME_DAILY_MOVE)

    fred_key = os.getenv("FRED_API_KEY") or None
    finnhub_key = os.getenv("FINNHUB_API_KEY") or None
    av_key = os.getenv("ALPHAVANTAGE_API_KEY") or None
    sec_agent = os.getenv("SEC_USER_AGENT") or None

    no_macro = MacroResult("skipped", "Macro module failed; see data-source status.")
    macro = (_guarded("macro", status, lambda: run_macro(run_date, now_utc, fred_key, finnhub_key), no_macro)
             if settings.USE_MACRO_REGIME else MacroResult("disabled", "USE_MACRO_REGIME is False"))
    status["macro"] = status.get("macro", macro.status)
    status.update(macro.source_status)

    deep = deep_funnel(scored, table, macro.sector_tilts, universe, held)
    status["funnel"] = f"{len(deep)} stocks get news and catalysts (top {settings.FUNNEL_DEEP_N} plus holdings)"

    news = None
    if settings.USE_NEWS_SENTIMENT:
        news = _guarded("news", status, lambda: run_news(deep, deep, run_date, now_utc, finnhub_key, av_key))
        if news is not None:
            status.update(news.source_status)
    else:
        status["news"] = "disabled"

    catalysts = None
    if settings.USE_CATALYST_FLAGS:
        sentiment = news.sentiment if news is not None else None
        catalysts = _guarded("catalysts", status,
                             lambda: run_catalysts(deep, sentiment, run_date, finnhub_key, sec_agent))
        if catalysts is not None:
            status.update(catalysts.source_status)
    else:
        status["catalysts"] = "disabled"

    scout = None
    if settings.USE_SCOUT_OVERLAY:
        scout = _guarded("scout", status, lambda: run_scout(scored, table, universe, held, prices, catalysts, now_utc))
        status.setdefault("scout", "ok" if scout is not None and len(scout) else "unavailable: no benchmark prices")
    else:
        status["scout"] = "disabled"
    overlays = {}
    if scout is not None and len(scout):
        overlays["scout"] = (scout.set_index("ticker")["value"], settings.W_SCOUT)
    politics = pd.DataFrame()
    if settings.USE_POLITICIAN_OVERLAY:
        house = _guarded("politician", status, lambda: fetch_house_trades(run_date, settings.POLITICIAN_LOOKBACK_DAYS))
        if house is not None:
            status.update(house.status)
            politics = PoliticianOverlay(house.trades, settings.W_POLITICIAN).compute(
                list(dict.fromkeys(list(universe) + held)), run_date)
            if len(politics):
                overlays["politician"] = (politics.set_index("ticker")["value"], settings.W_POLITICIAN)
    else:
        status["politician"] = "disabled"
    final = add_final_score(scored, universe, macro.sector_tilts,
                            news.sentiment["sentiment_z"] if news is not None else None, overlays=overlays)
    vetoed = vetoes(catalysts.flags) if catalysts is not None else {}
    review, basis = _review(final, table, universe, account_dir, prices, close, catalysts, vetoed)

    if not uses_original_model():
        selectable = {t: sector for t, sector in universe.items() if table.at[t, "eligible"]}
        incumbents, caps = incumbents_from_review(review) if len(review) else (None, None)
        portfolio, rejected, failures = build_competition_portfolio(
            final, macro.cash_target_pct, vetoed, selectable, incumbents=incumbents, weight_caps=caps)
    else:
        portfolio = build_portfolio(scored)
        portfolio["weight_pct"] = (portfolio["target_weight"] * 100).round().astype(int)
        rejected = pd.DataFrame(columns=["ticker", "sector", "rank", "final_score", "reason"])
        failures = []
    return RunEvidence(run_date, now_utc, prices, final, portfolio, rejected, failures, vetoed, macro,
                       news, catalysts, status, universe, excluded, quality, deep, review, basis, account_dir,
                       reference_prices(close if close is not None else prices), {},
                       scout if scout is not None else pd.DataFrame(), politics)


def reference_prices(frame: pd.DataFrame) -> pd.DataFrame:
    """Latest traded close per ticker, used to size trades."""
    rows = [{"ticker": t, "reference_price": float(frame[t].dropna().iloc[-1]),
             "reference_date": str(frame[t].dropna().index[-1].date())}
            for t in frame.columns if frame[t].notna().any()]
    return pd.DataFrame(rows, columns=["ticker", "reference_price", "reference_date"])


def run_scout(scored: pd.DataFrame, table: pd.DataFrame, universe: dict[str, str], held: list[str],
              prices: pd.DataFrame, catalysts, now_utc: datetime) -> pd.DataFrame:
    """Scout grades for eligible stocks and holdings. Team fit is measured against the top candidates."""
    eligible = [t for t in scored.index if t in table.index and table.at[t, "eligible"]]
    top = eligible[: settings.PROMPT_TOP_CANDIDATES]
    overlay = ScoutOverlay(prices, top, settings.W_SCOUT, catalysts.flags if catalysts is not None else {})
    tickers = list(dict.fromkeys(eligible + [t for t in held if t in scored.index]))
    return overlay.compute(tickers, now_utc)


def _review(final: pd.DataFrame, table: pd.DataFrame, universe: dict[str, str], account_dir: Path,
            prices: pd.DataFrame, close: pd.DataFrame | None, catalysts, vetoed: dict[str, str]) -> tuple[pd.DataFrame, str]:
    """Review every holding. Weights use traded closes where available."""
    account = load_account(account_dir)
    positions = reconcile(account).positions
    if account.holdings is None or positions.empty:
        return pd.DataFrame(), ""
    held = positions["ticker"].tolist()
    outside = [t for t in held if t not in universe and t in prices]
    extra = eligibility_table({t: "Other" for t in outside}, prices, close, None) if outside else table.iloc[0:0]
    bad = {t: r for t, r in pd.concat([table, extra])["reason"].items() if r and t in held}
    inputs = HoldingsInputs(
        positions=positions, final_scores=final, eligible=set(table.index[table["eligible"]]), universe=universe,
        prices=close if close is not None else prices, cash=account.metadata.cash,
        flags=catalysts.flags if catalysts is not None else {}, vetoed=vetoed, ineligible_reasons=bad)
    return review_holdings(inputs)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the weekly pipeline.")
    parser.add_argument("--placeholder", action="store_true",
                        help="Only write the clearly labeled placeholder submission files.")
    parser.add_argument("--agents", action="store_true",
                        help="Write agent input packets instead of the final prompt (see /weekly-picks).")
    args = parser.parse_args(argv)
    if args.placeholder:
        for path in create_submission_files():
            print(f"Created {path}")
        return

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    started = time.monotonic()
    run_date = date.today().isoformat()
    now_utc = datetime.now(timezone.utc)

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("1/4 Downloading daily prices...")
    close, volume = None, None
    if settings.UNIVERSE_MODE == "legacy33":
        prices = download_price_data(start="2020-01-01")
    else:
        market = download_market_data(list(get_universe()) + held_tickers(), now_utc)
        prices, close, volume = market.prices, market.close, market.volume
    if not uses_original_model():
        # Use completed sessions only, so nothing from an unfinished day enters the signals.
        last = expected_last_completed_session(now_utc)
        prices = prices.loc[prices.index.date <= last]

    print("2/4 Calculating signals and fetching macro and news (first run of the day can take minutes)...")
    evidence = build_run(prices, date.today(), now_utc, close, volume)
    evidence.contest = _guarded("contest_metric", evidence.source_status,
                                lambda: compute_contest(evidence, close if close is not None else prices, now_utc), {})
    scored_features = evidence.final_scores

    print("3/4 Building constrained portfolio...")
    portfolio = evidence.portfolio
    sector_weights = summarize_sector_weights(portfolio)

    print("4/4 Saving outputs...")

    scored_features.to_csv(
        PROCESSED_DATA_DIR / "latest_stock_rankings.csv"
    )
    portfolio.to_csv(
        PROCESSED_DATA_DIR / "latest_portfolio.csv", index=False
    )
    sector_weights.to_csv(
        PROCESSED_DATA_DIR / "latest_sector_weights.csv", index=False
    )

    report_path = REPORTS_DIR / f"portfolio_report_{run_date}.md"
    limits = settings.limits()

    total_invested = portfolio["actual_dollars"].sum()
    # Empty when vetoes and caps leave no stock; the rule failure is reported below.
    cash_remaining = (
        portfolio["cash_remaining_after_trade"].iloc[0] if len(portfolio) else settings.STARTING_CAPITAL
    )

    lines = [
        f"# AI Hedge Fund — Rules-Based Portfolio Report",
        "",
        f"**Run date:** {run_date}",
        f"**Regime:** {evidence.macro.regime or 'none (' + evidence.macro.status + ')'}",
        f"**Stocks selected:** {len(portfolio)}",
        f"**Hypothetical capital invested (of ${settings.STARTING_CAPITAL:,}):** ${total_invested:,.2f}",
        f"**Hypothetical cash remaining:** ${cash_remaining:,.2f}",
        "",
        "## Proposed Holdings",
        "",
        portfolio[
            [
                "ticker",
                "sector",
                "price",
                "composite_score",
                "target_weight",
                "actual_weight",
                "shares",
                "actual_dollars",
            ]
        ].to_markdown(index=False),
        "",
        "## Sector Weights",
        "",
        sector_weights.to_markdown(index=False),
        "",
        "## Method",
        "",
        (
            "Stocks are ranked by a composite of long-term momentum, "
            "short-term momentum, and trailing annualized volatility. "
            f"Strategy limits (profile {settings.PORTFOLIO_LIMITS}, our own choices, not class rules): "
            f"{limits['max_position_pct']}% per stock, {limits['max_sector_pct']}% per sector, "
            f"{limits['min_holdings']}-{limits['max_holdings']} positions, at least {limits['min_cash_pct']}% cash. "
            "Share counts use a "
            "hypothetical capital figure, not your account."
        ),
        "",
        *overlay_sections(evidence),
        "",
        "Educational paper-trading research only; not investment advice.",
    ]

    report_path.write_text("\n".join(lines), encoding="utf-8")

    run_dir = create_run_directory(RUNS_DIR)
    if args.agents:
        blocks = UntrustedBlocks()
        sections = prepare_sections(evidence, run_dir, blocks=blocks)
        packets = write_packets(evidence, run_dir, sections, blocks)
        prompt_path = None
    else:
        prompt_path = build_weight_prompt(evidence, run_dir)

    print("\nPortfolio created successfully.")
    print(portfolio[["ticker", "sector", "composite_score", "weight_pct"]])
    print(f"Regime: {evidence.macro.regime or 'none (' + evidence.macro.status + ')'}; "
          f"cash {100 - int(portfolio['weight_pct'].sum())}%")
    for failure in evidence.rule_failures:
        print(f"RULE FAILURE: {failure}")
    print("\nData sources:")
    for name, state in evidence.source_status.items():
        print(f"  {name}: {state}")
    print(f"\nNetwork calls this run: {http_utils.CALL_COUNTS or 'none (all cached)'}; "
          f"runtime {time.monotonic() - started:.0f}s")
    print(f"\nSaved report: {report_path}")
    if args.agents:
        print(f"Agent packets: {packets}")
        print(f"Next: run the committee (/weekly-picks), then python -m src.agents.assemble {run_dir}")
    else:
        print(f"Weight prompt: {prompt_path}")
        print(f"Next: paste it into Claude, save the response as {run_dir / 'recommendation_reason.txt'}, then run:")
        print(f"  python -m src.validate_allocation {run_dir / 'recommendation_reason.txt'}")
        print(f"  python -m src.trade_list {run_dir}")
    pending = _guarded("strategy_form", {}, changed_since_last_form, [])
    if pending:
        print("\nStrategy changed since last form: file one (python -m src.strategy_form).")
        for change in pending[:8]:
            print(f"  - {change}")


if __name__ == "__main__":
    main()