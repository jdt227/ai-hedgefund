import pandas as pd
import pytest

from config import settings
from src.banned_products import banned_reason, counts_as_stock, is_etf
from src.features import add_final_score
from src.macro import cash_target
from src.portfolio import build_competition_portfolio, holdings_needed
from src.validate_allocation import CLASS_RULES, STRATEGY_LIMITS, run, validate
from test_competition_portfolio import scored_universe


def rows(*pairs):
    return [(s, str(w)) for s, w in pairs]


TEN = ["AAPL", "MSFT", "JPM", "BAC", "JNJ", "LLY", "XOM", "CVX", "WMT", "KO"]


def failed(checks, group=None):
    return {c.name for c in checks if not c.passed and (group is None or c.group == group)}


@pytest.fixture
def alpha_v1(monkeypatch):
    monkeypatch.setattr(settings, "PORTFOLIO_LIMITS", "alpha_v1")


# --- profiles --------------------------------------------------------------------
def test_default_profile_in_config_is_alpha_v1():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fresh_settings", "config/settings.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh.PORTFOLIO_LIMITS == "alpha_v1"
    assert fresh.limits()["max_position_pct"] == 12


def test_each_profile_has_its_own_cash_table(monkeypatch):
    assert cash_target("risk_off") == 35
    monkeypatch.setattr(settings, "PORTFOLIO_LIMITS", "alpha_v1")
    assert cash_target("neutral") == 5 and cash_target("risk_off") == 15 and cash_target(None) == 5


@pytest.mark.parametrize("equity,count", [(95, 8), (85, 8), (96, 8), (60, 5), (48, 5)])
def test_holdings_needed_alpha_v1(alpha_v1, equity, count):
    assert holdings_needed(equity) == count


@pytest.mark.parametrize("profile", list(settings.PORTFOLIO_LIMIT_PROFILES))
@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize("cash", [5, 15, 25, 35])
def test_portfolio_respects_every_profile(monkeypatch, profile, seed, cash):
    monkeypatch.setattr(settings, "PORTFOLIO_LIMITS", profile)
    limits = settings.limits()
    final = add_final_score(scored_universe(seed), settings.UNIVERSE)
    portfolio, _, failures = build_competition_portfolio(final, cash)
    weights = portfolio["weight_pct"]
    assert not failures
    assert limits["min_holdings"] <= len(portfolio) <= limits["max_holdings"]
    assert (weights == weights.astype(int)).all() and weights.max() <= limits["max_position_pct"]
    assert 100 - weights.sum() >= limits["min_cash_pct"]
    assert portfolio.groupby("sector")["weight_pct"].sum().max() <= limits["max_sector_pct"]


def test_alpha_v1_stays_fully_invested_with_eight_names(alpha_v1):
    final = add_final_score(scored_universe(), settings.UNIVERSE)
    portfolio, _, _ = build_competition_portfolio(final, 5)
    assert len(portfolio) == 8 and portfolio["weight_pct"].sum() == 95


def test_fewer_names_means_more_cash(alpha_v1):
    final = add_final_score(scored_universe(), settings.UNIVERSE)
    keep = list(final.index[:30])
    # Keep names in distinct sectors so the sector limit does not also bind.
    seen, picks = set(), []
    for ticker in keep:
        if settings.UNIVERSE[ticker] not in seen:
            seen.add(settings.UNIVERSE[ticker])
            picks.append(ticker)
    picks = picks[:5]
    portfolio, _, failures = build_competition_portfolio(final, 5, {t: "x" for t in final.index if t not in picks})
    assert len(portfolio) == 5 and portfolio["weight_pct"].sum() == 60 and not failures


def test_fewer_than_five_stocks_is_a_class_rule_risk(alpha_v1):
    final = add_final_score(scored_universe(), settings.UNIVERSE)
    keep = set(final.index[:3])
    _, _, failures = build_competition_portfolio(final, 5, {t: "x" for t in final.index if t not in keep})
    assert any("CLASS RULE RISK" in f for f in failures)


# --- banned products -------------------------------------------------------------
@pytest.mark.parametrize("ticker,name,quote", [
    ("TQQQ", None, None),
    ("SQQQ", None, None),
    ("IBIT", None, None),
    ("BTC-USD", None, None),
    ("XYZ", None, "CRYPTOCURRENCY"),
    ("XYZ", None, "OPTION"),
    ("AAPL261218C00200000", None, None),
    ("NEWF", "Fictional 2x Daily Long ETF", "ETF"),
    ("NEWG", "Fictional UltraPro Fund", "ETF"),
    ("NEWH", "Fictional -1x Short Fund", "ETF"),
    ("NEWI", "Fictional Inverse Treasury ETF", "ETF"),
    ("NEWJ", "Fictional Bear 3X Shares", None),
    ("NEWK", "Fictional Bitcoin Trust", "ETF"),
])
def test_banned_products_detected(ticker, name, quote):
    assert banned_reason(ticker, name, quote)


@pytest.mark.parametrize("ticker,name,quote", [
    ("AAPL", "Apple Inc.", "EQUITY"),
    ("UCTT", "Ultra Clean Holdings, Inc.", "EQUITY"),
    ("BULL", "Fictional Bull Mining Corp", "EQUITY"),
    ("SPY", "SPDR S&P 500 ETF Trust", "ETF"),
    ("BRK-B", "Berkshire Hathaway", "EQUITY"),
])
def test_ordinary_products_are_not_banned(ticker, name, quote):
    assert banned_reason(ticker, name, quote) is None


def test_etfs_do_not_count_as_stocks():
    assert is_etf("SPY") and not counts_as_stock("SPY")
    assert is_etf("ANYX", "ETF") and not counts_as_stock("ANYX", quote_type="ETF")
    assert counts_as_stock("AAPL") and not counts_as_stock("TQQQ")


# --- validator groups ------------------------------------------------------------
def test_alpha_v1_allocation_passes(alpha_v1):
    alloc = rows(*[(t, 12) for t in TEN[:7]], (TEN[7], 11), ("CASH", 5))
    assert not failed(validate(alloc))


def test_too_few_stocks_is_a_class_rule_failure(alpha_v1):
    checks = validate(rows(("AAPL", 12), ("JPM", 12), ("JNJ", 12), ("XOM", 12), ("CASH", 52)))
    assert "At least 5 stocks (ETFs do not count)" in failed(checks, CLASS_RULES)


def test_spy_does_not_rescue_the_stock_minimum(alpha_v1, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_ETF_HOLDINGS", True)
    alloc = rows(("AAPL", 12), ("JPM", 12), ("JNJ", 12), ("XOM", 12), ("SPY", 12), ("CASH", 40))
    assert "At least 5 stocks (ETFs do not count)" in failed(validate(alloc), CLASS_RULES)
    assert "No ETF positions (ALLOW_ETF_HOLDINGS)" not in failed(validate(alloc))


def test_etf_holding_is_a_strategy_failure_not_a_class_failure(alpha_v1):
    alloc = rows(*[(t, 10) for t in TEN[:6]], ("SPY", 10), ("CASH", 30))
    assert "No ETF positions (ALLOW_ETF_HOLDINGS)" in failed(validate(alloc), STRATEGY_LIMITS)
    assert not failed(validate(alloc), CLASS_RULES)


def test_banned_product_is_a_class_rule_failure(alpha_v1):
    alloc = rows(*[(t, 12) for t in TEN[:5]], ("TQQQ", 12), ("CASH", 28))
    assert "Equities and ETFs only" in failed(validate(alloc), CLASS_RULES)


def test_position_limit_is_a_strategy_failure(alpha_v1):
    alloc = rows(("AAPL", 13), *[(t, 10) for t in TEN[1:7]], ("CASH", 27))
    assert "Each stock at most 12%" in failed(validate(alloc), STRATEGY_LIMITS)
    assert not failed(validate(alloc), CLASS_RULES)


def test_output_prints_both_groups(tmp_path, capsys, alpha_v1):
    from src.validate_allocation import main
    path = tmp_path / "r.txt"
    path.write_text("Symbol,Weight\nAAPL,50\nCASH,50\n")
    assert main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "Class rules (a failure is a grade risk)" in out and "Strategy limits" in out


def test_prompt_separates_rules_from_limits(monkeypatch):
    from datetime import date
    from src.build_weight_prompt import build_instructions
    from src.untrusted import UntrustedBlocks
    monkeypatch.setattr(settings, "PORTFOLIO_LIMITS", "alpha_v1")
    text = build_instructions(date(2030, 1, 15), UntrustedBlocks("T"))
    assert settings.OBJECTIVE_STATEMENT in text
    assert "Class rules (from the assignment" in text and 'Strategy limits (profile "alpha_v1"' in text
    assert "Maximum individual stock weight: 12%" in text and "Minimum cash allocation: 5%" in text
