# Agent committee

An optional layer on top of the weekly pipeline. **Python does the data and the math and enforces every rule.** Agents only reason over packets Python wrote, and only write their own memo files. The validator is the final gate. The system is recommend-only: you place every trade by hand in the class game.

## Objective given to every agent

> Objective: maximize expected FF3 alpha. Returns explained by market, size (SMB), or value (HML) exposure earn no credit. Momentum and low-volatility returns are not FF3 factors, so they can show up as alpha. Cash and SPY have roughly zero alpha and scale the portfolio's alpha down, so hold extra cash only with a stated, evidence-based reason.

## Order

```
python -m src.run_pipeline --agents        writes runs/<id>/agent_inputs/ and runs/.current_run
  1  Macro Strategist
  2  Equity Research Analysts (parallel, 5 tickers each)   3  NBA Scout   4  Politician Tracker
  5  Risk Manager
  6  LeBron
python -m src.agents.assemble runs/<id>    validates memos, applies vetoes/haircuts, writes weight_prompt.txt
  7  Portfolio Manager                     reads only weight_prompt.txt, writes recommendation_reason.txt
python -m src.validate_allocation ...      up to 2 retries with VALIDATOR FEEDBACK (src.agents.retry)
python -m src.trade_list runs/<id>
```

`/weekly-picks` (`.claude/skills/weekly-picks/SKILL.md`) runs all of this. Project agents load when a Claude Code session starts, so start a new session after adding or editing them.

## Roles and authority

| # | Role | File | Model | Reads | Can change the outcome by |
|---|---|---|---|---|---|
| 1 | Macro Strategist | `macro-strategist.md` | sonnet | macro packet | Recommending cash between the regime target and +10 points. It cannot change the regime label. |
| 2 | Equity Research Analyst | `equity-research-analyst.md` | sonnet | firm packets, risk packet, holdings review | Advice only. |
| 3 | NBA Scout | `nba-scout.md` | sonnet | scout rubric, firm packets | Advice only. Bench or Cut holdings are flagged as sell considerations. |
| 4 | Politician Tracker | `politician-tracker.md` | sonnet | politician packet | Advice only. Never infers intent or alleges wrongdoing. |
| 5 | Risk Manager | `risk-manager.md` | opus | all packets, memos 1-4 | **Vetoes** (holding becomes SELL; candidate can't be bought) and **haircuts** (holding becomes TRIM), each with evidence. Python applies them. |
| 6 | LeBron | `lebron.md` | sonnet | all packets, memos 1-5 | One captain's call: a swap, a reweight of up to 2 points, a keep that overrules a rank-based SELL (never a veto, ban, ETF, or failed eligibility), or none. The PM must accept it or explain why not. |
| 7 | Portfolio Manager | `portfolio-manager.md` | opus | `weight_prompt.txt` only | The final allocation, within the committee bounds. |
| 8 | Validator | Python | — | PM response, `agents/committee.json`, holdings review | Pass or fail. |

LeBron is a persona agent inspired by LeBron James's publicly known on-court approach. It never claims to be him, never invents quotes, and never implies he endorses a pick. Every LeBron memo ends: *"Persona agent inspired by LeBron James's on-court approach; not affiliated with or endorsed by him."*

To change a model, edit the `model:` line in the agent file. That is a strategy change and appears in the next form diff.

## Committee bounds (enforced by the validator)

- At most `AGENT_MAX_SWAPS` (3) new names versus the holdings-aware quant portfolio. LeBron's call counts toward this.
- Cash between the regime target and the target plus `AGENT_MAX_EXTRA_CASH_PCT` (10), within the active limit profile.
- Vetoed names are excluded, and haircut holdings stay at or below their caps.
- New names come from `eligible_candidates.csv`. Holdings stay only if eligible and not vetoed.
- A "Committee response" section is required.
- All class rules, strategy limits, and holdings actions still apply.

## Memo schemas (`src/agents/schemas.py`)

Each memo is a Markdown file plus a JSON sidecar in `runs/<id>/agents/`. A missing, malformed, or out-of-bounds sidecar makes the memo "Memo unavailable: <reason>", and its contents are not used.

| Role | JSON fields |
|---|---|
| Macro Strategist | `regime_view`, `cash_recommendation_pct`, `sector_stance{}`, `key_risks[]` |
| Equity Research Analyst | `ticker`, `is_holding`, `conviction` (1-5), `stance` (buy/hold/trim/sell/avoid), `bull[]`, `bear[]`, `evidence_refs[]` |
| NBA Scout | `board[]` of `ticker`, `tier` (Franchise/Starter/Rotation/Bench/Cut), `grades{}`, `one_liner` |
| Politician Tracker | `signals[]` of `ticker`, `signal` (confirm/contradict/nothing), `filings_used[]` |
| Risk Manager | `vetoes[]`, `haircuts[]`, `sell_recommendations[]` (each `ticker`, `reason`, `evidence`), `portfolio_warnings[]` |
| LeBron | `captains_call{type, out, in, ticker, delta_pct, reason}`, `rationale` |

## Safeguards

- **File access is enforced, not just requested.** Each agent has only Read, Glob, Grep, and Write. A `PreToolUse` hook (`.claude/hooks/agent_guard.py`) limits each role to the current run's `agent_inputs/`, the earlier memos it may read, and its own memo files. The PM can read only `weight_prompt.txt` and write only `recommendation_reason.txt`. Other runs, `private/`, `.env`, the network, and shell commands are blocked. Hooks run only after you trust the project folder in Claude Code.
- **External text is untrusted.** Headlines and every memo are wrapped in delimiters with a random per-run token, and memo text cannot close its own block.
- **The prompt is preserved.** The four original sections are byte-identical with or without the committee; the memos form a fifth section.

## Audit contract

`weight_prompt.txt` is the exact prompt given to the PM, and `recommendation_reason.txt` is its complete, unedited response. The PM agent's own system prompt is not part of `weight_prompt.txt`, so it is kept minimal (read the file, follow it, write the response). Its hash is stored with every run in `evidence/strategy_fingerprint.json`. If the PM is retried, each failed attempt's prompt, response, and validator table are saved in `pm_attempts/`, and the final `weight_prompt.txt` is the prompt of the attempt that produced the final response.

## Limitations

- LLM judgment is untested. The committee may add perspective, not evidence of skill.
- Persona agents (NBA Scout, LeBron) are reasoning metaphors.
- Politician data covers the House only and lags trades by up to 45 days.
- A full run uses roughly 10 subagents of your Claude plan.
