from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from config import settings
from src import run_pipeline
from src.features import add_standardized_scores, calculate_features
from src.portfolio import build_portfolio
from src.universe import game_symbol, get_universe, load_sp500, yahoo_symbol
from src.universe.eligibility import eligibility_table, extreme_move_flags
from src.universe.refresh import build_snapshot, diff_tickers
from test_weekly_pipeline import NOW, RUN, isolated, universe_prices  # noqa: F401


@pytest.fixture
def small_sp500(monkeypatch):
    """A fictional 12-stock S&P mode, offline."""
    tickers = [f"XMP{c}" for c in "ABCDEFGHIJKL"]
    sectors = ["Technology", "Energy", "Financials", "Utilities"]
    universe = {t: sectors[i % 4] for i, t in enumerate(tickers)}
    monkeypatch.setattr(settings, "UNIVERSE_MODE", "sp500")
    monkeypatch.setattr(settings, "PORTFOLIO_LIMITS", "alpha_v1")
    monkeypatch.setattr(run_pipeline, "get_universe", lambda: universe)
    return universe


def market(tickers, sessions=300):
    idx = pd.bdate_range(end="2030-01-15", periods=sessions, name="date")
    rng = np.random.default_rng(5)
    returns = rng.normal(0.0005, 0.012, (sessions, len(tickers)))
    prices = pd.DataFrame(100 * np.exp(np.cumsum(returns, axis=0)), index=idx, columns=tickers)
    volume = pd.DataFrame(5_000_000.0, index=idx, columns=tickers)
    return prices, prices.copy(), volume


# --- snapshot ------------------------------------------------------------------
def test_committed_snapshot_loads_with_share_classes_and_all_sectors():
    frame = load_sp500()
    row = frame.set_index("ticker").loc["BRK.B"]
    assert row["yahoo_symbol"] == "BRK-B" and row["game_symbol"] == "BRK.B"
    assert len(frame) > 490 and frame["as_of"].str.match(r"\d{4}-\d{2}-\d{2}").all()
    assert set(frame["sector"]) == set(settings.SECTOR_ETFS)


def test_symbol_mapping_round_trip():
    assert yahoo_symbol("BRK.B") == "BRK-B" and game_symbol("BF-B") == "BF.B"
    assert yahoo_symbol("AAPL") == "AAPL"


def test_mode_switch(monkeypatch):
    assert get_universe() == settings.UNIVERSE
    monkeypatch.setattr(settings, "UNIVERSE_MODE", "sp500")
    assert "BRK.B" in get_universe() and len(get_universe()) > 490
    monkeypatch.setattr(settings, "UNIVERSE_MODE", "nope")
    with pytest.raises(ValueError):
        get_universe()


def test_refresh_maps_gics_and_rejects_unknown_sectors():
    raw = pd.DataFrame({"Symbol": ["BRK.B", "AAPL"], "Security": ["Berkshire", "Apple"],
                        "GICS Sector": ["Financials", "Information Technology"]})
    snap = build_snapshot(raw, date(2030, 1, 1), "src")
    assert snap.set_index("ticker").at["AAPL", "sector"] == "Technology"
    assert snap.set_index("ticker").at["BRK.B", "yahoo_symbol"] == "BRK-B"
    raw.loc[0, "GICS Sector"] = "Martian Mining"
    with pytest.raises(ValueError):
        build_snapshot(raw, date(2030, 1, 1), "src")


def test_refresh_reports_adds_and_removes():
    assert diff_tickers({"A", "B"}, {"B", "C"}) == (["C"], ["A"])


def test_refresh_does_not_write_without_apply(monkeypatch, tmp_path, capsys):
    from src import universe as u
    from src.http_utils import FetchResult
    from src.universe import refresh

    target = tmp_path / "u.csv"
    monkeypatch.setattr(refresh, "SP500_PATH", target)
    csv = "Symbol,Security,GICS Sector\nAAPL,Apple,Information Technology\n"
    monkeypatch.setattr(refresh, "fetch", lambda *a, **k: FetchResult("ok", csv, "r"))
    monkeypatch.setattr(refresh, "cross_check", lambda *a: "skipped")
    assert refresh.main([]) == 0
    assert not target.exists() and "Nothing written" in capsys.readouterr().out
    assert refresh.main(["--apply"]) == 0
    assert target.exists() and u.load_sp500(target)["ticker"].tolist() == ["AAPL"]


# --- eligibility -----------------------------------------------------------------
def test_eligibility_filters_give_reasons():
    tickers = ["CHEAP", "THIN", "NEW", "OK"]
    prices, close, volume = market(tickers)
    close["CHEAP"] = 3.0
    volume["THIN"] = 10.0
    prices.loc[prices.index[:-100], "NEW"] = np.nan
    table = eligibility_table({t: "s" for t in tickers + ["GONE"]}, prices, close, volume)
    assert table.at["OK", "eligible"]
    assert "price below" in table.at["CHEAP", "reason"]
    assert "dollar volume" in table.at["THIN", "reason"]
    assert "history" in table.at["NEW", "reason"]
    assert not table.at["GONE", "eligible"]


def test_extreme_moves_are_flagged():
    prices, _, _ = market(["XMPA", "XMPB"])
    prices.iloc[-5:, 0] = prices.iloc[-5:, 0] * 2
    flags = extreme_move_flags(prices, 0.4)
    assert "XMPA" in flags and "XMPB" not in flags


# --- z-score cap -----------------------------------------------------------------
def test_cap_limits_outliers_and_reference_sets_the_scale():
    prices, _, _ = market([f"T{i}" for i in range(30)])
    prices["T0"] = prices["T0"] * np.exp(np.linspace(0, 4, len(prices)))
    features = calculate_features(prices)
    capped = add_standardized_scores(features, cap=3.0)
    assert capped["long_momentum_z"].abs().max() <= 3.0
    assert add_standardized_scores(features)["long_momentum_z"].abs().max() > 3.0
    others = features.index.drop("T0")
    scaled = add_standardized_scores(features, reference=others)
    unscaled = add_standardized_scores(features.loc[others])
    assert scaled.at["T1", "long_momentum_z"] == pytest.approx(unscaled.at["T1", "long_momentum_z"])


# --- funnel and invariant --------------------------------------------------------
def test_holdings_are_always_in_the_funnel(small_sp500, monkeypatch):
    monkeypatch.setattr(settings, "FUNNEL_DEEP_N", 3)
    prices, close, volume = market(list(small_sp500))
    scored, table = run_pipeline.score_universe(prices, close, volume, small_sp500)
    worst = scored.index[-1]
    deep = run_pipeline.deep_funnel(scored, table, {}, small_sp500, [worst, "SPY"])
    assert deep[:3] == list(scored.index[:3]) and worst in deep and "SPY" in deep and len(deep) == 5


def test_outside_funnel_stocks_are_not_penalized(small_sp500, monkeypatch, make_account, tmp_path):
    monkeypatch.setattr(settings, "FUNNEL_DEEP_N", 3)
    prices, close, volume = market(list(small_sp500))
    fake_sent = pd.DataFrame({"score": [0.5], "vader_shrunk": [0.5], "av_score": [None], "article_count": [9],
                              "sentiment_z": [1.0]}, index=pd.Index(["XMPA"], name="ticker"))

    class News:
        sentiment = fake_sent
        articles = pd.DataFrame(columns=["ticker"])
        source_status = {}

    monkeypatch.setattr(run_pipeline, "run_news", lambda *a, **k: News)
    evidence = run_pipeline.build_run(prices, RUN, NOW, close, volume, tmp_path / "none")
    outside = [t for t in evidence.final_scores.index if t not in evidence.deep_tickers and t != "XMPA"]
    assert outside
    assert (evidence.final_scores.loc[outside, "sentiment_contribution"] == 0).all()
    assert (evidence.final_scores.loc[outside, "final_score"]
            == evidence.final_scores.loc[outside, "composite_score"]).all()


def test_outside_funnel_candidate_is_labeled_in_the_prompt(small_sp500, monkeypatch, tmp_path):
    from src.build_weight_prompt import firm_section
    from src.untrusted import UntrustedBlocks

    monkeypatch.setattr(settings, "FUNNEL_DEEP_N", 2)
    prices, close, volume = market(list(small_sp500))
    evidence = run_pipeline.build_run(prices, RUN, NOW, close, volume, tmp_path / "none")
    text = firm_section(evidence, UntrustedBlocks("T"))
    assert "not fetched (outside funnel)" in text and "not a negative signal" in text


def test_ineligible_stocks_are_never_selected(small_sp500, tmp_path):
    prices, close, volume = market(list(small_sp500))
    best = add_standardized_scores(calculate_features(prices)).index[0]
    close[best] = 1.0
    evidence = run_pipeline.build_run(prices, RUN, NOW, close, volume, tmp_path / "none")
    assert best not in set(evidence.portfolio["ticker"]) and best in evidence.excluded.index


def test_holding_outside_universe_is_always_analyzed(small_sp500, make_account):
    prices, close, volume = market(list(small_sp500) + ["ZZZZ"])
    account = make_account(metadata={}, holdings_rows=["ZZZZ,10,90,100,1000,100,STOCK"])
    evidence = run_pipeline.build_run(prices, RUN, NOW, close, volume, account)
    assert "ZZZZ" in evidence.final_scores.index and "ZZZZ" in evidence.deep_tickers
    assert evidence.review.set_index("ticker").at["ZZZZ", "rank"] > 0


def test_legacy_invariant_matches_original_model(monkeypatch, tmp_path):
    for name in ("USE_MACRO_REGIME", "USE_NEWS_SENTIMENT", "USE_CATALYST_FLAGS", "USE_SCOUT_OVERLAY", "USE_POLITICIAN_OVERLAY"):
        monkeypatch.setattr(settings, name, False)
    assert run_pipeline.uses_original_model()
    prices = universe_prices()
    evidence = run_pipeline.build_run(prices, RUN, NOW, account_dir=tmp_path / "none")
    old = build_portfolio(add_standardized_scores(calculate_features(prices)))
    pd.testing.assert_frame_equal(evidence.portfolio.drop(columns="weight_pct"), old)
    monkeypatch.setattr(settings, "UNIVERSE_MODE", "sp500")
    assert not run_pipeline.uses_original_model()
