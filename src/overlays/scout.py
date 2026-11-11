"""Scout overlay: an equity "scouting report" with A-F grades from price data.

Basketball scouting is a reasoning metaphor, not a proven source of returns.
Every grade is a cross-sectional percentile among scored stocks
(A >= 80th, B >= 60th, C >= 40th, D >= 20th, else F):

* clutch:        downside capture vs SPY on SPY down days, last 126 sessions (lower is better)
* durability:    average of 1-year max drawdown and recovery to the 1-year high (higher is better)
* injury report: number of catalyst flags plus earnings inside the window (fewer is better)
* team fit:      average 63-session return correlation with the other top candidates (lower is better)
* scoring:       6-month return (display only)

The overlay value uses ONLY clutch, durability, and team fit, so momentum is not
counted twice: value = 2 x mean(percentile) - 1, in [-1, +1]. Its contribution
to the final score is W_SCOUT x value.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

NAME = "scout"
VERSION = "scout-1.0.0"
CLUTCH_SESSIONS = 126
FIT_SESSIONS = 63
GRADE_CUTS = [(0.8, "A"), (0.6, "B"), (0.4, "C"), (0.2, "D")]
VALUE_COMPONENTS = ["clutch", "durability", "team_fit"]


def grade(percentile: float) -> str:
    if pd.isna(percentile):
        return "n/a"
    return next((letter for cut, letter in GRADE_CUTS if percentile >= cut), "F")


def downside_capture(prices: pd.DataFrame, benchmark: str = "SPY", sessions: int = CLUTCH_SESSIONS) -> pd.Series:
    """Mean stock return on benchmark down days divided by the benchmark's mean on those days."""
    returns = prices.pct_change(fill_method=None).iloc[-sessions:]
    down = returns[benchmark] < 0
    if down.sum() == 0:
        return pd.Series(np.nan, index=prices.columns.drop(benchmark))
    bench = returns.loc[down, benchmark].mean()
    return returns.loc[down].drop(columns=benchmark).mean() / bench


def durability(prices: pd.DataFrame, sessions: int = 252) -> pd.DataFrame:
    window = prices.iloc[-sessions:]
    drawdown = (window / window.cummax() - 1).min()
    recovery = window.iloc[-1] / window.max()
    return pd.DataFrame({"max_drawdown": drawdown, "recovery": recovery})


def team_fit(prices: pd.DataFrame, top: list[str], sessions: int = FIT_SESSIONS) -> pd.Series:
    """Average correlation with the top candidates, excluding the stock itself."""
    returns = prices.pct_change(fill_method=None).iloc[-sessions:]
    top = [t for t in top if t in returns]
    if not top:
        return pd.Series(np.nan, index=prices.columns)
    corr = returns.corr().loc[:, top]
    out = {}
    for ticker in corr.index:
        others = corr.loc[ticker].drop(labels=[ticker], errors="ignore")
        out[ticker] = others.mean() if len(others) else np.nan
    return pd.Series(out)


def percentile(values: pd.Series, higher_is_better: bool) -> pd.Series:
    ranked = values.rank(pct=True, method="average")
    return ranked if higher_is_better else 1 - ranked + 1 / max(values.notna().sum(), 1)


class ScoutOverlay:
    """SignalOverlay built from prices already downloaded for the run."""

    name = NAME
    version = VERSION

    def __init__(self, prices: pd.DataFrame, top_candidates: list[str], max_weight: float,
                 flags: dict[str, list[dict]] | None = None, benchmark: str = "SPY"):
        self.prices = prices
        self.top = top_candidates
        self.max_weight = max_weight
        self.flags = flags or {}
        self.benchmark = benchmark

    def compute(self, tickers: list[str], cutoff: datetime) -> pd.DataFrame:
        """One row per ticker with grades, raw measures, value, available_at, and evidence."""
        prices = self.prices.loc[: pd.Timestamp(cutoff).tz_localize(None)]
        names = [t for t in tickers if t in prices and t != self.benchmark]
        if self.benchmark not in prices or not names:
            return pd.DataFrame(columns=["ticker", "value", "available_at", "evidence"])
        frame = prices[names + [self.benchmark]]
        table = pd.DataFrame(index=pd.Index(names, name="ticker"))
        table["capture"] = downside_capture(frame, self.benchmark)
        table = table.join(durability(frame[names]))
        table["avg_corr"] = team_fit(frame[names], self.top)
        table["six_month_return"] = frame[names].iloc[-1] / frame[names].iloc[-127] - 1 if len(frame) > 126 else np.nan
        table["injuries"] = [len([f for f in self.flags.get(t, []) if f["flag"] != "low_news_coverage"]) for t in names]

        pct = pd.DataFrame(index=table.index)
        pct["clutch"] = percentile(table["capture"], higher_is_better=False)
        pct["durability"] = (percentile(table["max_drawdown"], True) + percentile(table["recovery"], True)) / 2
        pct["team_fit"] = percentile(table["avg_corr"], higher_is_better=False)
        pct["injury"] = percentile(table["injuries"].astype(float), higher_is_better=False)
        pct["scoring"] = percentile(table["six_month_return"], higher_is_better=True)
        for column in pct.columns:
            table[f"grade_{column}"] = pct[column].map(grade)

        mean = pct[VALUE_COMPONENTS].mean(axis=1, skipna=False)
        table["value"] = (2 * mean - 1).clip(-1, 1)
        table["available_at"] = str(prices.index[-1].date())
        table["evidence"] = [self._evidence(row) for row in table.itertuples()]
        return table.reset_index()

    @staticmethod
    def _evidence(row) -> str:
        return (f"clutch {row.grade_clutch} (downside capture {row.capture:.2f}); durability {row.grade_durability} "
                f"(1y max drawdown {row.max_drawdown:.0%}, at {row.recovery:.0%} of 1y high); team fit "
                f"{row.grade_team_fit} (avg correlation {row.avg_corr:.2f}); injury report {row.grade_injury} "
                f"({row.injuries} flags); scoring {row.grade_scoring} (6M {row.six_month_return:+.0%}, display only)")
