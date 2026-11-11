"""JSON sidecar schemas for each committee memo, validated without extra dependencies.

Each validator returns a list of problems; an empty list means the memo is valid.
Context (regime cash target, eligible tickers, holdings, quant portfolio) comes
from agents/committee.json so bounds are checked against this run's data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config import settings

STANCES = {"buy", "hold", "trim", "sell", "avoid"}
TIERS = {"Franchise", "Starter", "Rotation", "Bench", "Cut"}
SIGNALS = {"confirm", "contradict", "nothing"}
CALL_TYPES = {"swap", "reweight", "keep", "none"}
ROLE_FILES = {
    "macro_strategist": "macro_strategist.json",
    "nba_scout": "nba_scout.json",
    "politician_tracker": "politician_tracker.json",
    "risk_manager": "risk_manager.json",
    "lebron": "lebron.json",
}


@dataclass
class Context:
    regime_cash_target: int
    eligible: set[str]
    holdings: set[str]
    quant_portfolio: dict[str, int]
    candidates: set[str]
    unkeepable: dict[str, str] = field(default_factory=dict)   # holdings LeBron may not keep, with reason


def _type(problems: list[str], data: dict, key: str, kind, label: str | None = None) -> bool:
    if key not in data:
        problems.append(f"missing '{key}'")
        return False
    value = data[key]
    ok = isinstance(value, kind) and not (kind is int and isinstance(value, bool))
    if not ok:
        problems.append(f"'{key}' must be {label or kind.__name__}")
    return ok


def _string_list(problems: list[str], data: dict, key: str) -> None:
    if _type(problems, data, key, list, "a list") and not all(isinstance(v, str) for v in data[key]):
        problems.append(f"'{key}' must contain only strings")


def validate_macro(data: dict, ctx: Context) -> list[str]:
    problems: list[str] = []
    _type(problems, data, "regime_view", str)
    _type(problems, data, "sector_stance", dict, "an object")
    _string_list(problems, data, "key_risks")
    if _type(problems, data, "cash_recommendation_pct", int, "an integer"):
        low, high = ctx.regime_cash_target, ctx.regime_cash_target + settings.AGENT_MAX_EXTRA_CASH_PCT
        if not low <= data["cash_recommendation_pct"] <= high:
            problems.append(f"cash_recommendation_pct must be between {low} and {high}")
    return problems


def validate_equity(data: dict, ctx: Context, expected_ticker: str | None = None) -> list[str]:
    problems: list[str] = []
    if _type(problems, data, "ticker", str):
        if expected_ticker and data["ticker"] != expected_ticker:
            problems.append(f"ticker {data['ticker']} does not match file for {expected_ticker}")
        if data["ticker"] not in ctx.candidates:
            problems.append(f"{data['ticker']} is not a candidate or holding in this run")
    _type(problems, data, "is_holding", bool, "true or false")
    if _type(problems, data, "conviction", int, "an integer") and not 1 <= data["conviction"] <= 5:
        problems.append("conviction must be 1-5")
    if _type(problems, data, "stance", str) and data["stance"] not in STANCES:
        problems.append(f"stance must be one of {sorted(STANCES)}")
    for key in ("bull", "bear", "evidence_refs"):
        _string_list(problems, data, key)
    return problems


def validate_scout(data: dict, ctx: Context) -> list[str]:
    problems: list[str] = []
    if not _type(problems, data, "board", list, "a list"):
        return problems
    for i, row in enumerate(data["board"]):
        if not isinstance(row, dict):
            problems.append(f"board[{i}] must be an object")
            continue
        sub: list[str] = []
        _type(sub, row, "ticker", str)
        if _type(sub, row, "tier", str) and row["tier"] not in TIERS:
            sub.append(f"tier must be one of {sorted(TIERS)}")
        _type(sub, row, "grades", dict, "an object")
        _type(sub, row, "one_liner", str)
        problems += [f"board[{i}]: {p}" for p in sub]
    return problems


def validate_politician(data: dict, ctx: Context) -> list[str]:
    problems: list[str] = []
    if not _type(problems, data, "signals", list, "a list"):
        return problems
    for i, row in enumerate(data["signals"]):
        if not isinstance(row, dict):
            problems.append(f"signals[{i}] must be an object")
            continue
        sub: list[str] = []
        _type(sub, row, "ticker", str)
        if _type(sub, row, "signal", str) and row["signal"] not in SIGNALS:
            sub.append(f"signal must be one of {sorted(SIGNALS)}")
        _string_list(sub, row, "filings_used")
        problems += [f"signals[{i}]: {p}" for p in sub]
    return problems


def _ticker_items(problems: list[str], data: dict, key: str) -> None:
    if not _type(problems, data, key, list, "a list"):
        return
    for i, item in enumerate(data[key]):
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item.get(k)
                                                 for k in ("ticker", "reason", "evidence")):
            problems.append(f"{key}[{i}] needs non-empty 'ticker', 'reason', and 'evidence'")


def validate_risk(data: dict, ctx: Context) -> list[str]:
    problems: list[str] = []
    _ticker_items(problems, data, "vetoes")
    _ticker_items(problems, data, "haircuts")
    _ticker_items(problems, data, "sell_recommendations")
    _string_list(problems, data, "portfolio_warnings")
    for item in data.get("haircuts", []) if isinstance(data.get("haircuts"), list) else []:
        if isinstance(item, dict) and item.get("ticker") not in ctx.holdings:
            problems.append(f"haircut on {item.get('ticker')}: haircuts apply only to current holdings")
    return problems


def validate_lebron(data: dict, ctx: Context) -> list[str]:
    problems: list[str] = []
    _type(problems, data, "rationale", str)
    if not _type(problems, data, "captains_call", dict, "an object"):
        return problems
    call = data["captains_call"]
    kind = call.get("type")
    if kind not in CALL_TYPES:
        return problems + [f"captains_call.type must be one of {sorted(CALL_TYPES)}"]
    if kind != "none" and not isinstance(call.get("reason"), str):
        problems.append("captains_call.reason is required")
    if kind == "swap":
        if call.get("out") not in ctx.quant_portfolio:
            problems.append("swap 'out' must be in the quant portfolio")
        if call.get("in") not in ctx.eligible or call.get("in") in ctx.quant_portfolio:
            problems.append("swap 'in' must be an eligible stock not already in the quant portfolio")
    elif kind == "reweight":
        delta = call.get("delta_pct")
        if call.get("ticker") not in ctx.quant_portfolio:
            problems.append("reweight 'ticker' must be in the quant portfolio")
        if not isinstance(delta, int) or isinstance(delta, bool) or not 0 < abs(delta) <= settings.AGENT_MAX_REWEIGHT_PCT:
            problems.append(f"reweight delta_pct must be a nonzero integer of at most {settings.AGENT_MAX_REWEIGHT_PCT}")
    elif kind == "keep":
        ticker = call.get("ticker")
        if ticker not in ctx.holdings:
            problems.append("keep 'ticker' must be a current holding")
        elif ticker in ctx.unkeepable:
            problems.append(f"keep is not allowed for {ticker}: {ctx.unkeepable[ticker]}")
    return problems


VALIDATORS = {"macro_strategist": validate_macro, "nba_scout": validate_scout,
              "politician_tracker": validate_politician, "risk_manager": validate_risk, "lebron": validate_lebron}
