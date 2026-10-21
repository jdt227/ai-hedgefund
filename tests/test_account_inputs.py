import json

from conftest import GAME, base_metadata
from src.account.inputs import load_account, parse_metadata


def codes(account, severity=None):
    return {i.code for i in account.issues if severity is None or i.severity == severity}


def test_valid_example_account_has_no_errors(make_account):
    account = load_account(make_account())
    assert account.errors == []
    assert "account_value_ok" in codes(account)
    assert "holdings_total_ok" in codes(account)


def test_templates_and_examples_load_cleanly(tmp_path):
    import shutil
    from pathlib import Path

    d = tmp_path / "acct"
    d.mkdir()
    ex = Path("templates/account/examples")
    shutil.copy(ex / "account_metadata.example.json", d / "account_metadata.json")
    shutil.copy(ex / "holdings.example.csv", d / "holdings.csv")
    shutil.copy(ex / "trades.example.csv", d / "trades.csv")
    assert load_account(d).errors == []


def test_missing_metadata_fields_are_unknown_not_errors(make_account):
    account = load_account(make_account(metadata={"snapshot_captured_at": "2030-01-15T16:30:00"}))
    assert account.errors == []
    assert account.metadata.cash is None and account.metadata.game_name is None
    assert {"unknown_cash", "unknown_game_name"} <= codes(account, "info")


def test_nan_and_infinity_rejected(make_account):
    rows = ["XMPA,nan,95,100,5000,250,STOCK", "XMPB,25,190,inf,5000,250,STOCK"]
    account = load_account(make_account(metadata=base_metadata(reported_holdings_value=None,
                                                               reported_account_value=None),
                                        holdings_rows=rows))
    assert sum(1 for i in account.errors if i.code == "non_finite") == 2


def test_metadata_rejects_nonfinite_cash():
    _, issues = parse_metadata({"cash": float("nan")})
    assert any(i.code == "metadata_number" for i in issues)


def test_duplicate_holding_and_blank_ticker(make_account):
    rows = ["XMPA,50,95,100,5000,250,STOCK", "XMPA,1,95,100,100,5,STOCK", ",3,1,1,3,0,STOCK"]
    account = load_account(make_account(metadata=base_metadata(reported_holdings_value=None,
                                                               reported_account_value=None),
                                        holdings_rows=rows))
    assert {"duplicate_holding", "blank_ticker"} <= codes(account, "error")


def test_market_value_mismatch_beyond_rounding(make_account):
    rows = ["XMPA,50,95.00,100.00,5000.40,250.00,STOCK"]   # tolerance is 50*0.005+0.01 = 0.26
    account = load_account(make_account(metadata=base_metadata(reported_holdings_value=None,
                                                               reported_account_value=None),
                                        holdings_rows=rows))
    assert "market_value_mismatch" in codes(account, "error")


def test_reported_pnl_preserved_alongside_calculated(make_account):
    rows = ["XMPA,40,100.12,110.37,4414.80,410.15,STOCK"]  # display rounding: calc 410.00
    account = load_account(make_account(metadata=base_metadata(reported_holdings_value=None,
                                                               reported_account_value=None),
                                        holdings_rows=rows))
    h = account.holdings.iloc[0]
    assert h["reported_unrealized_pnl"] == 410.15
    assert abs(h["calc_unrealized_pnl"] - 410.00) < 1e-9
    assert account.errors == []


def test_account_value_inconsistency(make_account):
    account = load_account(make_account(metadata=base_metadata(reported_account_value=70000.0)))
    assert "account_value_mismatch" in codes(account, "error")


def test_wrong_game_name_rejected(make_account):
    trades = "F1,O1,2030-01-16T10:00:00-05:00,XMPC,BUY,10,50,0,Some Other Game,,\n"
    account = load_account(make_account(trades=trades))
    assert "wrong_game" in codes(account, "error")
    assert not account.trades["valid"].iloc[0]


def test_duplicate_trade_ids(make_account):
    trades = (f"F1,O1,2030-01-16T10:00:00-05:00,XMPC,BUY,10,50,0,{GAME},,\n"
              f"F1,O2,2030-01-16T11:00:00-05:00,XMPC,BUY,5,50,0,{GAME},,\n")
    account = load_account(make_account(trades=trades))
    assert "duplicate_trade_id" in codes(account, "error")


def test_trade_field_validation(make_account):
    trades = (f"F1,,2030-01-16T10:00:00,XMPC,BUY,10,50,0,{GAME},,\n"          # naive timestamp
              f"F2,,2030-01-16T10:00:00-05:00,XMPC,HOLD,10,50,0,{GAME},,\n"   # bad side
              f"F3,,2030-01-16T10:00:00-05:00,XMPC,BUY,-1,50,0,{GAME},,\n"    # negative shares
              f"F4,,2030-01-16T10:00:00-05:00,XMPC,BUY,1,0,0,{GAME},,\n"      # zero price
              f"F5,,2030-01-16T10:00:00-05:00,XMPC,BUY,1,50,-1,{GAME},,\n"    # negative fee
              f",,2030-01-16T10:00:00-05:00,XMPC,BUY,1,50,0,{GAME},,\n")      # blank id
    account = load_account(make_account(trades=trades))
    assert {"naive_timestamp", "bad_side", "nonpositive_shares", "nonpositive_price",
            "negative_fees", "blank_value"} <= codes(account, "error")
    assert not account.trades["valid"].any()


def test_unknown_game_marks_trades_unverified(make_account):
    trades = f"F1,O1,2030-01-16T10:00:00-05:00,XMPC,BUY,10,50,0,{GAME},,\n"
    account = load_account(make_account(metadata=base_metadata(game_name=None), trades=trades))
    assert "game_unverified" in codes(account, "warning")


def test_timezone_offset_mismatch(make_account):
    meta = base_metadata(snapshot_captured_at="2030-01-15T16:30:00+00:00")
    account = load_account(make_account(metadata=meta))
    assert "metadata_timezone_mismatch" in codes(account, "error")


def test_metadata_file_is_json_object(make_account, tmp_path):
    d = make_account()
    (d / "account_metadata.json").write_text(json.dumps([1, 2]))
    assert "metadata_json" in codes(load_account(d), "error")
