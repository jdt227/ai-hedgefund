from dataclasses import dataclass
from datetime import datetime
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

    # Yahoo can return an all-empty row for a session it has not finalized.
    prices = prices.dropna(axis=1, how="all").dropna(axis=0, how="all").sort_index()
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


@dataclass
class MarketData:
    """Completed-session prices, with tickers in game format."""

    prices: pd.DataFrame     # adjusted closes, for return research
    close: pd.DataFrame      # traded closes, for share sizing and dollar volume
    volume: pd.DataFrame
    provider: str
    fetched_at_utc: str
    from_cache: bool
    errors: dict[str, str]


def download_market_data(tickers: list[str], now_utc: datetime) -> MarketData:
    """One batched, cached download for the whole universe plus holdings."""
    from src.market_data.yfinance_provider import YFinanceProvider
    from src.universe import game_symbol, yahoo_symbol

    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    symbols = sorted({yahoo_symbol(t) for t in tickers} | {BENCHMARK})
    bundle = YFinanceProvider(period="2y", with_info=False).fetch(symbols, now_utc)

    def game(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.rename(columns=game_symbol).dropna(axis=1, how="all")

    data = MarketData(game(bundle.adj_close), game(bundle.close), game(bundle.volume), bundle.provider,
                      bundle.fetched_at_utc, bundle.from_cache,
                      {game_symbol(k): v for k, v in bundle.ticker_errors.items()})
    data.prices.index.name = "date"
    data.prices.to_csv(RAW_DATA_DIR / "adjusted_close_prices.csv")
    return data
