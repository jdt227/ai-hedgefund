"""Extension point for future research overlays (NOT implemented yet).

Planned overlays - e.g. a politician-disclosure watcher, an evidence-based
"scout" rubric, an experiment planner - must implement ``SignalOverlay`` and
obey these rules so they can be evaluated against the baseline:

* Return one row per ticker with a numeric ``value`` and an ``available_at``
  timestamp (when the information became PUBLIC, e.g. a disclosure's filing
  date, not the underlying transaction date). Rows with available_at after
  the run cutoff must be dropped.
* Overlays may only add a bounded, configured weight to the composite score.
  They can never override eligibility, sizing limits, the cash buffer,
  the stock minimum, or the readiness gate in src/strategy/orders.py.
* Each overlay has its own version string, which goes into the run's
  strategy snapshot so its effect can be ablated (run with/without it).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import pandas as pd


class SignalOverlay(Protocol):
    name: str
    version: str
    max_weight: float

    def compute(self, tickers: list[str], cutoff: datetime) -> pd.DataFrame:
        """Return columns: ticker, value, available_at, evidence (text/URL)."""
        ...
