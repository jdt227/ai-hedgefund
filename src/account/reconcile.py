"""Roll an opening snapshot forward with executed fills, and count monthly buys.

Key ideas
---------
* The ledger starts at the opening snapshot, not at account inception. Fills
  before the snapshot are assumed to be already reflected in it; fills after it
  are applied on top.
* Every derived number carries a status. "verified" requires the inputs that
  support it (complete holdings, complete post-snapshot ledger, known cash).
  Otherwise the status is "estimate" or "cannot_verify" - never a silent guess.
* Monthly buys are counted only from recorded, valid, class-game BUY fills.
  Recommendations are never counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.account.inputs import AccountInputs, Issue

# Possible UTC offsets range from -12h to +14h. When the snapshot timezone is
# unknown, a naive local time T maps to a UTC instant in [T - 14h, T + 12h].
_MAX_POS_OFFSET = timedelta(hours=14)
_MAX_NEG_OFFSET = timedelta(hours=12)


@dataclass
class Reconciliation:
    positions: pd.DataFrame            # ticker, opening_shares, net_trade_shares, shares, status
    cash_at_snapshot: float | None
    current_cash: float | None
    current_cash_status: str           # verified | unknown
    positions_status: str              # verified | estimate | cannot_verify
    trade_timing: pd.DataFrame         # trade_id, timing: before|after|ambiguous|invalid
    issues: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def classify_trade_timing(inputs: AccountInputs) -> pd.DataFrame:
    """Label each fill as before/after the snapshot, or ambiguous."""
    trades = inputs.trades
    meta = inputs.metadata
    out = pd.DataFrame({"trade_id": trades["trade_id"], "timing": "invalid"})
    if trades.empty:
        return out

    aware = meta.snapshot_aware()
    naive = meta.snapshot_naive()
    for i, row in trades.iterrows():
        if not row["valid"] or pd.isna(row["executed_at_utc"]):
            continue
        when = row["executed_at_utc"]
        if naive is None:
            # No snapshot time: every fill is "after" an empty/unknown opening.
            timing = "after" if inputs.holdings is None or inputs.holdings.empty else "ambiguous"
        elif aware is not None:
            timing = "after" if when > pd.Timestamp(aware) else "before"
        else:
            earliest = pd.Timestamp(naive - _MAX_POS_OFFSET, tz="UTC")
            latest = pd.Timestamp(naive + _MAX_NEG_OFFSET, tz="UTC")
            if when > latest:
                timing = "after"
            elif when < earliest:
                timing = "before"
            else:
                timing = "ambiguous"
        out.at[i, "timing"] = timing
    return out


def reconcile(inputs: AccountInputs) -> Reconciliation:
    meta = inputs.metadata
    issues: list[Issue] = []
    notes: list[str] = []
    timing = classify_trade_timing(inputs)

    holdings = inputs.holdings
    if holdings is None:
        holdings = pd.DataFrame({"ticker": pd.Series(dtype=str), "shares": pd.Series(dtype=float)})
    opening = holdings.groupby("ticker")["shares"].sum()

    trades = inputs.trades.assign(timing=timing["timing"].to_numpy()) if len(inputs.trades) else None
    after = pd.DataFrame()
    if trades is not None:
        after = trades[trades["timing"] == "after"].sort_values("executed_at_utc", kind="stable")
        n_amb = int((trades["timing"] == "ambiguous").sum())
        if n_amb:
            issues.append(Issue("warning", "ambiguous_trade_timing",
                                f"{n_amb} fill(s) fall within the timezone-uncertainty window around the "
                                "snapshot; set snapshot_timezone so they can be placed before/after it.",
                                "reconcile"))
        n_invalid = int((trades["timing"] == "invalid").sum())
        if n_invalid:
            notes.append(f"{n_invalid} invalid fill(s) excluded from reconciliation.")

    signed = pd.Series(dtype=float)
    if not after.empty:
        direction = after["side"].map({"BUY": 1.0, "SELL": -1.0})
        signed = (after["shares"] * direction).groupby(after["ticker"]).sum()

    tickers = sorted(set(opening.index) | set(signed.index))
    positions = pd.DataFrame({"ticker": tickers})
    positions["opening_shares"] = positions["ticker"].map(opening).fillna(0.0)
    positions["net_trade_shares"] = positions["ticker"].map(signed).fillna(0.0)
    positions["shares"] = positions["opening_shares"] + positions["net_trade_shares"]

    complete_state = bool(meta.holdings_complete) and bool(meta.ledger_complete_after_snapshot)
    has_ambiguous = trades is not None and (trades["timing"] == "ambiguous").any()

    # Impossible long-only sells: check the running position fill by fill.
    if not after.empty:
        running = opening.to_dict()
        for _, row in after.iterrows():
            delta = row["shares"] if row["side"] == "BUY" else -row["shares"]
            running[row["ticker"]] = running.get(row["ticker"], 0.0) + delta
            if running[row["ticker"]] < -1e-9:
                if complete_state and not has_ambiguous:
                    issues.append(Issue("error", "impossible_sell",
                                        f"Fill {row['trade_id']} sells more {row['ticker']} than held "
                                        "(long-only account).", "reconcile"))
                else:
                    issues.append(Issue("warning", "unverifiable_sell",
                                        f"Fill {row['trade_id']} would leave {row['ticker']} negative, but the "
                                        "opening snapshot/ledger is not confirmed complete, so this cannot be "
                                        "verified as an error.", "reconcile"))

    positions = positions[positions["shares"].abs() > 1e-9].reset_index(drop=True)
    if complete_state and not has_ambiguous:
        positions_status = "verified"
    elif meta.holdings_complete is False:
        positions_status = "cannot_verify"
        notes.append("Snapshot is marked incomplete; positions may be missing.")
    else:
        positions_status = "estimate"
        notes.append("Positions are an estimate: holdings completeness and/or post-snapshot ledger "
                     "completeness are not confirmed in account_metadata.json.")
    positions["status"] = positions_status

    # Cash roll-forward.
    current_cash = None
    cash_status = "unknown"
    if meta.cash is not None and meta.ledger_complete_after_snapshot and not has_ambiguous:
        flows = 0.0
        if not after.empty:
            gross = after["shares"] * after["fill_price"]
            flows = float((gross.where(after["side"] == "SELL", -gross) - after["fees"]).sum())
        current_cash = meta.cash + flows
        cash_status = "verified"
        if current_cash < -0.01:
            issues.append(Issue("warning", "negative_cash",
                                f"Rolled-forward cash is {current_cash:.2f}; check fills and fees.",
                                "reconcile"))
    elif meta.cash is None:
        notes.append("Cash is unknown; account-ready order sizing is blocked.")
    else:
        notes.append("Cash at the snapshot is known but the post-snapshot ledger is not confirmed "
                     "complete, so current cash cannot be verified.")

    return Reconciliation(positions, meta.cash, current_cash, cash_status, positions_status,
                          timing, issues, notes)


# ---------------------------------------------------------------------------
# Monthly BUY compliance
# ---------------------------------------------------------------------------
@dataclass
class MonthlyBuyStatus:
    month: str
    timezone: str
    required: int
    status: str                 # compliant | gap | unknown
    count_lower_bound: int | None
    count_upper_bound: int | None
    coverage: str               # complete | partial | none
    days_remaining: int          # calendar days left in the month, including today
    weekdays_remaining: int      # weekdays left including today (holidays not modeled)
    detail: list[str] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def count_monthly_buys(inputs: AccountInputs, month: str, timezone: str, required: int,
                       now: datetime | None = None) -> MonthlyBuyStatus:
    """Count executed BUY orders in ``month`` (YYYY-MM) in ``timezone``.

    * Partial fills sharing an order_id count as one order.
    * Fills without order_id: upper bound counts each fill; lower bound merges
      fills for the same ticker on the same local day.
    * Coverage is "complete" only when ``buy_history_complete_from`` is on or
      before the start of the month. With partial coverage, the recorded
      count is a lower bound: it can prove compliance but never a gap.
    """
    tz = ZoneInfo(timezone)
    year, mon = (int(x) for x in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=tz)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=tz)
    now = now or datetime.now(tz)
    now_local = now.astimezone(tz)
    days_remaining = max(0, (end.date() - now_local.date()).days) if now_local < end else 0
    weekdays_remaining = int(np.busday_count(now_local.date(), end.date())) if now_local < end else 0
    detail: list[str] = []

    meta = inputs.metadata
    coverage = "none"
    if meta.buy_history_complete_from:
        cov = datetime.fromisoformat(meta.buy_history_complete_from)
        cov = cov if cov.tzinfo else cov.replace(tzinfo=tz)
        coverage = "complete" if cov <= start else "partial"
        if coverage == "partial":
            detail.append(f"BUY history is only recorded from {meta.buy_history_complete_from}; "
                          "earlier buys this month may be missing.")
    else:
        detail.append("buy_history_complete_from is not set, so unrecorded buys may exist.")

    trades = inputs.trades
    if meta.game_name is None:
        detail.append("Class-game name unknown: fills cannot be attributed to the class game.")
        buys = trades.iloc[0:0]
    else:
        buys = trades[(trades["valid"]) & (trades["side"] == "BUY") & (trades["game_name"] == meta.game_name)]
    if len(buys):
        local = buys["executed_at_utc"].dt.tz_convert(timezone)
        buys = buys.assign(local_time=local)
        buys = buys[(local >= pd.Timestamp(start)) & (local < pd.Timestamp(end))]

    orders: list[dict] = []
    upper = lower = 0
    if len(buys):
        with_id = buys[buys["order_id"] != ""]
        without = buys[buys["order_id"] == ""]
        for order_id, grp in with_id.groupby("order_id", sort=True):
            orders.append({"order_key": f"order:{order_id}", "tickers": sorted(set(grp["ticker"])),
                           "fills": len(grp), "first_fill": str(grp["local_time"].min())})
        upper = lower = with_id["order_id"].nunique()
        if len(without):
            upper += len(without)
            same_day = without.groupby([without["ticker"], without["local_time"].dt.date]).ngroups
            lower += same_day
            detail.append(f"{len(without)} BUY fill(s) have no order_id; counted as {lower - with_id['order_id'].nunique()}"
                          f"-{len(without)} orders (partial fills cannot be grouped with certainty).")
            for _, row in without.iterrows():
                orders.append({"order_key": f"fill:{row['trade_id']}", "tickers": [row["ticker"]],
                               "fills": 1, "first_fill": str(row["local_time"])})

    if meta.game_name is None:
        status, lower_b, upper_b = "unknown", None, None
    elif coverage == "complete":
        status = "compliant" if lower >= required else ("unknown" if upper >= required else "gap")
        lower_b, upper_b = lower, upper
        if status == "unknown":
            detail.append("Compliance depends on whether order_id-less fills are separate orders.")
    elif lower >= required:
        # Even an incomplete ledger proves compliance once the lower bound is met.
        status, lower_b, upper_b = "compliant", lower, None
    else:
        status, lower_b, upper_b = "unknown", lower, None
        detail.append("Monthly BUY count is UNKNOWN (not zero) because history is incomplete.")

    return MonthlyBuyStatus(month, timezone, required, status, lower_b, upper_b, coverage,
                            days_remaining, weekdays_remaining, detail, orders)
