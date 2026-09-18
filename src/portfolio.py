import math

import pandas as pd

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