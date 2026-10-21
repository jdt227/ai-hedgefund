"""Shared fixtures. All data here is fictitious (tickers XMP*, invented game)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GAME = "Example Class Game (FICTITIOUS)"
TRADE_HEADER = "trade_id,order_id,executed_at,ticker,side,shares,fill_price,fees,game_name,recommendation_run_id,notes\n"
HOLD_HEADER = "ticker,shares,displayed_purchase_price,displayed_current_price,market_value,reported_unrealized_pnl,asset_type\n"


def base_metadata(**overrides) -> dict:
    meta = {
        "game_name": GAME,
        "snapshot_captured_at": "2030-01-15T16:30:00",
        "snapshot_timezone": "America/New_York",
        "holdings_complete": True,
        "cash": 50000.0,
        "reported_account_value": 60000.0,
        "reported_holdings_value": 10000.0,
        "ledger_complete_after_snapshot": True,
        "buy_history_complete_from": "2030-01-01T00:00:00",
    }
    meta.update(overrides)
    return meta


@pytest.fixture
def make_account(tmp_path):
    """Write an account directory and return its path."""

    def _make(metadata: dict | None = None, holdings: str | None = None, trades: str = "",
              holdings_rows: list[str] | None = None) -> Path:
        d = tmp_path / "account"
        d.mkdir(exist_ok=True)
        (d / "account_metadata.json").write_text(json.dumps(metadata if metadata is not None else base_metadata()))
        if holdings is None:
            rows = holdings_rows if holdings_rows is not None else [
                "XMPA,50,95.00,100.00,5000.00,250.00,STOCK",
                "XMPB,25,190.00,200.00,5000.00,250.00,STOCK",
            ]
            holdings = HOLD_HEADER + "".join(r + "\n" for r in rows)
        (d / "holdings.csv").write_text(holdings)
        (d / "trades.csv").write_text(TRADE_HEADER + trades)
        return d

    return _make
