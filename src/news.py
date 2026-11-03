"""Company news and headline sentiment.

Sources: Finnhub company news (primary), yfinance Ticker.news (unofficial,
fallback only), and Alpha Vantage NEWS_SENTIMENT (25 requests/day, so top-ranked
candidates only). Every item keeps its source, publication time, and retrieval
time. Items dated after the run are dropped.

Headline text is untrusted data. It is scored and displayed, never followed.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from config import settings
from src.http_utils import fetch, remove_cached

logger = logging.getLogger(__name__)

FINNHUB_COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"


@dataclass
class NewsResult:
    articles: pd.DataFrame
    sentiment: pd.DataFrame            # index ticker: score, article_count, av_score, sentiment_z
    source_status: dict[str, str] = field(default_factory=dict)


FALLBACK_FAILURE_LIMIT = 3
ARTICLE_COLUMNS = ["ticker", "published_at", "source", "headline", "summary", "url", "retrieved_at"]


def normalize_headline(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def dedupe_articles(articles: pd.DataFrame) -> pd.DataFrame:
    """Keep the earliest report of each story per ticker, by URL and by headline."""
    if articles.empty:
        return articles
    frame = articles.sort_values("published_at", kind="stable").copy()
    frame["_headline"] = frame["headline"].map(normalize_headline)
    has_url = frame["url"].fillna("") != ""
    url_dupes = frame.duplicated(["ticker", "url"]) & has_url
    frame = frame[~url_dupes & ~frame.duplicated(["ticker", "_headline"])]
    return frame.drop(columns="_headline").reset_index(drop=True)


def filter_window(articles: pd.DataFrame, now_utc: datetime, lookback_days: int) -> pd.DataFrame:
    """Look-ahead guard: keep only items published in [now - lookback, now]."""
    if articles.empty:
        return articles
    published = pd.to_datetime(articles["published_at"], utc=True, errors="coerce")
    oldest = now_utc - timedelta(days=lookback_days)
    return articles[(published >= oldest) & (published <= now_utc)].reset_index(drop=True)


def _finnhub_articles(ticker: str, key: str, run_date: date) -> tuple[list[dict], str]:
    start = run_date - timedelta(days=settings.NEWS_LOOKBACK_DAYS)
    result = fetch("finnhub", FINNHUB_COMPANY_NEWS_URL, run_date=run_date,
                   params={"symbol": ticker, "from": start.isoformat(), "to": run_date.isoformat(), "token": key},
                   min_interval=settings.MIN_INTERVAL_SECONDS["finnhub"])
    if not result.ok or not isinstance(result.data, list):
        return [], result.status if not result.ok else "error"
    rows = []
    for item in result.data:
        if not item.get("datetime") or not item.get("headline"):
            continue
        rows.append({"ticker": ticker,
                     "published_at": datetime.fromtimestamp(item["datetime"], timezone.utc).isoformat(),
                     "source": item.get("source") or "Finnhub", "headline": item["headline"],
                     "summary": item.get("summary") or "", "url": item.get("url") or "",
                     "retrieved_at": result.retrieved_at})
    return rows, "ok"


def _yfinance_articles(ticker: str, now_utc: datetime) -> list[dict]:
    """Fallback only. The feed is unofficial and its format changes without notice."""
    import yfinance as yf

    rows = []
    for item in yf.Ticker(ticker).news or []:
        content = item.get("content") or item
        published = content.get("pubDate") or content.get("providerPublishTime")
        title = content.get("title")
        if not published or not title:
            continue
        stamp = (datetime.fromtimestamp(published, timezone.utc) if isinstance(published, (int, float))
                 else pd.Timestamp(published).tz_convert("UTC").to_pydatetime())
        provider = content.get("provider") or {}
        url = (content.get("canonicalUrl") or {}).get("url") or content.get("link") or ""
        rows.append({"ticker": ticker, "published_at": stamp.isoformat(),
                     "source": f"{provider.get('displayName') or content.get('publisher') or 'Yahoo'} (yfinance)",
                     "headline": title, "summary": content.get("summary") or "", "url": url,
                     "retrieved_at": now_utc.isoformat()})
    return rows


def collect_articles(tickers: list[str], run_date: date, now_utc: datetime,
                     finnhub_key: str | None) -> tuple[pd.DataFrame, dict[str, str]]:
    """Gather news per ticker. Failures reduce coverage and are reported."""
    rows: list[dict] = []
    finnhub_failures: dict[str, str] = {}
    fallback_used: list[str] = []
    fallback_failed: list[str] = []
    consecutive_failures = 0
    for ticker in tickers:
        found: list[dict] = []
        if finnhub_key:
            found, status = _finnhub_articles(ticker, finnhub_key, run_date)
            if status != "ok":
                finnhub_failures[ticker] = status
        # Stop after a few straight failures: the unofficial feed is likely down.
        if not found and consecutive_failures < FALLBACK_FAILURE_LIMIT:
            try:
                found = _yfinance_articles(ticker, now_utc)
                consecutive_failures = 0
                if found:
                    fallback_used.append(ticker)
            except Exception as exc:  # the unofficial feed raises many error types
                logger.warning("yfinance news failed for %s: %s", ticker, exc)
                fallback_failed.append(ticker)
                consecutive_failures += 1
        rows += found

    articles = pd.DataFrame(rows, columns=ARTICLE_COLUMNS)
    articles = dedupe_articles(filter_window(articles, now_utc, settings.NEWS_LOOKBACK_DAYS))
    status = {"finnhub_company_news": "skipped: FINNHUB_API_KEY not set"}
    if finnhub_key:
        status["finnhub_company_news"] = (
            f"failed for {len(finnhub_failures)}/{len(tickers)} tickers "
            f"({', '.join(sorted(set(finnhub_failures.values())))})" if finnhub_failures else "ok")
    status["yfinance_news_fallback"] = (f"used for {len(fallback_used)} tickers; "
                                        f"failed for {len(fallback_failed)}"
                                        + ("; stopped early" if consecutive_failures >= FALLBACK_FAILURE_LIMIT else ""))
    return articles, status


def fetch_alpha_vantage_scores(tickers: list[str], run_date: date, key: str | None) -> tuple[dict[str, float], str]:
    """Average relevance-weighted ticker sentiment. Stops at the daily limit."""
    if not key:
        return {}, "skipped: ALPHAVANTAGE_API_KEY not set"
    scores: dict[str, float] = {}
    params_base = {"function": "NEWS_SENTIMENT", "limit": 50,
                   "time_from": (run_date - timedelta(days=settings.NEWS_LOOKBACK_DAYS)).strftime("%Y%m%dT0000"),
                   "time_to": run_date.strftime("%Y%m%dT2359"), "apikey": key}
    requested = tickers[: min(settings.ALPHA_VANTAGE_TOP_N, settings.ALPHA_VANTAGE_DAILY_BUDGET)]
    for ticker in requested:
        params = {**params_base, "tickers": ticker}
        result = fetch("alphavantage", ALPHA_VANTAGE_URL, params=params, run_date=run_date,
                       min_interval=settings.MIN_INTERVAL_SECONDS["alphavantage"])
        if not result.ok:
            return scores, f"stopped after {len(scores)} tickers: {result.message or result.status}"
        data = result.data if isinstance(result.data, dict) else {}
        # Limit errors arrive as HTTP 200 with a note, so they must not stay cached.
        if "feed" not in data:
            remove_cached("alphavantage", ALPHA_VANTAGE_URL, params, run_date)
            note = str(data.get("Information") or data.get("Note") or data.get("Error Message") or "no feed returned")
            if "premium" in note.lower():
                return scores, "unavailable: NEWS_SENTIMENT is a premium endpoint for this key"
            return scores, f"stopped after {len(scores)} tickers: {note[:120]}"
        score = _av_ticker_score(data["feed"], ticker)
        if score is not None:
            scores[ticker] = score
    return scores, f"ok: {len(scores)}/{len(requested)} tickers scored"


def _av_ticker_score(feed: list[dict], ticker: str) -> float | None:
    total, weight = 0.0, 0.0
    for article in feed:
        for row in article.get("ticker_sentiment", []):
            if row.get("ticker") != ticker:
                continue
            try:
                relevance = float(row["relevance_score"])
                total += relevance * float(row["ticker_sentiment_score"])
                weight += relevance
            except (KeyError, TypeError, ValueError):
                continue
    return total / weight if weight > 0 else None


def score_headlines(articles: pd.DataFrame) -> pd.Series:
    """VADER compound score (-1 to +1) for each headline."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    analyzer = SentimentIntensityAnalyzer()
    return articles["headline"].map(lambda text: analyzer.polarity_scores(text)["compound"]).astype(float)


def aggregate_sentiment(articles: pd.DataFrame, tickers: list[str], now_utc: datetime,
                        av_scores: dict[str, float] | None = None) -> pd.DataFrame:
    """Recency-weighted per-ticker score, shrunk toward 0 when coverage is thin.

    score = blend(weighted_mean(VADER) * n/(n+k), Alpha Vantage), and sentiment_z
    is its cross-sectional z-score clipped to +/- SENTIMENT_Z_CLIP. A ticker with
    no news scores exactly 0: neutral, not negative.
    """
    av_scores = av_scores or {}
    rows = {}
    for ticker in tickers:
        group = articles[articles["ticker"] == ticker] if len(articles) else articles
        n = len(group)
        vader = 0.0
        if n:
            published = pd.to_datetime(group["published_at"], utc=True)
            age_days = ((pd.Timestamp(now_utc) - published).dt.total_seconds() / 86400).clip(lower=0)
            weights = 0.5 ** (age_days / settings.SENTIMENT_HALF_LIFE_DAYS)
            mean = float((group["vader"] * weights).sum() / weights.sum())
            vader = mean * n / (n + settings.SENTIMENT_SHRINKAGE_K)
        av = av_scores.get(ticker)
        blend = settings.ALPHA_VANTAGE_BLEND
        score = vader if av is None else (1 - blend) * vader + blend * av
        rows[ticker] = {"score": score, "vader_shrunk": vader, "av_score": av, "article_count": n}
    frame = pd.DataFrame.from_dict(rows, orient="index")
    frame.index.name = "ticker"
    frame["sentiment_z"] = zscore_clip(frame["score"]) if len(frame) else []
    return frame


def zscore_clip(values: pd.Series) -> pd.Series:
    std = values.std(ddof=0)
    if not math.isfinite(std) or std == 0:
        return pd.Series(0.0, index=values.index)
    limit = settings.SENTIMENT_Z_CLIP
    return ((values - values.mean()) / std).clip(-limit, limit)


def run_news(tickers: list[str], av_tickers: list[str], run_date: date, now_utc: datetime,
             finnhub_key: str | None, av_key: str | None) -> NewsResult:
    """Collect articles and compute sentiment for all tickers."""
    articles, status = collect_articles(tickers, run_date, now_utc, finnhub_key)
    if len(articles):
        articles = articles.assign(vader=score_headlines(articles))
    else:
        articles = articles.assign(vader=pd.Series(dtype=float))
    av_scores, status["alpha_vantage"] = fetch_alpha_vantage_scores(av_tickers, run_date, av_key)
    sentiment = aggregate_sentiment(articles, tickers, now_utc, av_scores)
    if not finnhub_key and not av_key:
        # The unofficial fallback alone is not trusted to move scores.
        sentiment[["score", "vader_shrunk", "sentiment_z"]] = 0.0
        status["sentiment_scoring"] = "skipped: no news API key; yfinance headlines shown as context only"
    else:
        status["sentiment_scoring"] = "ok"
    return NewsResult(articles, sentiment, status)
