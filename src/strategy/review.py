"""Top-five candidate report and current-holdings review.

The four outputs of the system are kept distinct:
  1. research candidates (this module's top-five)  - a ranking, not an order
  2. target portfolio                               - src/strategy/orders.py
  3. proposed orders                                - src/strategy/orders.py
  4. actual executed trades                         - private/account/trades.csv
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SIGNAL_LABELS = {
    "mom_12_1": "12-1M momentum",
    "mom_6_1": "6-1M momentum",
    "trend_200": "price vs 200d avg",
    "vol_63": "63d volatility",
    "max_dd_252": "1y max drawdown",
}


def classify_asset(ticker: str, declared: str | None, quote_type: str | None,
                   known_etfs: set[str]) -> tuple[str, str]:
    """Return (STOCK|ETF|UNKNOWN, source). Never guesses STOCK silently."""
    if isinstance(declared, str) and declared in ("STOCK", "ETF"):
        return declared, "holdings.csv asset_type"
    if isinstance(quote_type, str) and quote_type:
        if quote_type == "ETF":
            return "ETF", "provider quote type"
        if quote_type == "EQUITY":
            return "STOCK", "provider quote type"
        return "UNKNOWN", f"provider quote type {quote_type}"
    if ticker in known_etfs:
        return "ETF", "config KNOWN_ETFS"
    return "UNKNOWN", "unclassified"


def signal_percentiles(ranking: pd.DataFrame, signal_cfg: dict) -> pd.DataFrame:
    """Direction-adjusted percentile (1.0 = best) of each signal among eligible tickers."""
    eligible = ranking[ranking["eligible"]]
    out = pd.DataFrame(index=ranking.index)
    for name, cfg in signal_cfg.items():
        adjusted = eligible[name] * cfg["direction"]
        out[f"pct_{name}"] = adjusted.rank(pct=True, method="average").reindex(ranking.index)
    return out


def _fmt_signal(name: str, value: float) -> str:
    if name == "vol_63":
        return f"{value:.0%}"
    return f"{value:+.1%}"


def candidate_reasons(row: pd.Series, pct: pd.Series, signal_cfg: dict) -> str:
    parts = []
    contribs = sorted(((row[f"contrib_{k}"], k) for k in signal_cfg), reverse=True)
    for contrib, name in contribs:
        if contrib <= 0:
            continue
        parts.append(f"{SIGNAL_LABELS[name]} {_fmt_signal(name, row[name])} "
                     f"(better than {pct[f'pct_{name}']:.0%} of eligible; +{contrib:.2f} to score)")
    return "; ".join(parts) or "No component scored above the universe average."


def candidate_risks(row: pd.Series, ranking: pd.DataFrame, sector_note: str | None) -> str:
    eligible = ranking[ranking["eligible"]]
    risks = []
    if row["vol_63"] >= eligible["vol_63"].quantile(0.7):
        risks.append(f"high volatility ({row['vol_63']:.0%} annualized, top 30% of universe)")
    if row["max_dd_252"] <= -0.25:
        risks.append(f"deep 1y drawdown ({row['max_dd_252']:.0%})")
    if row["ret_1m"] >= 0.15:
        risks.append(f"extended: +{row['ret_1m']:.0%} in the last month (short-term reversal risk)")
    elif row["ret_1m"] <= -0.08:
        risks.append(f"recent pullback: {row['ret_1m']:.0%} in the last month")
    for z in ("z_mom_12_1", "z_mom_6_1"):
        if abs(row[z]) >= 2.9:
            risks.append("momentum outlier (z clipped at winsor limit)")
            break
    if sector_note:
        risks.append(sector_note)
    if row.get("data_flags"):
        risks.append(row["data_flags"])
    if row.get("sector_source") == "config fallback":
        risks.append("sector from config fallback")
    risks.append("price-only model: earnings dates, valuation, news and fundamentals not evaluated")
    return "; ".join(risks)


def sector_weights(review: pd.DataFrame, cash: float | None) -> tuple[pd.Series, str]:
    """Current sector weights and the basis they are measured on."""
    values = review.groupby("sector")["marked_value"].sum()
    if cash is not None:
        return values / (values.sum() + max(cash, 0.0)), "share of account equity (cash + marked holdings)"
    total = values.sum()
    return (values / total if total else values), "share of INVESTED holdings only (cash unknown)"


def holdings_review(positions: pd.DataFrame, holdings: pd.DataFrame | None, ranking: pd.DataFrame,
                    info: pd.DataFrame, known_etfs: set[str], scoring_cfg: dict) -> pd.DataFrame:
    """One row per current position with marked value, signals, and review flag."""
    if positions.empty:
        return pd.DataFrame(columns=["ticker", "shares", "asset_type", "marked_value"]).set_index("ticker")
    declared = {}
    snap = {}
    if holdings is not None and not holdings.empty:
        declared = holdings.set_index("ticker")["asset_type"].to_dict()
        snap = holdings.set_index("ticker").to_dict("index")

    n_eligible = int(ranking["eligible"].sum())
    rows = []
    for _, pos in positions.iterrows():
        t = pos["ticker"]
        r = ranking.loc[t] if t in ranking.index else None
        qt = info.loc[t, "quote_type"] if t in info.index and "quote_type" in info else None
        asset, asset_src = classify_asset(t, declared.get(t), qt, known_etfs)
        s = snap.get(t, {})
        ref = r["reference_close"] if r is not None else np.nan
        row = {
            "ticker": t, "shares": pos["shares"], "position_status": pos["status"],
            "asset_type": asset, "asset_type_source": asset_src,
            "company": r["company"] if r is not None else None,
            "sector": (r["sector"] if r is not None and isinstance(r["sector"], str)
                       else ("ETF (broad market)" if asset == "ETF" else "Unknown")),
            "snapshot_displayed_price": s.get("displayed_current_price"),
            "snapshot_market_value": s.get("market_value"),
            "snapshot_unrealized_pnl_reported": s.get("reported_unrealized_pnl"),
            "latest_close": ref,
            "latest_close_date": r["reference_date"] if r is not None else None,
            "marked_value": pos["shares"] * ref if pd.notna(ref) else np.nan,
            "rank": r["rank"] if r is not None else np.nan,
            "of_eligible": n_eligible,
            "composite_score": r["composite_score"] if r is not None else np.nan,
        }
        for k in ("mom_12_1", "mom_6_1", "ret_1m", "vol_63", "max_dd_252", "trend_200"):
            row[k] = r[k] if r is not None else np.nan
        if pd.notna(row["snapshot_displayed_price"]) and pd.notna(ref):
            row["close_vs_snapshot_price"] = ref / row["snapshot_displayed_price"] - 1
        else:
            row["close_vs_snapshot_price"] = np.nan

        if r is None or pd.isna(row["latest_close"]):
            flag = "NO MARKET DATA - cannot review"
        elif not r["eligible"]:
            flag = f"not ranked ({r['exclusion_reasons']})"
        else:
            pct = r["percentile"]
            if pct <= scoring_cfg["review_bottom_quantile"]:
                flag = "REVIEW: bottom quintile of current ranking"
            elif pct >= 1 - scoring_cfg["review_top_quantile"]:
                flag = "supported: top quintile of current ranking"
            else:
                flag = "neutral: middle of current ranking"
        row["review_flag"] = flag
        rows.append(row)
    return pd.DataFrame(rows).set_index("ticker")


def stock_counts(review: pd.DataFrame, etf_counts: bool | None, minimum: int) -> dict:
    stocks = int((review["asset_type"] == "STOCK").sum()) if len(review) else 0
    etfs = int((review["asset_type"] == "ETF").sum()) if len(review) else 0
    unknown = int((review["asset_type"] == "UNKNOWN").sum()) if len(review) else 0
    counted = stocks + (etfs if etf_counts else 0)
    if etf_counts is None:
        basis = "ETFs NOT counted (professor has not confirmed whether ETFs count)"
    else:
        basis = "ETFs counted (confirmed)" if etf_counts else "ETFs not counted (confirmed)"
    return {
        "individual_stocks": stocks, "etfs": etfs, "unknown_type": unknown,
        "total_positions": len(review), "counted_toward_minimum": counted, "minimum": minimum,
        "basis": basis, "meets_minimum": counted >= minimum,
        "shortfall": max(0, minimum - counted),
    }


def top_candidates(ranking: pd.DataFrame, held: set[str], sector_now: pd.Series, sector_basis: str,
                   max_sector_weight: float, signal_cfg: dict, n: int,
                   sector_values: pd.Series | None = None,
                   cash_known: bool = True) -> tuple[pd.DataFrame, str | None]:
    """Top-n eligible tickers with reasons, risks, and a status.

    When cash is unknown, sector weights are measured on invested holdings
    only, which overstates concentration. A sector-cap conflict is then
    reported as conditional, with the account equity at which it stops binding.
    """
    eligible = ranking[ranking["eligible"]].sort_values("rank")
    pct = signal_percentiles(ranking, signal_cfg)
    shortfall = None
    if len(eligible) < n:
        shortfall = (f"Only {len(eligible)} ticker(s) passed eligibility; {n} were requested. "
                     "The list is NOT padded with ineligible names.")
    rows = []
    for ticker, row in eligible.head(n).iterrows():
        sw = float(sector_now.get(row["sector"], 0.0))
        sector_note = None
        if ticker in held:
            status = "existing holding"
        elif sw >= max_sector_weight and cash_known:
            status = "constrained (sector cap)"
            sector_note = (f"{row['sector']} is already {sw:.0%} ({sector_basis}); "
                           f"cap is {max_sector_weight:.0%}")
        elif sw >= max_sector_weight:
            value = float(sector_values.get(row["sector"], 0.0)) if sector_values is not None else float("nan")
            need = value / max_sector_weight
            status = f"conditionally constrained (sector cap binds unless equity > ${need:,.0f})"
            sector_note = (f"{row['sector']} is {sw:.0%} of invested holdings (${value:,.0f}); with cash unknown, "
                           f"a {max_sector_weight:.0%} cap leaves room only if account equity exceeds ${need:,.0f}")
        else:
            status = "possible addition"
            if sw > 0:
                sector_note = f"adds to existing {row['sector']} exposure ({sw:.0%}, {sector_basis})"
        rows.append({
            "rank": int(row["rank"]), "ticker": ticker, "company": row["company"],
            "sector": row["sector"], "reference_close": row["reference_close"],
            "reference_date": row["reference_date"],
            "composite_score": row["composite_score"],
            **{k: row[k] for k in signal_cfg}, "ret_1m": row["ret_1m"],
            **{f"z_{k}": row[f"z_{k}"] for k in signal_cfg},
            "signal_coverage": f"{sum(pd.notna(row[k]) for k in signal_cfg)}/{len(signal_cfg)}",
            "history_sessions": int(row["history_sessions"]),
            "held": ticker in held, "status": status,
            "reasons": candidate_reasons(row, pct.loc[ticker], signal_cfg),
            "risks": candidate_risks(row, ranking, sector_note),
        })
    return pd.DataFrame(rows), shortfall
