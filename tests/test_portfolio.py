import pandas as pd

from src.portfolio import build_portfolio, summarize_sector_weights


def test_portfolio_respects_position_and_sector_caps():
    scored_features = pd.DataFrame(
        {
            "price": [100, 100, 100, 100, 100, 100],
            "long_momentum": [0.50, 0.45, 0.40, 0.35, 0.30, 0.25],
            "short_momentum": [0.10, 0.09, 0.08, 0.07, 0.06, 0.05],
            "annualized_volatility": [0.20, 0.21, 0.22, 0.23, 0.24, 0.25],
            "composite_score": [3, 2, 1, 0, -1, -2],
        },
        index=["AAPL", "MSFT", "NVDA", "JPM", "JNJ", "XOM"],
    )

    portfolio = build_portfolio(scored_features)
    sector_weights = summarize_sector_weights(portfolio)

    assert portfolio["actual_weight"].max() <= 0.08
    assert sector_weights["sector_weight"].max() <= 0.25
    assert len(portfolio) <= 10