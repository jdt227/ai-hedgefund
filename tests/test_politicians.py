import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from src import politicians
from src.http_utils import FetchResult
from src.overlays.politician import PoliticianOverlay, overlay_value
from src.politicians import amount_midpoint, fetch_house_trades, parse_index, parse_ptr_text, summarize_by_ticker

SAMPLE = (Path(__file__).parent / "fixtures" / "house_ptr_sample.txt").read_text()


def test_parser_keeps_stock_trades_only():
    name, trades = parse_ptr_text(SAMPLE)
    assert name == "Hon. Example Member"
    by = {(t["ticker"], t["side"]) for t in trades}
    assert by == {("XMPA", "BUY"), ("XMP.B", "SELL"), ("XMPD", "BUY")}  # option, bond, exchange skipped
    xmpa = next(t for t in trades if t["ticker"] == "XMPA")
    assert xmpa["traded"] == date(2029, 12, 1) and xmpa["amount_range"] == "$1,001 - $15,000"


@pytest.mark.parametrize("text,mid", [("$1,001 - $15,000", 8000.5), ("Over $50,000,000", 50_000_000.0), ("n/a", None)])
def test_amount_midpoints(text, mid):
    value = amount_midpoint(text)
    assert (pd.isna(value) if mid is None else value == mid)


def index_zip(rows, year=2029):
    header = "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID\n"
    body = "".join(f"\t{last}\t{first}\t\t{kind}\tZZ01\t{year}\t{filed}\t{doc}\n" for last, first, kind, filed, doc in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{year}FD.txt", header + body)
    return buf.getvalue()


def test_index_keeps_ptrs():
    data = index_zip([("Member", "Example", "P", "12/20/2029", "20000001"), ("Other", "Annual", "A", "5/1/2029", "1")])
    frame = parse_index(data, 2029)
    assert frame["doc_id"].tolist() == ["20000001"] and frame.at[0, "filed"] == date(2029, 12, 20)


def test_fetch_uses_filing_window_and_skips_scanned(monkeypatch):
    rows = [("Member", "Example", "P", "12/20/2029", "20000001"),
            ("Member", "Scanned", "P", "12/21/2029", "90000002"),
            ("Member", "Late", "P", "1/20/2030", "20000003"),
            ("Member", "Early", "P", "6/01/2029", "20000004")]
    monkeypatch.setattr(politicians, "fetch", lambda *a, **k: FetchResult("ok", index_zip(rows), "r"))
    seen = []
    monkeypatch.setattr(politicians, "_pdf_text", lambda doc, year, run: seen.append(doc) or SAMPLE)
    data = fetch_house_trades(date(2029, 12, 31), 90)
    assert seen == ["20000001"]
    assert "1 electronic filings parsed, 1 scanned filings unparsed" in data.status["house_ptr"]
    assert set(data.trades["lag_days"]) == {19, 18, 16}  # filed 12/20 vs traded 12/1, 12/2, 12/4


def test_missing_index_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(politicians, "fetch", lambda *a, **k: FetchResult("error", message="down"))
    data = fetch_house_trades(date(2029, 12, 31), 90)
    assert data.trades.empty and "unavailable" in data.status["house_ptr"]


def trades(rows):
    return pd.DataFrame([{"ticker": t, "side": s, "traded": date(2029, 12, 1), "filed": f, "lag_days": 10,
                          "amount_range": "$1,001 - $15,000", "amount_mid": 8000.5, "member": m, "doc_id": d}
                         for t, s, f, m, d in rows])


def test_summary_counts_distinct_members_and_drops_post_cutoff():
    frame = trades([("XMPA", "BUY", date(2029, 12, 10), "A", "1"), ("XMPA", "BUY", date(2029, 12, 11), "A", "2"),
                    ("XMPA", "BUY", date(2029, 12, 12), "B", "3"), ("XMPA", "SELL", date(2029, 12, 13), "C", "4"),
                    ("XMPA", "BUY", date(2030, 1, 5), "D", "5")])
    row = summarize_by_ticker(frame, date(2029, 12, 31)).set_index("ticker").loc["XMPA"]
    assert row["buyers"] == 2 and row["sellers"] == 1 and row["latest_filing"] == "2029-12-13"


@pytest.mark.parametrize("buyers,sellers,value", [(0, 0, 0.0), (1, 0, 1 / 3), (0, 1, -1 / 3), (10, 0, 10 / 12), (3, 3, 0.0)])
def test_value_is_shrunk_and_bounded(buyers, sellers, value):
    assert overlay_value(buyers, sellers) == pytest.approx(value)
    assert -1 <= overlay_value(buyers, sellers) <= 1


def test_overlay_rows_have_public_dates_and_evidence():
    frame = trades([("XMPA", "BUY", date(2029, 12, 10), "A", "1"), ("ZZZ", "BUY", date(2029, 12, 10), "A", "1")])
    out = PoliticianOverlay(frame, settings.W_POLITICIAN).compute(["XMPA"], date(2029, 12, 31))
    assert out["ticker"].tolist() == ["XMPA"] and out.at[0, "available_at"] == "2029-12-10"
    assert "ranges only" in out.at[0, "evidence"] and "filing lag" in out.at[0, "evidence"]


def test_pipeline_applies_and_labels_the_overlay(monkeypatch):
    from src import run_pipeline
    from src.build_weight_prompt import firm_section
    from src.politicians import HouseData
    from src.untrusted import UntrustedBlocks
    from test_weekly_pipeline import NOW, RUN, universe_prices
    house = trades([("AAPL", "BUY", date(2030, 1, 10), "A", "1"), ("AAPL", "BUY", date(2030, 1, 11), "B", "2")])
    monkeypatch.setattr(run_pipeline, "fetch_house_trades",
                        lambda *a: HouseData(house, {"house_ptr": "ok: 2 electronic filings parsed"}))
    evidence = run_pipeline.build_run(universe_prices(), RUN, NOW)
    assert evidence.final_scores.at["AAPL", "politician_contribution"] == pytest.approx(settings.W_POLITICIAN * 0.5)
    assert evidence.final_scores.loc[evidence.final_scores.index != "AAPL", "politician_contribution"].eq(0).all()
    monkeypatch.setattr(settings, "PROMPT_TOP_CANDIDATES", 33)
    text = firm_section(evidence, UntrustedBlocks("T"))
    assert "Congress (House only; public filing dates; up to 45-day lag" in text
    assert "no disclosed stock trades" in text


def test_toggle_off_skips_the_source(monkeypatch):
    from src import run_pipeline
    from test_weekly_pipeline import NOW, RUN, universe_prices

    def boom(*a):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(run_pipeline, "fetch_house_trades", boom)
    monkeypatch.setattr(settings, "USE_POLITICIAN_OVERLAY", False)
    evidence = run_pipeline.build_run(universe_prices(), RUN, NOW)
    assert evidence.source_status["politician"] == "disabled"
