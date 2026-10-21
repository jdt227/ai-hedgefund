"""Render the private Markdown report for one recommendation run."""

from __future__ import annotations

import pandas as pd

from config import strategy as cfg


def _money(x) -> str:
    return "unknown" if x is None or pd.isna(x) else f"${x:,.2f}"


def _pct(x, signed: bool = True) -> str:
    if x is None or pd.isna(x):
        return "n/a"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def render_report(res: dict, metadata: dict) -> str:
    bundle = res["bundle"]
    meta = res["account"].metadata
    L: list[str] = []
    title = "Quant Baseline Recommendation Report"
    if bundle.is_fixture:
        title = "FIXTURE DATA — NOT A RECOMMENDATION — " + title
    L += [f"# {title}", "",
          f"- Run: `{metadata['run_id']}` | strategy `{metadata['strategy_version']}` "
          f"(config `{metadata['config_hash']}`) | git `{(metadata['git']['commit'] or 'n/a')[:8]}`"
          f"{' (dirty working tree)' if metadata['git']['dirty'] else ''}",
          f"- Market data: {bundle.provider}; fetched {bundle.fetched_at_utc}"
          f"{' (cache)' if bundle.from_cache else ''}",
          f"- Data cutoff (last completed session): **{bundle.last_completed_session}** close, 16:00 America/New_York; "
          f"expected {bundle.expected_last_session}; staleness {bundle.staleness_sessions()} weekday session(s)"
          f"{' — STALE' if res['market_stale'] else ''}",
          f"- Incomplete bars dropped: {', '.join(bundle.dropped_incomplete_sessions) or 'none'}",
          f"- Ticker data errors: {', '.join(f'{k} ({v})' for k, v in bundle.ticker_errors.items()) or 'none'}",
          "", "Educational research output. Not investment advice; rankings do not guarantee returns or alpha.", ""]

    # Top five -------------------------------------------------------------
    L += ["## 1. Top-five research candidates (a ranking, not orders)", ""]
    if res["shortfall"]:
        L += [f"**Shortfall:** {res['shortfall']}", ""]
    top = res["top"]
    n_elig = int(res["ranking"]["eligible"].sum())
    if len(top):
        L += [f"Eligible universe: {n_elig} of {len(cfg.UNIVERSE)} configured stocks.", "",
              "| Rank | Ticker | Company | Sector | Ref. close (date) | Score | 12-1M | 6-1M | vs 200d | Vol 63d | MaxDD 1y | Held | Status |",
              "|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|"]
        for _, r in top.iterrows():
            L.append(f"| {r['rank']} | {r['ticker']} | {r['company'] or 'n/a'} | {r['sector']} | "
                     f"{_money(r['reference_close'])} ({r['reference_date']}) | {r['composite_score']:+.2f} | "
                     f"{_pct(r['mom_12_1'])} | {_pct(r['mom_6_1'])} | {_pct(r['trend_200'])} | "
                     f"{_pct(r['vol_63'], False)} | {_pct(r['max_dd_252'])} | {'yes' if r['held'] else 'no'} | {r['status']} |")
        L.append("")
        for _, r in top.iterrows():
            L += [f"**{r['rank']}. {r['ticker']}** — coverage {r['signal_coverage']} signals, "
                  f"{r['history_sessions']} sessions of history",
                  f"- Why: {r['reasons']}", f"- Risks/limits: {r['risks']}", ""]

    # Holdings review ------------------------------------------------------
    rev = res["review"]
    recon = res["recon"]
    c = res["counts"]
    L += ["## 2. Current holdings review", "",
          f"Positions status: **{recon.positions_status}**. Marked values use the latest completed close and assume "
          "no trades since the snapshot unless they are recorded in trades.csv. Screenshot values are kept "
          "separately and are not market quotes.", ""]
    if len(rev):
        L += ["| Ticker | Type | Shares | Snapshot price | Latest close (date) | Change | Marked value | Rank | Score | 12-1M | Vol 63d | Flag |",
              "|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---|"]
        for t, r in rev.iterrows():
            rank = "—" if pd.isna(r["rank"]) else f"{int(r['rank'])}/{r['of_eligible']}"
            score = "—" if pd.isna(r["composite_score"]) else f"{r['composite_score']:+.2f}"
            L.append(f"| {t} | {r['asset_type']} | {r['shares']:g} | {_money(r['snapshot_displayed_price'])} | "
                     f"{_money(r['latest_close'])} ({r['latest_close_date']}) | {_pct(r['close_vs_snapshot_price'])} | "
                     f"{_money(r['marked_value'])} | {rank} | {score} | {_pct(r['mom_12_1'])} | "
                     f"{_pct(r['vol_63'], False)} | {r['review_flag']} |")
        L += ["", f"Marked holdings total (estimate): {_money(rev['marked_value'].sum())}; "
                  f"screenshot holdings total: {_money(meta.reported_holdings_value)}.", ""]
        sec = res["sector_now"].sort_values(ascending=False)
        L += [f"Sector weights ({res['sector_basis']}): " +
              ", ".join(f"{k} {v:.0%}" for k, v in sec.items()), ""]
    L += [f"Stock-count check: {c['individual_stocks']} individual stock(s), {c['etfs']} ETF(s), "
          f"{c['unknown_type']} unclassified, {c['total_positions']} total positions. Basis: {c['basis']}. "
          f"Counted toward the {c['minimum']}-stock minimum: **{c['counted_toward_minimum']}** — "
          + ("meets minimum." if c["meets_minimum"] else f"**SHORT BY {c['shortfall']}** under this interpretation."),
          ""]

    # Monthly buys ---------------------------------------------------------
    m = res["monthly"]
    count = ("unknown" if m.count_lower_bound is None else
             f"{m.count_lower_bound}" + (f"–{m.count_upper_bound}" if m.count_upper_bound not in (None, m.count_lower_bound) else "")
             + ("" if m.coverage == "complete" else " recorded (lower bound)"))
    L += ["## 3. Monthly executed-BUY status", "",
          f"- Month {m.month} ({m.timezone}); required {m.required} buy orders; {m.days_remaining} calendar day(s) / "
          f"{m.weekdays_remaining} weekday(s) remaining including today",
          f"- Executed BUY orders: **{count}**; ledger coverage: {m.coverage}; status: **{m.status.upper()}**"]
    L += [f"- {d}" for d in m.detail] + ["- Recommendations and proposed orders are never counted as executed buys.", ""]

    # Orders ---------------------------------------------------------------
    L += ["## 4. Target portfolio and proposed orders", ""]
    if res["proposal"] is None:
        L += ["**Blocked.** Account-ready order quantities need official inputs that are missing. "
              "No budget has been assumed.", "", "| Field | Why | How to fix |", "|---|---|---|"]
        L += [f"| `{b.field}` | {b.reason} | {b.fix} |" for b in res["blockers"]]
        L += ["", "After filling these in, run: `python -m src.cli validate` then `python -m src.cli recommend`.", ""]
    else:
        p = res["proposal"]
        s = p.summary
        L += [f"Equity estimate {_money(s['equity_estimate'])}; cash {_money(s['cash_before'])} → "
              f"~{_money(s['cash_after_estimate'])} after buys (buffer {_money(s['cash_buffer'])}). "
              f"Proposed buys: {s['proposed_buys']}. Stocks after: {s['stocks_after']} (min {s['min_stocks']}).", "",
              "| Action | Ticker | Shares | Est. price | Est. value | Reason |", "|---|---|---:|---:|---:|---|"]
        for _, a in p.actions.iterrows():
            L.append(f"| {a['action']} | {a['ticker']} | {a['shares']:g} | {_money(a['est_price'])} | "
                     f"{_money(a['est_value'])} | {a['reason']} |")
        L += ["", "These are proposals. Execute manually in the class game, then record actual fills in "
              "private/account/trades.csv.", ""]

    # Factor exposure ------------------------------------------------------
    f = res["factor"]
    L += ["## 5. Ex-ante Fama–French 3-factor exposure of current holdings", ""]
    if f.get("status") == "ok":
        co, t = f["coefficients"], f["t_newey_west"]
        L += [f"{f['interpretation']}", "",
              f"- Sample {f['sample_start']} → {f['sample_end']} ({f['observations']} obs); factor data latest "
              f"{f['factor_latest_date']} ({f['factor_data']['source']})",
              f"- beta_mkt {co['beta_mkt']:.2f} (t {t['beta_mkt']:.1f}), beta_SMB {co['beta_smb']:.2f} "
              f"(t {t['beta_smb']:.1f}), beta_HML {co['beta_hml']:.2f} (t {t['beta_hml']:.1f}), R² {f['r_squared']:.2f}",
              f"- Historical intercept of this hypothetical basket: {f['alpha_annual_arithmetic']:+.1%}/yr arithmetic "
              f"(t {t['alpha']:.1f}, Newey-West). Not your alpha; past intercepts do not persist reliably.", ""]
    else:
        L += [f"Not available: {f.get('message', f.get('status'))}", ""]

    # Issues ---------------------------------------------------------------
    L += ["## 6. Account validation issues", ""]
    issues = [i for i in res["issues"] if i.severity in ("error", "warning")]
    L += [f"- **{i.severity.upper()}** `{i.code}`: {i.message}" for i in issues] or ["- none"]
    L += [f"- note: {n}" for n in recon.notes]
    return "\n".join(L) + "\n"
