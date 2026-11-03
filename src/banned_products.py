"""Detect products the class bans: options, crypto, and leveraged or inverse funds.

Used by the validator, holdings review, and trade list. Name patterns apply only
to funds and unknown types, so a company such as "Ultra Clean Holdings" is not
flagged. A symbol the guard cannot classify is not banned; provider data is used
where it exists.
"""

from __future__ import annotations

import re

from config import settings

# OCC format (e.g. AAPL261218C00200000) and common display forms ("AAPL 200C").
_OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$|^[A-Z.]{1,6}\s+.*\d+[CP]$")
_CRYPTO_PAIR = re.compile(r"^[A-Z0-9]{2,10}-(USD|USDT|USDC|EUR|BTC)$")
_LEVERAGED_NAME = re.compile(r"(?<![\w.])(-?[1-5](\.\d+)?x|ultra(pro)?|inverse|bear|bull|short)(?![\w])", re.I)
_CRYPTO_NAME = re.compile(r"\b(bitcoin|ethereum|ether|solana|crypto|xrp)\b", re.I)
_BANNED_QUOTE_TYPES = {"OPTION": "options", "CRYPTOCURRENCY": "crypto", "FUTURE": "futures"}


def banned_reason(ticker: str, name: str | None = None, quote_type: str | None = None) -> str | None:
    """Return why a product is banned by the class rules, or None if it is not."""
    symbol = (ticker or "").strip().upper()
    kind = (quote_type or "").strip().upper()
    if symbol in settings.BANNED_TICKERS:
        return f"{symbol} is on the banned list ({settings.BANNED_TICKERS[symbol]})"
    if kind in _BANNED_QUOTE_TYPES:
        return f"provider lists it as {_BANNED_QUOTE_TYPES[kind]}"
    if _OPTION_SYMBOL.match(symbol):
        return "symbol format is an option contract"
    if _CRYPTO_PAIR.match(symbol):
        return "symbol format is a crypto pair"
    if name and kind != "EQUITY":
        if _LEVERAGED_NAME.search(name):
            return f"name suggests a leveraged or inverse product ({name})"
        if _CRYPTO_NAME.search(name):
            return f"name suggests a crypto product ({name})"
    return None


def is_etf(ticker: str, quote_type: str | None = None) -> bool:
    """True when the provider or the configured list says this is an ETF."""
    if (quote_type or "").strip().upper() == "ETF":
        return True
    return ticker.strip().upper() in settings.ETF_TICKERS


def counts_as_stock(ticker: str, name: str | None = None, quote_type: str | None = None) -> bool:
    """Toward the 5-stock minimum. ETFs and banned products never count."""
    return not is_etf(ticker, quote_type) and banned_reason(ticker, name, quote_type) is None
