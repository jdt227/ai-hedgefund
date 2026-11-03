"""Rebuild the S&P 500 snapshot. Shows adds and removes; writes only with --apply.

    python -m src.universe.refresh           # show what would change
    python -m src.universe.refresh --apply   # write config/universe_sp500.csv

Primary source: the free datasets/s-and-p-500-companies CSV. Wikipedia's list is
a cross-check, and any disagreement between them is printed.
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[2]))

from src.http_utils import fetch  # noqa: E402
from src.universe import COLUMNS, SP500_PATH, load_sp500, yahoo_symbol  # noqa: E402

SOURCE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
CROSS_CHECK_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
HEADERS = {"User-Agent": "ai-hedgefund class project"}
GICS_TO_SECTOR = {"Information Technology": "Technology"}
KNOWN_SECTORS = {
    "Technology", "Financials", "Energy", "Health Care", "Consumer Staples", "Consumer Discretionary",
    "Industrials", "Materials", "Utilities", "Communication Services", "Real Estate",
}


def build_snapshot(raw: pd.DataFrame, as_of: date, source: str = SOURCE_URL) -> pd.DataFrame:
    """Convert the source table to the snapshot format. Unknown sectors are an error."""
    sectors = raw["GICS Sector"].map(lambda name: GICS_TO_SECTOR.get(name, name))
    unknown = sorted(set(sectors) - KNOWN_SECTORS)
    if unknown:
        raise ValueError(f"Unmapped GICS sectors: {unknown}")
    tickers = raw["Symbol"].str.strip().str.upper()
    frame = pd.DataFrame({
        "ticker": tickers, "yahoo_symbol": tickers.map(yahoo_symbol), "game_symbol": tickers,
        "name": raw["Security"].str.strip(), "sector": sectors, "as_of": as_of.isoformat(), "source": source,
    })
    return frame[COLUMNS].sort_values("ticker").reset_index(drop=True)


def diff_tickers(old: set[str], new: set[str]) -> tuple[list[str], list[str]]:
    """Return (adds, removes)."""
    return sorted(new - old), sorted(old - new)


def cross_check(snapshot: pd.DataFrame, today: date) -> str:
    """Compare against Wikipedia. A failure here is reported, never fatal."""
    page = fetch("universe", CROSS_CHECK_URL, headers=HEADERS, as_text=True, run_date=today)
    if not page.ok:
        return f"cross-check unavailable: {page.message or page.status}"
    try:
        other = set(pd.read_html(io.StringIO(page.data))[0]["Symbol"].str.upper())
    except (ValueError, KeyError, ImportError) as exc:
        return f"cross-check unavailable: {type(exc).__name__}"
    only_new, only_other = diff_tickers(other, set(snapshot["ticker"]))
    if not only_new and not only_other:
        return f"cross-check ok: Wikipedia lists the same {len(other)} tickers"
    return f"CROSS-CHECK DISAGREES: only in source {only_new}; only on Wikipedia {only_other}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the new snapshot.")
    args = parser.parse_args(argv)
    today = date.today()
    result = fetch("universe", SOURCE_URL, headers=HEADERS, as_text=True, run_date=today)
    if not result.ok:
        print(f"Source unavailable: {result.message or result.status}. Snapshot unchanged.")
        return 1
    snapshot = build_snapshot(pd.read_csv(io.StringIO(result.data), dtype=str), today)
    print(f"Source: {SOURCE_URL}\n{len(snapshot)} tickers. {cross_check(snapshot, today)}")
    old = set(load_sp500()["ticker"]) if SP500_PATH.exists() else set()
    adds, removes = diff_tickers(old, set(snapshot["ticker"]))
    print(f"Adds ({len(adds)}): {', '.join(adds) or 'none'}")
    print(f"Removes ({len(removes)}): {', '.join(removes) or 'none'}")
    if not args.apply:
        print("Nothing written. Rerun with --apply to save this snapshot.")
        return 0
    snapshot.to_csv(SP500_PATH, index=False)
    print(f"Wrote {SP500_PATH}. A universe change is a strategy change: file a form.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
