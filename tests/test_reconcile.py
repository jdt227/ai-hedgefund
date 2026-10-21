from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import GAME, HOLD_HEADER, base_metadata
from src.account.inputs import load_account
from src.account.reconcile import count_monthly_buys, reconcile

NY = "America/New_York"
NOW = datetime(2030, 1, 20, 12, 0, tzinfo=ZoneInfo(NY))


def trade(tid, when, ticker, side, shares, price, order="", fees=0, game=GAME):
    return f"{tid},{order},{when},{ticker},{side},{shares},{price},{fees},{game},,\n"


def test_complete_ledger_rolls_forward_positions_and_cash(make_account):
    trades = (trade("F1", "2030-01-16T10:00:00-05:00", "XMPC", "BUY", 10, 50.0, "O1", fees=1)
              + trade("F2", "2030-01-17T10:00:00-05:00", "XMPA", "SELL", 10, 101.0, "O2", fees=1))
    recon = reconcile(load_account(make_account(trades=trades)))
    pos = recon.positions.set_index("ticker")["shares"].to_dict()
    assert pos == {"XMPA": 40, "XMPB": 25, "XMPC": 10}
    assert recon.positions_status == "verified"
    assert recon.current_cash_status == "verified"
    assert abs(recon.current_cash - (50000 - 500 - 1 + 1010 - 1)) < 1e-9


def test_trades_before_snapshot_are_not_double_counted(make_account):
    trades = trade("F0", "2030-01-10T10:00:00-05:00", "XMPA", "BUY", 50, 95.0, "O0")
    recon = reconcile(load_account(make_account(trades=trades)))
    assert recon.positions.set_index("ticker").at["XMPA", "shares"] == 50
    assert recon.trade_timing.iloc[0]["timing"] == "before"


def test_partial_ledger_is_not_reported_as_verified(make_account):
    meta = base_metadata(ledger_complete_after_snapshot=None)
    trades = trade("F1", "2030-01-16T10:00:00-05:00", "XMPC", "BUY", 10, 50.0, "O1")
    recon = reconcile(load_account(make_account(metadata=meta, trades=trades)))
    assert recon.positions_status == "estimate"
    assert recon.current_cash is None and recon.current_cash_status == "unknown"


def test_impossible_sell_error_only_with_complete_history(make_account):
    sell = trade("F1", "2030-01-16T10:00:00-05:00", "XMPA", "SELL", 60, 100.0, "O1")
    complete = reconcile(load_account(make_account(trades=sell)))
    assert any(i.code == "impossible_sell" and i.severity == "error" for i in complete.issues)

    partial = reconcile(load_account(make_account(metadata=base_metadata(holdings_complete=None), trades=sell)))
    assert not any(i.code == "impossible_sell" for i in partial.issues)
    assert any(i.code == "unverifiable_sell" for i in partial.issues)


def test_empty_all_cash_account(make_account):
    meta = base_metadata(reported_holdings_value=0.0, reported_account_value=50000.0)
    account = load_account(make_account(metadata=meta, holdings=HOLD_HEADER))
    assert account.errors == []
    recon = reconcile(account)
    assert recon.positions.empty
    assert recon.current_cash == 50000.0


def test_unknown_snapshot_timezone_creates_ambiguity_window(make_account):
    meta = base_metadata(snapshot_timezone=None)
    trades = (trade("F1", "2030-01-15T22:00:00-05:00", "XMPC", "BUY", 1, 50.0, "O1")   # within +-14h
              + trade("F2", "2030-01-17T10:00:00-05:00", "XMPC", "BUY", 1, 50.0, "O2"))  # clearly after
    recon = reconcile(load_account(make_account(metadata=meta, trades=trades)))
    timing = dict(zip(recon.trade_timing["trade_id"], recon.trade_timing["timing"]))
    assert timing == {"F1": "ambiguous", "F2": "after"}
    assert recon.current_cash_status == "unknown"
    assert any(i.code == "ambiguous_trade_timing" for i in recon.issues)


# --- monthly BUY counting -------------------------------------------------
def test_partial_fills_grouped_by_order_id(make_account):
    trades = (trade("F1", "2030-01-16T10:00:00-05:00", "XMPC", "BUY", 5, 50.0, "O1")
              + trade("F2", "2030-01-16T10:01:00-05:00", "XMPC", "BUY", 5, 50.0, "O1")
              + trade("F3", "2030-01-16T11:00:00-05:00", "XMPD", "BUY", 5, 20.0, "O2")
              + trade("F4", "2030-01-17T11:00:00-05:00", "XMPA", "SELL", 5, 100.0, "O3"))
    status = count_monthly_buys(load_account(make_account(trades=trades)), "2030-01", NY, 5, now=NOW)
    assert status.count_lower_bound == status.count_upper_bound == 2
    assert status.status == "gap"


def test_fills_without_order_id_produce_bounds(make_account):
    trades = (trade("F1", "2030-01-16T10:00:00-05:00", "XMPC", "BUY", 5, 50.0)
              + trade("F2", "2030-01-16T10:01:00-05:00", "XMPC", "BUY", 5, 50.0))
    status = count_monthly_buys(load_account(make_account(trades=trades)), "2030-01", NY, 2, now=NOW)
    assert (status.count_lower_bound, status.count_upper_bound) == (1, 2)
    assert status.status == "unknown"


def test_month_boundary_uses_class_timezone(make_account):
    # 2030-02-01T03:00Z is still January 31 in New York.
    trades = trade("F1", "2030-02-01T03:00:00+00:00", "XMPC", "BUY", 1, 50.0, "O1")
    account = load_account(make_account(trades=trades))
    assert count_monthly_buys(account, "2030-01", NY, 1, now=NOW).count_lower_bound == 1
    assert count_monthly_buys(account, "2030-02", NY, 1, now=NOW).count_lower_bound == 0


def test_missing_history_is_unknown_not_zero(make_account):
    account = load_account(make_account(metadata=base_metadata(buy_history_complete_from=None)))
    status = count_monthly_buys(account, "2030-01", NY, 5, now=NOW)
    assert status.status == "unknown"
    assert status.coverage == "none"


def test_partial_history_can_prove_compliance(make_account):
    meta = base_metadata(buy_history_complete_from="2030-01-10T00:00:00")
    trades = "".join(trade(f"F{i}", f"2030-01-16T1{i}:00:00-05:00", "XMPC", "BUY", 1, 50.0, f"O{i}")
                     for i in range(5))
    status = count_monthly_buys(load_account(make_account(metadata=meta, trades=trades)), "2030-01", NY, 5, now=NOW)
    assert status.coverage == "partial" and status.status == "compliant"


def test_wrong_game_and_invalid_fills_not_counted(make_account):
    trades = (trade("F1", "2030-01-16T10:00:00-05:00", "XMPC", "BUY", 5, 50.0, "O1", game="Other Game")
              + trade("F2", "2030-01-16T10:00:00-05:00", "XMPC", "BUY", 5, 50.0, "O2"))
    status = count_monthly_buys(load_account(make_account(trades=trades)), "2030-01", NY, 5, now=NOW)
    assert status.count_lower_bound == 1
