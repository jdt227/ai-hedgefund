"""Politician overlay: net count of House members disclosing buys vs. sells.

value = (buyers - sellers) / (buyers + sellers + SHRINK), clipped to [-1, 1].
A single filer moves the value at most 1/3; no person or party is weighted.
available_at is the filing date (when the trade became public). The disclosures
lag trades by up to 45 days, and there is no evidence that these trades beat the
market, so the weight (W_POLITICIAN) is small.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from src.politicians import summarize_by_ticker

NAME = "politician"
VERSION = "politician-house-1.0.0"
SHRINK = 2.0


def overlay_value(buyers: int, sellers: int) -> float:
    return max(-1.0, min(1.0, (buyers - sellers) / (buyers + sellers + SHRINK)))


class PoliticianOverlay:
    """SignalOverlay over parsed House trades."""

    name = NAME
    version = VERSION

    def __init__(self, trades: pd.DataFrame, max_weight: float):
        self.trades = trades
        self.max_weight = max_weight

    def compute(self, tickers: list[str], cutoff: datetime | date) -> pd.DataFrame:
        day = cutoff.date() if isinstance(cutoff, datetime) else cutoff
        summary = summarize_by_ticker(self.trades, day)
        summary = summary[summary["ticker"].isin(tickers)].copy()
        summary["value"] = [overlay_value(b, s) for b, s in zip(summary["buyers"], summary["sellers"])]
        summary["available_at"] = summary["latest_filing"]
        summary["evidence"] = [
            f"House PTRs filed by {r.latest_filing}: {r.buyers} member(s) bought, {r.sellers} sold "
            f"(amount midpoints ${r.buy_amount_mid:,.0f} bought / ${r.sell_amount_mid:,.0f} sold; ranges only); "
            f"median filing lag {r.median_lag_days:.0f} days"
            for r in summary.itertuples()]
        return summary.reset_index(drop=True)
