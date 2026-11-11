"""Contest metric: realized FF3 alpha of the account, plus a clearly labeled proxy.

Official: Ken French factors. They lag by weeks, so recent data is missing.
Proxy (estimate only, NOT the graded number): SPY for the market, IWM minus IWB
for size, IWD minus IWF for value, with the risk-free rate from French where
available, then FRED's 3-month T-bill (DTB3), else zero.

Series and conventions:
* Daily reconstructed values use daily factors (252 periods a year).
* Logged values use interval returns between logs. If every interval is exactly
  one French week, the weekly file is used; otherwise daily factors are compounded
  over each interval. Annualization assumes 52 intervals a year.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import settings
from src.evaluation.fama_french import FACTORS, ff3_regression

DAILY = "daily"
INTERVAL = "logged intervals"
PROXY_TICKERS = ["SPY", "IWM", "IWB", "IWD", "IWF"]


def compound_factors(daily: pd.DataFrame, intervals: pd.DataFrame) -> pd.DataFrame:
    """Factor returns over each (start, end] interval, indexed by end date.

    The market is compounded as a total return and RF subtracted afterward. SMB
    and HML are long-short daily returns compounded directly, an approximation.
    Intervals extending past the factor data are dropped.
    """
    rows = {}
    last = daily.index.max()
    for row in intervals.itertuples():
        if row.end > last:
            continue
        window = daily.loc[(daily.index > row.start) & (daily.index <= row.end)]
        if window.empty:
            continue
        rf = float((1 + window["RF"]).prod() - 1)
        market = float((1 + window["Mkt-RF"] + window["RF"]).prod() - 1)
        rows[row.end] = {"Mkt-RF": market - rf, "SMB": float((1 + window["SMB"]).prod() - 1),
                         "HML": float((1 + window["HML"]).prod() - 1), "RF": rf}
    return pd.DataFrame.from_dict(rows, orient="index", columns=[*FACTORS, "RF"])


def weekly_aligned(intervals: pd.DataFrame, weekly: pd.DataFrame) -> bool:
    """True when every interval runs exactly from one French week-end to the next."""
    if intervals.empty or weekly.empty:
        return False
    weeks = list(weekly.index)
    position = {d: i for i, d in enumerate(weeks)}
    return all(row.end in position and row.start in position and position[row.end] == position[row.start] + 1
               for row in intervals.itertuples())


def interval_factors(intervals: pd.DataFrame, daily: pd.DataFrame | None,
                     weekly: pd.DataFrame | None) -> tuple[pd.DataFrame, str]:
    if weekly is not None and weekly_aligned(intervals, weekly):
        return weekly.loc[weekly.index.isin(intervals["end"])], "Ken French weekly file"
    if daily is None:
        return pd.DataFrame(columns=[*FACTORS, "RF"]), "no factor data"
    return compound_factors(daily, intervals), "Ken French daily factors compounded over each interval"


def proxy_daily_factors(adj_close: pd.DataFrame, rf_daily: pd.Series | None) -> pd.DataFrame:
    """ETF proxy factors. An estimate only: these funds are not the French portfolios."""
    returns = adj_close[PROXY_TICKERS].pct_change(fill_method=None).dropna()
    rf = (rf_daily.reindex(returns.index).ffill().fillna(0.0) if rf_daily is not None
          else pd.Series(0.0, index=returns.index))
    return pd.DataFrame({"Mkt-RF": returns["SPY"] - rf, "SMB": returns["IWM"] - returns["IWB"],
                         "HML": returns["IWD"] - returns["IWF"], "RF": rf})


def rf_from_dtb3(observations: pd.Series) -> pd.Series:
    """FRED DTB3 (annual percent, discount basis) to an approximate daily decimal rate."""
    return (1 + observations / 100) ** (1 / 252) - 1


def confidence_label(observations: int, kind: str) -> str:
    low = settings.CONTEST_LOW_CONFIDENCE_OBS[kind]
    if observations < settings.CONTEST_MIN_OBS[kind]:
        return "insufficient history"
    return "LOW CONFIDENCE (few observations)" if observations < low else "moderate confidence"


def regress(returns: pd.Series, factors: pd.DataFrame, kind: str) -> dict:
    """FF3 regression with the contest's thresholds and annualization."""
    periods = 252 if kind == DAILY else 52
    result = ff3_regression(returns, factors, min_obs=settings.CONTEST_MIN_OBS[kind], periods_per_year=periods)
    result["confidence"] = confidence_label(result["observations"], kind)
    result["series_kind"] = kind
    return result


def realized_alpha(series: pd.DataFrame, kind: str, daily: pd.DataFrame | None, weekly: pd.DataFrame | None,
                   proxy: pd.DataFrame | None) -> dict:
    """Official and proxy regressions of the account's own returns."""
    out: dict = {"series_kind": kind, "observations_in_series": 0}
    if kind == DAILY:
        returns = series.set_index("date")["account_value"].pct_change(fill_method=None).dropna()
        out["observations_in_series"] = int(len(returns))
        out["official"] = regress(returns, daily, DAILY) if daily is not None else {"status": "unavailable"}
        out["proxy"] = regress(returns, proxy, DAILY) if proxy is not None else {"status": "unavailable"}
        return out
    intervals = series
    out["observations_in_series"] = int(len(intervals))
    if intervals.empty:
        out["official"] = out["proxy"] = {"status": "insufficient_history",
                                          "message": "Log at least two account values to get one return."}
        return out
    returns = pd.Series(intervals["return"].to_numpy(), index=pd.DatetimeIndex(intervals["end"]))
    factors, source = interval_factors(intervals, daily, weekly)
    out["official"] = regress(returns, factors, INTERVAL) | {"factor_construction": source}
    if proxy is not None:
        out["proxy"] = regress(returns, compound_factors(proxy, intervals), INTERVAL) | {
            "factor_construction": "ETF proxy factors compounded over each interval"}
    else:
        out["proxy"] = {"status": "unavailable"}
    return out


def ex_ante_loadings(prices: pd.DataFrame, weights: dict[str, float], daily: pd.DataFrame | None,
                     proxy: pd.DataFrame | None, sessions: int = 252) -> dict:
    """FF3 loadings of a constant-weight portfolio over the past year. Not performance."""
    from src.evaluation.fama_french import fixed_weight_returns

    usable = {t: w for t, w in weights.items() if t in prices and w > 0}
    if not usable:
        return {"status": "unavailable", "message": "No priced stocks in the proposed portfolio."}
    basket = fixed_weight_returns(prices, usable, sessions=sessions)
    out = {"weights_used": {t: w / sum(usable.values()) for t, w in usable.items()},
           "note": ("Stocks only, rescaled to 100%; cash excluded. Loadings describe exposure and earn no alpha. "
                    "The intercept is not reported: these stocks were chosen for their past returns.")}
    out["official"] = regress(basket, daily, DAILY) if daily is not None else {"status": "unavailable"}
    out["proxy"] = regress(basket, proxy, DAILY) if proxy is not None else {"status": "unavailable"}
    return out


def summarize(result: dict, include_alpha: bool = True) -> str:
    """One readable line for a regression result.

    Ex-ante results leave alpha out: the intercept of stocks chosen for their past
    returns is hindsight, not an expected alpha.
    """
    if result.get("status") != "ok":
        return f"{result.get('status', 'unavailable')}: {result.get('message', 'no data')}"
    c, t = result["coefficients"], result["t_newey_west"]
    alpha = (f"alpha {result['alpha_per_period']:+.4%} per period ({result['alpha_annual_arithmetic']:+.1%}/yr, "
             f"t {t['alpha']:.1f}); " if include_alpha else "")
    return (f"{alpha}market {c['beta_mkt']:.2f}, SMB {c['beta_smb']:.2f}, HML {c['beta_hml']:.2f}; "
            f"R² {result['r_squared']:.2f}; {result['observations']} obs ({result['confidence']}); "
            f"factors through {result['factor_latest_date']}")


def nan_to_none(value):
    return None if isinstance(value, float) and np.isnan(value) else value
