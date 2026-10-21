"""Versioned configuration for the quant baseline recommendation pipeline.

Every value that can change a recommendation lives here so that a run can
snapshot it (see ``strategy_snapshot``) and later runs can be compared.

Change STRATEGY_VERSION whenever a value below changes, and add an entry to
docs/strategy_changelog.md describing what changed and why.

The legacy hypothetical $100k pipeline (src/run_pipeline.py) still reads
config/settings.py and is unaffected by this file.
"""

from __future__ import annotations

import hashlib
import json

STRATEGY_VERSION = "quant-baseline-1.0.0"

# ---------------------------------------------------------------------------
# Class / account rules
# ---------------------------------------------------------------------------
CLASS_RULES = {
    # Monthly buy-count rule is evaluated in this timezone.
    "timezone": "America/New_York",
    "min_individual_stocks": 5,
    "min_buy_orders_per_month": 5,
    # Unknown until the professor confirms. While None, ETFs are NOT counted
    # toward the stock minimum (the conservative interpretation).
    "etf_counts_toward_stock_minimum_default": None,
}

# Tickers we know are ETFs even if the data provider cannot classify them.
KNOWN_ETFS = {"SPY", "VOO", "IVV", "QQQ", "VTI", "DIA", "IWM"}

# Holdings that are monitored (priced, signals computed) but never ranked.
MONITOR_ONLY = {"SPY"}
BENCHMARK = "SPY"

# ---------------------------------------------------------------------------
# Candidate universe (v1)
# ---------------------------------------------------------------------------
# Hand-picked, highly liquid, large-cap U.S.-listed common stocks, roughly
# the largest names in each sector as of mid-2026. Sector labels here are
# FALLBACKS; the provider's sector is preferred when available.
#
# Selection-bias warning: this list was chosen today from companies that are
# large *now*. That is fine for forward-looking ranking, but any historical
# backtest on this list suffers survivorship/look-ahead bias (the list
# excludes companies that shrank, were acquired, or failed). Do not use it to
# claim historical outperformance. Dual-class/odd tickers (e.g. BRK.B) are
# excluded to avoid symbol-mapping errors between Yahoo and Investopedia.
UNIVERSE = {
    # Information Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AVGO": "Technology", "ORCL": "Technology", "CRM": "Technology",
    "AMD": "Technology", "ADBE": "Technology", "CSCO": "Technology",
    "ACN": "Technology", "IBM": "Technology", "QCOM": "Technology",
    "TXN": "Technology", "INTU": "Technology", "NOW": "Technology",
    "AMAT": "Technology", "MU": "Technology",
    # Communication Services
    "GOOGL": "Communication Services", "META": "Communication Services",
    "NFLX": "Communication Services", "TMUS": "Communication Services",
    "DIS": "Communication Services", "VZ": "Communication Services",
    "T": "Communication Services",
    # Consumer Discretionary
    "AMZN": "Consumer Cyclical", "TSLA": "Consumer Cyclical",
    "HD": "Consumer Cyclical", "MCD": "Consumer Cyclical",
    "LOW": "Consumer Cyclical", "BKNG": "Consumer Cyclical",
    "TJX": "Consumer Cyclical", "NKE": "Consumer Cyclical",
    "SBUX": "Consumer Cyclical",
    # Consumer Staples
    "WMT": "Consumer Defensive", "COST": "Consumer Defensive",
    "PG": "Consumer Defensive", "KO": "Consumer Defensive",
    "PEP": "Consumer Defensive", "PM": "Consumer Defensive",
    # Financials
    "JPM": "Financial Services", "BAC": "Financial Services",
    "GS": "Financial Services", "V": "Financial Services",
    "MA": "Financial Services", "WFC": "Financial Services",
    "MS": "Financial Services", "AXP": "Financial Services",
    "BLK": "Financial Services", "C": "Financial Services",
    "SCHW": "Financial Services",
    # Health Care
    "LLY": "Healthcare", "UNH": "Healthcare", "JNJ": "Healthcare",
    "ABBV": "Healthcare", "MRK": "Healthcare", "ABT": "Healthcare",
    "TMO": "Healthcare", "ISRG": "Healthcare", "AMGN": "Healthcare",
    "PFE": "Healthcare", "DHR": "Healthcare",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    # Industrials
    "CAT": "Industrials", "GE": "Industrials", "RTX": "Industrials",
    "HON": "Industrials", "UNP": "Industrials", "BA": "Industrials",
    "DE": "Industrials", "LMT": "Industrials",
    # Materials
    "LIN": "Basic Materials", "SHW": "Basic Materials",
    # Utilities
    "NEE": "Utilities", "SO": "Utilities", "DUK": "Utilities",
    # Real Estate
    "PLD": "Real Estate", "AMT": "Real Estate",
}

# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
DATA = {
    "provider": "yfinance",
    "history_period": "2y",          # enough for 252 + 21 sessions + buffer
    # A daily bar is treated as complete this many minutes after the
    # 16:00 America/New_York close.
    "close_buffer_minutes": 30,
    # More missing sessions than this (vs. the expected last completed
    # weekday session) marks data as stale and blocks order sizing.
    "max_stale_sessions": 2,
    "cache_max_age_hours": 12,
}

# ---------------------------------------------------------------------------
# Signals and scoring
# ---------------------------------------------------------------------------
# Each signal: lookback(s) in trading sessions, weight, and direction
# (+1 = higher is better, -1 = lower is better). Weights sum to 1.
SIGNALS = {
    "mom_12_1": {"weight": 0.35, "direction": +1,
                 "desc": "Return from t-252 to t-21 (12-month momentum skipping the latest month)"},
    "mom_6_1": {"weight": 0.25, "direction": +1,
                "desc": "Return from t-126 to t-21 (6-month momentum skipping the latest month)"},
    "trend_200": {"weight": 0.10, "direction": +1,
                  "desc": "Latest adjusted close / 200-session simple moving average - 1"},
    "vol_63": {"weight": 0.15, "direction": -1,
               "desc": "Annualized std. dev. of daily returns over 63 sessions (x sqrt(252))"},
    "max_dd_252": {"weight": 0.15, "direction": +1,
                   "desc": "Max drawdown over 252 sessions (a negative number; closer to 0 is better)"},
}
SCORING = {
    "winsor_z": 3.0,                 # clip z-scores to [-3, 3]
    "min_history_sessions": 273,     # 252 + 21: needed for mom_12_1
    "review_bottom_quantile": 0.20,  # holdings in bottom 20% get a review flag
    "review_top_quantile": 0.20,
    "top_n": 5,
}

ELIGIBILITY = {
    "allowed_quote_types": ["EQUITY"],
    "min_price": 5.0,
    "min_median_dollar_volume_63": 50_000_000,
    "exclude": [],
}

# ---------------------------------------------------------------------------
# Order sizing (only used when official cash + game name + ledger are known)
# ---------------------------------------------------------------------------
SIZING = {
    "max_position_weight": 0.15,
    "max_sector_weight": 0.35,
    "target_new_position_weight": 0.08,
    "min_cash_buffer_weight": 0.02,
    "min_cash_buffer_dollars": 500.0,
    "slippage_buffer": 0.005,         # size buys at price * (1 + 0.5%)
    "assumed_fee_per_trade": 0.0,     # overridden by account metadata when known
    "min_order_dollars": 1_000.0,
    "max_new_buys_per_run": 5,
    "buy_rank_cutoff": 15,            # only candidates ranked <= 15 are bought
    "allow_trim_sells": False,        # never sell automatically in v1
}


def strategy_snapshot() -> dict:
    """Return every recommendation-relevant setting as a JSON-safe dict."""
    return {
        "strategy_version": STRATEGY_VERSION,
        "class_rules": CLASS_RULES,
        "known_etfs": sorted(KNOWN_ETFS),
        "monitor_only": sorted(MONITOR_ONLY),
        "benchmark": BENCHMARK,
        "universe": dict(sorted(UNIVERSE.items())),
        "data": DATA,
        "signals": SIGNALS,
        "scoring": SCORING,
        "eligibility": ELIGIBILITY,
        "sizing": SIZING,
    }


def config_hash(snapshot: dict | None = None) -> str:
    """Short, stable hash of the strategy snapshot for run comparison."""
    payload = json.dumps(snapshot or strategy_snapshot(), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
