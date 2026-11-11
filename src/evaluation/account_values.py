"""The account value series the contest is graded on.

Two sources, in order of preference:

1. Daily reconstruction: opening snapshot (shares and cash) + every fill since it in
   trades.csv + daily traded closes. Used only when the ledger is confirmed complete
   since a dated snapshot. Dividends the game credits to cash are not in the ledger,
   so the series is cross-checked against logged values; gaps over 1% are reported.
2. Logged values: account values you copy from the class game into
   private/account/account_values.csv (date, account_value, source_note).
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pandas as pd

from src.account.inputs import AccountInputs

ACCOUNT_VALUES_FILE = "account_values.csv"
COLUMNS = ["date", "account_value", "source_note"]
CROSS_CHECK_TOLERANCE = 0.01


def load_account_values(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Logged values sorted by date, plus problems found. A missing file is an empty log."""
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS), []
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    problems = [f"missing column '{c}'" for c in COLUMNS if c not in frame.columns]
    if problems:
        return pd.DataFrame(columns=COLUMNS), problems
    rows = []
    for i, row in frame.iterrows():
        try:
            day = date.fromisoformat(row["date"].strip())
            value = float(row["account_value"])
        except ValueError:
            problems.append(f"row {i + 2}: unreadable date or value")
            continue
        if not math.isfinite(value) or value <= 0:
            problems.append(f"row {i + 2}: account value must be a positive number")
            continue
        rows.append({"date": pd.Timestamp(day), "account_value": value, "source_note": row["source_note"]})
    values = pd.DataFrame(rows, columns=COLUMNS)
    dupes = values["date"][values["date"].duplicated()]
    problems += [f"duplicate date {d.date()}" for d in dupes]
    return values.drop_duplicates("date", keep="first").sort_values("date").reset_index(drop=True), problems


def log_value(path: Path, day: str, value: str, note: str = "", today: date | None = None) -> str:
    """Append one validated row. Raises ValueError with a readable message on bad input."""
    try:
        parsed_day = date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError(f"Date must be YYYY-MM-DD (got '{day}').") from exc
    if parsed_day > (today or date.today()):
        raise ValueError(f"{day} is in the future.")
    try:
        amount = float(str(value).replace(",", "").replace("$", ""))
    except ValueError as exc:
        raise ValueError(f"Value must be a number (got '{value}').") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Account value must be a positive number.")
    existing, problems = load_account_values(path)
    if problems:
        raise ValueError(f"Fix {path} first: {'; '.join(problems)}")
    if (existing["date"] == pd.Timestamp(parsed_day)).any():
        raise ValueError(f"{day} is already logged. Edit {path} to change it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8") as fh:
        if new:
            fh.write(",".join(COLUMNS) + "\n")
        fh.write(f"{parsed_day.isoformat()},{amount:.2f},{note.replace(',', ';')}\n")
    return f"Logged {parsed_day.isoformat()}: ${amount:,.2f}"


def can_reconstruct(account: AccountInputs) -> tuple[bool, str]:
    """Daily reconstruction needs a dated, complete opening state and a complete ledger."""
    meta = account.metadata
    missing = []
    if not meta.snapshot_captured_at:
        missing.append("snapshot_captured_at")
    if meta.cash is None:
        missing.append("cash")
    if meta.holdings_complete is not True:
        missing.append("holdings_complete")
    if meta.ledger_complete_after_snapshot is not True:
        missing.append("ledger_complete_after_snapshot")
    if account.errors:
        missing.append("account validation errors")
    if missing:
        return False, "daily reconstruction needs: " + ", ".join(missing)
    return True, "ok"


def reconstruct_daily_values(opening_shares: dict[str, float], opening_cash: float, start: date,
                             trades: pd.DataFrame, closes: pd.DataFrame, timezone: str) -> pd.DataFrame:
    """Close-of-day account value from the snapshot date forward.

    Fills on a date change shares and cash before that day's close. Missing closes
    carry the last known close forward; a ticker with no close at all is reported.
    """
    shares = {t: float(s) for t, s in opening_shares.items()}
    cash = float(opening_cash)
    days = closes.index[closes.index.date >= start]
    fills = trades.copy()
    if len(fills):
        fills = fills[fills["valid"]].copy()
        fills["local_date"] = fills["executed_at_utc"].dt.tz_convert(timezone).dt.date
    filled = closes.ffill()
    rows = []
    for day in days:
        todays = fills[fills["local_date"] == day.date()] if len(fills) else fills
        for fill in todays.itertuples():
            signed = fill.shares if fill.side == "BUY" else -fill.shares
            shares[fill.ticker] = shares.get(fill.ticker, 0.0) + signed
            cash += -signed * fill.fill_price - fill.fees
        unpriced = [t for t, s in shares.items() if abs(s) > 1e-9 and (t not in filled or pd.isna(filled.at[day, t]))]
        value = cash + sum(s * filled.at[day, t] for t, s in shares.items()
                           if abs(s) > 1e-9 and t not in unpriced)
        rows.append({"date": day, "account_value": value, "unpriced": ", ".join(unpriced)})
    return pd.DataFrame(rows, columns=["date", "account_value", "unpriced"])


def cross_check(reconstructed: pd.DataFrame, logged: pd.DataFrame) -> list[str]:
    """Logged dates where the reconstruction differs by more than 1%."""
    issues = []
    rec = reconstructed.set_index("date")["account_value"]
    for row in logged.itertuples():
        if row.date in rec.index:
            gap = rec[row.date] / row.account_value - 1
            if abs(gap) > CROSS_CHECK_TOLERANCE:
                issues.append(f"{row.date.date()}: reconstructed ${rec[row.date]:,.2f} vs logged "
                              f"${row.account_value:,.2f} ({gap:+.1%}); check fills, fees, or dividends")
    return issues


def interval_returns(values: pd.DataFrame) -> pd.DataFrame:
    """Simple returns between consecutive values: start, end, return."""
    ordered = values.sort_values("date").reset_index(drop=True)
    rows = [{"start": ordered.at[i - 1, "date"], "end": ordered.at[i, "date"],
             "return": ordered.at[i, "account_value"] / ordered.at[i - 1, "account_value"] - 1}
            for i in range(1, len(ordered))]
    return pd.DataFrame(rows, columns=["start", "end", "return"])
