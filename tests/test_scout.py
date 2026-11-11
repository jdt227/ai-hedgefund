from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from config import settings
from src.features import add_final_score
from src.overlays.scout import VALUE_COMPONENTS, ScoutOverlay, downside_capture, grade, team_fit

NOW = datetime(2030, 1, 15, 21, 0, tzinfo=timezone.utc)


def prices(n=300, seed=3):
    idx = pd.bdate_range(end="2030-01-15", periods=n)
    rng = np.random.default_rng(seed)
    spy = rng.normal(0.0004, 0.01, n)
    data = {"SPY": spy,
            "DEFENSIVE": 0.3 * spy + rng.normal(0.0003, 0.004, n),       # falls less on down days
            "AGGRESSIVE": 2.0 * spy + rng.normal(0.0, 0.004, n),          # falls more
            "TWIN": spy + rng.normal(0.0, 0.001, n)}
    for i in range(6):
        data[f"T{i}"] = rng.normal(0.0004, 0.015, n)
    returns = pd.DataFrame(data, index=idx)
    return 100 * np.exp(returns.cumsum())


def test_downside_capture_orders_defensive_below_aggressive():
    cap = downside_capture(prices())
    assert cap["DEFENSIVE"] < cap["TWIN"] < cap["AGGRESSIVE"]
    assert cap["TWIN"] == pytest.approx(1.0, abs=0.1)


def test_team_fit_excludes_self():
    frame = prices()
    fit = team_fit(frame.drop(columns="SPY"), ["TWIN", "AGGRESSIVE"])
    assert fit["TWIN"] == pytest.approx(frame.drop(columns="SPY").pct_change().iloc[-63:].corr().at["TWIN", "AGGRESSIVE"])


@pytest.mark.parametrize("pct,letter", [(0.95, "A"), (0.8, "A"), (0.65, "B"), (0.4, "C"), (0.2, "D"), (0.1, "F")])
def test_grade_cuts(pct, letter):
    assert grade(pct) == letter


def test_overlay_rows_are_bounded_dated_and_explained():
    frame = prices()
    out = ScoutOverlay(frame, ["T0", "T1"], 0.05).compute([c for c in frame if c != "SPY"], NOW)
    assert set(out.columns) >= {"ticker", "value", "available_at", "evidence"}
    assert out["value"].between(-1, 1).all() and (out["available_at"] == "2030-01-15").all()
    rows = out.set_index("ticker")
    # Uncorrelated stocks barely move on SPY down days, so the 2x-beta stock grades worst.
    assert rows.at["AGGRESSIVE", "grade_clutch"] == "F"
    assert rows.at["DEFENSIVE", "capture"] < rows.at["AGGRESSIVE", "capture"]
    assert "display only" in rows.at["DEFENSIVE", "evidence"]


def test_value_excludes_momentum(monkeypatch):
    """The value is built only from clutch, durability, and team fit."""
    from src.overlays import scout
    assert "scoring" not in VALUE_COMPONENTS and "injury" not in VALUE_COMPONENTS
    frame = prices()
    base = ScoutOverlay(frame, ["T0"], 0.05).compute(list(frame.columns.drop("SPY")), NOW).set_index("ticker")
    original = scout.percentile

    def flip_scoring(values, higher_is_better):
        if values.name == "six_month_return":
            return 1 - original(values, higher_is_better)
        return original(values, higher_is_better)

    monkeypatch.setattr(scout, "percentile", flip_scoring)
    flipped = ScoutOverlay(frame, ["T0"], 0.05).compute(list(frame.columns.drop("SPY")), NOW).set_index("ticker")
    assert (flipped["grade_scoring"] != base["grade_scoring"]).any()
    pd.testing.assert_series_equal(flipped["value"], base["value"])


def test_post_cutoff_prices_are_ignored():
    frame = prices()
    later = frame.copy()
    later.loc[pd.Timestamp("2030-01-16")] = later.iloc[-1] * 3
    a = ScoutOverlay(frame, ["T0"], 0.05).compute(list(frame.columns.drop("SPY")), NOW)
    b = ScoutOverlay(later, ["T0"], 0.05).compute(list(frame.columns.drop("SPY")), NOW)
    pd.testing.assert_frame_equal(a, b)


def test_missing_benchmark_returns_empty():
    frame = prices().drop(columns="SPY")
    assert ScoutOverlay(frame, [], 0.05).compute(list(frame.columns), NOW).empty


def test_injury_flags_lower_the_injury_grade_only():
    frame = prices()
    flags = {"T0": [{"flag": "earnings_imminent", "reason": "x"}, {"flag": "negative_catalyst", "reason": "y"}]}
    out = ScoutOverlay(frame, ["T1"], 0.05, flags).compute(list(frame.columns.drop("SPY")), NOW).set_index("ticker")
    clean = ScoutOverlay(frame, ["T1"], 0.05).compute(list(frame.columns.drop("SPY")), NOW).set_index("ticker")
    assert out.at["T0", "injuries"] == 2 and out.at["T0", "grade_injury"] in ("D", "F")
    assert out.at["T0", "value"] == clean.at["T0", "value"]


def test_contribution_is_bounded_by_weight():
    scored = pd.DataFrame({"composite_score": [1.0, 0.5]}, index=["A", "B"])
    out = add_final_score(scored, {"A": "s", "B": "s"},
                          overlays={"scout": (pd.Series({"A": 5.0, "B": -0.5}), settings.W_SCOUT)})
    assert out.at["A", "scout_contribution"] == pytest.approx(settings.W_SCOUT)
    assert out.at["B", "final_score"] == pytest.approx(0.5 - 0.5 * settings.W_SCOUT)


def test_pipeline_shows_scout_in_prompt_and_toggle_removes_it(monkeypatch, tmp_path):
    from src import run_pipeline
    from src.build_weight_prompt import firm_section
    from src.untrusted import UntrustedBlocks
    from test_weekly_pipeline import NOW as RUN_NOW, RUN, universe_prices
    evidence = run_pipeline.build_run(universe_prices(), RUN, RUN_NOW)
    assert "scout_contribution" in evidence.final_scores and len(evidence.scout)
    assert "Scout report: clutch" in firm_section(evidence, UntrustedBlocks("T"))
    monkeypatch.setattr(settings, "USE_SCOUT_OVERLAY", False)
    off = run_pipeline.build_run(universe_prices(), RUN, RUN_NOW)
    assert "scout_contribution" not in off.final_scores and off.source_status["scout"] == "disabled"
