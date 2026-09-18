#The universe includes multiple companies from the same sector on purpose.
# The portfolio construction code will need to reject otherwise highly ranked stocks once a sector reaches its cap.

STARTING_CAPITAL = 100_000
BENCHMARK = "SPY"
TOP_N_HOLDINGS = 10
MAX_POSITION_WEIGHT = 0.08
MAX_SECTOR_WEIGHT = 0.25

MOMENTUM_LONG_WINDOW = 126
MOMENTUM_SHORT_WINDOW = 21
VOLATILITY_WINDOW = 63

UNIVERSE = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "NVDA": "Technology",
    "AVGO": "Technology",
    "ORCL": "Technology",
    "GOOGL": "Communication Services",
    "META": "Communication Services",
    "NFLX": "Communication Services",
    "AMZN": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary",
    "MCD": "Consumer Discretionary",
    "WMT": "Consumer Staples",
    "COST": "Consumer Staples",
    "PG": "Consumer Staples",
    "KO": "Consumer Staples",
    "JPM": "Financials",
    "BAC": "Financials",
    "GS": "Financials",
    "V": "Financials",
    "JNJ": "Health Care",
    "LLY": "Health Care",
    "UNH": "Health Care",
    "ABBV": "Health Care",
    "XOM": "Energy",
    "CVX": "Energy",
    "CAT": "Industrials",
    "GE": "Industrials",
    "LIN": "Materials",
    "NEE": "Utilities",
}