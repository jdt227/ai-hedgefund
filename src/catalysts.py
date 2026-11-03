"""Earnings dates, SEC 8-K filings, analyst trends, and catalyst flags.

Flags always carry a reason. ``settings.HARD_VETO_FLAGS`` decides which ones
remove a stock before selection; the rest are reported only.

Sources:
  Finnhub  /calendar/earnings, /stock/recommendation (free key)
  SEC      data.sec.gov/submissions/CIK##########.json (no key; at most 10
           requests/second; needs a descriptive User-Agent with a contact email)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from config import settings
from src.http_utils import fetch

FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
FINNHUB_RECOMMENDATION_URL = "https://finnhub.io/api/v1/stock/recommendation"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# 8-K items, in plain English. BAD_8K_ITEMS can contribute to negative_catalyst.
EIGHT_K_ITEMS = {
    "1.01": "entered a material agreement",
    "1.02": "ended a material agreement",
    "1.03": "bankruptcy or receivership",
    "1.05": "material cybersecurity incident",
    "2.01": "completed an acquisition or disposition",
    "2.02": "results of operations (earnings)",
    "2.03": "new direct financial obligation",
    "2.04": "event accelerating a financial obligation",
    "2.05": "exit or disposal costs (restructuring)",
    "2.06": "material impairment",
    "3.01": "delisting notice or listing-rule issue",
    "3.02": "unregistered sale of equity",
    "3.03": "change to shareholder rights",
    "4.01": "change of auditor",
    "4.02": "prior financial statements should no longer be relied on",
    "5.01": "change in control",
    "5.02": "director or executive change",
    "5.03": "charter or bylaw amendment",
    "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "other material event",
    "9.01": "financial statements and exhibits",
}
BAD_8K_ITEMS = {"1.03", "1.05", "2.04", "2.06", "3.01", "4.02"}


@dataclass
class CatalystResult:
    earnings: dict[str, str] = field(default_factory=dict)        # ticker -> next report date
    filings: dict[str, list[dict]] = field(default_factory=dict)  # ticker -> recent 8-Ks
    analyst: dict[str, dict] = field(default_factory=dict)        # ticker -> trend
    flags: dict[str, list[dict]] = field(default_factory=dict)    # ticker -> {flag, reason}
    source_status: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------
def parse_earnings(payload: dict, tickers: set[str], run_date: date) -> dict[str, str]:
    """Next report date on or after the run date, per ticker."""
    found: dict[str, str] = {}
    for row in (payload or {}).get("earningsCalendar", []):
        symbol, day = row.get("symbol"), row.get("date")
        if symbol in tickers and day and day >= run_date.isoformat():
            if symbol not in found or day < found[symbol]:
                found[symbol] = day
    return found


def parse_8k_filings(payload: dict, run_date: date, lookback_days: int) -> list[dict]:
    """8-K filings inside the lookback window. Filings after the run date are dropped."""
    recent = (payload or {}).get("filings", {}).get("recent", {})
    oldest = (run_date - timedelta(days=lookback_days)).isoformat()
    out = []
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        if form not in ("8-K", "8-K/A"):
            continue
        filed = recent["filingDate"][i]
        if not oldest <= filed <= run_date.isoformat():
            continue
        items = [c.strip() for c in (recent.get("items", [""] * len(forms))[i] or "").split(",") if c.strip()]
        out.append({"form": form, "filed": filed, "items": items,
                    "items_plain": [EIGHT_K_ITEMS.get(c, f"item {c}") for c in items],
                    "accession": recent.get("accessionNumber", [""] * len(forms))[i]})
    return out


def analyst_trend(payload: list[dict] | None) -> dict | None:
    """Change in bullish and bearish analyst counts between the two latest snapshots."""
    if not payload or len(payload) < 2:
        return None
    rows = sorted(payload, key=lambda r: r.get("period", ""), reverse=True)
    latest, previous = rows[0], rows[1]

    def bulls(row):
        return row.get("strongBuy", 0) + row.get("buy", 0)

    def bears(row):
        return row.get("strongSell", 0) + row.get("sell", 0)

    return {"period": latest.get("period"), "previous_period": previous.get("period"),
            "bull_change": bulls(latest) - bulls(previous), "bear_change": bears(latest) - bears(previous)}


def build_flags(ticker: str, sentiment: float | None, article_count: int, earnings_date: str | None,
                filings: list[dict], trend: dict | None, run_date: date,
                news_available: bool = True) -> list[dict]:
    """Rules for the catalyst flags. Each flag has a reason string."""
    flags: list[dict] = []
    threshold = settings.ANALYST_TREND_CHANGE_THRESHOLD
    downgrade = bool(trend) and trend["bull_change"] <= -threshold
    upgrade = bool(trend) and trend["bull_change"] >= threshold
    bad_items = sorted({c for f in filings for c in f["items"] if c in BAD_8K_ITEMS})
    very_negative = sentiment is not None and sentiment <= settings.NEGATIVE_SENTIMENT_THRESHOLD
    very_positive = sentiment is not None and sentiment >= settings.POSITIVE_SENTIMENT_THRESHOLD

    if bad_items:
        plain = "; ".join(EIGHT_K_ITEMS.get(c, c) for c in bad_items)
        flags.append({"flag": "negative_catalyst", "reason": f"recent 8-K: {plain}"})
    elif very_negative and downgrade:
        flags.append({"flag": "negative_catalyst",
                      "reason": f"sentiment {sentiment:+.2f} and {trend['bull_change']:+d} bullish analysts "
                                f"({trend['previous_period']} to {trend['period']})"})
    if very_positive and upgrade:
        flags.append({"flag": "positive_catalyst",
                      "reason": f"sentiment {sentiment:+.2f} and {trend['bull_change']:+d} bullish analysts "
                                f"({trend['previous_period']} to {trend['period']})"})
    if earnings_date:
        days = (date.fromisoformat(earnings_date) - run_date).days
        if 0 <= days <= settings.EARNINGS_WINDOW_DAYS:
            flags.append({"flag": "earnings_imminent", "reason": f"reports {earnings_date} ({days} days)"})
    if news_available and article_count < settings.MIN_ARTICLES_FOR_COVERAGE:
        flags.append({"flag": "low_news_coverage",
                      "reason": f"{article_count} articles in {settings.NEWS_LOOKBACK_DAYS} days"})
    return flags


def vetoes(flags: dict[str, list[dict]]) -> dict[str, str]:
    """Tickers removed before selection, with the reasons."""
    out: dict[str, str] = {}
    for ticker, items in flags.items():
        reasons = [f"{i['flag']}: {i['reason']}" for i in items if i["flag"] in settings.HARD_VETO_FLAGS]
        if reasons:
            out[ticker] = "; ".join(reasons)
    return out


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _fetch_earnings(tickers: list[str], run_date: date, key: str | None, res: CatalystResult) -> None:
    if not key:
        res.source_status["finnhub_earnings"] = "skipped: FINNHUB_API_KEY not set"
        return
    # Look far enough ahead to show the next report date, not only imminent ones.
    params = {"from": run_date.isoformat(), "to": (run_date + timedelta(days=45)).isoformat(), "token": key}
    result = fetch("finnhub", FINNHUB_EARNINGS_URL, params=params, run_date=run_date,
                   min_interval=settings.MIN_INTERVAL_SECONDS["finnhub"])
    res.source_status["finnhub_earnings"] = result.status if result.ok else f"unavailable: {result.message or result.status}"
    if result.ok:
        res.earnings = parse_earnings(result.data, set(tickers), run_date)


def _fetch_analyst(tickers: list[str], run_date: date, key: str | None, res: CatalystResult) -> None:
    if not key:
        res.source_status["finnhub_recommendations"] = "skipped: FINNHUB_API_KEY not set"
        return
    failed = 0
    for ticker in tickers:
        result = fetch("finnhub", FINNHUB_RECOMMENDATION_URL, params={"symbol": ticker, "token": key},
                       run_date=run_date, min_interval=settings.MIN_INTERVAL_SECONDS["finnhub"])
        if not result.ok:
            failed += 1
            if result.status == "forbidden":
                res.source_status["finnhub_recommendations"] = "unavailable: not on this plan"
                return
            continue
        # A snapshot for a future period would be look-ahead.
        rows = [r for r in result.data if r.get("period", "") <= run_date.isoformat()] if isinstance(result.data, list) else []
        trend = analyst_trend(rows)
        if trend:
            res.analyst[ticker] = trend
    res.source_status["finnhub_recommendations"] = f"failed for {failed} tickers" if failed else "ok"


def _fetch_filings(tickers: list[str], run_date: date, user_agent: str | None, res: CatalystResult) -> None:
    if not user_agent:
        res.source_status["sec_edgar"] = "skipped: SEC_USER_AGENT not set"
        return
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    interval = settings.MIN_INTERVAL_SECONDS["sec"]
    mapping = fetch("sec", SEC_TICKERS_URL, headers=headers, run_date=run_date, min_interval=interval)
    if not mapping.ok:
        res.source_status["sec_edgar"] = f"unavailable: {mapping.message or mapping.status}"
        return
    ciks = {row["ticker"]: row["cik_str"] for row in mapping.data.values()}
    missing, failed = [], 0
    for ticker in tickers:
        # SEC's file writes share classes like Yahoo does (BRK-B).
        cik = ciks.get(ticker) or ciks.get(ticker.replace(".", "-"))
        if cik is None:
            missing.append(ticker)
            continue
        result = fetch("sec", SEC_SUBMISSIONS_URL.format(cik=int(cik)), headers=headers,
                       run_date=run_date, min_interval=interval)
        if not result.ok:
            failed += 1
            continue
        res.filings[ticker] = parse_8k_filings(result.data, run_date, settings.SEC_LOOKBACK_DAYS)
    note = "ok" if not failed and not missing else f"failed for {failed}; no CIK for {missing}"
    res.source_status["sec_edgar"] = note


def run_catalysts(tickers: list[str], sentiment: pd.DataFrame | None, run_date: date, finnhub_key: str | None,
                  sec_user_agent: str | None) -> CatalystResult:
    """Fetch every catalyst source and build flags. Missing sources are reported."""
    res = CatalystResult()
    _fetch_earnings(tickers, run_date, finnhub_key, res)
    _fetch_analyst(tickers, run_date, finnhub_key, res)
    _fetch_filings(tickers, run_date, sec_user_agent, res)
    news_available = sentiment is not None and int(sentiment["article_count"].sum()) > 0
    for ticker in tickers:
        has_row = sentiment is not None and ticker in sentiment.index
        res.flags[ticker] = build_flags(
            ticker,
            float(sentiment.at[ticker, "score"]) if has_row else None,
            int(sentiment.at[ticker, "article_count"]) if has_row else 0,
            res.earnings.get(ticker), res.filings.get(ticker, []), res.analyst.get(ticker), run_date,
            news_available)
    return res
