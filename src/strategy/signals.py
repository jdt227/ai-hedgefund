"""Per-ticker signal table computed only from data up to the cutoff session.

All return-based signals use adjusted closes. Lookbacks are in trading
sessions (rows), where t is the last completed session:

    mom_12_1   = P[t-21] / P[t-252] - 1
    mom_6_1    = P[t-21] / P[t-126] - 1
    trend_200  = P[t] / mean(P[t-199..t]) - 1
    vol_63     = std(r[t-62..t], ddof=1) * sqrt(252),  r = daily simple return
    max_dd_252 = min over the last 252 sessions of P / running_max(P) - 1
    ret_1m     = P[t] / P[t-21] - 1                       (descriptive only)
    rs_6m_vs_benchmark = mom_6_1 - benchmark mom_6_1     (descriptive only)
    median_dollar_volume_63 = median(close * volume) over 63 sessions

Relative strength vs. the benchmark is descriptive only: subtracting the same
benchmark return from every stock does not change cross-sectional z-scores,
so as a score component it would duplicate mom_6_1.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _ret(series: pd.Series, start_lag: int, end_lag: int) -> float:
    """P[t-end_lag] / P[t-start_lag] - 1 on a NaN-free series."""
    if len(series) <= start_lag:
        return np.nan
    return float(series.iloc[-1 - end_lag] / series.iloc[-1 - start_lag] - 1)


def compute_signals(adj_close: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame,
                    cutoff: pd.Timestamp | None = None, benchmark: str | None = "SPY") -> pd.DataFrame:
    if cutoff is not None:
        adj_close = adj_close.loc[:cutoff]
        close = close.loc[:cutoff]
        volume = volume.loc[:cutoff]

    rows = {}
    for ticker in adj_close.columns:
        p = adj_close[ticker].dropna()
        row = {"history_sessions": len(p),
               "last_valid_date": p.index[-1].date().isoformat() if len(p) else None}
        if len(p) == 0:
            rows[ticker] = row
            continue
        r = p.pct_change().dropna()
        row["mom_12_1"] = _ret(p, 252, 21)
        row["mom_6_1"] = _ret(p, 126, 21)
        row["ret_1m"] = _ret(p, 21, 0)
        row["trend_200"] = float(p.iloc[-1] / p.iloc[-200:].mean() - 1) if len(p) >= 200 else np.nan
        row["vol_63"] = float(r.iloc[-63:].std(ddof=1) * np.sqrt(252)) if len(r) >= 63 else np.nan
        if len(p) >= 252:
            window = p.iloc[-252:]
            row["max_dd_252"] = float((window / window.cummax() - 1).min())
        else:
            row["max_dd_252"] = np.nan
        ref = close[ticker].dropna() if ticker in close else pd.Series(dtype=float)
        row["reference_close"] = float(ref.iloc[-1]) if len(ref) else np.nan
        row["reference_date"] = ref.index[-1].date().isoformat() if len(ref) else None
        dv = (close[ticker] * volume[ticker]).dropna() if ticker in volume else pd.Series(dtype=float)
        row["median_dollar_volume_63"] = float(dv.iloc[-63:].median()) if len(dv) >= 63 else np.nan
        rows[ticker] = row

    table = pd.DataFrame.from_dict(rows, orient="index")
    table.index.name = "ticker"
    table = table.replace([np.inf, -np.inf], np.nan)
    if benchmark and benchmark in table.index and pd.notna(table.at[benchmark, "mom_6_1"]):
        table["rs_6m_vs_benchmark"] = table["mom_6_1"] - table.at[benchmark, "mom_6_1"]
    else:
        table["rs_6m_vs_benchmark"] = np.nan
    return table.sort_index()
