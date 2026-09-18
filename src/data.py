from pathlib import Path

import pandas as pd
import yfinance as yf

from config.settings import BENCHMARK, UNIVERSE


RAW_DATA_DIR = Path("data/raw")


def download_price_data(start: str = "2020-01-01") -> pd.DataFrame:
    """Download adjusted daily close prices for the universe and benchmark."""
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    tickers = list(UNIVERSE.keys()) + [BENCHMARK]

    prices = yf.download(
        tickers=tickers,
        start=start,
        auto_adjust=True,
        progress=False,
        group_by="column",
    )["Close"]

    prices = prices.dropna(axis=1, how="all").sort_index()
    prices.index.name = "date"

    output_path = RAW_DATA_DIR / "adjusted_close_prices.csv"
    prices.to_csv(output_path)

    return prices


def load_price_data() -> pd.DataFrame:
    """Load the most recently downloaded prices."""
    file_path = RAW_DATA_DIR / "adjusted_close_prices.csv"

    if not file_path.exists():
        raise FileNotFoundError(
            "No price file found. Run download_price_data() first."
        )

    prices = pd.read_csv(file_path, index_col="date", parse_dates=True)
    return prices.sort_index()