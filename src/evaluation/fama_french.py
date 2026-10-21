"""Fama-French three-factor attribution.

Model (daily, decimal returns):

    Rp_t - RF_t = alpha + b_mkt (Mkt-RF)_t + b_smb SMB_t + b_hml HML_t + e_t

Conventions
-----------
* Inputs are TOTAL simple returns of the portfolio (decimal, 0.01 = 1%).
  The function subtracts RF itself; never pass excess returns.
* Ken French files publish factors in PERCENT; ``parse_french_csv`` divides
  by 100 so everything here is decimal.
* Dates are aligned on the intersection of portfolio and factor dates.
  French data lags by weeks to months; observations after the latest factor
  date are dropped and reported, not filled.
* alpha is per day. Annualized alpha is reported two ways:
  arithmetic alpha * 252, and geometric (1 + alpha)^252 - 1.
* Standard errors: classic OLS and Newey-West (HAC) with the
  floor(4 (n/100)^(2/9)) lag rule.
* Fewer than ``min_obs`` aligned observations -> "insufficient_history" and
  no estimates.

A significant alpha is evidence about the past sample, not a guarantee, and
zero factor loadings do not imply alpha.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

FRENCH_DAILY_URL = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
                    "F-F_Research_Data_Factors_daily_CSV.zip")
FACTORS = ["Mkt-RF", "SMB", "HML"]
PERIODS_PER_YEAR = 252


def parse_french_csv(text: str) -> pd.DataFrame:
    """Parse the daily 3-factor CSV (percent units) into decimal returns."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(",Mkt-RF"))
    rows = []
    for line in lines[start + 1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5 or not parts[0].isdigit() or len(parts[0]) != 8:
            if rows:
                break
            continue
        rows.append(parts[:5])
    frame = pd.DataFrame(rows, columns=["date", "Mkt-RF", "SMB", "HML", "RF"])
    frame["date"] = pd.to_datetime(frame["date"], format="%Y%m%d")
    frame = frame.set_index("date").astype(float) / 100.0
    return frame


def load_french_daily(cache_dir: Path = Path("data/cache"), max_age_days: float = 7,
                      refresh: bool = False) -> tuple[pd.DataFrame, dict]:
    import urllib.request

    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "ff3_daily.csv"
    meta_path = cache_dir / "ff3_daily.json"
    now = datetime.now(timezone.utc)
    if path.exists() and meta_path.exists() and not refresh:
        meta = json.loads(meta_path.read_text())
        age = (now - datetime.fromisoformat(meta["downloaded_at_utc"])).total_seconds() / 86400
        if age <= max_age_days:
            frame = pd.read_csv(path, index_col="date", parse_dates=True)
            return frame, meta | {"from_cache": True}
    with urllib.request.urlopen(FRENCH_DAILY_URL, timeout=30) as resp:
        payload = resp.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        text = zf.read(zf.namelist()[0]).decode("latin-1")
    frame = parse_french_csv(text)
    frame.to_csv(path)
    meta = {"source": FRENCH_DAILY_URL, "downloaded_at_utc": now.isoformat(),
            "first_date": str(frame.index[0].date()), "latest_date": str(frame.index[-1].date()),
            "units": "decimal daily returns (converted from percent)"}
    meta_path.write_text(json.dumps(meta, indent=1))
    return frame, meta | {"from_cache": False}


def _newey_west(X: np.ndarray, resid: np.ndarray, lags: int) -> np.ndarray:
    n = X.shape[0]
    xtx_inv = np.linalg.inv(X.T @ X)
    u = X * resid[:, None]
    s = u.T @ u
    for lag in range(1, lags + 1):
        w = 1 - lag / (lags + 1)
        g = u[lag:].T @ u[:-lag]
        s += w * (g + g.T)
    return xtx_inv @ s @ xtx_inv * n / (n - X.shape[1])


def ff3_regression(portfolio_returns: pd.Series, factors: pd.DataFrame, min_obs: int = 60) -> dict:
    """Regress portfolio excess returns on the three factors."""
    rp = portfolio_returns.dropna().astype(float)
    rp.index = pd.DatetimeIndex(rp.index).normalize()
    joined = pd.concat([rp.rename("rp"), factors[FACTORS + ["RF"]]], axis=1, join="inner").dropna()
    after_factors = int((rp.index > factors.index.max()).sum())
    result = {
        "observations": int(len(joined)),
        "portfolio_obs_after_latest_factor_date": after_factors,
        "factor_latest_date": str(factors.index.max().date()),
        "sample_start": str(joined.index.min().date()) if len(joined) else None,
        "sample_end": str(joined.index.max().date()) if len(joined) else None,
        "return_type": "total simple returns; RF subtracted inside",
        "annualization": f"arithmetic alpha x {PERIODS_PER_YEAR}; geometric (1+alpha)^{PERIODS_PER_YEAR}-1",
    }
    if len(joined) < min_obs:
        result["status"] = "insufficient_history"
        result["message"] = f"Need at least {min_obs} aligned daily observations; have {len(joined)}."
        return result

    y = (joined["rp"] - joined["RF"]).to_numpy()
    X = np.column_stack([np.ones(len(joined)), joined[FACTORS].to_numpy()])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    n, k = X.shape
    sigma2 = resid @ resid / (n - k)
    se_ols = np.sqrt(np.diag(sigma2 * np.linalg.inv(X.T @ X)))
    lags = int(np.floor(4 * (n / 100) ** (2 / 9)))
    se_nw = np.sqrt(np.diag(_newey_west(X, resid, lags)))
    names = ["alpha", "beta_mkt", "beta_smb", "beta_hml"]
    with np.errstate(divide="ignore", invalid="ignore"):
        t_nw = coef / se_nw
    ss_tot = ((y - y.mean()) ** 2).sum()
    result.update({
        "status": "ok",
        "coefficients": dict(zip(names, coef.tolist())),
        "se_ols": dict(zip(names, se_ols.tolist())),
        "se_newey_west": dict(zip(names, se_nw.tolist())),
        "t_newey_west": dict(zip(names, t_nw.tolist())),
        "newey_west_lags": lags,
        "r_squared": float(1 - resid @ resid / ss_tot) if ss_tot > 0 else float("nan"),
        "alpha_daily": float(coef[0]),
        "alpha_annual_arithmetic": float(coef[0] * PERIODS_PER_YEAR),
        "alpha_annual_geometric": float((1 + coef[0]) ** PERIODS_PER_YEAR - 1),
        "residual_vol_annual": float(np.sqrt(sigma2 * PERIODS_PER_YEAR)),
    })
    return result


def fixed_weight_returns(adj_close: pd.DataFrame, weights: dict[str, float], sessions: int = 252) -> pd.Series:
    """Daily returns of a constant-weight basket (rebalanced daily).

    Used only for EX-ANTE exposure: "what do today's weights load on?" It is
    NOT the account's realized performance.
    """
    tickers = [t for t in weights if t in adj_close.columns]
    w = pd.Series({t: weights[t] for t in tickers})
    w = w / w.sum()
    rets = adj_close[tickers].pct_change().iloc[-sessions:].dropna()
    return (rets * w).sum(axis=1)
