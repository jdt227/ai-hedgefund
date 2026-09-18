from datetime import date
from pathlib import Path
import sys

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.data import download_price_data
from src.features import add_standardized_scores, calculate_features
from src.portfolio import build_portfolio, summarize_sector_weights


PROCESSED_DATA_DIR = Path("data/processed")
REPORTS_DIR = Path("reports")


def main() -> None:
    run_date = date.today().isoformat()

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("1/4 Downloading daily prices...")
    prices = download_price_data(start="2020-01-01")

    print("2/4 Calculating momentum and risk features...")
    features = calculate_features(prices)
    scored_features = add_standardized_scores(features)

    print("3/4 Building constrained portfolio...")
    portfolio = build_portfolio(scored_features)
    sector_weights = summarize_sector_weights(portfolio)

    print("4/4 Saving outputs...")

    scored_features.to_csv(
        PROCESSED_DATA_DIR / "latest_stock_rankings.csv"
    )
    portfolio.to_csv(
        PROCESSED_DATA_DIR / "latest_portfolio.csv", index=False
    )
    sector_weights.to_csv(
        PROCESSED_DATA_DIR / "latest_sector_weights.csv", index=False
    )

    report_path = REPORTS_DIR / f"portfolio_report_{run_date}.md"

    total_invested = portfolio["actual_dollars"].sum()
    cash_remaining = portfolio["cash_remaining_after_trade"].iloc[0]

    lines = [
        f"# AI Hedge Fund — Rules-Based Portfolio Report",
        "",
        f"**Run date:** {run_date}",
        f"**Stocks selected:** {len(portfolio)}",
        f"**Capital invested:** ${total_invested:,.2f}",
        f"**Cash remaining:** ${cash_remaining:,.2f}",
        "",
        "## Proposed Holdings",
        "",
        portfolio[
            [
                "ticker",
                "sector",
                "price",
                "composite_score",
                "target_weight",
                "actual_weight",
                "shares",
                "actual_dollars",
            ]
        ].to_markdown(index=False),
        "",
        "## Sector Weights",
        "",
        sector_weights.to_markdown(index=False),
        "",
        "## Method",
        "",
        (
            "Stocks are ranked by a composite of long-term momentum, "
            "short-term momentum, and trailing annualized volatility. "
            "The portfolio applies an 8% maximum position weight, a 25% "
            "sector limit, and a maximum of 10 positions."
        ),
        "",
        "Educational paper-trading research only; not investment advice.",
    ]

    report_path.write_text("\n".join(lines), encoding="utf-8")

    print("\nPortfolio created successfully.")
    print(portfolio[["ticker", "sector", "composite_score", "actual_weight"]])
    print(f"\nSaved report: {report_path}")


if __name__ == "__main__":
    main()