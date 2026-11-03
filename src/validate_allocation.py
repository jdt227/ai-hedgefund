"""Check an LLM's final Symbol,Weight allocation against the class rules and strategy limits.

    python -m src.validate_allocation runs/<run_id>/recommendation_reason.txt

Prints a pass/fail table and writes final_allocation.csv next to the input file
only if everything passes. Exit code 0 means pass, 1 means fail.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from src.banned_products import banned_reason, counts_as_stock, is_etf  # noqa: E402
from src.universe import get_universe  # noqa: E402

CASH = "CASH"
_ROW = re.compile(r"^\s*\|?\s*([A-Za-z][A-Za-z.\-]*)\s*[,|]\s*([^,|]+?)\s*\|?\s*$")


FORMAT = "Format"
CLASS_RULES = "Class rules"
STRATEGY_LIMITS = "Strategy limits"
GROUP_NOTES = {
    FORMAT: "the response cannot be read as a valid allocation",
    CLASS_RULES: "a failure is a grade risk",
    STRATEGY_LIMITS: "a failure breaks our own limits, not a class rule",
}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    group: str = FORMAT


def extract_block(text: str) -> tuple[list[tuple[str, str]], str | None]:
    """Parse the LAST Symbol,Weight block. Returns (rows, error)."""
    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*symbol\s*,\s*weight\s*", line, re.I)]
    if not headers:
        return [], "No 'Symbol,Weight' block found."
    rows = []
    for line in lines[headers[-1] + 1:]:
        stripped = line.strip()
        if not stripped or stripped.strip("`").upper() == "CSV" or stripped.startswith("```"):
            if rows:
                break
            continue
        match = _ROW.match(stripped)
        if not match:
            if rows:
                break
            return [], f"Could not read this line in the block: {stripped[:60]}"
        rows.append((match.group(1).upper(), match.group(2)))
    return (rows, None) if rows else ([], "The 'Symbol,Weight' block has no rows.")


def _parse_weight(raw: str) -> float | None:
    try:
        return float(raw.replace("%", "").strip())
    except ValueError:
        return None


def validate(rows: list[tuple[str, str]], universe: dict[str, str] | None = None) -> list[Check]:
    """Run every rule. All rules are reported even after one fails."""
    universe = universe if universe is not None else get_universe()
    limits = settings.limits()
    rules = settings.CLASS_RULES
    weights = {symbol: _parse_weight(raw) for symbol, raw in rows}
    symbols = [symbol for symbol, _ in rows]
    stocks = {s: w for s, w in weights.items() if s != CASH}
    numeric = {s: w for s, w in stocks.items() if w is not None}
    cash = weights.get(CASH)

    dupes = sorted({s for s in symbols if symbols.count(s) > 1})
    bad = [s for s, w in weights.items() if w is None]
    negative = [s for s, w in weights.items() if w is not None and w < 0]
    fractional = [s for s, w in weights.items() if w is not None and w != int(w)]
    total = sum(w for w in weights.values() if w is not None)

    banned = {s: banned_reason(s) for s in stocks if banned_reason(s)}
    stock_count = sum(1 for s, w in numeric.items() if w > 0 and counts_as_stock(s))
    etfs = sorted(s for s in stocks if is_etf(s))
    unknown = sorted(set(stocks) - set(universe))
    over = [f"{s} {w:g}%" for s, w in numeric.items() if w > limits["max_position_pct"]]
    equity = sum(numeric.values())
    sectors: dict[str, float] = {}
    for symbol, weight in numeric.items():
        sector = universe.get(symbol)
        if sector:
            sectors[sector] = sectors.get(sector, 0.0) + weight
    over_sector = [f"{s} {w:g}%" for s, w in sectors.items() if w > limits["max_sector_pct"]]
    position_label = f"{limits['min_holdings']}-{limits['max_holdings']} positions"
    abstain = " (all-cash abstention is not a valid final allocation)" if not stocks else ""

    return [
        Check("No duplicate symbols", not dupes, ", ".join(dupes) or "ok"),
        Check("Weights are valid and non-negative", not bad and not negative, ", ".join(bad + negative) or "ok"),
        Check("Whole-percentage weights", not fractional, ", ".join(fractional) or "ok"),
        Check("Weights sum to exactly 100", total == 100, f"{total:g}"),
        Check(f"At least {rules['min_stocks']} stocks (ETFs do not count)", stock_count >= rules["min_stocks"],
              f"{stock_count} stocks{abstain}", CLASS_RULES),
        Check("Equities and ETFs only", not banned, "; ".join(banned.values()) or "ok", CLASS_RULES),
        Check(position_label, limits["min_holdings"] <= len(stocks) <= limits["max_holdings"],
              f"{len(stocks)} positions{abstain}", STRATEGY_LIMITS),
        Check("Tickers are in the universe", not unknown, ", ".join(unknown) or "ok", STRATEGY_LIMITS),
        Check("No ETF positions (ALLOW_ETF_HOLDINGS)", settings.ALLOW_ETF_HOLDINGS or not etfs,
              ", ".join(etfs) or "ok", STRATEGY_LIMITS),
        Check(f"Each stock at most {limits['max_position_pct']}%", not over, ", ".join(over) or "ok", STRATEGY_LIMITS),
        Check(f"Each sector at most {limits['max_sector_pct']}%", not over_sector and not unknown,
              ", ".join(over_sector) or ("cannot verify unknown tickers" if unknown else "ok"), STRATEGY_LIMITS),
        Check(f"Equity at most {limits['max_equity_pct']}%", equity <= limits["max_equity_pct"], f"{equity:g}%",
              STRATEGY_LIMITS),
        Check(f"CASH row present, at least {limits['min_cash_pct']}%",
              cash is not None and cash >= limits["min_cash_pct"], "missing" if cash is None else f"{cash:g}%",
              STRATEGY_LIMITS),
    ]


def run(path: Path, universe: dict[str, str] | None = None) -> tuple[bool, list[Check]]:
    """Validate a response file. Writes final_allocation.csv only on a full pass."""
    output = path.parent / "final_allocation.csv"
    # A stale passing file must not survive a failed re-validation.
    output.unlink(missing_ok=True)
    rows, error = extract_block(path.read_text(encoding="utf-8"))
    checks = [Check("Allocation block found", error is None, error or "ok")]
    if error is None:
        checks += validate(rows, universe)
    passed = all(check.passed for check in checks)
    if passed:
        body = "".join(f"{symbol},{int(float(raw.replace('%', '')))}\n" for symbol, raw in rows)
        output.write_text("Symbol,Weight\n" + body, encoding="utf-8")
    return passed, checks


def print_checks(checks: list[Check]) -> None:
    """Print pass/fail tables, one per check group."""
    width = max(len(check.name) for check in checks)
    groups = list(dict.fromkeys(check.group for check in checks))
    for group in groups:
        print(f"\n{group} ({GROUP_NOTES.get(group, 'checks')}):")
        for check in (c for c in checks if c.group == group):
            print(f"  {'PASS' if check.passed else 'FAIL'}  {check.name:<{width}}  {check.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("response", type=Path, help="Saved LLM response, e.g. recommendation_reason.txt")
    args = parser.parse_args(argv)
    if not args.response.exists():
        print(f"File not found: {args.response}")
        return 1
    passed, checks = run(args.response)
    print_checks(checks)
    if passed:
        print(f"\nAll checks pass. Wrote {args.response.parent / 'final_allocation.csv'}")
        return 0
    print("\nAllocation rejected. final_allocation.csv was not written.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
