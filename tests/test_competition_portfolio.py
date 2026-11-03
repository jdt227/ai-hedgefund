import numpy as np
import pandas as pd
import pytest

from config import settings
from src.features import add_final_score
from src.portfolio import (build_competition_portfolio, build_portfolio, holdings_needed,
                           whole_percent_weights)


def scored_universe(seed=0, tickers=None):
    tickers = tickers or list(settings.UNIVERSE)
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame({
        "price": rng.uniform(20, 900, len(tickers)),
        "long_momentum": rng.normal(0.1, 0.2, len(tickers)),
        "short_momentum": rng.normal(0.01, 0.05, len(tickers)),
        "annualized_volatility": rng.uniform(0.15, 0.6, len(tickers)),
        "composite_score": rng.normal(0, 1, len(tickers)),
    }, index=tickers)
    return frame.sort_values("composite_score", ascending=False)


def assert_rules(portfolio, failures):
    assert not failures
    weights = portfolio["weight_pct"]
    assert settings.MIN_HOLDINGS <= len(portfolio) <= settings.MAX_HOLDINGS
    assert (weights == weights.astype(int)).all()
    assert weights.max() <= settings.MAX_POSITION_PCT
    assert weights.sum() <= settings.MAX_EQUITY_PCT
    assert 100 - weights.sum() >= settings.MIN_CASH_PCT
    assert portfolio.groupby("sector")["weight_pct"].sum().max() <= settings.MAX_SECTOR_PCT
    assert set(portfolio["ticker"]) <= set(settings.UNIVERSE)


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("cash", sorted(settings.REGIME_CASH_TARGET_PCT.values()) + [20, 30, 40, 50, 60])
def test_rules_hold_under_every_regime_and_score_order(seed, cash):
    final = add_final_score(scored_universe(seed), settings.UNIVERSE)
    portfolio, _, failures = build_competition_portfolio(final, cash)
    assert_rules(portfolio, failures)
    assert 100 - portfolio["weight_pct"].sum() == cash


@pytest.mark.parametrize("cash", [0, 5, 19])
def test_cash_target_below_minimum_is_raised(cash):
    final = add_final_score(scored_universe(), settings.UNIVERSE)
    portfolio, _, failures = build_competition_portfolio(final, cash)
    assert_rules(portfolio, failures)
    assert 100 - portfolio["weight_pct"].sum() == settings.MIN_CASH_PCT


@pytest.mark.parametrize("equity,count", [(80, 10), (75, 10), (72, 9), (65, 9), (64, 8), (40, 8)])
def test_holdings_needed(equity, count):
    assert holdings_needed(equity) == count


def test_whole_percent_weights_sum_exactly():
    assert whole_percent_weights(65, 9) == [8, 8, 7, 7, 7, 7, 7, 7, 7]
    assert sum(whole_percent_weights(75, 10)) == 75


def test_conflict_between_cash_and_minimum_holdings_lowers_weights():
    final = add_final_score(scored_universe(), settings.UNIVERSE)
    portfolio, _, _ = build_competition_portfolio(final, 60)
    assert len(portfolio) == 8 and portfolio["weight_pct"].tolist() == [5] * 8


def test_vetoed_names_are_never_selected_and_are_recorded():
    final = add_final_score(scored_universe(), settings.UNIVERSE)
    top = final.index[0]
    portfolio, rejected, failures = build_competition_portfolio(final, 25, {top: "negative_catalyst: test"})
    assert_rules(portfolio, failures)
    assert top not in set(portfolio["ticker"])
    assert "veto" in rejected.set_index("ticker").at[top, "reason"]


def test_sector_cap_rejections_are_recorded():
    frame = scored_universe()
    tech = [t for t, s in settings.UNIVERSE.items() if s == "Technology"]
    frame.loc[tech, "composite_score"] = 10 + np.arange(len(tech))
    final = add_final_score(frame.sort_values("composite_score", ascending=False), settings.UNIVERSE)
    portfolio, rejected, failures = build_competition_portfolio(final, 20)
    assert_rules(portfolio, failures)
    assert (portfolio["sector"] == "Technology").sum() == 3
    assert rejected["reason"].str.contains("sector cap").sum() == len(tech) - 3


def test_too_few_names_reports_failure_without_breaking_caps():
    final = add_final_score(scored_universe(), settings.UNIVERSE)
    keep = list(final.index[:5])
    vetoed = {t: "test" for t in final.index if t not in keep}
    portfolio, _, failures = build_competition_portfolio(final, 20, vetoed)
    assert len(portfolio) == 5 and failures
    assert portfolio["weight_pct"].max() <= settings.MAX_POSITION_PCT
    assert portfolio.groupby("sector")["weight_pct"].sum().max() <= settings.MAX_SECTOR_PCT


def test_everything_vetoed_gives_empty_portfolio_and_failure():
    final = add_final_score(scored_universe(), settings.UNIVERSE)
    portfolio, _, failures = build_competition_portfolio(final, 20, {t: "test" for t in final.index})
    assert portfolio.empty and failures


def test_benchmark_is_not_selectable():
    frame = scored_universe()
    frame.loc["SPY"] = [500.0, 0.5, 0.1, 0.1, 99.0]
    final = add_final_score(frame.sort_values("composite_score", ascending=False), settings.UNIVERSE)
    portfolio, _, _ = build_competition_portfolio(final, 20)
    assert "SPY" not in set(portfolio["ticker"])


@pytest.mark.parametrize("seed", range(10))
def test_no_overlay_data_matches_old_model(seed):
    """Golden test: zero overlays give the same picks and weights as build_portfolio."""
    scored = scored_universe(seed)
    old = build_portfolio(scored)
    new, _, _ = build_competition_portfolio(add_final_score(scored, settings.UNIVERSE))
    assert old["ticker"].tolist() == new["ticker"].tolist()
    assert old["target_weight"].tolist() == new["target_weight"].tolist()
    assert old["shares"].tolist() == new["shares"].tolist()


def test_final_score_formula_and_clipping():
    frame = scored_universe(tickers=["AAPL", "XOM"])
    tilts = {"Technology": 0.3, "Energy": -0.3}
    sentiment = pd.Series({"AAPL": 50.0, "XOM": -1.0})
    out = add_final_score(frame, settings.UNIVERSE, tilts, sentiment, w_sector=0.15, w_sentiment=0.10)
    assert out.at["AAPL", "sentiment_z"] == settings.SENTIMENT_Z_CLIP
    expected = frame.at["AAPL", "composite_score"] + 0.15 * 0.3 + 0.10 * settings.SENTIMENT_Z_CLIP
    assert out.at["AAPL", "final_score"] == pytest.approx(expected)
    assert out.at["XOM", "final_score"] == pytest.approx(frame.at["XOM", "composite_score"] - 0.045 - 0.10)


def test_missing_overlay_inputs_leave_score_unchanged():
    frame = scored_universe()
    out = add_final_score(frame, settings.UNIVERSE)
    assert out["final_score"].equals(out["composite_score"])
    assert out.index.tolist() == frame.index.tolist()
