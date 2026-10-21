from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from config import strategy as cfg
from src.market_data.provider import (FixtureProvider, drop_incomplete_bars,
                                      expected_last_completed_session)
from src.strategy.orders import order_readiness, propose_orders
from src.strategy.review import top_candidates
from src.strategy.scoring import build_ranking
from src.strategy.signals import compute_signals

# Tue 2030-01-15 21:30 UTC = 16:30 New York (after the close + buffer)
AFTER_CLOSE = datetime(2030, 1, 15, 21, 31, tzinfo=timezone.utc)
DURING_SESSION = datetime(2030, 1, 15, 17, 0, tzinfo=timezone.utc)
TICKERS = [f"XMP{c}" for c in "ABCDEFGHIJ"]
UNIVERSE = {t: "Fixture Sector" for t in TICKERS}


def rank(bundle, universe=UNIVERSE, stale=frozenset()):
    sig = compute_signals(bundle.adj_close, bundle.close, bundle.volume, None, None)
    elig = dict(cfg.ELIGIBILITY, min_median_dollar_volume_63=1_000_000)
    return build_ranking(sig, bundle.info, universe, elig, cfg.SCORING, cfg.SIGNALS, set(), set(stale))


def test_expected_session_respects_close_and_weekends():
    assert expected_last_completed_session(AFTER_CLOSE) == date(2030, 1, 15)
    assert expected_last_completed_session(DURING_SESSION) == date(2030, 1, 14)
    sunday = datetime(2030, 1, 20, 15, 0, tzinfo=timezone.utc)
    assert expected_last_completed_session(sunday) == date(2030, 1, 18)


def test_incomplete_bar_dropped():
    idx = pd.to_datetime(["2030-01-14", "2030-01-15"])
    frame = pd.DataFrame({"XMPA": [1.0, 2.0]}, index=idx)
    out, dropped = drop_incomplete_bars({"adj_close": frame, "close": frame, "volume": frame}, DURING_SESSION, 30)
    assert list(out["adj_close"].index.date) == [date(2030, 1, 14)]
    assert dropped == ["2030-01-15"]


def test_stale_and_missing_data_visible():
    bundle = FixtureProvider(end=date(2030, 1, 10), missing={"XMPB"}).fetch(TICKERS, AFTER_CLOSE)
    assert bundle.staleness_sessions() == 3          # Jan 11, 14, 15 missing
    q = bundle.ticker_quality(TICKERS)
    assert q.at["XMPB", "stale_vs_market"] and q.at["XMPB", "error"]
    ranking = rank(bundle, stale={"XMPB"})
    assert not ranking.at["XMPB", "eligible"]
    assert "stale" in ranking.at["XMPB", "exclusion_reasons"]


def test_signals_formulas_on_known_series():
    idx = pd.bdate_range("2028-01-03", periods=300)
    prices = pd.DataFrame({"XMPA": 100 * 1.001 ** np.arange(300)}, index=idx)
    sig = compute_signals(prices, prices, prices * 0 + 1e6, None, None).loc["XMPA"]
    assert sig["mom_12_1"] == pytest.approx(1.001 ** 231 - 1)
    assert sig["mom_6_1"] == pytest.approx(1.001 ** 105 - 1)
    assert sig["vol_63"] == pytest.approx(0.0, abs=1e-9)
    assert sig["max_dd_252"] == pytest.approx(0.0)


def test_cutoff_excludes_future_data():
    bundle = FixtureProvider(end=date(2030, 1, 15)).fetch(TICKERS, AFTER_CLOSE)
    cutoff = pd.Timestamp("2030-01-08")
    sig = compute_signals(bundle.adj_close, bundle.close, bundle.volume, cutoff, None)
    assert sig["last_valid_date"].max() == "2030-01-08"


def test_scoring_is_deterministic_and_tie_broken_by_ticker():
    a = rank(FixtureProvider(end=date(2030, 1, 15)).fetch(TICKERS, AFTER_CLOSE))
    b = rank(FixtureProvider(end=date(2030, 1, 15)).fetch(TICKERS, AFTER_CLOSE))
    pd.testing.assert_frame_equal(a, b)
    weights_sum = sum(c["weight"] for c in cfg.SIGNALS.values())
    assert weights_sum == pytest.approx(1.0)
    # Identical series -> identical scores -> alphabetical order.
    idx = pd.bdate_range("2028-01-03", periods=300)
    p = pd.Series(100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, 300))), index=idx)
    prices = pd.DataFrame({"XMPZ": p, "XMPA": p, "XMPM": p})
    info = pd.DataFrame({"quote_type": "EQUITY", "name": "x", "sector": "s"}, index=prices.columns)
    sig = compute_signals(prices, prices, prices * 0 + 1e7, None, None)
    r = build_ranking(sig, info, {t: "s" for t in prices}, cfg.ELIGIBILITY, cfg.SCORING, cfg.SIGNALS, set(), set())
    assert list(r.index) == ["XMPA", "XMPM", "XMPZ"]


def test_missing_history_is_ineligible_not_penalized():
    bundle = FixtureProvider(end=date(2030, 1, 15), short_history={"XMPC": 100}).fetch(TICKERS, AFTER_CLOSE)
    r = rank(bundle)
    assert not r.at["XMPC", "eligible"]
    assert "insufficient history" in r.at["XMPC", "exclusion_reasons"]
    assert pd.isna(r.at["XMPC", "composite_score"])


def test_etf_quote_type_excluded():
    bundle = FixtureProvider(end=date(2030, 1, 15), quote_types={"XMPD": "ETF"}).fetch(TICKERS, AFTER_CLOSE)
    assert "quote type ETF" in rank(bundle).at["XMPD", "exclusion_reasons"]


def test_insufficient_candidates_reports_shortfall():
    tickers = TICKERS[:3]
    bundle = FixtureProvider(end=date(2030, 1, 15)).fetch(tickers, AFTER_CLOSE)
    r = rank(bundle, universe={t: "s" for t in tickers})
    top, shortfall = top_candidates(r, set(), pd.Series(dtype=float), "n/a", 0.35, cfg.SIGNALS, 5)
    assert len(top) == 3
    assert "Only 3" in shortfall


# --- order sizing -----------------------------------------------------------
def _review(rows):
    return pd.DataFrame(rows).set_index("ticker")


def test_orders_respect_cash_buffer_caps_and_whole_shares():
    bundle = FixtureProvider(end=date(2030, 1, 15)).fetch(TICKERS, AFTER_CLOSE)
    r = rank(bundle)
    held = r.index[r["eligible"]][-1]
    review = _review([{"ticker": held, "shares": 10, "marked_value": 10 * r.at[held, "reference_close"],
                       "latest_close": r.at[held, "reference_close"], "sector": "Fixture Sector",
                       "asset_type": "STOCK", "review_flag": "neutral"}])
    sizing = dict(cfg.SIZING, max_sector_weight=1.0)
    cash = 20_000.0
    p = propose_orders(review, r, cash, sizing, fee_per_trade=5.0, fractional=False, min_stocks=5, etf_counts=None)
    buys = p.actions[p.actions["action"] == "BUY"]
    assert len(buys) >= 1
    assert (buys["shares"] == buys["shares"].round()).all()
    equity = p.summary["equity_estimate"]
    assert p.summary["cash_after_estimate"] >= p.summary["cash_buffer"] - 1e-6
    assert (buys["est_value"] * (1 + sizing["slippage_buffer"]) <= sizing["max_position_weight"] * equity + 1e-6).all()
    assert "HOLD" in set(p.actions["action"])
    assert "SELL" not in set(p.actions["action"])


def test_sector_cap_rejects_buys():
    bundle = FixtureProvider(end=date(2030, 1, 15)).fetch(TICKERS, AFTER_CLOSE)
    r = rank(bundle)
    held = r.index[r["eligible"]][-1]
    price = r.at[held, "reference_close"]
    review = _review([{"ticker": held, "shares": 100, "marked_value": 100 * price, "latest_close": price,
                       "sector": "Fixture Sector", "asset_type": "STOCK", "review_flag": "neutral"}])
    p = propose_orders(review, r, 100 * price, dict(cfg.SIZING, max_sector_weight=0.5), 0.0, False, 5, None)
    assert "BUY" not in set(p.actions["action"])
    assert p.actions[p.actions["action"] == "REJECTED"]["reason"].str.contains("sector cap").all()


def test_orders_blocked_without_cash_or_game():
    class Meta:
        game_name = None
        cash = None
        holdings_complete = None
        ledger_complete_after_snapshot = None

    class Recon:
        current_cash_status = "unknown"

    fields = {b.field for b in order_readiness(Meta, Recon, market_is_fixture=True, market_stale=False,
                                                account_errors=0)}
    assert {"game_name", "cash", "holdings_complete", "ledger_complete_after_snapshot", "market_data"} <= fields
