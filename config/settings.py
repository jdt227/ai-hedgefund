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
USE_SCOUT_OVERLAY = True
W_SCOUT = 0.05               # scout value is in [-1, 1], so at most +/-0.05 of score

# final_score = composite_score + W_SECTOR_TILT * sector_tilt + W_SENTIMENT * sentiment_z
W_SECTOR_TILT = 0.15
W_SENTIMENT = 0.10
SENTIMENT_Z_CLIP = 2.0

# Universe and funnel. "sp500" uses config/universe_sp500.csv; "legacy33" uses UNIVERSE above.
UNIVERSE_MODE = "sp500"
ELIGIBILITY_MIN_PRICE = 5.0
ELIGIBILITY_MIN_DOLLAR_VOLUME = 50_000_000
ELIGIBILITY_MIN_SESSIONS = 252
FUNNEL_DEEP_N = 50            # top N by score, plus holdings, get news and catalysts
Z_SCORE_CAP = 3.0             # sp500 mode only; legacy33 keeps the original uncapped formula
EXTREME_DAILY_MOVE = 0.40     # a one-day move this large is flagged as a data-quality warning

# Holdings review. New buys need the buy cutoff; incumbents stay until the sell line.
BUY_RANK_CUTOFF = 15
SELL_RANK_THRESHOLD = 75      # top 15% of about 500 stocks
MIN_TRADE_PCT = 1             # ignore weight changes smaller than this many points

# Trade list.
MAX_STALE_SESSIONS = 2            # older reference prices block share quantities
EXPECTED_BUYS_PER_WEEKLY_RUN = 1  # cautious guess for later runs this month, for compliance planning

# Contest metric (realized FF3 alpha). Below the minimum, no estimate is reported.
CONTEST_MIN_OBS = {"daily": 60, "logged intervals": 8}
CONTEST_LOW_CONFIDENCE_OBS = {"daily": 126, "logged intervals": 26}

# Class rules come from the assignment. A failure is a grade risk.
CLASS_RULES = {
    "min_stocks": 5,                    # held at all times; ETFs do not count
    "min_buy_orders_per_month": 5,      # rebalancing buys count
    "month_timezone": "America/New_York",
    "allowed_products": ["equities", "ETFs"],
    "banned_products": ["crypto", "options", "leveraged or inverse products"],
    "game": "trades must be placed in the class game",
    "strategy_changes": "every strategy change requires a submitted form",
    "graded_metric": "Fama-French 3-factor alpha, ranked against the class",
}

# Strategy limits are our own choices, not class rules. Whole-percent weights
# summing to 100 with CASH is a format rule and applies to every profile.
PORTFOLIO_LIMIT_PROFILES = {
    "legacy": {"min_holdings": 8, "max_holdings": 10, "max_position_pct": 8,
               "max_sector_pct": 25, "min_cash_pct": 20, "max_equity_pct": 80},
    "alpha_v1": {"min_holdings": 5, "max_holdings": 10, "max_position_pct": 12,
                 "max_sector_pct": 35, "min_cash_pct": 5, "max_equity_pct": 95},
}
PORTFOLIO_LIMITS = "alpha_v1"

OBJECTIVE_STATEMENT = (
    "Objective: maximize expected FF3 alpha. Returns explained by market, size (SMB), or value "
    "(HML) exposure earn no credit. Momentum and low-volatility returns are not FF3 factors, so "
    "they can show up as alpha. Cash and SPY have roughly zero alpha and scale the portfolio's "
    "alpha down, so hold extra cash only with a stated, evidence-based reason."
)

# ETFs are allowed by the class. Our strategy picks stocks because an index ETF
# adds roughly zero alpha. This is a config choice, not a rule.
ALLOW_ETF_HOLDINGS = False
ETF_TICKERS = {
    "SPY", "VOO", "IVV", "QQQ", "VTI", "DIA", "IWM", "IWB", "IWD", "IWF",
    "XLK", "XLF", "XLE", "XLV", "XLP", "XLY", "XLI", "XLB", "XLU", "XLC", "XLRE",
}
# Maintained list of banned products that name patterns may miss: ticker -> why.
BANNED_TICKERS = {
    **{t: "leveraged ETF" for t in ("TQQQ", "QLD", "SSO", "UPRO", "SPXL", "UDOW", "TNA", "SOXL",
                                    "FNGU", "TECL", "NVDL", "TSLL", "MSTU", "MSTX", "CONL")},
    **{t: "inverse ETF" for t in ("SQQQ", "PSQ", "SH", "SDS", "SPXU", "SPXS", "DOG", "DXD", "SDOW",
                                  "RWM", "TZA", "SOXS", "FNGD", "TECS", "TSLQ", "TSLS", "NVDD")},
    **{t: "crypto ETF" for t in ("IBIT", "FBTC", "GBTC", "BITB", "ARKB", "HODL", "BTCO", "BITO",
                                 "BTC", "ETHA", "ETHE", "FETH", "ETH", "BITX")},
}

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
REGIME_CASH_TARGET_PCT = {"risk_on": 5, "neutral": 5, "risk_off": 15}  # alpha_v1
LEGACY_REGIME_CASH_TARGET_PCT = {"risk_on": 20, "neutral": 25, "risk_off": 35}

# Sector tilt in z-units = regime table + ETF relative strength, clipped to the limit.
SECTOR_TILT_LIMIT = 0.3
SECTOR_RS_TILT_RANGE = 0.1   # strongest 3M ETF gets +this, weakest gets -this
DEFENSIVE_SECTORS = ["Consumer Staples", "Health Care", "Utilities"]
CYCLICAL_SECTORS = [
    "Technology", "Consumer Discretionary", "Financials",
    "Industrials", "Materials", "Energy", "Communication Services",
]
# Real Estate has no regime tilt; sector ETF relative strength still applies.
NEUTRAL_SECTORS = ["Real Estate"]
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
    "Real Estate": "XLRE",
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


def limits() -> dict:
    """Strategy limits for the active profile."""
    return PORTFOLIO_LIMIT_PROFILES[PORTFOLIO_LIMITS]


def regime_cash_table() -> dict[str, int]:
    """Target cash by regime for the active profile."""
    return LEGACY_REGIME_CASH_TARGET_PCT if PORTFOLIO_LIMITS == "legacy" else REGIME_CASH_TARGET_PCT
