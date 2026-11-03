"""Turn a validated allocation into an ordered list of suggested trades.

    python -m src.trade_list runs/<run_id>

Runs only after the validator has written final_allocation.csv. Share quantities
need official cash, game name, and completeness (the readiness gate in
src/strategy/orders.py). Without them it lists weight changes only and names the
missing fields. It never guesses cash.

Order: normally SELLs, TRIMs, then BUYs/ADDs. If a sale would ever leave fewer
than the class minimum of stocks, enough buys are moved ahead of it.

Recommendation only. Place trades manually in the class game.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from src.account.inputs import DEFAULT_ACCOUNT_DIR, load_account  # noqa: E402
from src.account.reconcile import count_monthly_buys, reconcile  # noqa: E402
from src.banned_products import counts_as_stock  # noqa: E402
from src.market_data.provider import expected_last_completed_session, weekday_gap  # noqa: E402
from src.strategy.orders import order_readiness  # noqa: E402
from src.universe import get_universe  # noqa: E402

DISCLAIMER = "Recommendation only. Place trades manually in the class game."
COMPLIANCE_LABEL = "compliance buy (rebalancing counts toward the 5-buy rule)"
COLUMNS = ["step", "action", "ticker", "shares", "reference_price", "reference_date", "est_dollars", "commission",
           "current_weight_pct", "target_weight_pct", "stocks_held_after", "cash_after", "note"]


@dataclass
class TradeInputs:
    """Everything needed to build the list. ``cash`` is None when not ready for shares."""

    shares: dict[str, float]
    targets: dict[str, int]
    current_weights: dict[str, float]
    ranks: dict[str, float]
    actions: dict[str, str]
    sectors: dict[str, str]
    prices: dict[str, float]
    price_dates: dict[str, str] = field(default_factory=dict)
    cash: float | None = None
    commission: float = 0.0
    fractional: bool = False
    executed_buys_lower_bound: int = 0
    executed_known: bool = False
    runs_remaining: int = 0


@dataclass
class TradeList:
    trades: list[dict]
    notes: list[str]
    buys_in_list: int
    projected_buys: int
    min_stocks_held: int
    shortfall: int


def _diff_weights(inputs: TradeInputs) -> list[dict]:
    """Compare current and target weights. Small changes are ignored; exits are not."""
    trades = []
    for ticker in sorted(set(inputs.targets) | set(inputs.shares)):
        current = inputs.current_weights.get(ticker, 0.0)
        target = inputs.targets.get(ticker, 0)
        held = inputs.shares.get(ticker, 0) > 0
        if target <= 0 and held:
            action = "SELL"
        elif not held and target > 0:
            action = "BUY"
        elif abs(target - current) < settings.MIN_TRADE_PCT:
            continue
        else:
            action = "ADD" if target > current else "TRIM"
        trades.append({"action": action, "ticker": ticker, "current_weight_pct": current,
                       "target_weight_pct": target, "note": ""})
    return trades


def _size(trade: dict, inputs: TradeInputs, equity: float) -> None:
    """Fill in whole shares and dollars. Buys round down, sells round up to reach the target."""
    price = inputs.prices.get(trade["ticker"])
    trade["reference_price"] = price
    trade["reference_date"] = inputs.price_dates.get(trade["ticker"])
    if not price:
        trade["shares"] = None
        trade["note"] = (trade["note"] + " No reference price, so shares could not be sized.").strip()
        return
    held = inputs.shares.get(trade["ticker"], 0)
    change = abs(trade["target_weight_pct"] - trade["current_weight_pct"]) / 100 * equity / price
    if trade["action"] == "SELL":
        shares = held
    elif trade["action"] == "TRIM":
        shares = min(held, change if inputs.fractional else math.ceil(change))
    else:
        shares = math.floor(change * 1000) / 1000 if inputs.fractional else math.floor(change)
    trade["shares"] = shares
    trade["est_dollars"] = round(shares * price, 2)
    trade["commission"] = inputs.commission


def order_for_stock_floor(trades: list[dict], stocks_now: int, minimum: int) -> tuple[list[dict], list[str]]:
    """SELLs (ETFs first), TRIMs, then BUYs/ADDs, moving buys ahead when the floor requires it."""
    sells = sorted((t for t in trades if t["action"] == "SELL"), key=lambda t: counts_as_stock(t["ticker"]))
    trims = [t for t in trades if t["action"] == "TRIM"]
    new_buys = [t for t in trades if t["action"] == "BUY" and counts_as_stock(t["ticker"])]
    others = [t for t in trades if t["action"] == "ADD" or (t["action"] == "BUY" and t not in new_buys)]
    ordered: list[dict] = []
    notes: list[str] = []
    count = stocks_now
    for sell in sells:
        if counts_as_stock(sell["ticker"]):
            while count - 1 < minimum and new_buys:
                buy = new_buys.pop(0)
                buy["note"] = (buy["note"] + f" Placed before sales to keep at least {minimum} stocks.").strip()
                notes.append(f"{buy['ticker']} is bought before {sell['ticker']} is sold, to keep at least "
                             f"{minimum} stocks held at every step.")
                ordered.append(buy)
                count += 1
            count -= 1
        ordered.append(sell)
    return ordered + trims + new_buys + others, notes


def _walk(trades: list[dict], inputs: TradeInputs, stocks_now: int) -> tuple[int, list[str]]:
    """Number the steps and track stocks held, and cash when known."""
    count, low = stocks_now, stocks_now
    cash = inputs.cash
    notes: list[str] = []
    for step, trade in enumerate(trades, start=1):
        trade["step"] = step
        stock = counts_as_stock(trade["ticker"])
        if trade["action"] == "SELL" and stock:
            count -= 1
        elif trade["action"] == "BUY" and stock:
            count += 1
        low = min(low, count)
        trade["stocks_held_after"] = count
        if cash is not None and trade.get("est_dollars") is not None:
            flow = trade["est_dollars"] if trade["action"] in ("SELL", "TRIM") else -trade["est_dollars"]
            cash += flow - inputs.commission
            trade["cash_after"] = round(cash, 2)
            if cash < 0:
                notes.append(f"Cash would go negative at step {step} ({trade['ticker']}). Place sales first if the "
                             "game settles them immediately, or reduce this buy.")
    return low, notes


def add_compliance_buys(trades: list[dict], inputs: TradeInputs, equity: float | None) -> tuple[int, list[str]]:
    """Add the smallest rule-respecting buys when the month would fall short."""
    limits = settings.limits()
    planned = sum(1 for t in trades if t["action"] in ("BUY", "ADD"))
    expected = inputs.runs_remaining * settings.EXPECTED_BUYS_PER_WEEKLY_RUN
    required = settings.CLASS_RULES["min_buy_orders_per_month"]
    shortfall = required - inputs.executed_buys_lower_bound - planned - expected
    notes: list[str] = []
    if shortfall <= 0:
        return 0, notes
    traded = {t["ticker"] for t in trades}
    weights = {t: float(w) for t, w in inputs.targets.items()}
    sector_totals: dict[str, float] = {}
    for ticker, weight in weights.items():
        sector = inputs.sectors.get(ticker, "Other")
        sector_totals[sector] = sector_totals.get(sector, 0.0) + weight
    invested = sum(weights.values())
    candidates = sorted((t for t in inputs.targets if t not in traded and inputs.shares.get(t, 0) > 0
                         and inputs.actions.get(t, "HOLD") == "HOLD"),
                        key=lambda t: inputs.ranks.get(t, 10**9))
    added = 0
    for ticker in candidates:
        if added >= shortfall:
            break
        step = settings.MIN_TRADE_PCT
        sector = inputs.sectors.get(ticker, "Other")
        fits = (weights[ticker] + step <= limits["max_position_pct"]
                and sector_totals[sector] + step <= limits["max_sector_pct"]
                and invested + step <= limits["max_equity_pct"])
        if not fits:
            continue
        trade = {"action": "ADD", "ticker": ticker, "current_weight_pct": inputs.current_weights.get(ticker, 0.0),
                 "target_weight_pct": weights[ticker] + step, "note": COMPLIANCE_LABEL}
        if equity is not None:
            price = inputs.prices.get(ticker)
            if not price:
                continue
            trade.update(reference_price=price, reference_date=inputs.price_dates.get(ticker),
                         shares=math.ceil(step / 100 * equity / price), commission=inputs.commission)
            trade["est_dollars"] = round(trade["shares"] * price, 2)
            # A whole share can exceed one point; the limits must still hold after rounding.
            actual = trade["est_dollars"] / equity * 100
            if weights[ticker] + actual > limits["max_position_pct"] or invested + actual > limits["max_equity_pct"]:
                continue
            step = actual
        trades.append(trade)
        weights[ticker] += step
        sector_totals[sector] += step
        invested += step
        added += 1
    if added < shortfall:
        notes.append(f"{shortfall - added} more buy order(s) may be needed this month, but no HOLD position had room "
                     "under every limit. No limit was broken to reach the count.")
    return added, notes


def build_trade_list(inputs: TradeInputs) -> TradeList:
    """Build the ordered list. Share sizes appear only when cash was supplied."""
    minimum = settings.CLASS_RULES["min_stocks"]
    equity = None
    if inputs.cash is not None:
        held_value = sum(s * inputs.prices.get(t, 0.0) for t, s in inputs.shares.items())
        equity = held_value + inputs.cash
    trades = _diff_weights(inputs)
    if equity is not None:
        for trade in trades:
            _size(trade, inputs, equity)
        dropped = [t for t in trades if t.get("shares") == 0]
        trades = [t for t in trades if t.get("shares") != 0]
    else:
        dropped = []
    added, notes = add_compliance_buys(trades, inputs, equity)
    notes += [f"{t['ticker']}: the change is smaller than one whole share, so it was skipped." for t in dropped]
    stocks_now = sum(1 for t, s in inputs.shares.items() if s > 0 and counts_as_stock(t))
    ordered, floor_notes = order_for_stock_floor(trades, stocks_now, minimum)
    low, walk_notes = _walk(ordered, inputs, stocks_now)
    notes += floor_notes + walk_notes
    if low < minimum:
        notes.append(f"CLASS RULE RISK: stocks held would reach {low}, below the minimum of {minimum}.")
    buys = sum(1 for t in ordered if t["action"] in ("BUY", "ADD"))
    projected = inputs.executed_buys_lower_bound + buys
    expected = inputs.runs_remaining * settings.EXPECTED_BUYS_PER_WEEKLY_RUN
    required = settings.CLASS_RULES["min_buy_orders_per_month"]
    return TradeList(ordered, notes, buys, projected, low, max(0, required - projected - expected))


# ---------------------------------------------------------------------------
# Loading a run, and writing the outputs
# ---------------------------------------------------------------------------
def _load_inputs(run_dir: Path, account_dir: Path, now_utc: datetime) -> tuple[TradeInputs, list, dict]:
    account = load_account(account_dir)
    recon = reconcile(account)
    meta = account.metadata
    targets = {row.Symbol: int(row.Weight) for row in pd.read_csv(run_dir / "final_allocation.csv").itertuples()
               if row.Symbol != "CASH"}
    review_path = run_dir / "holdings_review.csv"
    review = pd.read_csv(review_path) if review_path.exists() else pd.DataFrame(
        columns=["ticker", "current_weight_pct", "rank", "action"])
    prices_path = run_dir / "reference_prices.csv"
    prices = pd.read_csv(prices_path) if prices_path.exists() else pd.DataFrame(
        columns=["ticker", "reference_price", "reference_date"])
    price_map = dict(zip(prices["ticker"], prices["reference_price"]))
    date_map = dict(zip(prices["ticker"], prices["reference_date"]))

    stale = True
    if len(prices):
        latest = pd.to_datetime(prices["reference_date"]).max().date()
        expected = expected_last_completed_session(now_utc)
        stale = weekday_gap(expected, latest) > settings.MAX_STALE_SESSIONS
    blockers = order_readiness(meta, recon, False, stale, len(account.errors))

    tz = settings.CLASS_RULES["month_timezone"]
    month = now_utc.astimezone(ZoneInfo(tz)).strftime("%Y-%m")
    monthly = count_monthly_buys(account, month, tz, settings.CLASS_RULES["min_buy_orders_per_month"], now=now_utc)
    universe = get_universe()
    inputs = TradeInputs(
        shares=dict(zip(recon.positions["ticker"], recon.positions["shares"])), targets=targets,
        current_weights=dict(zip(review["ticker"], review["current_weight_pct"])),
        ranks={r.ticker: r.rank for r in review.itertuples() if pd.notna(r.rank)},
        actions=dict(zip(review["ticker"], review["action"])), sectors=universe, prices=price_map,
        price_dates=date_map, cash=None if blockers else recon.current_cash,
        commission=meta.commission_per_trade or 0.0, fractional=bool(meta.fractional_shares_allowed),
        executed_buys_lower_bound=monthly.count_lower_bound or 0, executed_known=monthly.status != "unknown",
        runs_remaining=max(0, math.ceil(monthly.weekdays_remaining / 5) - 1))
    context = {"month": month, "monthly": monthly, "commission_known": meta.commission_per_trade is not None}
    return inputs, blockers, context


def render_summary(result: TradeList, inputs: TradeInputs, blockers: list, context: dict) -> str:
    """Readable Markdown summary."""
    required = settings.CLASS_RULES["min_buy_orders_per_month"]
    monthly = context["monthly"]
    lines = ["# Suggested trade list", "", f"**{DISCLAIMER}**", ""]
    if blockers:
        lines += ["## Weight changes only", "", "Share quantities are blocked until these are supplied. Cash is never guessed:", ""]
        lines += [f"- `{b.field}`: {b.fix}" for b in blockers]
        lines += ["", "| Step | Action | Ticker | Current | Target | Stocks held after | Note |", "|---:|---|---|---:|---:|---:|---|"]
        for t in result.trades:
            lines.append(f"| {t['step']} | {t['action']} | {t['ticker']} | {t['current_weight_pct']:.1f}% | "
                         f"{t['target_weight_pct']:.0f}% | {t['stocks_held_after']} | {t['note']} |")
    else:
        lines += ["| Step | Action | Ticker | Shares | Ref. price (date) | Est. dollars | Current | Target | Stocks held after | Note |",
                  "|---:|---|---|---:|---|---:|---:|---:|---:|---|"]
        for t in result.trades:
            price = "n/a" if t.get("reference_price") is None else f"${t['reference_price']:,.2f} ({t['reference_date']})"
            dollars = "n/a" if t.get("est_dollars") is None else f"${t['est_dollars']:,.2f}"
            lines.append(f"| {t['step']} | {t['action']} | {t['ticker']} | {t.get('shares', 'n/a')} | {price} | {dollars} | "
                         f"{t['current_weight_pct']:.1f}% | {t['target_weight_pct']:.0f}% | {t['stocks_held_after']} | {t['note']} |")
        if not context["commission_known"]:
            lines += ["", "Commission is unknown, so $0 is assumed. Set `commission_per_trade` if the game charges one."]
    executed = f"{monthly.count_lower_bound}" if inputs.executed_known else "unknown (0 assumed for planning)"
    lines += ["", "## Class rule status", "",
              f"- Stocks held: never below {result.min_stocks_held} during this list (minimum {settings.CLASS_RULES['min_stocks']}; ETFs do not count).",
              f"- Buy orders in {context['month']}: {executed} executed + {result.buys_in_list} in this list, against {required}. "
              f"{monthly.weekdays_remaining} weekdays and about {inputs.runs_remaining} more weekly run(s) remain.",
              "- Suggested trades count only after you place them and record the fills."]
    if result.shortfall:
        lines.append(f"- Possible shortfall this month: {result.shortfall} buy order(s).")
    if result.notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in result.notes]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--account-dir", type=Path, default=DEFAULT_ACCOUNT_DIR)
    args = parser.parse_args(argv)
    if not (args.run_dir / "final_allocation.csv").exists():
        print(f"No final_allocation.csv in {args.run_dir}. Run the validator first; it writes that file only on a pass.")
        return 1
    inputs, blockers, context = _load_inputs(args.run_dir, args.account_dir, datetime.now(timezone.utc))
    result = build_trade_list(inputs)
    pd.DataFrame(result.trades, columns=COLUMNS).to_csv(args.run_dir / "trade_list.csv", index=False)
    summary = render_summary(result, inputs, blockers, context)
    (args.run_dir / "trade_list_summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
