import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from src.catalysts import analyst_trend, build_flags, parse_8k_filings, parse_earnings, run_catalysts, vetoes
from src.http_utils import FetchResult
from src.news import (_av_ticker_score, aggregate_sentiment, dedupe_articles, fetch_alpha_vantage_scores,
                      filter_window, score_headlines, zscore_clip)

FIXTURES = Path(__file__).parent / "fixtures"
RUN = date(2030, 1, 15)
NOW = datetime(2030, 1, 15, 21, 0, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIXTURES / name).read_text())


def article(ticker="XMP", when="2030-01-14T12:00:00+00:00", headline="ok", url="", vader=0.0):
    return {"ticker": ticker, "published_at": when, "source": "s", "headline": headline, "summary": "",
            "url": url, "retrieved_at": "r", "vader": vader}


# --- dedupe and look-ahead ---------------------------------------------------
def test_dedupe_by_url_and_normalized_headline():
    rows = pd.DataFrame([
        article(headline="Big news!", url="u1"),
        article(headline="big news", url="u2"),
        article(headline="Other", url="u1"),
        article(headline="Different", url=""),
        article(ticker="ZZZ", headline="Big news!", url="u1"),
    ])
    out = dedupe_articles(rows)
    assert sorted(zip(out["ticker"], out["headline"])) == [("XMP", "Big news!"), ("XMP", "Different"), ("ZZZ", "Big news!")]


def test_look_ahead_guard_drops_future_and_stale_items():
    rows = pd.DataFrame([
        article(headline="now", when="2030-01-15T20:00:00+00:00"),
        article(headline="future", when="2030-01-15T22:00:00+00:00"),
        article(headline="old", when="2030-01-01T00:00:00+00:00"),
    ])
    assert filter_window(rows, NOW, 7)["headline"].tolist() == ["now"]


def test_vader_scores_direction():
    frame = pd.DataFrame({"headline": ["great excellent win", "terrible awful disaster"]})
    scores = score_headlines(frame)
    assert scores[0] > 0 > scores[1]


# --- aggregation -------------------------------------------------------------
def test_no_news_scores_neutral_not_negative():
    out = aggregate_sentiment(pd.DataFrame(columns=["ticker", "published_at", "vader"]), ["AAA", "BBB"], NOW)
    assert out["score"].eq(0).all() and out["sentiment_z"].eq(0).all() and out["article_count"].eq(0).all()


def test_few_articles_count_for_less_than_many():
    one = [article(ticker="AAA", vader=0.8)]
    many = [article(ticker="BBB", vader=0.8) for _ in range(20)]
    out = aggregate_sentiment(pd.DataFrame(one + many), ["AAA", "BBB"], NOW)
    assert 0 < out.at["AAA", "score"] < out.at["BBB", "score"] < 0.8


def test_recent_news_outweighs_old_news():
    rows = [article(ticker="AAA", when="2030-01-15T20:00:00+00:00", vader=0.8),
            article(ticker="AAA", when="2030-01-08T22:00:00+00:00", vader=-0.8)]
    assert aggregate_sentiment(pd.DataFrame(rows), ["AAA"], NOW).at["AAA", "vader_shrunk"] > 0


def test_alpha_vantage_is_blended():
    rows = [article(ticker="AAA", vader=0.5)]
    without = aggregate_sentiment(pd.DataFrame(rows), ["AAA"], NOW).at["AAA", "score"]
    blended = aggregate_sentiment(pd.DataFrame(rows), ["AAA"], NOW, {"AAA": 0.9}).at["AAA", "score"]
    assert blended == pytest.approx(0.5 * without + 0.5 * 0.9)


def test_sentiment_z_is_clipped():
    values = pd.Series([100.0] + [0.0] * 30)
    z = zscore_clip(values)
    assert z.max() == settings.SENTIMENT_Z_CLIP
    assert z.min() >= -settings.SENTIMENT_Z_CLIP


def test_zscore_of_constant_values_is_zero():
    assert zscore_clip(pd.Series([1.0, 1.0])).eq(0).all()


# --- Alpha Vantage -----------------------------------------------------------
def test_av_score_is_relevance_weighted_for_that_ticker_only():
    assert _av_ticker_score(load("alpha_vantage_news.json")["feed"], "XMP") == pytest.approx(0.3)
    assert _av_ticker_score([], "XMP") is None


def test_av_limit_note_stops_calls_and_is_not_cached(monkeypatch):
    calls, removed = [], []
    monkeypatch.setattr("src.news.fetch", lambda *a, **k: calls.append(1) or FetchResult(
        "ok", {"Information": "rate limit reached"}, "r"))
    monkeypatch.setattr("src.news.remove_cached", lambda *a, **k: removed.append(1))
    scores, status = fetch_alpha_vantage_scores(["AAA", "BBB"], RUN, "key")
    assert scores == {} and len(calls) == 1 and removed == [1] and "limit" in status


def test_av_premium_response_is_reported_clearly(monkeypatch):
    monkeypatch.setattr("src.news.fetch", lambda *a, **k: FetchResult(
        "ok", {"Information": "This is a premium endpoint."}, "r"))
    monkeypatch.setattr("src.news.remove_cached", lambda *a, **k: None)
    scores, status = fetch_alpha_vantage_scores(["AAA"], RUN, "key")
    assert scores == {} and "premium endpoint" in status and status.startswith("unavailable")


def test_av_respects_top_n_budget(monkeypatch):
    calls = []
    monkeypatch.setattr("src.news.fetch", lambda *a, **k: calls.append(k["params"]["tickers"]) or FetchResult(
        "ok", {"feed": []}, "r"))
    fetch_alpha_vantage_scores([f"T{i}" for i in range(30)], RUN, "key")
    assert len(calls) == settings.ALPHA_VANTAGE_TOP_N


# --- catalysts ---------------------------------------------------------------
def test_8k_parser_filters_window_and_translates_items():
    filings = parse_8k_filings(load("sec_submissions.json"), RUN, 14)
    assert [f["accession"] for f in filings] == ["good", "bad"]
    assert "results of operations (earnings)" in filings[0]["items_plain"]


def test_analyst_trend_measures_change():
    rows = [r for r in load("finnhub_recommendation.json") if r["period"] <= RUN.isoformat()]
    assert analyst_trend(rows)["bull_change"] == -3
    assert analyst_trend(rows[:1]) is None


def test_earnings_picks_next_date_for_known_tickers():
    payload = {"earningsCalendar": [{"symbol": "XMP", "date": "2030-02-01"}, {"symbol": "XMP", "date": "2030-01-20"},
                                    {"symbol": "XMP", "date": "2030-01-01"}, {"symbol": "ZZZ", "date": "2030-01-16"}]}
    assert parse_earnings(payload, {"XMP"}, RUN) == {"XMP": "2030-01-20"}


def flags_of(**kwargs):
    base = dict(ticker="XMP", sentiment=0.0, article_count=10, earnings_date=None, filings=[], trend=None, run_date=RUN)
    base.update(kwargs)
    return {f["flag"] for f in build_flags(**base)}


def test_negative_catalyst_needs_sentiment_and_downgrade_or_bad_filing():
    down = {"bull_change": -3, "period": "b", "previous_period": "a"}
    assert "negative_catalyst" in flags_of(sentiment=-0.5, trend=down)
    assert "negative_catalyst" not in flags_of(sentiment=-0.5)
    assert "negative_catalyst" not in flags_of(sentiment=0.1, trend=down)
    assert "negative_catalyst" in flags_of(filings=[{"items": ["4.02"]}])
    assert "negative_catalyst" not in flags_of(filings=[{"items": ["2.02"]}])


def test_positive_earnings_and_coverage_flags():
    up = {"bull_change": 3, "period": "b", "previous_period": "a"}
    assert "positive_catalyst" in flags_of(sentiment=0.5, trend=up)
    assert "earnings_imminent" in flags_of(earnings_date="2030-01-20")
    assert "earnings_imminent" not in flags_of(earnings_date="2030-03-01")
    assert "low_news_coverage" in flags_of(article_count=1)
    assert "low_news_coverage" not in flags_of(article_count=1, news_available=False)


def test_every_flag_has_a_reason():
    items = build_flags("XMP", 0.0, 0, "2030-01-16", [{"items": ["1.03"]}], None, RUN)
    assert len(items) == 3 and all(i["reason"] for i in items)


def test_veto_logic_follows_config(monkeypatch):
    flags = {"AAA": [{"flag": "negative_catalyst", "reason": "bad"}], "BBB": [{"flag": "earnings_imminent", "reason": "soon"}]}
    assert set(vetoes(flags)) == {"AAA"}
    monkeypatch.setattr(settings, "HARD_VETO_FLAGS", ["earnings_imminent"])
    assert set(vetoes(flags)) == {"BBB"}
    monkeypatch.setattr(settings, "HARD_VETO_FLAGS", [])
    assert vetoes(flags) == {}


def test_catalysts_without_keys_report_skips_and_make_no_calls(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network expected")
    monkeypatch.setattr("src.catalysts.fetch", boom)
    res = run_catalysts(["XMP"], None, RUN, None, None)
    assert all(s.startswith("skipped") for s in res.source_status.values())
    assert vetoes(res.flags) == {}


def test_forbidden_recommendations_endpoint_is_skipped(monkeypatch):
    monkeypatch.setattr("src.catalysts.fetch", lambda url, *a, **k: FetchResult("forbidden", message="HTTP 403"))
    res = run_catalysts(["XMP"], None, RUN, "key", None)
    assert "unavailable" in res.source_status["finnhub_recommendations"]
    assert "unavailable" in res.source_status["finnhub_earnings"]
