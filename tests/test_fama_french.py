import numpy as np
import pandas as pd
import pytest

from src.evaluation.fama_french import ff3_regression, parse_french_csv


def synthetic_factors(n=600, seed=3):
    idx = pd.bdate_range("2028-01-03", periods=n)
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "Mkt-RF": rng.normal(0.0004, 0.01, n),
        "SMB": rng.normal(0.0, 0.005, n),
        "HML": rng.normal(0.0, 0.005, n),
        "RF": np.full(n, 0.0002),
    }, index=idx)


def test_recovers_known_alpha_and_betas():
    f = synthetic_factors()
    noise = np.random.default_rng(9).normal(0, 0.002, len(f))
    alpha = 0.0003
    total = f["RF"] + alpha + 1.2 * f["Mkt-RF"] - 0.4 * f["SMB"] + 0.6 * f["HML"] + noise
    res = ff3_regression(total, f)
    assert res["status"] == "ok"
    c = res["coefficients"]
    assert c["alpha"] == pytest.approx(alpha, abs=2e-4)
    assert c["beta_mkt"] == pytest.approx(1.2, abs=0.05)
    assert c["beta_smb"] == pytest.approx(-0.4, abs=0.08)
    assert c["beta_hml"] == pytest.approx(0.6, abs=0.08)
    assert res["alpha_annual_arithmetic"] == pytest.approx(c["alpha"] * 252)
    assert res["observations"] == len(f)


def test_rf_is_subtracted_exactly_once():
    f = synthetic_factors()
    # A portfolio earning exactly RF has zero alpha and zero betas.
    res = ff3_regression(f["RF"].copy(), f)
    assert abs(res["coefficients"]["alpha"]) < 1e-12
    assert abs(res["coefficients"]["beta_mkt"]) < 1e-12


def test_insufficient_history():
    f = synthetic_factors(n=30)
    res = ff3_regression(f["RF"] + f["Mkt-RF"], f, min_obs=60)
    assert res["status"] == "insufficient_history"
    assert "coefficients" not in res


def test_dates_after_factor_coverage_are_reported_not_filled():
    f = synthetic_factors(n=200)
    extra = pd.bdate_range(f.index[-1] + pd.Timedelta(days=1), periods=10)
    rp = pd.concat([f["RF"] + f["Mkt-RF"], pd.Series(0.01, index=extra)])
    res = ff3_regression(rp, f)
    assert res["observations"] == 200
    assert res["portfolio_obs_after_latest_factor_date"] == 10


def test_parse_french_csv_converts_percent_to_decimal():
    text = ("This file was created by CMPT_ME_BEME_RETS_DAILY\n\n"
            ",Mkt-RF,SMB,HML,RF\n"
            "20300102,   1.00,  -0.50,   0.25,   0.02\n"
            "20300103,  -2.00,   0.10,   0.00,   0.02\n\n"
            "Copyright 2030\n")
    f = parse_french_csv(text)
    assert list(f.columns) == ["Mkt-RF", "SMB", "HML", "RF"]
    assert f.loc["2030-01-02", "Mkt-RF"] == pytest.approx(0.01)
    assert f.loc["2030-01-03", "SMB"] == pytest.approx(0.001)
