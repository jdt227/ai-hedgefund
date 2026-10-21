"""Provider-neutral market-data bundle, completed-bar logic, and a fixture provider.

Price series kept separately:

* ``adj_close`` - split- and dividend-adjusted closes. Used ONLY for return
  research (momentum, volatility, drawdown, factor regressions).
* ``close``     - provider "Close": the traded closing price (Yahoo adjusts it
  for splits only). Used as the reference price for share sizing.
* ``volume``    - shares traded; dollar volume uses ``close``.

Only completed daily bars are kept: a session's bar is dropped if the
America/New_York clock has not passed 16:00 + a buffer on that date.
Exchange holidays are not modeled, so a missing weekday is reported as
"possibly stale or a market holiday" rather than silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)


def expected_last_completed_session(now_utc: datetime, close_buffer_minutes: int = 30) -> date:
    """Most recent weekday whose 16:00 ET close (+buffer) has passed."""
    now_ny = now_utc.astimezone(NY)
    cutoff = (datetime.combine(now_ny.date(), MARKET_CLOSE, NY)
              + timedelta(minutes=close_buffer_minutes))
    day = now_ny.date() if now_ny >= cutoff else now_ny.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def weekday_gap(later: date, earlier: date) -> int:
    """Number of weekdays after ``earlier`` up to and including ``later``."""
    if later <= earlier:
        return 0
    return int(np.busday_count(earlier + timedelta(days=1), later + timedelta(days=1)))


@dataclass
class MarketDataBundle:
    adj_close: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    info: pd.DataFrame                  # index ticker: name, sector, quote_type, ...
    provider: str
    fetched_at_utc: str
    expected_last_session: date
    is_fixture: bool = False
    dropped_incomplete_sessions: list[str] = field(default_factory=list)
    ticker_errors: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    @property
    def last_completed_session(self) -> date | None:
        valid = self.adj_close.dropna(how="all")
        return None if valid.empty else valid.index[-1].date()

    def staleness_sessions(self) -> int | None:
        last = self.last_completed_session
        return None if last is None else weekday_gap(self.expected_last_session, last)

    def ticker_quality(self, tickers: list[str]) -> pd.DataFrame:
        """Per-ticker coverage, last valid date, and staleness."""
        ref = self.last_completed_session
        rows = []
        for ticker in tickers:
            series = self.adj_close[ticker].dropna() if ticker in self.adj_close else pd.Series(dtype=float)
            last = series.index[-1].date() if len(series) else None
            ref_close = self.close[ticker].dropna() if ticker in self.close else pd.Series(dtype=float)
            info = self.info.loc[ticker] if ticker in self.info.index else None
            rows.append({
                "ticker": ticker,
                "history_sessions": int(len(series)),
                "first_date": str(series.index[0].date()) if len(series) else None,
                "last_valid_date": str(last) if last else None,
                "stale_vs_market": (last is None) or (ref is not None and last < ref),
                "reference_close_available": bool(len(ref_close)),
                "info_available": info is not None and pd.notna(info.get("quote_type")),
                "error": self.ticker_errors.get(ticker),
            })
        return pd.DataFrame(rows).set_index("ticker")

    def provenance(self) -> dict:
        return {
            "provider": self.provider,
            "is_fixture": self.is_fixture,
            "fetched_at_utc": self.fetched_at_utc,
            "from_cache": self.from_cache,
            "last_completed_session": str(self.last_completed_session),
            "expected_last_session": str(self.expected_last_session),
            "staleness_weekday_sessions": self.staleness_sessions(),
            "dropped_incomplete_sessions": self.dropped_incomplete_sessions,
            "ticker_errors": self.ticker_errors,
            "reference_price_meaning": "Provider close of the last completed session (16:00 America/New_York); "
                                       "split-adjusted only, not dividend-adjusted.",
        }


class MarketDataProvider(Protocol):
    name: str

    def fetch(self, tickers: list[str], now_utc: datetime) -> MarketDataBundle:
        ...


def drop_incomplete_bars(frames: dict[str, pd.DataFrame], now_utc: datetime,
                         close_buffer_minutes: int) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Remove bars for sessions that have not closed yet, and all-empty rows."""
    expected = expected_last_completed_session(now_utc, close_buffer_minutes)
    adj = frames["adj_close"]
    dates = pd.DatetimeIndex(adj.index).normalize()
    keep = dates.date <= expected
    dropped = sorted({str(d) for d in dates[~keep].date})
    out = {k: v.loc[keep] for k, v in frames.items()}
    nonempty = out["adj_close"].notna().any(axis=1)
    dropped += [str(d.date()) for d in out["adj_close"].index[~nonempty]]
    out = {k: v.loc[nonempty.to_numpy()] for k, v in out.items()}
    return out, dropped


class FixtureProvider:
    """Deterministic synthetic prices for tests and offline demos.

    Output is ALWAYS labeled as fixture data and must never be presented as a
    real recommendation.
    """

    name = "fixture"

    def __init__(self, sessions: int = 320, seed: int = 7, end: date | None = None,
                 drifts: dict[str, float] | None = None, quote_types: dict[str, str] | None = None,
                 missing: set[str] | None = None, short_history: dict[str, int] | None = None):
        self.sessions = sessions
        self.seed = seed
        self.end = end
        self.drifts = drifts or {}
        self.quote_types = quote_types or {}
        self.missing = missing or set()
        self.short_history = short_history or {}

    def fetch(self, tickers: list[str], now_utc: datetime) -> MarketDataBundle:
        expected = expected_last_completed_session(now_utc)
        end = self.end or expected
        index = pd.bdate_range(end=pd.Timestamp(end), periods=self.sessions, name="date")
        rng = np.random.default_rng(self.seed)
        adj, close, vol, info, errors = {}, {}, {}, {}, {}
        for n, ticker in enumerate(sorted(tickers)):
            if ticker in self.missing:
                errors[ticker] = "fixture: no data"
                continue
            drift = self.drifts.get(ticker, 0.0003 * ((n % 7) - 3))
            rets = rng.normal(drift, 0.015 + 0.002 * (n % 5), len(index))
            prices = 50.0 * (1 + n % 9) * np.exp(np.cumsum(rets))
            series = pd.Series(prices, index=index)
            if ticker in self.short_history:
                series.iloc[: len(index) - self.short_history[ticker]] = np.nan
            adj[ticker] = series
            close[ticker] = series * 1.01  # pretend dividends make adj < close
            vol[ticker] = pd.Series(2_000_000.0, index=index).where(series.notna())
            info[ticker] = {"name": f"{ticker} Fixture Corp", "sector": "Fixture Sector",
                            "quote_type": self.quote_types.get(ticker, "EQUITY"),
                            "info_source": "fixture"}
        return MarketDataBundle(
            adj_close=pd.DataFrame(adj, index=index), close=pd.DataFrame(close, index=index),
            volume=pd.DataFrame(vol, index=index),
            info=pd.DataFrame.from_dict(info, orient="index"),
            provider=self.name, fetched_at_utc=now_utc.astimezone(timezone.utc).isoformat(),
            expected_last_session=expected, is_fixture=True, ticker_errors=errors)
