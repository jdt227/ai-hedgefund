"""Gather data for the contest metric during a weekly run. Every source is optional."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from config import settings
from src.account.inputs import load_account
from src.evaluation.account_values import (ACCOUNT_VALUES_FILE, can_reconstruct, cross_check, interval_returns,
                                           load_account_values, reconstruct_daily_values)
from src.evaluation.contest import (DAILY, INTERVAL, PROXY_TICKERS, ex_ante_loadings, proxy_daily_factors,
                                    realized_alpha, rf_from_dtb3)

logger = logging.getLogger(__name__)
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def _french(frequency: str, status: dict) -> pd.DataFrame | None:
    from src.evaluation.fama_french import load_french

    try:
        frame, meta = load_french(frequency)
    except Exception as exc:  # network or format problems leave the metric partial
        status[f"french_{frequency}"] = f"unavailable: {type(exc).__name__}"
        return None
    status[f"french_{frequency}"] = f"ok through {meta['latest_date']}"
    return frame


def _risk_free(daily: pd.DataFrame | None, run_date: date, status: dict) -> pd.Series | None:
    """French RF where it exists, then FRED DTB3 for later dates."""
    from src.http_utils import fetch

    pieces = [daily["RF"]] if daily is not None else []
    key = os.getenv("FRED_API_KEY")
    if key:
        params = {"series_id": "DTB3", "api_key": key, "file_type": "json",
                  "observation_start": "2025-01-01", "observation_end": run_date.isoformat()}
        result = fetch("fred", FRED_URL, params=params, run_date=run_date,
                       min_interval=settings.MIN_INTERVAL_SECONDS["fred"])
        if result.ok:
            obs = {pd.Timestamp(o["date"]): float(o["value"]) for o in result.data.get("observations", [])
                   if o.get("value") not in (None, ".")}
            dtb3 = rf_from_dtb3(pd.Series(obs, dtype=float))
            start = daily.index.max() if daily is not None else pd.Timestamp.min
            pieces.append(dtb3[dtb3.index > start])
            status["proxy_risk_free"] = "French RF, then FRED DTB3"
    if not pieces:
        status["proxy_risk_free"] = "none: the proxy assumes a zero risk-free rate"
        return None
    status.setdefault("proxy_risk_free", "French RF only (later dates carried forward)")
    return pd.concat(pieces).sort_index()


def _proxy(now_utc: datetime, rf: pd.Series | None, status: dict) -> pd.DataFrame | None:
    from src.market_data.yfinance_provider import YFinanceProvider

    try:
        bundle = YFinanceProvider(period="2y", with_info=False).fetch(PROXY_TICKERS, now_utc)
        frame = proxy_daily_factors(bundle.adj_close, rf)
    except Exception as exc:  # provider failures are reported, not raised
        status["proxy_factors"] = f"unavailable: {type(exc).__name__}"
        return None
    status["proxy_factors"] = f"ok through {frame.index.max().date()}"
    return frame


def account_series(account_dir: Path, closes: pd.DataFrame) -> tuple[pd.DataFrame, str, list[str]]:
    """Daily reconstruction when possible, else logged interval returns."""
    account = load_account(account_dir)
    logged, problems = load_account_values(account_dir / ACCOUNT_VALUES_FILE)
    notes = [f"account_values.csv: {p}" for p in problems]
    ok, reason = can_reconstruct(account)
    if ok:
        meta = account.metadata
        start = meta.snapshot_naive().date()
        shares = dict(zip(account.holdings["ticker"], account.holdings["shares"])) if account.holdings is not None else {}
        trades = account.trades
        daily = reconstruct_daily_values(shares, meta.cash, start, trades, closes,
                                         settings.CLASS_RULES["month_timezone"])
        unpriced = sorted({t for cell in daily["unpriced"] for t in cell.split(", ") if t})
        if unpriced:
            notes.append(f"No prices for {', '.join(unpriced)}; reconstructed values leave them out.")
        notes += cross_check(daily, logged)
        return daily, DAILY, notes
    notes.append(f"Using logged values: {reason}.")
    return logged, INTERVAL, notes


def compute_contest(evidence, closes: pd.DataFrame, now_utc: datetime) -> dict:
    """Realized alpha, a labeled proxy estimate, and ex-ante loadings of the model portfolio."""
    status: dict[str, str] = {}
    daily = _french("daily", status)
    weekly = _french("weekly", status)
    rf = _risk_free(daily, evidence.run_date, status)
    proxy = _proxy(now_utc, rf, status)
    series, kind, notes = account_series(evidence.account_dir, closes)
    regression_input = series if kind == DAILY else interval_returns(series)
    realized = realized_alpha(regression_input, kind, daily, weekly, proxy)
    weights = dict(zip(evidence.portfolio["ticker"], evidence.portfolio["weight_pct"])) if len(evidence.portfolio) else {}
    loadings = ex_ante_loadings(evidence.prices, weights, daily, proxy)
    values = series[["date", "account_value"]] if len(series) else pd.DataFrame(columns=["date", "account_value"])
    return {"series_kind": kind, "series_points": int(len(series)), "realized": realized,
            "ex_ante_loadings": loadings, "notes": notes, "sources": status,
            "values": [{"date": str(pd.Timestamp(r.date).date()), "account_value": float(r.account_value)}
                       for r in values.itertuples()]}
