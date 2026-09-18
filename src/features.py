import numpy as np
import pandas as pd

from config.settings import (
    MOMENTUM_LONG_WINDOW,
    MOMENTUM_SHORT_WINDOW,
    VOLATILITY_WINDOW,
)


def calculate_features(prices: pd.DataFrame) -> pd.DataFrame:
    """
    Produce one latest-date row per ticker.

    Signals:
    - Long momentum: 6-month return excluding the most recent month.
    - Short momentum: most recent 1-month return.
    - Annualized volatility: trailing 63-trading-day volatility.
    """
    daily_returns = prices.pct_change()

    long_momentum = (
        prices.shift(MOMENTUM_SHORT_WINDOW)
        / prices.shift(MOMENTUM_LONG_WINDOW)
        - 1
    )

    short_momentum = (
        prices / prices.shift(MOMENTUM_SHORT_WINDOW) - 1
    )

    annualized_volatility = (
        daily_returns.rolling(VOLATILITY_WINDOW).std() * np.sqrt(252)
    )

    latest = pd.DataFrame(
        {
            "price": prices.iloc[-1],
            "long_momentum": long_momentum.iloc[-1],
            "short_momentum": short_momentum.iloc[-1],
            "annualized_volatility": annualized_volatility.iloc[-1],
        }
    )

    latest.index.name = "ticker"

    latest = latest.replace([np.inf, -np.inf], np.nan).dropna()

    return latest


def add_standardized_scores(features: pd.DataFrame) -> pd.DataFrame:
    """
    Convert different units to comparable z-scores.

    Higher long/short momentum is better.
    Lower volatility is better, so volatility enters negatively.
    """
    scored = features.copy()

    for column in [
        "long_momentum",
        "short_momentum",
        "annualized_volatility",
    ]:
        std = scored[column].std(ddof=0)

        if std == 0 or pd.isna(std):
            scored[f"{column}_z"] = 0.0
        else:
            scored[f"{column}_z"] = (
                scored[column] - scored[column].mean()
            ) / std

    scored["composite_score"] = (
        0.70 * scored["long_momentum_z"]
        + 0.15 * scored["short_momentum_z"]
        - 0.15 * scored["annualized_volatility_z"]
    )

    return scored.sort_values("composite_score", ascending=False)