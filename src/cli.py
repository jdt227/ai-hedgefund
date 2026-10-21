"""Command-line entry points.

    python -m src.cli init-account   # copy blank templates into private/account/
    python -m src.cli validate       # validate + reconcile account inputs (no network)
    python -m src.cli recommend      # full run with live market data
    python -m src.cli recommend --fixture   # offline demo; output labeled FIXTURE
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[1]))

from config import strategy as cfg  # noqa: E402
from src.account.inputs import DEFAULT_ACCOUNT_DIR, load_account  # noqa: E402
from src.account.reconcile import count_monthly_buys, reconcile  # noqa: E402

TEMPLATES = Path("templates/account")


def cmd_init(args) -> int:
    dest = Path(args.account_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for name, target in (("account_metadata.template.json", "account_metadata.json"),
                         ("holdings.template.csv", "holdings.csv"),
                         ("trades.template.csv", "trades.csv")):
        out = dest / target
        if out.exists():
            print(f"exists, not overwritten: {out}")
        else:
            shutil.copy(TEMPLATES / name, out)
            print(f"created {out}")
    return 0


def cmd_validate(args) -> int:
    account = load_account(Path(args.account_dir))
    recon = reconcile(account)
    tz = cfg.CLASS_RULES["timezone"]
    month = args.month or datetime.now(ZoneInfo(tz)).strftime("%Y-%m")
    monthly = count_monthly_buys(account, month, tz, cfg.CLASS_RULES["min_buy_orders_per_month"])
    meta = account.metadata
    print(f"Account dir: {account.account_dir}")
    print(f"Game: {meta.game_name or 'UNKNOWN'} | cash: {meta.cash if meta.cash is not None else 'UNKNOWN'} | "
          f"snapshot: {meta.snapshot_captured_at or 'UNKNOWN'} {meta.snapshot_timezone or '(timezone UNKNOWN)'}")
    print(f"Holdings rows: {0 if account.holdings is None else len(account.holdings)} | "
          f"trade fills: {len(account.trades)}")
    print("\nIssues:")
    for issue in account.issues + recon.issues:
        print(f"  [{issue.severity.upper():7}] {issue.code}: {issue.message}")
    print(f"\nPositions ({recon.positions_status}):")
    print(recon.positions.to_string(index=False) if len(recon.positions) else "  (none — all cash)")
    print(f"Current cash: {recon.current_cash if recon.current_cash is not None else 'UNKNOWN'} "
          f"({recon.current_cash_status})")
    for note in recon.notes:
        print(f"  note: {note}")
    count = "UNKNOWN" if monthly.count_lower_bound is None else monthly.count_lower_bound
    print(f"\nMonthly BUY orders {monthly.month}: {count} (coverage {monthly.coverage}) -> {monthly.status.upper()}")
    for d in monthly.detail:
        print(f"  {d}")
    errors = len([i for i in account.issues + recon.issues if i.severity == "error"])
    print(f"\n{errors} error(s).")
    return 1 if errors else 0


def cmd_recommend(args) -> int:
    from src.pipeline import run_recommendations

    if args.fixture:
        from src.market_data.provider import FixtureProvider
        provider = FixtureProvider()
        print("*** FIXTURE MODE: synthetic prices. Output is NOT a real recommendation. ***")
    else:
        from src.market_data.yfinance_provider import YFinanceProvider
        provider = YFinanceProvider(period=cfg.DATA["history_period"],
                                    close_buffer_minutes=cfg.DATA["close_buffer_minutes"],
                                    cache_max_age_hours=cfg.DATA["cache_max_age_hours"], refresh=args.refresh)
    res = run_recommendations(provider, Path(args.account_dir), datetime.now(timezone.utc),
                              with_factors=not args.no_factors)
    b = res["bundle"]
    print(f"Data: {b.provider} | cutoff session {b.last_completed_session} | stale={res['market_stale']}")
    if res["shortfall"]:
        print(res["shortfall"])
    cols = ["rank", "ticker", "sector", "reference_close", "composite_score", "status"]
    print("\nTop candidates:\n" + (res["top"][cols].to_string(index=False) if len(res["top"]) else "(none)"))
    if res["blockers"]:
        print("\nProposed orders BLOCKED — missing:")
        for blk in res["blockers"]:
            print(f"  - {blk.field}: {blk.fix}")
    print(f"\nArtifacts: {res['run_dir']}")
    print(f"Report:    {res['run_dir'] / 'report.md'}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    parser.add_argument("--account-dir", default=str(DEFAULT_ACCOUNT_DIR))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-account").set_defaults(func=cmd_init)
    v = sub.add_parser("validate")
    v.add_argument("--month", help="YYYY-MM (default: current month in class timezone)")
    v.set_defaults(func=cmd_validate)
    r = sub.add_parser("recommend")
    r.add_argument("--fixture", action="store_true", help="offline synthetic data (labeled FIXTURE)")
    r.add_argument("--refresh", action="store_true", help="ignore the market-data cache")
    r.add_argument("--no-factors", action="store_true", help="skip the Fama-French download")
    r.set_defaults(func=cmd_recommend)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
