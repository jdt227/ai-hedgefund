from pathlib import Path
import sys

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.data import load_price_data


PORTFOLIO_PATH = Path("data/processed/latest_portfolio.csv")
SECTOR_PATH = Path("data/processed/latest_sector_weights.csv")


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
prices = load_price_data()

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