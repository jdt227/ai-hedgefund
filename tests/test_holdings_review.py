import numpy as np
import pandas as pd
import pytest

from config import settings
from src.holdings_review import HoldingsInputs, incumbents_from_review, rank_stocks, review_holdings
from src.portfolio import build_competition_portfolio
from src.validate_allocation import HOLDINGS_ACTIONS, CLASS_RULES, extract_holdings_actions, run

SECTORS = ["Technology", "Energy", "Financials", "Utilities", "Health Care", "Materials", "Industrials", "Real Estate"]


@pytest.fixture(autouse=True)
def alpha_profile(monkeypatch):
    monkeypatch.setattr(settings, "PORTFOLIO_LIMITS", "alpha_v1")


def tickers(n=120):
    return [f"T{i:03d}" for i in range(1, n + 1)]


def scores(n=120):
    """T001 ranks 1, T002 ranks 2, and so on."""
    names = tickers(n)
    return pd.DataFrame({"final_score": np.linspace(5, -5, n), "composite_score": np.linspace(5, -5, n),
                         "price": 100.0, "long_momentum": 0.1, "short_momentum": 0.01,
                         "annualized_volatility": 0.2}, index=pd.Index(names, name="ticker"))


def universe(n=120):
    return {t: SECTORS[i % len(SECTORS)] for i, t in enumerate(tickers(n))}


def inputs(holdings, cash=100_000.0, **kw):
    """holdings: {ticker: dollar value at a $100 price}."""
    names = list(holdings)
    idx = pd.bdate_range(end="2030-01-15", periods=3)
    prices = pd.DataFrame({t: 100.0 for t in names}, index=idx)
    positions = pd.DataFrame({"ticker": names, "shares": [holdings[t] / 100 for t in names]})
    final = scores()
    for t in names:
        if t not in final.index:
            final.loc[t] = final.iloc[0]
    return HoldingsInputs(positions=positions, final_scores=final, eligible=set(tickers()), universe=universe(),
                          prices=prices, cash=cash, **kw)


def action(frame, ticker):
    return frame.set_index("ticker").at[ticker, "action"]


def row(frame, ticker):
    return frame.set_index("ticker").loc[ticker]


# --- SELL ---------------------------------------------------------------------
def test_rank_worse_than_sell_line_sells():
    frame, _ = review_holdings(inputs({"T076": 5000}))
    assert action(frame, "T076") == "SELL" and "sell line of 75" in row(frame, "T076")["reasons"]
    assert row(frame, "T076")["target_weight_pct"] == 0


def test_rank_at_sell_line_holds():
    frame, _ = review_holdings(inputs({"T075": 5000}))
    assert action(frame, "T075") == "HOLD" and row(frame, "T075")["action_label"] == "HOLD (watch)"


def test_etf_sells_with_alpha_reason_unless_allowed(monkeypatch):
    frame, _ = review_holdings(inputs({"SPY": 5000}))
    assert action(frame, "SPY") == "SELL" and "near-zero FF3 alpha" in row(frame, "SPY")["reasons"]
    monkeypatch.setattr(settings, "ALLOW_ETF_HOLDINGS", True)
    assert action(review_holdings(inputs({"SPY": 5000}))[0], "SPY") == "HOLD"


def test_banned_product_sells():
    frame, _ = review_holdings(inputs({"TQQQ": 5000}))
    assert action(frame, "TQQQ") == "SELL" and row(frame, "TQQQ")["reasons"].startswith("banned by class rules")


def test_hard_veto_risk_veto_and_ineligible_sell():
    assert action(review_holdings(inputs({"T001": 5000}, vetoed={"T001": "bad 8-K"}))[0], "T001") == "SELL"
    assert "Risk Manager veto" in row(review_holdings(inputs({"T001": 5000}, risk_vetoes={"T001": "x"}))[0], "T001")["reasons"]
    assert "failed eligibility" in row(review_holdings(inputs({"T001": 5000}, ineligible_reasons={"T001": "thin"}))[0], "T001")["reasons"]


# --- TRIM ---------------------------------------------------------------------
def test_over_position_limit_trims_to_limit():
    frame, basis = review_holdings(inputs({"T001": 20000}, cash=80000))
    assert basis == "% of account"
    assert action(frame, "T001") == "TRIM" and row(frame, "T001")["target_weight_pct"] == 12


def test_small_excess_under_min_trade_is_ignored():
    frame, _ = review_holdings(inputs({"T001": 12500}, cash=87500))
    assert action(frame, "T001") == "HOLD"


def test_sector_over_limit_trims_lowest_ranked_first():
    # T001, T009, T017 are all Technology, at 15% / 12% / 12% before limits.
    holdings = {"T001": 12000, "T009": 12000, "T017": 12000, "T049": 12000}
    frame, _ = review_holdings(inputs(holdings, cash=52000))
    assert row(frame, "T049")["sector"] == "Technology"
    assert action(frame, "T049") == "TRIM" and action(frame, "T001") == "HOLD"
    kept = frame.set_index("ticker")
    total = sum(kept.at[t, "current_weight_pct"] if pd.isna(kept.at[t, "target_weight_pct"]) else kept.at[t, "target_weight_pct"]
                for t in holdings)
    assert total <= settings.limits()["max_sector_pct"]


def test_sold_holdings_do_not_count_toward_sector_limit():
    holdings = {"T001": 12000, "T009": 12000, "T017": 12000, "T105": 12000}  # T105 ranks beyond the sell line
    frame, _ = review_holdings(inputs(holdings, cash=52000))
    assert action(frame, "T105") == "SELL"
    # 36% remains after the sale, so only the lowest-ranked of the three is trimmed.
    assert action(frame, "T001") == "HOLD" and action(frame, "T009") == "HOLD"
    assert action(frame, "T017") == "TRIM" and row(frame, "T017")["target_weight_pct"] == 11


def test_risk_haircut_trims():
    frame, _ = review_holdings(inputs({"T001": 10000}, cash=90000, risk_haircuts={"T001": "correlated"}))
    assert action(frame, "T001") == "TRIM" and row(frame, "T001")["target_weight_pct"] == 9
    assert "Risk Manager haircut" in row(frame, "T001")["reasons"]


# --- HOLD and hysteresis ------------------------------------------------------
def test_hold_inside_buy_cutoff_is_plain_hold():
    frame, _ = review_holdings(inputs({"T010": 5000}))
    assert row(frame, "T010")["action_label"] == "HOLD"


def test_earnings_in_window_is_watch():
    flags = {"T001": [{"flag": "earnings_imminent", "reason": "soon"}]}
    frame, _ = review_holdings(inputs({"T001": 5000}, flags=flags))
    assert row(frame, "T001")["action_label"] == "HOLD (watch)" and "earnings" in row(frame, "T001")["reasons"]


def test_hysteresis_incumbent_stays_where_a_new_buy_would_not():
    final = scores()
    new_portfolio, _, _ = build_competition_portfolio(final, 5, sectors=universe(), incumbents=[])
    assert "T040" not in set(new_portfolio["ticker"])
    kept, _, _ = build_competition_portfolio(final, 5, sectors=universe(), incumbents=["T040"])
    assert "T040" in set(kept["ticker"])
    assert action(review_holdings(inputs({"T040": 5000}))[0], "T040") == "HOLD"


# --- cash unknown --------------------------------------------------------------
def test_cash_unknown_uses_invested_basis_and_skips_limits():
    frame, basis = review_holdings(inputs({"T001": 50000, "T002": 50000}, cash=None))
    assert "invested holdings" in basis and "not applied" in basis
    assert row(frame, "T001")["current_weight_pct"] == 50
    assert action(frame, "T001") == "HOLD"


def test_rank_ties_break_alphabetically():
    final = pd.DataFrame({"final_score": [1.0, 1.0, 2.0]}, index=["B", "A", "C"])
    assert rank_stocks(final, {"A", "B", "C"}) == {"C": 1, "A": 2, "B": 3}


# --- holdings-aware portfolio -----------------------------------------------------
def holdings_aware(incumbents, caps=None, cash=5):
    return build_competition_portfolio(scores(), cash, sectors=universe(), incumbents=incumbents, weight_caps=caps)


def assert_limits(portfolio):
    limits = settings.limits()
    weights = portfolio["weight_pct"]
    assert (weights == weights.astype(int)).all() and weights.max() <= limits["max_position_pct"]
    assert 100 - weights.sum() >= limits["min_cash_pct"]
    assert portfolio.groupby("sector")["weight_pct"].sum().max() <= limits["max_sector_pct"]
    assert len(portfolio) <= limits["max_holdings"]


def test_incumbents_come_first_then_best_new_names():
    portfolio, _, failures = holdings_aware(["T050", "T060"])
    assert portfolio["ticker"].tolist()[:2] == ["T050", "T060"]
    assert {"T001", "T002"} <= set(portfolio["ticker"]) and not failures
    assert_limits(portfolio)


def test_new_names_must_be_within_buy_cutoff():
    portfolio, _, _ = holdings_aware([])
    ranks = [int(t[1:]) for t in portfolio["ticker"]]
    assert max(ranks) <= settings.BUY_RANK_CUTOFF


def test_trim_cap_is_respected_and_leaves_cash():
    portfolio, _, _ = holdings_aware(["T001"], {"T001": 4})
    assert portfolio.set_index("ticker").at["T001", "weight_pct"] == 4
    assert 100 - portfolio["weight_pct"].sum() > 5
    assert_limits(portfolio)


def test_more_incumbents_than_needed_keep_within_max_holdings():
    names = [f"T{i:03d}" for i in range(1, 13)]
    portfolio, _, _ = holdings_aware(names)
    assert len(portfolio) == settings.limits()["max_holdings"]
    assert_limits(portfolio)


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("cash", [5, 15, 25])
def test_holdings_aware_limits_hold(seed, cash):
    rng = np.random.default_rng(seed)
    picks = rng.choice(tickers(), size=int(rng.integers(0, 12)), replace=False).tolist()
    portfolio, _, _ = holdings_aware(picks, cash=cash)
    assert_limits(portfolio)


def test_fewer_eligible_new_names_mean_more_cash(monkeypatch):
    monkeypatch.setattr(settings, "BUY_RANK_CUTOFF", 5)
    portfolio, _, _ = holdings_aware([])
    assert len(portfolio) == 5 and portfolio["weight_pct"].sum() == 60


def test_incumbents_from_review_orders_and_caps():
    frame, _ = review_holdings(inputs({"T010": 20000, "T002": 5000, "SPY": 5000}, cash=70000))
    kept, caps = incumbents_from_review(frame)
    assert kept == ["T002", "T010"] and caps == {"T010": 12}


# --- validator: Holdings actions ------------------------------------------------
GOOD_ALLOC = ["T001,12", "T002,12", "T003,12", "T004,12", "T005,12", "T006,12", "T007,12", "T008,11", "CASH,5"]


def write_run(tmp_path, actions, alloc=GOOD_ALLOC, review=None):
    pd.DataFrame(review or [
        {"ticker": "T001", "sector": "Technology", "action": "HOLD", "current_weight_pct": 10.0, "reasons": "ok"},
        {"ticker": "T002", "sector": "Energy", "action": "TRIM", "current_weight_pct": 15.0, "reasons": "over"},
        {"ticker": "SPY", "sector": "Other", "action": "SELL", "current_weight_pct": 8.0, "reasons": "etf"},
    ]).to_csv(tmp_path / "holdings_review.csv", index=False)
    text = ("## Holdings actions\nSymbol,Action,TargetWeight,Reason\n" + "\n".join(actions)
            + "\n\nCSV\nSymbol,Weight\n" + "\n".join(alloc) + "\nCSV\n")
    path = tmp_path / "recommendation_reason.txt"
    path.write_text(text)
    return path


GOOD_ACTIONS = ["T001,HOLD,12,still strong, with a comma", "T002,TRIM,12,over limit", "SPY,SELL,0,etf"]


def failed(checks, group=HOLDINGS_ACTIONS):
    return {c.name for c in checks if not c.passed and c.group == group}


@pytest.fixture
def fake_universe(monkeypatch):
    monkeypatch.setattr("src.validate_allocation.get_universe", universe)


def test_actions_pass_and_commas_survive_in_reason(tmp_path, fake_universe):
    path = write_run(tmp_path, GOOD_ACTIONS)
    passed, checks = run(path)
    assert passed, [c for c in checks if not c.passed]
    assert extract_holdings_actions(path.read_text())["T001"][2] == "still strong, with a comma"


@pytest.mark.parametrize("actions,rule", [
    (GOOD_ACTIONS[1:], "Every holding listed"),
    (GOOD_ACTIONS + ["ZZZ,HOLD,1,x"], "No unknown holdings listed"),
    (["T001,SELL,0,x"] + GOOD_ACTIONS[1:], "SELL is absent from the allocation"),
    (GOOD_ACTIONS[:2] + ["SPY,HOLD,5,x"], "HOLD is present in the allocation"),
    (GOOD_ACTIONS[:1] + ["T002,TRIM,12,x", "SPY,SELL,0,x"], None),
    (["T001,HOLD,11,x"] + GOOD_ACTIONS[1:], "Target weights match the allocation"),
])
def test_broken_actions_rejected(tmp_path, fake_universe, actions, rule):
    passed, checks = run(write_run(tmp_path, actions))
    if rule is None:
        assert passed
    else:
        assert not passed and rule in failed(checks)
        assert not (tmp_path / "final_allocation.csv").exists()


def test_trim_must_lower_the_weight(tmp_path, fake_universe):
    review = [{"ticker": "T002", "sector": "Energy", "action": "TRIM", "current_weight_pct": 10.0, "reasons": "over"}]
    passed, checks = run(write_run(tmp_path, ["T002,TRIM,12,x"], review=review))
    assert not passed and "TRIM has a lower weight than today" in failed(checks)


def test_banned_holding_kept_is_a_class_rule_failure(tmp_path, fake_universe):
    review = [{"ticker": "T001", "sector": "Technology", "action": "SELL", "current_weight_pct": 5.0,
               "reasons": "banned by class rules: test"}]
    _, checks = run(write_run(tmp_path, ["T001,HOLD,12,x"], review=review))
    assert "Banned holdings are sold" in failed(checks, CLASS_RULES)


def test_no_review_file_skips_the_group(tmp_path, fake_universe):
    path = tmp_path / "r.txt"
    path.write_text("Symbol,Weight\n" + "\n".join(GOOD_ALLOC) + "\n")
    passed, checks = run(path)
    assert passed and not [c for c in checks if c.group == HOLDINGS_ACTIONS]


def test_full_pipeline_writes_review_and_prompt_lines(make_account, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from src import run_pipeline
    from src.build_weight_prompt import build_weight_prompt
    from test_universe_funnel import market
    uni = universe()
    monkeypatch.setattr(settings, "UNIVERSE_MODE", "sp500")
    monkeypatch.setattr(run_pipeline, "get_universe", lambda: uni)
    for name in ("USE_MACRO_REGIME", "USE_NEWS_SENTIMENT", "USE_CATALYST_FLAGS", "USE_SCOUT_OVERLAY", "USE_POLITICIAN_OVERLAY"):
        monkeypatch.setattr(settings, name, False)
    prices, close, volume = market(list(uni) + ["SPY"])
    from src.features import add_standardized_scores, calculate_features
    best = add_standardized_scores(calculate_features(prices[list(uni)])).index[0]
    acct = make_account(metadata={"cash": 90000.0},
                        holdings_rows=[f"{best},10,90,100,1000,100,STOCK", "SPY,10,90,100,1000,100,ETF"])
    now = datetime(2030, 1, 15, 21, 0, tzinfo=timezone.utc)
    evidence = run_pipeline.build_run(prices, now.date(), now, close, volume, acct)
    assert action(evidence.review, "SPY") == "SELL"
    assert "SPY" not in set(evidence.portfolio["ticker"]) and best in set(evidence.portfolio["ticker"])
    text = build_weight_prompt(evidence, tmp_path).read_text()
    assert "SPY: 10 shares" in text and "suggested SELL" in text and "% of account" in text
    assert "CURRENT HOLDING" in text and "Symbol,Action,TargetWeight,Reason" in text
    assert "$" not in text.split("Current holdings and class tracking:")[1]
    assert (tmp_path / "holdings_review.csv").exists()


# --- a held stock whose ticker is CASH (Pathward Financial) -----------------------
CASH_REVIEW = [
    {"ticker": "T001", "sector": "Technology", "action": "HOLD", "current_weight_pct": 10.0, "reasons": "ok"},
    {"ticker": "CASH", "sector": "Financials", "action": "SELL", "current_weight_pct": 4.0,
     "reasons": "ticker CASH is the same as the allocation's CASH (cash balance) row"},
]


def test_holding_with_cash_ticker_is_always_sold():
    frame, _ = review_holdings(inputs({"T001": 5000, "CASH": 4000}))
    assert action(frame, "CASH") == "SELL"
    assert "cash balance" in row(frame, "CASH")["reasons"]
    assert action(frame, "T001") != "SELL"


def test_cash_balance_row_is_not_read_as_the_cash_stock(tmp_path, fake_universe):
    path = write_run(tmp_path, ["T001,HOLD,12,strong", "CASH,SELL,0,ticker collides with cash row"],
                     review=CASH_REVIEW)
    passed, checks = run(path)
    assert not failed(checks), failed(checks)
    assert passed


def test_keeping_the_cash_stock_fails(tmp_path, fake_universe):
    path = write_run(tmp_path, ["T001,HOLD,12,strong", "CASH,HOLD,5,keep it"], review=CASH_REVIEW)
    passed, checks = run(path)
    assert not passed
    assert "A held stock with ticker CASH is sold" in failed(checks)
