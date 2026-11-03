"""Macro data, a rules-based regime call, and its two bounded outputs.

Macro is an overlay, not a stock picker. It only sets:
  * the target cash level (never below the active profile's minimum), and
  * small per-sector score tilts.

Sources: FRED API (needs a free key) and sector ETFs / SPY from yfinance.
Without FRED data the module reports "skipped": cash stays at the profile minimum and all
tilts are zero, which leaves the price-only ranking unchanged.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import pandas as pd

from config import settings
from src.http_utils import fetch

logger = logging.getLogger(__name__)

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FED_RSS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
FINNHUB_MARKET_NEWS_URL = "https://finnhub.io/api/v1/news"
FED_SOURCE = "Federal Reserve press releases"


@dataclass
class Indicator:
    name: str
    series_id: str
    source: str
    value: float | None
    observation_date: str | None
    change_1m: float | None
    change_3m: float | None
    retrieved_at: str | None
    units: str = ""


@dataclass
class MacroResult:
    status: str  # ok | partial | skipped | disabled
    message: str = ""
    indicators: dict[str, Indicator] = field(default_factory=dict)
    spy_vs_200dma: float | None = None
    spy_date: str | None = None
    sector_etf_3m: dict[str, float] = field(default_factory=dict)
    regime: str | None = None
    regime_score: int | None = None
    dimensions: dict[str, dict] = field(default_factory=dict)
    cash_target_pct: int = field(default_factory=lambda: settings.limits()["min_cash_pct"])
    sector_tilts: dict[str, float] = field(default_factory=dict)
    headlines: list[dict] = field(default_factory=list)
    source_status: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def parse_fred_observations(payload: dict, run_date: date) -> pd.Series:
    """Turn a FRED response into a float series. '.' means missing; future rows are dropped."""
    rows = {}
    for obs in payload.get("observations", []):
        value = obs.get("value")
        if value in (None, "."):
            continue
        day = pd.Timestamp(obs["date"])
        if day.date() <= run_date:
            rows[day] = float(value)
    return pd.Series(rows, dtype=float).sort_index()


def change_over(series: pd.Series, days: int) -> float | None:
    """Latest value minus the last value at least ``days`` before it."""
    if series.empty:
        return None
    earlier = series.loc[: series.index[-1] - timedelta(days=days)]
    return None if earlier.empty else float(series.iloc[-1] - earlier.iloc[-1])


def cpi_yoy_series(index_levels: pd.Series) -> pd.Series:
    """Year-over-year percent change of a monthly index, by calendar month.

    Unpublished months (e.g. Oct 2025) must stay as gaps. Comparing by row count
    would silently span 13 months and overstate inflation.
    """
    monthly = index_levels.asfreq("MS")
    return (monthly.pct_change(12, fill_method=None) * 100).dropna()


def build_indicator(name: str, series_id: str, series: pd.Series, retrieved_at: str | None,
                    source: str = "FRED", units: str = "") -> Indicator:
    if series.empty:
        return Indicator(name, series_id, source, None, None, None, None, retrieved_at, units)
    return Indicator(name, series_id, source, float(series.iloc[-1]), str(series.index[-1].date()),
                     change_over(series, 28), change_over(series, 88), retrieved_at, units)


def fetch_fred_indicators(api_key: str, run_date: date) -> tuple[dict[str, Indicator], list[str]]:
    """Fetch every configured series. Per-series failures are returned, not raised."""
    indicators: dict[str, Indicator] = {}
    failures: list[str] = []
    start = run_date - timedelta(days=settings.MACRO_HISTORY_DAYS)
    for name, series_id in settings.FRED_SERIES.items():
        # observation_end stops any value dated after the run.
        params = {"series_id": series_id, "api_key": api_key, "file_type": "json",
                  "observation_start": start.isoformat(), "observation_end": run_date.isoformat()}
        result = fetch("fred", FRED_URL, params=params, run_date=run_date,
                       min_interval=settings.MIN_INTERVAL_SECONDS["fred"])
        if not result.ok:
            failures.append(f"{series_id}: {result.message or result.status}")
            continue
        series = parse_fred_observations(result.data, run_date)
        if name == "cpi_index":
            indicators["cpi_yoy"] = build_indicator("cpi_yoy", series_id, cpi_yoy_series(series),
                                                    result.retrieved_at, units="% YoY")
        elif name == "initial_claims":
            indicator = build_indicator(name, series_id, series, result.retrieved_at, units="claims")
            earlier = series.loc[: series.index[-1] - timedelta(days=88)] if len(series) else series
            if len(earlier) and earlier.iloc[-1]:
                indicator.change_3m = float((series.iloc[-1] / earlier.iloc[-1] - 1) * 100)
                indicator.units = "claims; 3M change in %"
            indicators[name] = indicator
        else:
            indicators[name] = build_indicator(name, series_id, series, result.retrieved_at)
    return indicators, failures


def market_proxies(prices: pd.DataFrame) -> tuple[float | None, str | None, dict[str, float]]:
    """SPY vs its 200-day average, and 3-month return per sector from its ETF."""
    spy_gap, spy_date = None, None
    if "SPY" in prices and prices["SPY"].notna().sum() >= 200:
        spy = prices["SPY"].dropna()
        spy_gap = float(spy.iloc[-1] / spy.iloc[-200:].mean() - 1)
        spy_date = str(spy.index[-1].date())
    returns = {}
    for sector, etf in settings.SECTOR_ETFS.items():
        if etf in prices and prices[etf].notna().sum() > 63:
            series = prices[etf].dropna()
            returns[sector] = float(series.iloc[-1] / series.iloc[-64] - 1)
    return spy_gap, spy_date, returns


def download_market_proxies(now_utc: datetime) -> pd.DataFrame:
    """Completed-session adjusted closes for SPY and the sector ETFs."""
    import yfinance as yf

    from src.market_data.provider import expected_last_completed_session

    tickers = ["SPY", *settings.SECTOR_ETFS.values()]
    prices = yf.download(tickers=tickers, period="2y", auto_adjust=True, progress=False,
                         group_by="column")["Close"]
    prices.index = pd.DatetimeIndex(prices.index).tz_localize(None)
    last = expected_last_completed_session(now_utc)
    return prices.loc[prices.index.date <= last].dropna(how="all").sort_index()


# ---------------------------------------------------------------------------
# Regime rules (deterministic)
# ---------------------------------------------------------------------------
def _value(indicators: dict[str, Indicator], name: str) -> float | None:
    item = indicators.get(name)
    return None if item is None else item.value


def _change_3m(indicators: dict[str, Indicator], name: str) -> float | None:
    item = indicators.get(name)
    return None if item is None else item.change_3m


def score_growth(indicators: dict[str, Indicator]) -> tuple[int, str]:
    t = settings.REGIME_THRESHOLDS
    claims = _change_3m(indicators, "initial_claims")
    unemployment = _change_3m(indicators, "unemployment")
    if claims is None and unemployment is None:
        return 0, "no data: claims and unemployment trends unavailable"
    facts = (f"claims 3M {'n/a' if claims is None else f'{claims:+.1f}%'}, "
             f"unemployment 3M {'n/a' if unemployment is None else f'{unemployment:+.2f} pts'}")
    if (claims is not None and claims >= t["claims_3m_change_pct_bad"]) or (
            unemployment is not None and unemployment >= t["unemployment_3m_change_bad"]):
        return -1, f"weakening labor market ({facts})"
    if claims is not None and claims <= t["claims_3m_change_pct_good"] and (
            unemployment is None or unemployment <= 0):
        return 1, f"improving labor market ({facts})"
    return 0, f"stable labor market ({facts})"


def score_inflation(indicators: dict[str, Indicator]) -> tuple[int, str]:
    t = settings.REGIME_THRESHOLDS
    yoy = _value(indicators, "cpi_yoy")
    if yoy is None:
        return 0, "no data: CPI unavailable"
    trend = _change_3m(indicators, "cpi_yoy")
    facts = f"CPI {yoy:.1f}% YoY, 3M change {'n/a' if trend is None else f'{trend:+.2f} pts'}"
    if yoy >= t["cpi_yoy_high"] and (trend is None or trend >= 0):
        return -1, f"high and not falling ({facts})"
    if yoy <= t["cpi_yoy_low"]:
        return 1, f"contained ({facts})"
    return 0, f"moderate or easing ({facts})"


def score_conditions(indicators: dict[str, Indicator]) -> tuple[int, str]:
    t = settings.REGIME_THRESHOLDS
    spread = _value(indicators, "high_yield_spread")
    vix = _value(indicators, "vix")
    curve = _value(indicators, "curve_10y_2y")
    if spread is None and vix is None:
        return 0, "no data: credit spread and VIX unavailable"
    facts = (f"HY spread {'n/a' if spread is None else f'{spread:.2f}'}, "
             f"VIX {'n/a' if vix is None else f'{vix:.1f}'}, "
             f"10Y-2Y {'n/a' if curve is None else f'{curve:+.2f}'}")
    if (spread is not None and spread >= t["hy_spread_stress"]) or (
            vix is not None and vix >= t["vix_stress"]):
        return -1, f"stressed ({facts})"
    calm = ((spread is None or spread <= t["hy_spread_calm"])
            and (vix is None or vix <= t["vix_calm"]))
    if calm and (curve is None or curve >= 0):
        return 1, f"easy ({facts})"
    return 0, f"neutral ({facts})"


def score_trend(spy_vs_200dma: float | None) -> tuple[int, str]:
    if spy_vs_200dma is None:
        return 0, "no data: SPY trend unavailable"
    if spy_vs_200dma > 0:
        return 1, f"SPY {spy_vs_200dma:+.1%} vs 200-day average"
    return -1, f"SPY {spy_vs_200dma:+.1%} vs 200-day average"


def classify_regime(indicators: dict[str, Indicator],
                    spy_vs_200dma: float | None) -> tuple[str, int, dict[str, dict]]:
    """Sum four -1/0/+1 dimension scores into risk_on, neutral, or risk_off."""
    scores = {
        "growth": score_growth(indicators),
        "inflation": score_inflation(indicators),
        "financial_conditions": score_conditions(indicators),
        "market_trend": score_trend(spy_vs_200dma),
    }
    total = sum(score for score, _ in scores.values())
    if total >= settings.REGIME_RISK_ON_MIN_SCORE:
        label = "risk_on"
    elif total <= settings.REGIME_RISK_OFF_MAX_SCORE:
        label = "risk_off"
    else:
        label = "neutral"
    return label, total, {k: {"score": s, "reason": r} for k, (s, r) in scores.items()}


def cash_target(regime: str | None) -> int:
    """Target cash percent for a regime. The active profile's minimum always holds."""
    minimum = settings.limits()["min_cash_pct"]
    if regime is None:
        return minimum
    return max(minimum, settings.regime_cash_table()[regime])


def sector_tilts(regime: str, etf_returns: dict[str, float]) -> dict[str, float]:
    """Regime table plus ETF relative strength, clipped to the configured limit."""
    table = settings.REGIME_SECTOR_TILT[regime]
    tilts: dict[str, float] = {}
    ranks = pd.Series(etf_returns, dtype=float).rank(pct=False)
    spread = (len(ranks) - 1) or 1
    for sector in settings.DEFENSIVE_SECTORS + settings.CYCLICAL_SECTORS:
        base = table["defensive"] if sector in settings.DEFENSIVE_SECTORS else table["cyclical"]
        strength = 0.0
        if sector in ranks.index and len(ranks) > 1:
            strength = (2 * (ranks[sector] - 1) / spread - 1) * settings.SECTOR_RS_TILT_RANGE
        limit = settings.SECTOR_TILT_LIMIT
        tilts[sector] = round(float(max(-limit, min(limit, base + strength))), 4)
    return tilts


# ---------------------------------------------------------------------------
# Headlines (context only, not scored)
# ---------------------------------------------------------------------------
def parse_fed_rss(xml_text: str, now_utc: datetime, retrieved_at: str | None) -> list[dict]:
    items = []
    for item in ET.fromstring(xml_text).iter("item"):
        raw_date = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published > now_utc:
            continue
        items.append({"source": FED_SOURCE, "headline": item.findtext("title") or "",
                      "url": item.findtext("link") or "", "published_at": published.isoformat(),
                      "retrieved_at": retrieved_at})
    return items


def select_macro_headlines(items: list[dict], limit: int) -> list[dict]:
    """Pick headlines that matter for macro: Fed releases first, then keyword matches.

    General market news is mostly unrelated to macro, and taking the newest items
    would push out a days-old FOMC statement.
    """
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, settings.MACRO_HEADLINE_KEYWORDS)) + r")\b", re.I)
    relevant = [i for i in items if pattern.search(i["headline"])]
    fed = [i for i in relevant if i["source"] == FED_SOURCE][: settings.PROMPT_MAX_FED_HEADLINES]
    others = [i for i in relevant if i["source"] != FED_SOURCE]
    return (fed + others)[:limit]


def fetch_macro_headlines(run_date: date, now_utc: datetime, finnhub_key: str | None) -> tuple[list[dict], dict]:
    """Fed RSS and Finnhub market news, newest first, within the news lookback."""
    status: dict[str, str] = {}
    items: list[dict] = []
    fed = fetch("fed", FED_RSS_URL, run_date=run_date, as_text=True)
    status["fed_rss"] = fed.status if fed.ok else f"unavailable: {fed.message or fed.status}"
    if fed.ok:
        try:
            items += parse_fed_rss(fed.data, now_utc, fed.retrieved_at)
        except ET.ParseError as exc:
            status["fed_rss"] = f"unavailable: bad XML ({exc})"
    if finnhub_key:
        market = fetch("finnhub", FINNHUB_MARKET_NEWS_URL, run_date=run_date,
                       params={"category": "general", "token": finnhub_key},
                       min_interval=settings.MIN_INTERVAL_SECONDS["finnhub"])
        status["finnhub_market_news"] = market.status if market.ok else f"unavailable: {market.message or market.status}"
        if market.ok and isinstance(market.data, list):
            for row in market.data:
                stamp = row.get("datetime")
                if not stamp:
                    continue
                published = datetime.fromtimestamp(stamp, timezone.utc)
                if published <= now_utc:
                    items.append({"source": row.get("source") or "Finnhub", "headline": row.get("headline") or "",
                                  "url": row.get("url") or "", "published_at": published.isoformat(),
                                  "retrieved_at": market.retrieved_at})
    else:
        status["finnhub_market_news"] = "skipped: FINNHUB_API_KEY not set"
    oldest = now_utc - timedelta(days=settings.NEWS_LOOKBACK_DAYS)
    recent = [i for i in items if i["headline"] and datetime.fromisoformat(i["published_at"]) >= oldest]
    recent.sort(key=lambda i: i["published_at"], reverse=True)
    return recent, status


# ---------------------------------------------------------------------------
# Entry point and report
# ---------------------------------------------------------------------------
def run_macro(run_date: date, now_utc: datetime, fred_key: str | None, finnhub_key: str | None,
              prices: pd.DataFrame | None = None) -> MacroResult:
    """Build the macro result. Any missing source degrades it; nothing raises."""
    if not settings.USE_MACRO_REGIME:
        return MacroResult("disabled", "USE_MACRO_REGIME is False")
    headlines, source_status = fetch_macro_headlines(run_date, now_utc, finnhub_key)
    if not fred_key:
        source_status["fred"] = "skipped: FRED_API_KEY not set"
        return MacroResult("skipped", "FRED_API_KEY not set: no regime call, minimum cash, zero tilts.",
                           headlines=headlines, source_status=source_status)
    indicators, failures = fetch_fred_indicators(fred_key, run_date)
    source_status["fred"] = "ok" if not failures else "; ".join(failures)
    if not indicators:
        return MacroResult("skipped", "No FRED data available: no regime call, minimum cash, zero tilts.",
                           headlines=headlines, source_status=source_status)

    if prices is None:
        try:
            prices = download_market_proxies(now_utc)
            source_status["yfinance_proxies"] = "ok"
        except Exception as exc:  # the provider raises many error types
            logger.warning("Market proxies unavailable: %s", exc)
            source_status["yfinance_proxies"] = f"unavailable: {type(exc).__name__}"
            prices = pd.DataFrame()
    else:
        source_status["yfinance_proxies"] = "ok"
    spy_gap, spy_date, etf_returns = market_proxies(prices)

    regime, score, dimensions = classify_regime(indicators, spy_gap)
    return MacroResult(
        "partial" if failures or spy_gap is None else "ok",
        "; ".join(failures), indicators, spy_gap, spy_date, etf_returns, regime, score, dimensions,
        cash_target(regime), sector_tilts(regime, etf_returns), headlines, source_status)


def render_macro_report(macro: MacroResult) -> str:
    """Numbers and the regime call in Markdown. Headlines are added by the caller."""
    if macro.regime is None:
        return f"Macro data unavailable for this run. {macro.message}".strip()
    lines = [
        f"**Regime: {macro.regime}** (score {macro.regime_score:+d}; "
        f"risk_on at {settings.REGIME_RISK_ON_MIN_SCORE:+d} or more, "
        f"risk_off at {settings.REGIME_RISK_OFF_MAX_SCORE:+d} or less)",
        f"Target cash: {macro.cash_target_pct}%.",
        "",
        "Regime reasons:",
    ]
    lines += [f"- {name}: {d['score']:+d}, {d['reason']}" for name, d in macro.dimensions.items()]
    lines += ["", "| Indicator | Value | Observation date | 1M change | 3M change | Source | Retrieved |",
              "|---|---:|---|---:|---:|---|---|"]
    for item in macro.indicators.values():
        fmt = lambda v: "n/a" if v is None else f"{v:,.2f}"  # noqa: E731
        lines.append(f"| {item.name} ({item.units or 'level'}) | {fmt(item.value)} | {item.observation_date or 'n/a'} | "
                     f"{fmt(item.change_1m)} | {fmt(item.change_3m)} | {item.source} {item.series_id} | "
                     f"{item.retrieved_at or 'n/a'} |")
    if macro.spy_vs_200dma is not None:
        lines += ["", f"SPY vs 200-day average: {macro.spy_vs_200dma:+.1%} as of {macro.spy_date} (yfinance)."]
    if macro.sector_tilts:
        lines += ["", "Sector tilts (z-units) and sector ETF 3-month return (yfinance):"]
        for sector, tilt in sorted(macro.sector_tilts.items(), key=lambda kv: -kv[1]):
            ret = macro.sector_etf_3m.get(sector)
            lines.append(f"- {sector}: {tilt:+.2f} ({settings.SECTOR_ETFS[sector]} "
                         f"{'n/a' if ret is None else f'{ret:+.1%}'})")
    return "\n".join(lines)
