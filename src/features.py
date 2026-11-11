import numpy as np
import pandas as pd

from config.settings import (
    MOMENTUM_LONG_WINDOW,
    MOMENTUM_SHORT_WINDOW,
    SENTIMENT_Z_CLIP,
    VOLATILITY_WINDOW,
    W_SECTOR_TILT,
    W_SENTIMENT,
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


def add_standardized_scores(
    features: pd.DataFrame,
    cap: float | None = None,
    reference: pd.Index | None = None,
) -> pd.DataFrame:
    """
    Convert different units to comparable z-scores.

    Higher long/short momentum is better.
    Lower volatility is better, so volatility enters negatively.

    Means and standard deviations come from ``reference`` tickers when given, so
    a holding outside the universe can be scored without shifting everyone else.
    ``cap`` clips each z-score to +/- cap so extreme values can't dominate.
    """
    scored = features.copy()
    base = scored if reference is None else scored.loc[scored.index.intersection(reference)]

    for column in [
        "long_momentum",
        "short_momentum",
        "annualized_volatility",
    ]:
        std = base[column].std(ddof=0)

        if std == 0 or pd.isna(std):
            scored[f"{column}_z"] = 0.0
        else:
            scored[f"{column}_z"] = (
                scored[column] - base[column].mean()
            ) / std
        if cap is not None:
            scored[f"{column}_z"] = scored[f"{column}_z"].clip(-cap, cap)

    scored["composite_score"] = (
        0.70 * scored["long_momentum_z"]
        + 0.15 * scored["short_momentum_z"]
        - 0.15 * scored["annualized_volatility_z"]
    )

    return scored.sort_values("composite_score", ascending=False)


def add_final_score(
    scored: pd.DataFrame,
    sectors: dict[str, str],
    sector_tilts: dict[str, float] | None = None,
    sentiment_z: pd.Series | None = None,
    w_sector: float = W_SECTOR_TILT,
    w_sentiment: float = W_SENTIMENT,
    overlays: dict[str, tuple[pd.Series, float]] | None = None,
) -> pd.DataFrame:
    """
    final_score = composite_score + w_sector * sector_tilt + w_sentiment * sentiment_z
                  + sum of weight * value for each overlay

    Overlay values must already be bounded to [-1, 1]. Missing tilts, sentiment,
    and overlay values count as 0, so with no overlay data the final score
    equals the composite score and the ranking does not change.
    """
    out = scored.copy()
    tilts = sector_tilts or {}
    out["sector_tilt"] = [
        float(tilts.get(sectors.get(ticker), 0.0)) for ticker in out.index
    ]
    if sentiment_z is None:
        out["sentiment_z"] = 0.0
    else:
        out["sentiment_z"] = (
            sentiment_z.reindex(out.index)
            .fillna(0.0)
            .clip(-SENTIMENT_Z_CLIP, SENTIMENT_Z_CLIP)
        )
    out["sector_contribution"] = w_sector * out["sector_tilt"]
    out["sentiment_contribution"] = w_sentiment * out["sentiment_z"]
    out["final_score"] = (
        out["composite_score"]
        + out["sector_contribution"]
        + out["sentiment_contribution"]
    )
    for name, (values, weight) in (overlays or {}).items():
        out[f"{name}_value"] = values.reindex(out.index).fillna(0.0).clip(-1, 1)
        out[f"{name}_contribution"] = weight * out[f"{name}_value"]
        out["final_score"] += out[f"{name}_contribution"]
    return out.sort_values("final_score", ascending=False, kind="stable")
