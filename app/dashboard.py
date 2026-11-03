import json
from pathlib import Path
import sys

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.data import load_price_data


PORTFOLIO_PATH = Path("data/processed/latest_portfolio.csv")
SECTOR_PATH = Path("data/processed/latest_sector_weights.csv")
RUNS_DIR = Path("runs")


st.set_page_config(
    page_title="AI Hedge Fund Dashboard",
    page_icon="📈",
    layout="wide",
)

st.title("📈 AI Hedge Fund: Rules-Based Paper Portfolio")
st.caption(
    "Educational research dashboard. This is not investment advice."
)

if not PORTFOLIO_PATH.exists():
    st.warning(
        "No portfolio output exists yet. Run `python -m src.run_pipeline` "
        "from the repository root first."
    )
    st.stop()

portfolio = pd.read_csv(PORTFOLIO_PATH)
sector_weights = pd.read_csv(SECTOR_PATH)
if portfolio.empty:
    st.error("The latest run could not build a portfolio. See its report for the rule failure.")
    st.stop()
prices = load_price_data()


def latest_evidence() -> Path | None:
    """Newest run folder that has an evidence snapshot."""
    folders = sorted(RUNS_DIR.glob("*/evidence"))
    return folders[-1] if folders else None


def read_json(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


evidence_dir = latest_evidence()
portfolio_tab, macro_tab, news_tab = st.tabs(["Portfolio", "Macro", "News"])

with macro_tab:
    macro = read_json(evidence_dir / "macro.json") if evidence_dir else None
    if not macro or not macro.get("regime"):
        st.info(macro["message"] if macro and macro.get("message")
                else "No macro data yet. Set FRED_API_KEY and run the pipeline.")
    else:
        col1, col2, col3 = st.columns(3)
        col1.metric("Regime", macro["regime"])
        col2.metric("Regime score", f"{macro['regime_score']:+d}")
        col3.metric("Target cash", f"{macro['cash_target_pct']}%")
        st.subheader("Why")
        for name, detail in macro["dimensions"].items():
            st.write(f"**{name}** ({detail['score']:+d}): {detail['reason']}")
        st.subheader("Key indicators")
        st.dataframe(pd.DataFrame(macro["indicators"].values()), use_container_width=True, hide_index=True)
        st.subheader("Sector tilts")
        tilts = pd.Series(macro["sector_tilts"], name="tilt").rename_axis("sector").reset_index()
        st.dataframe(tilts.sort_values("tilt", ascending=False), use_container_width=True, hide_index=True)
    if macro and macro.get("headlines"):
        st.subheader("Recent headlines")
        st.dataframe(pd.DataFrame(macro["headlines"]), use_container_width=True, hide_index=True)

with news_tab:
    articles_path = evidence_dir / "news_articles.csv" if evidence_dir else None
    sentiment_path = evidence_dir / "sentiment.csv" if evidence_dir else None
    articles = pd.read_csv(articles_path) if articles_path and articles_path.exists() else pd.DataFrame()
    if sentiment_path and sentiment_path.exists() and not articles.empty:
        st.subheader("Sentiment by ticker")
        st.dataframe(pd.read_csv(sentiment_path), use_container_width=True, hide_index=True)
    catalysts = read_json(evidence_dir / "catalysts.json") if evidence_dir else None
    if catalysts:
        flagged = [
            {"ticker": ticker, "flag": item["flag"], "reason": item["reason"]}
            for ticker, items in catalysts["flags"].items() for item in items
        ]
        if flagged:
            st.subheader("Catalyst flags")
            st.dataframe(pd.DataFrame(flagged), use_container_width=True, hide_index=True)
    if articles.empty:
        st.info("News data unavailable for the latest run.")
    else:
        st.subheader("Headlines")
        ticker = st.selectbox("Ticker", sorted(articles["ticker"].unique()))
        columns = ["published_at", "source", "headline", "vader", "url"]
        st.dataframe(articles.loc[articles["ticker"] == ticker, columns], use_container_width=True, hide_index=True)

with portfolio_tab:
    total_invested = portfolio["actual_dollars"].sum()
    cash_remaining = portfolio["cash_remaining_after_trade"].iloc[0]
    number_of_holdings = len(portfolio)

    col1, col2, col3 = st.columns(3)
    col1.metric("Holdings", number_of_holdings)
    col2.metric("Capital invested", f"${total_invested:,.0f}")
    col3.metric("Cash remaining", f"${cash_remaining:,.0f}")

    st.divider()

    st.subheader("Current Proposed Holdings")

    display_columns = [
        "ticker",
        "sector",
        "price",
        "long_momentum",
        "short_momentum",
        "annualized_volatility",
        "composite_score",
        "actual_weight",
        "shares",
        "actual_dollars",
    ]

    st.dataframe(
        portfolio[display_columns],
        use_container_width=True,
        hide_index=True,
        column_config={
            "price": st.column_config.NumberColumn("Price", format="$%.2f"),
            "long_momentum": st.column_config.NumberColumn(
                "6M Momentum ex. 1M", format="%.2f%%"
            ),
            "short_momentum": st.column_config.NumberColumn(
                "1M Momentum", format="%.2f%%"
            ),
            "annualized_volatility": st.column_config.NumberColumn(
                "Annualized Volatility", format="%.2f%%"
            ),
            "composite_score": st.column_config.NumberColumn(
                "Composite Score", format="%.3f"
            ),
            "actual_weight": st.column_config.NumberColumn(
                "Portfolio Weight", format="%.2f%%"
            ),
            "actual_dollars": st.column_config.NumberColumn(
                "Position Value", format="$%.2f"
            ),
        },
    )

    st.subheader("Sector Allocation")

    sector_chart = px.bar(
        sector_weights,
        x="sector",
        y="sector_weight",
        text_auto=".1%",
        labels={
            "sector": "Sector",
            "sector_weight": "Portfolio Weight",
        },
    )

    sector_chart.update_layout(yaxis_tickformat=".0%")
    st.plotly_chart(sector_chart, use_container_width=True)

    st.subheader("Price History: Portfolio Names vs. SPY")

    selected_tickers = portfolio["ticker"].tolist() + ["SPY"]
    available_tickers = [ticker for ticker in selected_tickers if ticker in prices.columns]

    normalized_prices = (
        prices[available_tickers]
        .dropna()
        .div(prices[available_tickers].dropna().iloc[0])
        * 100
    )

    st.line_chart(normalized_prices)

    st.caption(
        "Lines are normalized to 100 at the first available date. "
        "This chart compares price paths, not a historical portfolio backtest."
    )