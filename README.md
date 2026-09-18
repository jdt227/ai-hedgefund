# AI Hedge Fund

An educational, rules-based paper-trading research system.

## Current milestone

- Downloads daily adjusted prices with yfinance
- Calculates momentum and volatility signals
- Ranks a defined universe of large-cap U.S. stocks
- Builds a constrained portfolio
- Enforces a maximum 8% position weight
- Enforces a maximum 25% sector weight
- Saves daily rankings, holdings, and Markdown reports
- Displays results in a Streamlit dashboard

## Run

```bash
pip install -r requirements.txt
python -m src.run_pipeline
streamlit run app/dashboard.py
```

## Disclaimer

This project is for educational purposes and paper-trading research only.
It is not financial advice and does not execute live trades.
