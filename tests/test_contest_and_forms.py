import json
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from config import settings
from src.evaluation.account_values import (can_reconstruct, cross_check, interval_returns, load_account_values,
                                           log_value, reconstruct_daily_values)
from src.evaluation.contest import (DAILY, INTERVAL, compound_factors, confidence_label, ex_ante_loadings,
                                    interval_factors, proxy_daily_factors, realized_alpha, rf_from_dtb3,
                                    weekly_aligned)

TODAY = date(2030, 3, 1)


# --- log-value -------------------------------------------------------------------
def test_log_value_appends_with_header(tmp_path):
    path = tmp_path / "account_values.csv"
    assert "Logged 2030-01-10" in log_value(path, "2030-01-10", "$98,765.43", "game, overview", today=TODAY)
    log_value(path, "2030-01-17", "102000", today=TODAY)
    values, problems = load_account_values(path)
    assert not problems and values["account_value"].tolist() == [98765.43, 102000.0]
    assert values.at[0, "source_note"] == "game; overview"


@pytest.mark.parametrize("day,value,message", [
    ("2030-13-01", "100", "YYYY-MM-DD"),
    ("2031-01-01", "100", "future"),
    ("2030-01-10", "-5", "positive"),
    ("2030-01-10", "nan", "positive"),
    ("2030-01-10", "lots", "number"),
])
def test_log_value_rejects_bad_input(tmp_path, day, value, message):
    with pytest.raises(ValueError, match=message):
        log_value(tmp_path / "v.csv", day, value, today=TODAY)


def test_log_value_rejects_duplicate_dates(tmp_path):
    path = tmp_path / "v.csv"
    log_value(path, "2030-01-10", "100", today=TODAY)
    with pytest.raises(ValueError, match="already logged"):
        log_value(path, "2030-01-10", "101", today=TODAY)


def test_cli_log_value(tmp_path, capsys):
    from src.cli import main
    assert main(["--account-dir", str(tmp_path), "log-value", "2026-01-02", "1000"]) == 0
    assert main(["--account-dir", str(tmp_path), "log-value", "2026-01-02", "1000"]) == 1
    assert "already logged" in capsys.readouterr().out


def test_loader_reports_bad_rows(tmp_path):
    path = tmp_path / "v.csv"
    path.write_text("date,account_value,source_note\n2030-01-10,100,a\nbad,5,b\n2030-01-10,90,c\n2030-01-03,-1,d\n")
    values, problems = load_account_values(path)
    assert len(values) == 1 and len(problems) == 3


# --- reconstruction ----------------------------------------------------------------
def ledger(rows):
    frame = pd.DataFrame(rows, columns=["executed_at_utc", "ticker", "side", "shares", "fill_price", "fees"])
    frame["executed_at_utc"] = pd.to_datetime(frame["executed_at_utc"], utc=True)
    frame["valid"] = True
    return frame


def test_daily_reconstruction_applies_fills_on_their_local_date():
    days = pd.bdate_range("2030-01-07", periods=4)
    closes = pd.DataFrame({"A": [10.0, 11.0, 12.0, 12.0], "B": [50.0, 50.0, 55.0, np.nan]}, index=days)
    # 2030-01-09T02:00Z is still Jan 8 in New York.
    fills = ledger([("2030-01-09T02:00:00Z", "B", "BUY", 10, 50.0, 1.0)])
    out = reconstruct_daily_values({"A": 100}, 1000.0, date(2030, 1, 7), fills, closes, "America/New_York")
    values = out.set_index("date")["account_value"]
    assert values.iloc[0] == pytest.approx(2000.0)
    assert values.iloc[1] == pytest.approx(100 * 11 + 1000 - 501 + 10 * 50)
    assert values.iloc[3] == pytest.approx(100 * 12 + 499 + 10 * 55)  # missing close carried forward


def test_unpriced_holding_is_reported():
    days = pd.bdate_range("2030-01-07", periods=2)
    out = reconstruct_daily_values({"A": 1, "ZZZ": 5}, 0.0, date(2030, 1, 7), ledger([]),
                                   pd.DataFrame({"A": [1.0, 2.0]}, index=days), "America/New_York")
    assert set(out["unpriced"]) == {"ZZZ"}


def test_cross_check_flags_gaps_over_one_percent():
    rec = pd.DataFrame({"date": pd.to_datetime(["2030-01-07", "2030-01-08"]), "account_value": [100.0, 100.0]})
    logged = pd.DataFrame({"date": pd.to_datetime(["2030-01-07", "2030-01-08"]), "account_value": [100.5, 102.0],
                           "source_note": ["", ""]})
    issues = cross_check(rec, logged)
    assert len(issues) == 1 and "2030-01-08" in issues[0]


def test_reconstruction_needs_complete_dated_state(make_account):
    from conftest import base_metadata
    from src.account.inputs import load_account
    assert can_reconstruct(load_account(make_account()))[0]
    ok, reason = can_reconstruct(load_account(make_account(metadata=base_metadata(ledger_complete_after_snapshot=None,
                                                                                  snapshot_captured_at=None))))
    assert not ok and "ledger_complete_after_snapshot" in reason and "snapshot_captured_at" in reason


def test_interval_returns():
    values = pd.DataFrame({"date": pd.to_datetime(["2030-01-10", "2030-01-03"]), "account_value": [110.0, 100.0]})
    out = interval_returns(values)
    assert len(out) == 1 and out.at[0, "return"] == pytest.approx(0.10)


# --- factors -------------------------------------------------------------------------
def daily_factors(n=400, seed=2):
    idx = pd.bdate_range("2028-01-03", periods=n)
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"Mkt-RF": rng.normal(0.0004, 0.01, n), "SMB": rng.normal(0, 0.005, n),
                         "HML": rng.normal(0, 0.005, n), "RF": np.full(n, 0.0001)}, index=idx)


def test_compound_factors_over_intervals():
    daily = daily_factors(10)
    intervals = pd.DataFrame({"start": [daily.index[0]], "end": [daily.index[4]]})
    out = compound_factors(daily, intervals)
    window = daily.iloc[1:5]
    rf = (1 + window["RF"]).prod() - 1
    assert out.iloc[0]["RF"] == pytest.approx(rf)
    assert out.iloc[0]["Mkt-RF"] == pytest.approx((1 + window["Mkt-RF"] + window["RF"]).prod() - 1 - rf)


def test_intervals_past_factor_data_are_dropped():
    daily = daily_factors(10)
    intervals = pd.DataFrame({"start": [daily.index[-1]], "end": [daily.index[-1] + pd.Timedelta(days=7)]})
    assert compound_factors(daily, intervals).empty


def test_weekly_file_used_only_when_intervals_match_its_weeks():
    weeks = pd.date_range("2030-01-04", periods=5, freq="W-FRI")
    weekly = pd.DataFrame(0.01, index=weeks, columns=["Mkt-RF", "SMB", "HML", "RF"])
    aligned = pd.DataFrame({"start": weeks[:-1], "end": weeks[1:]})
    assert weekly_aligned(aligned, weekly)
    assert interval_factors(aligned, None, weekly)[1] == "Ken French weekly file"
    offset = pd.DataFrame({"start": weeks[:-1] - pd.Timedelta(days=1), "end": weeks[1:] - pd.Timedelta(days=1)})
    assert not weekly_aligned(offset, weekly)
    assert "compounded" in interval_factors(offset, daily_factors(), weekly)[1]


def test_proxy_factors_and_risk_free():
    idx = pd.bdate_range("2030-01-01", periods=3)
    prices = pd.DataFrame({t: [100.0, 101.0, 102.0] for t in ("SPY", "IWM", "IWB", "IWD", "IWF")}, index=idx)
    prices["IWM"] = [100.0, 102.0, 102.0]
    frame = proxy_daily_factors(prices, None)
    assert frame["SMB"].iloc[0] == pytest.approx(0.02 - 0.01) and (frame["RF"] == 0).all()
    assert rf_from_dtb3(pd.Series([5.0])).iloc[0] == pytest.approx(1.05 ** (1 / 252) - 1)


# --- regressions -------------------------------------------------------------------------
def test_daily_series_recovers_known_alpha_and_betas():
    f = daily_factors()
    noise = np.random.default_rng(4).normal(0, 0.002, len(f))
    total = f["RF"] + 0.0004 + 1.1 * f["Mkt-RF"] + 0.3 * f["SMB"] - 0.5 * f["HML"] + noise
    values = pd.DataFrame({"date": f.index, "account_value": 100_000 * (1 + total).cumprod()})
    out = realized_alpha(values, DAILY, f, None, f)
    c = out["official"]["coefficients"]
    assert out["official"]["status"] == "ok" and out["official"]["confidence"] == "moderate confidence"
    assert c["alpha"] == pytest.approx(0.0004, abs=2.5e-4)
    assert c["beta_mkt"] == pytest.approx(1.1, abs=0.05) and c["beta_hml"] == pytest.approx(-0.5, abs=0.1)


def test_logged_weekly_series_recovers_alpha_with_compounded_factors():
    f = daily_factors(n=800, seed=7)
    total = f["RF"] + 0.0003 + 1.0 * f["Mkt-RF"]
    level = 100_000 * (1 + total).cumprod()
    logs = level.iloc[::5]
    intervals = interval_returns(pd.DataFrame({"date": logs.index, "account_value": logs.to_numpy()}))
    out = realized_alpha(intervals, INTERVAL, f, None, None)
    assert out["official"]["status"] == "ok"
    assert "compounded" in out["official"]["factor_construction"]
    assert out["official"]["coefficients"]["beta_mkt"] == pytest.approx(1.0, abs=0.05)
    assert out["official"]["alpha_per_period"] == pytest.approx((1.0003 ** 5) - 1, abs=6e-4)
    assert out["official"]["periods_per_year"] == 52


@pytest.mark.parametrize("obs,kind,label", [
    (5, INTERVAL, "insufficient history"), (8, INTERVAL, "LOW CONFIDENCE (few observations)"),
    (30, INTERVAL, "moderate confidence"), (59, DAILY, "insufficient history"),
    (100, DAILY, "LOW CONFIDENCE (few observations)"),
])
def test_confidence_labels(obs, kind, label):
    assert confidence_label(obs, kind) == label


def test_few_logged_values_report_insufficient_not_a_number():
    f = daily_factors()
    values = pd.DataFrame({"date": f.index[[0, 5, 10]], "account_value": [100.0, 101.0, 103.0]})
    out = realized_alpha(interval_returns(values), INTERVAL, f, None, None)
    assert out["official"]["status"] == "insufficient_history" and "coefficients" not in out["official"]
    single = realized_alpha(interval_returns(values.iloc[:1]), INTERVAL, f, None, None)
    assert "two account values" in single["official"]["message"]


def test_ex_ante_loadings_of_a_market_like_basket():
    f = daily_factors()
    prices = pd.DataFrame({"A": 100 * (1 + f["Mkt-RF"] + f["RF"]).cumprod(),
                           "B": 100 * (1 + f["Mkt-RF"] + f["RF"]).cumprod()})
    out = ex_ante_loadings(prices, {"A": 12, "B": 12, "CASHLIKE": 0}, f, None)
    assert out["official"]["coefficients"]["beta_mkt"] == pytest.approx(1.0, abs=0.01)
    assert out["weights_used"] == {"A": 0.5, "B": 0.5} and "earn no alpha" in out["note"]
    assert ex_ante_loadings(prices, {}, f, None)["status"] == "unavailable"


def test_account_series_falls_back_to_logged_values(make_account):
    from conftest import base_metadata
    from src.evaluation.contest_run import account_series
    acct = make_account(metadata=base_metadata(ledger_complete_after_snapshot=None))
    (acct / "account_values.csv").write_text("date,account_value,source_note\n2030-01-03,100,a\n2030-01-10,101,b\n")
    series, kind, notes = account_series(acct, pd.DataFrame())
    assert kind == INTERVAL and len(series) == 2 and "Using logged values" in notes[-1]


def test_prompt_and_report_show_loadings_and_contest(tmp_path):
    from src.build_weight_prompt import loadings_lines
    from src.reporting.weekly_sections import contest_lines

    class Ev:
        contest = {"series_kind": INTERVAL, "series_points": 1, "notes": ["n"], "sources": {"s": "ok"},
                   "realized": {"official": {"status": "insufficient_history", "message": "few"},
                                "proxy": {"status": "unavailable"}},
                   "ex_ante_loadings": {"official": {"status": "unavailable"}, "proxy": {"status": "unavailable"}}}
    assert "earn no credit" in "\n".join(loadings_lines(Ev))
    text = "\n".join(contest_lines(Ev))
    assert "Official (Ken French):** insufficient_history" in text and "NOT the graded number" in text


# --- fingerprint and forms ----------------------------------------------------------------
@pytest.fixture
def forms(tmp_path, monkeypatch):
    from src import strategy_fingerprint
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "risk_manager.md").write_text("v1")
    monkeypatch.setattr(strategy_fingerprint, "AGENTS_DIR", agents)
    changelog = tmp_path / "changelog.md"
    changelog.write_text("# Log\n\n## First change\n\nWhy one.\n")
    return tmp_path / "forms", changelog, agents


def fp(agents):
    from src.strategy_fingerprint import fingerprint
    return fingerprint(agents)


def save(forms_dir, result):
    forms_dir.mkdir(exist_ok=True)
    (forms_dir / result[0]).write_text(result[1])


def test_first_form_includes_changelog_and_fingerprint(forms):
    from src.strategy_form import MARKER, build_form, changed_since_last_form, last_form
    forms_dir, changelog, agents = forms
    assert changed_since_last_form(forms_dir, fp(agents)) == ["no strategy form has been logged yet"]
    name, text = build_form(date(2030, 1, 15), forms_dir, changelog, fp(agents))
    assert name.startswith("2030-01-15_weekly-") and "### First change" in text and "Why one." in text
    assert MARKER in text and "First form" in text
    save(forms_dir, (name, text))
    assert last_form(forms_dir)[1]["fingerprint"]["hash"] == fp(agents)["hash"]
    assert changed_since_last_form(forms_dir, fp(agents)) == []
    assert build_form(date(2030, 1, 16), forms_dir, changelog, fp(agents)) is None


def test_setting_and_agent_edits_are_detected(forms, monkeypatch):
    from src.strategy_form import build_form, changed_since_last_form
    forms_dir, changelog, agents = forms
    save(forms_dir, build_form(date(2030, 1, 15), forms_dir, changelog, fp(agents)))
    monkeypatch.setattr(settings, "W_SENTIMENT", 0.2)
    (agents / "risk_manager.md").write_text("v2")
    changes = changed_since_last_form(forms_dir, fp(agents))
    assert "setting W_SENTIMENT: 0.1 -> 0.2" in changes and "agent prompt edited: risk_manager.md" in changes
    changelog.write_text(changelog.read_text() + "\n## Second change\n\nWhy two.\n")
    name, text = build_form(date(2030, 1, 20), forms_dir, changelog, fp(agents))
    assert "### Second change" in text and "### First change" not in text
    assert "setting W_SENTIMENT" in text


def test_universe_change_changes_the_fingerprint(monkeypatch, forms):
    from src import strategy_fingerprint
    from src.strategy_fingerprint import diff
    _, _, agents = forms
    before = fp(agents)
    monkeypatch.setattr(strategy_fingerprint, "universe_hash", lambda: "other")
    assert "universe snapshot changed (tickers or sectors)" in diff(before, fp(agents))


def test_fingerprint_is_saved_with_run_evidence(tmp_path):
    from src.build_weight_prompt import write_evidence
    from test_weekly_pipeline import NOW, RUN, universe_prices
    from src import run_pipeline
    evidence = run_pipeline.build_run(universe_prices(), RUN, NOW)
    write_evidence(evidence, tmp_path)
    saved = json.loads((tmp_path / "evidence" / "strategy_fingerprint.json").read_text())
    assert {"settings", "universe", "agents", "hash"} <= set(saved)


def test_ex_ante_output_never_shows_alpha():
    from src.build_weight_prompt import loadings_lines
    from src.evaluation.contest import summarize
    ok = {"status": "ok", "coefficients": {"alpha": 0.002, "beta_mkt": 1.0, "beta_smb": 0.1, "beta_hml": 0.2},
          "t_newey_west": {"alpha": 2.5}, "alpha_per_period": 0.002, "alpha_annual_arithmetic": 0.5,
          "r_squared": 0.4, "observations": 250, "confidence": "moderate confidence", "factor_latest_date": "x"}

    class Ev:
        contest = {"ex_ante_loadings": {"official": ok, "proxy": ok}}
    text = "\n".join(loadings_lines(Ev))
    assert "alpha +" not in text and "market 1.00" in text and "hindsight" in text
    assert "alpha +0.2000%" in summarize(ok)
