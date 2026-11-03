"""Extra Markdown sections for the weekly report: overlays, vetoes, data status."""

from __future__ import annotations

from src.build_weight_prompt import RunEvidence, catalyst_data_available
from src.macro import render_macro_report


def score_breakdown(evidence: RunEvidence) -> list[str]:
    scores = evidence.final_scores
    rows = ["| Ticker | Weight | Final | Momentum/vol | Sector tilt | Sentiment |", "|---|---:|---:|---:|---:|---:|"]
    for row in evidence.portfolio.itertuples():
        s = scores.loc[row.ticker]
        rows.append(f"| {row.ticker} | {int(row.weight_pct)}% | {s['final_score']:+.3f} | {s['composite_score']:+.3f} | "
                    f"{s['sector_contribution']:+.3f} | {s['sentiment_contribution']:+.3f} |")
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


def overlay_sections(evidence: RunEvidence) -> list[str]:
    """Report lines for macro, score breakdown, catalysts, rejections, and data status."""
    lines = ["", "## Macro Regime", "", render_macro_report(evidence.macro), "",
             "## Score Breakdown", ""]
    lines += score_breakdown(evidence)
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
