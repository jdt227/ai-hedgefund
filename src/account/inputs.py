"""Load and validate the three official account inputs.

Files (default directory: private/account/, git-ignored):

* account_metadata.json  - game name, snapshot time/zone, cash, coverage flags
* holdings.csv           - opening holdings snapshot
* trades.csv             - actual executed fills (one row per fill)

Each loader returns the parsed object plus a list of ``Issue`` records instead
of raising, so a partially filled account can still produce research output
while the operations that need missing data are blocked.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

DEFAULT_ACCOUNT_DIR = Path("private/account")

HOLDINGS_REQUIRED = ["ticker", "shares"]
HOLDINGS_OPTIONAL = [
    # Investopedia "Purchase Price" column: displayed (rounded) average cost
    # per share. Used only for the reported-vs-calculated P&L check.
    "displayed_purchase_price",
    # Investopedia "Current Price" column at the snapshot. A reference price,
    # NOT a timestamped market quote.
    "displayed_current_price",
    "market_value",
    "reported_unrealized_pnl",
    # Optional STOCK / ETF. When blank, the market-data quote type is used.
    "asset_type",
]

TRADES_REQUIRED = [
    "trade_id", "executed_at", "ticker", "side", "shares", "fill_price",
    "fees", "game_name",
]
TRADES_OPTIONAL = ["order_id", "recommendation_run_id", "notes"]

METADATA_FIELDS = {
    "game_name": "Exact class-game name as shown in Investopedia",
    "snapshot_captured_at": "Snapshot time, ISO 8601 (e.g. 2026-10-20T20:31:00)",
    "snapshot_timezone": "IANA zone of the snapshot clock, e.g. America/New_York",
    "snapshot_source": "Where the snapshot came from",
    "holdings_complete": "true if the snapshot lists every position held",
    "cash": "Cash balance at the snapshot",
    "reported_account_value": "Account value shown at the snapshot",
    "reported_holdings_value": "Total market value of holdings shown at the snapshot",
    "reported_unrealized_pnl_total": "Total unrealized gain/loss shown",
    "ledger_complete_after_snapshot": "true if trades.csv has every fill after the snapshot",
    "buy_history_complete_from": "ISO date/time from which every BUY fill is recorded",
    "etf_counts_toward_stock_minimum": "true/false once the professor confirms; null if unknown",
    "fractional_shares_allowed": "true/false; null if unknown (whole shares used)",
    "commission_per_trade": "Commission charged per trade in the game; null if unknown",
    "notes": "Free text",
}

# Rounding tolerances. Investopedia displays prices to the cent, so a value
# computed from a displayed price can differ from the displayed value by up to
# half a cent per share, plus one cent of display rounding.
PRICE_DISPLAY_HALF_TICK = 0.005
VALUE_DISPLAY_TICK = 0.01


@dataclass
class Issue:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    source: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AccountMetadata:
    game_name: str | None = None
    snapshot_captured_at: str | None = None
    snapshot_timezone: str | None = None
    snapshot_source: str | None = None
    holdings_complete: bool | None = None
    cash: float | None = None
    reported_account_value: float | None = None
    reported_holdings_value: float | None = None
    reported_unrealized_pnl_total: float | None = None
    ledger_complete_after_snapshot: bool | None = None
    buy_history_complete_from: str | None = None
    etf_counts_toward_stock_minimum: bool | None = None
    fractional_shares_allowed: bool | None = None
    commission_per_trade: float | None = None
    notes: str | None = None
    extra: dict = field(default_factory=dict)

    def snapshot_naive(self) -> datetime | None:
        """Snapshot time as written, without timezone information."""
        if not self.snapshot_captured_at:
            return None
        parsed = datetime.fromisoformat(self.snapshot_captured_at)
        return parsed.replace(tzinfo=None)

    def snapshot_aware(self) -> datetime | None:
        """Snapshot time with its timezone, or None when the zone is unknown."""
        if not self.snapshot_captured_at:
            return None
        parsed = datetime.fromisoformat(self.snapshot_captured_at)
        if parsed.tzinfo is not None:
            return parsed
        if self.snapshot_timezone:
            return parsed.replace(tzinfo=ZoneInfo(self.snapshot_timezone))
        return None


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
def load_metadata(path: Path) -> tuple[AccountMetadata, list[Issue]]:
    issues: list[Issue] = []
    src = str(path)
    if not path.exists():
        issues.append(Issue("warning", "metadata_missing",
                            f"{path} not found; all account metadata treated as unknown.", src))
        return AccountMetadata(), issues

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        issues.append(Issue("error", "metadata_json", f"Invalid JSON: {exc}", src))
        return AccountMetadata(), issues
    if not isinstance(raw, dict):
        issues.append(Issue("error", "metadata_json", "Metadata must be a JSON object.", src))
        return AccountMetadata(), issues

    return parse_metadata(raw, src)


def parse_metadata(raw: dict, src: str = "metadata") -> tuple[AccountMetadata, list[Issue]]:
    issues: list[Issue] = []
    known = {k: raw.get(k) for k in METADATA_FIELDS}
    extra = {k: v for k, v in raw.items() if k not in METADATA_FIELDS and not k.startswith("_")}
    for key in extra:
        issues.append(Issue("warning", "metadata_unknown_field", f"Unknown field '{key}' ignored.", src))

    for key in ("game_name", "snapshot_captured_at", "snapshot_timezone", "snapshot_source",
                "buy_history_complete_from", "notes"):
        value = known[key]
        if value is not None and not isinstance(value, str):
            issues.append(Issue("error", "metadata_type", f"'{key}' must be a string or null.", src))
            known[key] = None
        elif isinstance(value, str) and not value.strip():
            known[key] = None

    for key in ("holdings_complete", "ledger_complete_after_snapshot",
                "etf_counts_toward_stock_minimum", "fractional_shares_allowed"):
        if known[key] is not None and not isinstance(known[key], bool):
            issues.append(Issue("error", "metadata_type", f"'{key}' must be true, false, or null.", src))
            known[key] = None

    for key in ("cash", "reported_account_value", "reported_holdings_value",
                "reported_unrealized_pnl_total", "commission_per_trade"):
        value = known[key]
        if value is None:
            continue
        if not _finite(value):
            issues.append(Issue("error", "metadata_number",
                                f"'{key}' must be a finite number or null (got {value!r}).", src))
            known[key] = None
            continue
        known[key] = float(value)

    if known["cash"] is not None and known["cash"] < 0:
        issues.append(Issue("warning", "metadata_negative_cash",
                            "Cash is negative (margin?). Order sizing will treat buying power as zero.", src))
    for key in ("reported_account_value", "reported_holdings_value", "commission_per_trade"):
        if known[key] is not None and known[key] < 0:
            issues.append(Issue("error", "metadata_negative", f"'{key}' cannot be negative.", src))
            known[key] = None

    if known["snapshot_timezone"]:
        try:
            ZoneInfo(known["snapshot_timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            issues.append(Issue("error", "metadata_timezone",
                                f"Unknown timezone '{known['snapshot_timezone']}'.", src))
            known["snapshot_timezone"] = None

    for key in ("snapshot_captured_at", "buy_history_complete_from"):
        if known[key]:
            try:
                parsed = datetime.fromisoformat(known[key])
            except ValueError:
                issues.append(Issue("error", "metadata_timestamp", f"'{key}' is not ISO 8601.", src))
                known[key] = None
                continue
            if key == "snapshot_captured_at" and parsed.tzinfo is not None and known["snapshot_timezone"]:
                zone_offset = parsed.replace(tzinfo=None).replace(
                    tzinfo=ZoneInfo(known["snapshot_timezone"])).utcoffset()
                if zone_offset != parsed.utcoffset():
                    issues.append(Issue("error", "metadata_timezone_mismatch",
                                        "snapshot_captured_at offset disagrees with snapshot_timezone.", src))

    meta = AccountMetadata(**known, extra=extra)

    for key, label in [("game_name", "class-game name"), ("cash", "cash balance"),
                       ("snapshot_timezone", "snapshot timezone"),
                       ("holdings_complete", "holdings completeness"),
                       ("ledger_complete_after_snapshot", "post-snapshot ledger completeness")]:
        if getattr(meta, key) is None:
            issues.append(Issue("info", f"unknown_{key}", f"Unknown {label} ('{key}').", src))
    return meta, issues


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def _read_csv(path: Path, required: list[str], optional: list[str], src: str):
    issues: list[Issue] = []
    if not path.exists():
        return None, [Issue("warning", "file_missing", f"{path} not found.", src)]
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, skipinitialspace=True)
    frame.columns = [c.strip() for c in frame.columns]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        issues.append(Issue("error", "missing_columns", f"Missing required columns: {missing}", src))
        return None, issues
    unknown = [c for c in frame.columns if c not in required + optional]
    for col in unknown:
        issues.append(Issue("warning", "unknown_column", f"Unknown column '{col}' ignored.", src))
    for col in required + optional:
        if col not in frame.columns:
            frame[col] = ""
    frame = frame[required + optional].copy()
    for col in frame.columns:
        frame[col] = frame[col].astype(str).str.strip()
    # Drop fully blank lines.
    frame = frame[~(frame == "").all(axis=1)].reset_index(drop=True)
    return frame, issues


def _numeric(frame: pd.DataFrame, col: str, src: str, issues: list[Issue],
             required: bool, row_label, bad: pd.Series | None = None) -> pd.Series:
    """Parse a numeric column; flag blanks (if required), text, NaN and infinity.

    Rows with an error are marked True in ``bad`` when it is given.
    """
    raw = frame[col]
    values = pd.to_numeric(raw.str.replace(",", "", regex=False).str.replace("$", "", regex=False),
                           errors="coerce")
    for i, (text, value) in enumerate(zip(raw, values)):
        if text == "":
            if required:
                issues.append(Issue("error", "blank_value", f"{row_label(i)}: '{col}' is blank.", src))
                if bad is not None:
                    bad.iloc[i] = True
            continue
        if pd.isna(value) or not math.isfinite(value):
            issues.append(Issue("error", "non_finite",
                                f"{row_label(i)}: '{col}' must be a finite number (got '{text}').", src))
            if bad is not None:
                bad.iloc[i] = True
    values = values.where(values.map(lambda v: pd.notna(v) and math.isfinite(v)))
    return values.astype(float)


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------
def load_holdings(path: Path) -> tuple[pd.DataFrame | None, list[Issue]]:
    src = str(path)
    frame, issues = _read_csv(path, HOLDINGS_REQUIRED, HOLDINGS_OPTIONAL, src)
    if frame is None:
        return None, issues
    return validate_holdings(frame, src, issues)


def validate_holdings(frame: pd.DataFrame, src: str = "holdings",
                      issues: list[Issue] | None = None) -> tuple[pd.DataFrame, list[Issue]]:
    """Validate holdings; keep reported fields and add calculated ones beside them."""
    issues = list(issues or [])
    frame = frame.copy()
    for col in HOLDINGS_OPTIONAL:
        if col not in frame.columns:
            frame[col] = ""
    frame["ticker"] = frame["ticker"].astype(str).str.strip().str.upper()

    def label(i):
        return f"row {i + 2} ({frame.at[i, 'ticker'] or 'blank ticker'})"

    for i, ticker in enumerate(frame["ticker"]):
        if not ticker:
            issues.append(Issue("error", "blank_ticker", f"row {i + 2}: ticker is blank.", src))
    dupes = frame.loc[frame["ticker"].duplicated(keep=False) & (frame["ticker"] != ""), "ticker"]
    for ticker in sorted(set(dupes)):
        issues.append(Issue("error", "duplicate_holding",
                            f"Ticker {ticker} appears more than once; combine lots into one row.", src))

    frame["shares"] = _numeric(frame, "shares", src, issues, True, label)
    for i, shares in enumerate(frame["shares"]):
        if pd.notna(shares) and shares <= 0:
            issues.append(Issue("error", "nonpositive_shares",
                                f"{label(i)}: shares must be > 0 (long-only; omit closed positions).", src))

    for col in ("displayed_purchase_price", "displayed_current_price", "market_value"):
        frame[col] = _numeric(frame, col, src, issues, False, label)
        for i, value in enumerate(frame[col]):
            if pd.notna(value) and value <= 0:
                issues.append(Issue("error", "nonpositive_value", f"{label(i)}: '{col}' must be > 0.", src))
    frame["reported_unrealized_pnl"] = _numeric(frame, "reported_unrealized_pnl", src, issues, False, label)

    frame["asset_type"] = frame["asset_type"].astype(str).str.upper().replace({"": None})
    bad_types = frame["asset_type"].notna() & ~frame["asset_type"].isin(["STOCK", "ETF"])
    for i in frame.index[bad_types]:
        issues.append(Issue("error", "asset_type", f"{label(i)}: asset_type must be STOCK or ETF.", src))

    # Calculated fields are stored next to reported ones; never overwrite.
    frame["calc_market_value"] = frame["shares"] * frame["displayed_current_price"]
    frame["market_value_diff"] = frame["market_value"] - frame["calc_market_value"]
    frame["calc_unrealized_pnl"] = (
        (frame["displayed_current_price"] - frame["displayed_purchase_price"]) * frame["shares"])
    frame["unrealized_pnl_diff"] = frame["reported_unrealized_pnl"] - frame["calc_unrealized_pnl"]

    for i, row in frame.iterrows():
        if pd.notna(row["market_value_diff"]):
            tol = row["shares"] * PRICE_DISPLAY_HALF_TICK + VALUE_DISPLAY_TICK
            if abs(row["market_value_diff"]) > tol:
                issues.append(Issue(
                    "error", "market_value_mismatch",
                    f"{label(i)}: shares x displayed price = {row['calc_market_value']:.2f} but reported "
                    f"market value = {row['market_value']:.2f} (tolerance {tol:.2f}).", src))
        if pd.notna(row["unrealized_pnl_diff"]):
            # Both displayed prices are rounded, so allow one cent per share.
            tol = row["shares"] * 2 * PRICE_DISPLAY_HALF_TICK + VALUE_DISPLAY_TICK
            sev = "warning" if abs(row["unrealized_pnl_diff"]) > tol else "info"
            if abs(row["unrealized_pnl_diff"]) > 1e-9:
                issues.append(Issue(
                    sev, "pnl_rounding",
                    f"{label(i)}: reported P&L {row['reported_unrealized_pnl']:.2f} vs calculated "
                    f"{row['calc_unrealized_pnl']:.2f} (diff {row['unrealized_pnl_diff']:+.2f}, "
                    f"rounding tolerance {tol:.2f}). Reported value kept.", src))
    return frame, issues


def check_snapshot_totals(meta: AccountMetadata, holdings: pd.DataFrame | None) -> list[Issue]:
    """Cross-check holdings rows against snapshot-level totals and cash."""
    issues: list[Issue] = []
    src = "snapshot"
    rows = holdings if holdings is not None else pd.DataFrame(columns=["market_value", "shares"])
    n = len(rows)
    if meta.reported_holdings_value is not None and n:
        total = rows["market_value"].sum(min_count=n)
        if pd.notna(total):
            tol = VALUE_DISPLAY_TICK * (n + 1)
            if abs(total - meta.reported_holdings_value) > tol:
                issues.append(Issue("error", "holdings_total_mismatch",
                                    f"Sum of holding market values {total:.2f} != reported holdings value "
                                    f"{meta.reported_holdings_value:.2f}.", src))
            else:
                issues.append(Issue("info", "holdings_total_ok",
                                    f"Holding values sum to the reported total {total:.2f}.", src))
    if meta.reported_unrealized_pnl_total is not None and n:
        total = rows["reported_unrealized_pnl"].sum(min_count=n)
        if pd.notna(total) and abs(total - meta.reported_unrealized_pnl_total) > VALUE_DISPLAY_TICK * (n + 1):
            issues.append(Issue("warning", "pnl_total_mismatch",
                                f"Row P&L sums to {total:.2f}; reported total is "
                                f"{meta.reported_unrealized_pnl_total:.2f}.", src))
    if n == 0 and meta.reported_holdings_value not in (None, 0.0):
        issues.append(Issue("error", "holdings_missing_rows",
                            "Metadata reports a holdings value but holdings.csv has no rows.", src))
    if meta.cash is not None and meta.reported_account_value is not None:
        holdings_value = meta.reported_holdings_value
        if holdings_value is None and n:
            holdings_value = rows["market_value"].sum(min_count=n)
        if holdings_value is None and n == 0:
            holdings_value = 0.0
        if holdings_value is not None and pd.notna(holdings_value):
            diff = meta.cash + holdings_value - meta.reported_account_value
            tol = max(1.0, 0.0001 * meta.reported_account_value)
            if abs(diff) > tol:
                issues.append(Issue("error", "account_value_mismatch",
                                    f"cash {meta.cash:.2f} + holdings {holdings_value:.2f} differs from "
                                    f"reported account value {meta.reported_account_value:.2f} by {diff:+.2f} "
                                    f"(tolerance {tol:.2f}). Shorts/margin are not modeled.", src))
            else:
                issues.append(Issue("info", "account_value_ok",
                                    "cash + holdings matches reported account value.", src))
    return issues


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------
def load_trades(path: Path, expected_game: str | None) -> tuple[pd.DataFrame, list[Issue]]:
    src = str(path)
    frame, issues = _read_csv(path, TRADES_REQUIRED, TRADES_OPTIONAL, src)
    if frame is None:
        if any(i.severity == "error" for i in issues):
            return empty_trades(), issues
        # A missing trades file is the same as an empty ledger.
        return empty_trades(), [Issue("info", "no_trades_file", f"{path} not found; ledger is empty.", src)]
    return validate_trades(frame, expected_game, src, issues)


def empty_trades() -> pd.DataFrame:
    frame = pd.DataFrame(columns=TRADES_REQUIRED + TRADES_OPTIONAL + ["executed_at_utc", "valid"])
    frame["executed_at_utc"] = pd.to_datetime(frame["executed_at_utc"], utc=True)
    return frame


def validate_trades(frame: pd.DataFrame, expected_game: str | None, src: str = "trades",
                    issues: list[Issue] | None = None) -> tuple[pd.DataFrame, list[Issue]]:
    """Validate executed fills. Invalid rows are kept but marked valid=False."""
    issues = list(issues or [])
    frame = frame.copy().reset_index(drop=True)
    for col in TRADES_OPTIONAL:
        if col not in frame.columns:
            frame[col] = ""
    if frame.empty:
        out = empty_trades()
        return out, issues

    frame["ticker"] = frame["ticker"].astype(str).str.strip().str.upper()
    frame["side"] = frame["side"].astype(str).str.strip().str.upper()
    bad = pd.Series(False, index=frame.index)

    def label(i):
        return f"row {i + 2} (trade_id={frame.at[i, 'trade_id'] or 'blank'})"

    def flag(i, code, message, severity="error"):
        issues.append(Issue(severity, code, f"{label(i)}: {message}", src))
        if severity == "error":
            bad.iloc[i] = True

    for col in ("trade_id", "ticker", "executed_at", "game_name"):
        for i, value in enumerate(frame[col]):
            if not value:
                flag(i, "blank_value", f"'{col}' is blank.")

    dupes = frame["trade_id"].duplicated(keep=False) & (frame["trade_id"] != "")
    for i in frame.index[dupes]:
        flag(i, "duplicate_trade_id", "trade_id is duplicated; each fill needs a unique ID.")

    for i, side in enumerate(frame["side"]):
        if side not in ("BUY", "SELL"):
            flag(i, "bad_side", f"side must be BUY or SELL (got '{side}').")

    frame["shares"] = _numeric(frame, "shares", src, issues, True, label, bad)
    frame["fill_price"] = _numeric(frame, "fill_price", src, issues, True, label, bad)
    frame["fees"] = _numeric(frame, "fees", src, issues, True, label, bad)
    for i in frame.index:
        if pd.notna(frame.at[i, "shares"]) and frame.at[i, "shares"] <= 0:
            flag(i, "nonpositive_shares", "shares must be > 0 (direction comes from side).")
        if pd.notna(frame.at[i, "fill_price"]) and frame.at[i, "fill_price"] <= 0:
            flag(i, "nonpositive_price", "fill_price must be > 0.")
        if pd.notna(frame.at[i, "fees"]) and frame.at[i, "fees"] < 0:
            flag(i, "negative_fees", "fees cannot be negative.")

    utc_times = []
    for i, text in enumerate(frame["executed_at"]):
        stamp = pd.NaT
        if text:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                flag(i, "bad_timestamp", f"executed_at '{text}' is not ISO 8601.")
            else:
                if parsed.tzinfo is None:
                    flag(i, "naive_timestamp",
                         "executed_at needs a UTC offset, e.g. 2026-10-21T10:15:00-04:00.")
                else:
                    stamp = pd.Timestamp(parsed).tz_convert("UTC")
        utc_times.append(stamp)
    frame["executed_at_utc"] = pd.to_datetime(pd.Series(utc_times, index=frame.index), utc=True)

    if expected_game is None:
        if len(frame):
            issues.append(Issue("warning", "game_unverified",
                                "Class-game name is unknown in metadata; trades cannot be verified as "
                                "belonging to the class game and are excluded from compliance counts.", src))
    else:
        for i, game in enumerate(frame["game_name"]):
            if game and game != expected_game:
                flag(i, "wrong_game", f"game_name '{game}' does not match the class game '{expected_game}'.")

    frame["valid"] = ~bad
    return frame, issues


@dataclass
class AccountInputs:
    metadata: AccountMetadata
    holdings: pd.DataFrame | None
    trades: pd.DataFrame
    issues: list[Issue]
    account_dir: Path

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


def load_account(account_dir: Path = DEFAULT_ACCOUNT_DIR) -> AccountInputs:
    account_dir = Path(account_dir)
    meta, issues = load_metadata(account_dir / "account_metadata.json")
    holdings, h_issues = load_holdings(account_dir / "holdings.csv")
    trades, t_issues = load_trades(account_dir / "trades.csv", meta.game_name)
    issues = issues + h_issues + t_issues + check_snapshot_totals(meta, holdings)
    return AccountInputs(meta, holdings, trades, issues, account_dir)
