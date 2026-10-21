"""Live smoke tests (network). Run with:  RUN_LIVE=1 pytest -m live"""

import os
from datetime import datetime, timezone

import pytest

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to run")]


def test_yfinance_returns_completed_bars(tmp_path):
    from src.market_data.yfinance_provider import YFinanceProvider

    bundle = YFinanceProvider(period="3mo", cache_dir=tmp_path).fetch(["AAPL", "SPY"], datetime.now(timezone.utc))
    assert not bundle.is_fixture
    assert bundle.last_completed_session is not None
    assert bundle.staleness_sessions() <= 3
    assert bundle.info.loc["SPY", "quote_type"] == "ETF"


def test_french_factor_download(tmp_path):
    from src.evaluation.fama_french import load_french_daily

    factors, meta = load_french_daily(cache_dir=tmp_path, refresh=True)
    assert list(factors.columns) == ["Mkt-RF", "SMB", "HML", "RF"]
    assert factors.abs().max().max() < 0.5   # decimal, not percent
