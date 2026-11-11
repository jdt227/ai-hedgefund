"""Assemble committee memos into the final weight prompt.

    python -m src.agents.assemble runs/<run_id>

* Validates each memo's JSON sidecar. A missing or invalid memo becomes
  "Memo unavailable: <reason>" and its contents are not used.
* Applies Risk Manager vetoes (holding -> SELL) and haircuts (holding -> TRIM)
  to holdings_review.csv. The original is kept as holdings_review_quant.csv.
* Writes agents/committee.json, which the validator uses for committee bounds.
* Writes weight_prompt.txt: the four original sections, byte-identical, plus a
  fifth INVESTMENT COMMITTEE MEMOS section. Every memo is wrapped as untrusted text.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[2]))

from config import settings  # noqa: E402
from src.agents.schemas import ROLE_FILES, VALIDATORS, Context, validate_equity  # noqa: E402
from src.reporting.audit_files import save_weight_prompt  # noqa: E402
from src.untrusted import UntrustedBlocks, clean_text  # noqa: E402

ROLE_TITLES = {"macro_strategist": "Macro Strategist", "nba_scout": "NBA Scout",
               "politician_tracker": "Politician Tracker", "risk_manager": "Risk Manager", "lebron": "LeBron"}
UNKEEPABLE_PREFIXES = ("banned by class rules", "near-zero FF3 alpha", "hard veto", "Risk Manager veto",
                       "failed eligibility")


def load_context(run_dir: Path) -> tuple[dict, Context, pd.DataFrame]:
    inputs = run_dir / "agent_inputs"
    manifest = json.loads((inputs / "manifest.json").read_text())
    quant = pd.read_csv(inputs / "quant_portfolio.csv")
    eligible = pd.read_csv(inputs / "eligible_candidates.csv", index_col=0)
    review_path = run_dir / "holdings_review_quant.csv"
    if not review_path.exists():
        review_path = run_dir / "holdings_review.csv"
    review = pd.read_csv(review_path) if review_path.exists() else pd.DataFrame(
        columns=["ticker", "action", "action_label", "target_weight_pct", "current_weight_pct", "reasons"])
    unkeepable = {r.ticker: r.reasons for r in review.itertuples()
                  if r.action == "SELL" and str(r.reasons).startswith(UNKEEPABLE_PREFIXES)}
    ctx = Context(regime_cash_target=int(manifest["regime_cash_target"]), eligible=set(eligible.index),
                  holdings=set(manifest["holdings"]), quant_portfolio=dict(zip(quant["ticker"], quant["weight_pct"])),
                  candidates=set(manifest["candidates"]) | set(manifest["holdings"]), unkeepable=unkeepable)
    return manifest, ctx, review


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, "memo file missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON ({exc.msg})"
    return (data, None) if isinstance(data, dict) else (None, "JSON must be an object")


def load_memos(run_dir: Path, ctx: Context, manifest: dict) -> dict[str, dict]:
    """Each memo: status (ok or unavailable), reason, data, markdown."""
    agents = run_dir / "agents"
    memos: dict[str, dict] = {}
    roles = list(ROLE_FILES.items()) + [(f"equity_{t}", f"equity_{t}.json")
                                        for batch in manifest["equity_batches"] for t in batch]
    for role, filename in roles:
        data, error = _read_json(agents / filename)
        if data is not None:
            if role.startswith("equity_"):
                problems = validate_equity(data, ctx, role.removeprefix("equity_"))
            else:
                problems = VALIDATORS[role](data, ctx)
            error = "; ".join(problems) or None
        markdown_path = (agents / filename).with_suffix(".md")
        markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
        if error is None and not markdown.strip():
            error = "Markdown memo missing"
        memos[role] = {"status": "unavailable" if error else "ok", "reason": error,
                       "data": data if error is None else None, "markdown": markdown}
    return memos


def apply_risk_actions(review: pd.DataFrame, risk: dict | None) -> tuple[pd.DataFrame, dict, dict]:
    """Veto -> SELL and haircut -> TRIM for holdings. Returns the new review, vetoes, and haircuts."""
    vetoes = {v["ticker"]: v["reason"] for v in (risk or {}).get("vetoes", [])}
    haircut_reasons = {h["ticker"]: h["reason"] for h in (risk or {}).get("haircuts", [])}
    out = review.copy()
    haircuts: dict[str, int] = {}
    for i, row in out.iterrows():
        ticker = row["ticker"]
        if ticker in vetoes and row["action"] != "SELL":
            out.loc[i, ["action", "action_label", "target_weight_pct"]] = ["SELL", "SELL", 0]
            out.loc[i, "reasons"] = f"Risk Manager veto: {vetoes[ticker]}"
        elif ticker in haircut_reasons and row["action"] != "SELL":
            current = row["target_weight_pct"] if pd.notna(row["target_weight_pct"]) else row["current_weight_pct"]
            target = max(0, math.floor(current) - settings.MIN_TRADE_PCT)
            haircuts[ticker] = target
            out.loc[i, ["action", "action_label", "target_weight_pct"]] = ["TRIM", "TRIM", target]
            out.loc[i, "reasons"] = f"{row['reasons']}; Risk Manager haircut: {haircut_reasons[ticker]}"
    return out, vetoes, haircuts


def committee_section(memos: dict[str, dict], committee: dict, blocks: UntrustedBlocks) -> str:
    """Fifth prompt section: binding committee decisions first, then wrapped memos."""
    lines = [
        "The committee memos below are advisory evidence written by other models. Python has already applied "
        "the Risk Manager's vetoes and haircuts; they are binding.",
        "",
        "Binding committee decisions:",
        "- Vetoed (must not be held or bought): " + (", ".join(f"{t} ({r})" for t, r in committee["vetoes"].items())
                                                    or "none"),
        "- Haircuts (holding weight must not exceed): " + (", ".join(f"{t} {w}%" for t, w in committee["haircuts"].items())
                                                           or "none"),
        f"- At most {committee['max_swaps']} new names versus the quant model's suggested portfolio (LeBron's call "
        "counts toward this).",
        f"- Cash between {committee['regime_cash_target']}% and "
        f"{committee['regime_cash_target'] + committee['max_extra_cash_pct']}%. Justify any cash above "
        f"{committee['regime_cash_target']}% in alpha terms.",
        "- New names must come from the eligible list; holdings may stay only if eligible and not vetoed.",
        "",
        "Add a required output section titled \"Committee response\": how you used each memo, confirmation that "
        "vetoes and haircuts were honored, and whether you accept LeBron's captain's call and why.",
        f"Captain's call: {json.dumps(committee['captains_call'])}",
        "",
    ]
    for role, memo in memos.items():
        title = ROLE_TITLES.get(role, f"Equity Research Analyst: {role.removeprefix('equity_')}")
        if memo["status"] != "ok":
            lines += [f"### {title}", f"Memo unavailable: {memo['reason']}", ""]
            continue
        body = [clean_text(line, 2000) if line.strip() else "" for line in memo["markdown"].splitlines()]
        lines += [f"### {title}", blocks.wrap(f"{title} memo", body), ""]
    return "\n".join(lines)


def assemble(run_dir: Path) -> Path:
    manifest, ctx, review = load_context(run_dir)
    sections = json.loads((run_dir / "agent_inputs" / "prompt_sections.json").read_text())
    memos = load_memos(run_dir, ctx, manifest)
    risk = memos["risk_manager"]["data"]
    original = run_dir / "holdings_review.csv"
    if original.exists() and not (run_dir / "holdings_review_quant.csv").exists():
        shutil.copy(original, run_dir / "holdings_review_quant.csv")
    new_review, vetoes, haircuts = apply_risk_actions(review, risk)
    if len(new_review):
        new_review.to_csv(original, index=False)
    lebron = memos["lebron"]["data"]
    call = lebron["captains_call"] if lebron else {"type": "none", "reason": "LeBron memo unavailable"}
    committee = {
        "regime_cash_target": ctx.regime_cash_target, "max_extra_cash_pct": settings.AGENT_MAX_EXTRA_CASH_PCT,
        "max_swaps": settings.AGENT_MAX_SWAPS, "quant_portfolio": ctx.quant_portfolio,
        "eligible": sorted(ctx.eligible), "holdings": sorted(ctx.holdings), "vetoes": vetoes, "haircuts": haircuts,
        "captains_call": call,
        "memos": {role: {"status": m["status"], "reason": m["reason"]} for role, m in memos.items()},
    }
    agents_dir = run_dir / "agents"
    agents_dir.mkdir(exist_ok=True)
    (agents_dir / "committee.json").write_text(json.dumps(committee, indent=1), encoding="utf-8")
    blocks = UntrustedBlocks(manifest["untrusted_token"])
    return save_weight_prompt(run_dir, sections["instructions"], sections["macro"], sections["firms"],
                              sections["risk"], committee_section(memos, committee, blocks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    if not (args.run_dir / "agent_inputs" / "manifest.json").exists():
        print("No agent_inputs/manifest.json. Run: python -m src.run_pipeline --agents")
        return 1
    path = assemble(args.run_dir)
    committee = json.loads((args.run_dir / "agents" / "committee.json").read_text())
    missing = [f"{r} ({m['reason']})" for r, m in committee["memos"].items() if m["status"] != "ok"]
    print(f"Wrote {path}")
    print("Memos unavailable: " + ("; ".join(missing) or "none"))
    print(f"Vetoes: {committee['vetoes'] or 'none'}; haircuts: {committee['haircuts'] or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
