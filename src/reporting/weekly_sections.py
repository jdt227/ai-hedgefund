"""Extra Markdown sections for the weekly report: overlays, vetoes, data status."""

from __future__ import annotations

import pandas as pd

from config import settings
from src.build_weight_prompt import RunEvidence, catalyst_data_available
from src.macro import render_macro_report


def score_breakdown(evidence: RunEvidence) -> list[str]:
    scores = evidence.final_scores
    rows = ["| Ticker | Weight | Final | Momentum/vol | Sector tilt | Sentiment | Scout |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in evidence.portfolio.itertuples():
        s = scores.loc[row.ticker]
        scout = s.get("scout_contribution", 0.0)
        rows.append(f"| {row.ticker} | {int(row.weight_pct)}% | {s['final_score']:+.3f} | {s['composite_score']:+.3f} | "
                    f"{s['sector_contribution']:+.3f} | {s['sentiment_contribution']:+.3f} | {scout:+.3f} |")
    return rows


def catalyst_lines(evidence: RunEvidence) -> list[str]:
    if not catalyst_data_available(evidence.catalysts):
        return ["Catalyst data unavailable for this run."]
    lines = []
    for ticker in evidence.portfolio["ticker"]:
        flags = evidence.catalysts.flags.get(ticker, [])
        text = "; ".join(f"{f['flag']} ({f['reason']})" for f in flags) or "none"
        earnings = evidence.catalysts.earnings.get(ticker, "n/a")
        lines.append(f"- {ticker}: {text}. Next earnings: {earnings}.")
    return lines


def holdings_lines(evidence: RunEvidence) -> list[str]:
    if not len(evidence.review):
        return ["No holdings file, so there is nothing to review."]
    rows = [f"Weights are {evidence.weight_basis}. These are suggestions; you place every trade.", "",
            "| Ticker | Weight | Rank | Action | Target | Reasons |", "|---|---:|---:|---|---:|---|"]
    for r in evidence.review.itertuples():
        rank = "—" if pd.isna(r.rank) else int(r.rank)
        target = "—" if pd.isna(r.target_weight_pct) else f"{int(r.target_weight_pct)}%"
        rows.append(f"| {r.ticker} | {r.current_weight_pct:.1f}% | {rank} | {r.action_label} | {target} | {r.reasons} |")
    return rows


def contest_lines(evidence: RunEvidence) -> list[str]:
    """Realized alpha to date, with the official number first and the proxy labeled."""
    from src.evaluation.contest import summarize

    contest = evidence.contest
    if not contest:
        return ["Contest metric unavailable for this run."]
    realized = contest["realized"]
    lines = [f"Series: {contest['series_kind']}, {contest['series_points']} point(s). "
             "The grade is FF3 alpha; only the official line uses the factors the class is graded on.", "",
             f"- **Official (Ken French):** {summarize(realized['official'])}",
             f"- Proxy estimate (ETFs, NOT the graded number): {summarize(realized['proxy'])}"]
    loadings = contest["ex_ante_loadings"]
    if loadings.get("status") != "unavailable":
        lines += ["", "Ex-ante loadings of this week's model portfolio (exposure, not performance):",
                  f"- Official: {summarize(loadings['official'], include_alpha=False)}",
                  f"- Proxy: {summarize(loadings['proxy'], include_alpha=False)}",
                  "- No alpha shown: past returns of stocks chosen for their past returns are hindsight."]
    lines += [""] + [f"- Note: {n}" for n in contest["notes"]]
    lines += [f"- Source {k}: {v}" for k, v in contest["sources"].items()]
    return lines


def overlay_sections(evidence: RunEvidence) -> list[str]:
    """Report lines for macro, score breakdown, catalysts, rejections, and data status."""
    lines = ["", "## Macro Regime", "", render_macro_report(evidence.macro), "",
             "## Score Breakdown", ""]
    lines += score_breakdown(evidence)
    lines += ["", "## Holdings Review", ""] + holdings_lines(evidence)
    invested = int(evidence.portfolio["weight_pct"].sum()) if len(evidence.portfolio) else 0
    if invested < settings.limits()["max_equity_pct"]:
        lines += ["", f"The model portfolio invests {invested}%: too few names fit the buy cutoff and limits, "
                      "so cash fills the rest."]
    lines += ["", "## Contest Metric", ""] + contest_lines(evidence)
    lines += ["", "## Catalysts", ""] + catalyst_lines(evidence)
    lines += ["", "## Vetoes and Rejected Candidates", ""]
    if evidence.rule_failures:
        lines += [f"- **RULE FAILURE:** {failure}" for failure in evidence.rule_failures]
    if len(evidence.rejected):
        lines += [f"- {r.ticker} (rank {r.rank}): {r.reason}" for r in evidence.rejected.itertuples()]
    if not evidence.rule_failures and not len(evidence.rejected):
        lines.append("None.")
    lines += ["", "## Data-Source Status", ""]
    lines += [f"- {name}: {state}" for name, state in evidence.source_status.items()]
    return lines
