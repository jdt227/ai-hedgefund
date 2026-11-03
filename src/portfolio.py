import math

import pandas as pd

from config import settings
from config.settings import (
    MAX_POSITION_WEIGHT,
    MAX_SECTOR_WEIGHT,
    STARTING_CAPITAL,
    TOP_N_HOLDINGS,
    UNIVERSE,
)


def build_portfolio(
    scored_features: pd.DataFrame,
    capital: float = STARTING_CAPITAL,
) -> pd.DataFrame:
    """
    Select the highest-ranked stocks while enforcing:
    - Max number of holdings
    - Max position weight
    - Max sector weight

    Selected positions receive equal weights, subject to max position weight.
    """
    candidates = scored_features.copy()
    candidates["sector"] = candidates.index.map(UNIVERSE)

    selected_rows = []
    sector_weights = {}

    target_weight = min(1 / TOP_N_HOLDINGS, MAX_POSITION_WEIGHT)

    for ticker, row in candidates.iterrows():
        sector = row["sector"]

        if len(selected_rows) >= TOP_N_HOLDINGS:
            break

        current_sector_weight = sector_weights.get(sector, 0.0)

        if current_sector_weight + target_weight > MAX_SECTOR_WEIGHT:
            continue

        selected_rows.append(
            {
                "ticker": ticker,
                "sector": sector,
                "price": row["price"],
                "long_momentum": row["long_momentum"],
                "short_momentum": row["short_momentum"],
                "annualized_volatility": row["annualized_volatility"],
                "composite_score": row["composite_score"],
                "target_weight": target_weight,
            }
        )

        sector_weights[sector] = current_sector_weight + target_weight

    portfolio = pd.DataFrame(selected_rows)

    if portfolio.empty:
        raise ValueError("No stocks passed the portfolio constraints.")

    portfolio["target_dollars"] = portfolio["target_weight"] * capital
    portfolio["shares"] = (
        portfolio["target_dollars"] / portfolio["price"]
    ).apply(math.floor)
    portfolio["actual_dollars"] = portfolio["shares"] * portfolio["price"]
    portfolio["actual_weight"] = portfolio["actual_dollars"] / capital

    cash_remaining = capital - portfolio["actual_dollars"].sum()
    portfolio["cash_remaining_after_trade"] = cash_remaining

    return portfolio.sort_values(
        "composite_score", ascending=False
    ).reset_index(drop=True)


def summarize_sector_weights(portfolio: pd.DataFrame) -> pd.DataFrame:
    """Aggregate the selected portfolio by sector."""
    return (
        portfolio.groupby("sector", as_index=False)["actual_weight"]
        .sum()
        .sort_values("actual_weight", ascending=False)
        .rename(columns={"actual_weight": "sector_weight"})
    )


REJECTED_COLUMNS = ["ticker", "sector", "rank", "final_score", "reason"]


def whole_percent_weights(equity_pct: int, holdings: int) -> list[int]:
    """Split whole percentage points across holdings, largest weights first."""
    base, extra = divmod(equity_pct, holdings)
    return [base + 1] * extra + [base] * (holdings - extra)


def holdings_needed(equity_pct: int) -> int:
    """Smallest allowed holdings count that fits the equity under the position cap."""
    limits = settings.limits()
    for count in range(limits["min_holdings"], limits["max_holdings"] + 1):
        if count * limits["max_position_pct"] >= equity_pct:
            return count
    return limits["max_holdings"]


def _sector_ok(tickers: list[str], weights: list[int], sectors: dict[str, str]) -> bool:
    totals: dict[str, int] = {}
    for ticker, weight in zip(tickers, weights):
        totals[sectors[ticker]] = totals.get(sectors[ticker], 0) + weight
    return all(total <= settings.limits()["max_sector_pct"] for total in totals.values())


def build_competition_portfolio(
    scored: pd.DataFrame,
    cash_target_pct: int | None = None,
    vetoed: dict[str, str] | None = None,
    sectors: dict[str, str] = UNIVERSE,
    capital: float = STARTING_CAPITAL,
    incumbents: list[str] | None = None,
    weight_caps: dict[str, int] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """
    Select by final_score under the class rules and the active strategy limits.

    Returns (portfolio, rejected candidates, rule failures). Only universe stocks
    can be selected. When too few names survive, cash rises rather than a limit
    being broken, and any shortfall is reported as a failure.

    With ``incumbents``, those holdings are kept first and open slots go only to
    new names within BUY_RANK_CUTOFF.
    """
    limits = settings.limits()
    if incumbents is not None:
        return _holdings_aware(scored, cash_target_pct, vetoed or {}, sectors, capital, incumbents, weight_caps or {})
    max_sector = limits["max_sector_pct"]
    vetoed = vetoed or {}
    cash_pct = max(limits["min_cash_pct"], limits["min_cash_pct"] if cash_target_pct is None else int(cash_target_pct))
    equity_pct = min(limits["max_equity_pct"], 100 - cash_pct)
    count = holdings_needed(equity_pct)
    slots = whole_percent_weights(equity_pct, count)

    ranked = scored[scored.index.isin(list(sectors))]
    selected: list[str] = []
    rejected: list[dict] = []
    sector_totals: dict[str, int] = {}

    for rank, (ticker, row) in enumerate(ranked.iterrows(), start=1):
        reason = None
        sector = sectors[ticker]
        if ticker in vetoed:
            reason = f"veto: {vetoed[ticker]}"
        elif len(selected) < count:
            weight = slots[len(selected)]
            if sector_totals.get(sector, 0) + weight > max_sector:
                reason = f"sector limit: {sector} would exceed {max_sector}%"
            else:
                selected.append(ticker)
                sector_totals[sector] = sector_totals.get(sector, 0) + weight
        if reason:
            rejected.append({"ticker": ticker, "sector": sector, "rank": rank,
                             "final_score": row["final_score"], "reason": reason})

    failures: list[str] = []
    if len(selected) < limits["min_holdings"]:
        failures.append(
            f"Only {len(selected)} stocks survived vetoes and limits; the strategy limit requires "
            f"at least {limits['min_holdings']}."
        )
    if len(selected) < settings.CLASS_RULES["min_stocks"]:
        failures.append(
            f"CLASS RULE RISK: only {len(selected)} stocks selected; the class requires at least "
            f"{settings.CLASS_RULES['min_stocks']}."
        )

    # With fewer names than slots, deploy less rather than break a limit.
    invested = min(equity_pct, len(selected) * limits["max_position_pct"])
    while selected and not _sector_ok(selected, whole_percent_weights(invested, len(selected)), sectors):
        invested -= 1
    weights = whole_percent_weights(invested, len(selected)) if selected else []

    portfolio = _portfolio_frame(ranked, selected, weights, sectors, capital)
    return portfolio, pd.DataFrame(rejected, columns=REJECTED_COLUMNS), failures


def _portfolio_frame(ranked: pd.DataFrame, selected: list[str], weights: list[int], sectors: dict[str, str],
                     capital: float) -> pd.DataFrame:
    portfolio = ranked.loc[selected].reset_index().rename(columns={"index": "ticker"})
    portfolio.insert(1, "sector", [sectors[t] for t in selected])
    portfolio["weight_pct"] = weights
    portfolio["target_weight"] = portfolio["weight_pct"] / 100
    portfolio["target_dollars"] = portfolio["target_weight"] * capital
    portfolio["shares"] = (portfolio["target_dollars"] / portfolio["price"]).apply(math.floor)
    portfolio["actual_dollars"] = portfolio["shares"] * portfolio["price"]
    portfolio["actual_weight"] = portfolio["actual_dollars"] / capital
    portfolio["cash_remaining_after_trade"] = capital - portfolio["actual_dollars"].sum()
    return portfolio


def _assign_weights(picks: list[str], equity_pct: int, sectors: dict[str, str],
                    caps: dict[str, int]) -> dict[str, int]:
    """Whole-percent weights in pick order, reduced to fit sector room and trim caps."""
    limits = settings.limits()
    invested = min(equity_pct, len(picks) * limits["max_position_pct"])
    slots = whole_percent_weights(invested, len(picks))
    totals: dict[str, int] = {}
    weights: dict[str, int] = {}
    for ticker, slot in zip(picks, slots):
        room = limits["max_sector_pct"] - totals.get(sectors[ticker], 0)
        weight = min(slot, room, caps.get(ticker, slot))
        if weight >= settings.MIN_TRADE_PCT:
            weights[ticker] = weight
            totals[sectors[ticker]] = totals.get(sectors[ticker], 0) + weight
    return weights


def _holdings_aware(scored: pd.DataFrame, cash_target_pct: int | None, vetoed: dict[str, str],
                    sectors: dict[str, str], capital: float, incumbents: list[str],
                    caps: dict[str, int]) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Keep incumbents, then fill open slots with the best new names within the buy cutoff."""
    limits = settings.limits()
    cash_pct = max(limits["min_cash_pct"], limits["min_cash_pct"] if cash_target_pct is None else int(cash_target_pct))
    equity_pct = min(limits["max_equity_pct"], 100 - cash_pct)
    kept = [t for t in incumbents if t in scored.index][: limits["max_holdings"]]
    target_count = max(holdings_needed(equity_pct), min(len(kept), limits["max_holdings"]))
    all_sectors = {**sectors, **{t: sectors.get(t, "Other") for t in kept}}
    ranked = scored[scored.index.isin(list(sectors))]

    picks = list(kept)
    rejected: list[dict] = []
    for rank, (ticker, row) in enumerate(ranked.iterrows(), start=1):
        if ticker in kept:
            continue
        reason = None
        if ticker in vetoed:
            reason = f"veto: {vetoed[ticker]}"
        elif rank > settings.BUY_RANK_CUTOFF:
            break
        elif len(picks) < target_count:
            trial = _assign_weights([*picks, ticker], equity_pct, all_sectors, caps)
            slot = whole_percent_weights(min(equity_pct, (len(picks) + 1) * limits["max_position_pct"]),
                                         len(picks) + 1)[len(picks)]
            if trial.get(ticker, 0) < slot:
                reason = f"sector limit: {sectors[ticker]} would exceed {limits['max_sector_pct']}%"
            else:
                picks.append(ticker)
        if reason:
            rejected.append({"ticker": ticker, "sector": sectors[ticker], "rank": rank,
                             "final_score": row["final_score"], "reason": reason})

    weights = _assign_weights(picks, equity_pct, all_sectors, caps)
    selected = list(weights)
    failures: list[str] = []
    if len(selected) < limits["min_holdings"]:
        failures.append(f"Only {len(selected)} stocks fit; the strategy limit requires at least {limits['min_holdings']}.")
    if len(selected) < settings.CLASS_RULES["min_stocks"]:
        failures.append(f"CLASS RULE RISK: only {len(selected)} stocks selected; the class requires at least "
                        f"{settings.CLASS_RULES['min_stocks']}.")
    source = scored.loc[selected].sort_values("final_score", ascending=False)
    ordered = [t for t in selected]
    portfolio = _portfolio_frame(source, ordered, [weights[t] for t in ordered], all_sectors, capital)
    return portfolio, pd.DataFrame(rejected, columns=REJECTED_COLUMNS), failures
