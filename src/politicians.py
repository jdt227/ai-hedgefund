"""House periodic transaction reports (PTRs): fetch, parse, and summarize.

Source: the House Clerk's free financial-disclosure files (verified 2026-11-11).
  index:   https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<YEAR>FD.zip
  filings: https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/<YEAR>/<DocID>.pdf

Rules:
* Only the House is covered. Senate filings are not used.
* Electronic filings (DocID starting with "2") are parsed with pypdf. Scanned
  filings are marked "unparsed"; there is no OCR guessing.
* A trade becomes usable on its FILING date (when it became public), not its
  transaction date. Members may file up to 45 days after a trade.
* Amounts are disclosed as ranges; the midpoint is a rough size only.
* Only stock trades ([ST]) with a ticker count. Options, bonds, funds without a
  ticker, and exchanges are skipped.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from config import settings
from src.http_utils import fetch

logger = logging.getLogger(__name__)

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
PDF_CACHE = Path("data/cache/house_ptr")
HEADERS = {"User-Agent": "ai-hedgefund class project"}

# "<asset name> (TICKER) [ST] P 10/07/2026 10/12/2026 $1,001 - $15,000"
TRADE = re.compile(
    r"\((?P<ticker>[A-Z][A-Z0-9./]{0,6})\)\s*\[(?P<kind>[A-Z]{2})\]\s*"
    r"(?P<side>P|S \(partial\)|S|E)\s+(?P<traded>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+\s*-\s*\$[\d,]+|Over \$[\d,]+)")
NAME = re.compile(r"Name:\s*(?P<name>.+?)\s+Status:")


@dataclass
class HouseData:
    trades: pd.DataFrame
    status: dict[str, str] = field(default_factory=dict)


def amount_midpoint(text: str) -> float:
    """Midpoint of a disclosed range, e.g. '$1,001 - $15,000' -> 8000.5. 'Over $X' -> X."""
    numbers = [float(n.replace(",", "")) for n in re.findall(r"\$([\d,]+)", text)]
    if not numbers:
        return float("nan")
    return numbers[0] if len(numbers) == 1 else (numbers[0] + numbers[1]) / 2


def parse_ptr_text(text: str) -> tuple[str | None, list[dict]]:
    """Member name and stock trades found in one electronic filing's text."""
    flat = re.sub(r"\s+", " ", text)
    name_match = NAME.search(flat)
    trades = []
    for match in TRADE.finditer(flat):
        if match["kind"] != "ST" or match["side"] == "E":
            continue
        trades.append({
            "ticker": match["ticker"].replace("/", "."),
            "side": "BUY" if match["side"] == "P" else "SELL",
            "traded": datetime.strptime(match["traded"], "%m/%d/%Y").date(),
            "amount_range": match["amount"],
            "amount_mid": amount_midpoint(match["amount"]),
        })
    return (name_match["name"].strip() if name_match else None), trades


def parse_index(zip_bytes: bytes, year: int) -> pd.DataFrame:
    """PTR rows from the yearly index: member, filing date, DocID."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        name = f"{year}FD.txt"
        text = archive.read(name).decode("utf-8", errors="replace")
    rows = [r for r in csv.DictReader(io.StringIO(text), delimiter="\t") if r.get("FilingType") == "P"]
    frame = pd.DataFrame([{"doc_id": r["DocID"].strip(), "member": f"{r['First'].strip()} {r['Last'].strip()}",
                           "filed": datetime.strptime(r["FilingDate"].strip(), "%m/%d/%Y").date(), "year": year}
                          for r in rows], columns=["doc_id", "member", "filed", "year"])
    return frame


def _pdf_text(doc_id: str, year: int, run_date: date) -> str | None:
    """Text of one filing. Filings never change, so PDFs are cached permanently."""
    from pypdf import PdfReader

    PDF_CACHE.mkdir(parents=True, exist_ok=True)
    path = PDF_CACHE / f"{year}_{doc_id}.pdf"
    if not path.exists():
        result = fetch("house_ptr", PTR_URL.format(year=year, doc_id=doc_id), headers=HEADERS, as_bytes=True,
                       run_date=run_date, min_interval=settings.MIN_INTERVAL_SECONDS["house"])
        if not result.ok:
            return None
        path.write_bytes(result.data)
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception as exc:  # malformed PDFs are reported, not fatal
        logger.warning("Could not read House filing %s: %s", doc_id, exc)
        return None


def fetch_house_trades(cutoff: date, lookback_days: int) -> HouseData:
    """Stock trades from PTRs filed in (cutoff - lookback, cutoff]."""
    start = cutoff - timedelta(days=lookback_days)
    filings = []
    status: dict[str, str] = {}
    for year in sorted({start.year, cutoff.year}):
        result = fetch("house_index", INDEX_URL.format(year=year), headers=HEADERS, as_bytes=True, run_date=cutoff)
        if not result.ok:
            status[f"house_index_{year}"] = f"unavailable: {result.message or result.status}"
            continue
        filings.append(parse_index(result.data, year))
    if not filings:
        return HouseData(pd.DataFrame(columns=TRADE_COLUMNS), status | {"house_ptr": "unavailable: no index"})
    index = pd.concat(filings)
    window = index[(index["filed"] > start) & (index["filed"] <= cutoff)]
    electronic = window[window["doc_id"].str.startswith("2")]
    scanned = len(window) - len(electronic)
    rows, failed = [], 0
    for filing in electronic.itertuples():
        text = _pdf_text(filing.doc_id, filing.year, cutoff)
        if text is None:
            failed += 1
            continue
        parsed_name, trades = parse_ptr_text(text)
        for trade in trades:
            rows.append({**trade, "member": parsed_name or filing.member, "filed": filing.filed,
                         "doc_id": filing.doc_id, "lag_days": (filing.filed - trade["traded"]).days})
    trades = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    status["house_ptr"] = (f"ok: {len(electronic) - failed} electronic filings parsed, {scanned} scanned filings "
                           f"unparsed, {failed} failed; {len(trades)} stock trades")
    return HouseData(trades, status)


TRADE_COLUMNS = ["ticker", "side", "traded", "filed", "lag_days", "amount_range", "amount_mid", "member", "doc_id"]


def summarize_by_ticker(trades: pd.DataFrame, cutoff: date) -> pd.DataFrame:
    """Per ticker: distinct buyers and sellers, amount midpoints, latest filing, median lag.

    Trades filed after the cutoff are dropped: they were not public yet.
    """
    public = trades[pd.to_datetime(trades["filed"]).dt.date <= cutoff] if len(trades) else trades
    rows = []
    for ticker, group in public.groupby("ticker"):
        buys, sells = group[group["side"] == "BUY"], group[group["side"] == "SELL"]
        rows.append({"ticker": ticker, "buyers": buys["member"].nunique(), "sellers": sells["member"].nunique(),
                     "buy_amount_mid": float(buys["amount_mid"].sum()), "sell_amount_mid": float(sells["amount_mid"].sum()),
                     "latest_filing": str(max(group["filed"])), "median_lag_days": float(group["lag_days"].median()),
                     "filings": ", ".join(sorted(set(group["doc_id"])))})
    return pd.DataFrame(rows, columns=["ticker", "buyers", "sellers", "buy_amount_mid", "sell_amount_mid",
                                       "latest_filing", "median_lag_days", "filings"])
