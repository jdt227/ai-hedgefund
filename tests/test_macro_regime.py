import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from config import settings
from src.macro import (FED_SOURCE, Indicator, cash_target, classify_regime, cpi_yoy_series, market_proxies,
                       parse_fed_rss, parse_fred_observations, sector_tilts, select_macro_headlines)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2030, 1, 15, 21, 0, tzinfo=timezone.utc)


def ind(value=None, change_3m=None):
    return Indicator("x", "X", "FRED", value, "2030-01-01", None, change_3m, "2030-01-15T00:00:00+00:00")


def inputs(claims=0.0, unemployment=0.0, cpi=3.0, spread=4.0, vix=20.0, curve=0.5):
    return {"initial_claims": ind(200_000, claims), "unemployment": ind(4.0, unemployment),
            "cpi_yoy": ind(cpi, 0.0), "high_yield_spread": ind(spread), "vix": ind(vix),
            "curve_10y_2y": ind(curve)}


def test_risk_on_when_conditions_are_good():
    label, score, dims = classify_regime(inputs(claims=-6, cpi=2.0, spread=3.0, vix=15), 0.05)
    assert label == "risk_on" and score == 4
    assert all(d["reason"] for d in dims.values())


def test_risk_off_when_conditions_are_bad():
    label, score, _ = classify_regime(inputs(claims=15, cpi=4.5, spread=6.0, vix=35), -0.05)
    assert label == "risk_off" and score == -4


def test_neutral_when_mixed():
    label, score, _ = classify_regime(inputs(), 0.05)
    assert label == "neutral" and score == 1


def test_inverted_curve_blocks_easy_conditions():
    _, _, dims = classify_regime(inputs(spread=3.0, vix=15, curve=-0.2), 0.05)
    assert dims["financial_conditions"]["score"] == 0


def test_missing_inputs_score_zero_with_a_note():
    label, score, dims = classify_regime({}, None)
    assert label == "neutral" and score == 0
    assert all("no data" in d["reason"] for d in dims.values())


@pytest.mark.parametrize("regime", [None, "risk_on", "neutral", "risk_off"])
def test_cash_never_below_minimum(regime):
    assert cash_target(regime) >= settings.MIN_CASH_PCT


def test_cash_minimum_holds_even_if_config_is_lower(monkeypatch):
    monkeypatch.setitem(settings.REGIME_CASH_TARGET_PCT, "risk_on", 5)
    assert cash_target("risk_on") == settings.MIN_CASH_PCT


@pytest.mark.parametrize("regime", ["risk_on", "neutral", "risk_off"])
def test_tilts_are_bounded_and_cover_every_universe_sector(regime):
    returns = {sector: i / 10 for i, sector in enumerate(settings.SECTOR_ETFS)}
    tilts = sector_tilts(regime, returns)
    assert set(settings.UNIVERSE.values()) <= set(tilts)
    assert all(abs(t) <= settings.SECTOR_TILT_LIMIT for t in tilts.values())


def test_risk_off_favors_defensive_over_cyclical():
    tilts = sector_tilts("risk_off", {})
    assert tilts["Utilities"] > 0 > tilts["Technology"]


def test_relative_strength_orders_tilts():
    returns = {sector: 0.0 for sector in settings.SECTOR_ETFS}
    returns["Energy"], returns["Technology"] = 0.2, -0.2
    tilts = sector_tilts("neutral", returns)
    assert tilts["Energy"] == pytest.approx(settings.SECTOR_RS_TILT_RANGE)
    assert tilts["Technology"] == pytest.approx(-settings.SECTOR_RS_TILT_RANGE)


def test_fred_parser_skips_missing_and_future_rows():
    payload = json.loads((FIXTURES / "fred_dgs10.json").read_text())
    series = parse_fred_observations(payload, date(2030, 1, 15))
    assert series.iloc[-1] == 4.50
    assert series.index[-1] == pd.Timestamp("2030-01-14")
    assert 9.99 not in series.values and len(series) == 3


def test_cpi_yoy_is_percent_change_over_12_months():
    idx = pd.date_range("2028-01-01", periods=13, freq="MS")
    yoy = cpi_yoy_series(pd.Series(np.linspace(100, 103, 13), index=idx))
    assert yoy.iloc[-1] == pytest.approx(3.0)


def test_fed_rss_drops_future_items():
    items = parse_fed_rss((FIXTURES / "fed_press.xml").read_text(), NOW, "2030-01-15")
    assert [i["headline"] for i in items] == ["Fictional FOMC statement"]


def test_market_proxies_spy_gap_and_etf_returns():
    idx = pd.bdate_range("2028-01-03", periods=260)
    prices = pd.DataFrame({"SPY": np.linspace(100, 200, 260), "XLK": np.linspace(100, 110, 260)}, index=idx)
    gap, day, returns = market_proxies(prices)
    assert gap > 0 and day == str(idx[-1].date())
    assert returns["Technology"] > 0 and "Energy" not in returns


def test_cpi_yoy_compares_calendar_months_across_a_gap():
    """A missing month must not make the comparison span 13 months."""
    idx = pd.date_range("2024-01-01", periods=25, freq="MS")
    levels = pd.Series(np.linspace(100, 124, 25), index=idx).drop(pd.Timestamp("2024-11-01"))
    yoy = cpi_yoy_series(levels)
    jan_2026 = levels[pd.Timestamp("2026-01-01")] / levels[pd.Timestamp("2025-01-01")] - 1
    assert yoy[pd.Timestamp("2026-01-01")] == pytest.approx(jan_2026 * 100)
    assert pd.Timestamp("2025-11-01") not in yoy.index


def headline(source, text, day="2030-01-14"):
    return {"source": source, "headline": text, "published_at": day, "url": "", "retrieved_at": "r"}


def test_macro_headlines_keep_fed_and_drop_noise():
    fed = headline(FED_SOURCE, "Federal Reserve issues FOMC statement", "2030-01-08")
    items = [headline("CNBC", "Team owner says no rush on backup QB"),
             headline("CNBC", "Oil edges up as dollar drifts"),
             headline(FED_SOURCE, "Board announces approval of bank application"),
             fed]
    assert select_macro_headlines(items, 8) == [fed, items[1]]


def test_keyword_match_uses_whole_words():
    assert select_macro_headlines([headline("CNBC", "Fedora sales boil over")], 8) == []
