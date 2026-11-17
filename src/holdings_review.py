"""HOLD / TRIM / SELL suggestions for every current holding.

Rules, in order:
  SELL: banned product; ETF while ALLOW_ETF_HOLDINGS is False; hard-veto flag;
        Risk Manager veto; failed eligibility; rank worse than SELL_RANK_THRESHOLD.
  TRIM: over the position limit; sector over its limit (lowest-ranked holdings in
        that sector first); Risk Manager haircut.
  HOLD: everything else. "HOLD (watch)" when rank is between the buy cutoff and the
        sell line, or earnings fall inside the window.

New buys need rank within BUY_RANK_CUTOFF, while incumbents stay until they pass
SELL_RANK_THRESHOLD. That gap (hysteresis) limits churn on a noisy signal. Changes
smaller than MIN_TRADE_PCT points are ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from config import settings
from src.banned_products import banned_reason, is_etf

COLUMNS = ["ticker", "sector", "rank", "final_score", "flags", "current_weight_pct", "action", "action_label",
           "target_weight_pct", "reasons"]


@dataclass
class HoldingsInputs:
    positions: pd.DataFrame                  # ticker, shares
    final_scores: pd.DataFrame               # index ticker, final_score
    eligible: set[str]                       # universe stocks that passed eligibility
    universe: dict[str, str]
    prices: pd.DataFrame                     # traded closes where available, else adjusted
    cash: float | None = None                # official cash when known; never guessed
    flags: dict[str, list[dict]] = field(default_factory=dict)
    vetoed: dict[str, str] = field(default_factory=dict)
    ineligible_reasons: dict[str, str] = field(default_factory=dict)
    risk_vetoes: dict[str, str] = field(default_factory=dict)
    risk_haircuts: dict[str, str] = field(default_factory=dict)
    quote_types: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)


def rank_stocks(final_scores: pd.DataFrame, eligible: set[str]) -> dict[str, int]:
    """Rank eligible stocks by final score, best first. Ties break alphabetically."""
    rows = final_scores[final_scores.index.isin(eligible)]
    ordered = sorted(rows.index, key=lambda t: (-rows.at[t, "final_score"], t))
    return {ticker: i for i, ticker in enumerate(ordered, start=1)}


def would_be_rank(ticker: str, final_scores: pd.DataFrame, ranks: dict[str, int]) -> int | None:
    """Where a stock outside the eligible set would sit if it were ranked."""
    if ticker not in final_scores.index:
        return None
    score = final_scores.at[ticker, "final_score"]
    return 1 + sum(1 for t in ranks if final_scores.at[t, "final_score"] > score)


def current_weights(inputs: HoldingsInputs) -> tuple[dict[str, float], str]:
    """Weight of each holding, and the basis it is measured on."""
    values = {}
    for row in inputs.positions.itertuples():
        if row.ticker in inputs.prices and inputs.prices[row.ticker].notna().any():
            values[row.ticker] = row.shares * float(inputs.prices[row.ticker].dropna().iloc[-1])
    invested = sum(values.values())
    if inputs.cash is not None:
        total = invested + max(inputs.cash, 0.0)
        basis = "% of account"
    else:
        total = invested
        basis = "% of invested holdings (cash unknown, so position and sector limits are not applied)"
    weights = {t: 100 * v / total for t, v in values.items()} if total else {}
    return weights, basis


def _sell_reason(ticker: str, rank: int | None, inputs: HoldingsInputs) -> str | None:
    if ticker.upper() == settings.ALLOCATION_CASH_SYMBOL:
        return (f"ticker {ticker} is the same as the allocation's {settings.ALLOCATION_CASH_SYMBOL} "
                "(cash balance) row, so it cannot be kept in a Symbol,Weight block")
    banned = banned_reason(ticker, inputs.names.get(ticker), inputs.quote_types.get(ticker))
    if banned:
        return f"banned by class rules: {banned}"
    if is_etf(ticker, inputs.quote_types.get(ticker)) and not settings.ALLOW_ETF_HOLDINGS:
        return "near-zero FF3 alpha by construction; frees capital for picks"
    if ticker in inputs.vetoed:
        return f"hard veto: {inputs.vetoed[ticker]}"
    if ticker in inputs.risk_vetoes:
        return f"Risk Manager veto: {inputs.risk_vetoes[ticker]}"
    if ticker in inputs.ineligible_reasons:
        return f"failed eligibility: {inputs.ineligible_reasons[ticker]}"
    if rank is None:
        return "no usable score"
    if rank > settings.SELL_RANK_THRESHOLD:
        return f"rank {rank} is worse than the sell line of {settings.SELL_RANK_THRESHOLD}"
    return None


def _apply_position_limit(rows: dict[str, dict], limit: int) -> None:
    for ticker, row in rows.items():
        if row["action"] != "SELL" and row["current_weight_pct"] > limit + settings.MIN_TRADE_PCT - 1e-9:
            row.update(action="TRIM", target_weight_pct=limit)
            row["reasons"].append(f"over the {limit}% position limit")


def _apply_sector_limit(rows: dict[str, dict], limit: int) -> None:
    """Trim the lowest-ranked holdings first until each sector is back within its limit."""
    def effective(row: dict) -> float:
        if row["action"] == "SELL":
            return 0.0
        target = row["target_weight_pct"]
        return row["current_weight_pct"] if target is None else target

    for sector in {row["sector"] for row in rows.values()}:
        members = [t for t, row in rows.items() if row["sector"] == sector and row["action"] != "SELL"]
        excess = sum(effective(rows[t]) for t in members) - limit
        worst_first = sorted(members, key=lambda t: rows[t]["rank"] or 10**9, reverse=True)
        for ticker in worst_first:
            if excess < settings.MIN_TRADE_PCT:
                break
            row = rows[ticker]
            current = effective(row)
            cut = min(math.ceil(excess), math.floor(current))
            if cut < settings.MIN_TRADE_PCT:
                continue
            row.update(action="TRIM", target_weight_pct=math.floor(current) - cut)
            row["reasons"].append(f"{sector} is over its {limit}% limit; lowest-ranked trimmed first")
            excess -= current - row["target_weight_pct"]


def review_holdings(inputs: HoldingsInputs) -> tuple[pd.DataFrame, str]:
    """One row per holding with a suggested action and its reasons."""
    limits = settings.limits()
    ranks = rank_stocks(inputs.final_scores, inputs.eligible)
    weights, basis = current_weights(inputs)
    rows: dict[str, dict] = {}
    for position in inputs.positions.itertuples():
        ticker = position.ticker
        rank = ranks.get(ticker)
        if rank is None and ticker not in inputs.ineligible_reasons:
            rank = would_be_rank(ticker, inputs.final_scores, ranks)
        flags = inputs.flags.get(ticker, [])
        sell = _sell_reason(ticker, rank, inputs)
        score = inputs.final_scores.at[ticker, "final_score"] if ticker in inputs.final_scores.index else float("nan")
        rows[ticker] = {
            "ticker": ticker, "sector": inputs.universe.get(ticker, "Other"), "rank": rank, "final_score": score,
            "flags": "; ".join(f["flag"] for f in flags), "current_weight_pct": weights.get(ticker, 0.0),
            "action": "SELL" if sell else "HOLD", "target_weight_pct": 0 if sell else None,
            "reasons": [sell] if sell else [],
        }

    # Without cash, invested-only weights overstate concentration, so limits are not applied.
    if inputs.cash is not None:
        _apply_position_limit(rows, limits["max_position_pct"])
    for ticker, reason in inputs.risk_haircuts.items():
        if ticker in rows and rows[ticker]["action"] != "SELL":
            target = max(0, math.floor(rows[ticker]["current_weight_pct"]) - settings.MIN_TRADE_PCT)
            if rows[ticker]["target_weight_pct"] is not None:
                target = min(target, rows[ticker]["target_weight_pct"])
            rows[ticker].update(action="TRIM", target_weight_pct=target)
            rows[ticker]["reasons"].append(f"Risk Manager haircut: {reason}")
    if inputs.cash is not None:
        _apply_sector_limit(rows, limits["max_sector_pct"])

    for row in rows.values():
        _label(row, inputs.flags.get(row["ticker"], []))
        row["reasons"] = "; ".join(row["reasons"]) or "within limits and above the sell line"
    frame = pd.DataFrame(rows.values(), columns=COLUMNS)
    return frame, basis


def _label(row: dict, flags: list[dict]) -> None:
    row["action_label"] = row["action"]
    if row["action"] != "HOLD":
        return
    watch = []
    rank = row["rank"]
    if rank is not None and rank > settings.BUY_RANK_CUTOFF:
        watch.append(f"rank {rank} is outside the buy cutoff of {settings.BUY_RANK_CUTOFF}")
    if any(flag["flag"] == "earnings_imminent" for flag in flags):
        watch.append("earnings inside the window")
    if watch:
        row["action_label"] = "HOLD (watch)"
        row["reasons"].extend(watch)


def incumbents_from_review(review: pd.DataFrame) -> tuple[list[str], dict[str, int]]:
    """Holdings the portfolio keeps, best rank first, and caps for those being trimmed."""
    kept = review[review["action"] != "SELL"].copy()
    kept["_rank"] = kept["rank"].fillna(10**9)
    kept = kept.sort_values(["_rank", "ticker"])
    trims = kept[kept["action"] == "TRIM"]
    caps = {row.ticker: int(row.target_weight_pct) for row in trims.itertuples()}
    return kept["ticker"].tolist(), caps
