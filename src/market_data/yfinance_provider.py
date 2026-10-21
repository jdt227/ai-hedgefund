"""yfinance adapter: one batched price download plus cached per-ticker metadata.

Requests are limited by downloading all tickers in a single batched call and
caching both prices and metadata under data/cache/ (git-ignored). Cached
prices are reused only while they are younger than ``cache_max_age_hours``
AND cover the same expected last session.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.market_data.provider import MarketDataBundle, drop_incomplete_bars, expected_last_completed_session

CACHE_DIR = Path("data/cache")
INFO_FIELDS = {
    "longName": "name", "shortName": "short_name", "sector": "sector", "industry": "industry",
    "quoteType": "quote_type", "exchange": "exchange", "currency": "currency",
    "marketCap": "market_cap_current",
}


class YFinanceProvider:
    name = "yfinance"

    def __init__(self, period: str = "2y", close_buffer_minutes: int = 30,
                 cache_dir: Path = CACHE_DIR, cache_max_age_hours: float = 12, refresh: bool = False):
        self.period = period
        self.close_buffer_minutes = close_buffer_minutes
        self.cache_dir = Path(cache_dir)
        self.cache_max_age_hours = cache_max_age_hours
        self.refresh = refresh

    # -- caching ---------------------------------------------------------
    def _cache_path(self, tickers: list[str], expected) -> Path:
        key = hashlib.sha256((",".join(sorted(tickers)) + self.period).encode()).hexdigest()[:10]
        return self.cache_dir / f"yf_prices_{expected}_{key}.pkl"

    def _load_cache(self, path: Path, now_utc: datetime):
        if self.refresh or not path.exists():
            return None
        with path.open("rb") as fh:
            cached = pickle.load(fh)
        age_h = (now_utc - datetime.fromisoformat(cached["fetched_at_utc"])).total_seconds() / 3600
        return cached if age_h <= self.cache_max_age_hours else None

    def _info(self, tickers: list[str], day) -> tuple[pd.DataFrame, dict[str, str]]:
        import yfinance as yf

        path = self.cache_dir / f"yf_info_{day}.json"
        cache = json.loads(path.read_text()) if path.exists() and not self.refresh else {}
        errors: dict[str, str] = {}
        for ticker in tickers:
            if ticker in cache:
                continue
            try:
                raw = yf.Ticker(ticker).info or {}
                cache[ticker] = {new: raw.get(old) for old, new in INFO_FIELDS.items()}
                cache[ticker]["info_source"] = f"yfinance Ticker.info fetched {day}"
            except Exception as exc:  # provider errors are reported, not fatal
                errors[ticker] = f"info: {type(exc).__name__}: {exc}"
            time.sleep(0.05)
        path.write_text(json.dumps(cache, indent=1, sort_keys=True))
        info = pd.DataFrame.from_dict({t: cache[t] for t in tickers if t in cache}, orient="index")
        return info, errors

    # -- main entry ------------------------------------------------------
    def fetch(self, tickers: list[str], now_utc: datetime | None = None) -> MarketDataBundle:
        import yfinance as yf

        now_utc = now_utc or datetime.now(timezone.utc)
        tickers = sorted(set(tickers))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        expected = expected_last_completed_session(now_utc, self.close_buffer_minutes)
        path = self._cache_path(tickers, expected)
        cached = self._load_cache(path, now_utc)
        from_cache = cached is not None

        if cached is None:
            raw = yf.download(tickers=tickers, period=self.period, interval="1d", auto_adjust=False,
                              actions=False, progress=False, group_by="column", threads=True)
            if raw is None or raw.empty:
                raise RuntimeError("yfinance returned no data (network failure or rate limit?).")
            frames = {}
            for field, key in (("Adj Close", "adj_close"), ("Close", "close"), ("Volume", "volume")):
                frame = raw[field] if isinstance(raw.columns, pd.MultiIndex) else raw[[field]]
                if not isinstance(raw.columns, pd.MultiIndex):
                    frame.columns = tickers
                frame = frame.reindex(columns=tickers).sort_index()
                frame.index = pd.DatetimeIndex(frame.index).tz_localize(None)
                frame.index.name = "date"
                frames[key] = frame.astype(float)
            cached = {"frames": frames, "fetched_at_utc": now_utc.isoformat()}
            with path.open("wb") as fh:
                pickle.dump(cached, fh)

        frames, dropped = drop_incomplete_bars(cached["frames"], now_utc, self.close_buffer_minutes)
        errors = {t: "price: no data returned" for t in tickers if frames["adj_close"][t].dropna().empty}
        info, info_errors = self._info(tickers, expected)
        for t, msg in info_errors.items():
            errors[t] = "; ".join(filter(None, [errors.get(t), msg]))
        return MarketDataBundle(
            adj_close=frames["adj_close"], close=frames["close"], volume=frames["volume"], info=info,
            provider=f"yfinance {yf.__version__} (Yahoo Finance, unofficial)",
            fetched_at_utc=cached["fetched_at_utc"], expected_last_session=expected,
            dropped_incomplete_sessions=dropped, ticker_errors=errors, from_cache=from_cache)
