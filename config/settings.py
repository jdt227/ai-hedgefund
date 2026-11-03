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
    "AMD": "Technology",  # added 2026-11-03 because held; see docs/strategy_changelog.md
    "MU": "Technology",  # added 2026-11-03 because held
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
    "MRK": "Health Care",  # added 2026-11-03 because held
    "XOM": "Energy",
    "CVX": "Energy",
    "CAT": "Industrials",
    "GE": "Industrials",
    "LIN": "Materials",
    "NEE": "Utilities",
}

# ---------------------------------------------------------------------------
# News + macro (src/macro.py, src/news.py, src/catalysts.py)
# ---------------------------------------------------------------------------
# With all three toggles False, src.run_pipeline picks match the price-only model.
USE_MACRO_REGIME = True
USE_NEWS_SENTIMENT = True
USE_CATALYST_FLAGS = True

# final_score = composite_score + W_SECTOR_TILT * sector_tilt + W_SENTIMENT * sentiment_z
W_SECTOR_TILT = 0.15
W_SENTIMENT = 0.10
SENTIMENT_Z_CLIP = 2.0

# Competition rules (source of truth: src/create_submission_placeholders.py).
MIN_HOLDINGS = 8
MAX_HOLDINGS = 10
MIN_CASH_PCT = 20
MAX_EQUITY_PCT = 80
MAX_POSITION_PCT = 8
MAX_SECTOR_PCT = 25
MIN_BUY_ORDERS_PER_MONTH = 5  # tracked and reported only; does not affect selection

NEWS_LOOKBACK_DAYS = 7
EARNINGS_WINDOW_DAYS = 7
SEC_LOOKBACK_DAYS = 14
MACRO_HISTORY_DAYS = 900  # CPI YoY needs 12 months before the 3-month trend
MIN_ARTICLES_FOR_COVERAGE = 3

# Sentiment: recency half-life, shrinkage toward 0 by n / (n + k), and the
# share given to Alpha Vantage when a ticker has a score from it.
SENTIMENT_HALF_LIFE_DAYS = 3.0
SENTIMENT_SHRINKAGE_K = 5
ALPHA_VANTAGE_BLEND = 0.5
NEGATIVE_SENTIMENT_THRESHOLD = -0.25
POSITIVE_SENTIMENT_THRESHOLD = 0.25
# Decline in (strongBuy + buy) analysts between the two latest monthly snapshots.
ANALYST_TREND_CHANGE_THRESHOLD = 2

# Flags listed here remove a stock before selection. All others are only reported.
HARD_VETO_FLAGS = ["negative_catalyst"]

# Free-tier limits and our call budgets per run.
# Finnhub: 60 calls/min, confirmed from X-Ratelimit-Limit on a free key (2026-11-03).
#   A 403 skips that endpoint.
# Alpha Vantage: 25 requests/day (alphavantage.co/support), but NEWS_SENTIMENT
#   answered "premium endpoint" on a free key on 2026-11-03, so expect it to skip.
# SEC EDGAR: at most 10 requests/second (sec.gov/about/developer-resources).
# FRED: no published number; terms prohibit unreasonable use, so stay well below 1/s.
MIN_INTERVAL_SECONDS = {"finnhub": 1.1, "alphavantage": 1.5, "sec": 0.2, "fred": 0.6, "fed": 0.0}
ALPHA_VANTAGE_TOP_N = 15
ALPHA_VANTAGE_DAILY_BUDGET = 25

# FRED series: name -> series ID.
FRED_SERIES = {
    "treasury_10y": "DGS10",
    "treasury_2y": "DGS2",
    "curve_10y_2y": "T10Y2Y",
    "fed_funds": "FEDFUNDS",
    "cpi_index": "CPIAUCSL",
    "unemployment": "UNRATE",
    "initial_claims": "ICSA",
    "high_yield_spread": "BAMLH0A0HYM2",
    "vix": "VIXCLS",
    "trade_weighted_dollar": "DTWEXBGS",
}

# Regime rules. Each dimension scores -1 / 0 / +1; a missing input scores 0.
REGIME_THRESHOLDS = {
    "claims_3m_change_pct_bad": 10.0,      # growth: claims up this much is -1
    "claims_3m_change_pct_good": -5.0,     # growth: claims down this much is +1
    "unemployment_3m_change_bad": 0.3,     # growth: pct-point rise is -1
    "cpi_yoy_high": 3.5,                   # inflation: above this and not falling is -1
    "cpi_yoy_low": 2.5,                    # inflation: below this is +1
    "hy_spread_stress": 5.0,               # conditions: pct points
    "hy_spread_calm": 3.5,
    "vix_stress": 28.0,
    "vix_calm": 18.0,
}
REGIME_RISK_ON_MIN_SCORE = 2
REGIME_RISK_OFF_MAX_SCORE = -2
REGIME_CASH_TARGET_PCT = {"risk_on": 20, "neutral": 25, "risk_off": 35}

# Sector tilt in z-units = regime table + ETF relative strength, clipped to the limit.
SECTOR_TILT_LIMIT = 0.3
SECTOR_RS_TILT_RANGE = 0.1   # strongest 3M ETF gets +this, weakest gets -this
DEFENSIVE_SECTORS = ["Consumer Staples", "Health Care", "Utilities"]
CYCLICAL_SECTORS = [
    "Technology", "Consumer Discretionary", "Financials",
    "Industrials", "Materials", "Energy", "Communication Services",
]
REGIME_SECTOR_TILT = {
    "risk_on": {"defensive": -0.1, "cyclical": 0.1},
    "neutral": {"defensive": 0.0, "cyclical": 0.0},
    "risk_off": {"defensive": 0.2, "cyclical": -0.2},
}
SECTOR_ETFS = {
    "Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Consumer Staples": "XLP",
    "Consumer Discretionary": "XLY",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Communication Services": "XLC",
}

# Weight prompt.
PROMPT_TOP_CANDIDATES = 15
PROMPT_HEADLINES_PER_FIRM = 5
PROMPT_MACRO_HEADLINES = 8
PROMPT_MAX_FED_HEADLINES = 3
MACRO_HEADLINE_KEYWORDS = [
    "Fed", "FOMC", "Federal Reserve", "Powell", "monetary", "interest rate", "interest rates", "rate cut",
    "rate cuts", "rate hike", "inflation", "CPI", "PCE", "jobs", "payrolls", "jobless", "unemployment",
    "recession", "GDP", "Treasury", "Treasuries", "yields", "bond", "bonds", "dollar", "tariff", "tariffs",
    "oil", "shutdown", "midterm", "midterms", "economy", "economic",
]
PROMPT_MAX_TEXT_CHARS = 300
