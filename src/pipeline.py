"""End-to-end recommendation run.

account inputs -> reconciliation -> market data -> signals -> ranking
-> top five + holdings review -> (gated) proposed orders -> FF3 exposure
-> private dated artifacts under private/runs/<run_id>/.
"""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from config import strategy as cfg
from src.account.inputs import DEFAULT_ACCOUNT_DIR, AccountInputs, load_account
from src.account.reconcile import count_monthly_buys, reconcile
from src.market_data.provider import MarketDataProvider
from src.strategy.orders import order_readiness, propose_orders
from src.strategy.review import holdings_review, sector_weights, stock_counts, top_candidates
from src.strategy.scoring import build_ranking
from src.strategy.signals import compute_signals

PRIVATE_RUNS_DIR = Path("private/runs")


def git_state() -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return None
    porcelain = run("status", "--porcelain") or ""
    return {"commit": run("rev-parse", "HEAD"), "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(porcelain), "changed_paths": [line[3:] for line in porcelain.splitlines()]}


def run_recommendations(provider: MarketDataProvider, account_dir: Path = DEFAULT_ACCOUNT_DIR,
                        now_utc: datetime | None = None, with_factors: bool = True,
                        runs_dir: Path = PRIVATE_RUNS_DIR, write: bool = True) -> dict:
    now_utc = now_utc or datetime.now(timezone.utc)
    rules = cfg.CLASS_RULES

    # 1. Account -----------------------------------------------------------
    account: AccountInputs = load_account(account_dir)
    meta = account.metadata
    recon = reconcile(account)
    month = now_utc.astimezone(ZoneInfo(rules["timezone"])).strftime("%Y-%m")
    monthly = count_monthly_buys(account, month, rules["timezone"], rules["min_buy_orders_per_month"],
                                 now=now_utc)
    all_issues = account.issues + recon.issues

    # 2. Market data -------------------------------------------------------
    held = set(recon.positions["ticker"])
    tickers = sorted(set(cfg.UNIVERSE) | held | {cfg.BENCHMARK})
    bundle = provider.fetch(tickers, now_utc)
    quality = bundle.ticker_quality(tickers)
    staleness = bundle.staleness_sessions()
    market_stale = staleness is None or staleness > cfg.DATA["max_stale_sessions"]
    stale = set(quality.index[quality["stale_vs_market"]])

    # 3. Signals + ranking (cutoff = last completed session) ---------------
    cutoff = pd.Timestamp(bundle.last_completed_session) if bundle.last_completed_session else None
    signals = compute_signals(bundle.adj_close, bundle.close, bundle.volume, cutoff, cfg.BENCHMARK)
    ranking = build_ranking(signals, bundle.info, cfg.UNIVERSE, cfg.ELIGIBILITY, cfg.SCORING,
                            cfg.SIGNALS, cfg.MONITOR_ONLY, stale)

    # 4. Holdings review + top five ----------------------------------------
    review = holdings_review(recon.positions, account.holdings, ranking, bundle.info,
                             cfg.KNOWN_ETFS, cfg.SCORING)
    cash_for_weights = recon.current_cash if recon.current_cash_status == "verified" else None
    sector_now, sector_basis = sector_weights(review, cash_for_weights) if len(review) else (pd.Series(dtype=float), "n/a")
    etf_counts = meta.etf_counts_toward_stock_minimum
    counts = stock_counts(review, etf_counts, rules["min_individual_stocks"])
    sector_values = review.groupby("sector")["marked_value"].sum() if len(review) else pd.Series(dtype=float)
    top, shortfall = top_candidates(ranking, held, sector_now, sector_basis,
                                    cfg.SIZING["max_sector_weight"], cfg.SIGNALS, cfg.SCORING["top_n"],
                                    sector_values, cash_known=cash_for_weights is not None)

    # 5. Orders (gated) ----------------------------------------------------
    blockers = order_readiness(meta, recon, bundle.is_fixture, market_stale, len(account.errors))
    if blockers:
        proposal = None
    else:
        fee = meta.commission_per_trade if meta.commission_per_trade is not None else cfg.SIZING["assumed_fee_per_trade"]
        proposal = propose_orders(review, ranking, recon.current_cash, cfg.SIZING, fee,
                                  bool(meta.fractional_shares_allowed), rules["min_individual_stocks"], etf_counts)

    # 6. Ex-ante factor exposure of current holdings (NOT performance) -----
    factor = {"status": "skipped"}
    if with_factors and len(review) and review["marked_value"].notna().any():
        try:
            from src.evaluation.fama_french import ff3_regression, fixed_weight_returns, load_french_daily
            factors, fmeta = load_french_daily()
            weights = review["marked_value"].dropna().to_dict()
            basket = fixed_weight_returns(bundle.adj_close, weights, sessions=252)
            factor = ff3_regression(basket, factors) | {"factor_data": fmeta, "weights_used": {
                k: v / sum(weights.values()) for k, v in weights.items()},
                "interpretation": "EX-ANTE exposure: today's holdings weights applied to the past "
                                  "~252 sessions of returns (cash excluded). This is NOT your realized "
                                  "performance or alpha."}
        except Exception as exc:
            factor = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    result = {
        "now_utc": now_utc, "account": account, "recon": recon, "monthly": monthly,
        "issues": all_issues, "bundle": bundle, "quality": quality, "market_stale": market_stale,
        "signals": signals, "ranking": ranking, "review": review, "sector_now": sector_now,
        "sector_basis": sector_basis, "counts": counts, "top": top, "shortfall": shortfall,
        "blockers": blockers, "proposal": proposal, "factor": factor,
    }
    if write:
        result["run_dir"] = write_artifacts(result, runs_dir)
    return result


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------
def _previous_snapshot(runs_dir: Path, current: Path) -> tuple[str | None, dict | None]:
    if not runs_dir.exists():
        return None, None
    prior = sorted(p for p in runs_dir.iterdir() if p.is_dir() and p != current
                   and (p / "strategy_config.json").exists())
    if not prior:
        return None, None
    return prior[-1].name, json.loads((prior[-1] / "strategy_config.json").read_text())


def _diff(old: dict, new: dict, prefix: str = "") -> list[str]:
    changes = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            changes += _diff(a, b, f"{prefix}{key}.")
        elif a != b:
            changes.append(f"{prefix}{key}: {json.dumps(a)} -> {json.dumps(b)}")
    return changes


def write_artifacts(res: dict, runs_dir: Path) -> Path:
    from src.reporting.run_report import render_report

    bundle = res["bundle"]
    run_id = res["now_utc"].strftime("%Y-%m-%d_%H%M%SZ") + ("_FIXTURE" if bundle.is_fixture else "")
    run_dir = Path(runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    snapshot = cfg.strategy_snapshot()
    (run_dir / "strategy_config.json").write_text(json.dumps(snapshot, indent=2))
    prev_id, prev = _previous_snapshot(Path(runs_dir), run_dir)
    changes = _diff(prev, snapshot) if prev else []
    res["strategy_changes"] = {"previous_run": prev_id, "changes": changes}

    metadata = {
        "run_id": run_id,
        "created_at_utc": res["now_utc"].isoformat(),
        "strategy_version": cfg.STRATEGY_VERSION,
        "config_hash": cfg.config_hash(snapshot),
        "data_cutoff_session": str(bundle.last_completed_session),
        "market_data": bundle.provenance(),
        "is_fixture": bundle.is_fixture,
        "git": git_state(),
        "python": platform.python_version(),
        "packages": {m: __import__(m).__version__ for m in ("pandas", "numpy")},
        "account_dir": str(res["account"].account_dir),
        "orders_ready": not res["blockers"],
    }
    (run_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2, default=str))

    dq = {
        "market": bundle.provenance(),
        "market_stale": res["market_stale"],
        "per_ticker": res["quality"].reset_index().to_dict("records"),
        "account_issues": [i.as_dict() for i in res["issues"]],
    }
    (run_dir / "data_quality.json").write_text(json.dumps(dq, indent=2, default=str))
    res["signals"].to_csv(run_dir / "features.csv")
    res["ranking"].to_csv(run_dir / "ranked_scores.csv")
    res["top"].to_csv(run_dir / "top5_candidates.csv", index=False)
    res["review"].to_csv(run_dir / "holdings_review.csv")
    res["recon"].positions.to_csv(run_dir / "reconciled_positions.csv", index=False)
    (run_dir / "monthly_buy_status.json").write_text(json.dumps(res["monthly"].as_dict(), indent=2, default=str))
    (run_dir / "factor_exposure.json").write_text(json.dumps(res["factor"], indent=2, default=str))
    if res["proposal"] is not None:
        res["proposal"].actions.to_csv(run_dir / "trade_blotter.csv", index=False)
        res["proposal"].target_portfolio.to_csv(run_dir / "target_portfolio.csv", index=False)
    else:
        (run_dir / "orders_blocked.json").write_text(
            json.dumps([asdict(b) for b in res["blockers"]], indent=2))
    (run_dir / "report.md").write_text(render_report(res, metadata), encoding="utf-8")
    (run_dir / "strategy_change_summary.md").write_text(_change_summary(res, metadata), encoding="utf-8")
    return run_dir


def _change_summary(res: dict, metadata: dict) -> str:
    ch = res["strategy_changes"]
    lines = [f"# Strategy change summary — {metadata['run_id']}", "",
             f"- Strategy version: `{metadata['strategy_version']}` (config hash `{metadata['config_hash']}`)",
             f"- Previous run compared: {ch['previous_run'] or 'none (first recorded run)'}", ""]
    if not ch["previous_run"]:
        lines += ["This is the first recorded run of the quant baseline. Text adaptable for class forms:", "",
                  "> The portfolio is guided by a transparent, rules-based quantitative ranking of a fixed "
                  "universe of liquid U.S. large-cap common stocks. Each stock is scored on 12-month and "
                  "6-month price momentum (skipping the most recent month), its trend versus the 200-day "
                  "average, and penalties for 63-day volatility and one-year maximum drawdown. Scores are "
                  "cross-sectional z-scores combined with fixed weights (see docs/STRATEGY.md). Trades are "
                  "proposed only within position, sector and cash-buffer limits, executed manually, and "
                  "recorded with fill prices for audit."]
    elif ch["changes"]:
        lines += ["Configuration changes since the previous run:", ""] + [f"- `{c}`" for c in ch["changes"]]
        lines += ["", "Record the reason for each change in docs/strategy_changelog.md."]
    else:
        lines += ["No configuration changes since the previous run."]
    return "\n".join(lines) + "\n"
