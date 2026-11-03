"""Eligibility filters and data-quality flags for the weekly universe."""

from __future__ import annotations

import pandas as pd

from config import settings


def eligibility_table(universe: dict[str, str], prices: pd.DataFrame, close: pd.DataFrame | None,
                      volume: pd.DataFrame | None) -> pd.DataFrame:
    """One row per universe ticker: eligible, and the reasons when it is not.

    Without close and volume data (legacy33), only the history check applies.
    """
    rows = {}
    for ticker in universe:
        reasons = []
        series = prices[ticker].dropna() if ticker in prices else pd.Series(dtype=float)
        if len(series) < settings.ELIGIBILITY_MIN_SESSIONS:
            reasons.append(f"history {len(series)} sessions < {settings.ELIGIBILITY_MIN_SESSIONS}")
        if close is not None and volume is not None:
            raw = close[ticker].dropna() if ticker in close else pd.Series(dtype=float)
            if raw.empty or raw.iloc[-1] < settings.ELIGIBILITY_MIN_PRICE:
                reasons.append(f"price below ${settings.ELIGIBILITY_MIN_PRICE:.0f} or unavailable")
            if ticker in volume:
                dollars = (close[ticker] * volume[ticker]).dropna().iloc[-63:]
            else:
                dollars = pd.Series(dtype=float)
            if dollars.empty or dollars.median() < settings.ELIGIBILITY_MIN_DOLLAR_VOLUME:
                reasons.append("median 63-day dollar volume below floor or unavailable")
        rows[ticker] = {"eligible": not reasons, "reason": "; ".join(reasons)}
    frame = pd.DataFrame.from_dict(rows, orient="index", columns=["eligible", "reason"])
    frame.index.name = "ticker"
    return frame


def extreme_move_flags(prices: pd.DataFrame, threshold: float, sessions: int = 63) -> dict[str, str]:
    """Flag recent one-day moves large enough to deserve a data-quality check."""
    flags: dict[str, str] = {}
    for ticker in prices.columns:
        returns = prices[ticker].dropna().pct_change(fill_method=None).iloc[-sessions:].dropna()
        if len(returns) and returns.abs().max() >= threshold:
            day = returns.abs().idxmax()
            flags[ticker] = f"{returns[day]:+.0%} on {day.date()}"
    return flags
