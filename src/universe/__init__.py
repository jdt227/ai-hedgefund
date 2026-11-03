"""The stock universe: a dated S&P 500 snapshot, or the original 33-stock list.

Tickers inside the pipeline use the class-game format (BRK.B). Yahoo uses a
different format (BRK-B), so conversion happens only at the download boundary.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from config import settings

SP500_PATH = Path(__file__).resolve().parents[2] / "config" / "universe_sp500.csv"
COLUMNS = ["ticker", "yahoo_symbol", "game_symbol", "name", "sector", "as_of", "source"]


def load_sp500(path: Path = SP500_PATH) -> pd.DataFrame:
    """Load the dated constituents snapshot."""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [column for column in COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing columns: {missing}")
    return frame[COLUMNS]


def get_universe() -> dict[str, str]:
    """Ticker to sector for the active UNIVERSE_MODE."""
    if settings.UNIVERSE_MODE == "legacy33":
        return dict(settings.UNIVERSE)
    if settings.UNIVERSE_MODE == "sp500":
        frame = load_sp500()
        return dict(zip(frame["ticker"], frame["sector"]))
    raise ValueError(f"Unknown UNIVERSE_MODE: {settings.UNIVERSE_MODE}")


def yahoo_symbol(ticker: str) -> str:
    """Yahoo's symbol for a game-format ticker, such as BRK-B for BRK.B."""
    return ticker.replace(".", "-")


def game_symbol(symbol: str) -> str:
    """Game-format ticker for a Yahoo symbol, such as BRK.B for BRK-B."""
    return symbol.replace("-", ".")


def company_names() -> dict[str, str]:
    """Ticker to company name, when the snapshot is in use."""
    if settings.UNIVERSE_MODE != "sp500":
        return {}
    frame = load_sp500()
    return dict(zip(frame["ticker"], frame["name"]))
