"""Eligibility filters, cross-sectional z-scores, and the composite score.

Procedure
---------
1. Eligibility: in the candidate universe, not monitor-only, quote type
   EQUITY (if the provider can't classify, the ticker is still eligible but
   flagged), enough history, price and liquidity floors, and fresh data.
   Every exclusion carries a reason.
2. For each signal k, compute a z-score across ELIGIBLE tickers only:
   z = (x - mean) / std (ddof=0), clipped to [-winsor, +winsor].
3. composite = sum_k weight_k * direction_k * z_k. Weights sum to 1.
   Missing signals never get a fill-in value: a ticker missing any required
   signal is ineligible ("insufficient history"), not scored as poor.
4. Ranking: composite descending; ties broken alphabetically by ticker so the
   output is deterministic.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def apply_eligibility(signals: pd.DataFrame, info: pd.DataFrame, universe: dict[str, str],
                      eligibility: dict, scoring: dict, signal_cfg: dict,
                      monitor_only: set[str], stale: set[str]) -> pd.DataFrame:
    table = signals.copy()
    table["in_universe"] = table.index.isin(list(universe))
    info = info.reindex(table.index)
    table["company"] = info.get("name")
    table["quote_type"] = info.get("quote_type")
    provider_sector = info.get("sector") if "sector" in info else pd.Series(index=table.index, dtype=object)
    table["sector"] = provider_sector.where(provider_sector.notna() & (provider_sector != ""),
                                            table.index.map(universe))
    table["sector_source"] = np.where(provider_sector.notna() & (provider_sector != ""),
                                      "provider", "config fallback")
    table["market_cap_current"] = info.get("market_cap_current")

    reasons: dict[str, list[str]] = {t: [] for t in table.index}
    flags: dict[str, list[str]] = {t: [] for t in table.index}
    allowed = set(eligibility["allowed_quote_types"])
    for ticker, row in table.iterrows():
        r = reasons[ticker]
        if not row["in_universe"]:
            r.append("not in candidate universe (monitored only)")
        if ticker in monitor_only:
            r.append("monitor-only (ETF/benchmark)")
        if ticker in eligibility.get("exclude", []):
            r.append("excluded in config")
        qt = row["quote_type"]
        if isinstance(qt, str) and qt:
            if qt not in allowed:
                r.append(f"quote type {qt} not allowed")
        else:
            flags[ticker].append("quote type unverified")
        if ticker in stale:
            r.append("price data stale or missing for the latest session")
        if row["history_sessions"] < scoring["min_history_sessions"]:
            r.append(f"insufficient history ({row['history_sessions']} < {scoring['min_history_sessions']} sessions)")
        missing = [k for k in signal_cfg if pd.isna(row.get(k))]
        if missing and row["history_sessions"] >= scoring["min_history_sessions"]:
            r.append(f"missing signals: {', '.join(missing)}")
        if pd.isna(row.get("reference_close")) or row["reference_close"] < eligibility["min_price"]:
            r.append(f"price below ${eligibility['min_price']:.0f} or unavailable")
        mdv = row.get("median_dollar_volume_63")
        if pd.isna(mdv) or mdv < eligibility["min_median_dollar_volume_63"]:
            r.append("median 63-session dollar volume below floor or unavailable")

    table["eligible"] = [not reasons[t] for t in table.index]
    table["exclusion_reasons"] = ["; ".join(reasons[t]) for t in table.index]
    table["data_flags"] = ["; ".join(flags[t]) for t in table.index]
    return table


def score(table: pd.DataFrame, signal_cfg: dict, winsor: float) -> pd.DataFrame:
    """Add z-scores, composite, rank and percentile for eligible tickers."""
    out = table.copy()
    eligible = out.index[out["eligible"]]
    out["composite_score"] = np.nan
    total = pd.Series(0.0, index=eligible)
    for name, cfg in signal_cfg.items():
        values = out.loc[eligible, name].astype(float)
        std = values.std(ddof=0)
        if len(values) < 2 or not np.isfinite(std) or std == 0:
            z = pd.Series(0.0, index=eligible)
        else:
            z = ((values - values.mean()) / std).clip(-winsor, winsor)
        out[f"z_{name}"] = np.nan
        out.loc[eligible, f"z_{name}"] = z
        contribution = cfg["weight"] * cfg["direction"] * z
        out[f"contrib_{name}"] = np.nan
        out.loc[eligible, f"contrib_{name}"] = contribution
        total = total + contribution
    out.loc[eligible, "composite_score"] = total

    ranked = out.loc[eligible].reset_index().sort_values(
        ["composite_score", "ticker"], ascending=[False, True], kind="mergesort")
    out["rank"] = pd.Series(range(1, len(ranked) + 1), index=ranked["ticker"].to_numpy()).reindex(out.index)
    n = len(ranked)
    out["percentile"] = (1 - (out["rank"] - 1) / max(n - 1, 1)) if n else np.nan
    return out.sort_values(["rank", "ticker"], na_position="last", kind="mergesort")


def build_ranking(signals: pd.DataFrame, info: pd.DataFrame, universe: dict[str, str],
                  eligibility: dict, scoring_cfg: dict, signal_cfg: dict,
                  monitor_only: set[str], stale: set[str]) -> pd.DataFrame:
    weights = sum(c["weight"] for c in signal_cfg.values())
    if not np.isclose(weights, 1.0):
        raise ValueError(f"Signal weights must sum to 1 (got {weights}).")
    # Every universe ticker must appear, even if the provider returned nothing,
    # so a missing name shows up as excluded-with-reason rather than vanishing.
    signals = signals.reindex(sorted(set(signals.index) | set(universe)))
    signals["history_sessions"] = signals["history_sessions"].fillna(0).astype(int)
    table = apply_eligibility(signals, info, universe, eligibility, scoring_cfg, signal_cfg,
                              monitor_only, stale)
    return score(table, signal_cfg, scoring_cfg["winsor_z"])
