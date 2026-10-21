"""Portfolio-aware proposed orders, produced only from official account inputs.

Readiness gate: proposed share quantities require ALL of
  * class-game name (so we know which account this is for),
  * cash balance at the snapshot, rolled forward through a complete ledger,
  * holdings snapshot confirmed complete,
  * fresh market data (not fixture data).
If any is missing, ``order_readiness`` returns blockers with the exact field
and fix, and no quantities are produced. No budget is ever invented.

Sizing rules (v1, deliberately simple):
  * Never sell automatically (``allow_trim_sells`` = False). Overweight
    positions are reported, not traded.
  * Buy only new names ranked within ``buy_rank_cutoff``, in rank order, up
    to ``max_new_buys_per_run``.
  * Each buy targets ``target_new_position_weight`` of equity, reduced to fit
    the position cap, sector headroom, and cash above the buffer.
  * Whole shares unless fractional trading is confirmed. Shares are sized at
    reference price x (1 + slippage buffer), then every constraint is
    re-checked after rounding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Blocker:
    field: str
    reason: str
    fix: str


@dataclass
class OrderProposal:
    ready: bool
    blockers: list[Blocker]
    actions: pd.DataFrame = field(default_factory=pd.DataFrame)   # BUY/HOLD/SELL/REJECTED
    target_portfolio: pd.DataFrame = field(default_factory=pd.DataFrame)
    summary: dict = field(default_factory=dict)


def order_readiness(meta, recon, market_is_fixture: bool, market_stale: bool,
                    account_errors: int) -> list[Blocker]:
    b: list[Blocker] = []
    path = "private/account/account_metadata.json"
    if meta.game_name is None:
        b.append(Blocker("game_name", "Orders must be tied to the exact class game.",
                         f'Set "game_name" in {path} to the exact class-game name.'))
    if meta.cash is None:
        b.append(Blocker("cash", "Cannot size orders without the official cash balance.",
                         f'Set "cash" in {path} to the cash shown at the snapshot time.'))
    if meta.holdings_complete is not True:
        b.append(Blocker("holdings_complete", "The snapshot is not confirmed to list every position.",
                         f'Set "holdings_complete": true in {path} after checking the screenshot.'))
    if meta.ledger_complete_after_snapshot is not True:
        b.append(Blocker("ledger_complete_after_snapshot",
                         "Current cash/positions depend on trades made after the snapshot.",
                         "Record every fill since the snapshot in private/account/trades.csv, then set "
                         f'"ledger_complete_after_snapshot": true in {path}.'))
    if meta.cash is not None and recon.current_cash_status != "verified" and meta.ledger_complete_after_snapshot:
        b.append(Blocker("trades", "Current cash could not be verified from the ledger.",
                         "Fix trade-timing/validation issues reported by `python -m src.cli validate`."))
    if account_errors:
        b.append(Blocker("validation", f"{account_errors} account validation error(s).",
                         "Run `python -m src.cli validate` and fix every error."))
    if market_is_fixture:
        b.append(Blocker("market_data", "Fixture data is not real market data.",
                         "Rerun without --fixture once network access works."))
    if market_stale:
        b.append(Blocker("market_data", "Market data is stale.",
                         "Rerun with --refresh when the data provider is reachable."))
    return b


def propose_orders(review: pd.DataFrame, ranking: pd.DataFrame, cash: float, sizing: dict,
                   fee_per_trade: float, fractional: bool, min_stocks: int,
                   etf_counts: bool | None) -> OrderProposal:
    actions = []
    holdings_value = float(review["marked_value"].sum()) if len(review) else 0.0
    equity = holdings_value + cash
    buffer = max(sizing["min_cash_buffer_weight"] * equity, sizing["min_cash_buffer_dollars"])
    available = max(0.0, cash - buffer)
    max_pos = sizing["max_position_weight"] * equity
    max_sector = sizing["max_sector_weight"] * equity
    sector_value = review.groupby("sector")["marked_value"].sum().to_dict() if len(review) else {}
    slip = sizing["slippage_buffer"]

    for ticker, pos in review.iterrows():
        note = [pos["review_flag"]]
        w = pos["marked_value"] / equity if equity else 0.0
        if w > sizing["max_position_weight"]:
            note.append(f"weight {w:.1%} exceeds {sizing['max_position_weight']:.0%} cap; "
                        "trim NOT proposed (allow_trim_sells is False) - review manually")
        actions.append({"action": "HOLD", "ticker": ticker, "shares": pos["shares"],
                        "est_price": pos["latest_close"], "est_value": pos["marked_value"],
                        "reason": "; ".join(note)})

    buys = 0
    candidates = ranking[ranking["eligible"]].sort_values("rank")
    for ticker, row in candidates.iterrows():
        if ticker in review.index:
            continue
        if buys >= sizing["max_new_buys_per_run"]:
            break
        if row["rank"] > sizing["buy_rank_cutoff"]:
            break
        sector = row["sector"]
        headroom = max_sector - sector_value.get(sector, 0.0)
        target = min(sizing["target_new_position_weight"] * equity, max_pos, headroom, available - fee_per_trade)
        price = row["reference_close"] * (1 + slip)
        if target < sizing["min_order_dollars"]:
            why = ("sector cap" if headroom < sizing["min_order_dollars"] else
                   "insufficient cash above buffer")
            actions.append({"action": "REJECTED", "ticker": ticker, "shares": 0, "est_price": row["reference_close"],
                            "est_value": 0.0, "reason": f"rank {int(row['rank'])}: {why} "
                            f"(room ${max(target, 0):,.0f} < min order ${sizing['min_order_dollars']:,.0f})"})
            continue
        shares = target / price
        shares = math.floor(shares * 1000) / 1000 if fractional else math.floor(shares)
        cost = shares * price + fee_per_trade
        # Re-validate after rounding.
        ok = (shares > 0 and cost <= available + 1e-9 and shares * price <= max_pos + 1e-9
              and sector_value.get(sector, 0.0) + shares * price <= max_sector + 1e-9)
        if not ok:
            actions.append({"action": "REJECTED", "ticker": ticker, "shares": 0, "est_price": row["reference_close"],
                            "est_value": 0.0, "reason": "failed constraint re-check after rounding"})
            continue
        available -= cost
        sector_value[sector] = sector_value.get(sector, 0.0) + shares * price
        buys += 1
        actions.append({"action": "BUY", "ticker": ticker, "shares": shares, "est_price": row["reference_close"],
                        "est_value": shares * row["reference_close"],
                        "reason": f"rank {int(row['rank'])}, score {row['composite_score']:+.2f}; "
                                  f"target {shares * price / equity:.1%} of equity; sized at "
                                  f"+{slip:.1%} slippage buffer"})

    acts = pd.DataFrame(actions)
    target = acts[acts["action"].isin(["HOLD", "BUY"])].copy()
    target["weight"] = target["est_value"] / equity
    target["asset_type"] = target["ticker"].map(review["asset_type"]).fillna("STOCK")
    cash_after = cash - sum(a["shares"] * a["est_price"] * (1 + slip) + fee_per_trade
                            for a in actions if a["action"] == "BUY")
    n_stocks = int((target["asset_type"] == "STOCK").sum()) + (
        int((target["asset_type"] == "ETF").sum()) if etf_counts else 0)
    summary = {
        "equity_estimate": equity, "cash_before": cash, "cash_buffer": buffer,
        "cash_after_estimate": cash_after, "proposed_buys": buys,
        "stocks_after": n_stocks, "min_stocks": min_stocks,
        "meets_stock_minimum_after": n_stocks >= min_stocks,
        "max_position_weight_after": float(target["weight"].max()) if len(target) else 0.0,
    }
    return OrderProposal(True, [], acts, target.reset_index(drop=True), summary)
