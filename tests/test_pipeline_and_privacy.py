import json
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from conftest import base_metadata
from src.market_data.provider import FixtureProvider
from src.pipeline import run_recommendations

NOW = datetime(2030, 1, 15, 21, 31, tzinfo=timezone.utc)


def test_fixture_pipeline_end_to_end_blocks_orders_without_cash(make_account, tmp_path):
    acct = make_account(metadata=base_metadata(cash=None, reported_account_value=None))
    res = run_recommendations(FixtureProvider(end=date(2030, 1, 15)), acct, NOW, with_factors=False,
                              runs_dir=tmp_path / "runs")
    run_dir = res["run_dir"]
    assert run_dir.name.endswith("_FIXTURE")
    assert res["proposal"] is None
    assert {"cash", "market_data"} <= {b.field for b in res["blockers"]}
    for name in ("run_metadata.json", "strategy_config.json", "data_quality.json", "features.csv",
                 "ranked_scores.csv", "top5_candidates.csv", "holdings_review.csv", "monthly_buy_status.json",
                 "orders_blocked.json", "report.md", "strategy_change_summary.md"):
        assert (run_dir / name).exists(), name
    report = (run_dir / "report.md").read_text()
    assert report.startswith("# FIXTURE DATA — NOT A RECOMMENDATION")
    meta = json.loads((run_dir / "run_metadata.json").read_text())
    assert meta["is_fixture"] and meta["strategy_version"] and meta["data_cutoff_session"] == "2030-01-15"


def test_strategy_change_summary_compares_previous_run(make_account, tmp_path):
    acct = make_account()
    runs = tmp_path / "runs"
    run_recommendations(FixtureProvider(end=date(2030, 1, 15)), acct, NOW, False, runs)
    later = datetime(2030, 1, 15, 22, 0, tzinfo=timezone.utc)
    res = run_recommendations(FixtureProvider(end=date(2030, 1, 15)), acct, later, False, runs)
    assert res["strategy_changes"]["previous_run"] is not None
    assert res["strategy_changes"]["changes"] == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_private_paths_are_git_ignored():
    root = Path(__file__).resolve().parents[1]
    paths = ["private/account/holdings.csv", "private/account/account_metadata.json",
             "private/account/trades.csv", "private/runs/any/report.md", "data/cache/x.pkl"]
    out = subprocess.run(["git", "check-ignore", *paths], cwd=root, capture_output=True, text=True)
    assert sorted(out.stdout.split()) == sorted(paths)
