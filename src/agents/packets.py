"""Write the agent input packets for one run (python -m src.run_pipeline --agents).

Agents read only these files and earlier memos. External text stays inside the
run's UntrustedBlocks delimiters. The four original prompt sections are saved so
the assembler can reproduce them byte for byte.
"""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from config import settings
from src.account.inputs import load_account
from src.account.reconcile import count_monthly_buys, reconcile
from src.banned_products import banned_reason, counts_as_stock
from src.build_weight_prompt import (RunEvidence, candidates, firm_block, holdings_section, loadings_lines,
                                     selectable_tickers)
from src.untrusted import UntrustedBlocks

SCOUT_RUBRIC = """# Scout rubric (computed by Python; cite these grades, do not invent new ones)

Grades are cross-sectional percentiles among scored stocks: A >= 80th, B >= 60th, C >= 40th, D >= 20th, else F.

1. Clutch: downside capture vs SPY on SPY down days, last 126 sessions (lower is better).
2. Durability: 1-year max drawdown and recovery to the 1-year high.
3. Injury report: catalyst flags plus earnings inside the window (fewer is better).
4. Team fit: average 63-session correlation with the other top candidates (lower is better).
5. Scoring: 6-month return. Display only: momentum is already in the quant score.

Only clutch, durability, and team fit move the score (at most +/-0.05).
Basketball scouting is a reasoning metaphor, not a proven source of returns.
"""


def class_rules_status(evidence: RunEvidence) -> str:
    """Floor, month-to-date buys, and banned holdings, from the account files."""
    account = load_account(evidence.account_dir)
    positions = reconcile(account).positions
    stocks = [t for t in positions["ticker"] if counts_as_stock(t)]
    banned = {t: banned_reason(t) for t in positions["ticker"] if banned_reason(t)}
    tz = settings.CLASS_RULES["month_timezone"]
    month = evidence.now_utc.astimezone(ZoneInfo(tz)).strftime("%Y-%m")
    monthly = count_monthly_buys(account, month, tz, settings.CLASS_RULES["min_buy_orders_per_month"],
                                 now=evidence.now_utc)
    executed = "unknown" if monthly.count_lower_bound is None else f"at least {monthly.count_lower_bound}"
    return "\n".join([
        "# Class rules status", "",
        f"- Stocks held now (ETFs do not count): {len(stocks)} ({', '.join(stocks) or 'none'}); minimum "
        f"{settings.CLASS_RULES['min_stocks']} at all times.",
        f"- Buy orders executed in {month}: {executed} of {monthly.required}; status {monthly.status}; "
        f"{monthly.weekdays_remaining} weekdays left.",
        "- Banned holdings: " + ("; ".join(f"{t} ({r})" for t, r in banned.items()) or "none"),
        "- Equities and ETFs only: no crypto, options, or leveraged or inverse products.",
    ]) + "\n"


def politician_packet(evidence: RunEvidence, tickers: list[str], blocks: UntrustedBlocks) -> str:
    """Parsed House filings for candidates and holdings. Names come from public filings."""
    status = evidence.source_status.get("house_ptr", "unavailable")
    lines = ["# Politician packet (House periodic transaction reports only)", "",
             f"Source status: {status}",
             f"Window: filings made public in the last {settings.POLITICIAN_LOOKBACK_DAYS} days, by filing date. "
             "Members may file up to 45 days after a trade, and amounts are ranges. Senate filings are not used.", ""]
    if not len(evidence.politics):
        return "\n".join(lines + ["No parsed House stock trades for this run."]) + "\n"
    summary = evidence.politics.set_index("ticker")
    for ticker in tickers:
        if ticker in summary.index:
            lines.append(f"- {ticker}: {summary.at[ticker, 'evidence']}; filings {summary.at[ticker, 'filings']}")
        else:
            lines.append(f"- {ticker}: no disclosed stock trades in the window")
    return "\n".join(lines) + "\n"


def write_packets(evidence: RunEvidence, run_dir: Path, sections: dict[str, str], blocks: UntrustedBlocks) -> Path:
    """Write agent_inputs/ for the committee and return its path."""
    out = run_dir / "agent_inputs"
    out.mkdir(parents=True, exist_ok=True)
    chosen = candidates(evidence)
    tickers = list(chosen.index)
    held = evidence.review["ticker"].tolist() if len(evidence.review) else []

    (out / "macro_packet.md").write_text("# Macro packet\n\n" + sections["macro"] + "\n", encoding="utf-8")
    for ticker, row in chosen.iterrows():
        (out / f"firm_{ticker}.md").write_text(firm_block(ticker, row, evidence, blocks) + "\n", encoding="utf-8")
    (out / "risk_packet.md").write_text("# Risk packet\n\n" + sections["risk"] + "\n", encoding="utf-8")
    (out / "holdings_review.md").write_text(
        "# Holdings review\n\n" + holdings_section(evidence, evidence.account_dir) + "\n", encoding="utf-8")
    scout = evidence.scout[evidence.scout["ticker"].isin(tickers)] if len(evidence.scout) else evidence.scout
    grades = scout[["ticker", *[c for c in scout.columns if c.startswith("grade_")], "evidence"]].to_markdown(index=False) \
        if len(scout) else "Scout grades unavailable for this run."
    (out / "scout_rubric.md").write_text(SCOUT_RUBRIC + "\n## Grades for candidates and holdings\n\n" + grades + "\n",
                                         encoding="utf-8")
    (out / "politician_packet.md").write_text(politician_packet(evidence, tickers, blocks), encoding="utf-8")
    quant = evidence.portfolio[["ticker", "sector", "weight_pct"]] if len(evidence.portfolio) else pd.DataFrame(
        columns=["ticker", "sector", "weight_pct"])
    quant.to_csv(out / "quant_portfolio.csv", index=False)
    eligible = evidence.final_scores[evidence.final_scores.index.isin(selectable_tickers(evidence))].copy()
    eligible["rank"] = range(1, len(eligible) + 1)
    eligible = eligible[~eligible.index.isin(list(evidence.vetoed))]
    eligible.assign(sector=[evidence.universe.get(t) for t in eligible.index])[["rank", "sector", "final_score"]].to_csv(
        out / "eligible_candidates.csv")
    (out / "class_rules_status.md").write_text(class_rules_status(evidence), encoding="utf-8")
    (out / "prompt_sections.json").write_text(json.dumps(sections, indent=1), encoding="utf-8")

    batch = settings.AGENT_EQUITY_BATCH_SIZE
    analyst_tickers = list(dict.fromkeys(tickers + [t for t in held if t in evidence.final_scores.index]))
    manifest = {
        "run_id": run_dir.name, "run_date": evidence.run_date.isoformat(),
        "regime": evidence.macro.regime, "regime_cash_target": evidence.macro.cash_target_pct,
        "limits_profile": settings.PORTFOLIO_LIMITS, "limits": settings.limits(),
        "objective": settings.OBJECTIVE_STATEMENT,
        "bounds": {"max_swaps": settings.AGENT_MAX_SWAPS, "max_extra_cash_pct": settings.AGENT_MAX_EXTRA_CASH_PCT,
                   "max_reweight_pct": settings.AGENT_MAX_REWEIGHT_PCT},
        "holdings": held, "candidates": tickers,
        "equity_batches": [analyst_tickers[i:i + batch] for i in range(0, len(analyst_tickers), batch)],
        "untrusted_token": blocks.token,
        "files": sorted(p.name for p in out.iterdir()),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return out


def loadings_text(evidence: RunEvidence) -> str:
    return "\n".join(loadings_lines(evidence))
