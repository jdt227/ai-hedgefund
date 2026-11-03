import pandas as pd
import pytest

from config import settings
from src.strategy.orders import order_readiness
from src.trade_list import COMPLIANCE_LABEL, TradeInputs, build_trade_list, main, order_for_stock_floor


@pytest.fixture(autouse=True)
def alpha_profile(monkeypatch):
    monkeypatch.setattr(settings, "PORTFOLIO_LIMITS", "alpha_v1")


def make_inputs(shares, targets, cash=50_000.0, price=100.0, **kw):
    tickers = set(shares) | set(targets)
    total = sum(s * price for s in shares.values()) + (cash or 0)
    defaults = dict(
        shares=shares, targets=targets,
        current_weights={t: 100 * s * price / total for t, s in shares.items()},
        ranks={t: i for i, t in enumerate(sorted(tickers), start=1)}, actions={t: "HOLD" for t in shares},
        sectors={t: f"S{i}" for i, t in enumerate(sorted(tickers))}, prices={t: price for t in tickers},
        price_dates={t: "2030-01-15" for t in tickers}, cash=cash, executed_buys_lower_bound=5, executed_known=True)
    defaults.update(kw)
    return TradeInputs(**defaults)


def sequence(result):
    return [(t["action"], t["ticker"]) for t in result.trades]


def test_sells_then_trims_then_buys_when_floor_is_safe():
    shares = {f"S{i}": 100 for i in range(7)} | {"OLD": 100, "BIG": 300}
    targets = {f"S{i}": 10 for i in range(7)} | {"BIG": 5, "NEW": 10}
    inputs = make_inputs(shares, targets, cash=20_000)
    result = build_trade_list(inputs)
    actions = [a for a, _ in sequence(result)]
    assert actions.index("SELL") < actions.index("TRIM") < actions.index("BUY")
    assert result.min_stocks_held >= 5


def test_floor_moves_buys_before_sales_and_keeps_five_at_every_step():
    shares = {t: 100 for t in ("A", "B", "C", "D", "E")}
    targets = {"A": 12, "D": 12, "E": 12, "X": 12, "Y": 12}
    result = build_trade_list(make_inputs(shares, targets))
    order = sequence(result)
    assert order.index(("BUY", "X")) < order.index(("SELL", "B"))
    assert order.index(("BUY", "Y")) < order.index(("SELL", "C"))
    assert all(t["stocks_held_after"] >= 5 for t in result.trades)
    assert any("to keep at least 5 stocks" in n for n in result.notes)


def test_etf_sale_never_needs_reordering_and_comes_first():
    shares = {t: 100 for t in ("A", "B", "C", "D", "E", "SPY")}
    targets = {t: 12 for t in ("A", "B", "C", "D", "E")} | {"X": 12}
    result = build_trade_list(make_inputs(shares, targets))
    assert sequence(result)[0] == ("SELL", "SPY")
    assert not any("to keep at least" in n for n in result.notes)


def test_floor_breach_without_enough_buys_is_flagged():
    shares = {t: 100 for t in ("A", "B", "C", "D", "E")}
    result = build_trade_list(make_inputs(shares, {"A": 12, "B": 12, "C": 12}))
    assert result.min_stocks_held == 3 and any("CLASS RULE RISK" in n for n in result.notes)


def test_whole_shares_buys_round_down_and_sells_exit_completely():
    inputs = make_inputs({"A": 100, "OLD": 37}, {"A": 10, "NEW": 10}, cash=90_000, price=333.0)
    result = build_trade_list(inputs)
    by = {t["ticker"]: t for t in result.trades}
    assert by["OLD"]["shares"] == 37
    assert float(by["NEW"]["shares"]).is_integer() and by["NEW"]["shares"] * 333 <= 0.10 * inputs.cash + 0.1 * 45_621
    assert by["NEW"]["est_dollars"] == by["NEW"]["shares"] * 333


def test_small_changes_are_ignored_but_exits_are_not():
    inputs = make_inputs({"A": 100, "B": 1}, {"A": 10}, cash=90_000)
    inputs.current_weights["A"] = 9.6
    assert sequence(build_trade_list(inputs)) == [("SELL", "B")]


def test_change_smaller_than_one_share_is_skipped_with_a_note():
    inputs = make_inputs({"A": 1}, {"A": 2}, cash=1_000, price=1_000.0)
    inputs.current_weights["A"] = 0.5
    result = build_trade_list(inputs)
    assert result.trades == [] and any("smaller than one whole share" in n for n in result.notes)


def test_commission_and_cash_are_tracked():
    inputs = make_inputs({"OLD": 10}, {"NEW": 10}, cash=99_000, commission=5.0)
    result = build_trade_list(inputs)
    # NEW is 10% of $100,000. The floor places it first, so the sale's cash arrives last.
    assert result.trades[0]["cash_after"] == pytest.approx(99_000 - 10_000 - 5)
    assert result.trades[-1]["cash_after"] == pytest.approx(99_000 - 10_000 - 5 + 1_000 - 5)
    assert result.trades[-1]["commission"] == 5.0


def test_cash_unknown_gives_weight_changes_only():
    inputs = make_inputs({"A": 100, "OLD": 100}, {"A": 12, "NEW": 12}, cash=None)
    result = build_trade_list(inputs)
    assert all("shares" not in t and "est_dollars" not in t for t in result.trades)
    assert all("cash_after" not in t for t in result.trades)


# --- monthly buy compliance -------------------------------------------------------
def test_compliance_buys_added_only_when_needed():
    shares = {"A": 100, "B": 100}
    targets = {"A": 10, "B": 10}
    fine = make_inputs(shares, targets, cash=80_000, executed_buys_lower_bound=5)
    assert build_trade_list(fine).trades == []
    short = make_inputs(shares, targets, cash=80_000, executed_buys_lower_bound=4)
    result = build_trade_list(short)
    assert sequence(result) == [("ADD", "A")]
    assert result.trades[0]["note"] == COMPLIANCE_LABEL


def test_compliance_uses_highest_ranked_hold_and_smallest_whole_shares():
    inputs = make_inputs({"A": 100, "B": 100}, {"A": 10, "B": 10}, cash=80_000, executed_buys_lower_bound=4,
                         ranks={"A": 9, "B": 2})
    trade = build_trade_list(inputs).trades[0]
    assert trade["ticker"] == "B" and trade["shares"] == 10  # 1% of $100,000 at $100


def test_compliance_never_breaks_a_limit():
    inputs = make_inputs({"A": 100}, {"A": 12}, cash=90_000, executed_buys_lower_bound=0)
    result = build_trade_list(inputs)
    assert not [t for t in result.trades if t["note"] == COMPLIANCE_LABEL]
    assert any("No limit was broken" in n for n in result.notes)


def test_compliance_respects_equity_limit_and_share_rounding():
    # At $5,000 a share, one whole share is 5 points; A at 10 + 5 would exceed 12.
    inputs = make_inputs({"A": 2}, {"A": 10}, cash=90_000, price=5_000.0, executed_buys_lower_bound=4)
    assert not [t for t in build_trade_list(inputs).trades if t["note"] == COMPLIANCE_LABEL]


def test_only_hold_positions_get_compliance_buys():
    inputs = make_inputs({"A": 100}, {"A": 10}, cash=90_000, executed_buys_lower_bound=4, actions={"A": "TRIM"})
    assert not build_trade_list(inputs).trades


def test_expected_future_runs_reduce_the_need():
    inputs = make_inputs({"A": 100, "B": 100}, {"A": 10, "B": 10}, cash=80_000, executed_buys_lower_bound=4,
                         runs_remaining=1)
    assert build_trade_list(inputs).trades == []


def test_buys_in_list_count_toward_projection():
    inputs = make_inputs({"A": 100}, {"A": 10, "X": 10, "Y": 10}, cash=90_000, executed_buys_lower_bound=3)
    result = build_trade_list(inputs)
    assert result.buys_in_list == 2 and result.projected_buys == 5 and result.shortfall == 0


# --- ordering function ------------------------------------------------------------
def test_order_function_is_stable_without_sells():
    trades = [{"action": "ADD", "ticker": "A", "note": ""}, {"action": "BUY", "ticker": "B", "note": ""}]
    ordered, notes = order_for_stock_floor(trades, 5, 5)
    assert [t["ticker"] for t in ordered] == ["B", "A"] and not notes


# --- CLI and readiness gate --------------------------------------------------------
def run_dir(tmp_path, alloc):
    run = tmp_path / "run"
    run.mkdir()
    pd.DataFrame(alloc, columns=["Symbol", "Weight"]).to_csv(run / "final_allocation.csv", index=False)
    pd.DataFrame([{"ticker": "XMPA", "current_weight_pct": 8.3, "rank": 3, "action": "HOLD"},
                  {"ticker": "XMPB", "current_weight_pct": 8.3, "rank": 90, "action": "SELL"}]).to_csv(
        run / "holdings_review.csv", index=False)
    today = pd.Timestamp.now(tz="UTC").date().isoformat()
    pd.DataFrame([{"ticker": t, "reference_price": 100.0, "reference_date": today} for t in ("XMPA", "XMPB", "XMPC")]
                 ).to_csv(run / "reference_prices.csv", index=False)
    return run


ALLOC = [("XMPA", 12), ("XMPC", 12), ("CASH", 76)]


def test_cli_requires_a_passed_validation(tmp_path, capsys, make_account):
    run = tmp_path / "empty"
    run.mkdir()
    assert main([str(run), "--account-dir", str(make_account())]) == 1
    assert "Run the validator first" in capsys.readouterr().out


def test_cli_ready_account_gets_shares(tmp_path, capsys, make_account):
    run = run_dir(tmp_path, ALLOC)
    assert main([str(run), "--account-dir", str(make_account())]) == 0
    out = capsys.readouterr().out
    frame = pd.read_csv(run / "trade_list.csv")
    assert frame["shares"].notna().all() and "Recommendation only" in out
    assert (run / "trade_list_summary.md").exists() and "Weight changes only" not in out


def test_cli_missing_inputs_blocks_shares_and_names_fields(tmp_path, capsys, make_account):
    from conftest import base_metadata
    run = run_dir(tmp_path, ALLOC)
    meta = base_metadata(cash=None, game_name=None, reported_account_value=None)
    assert main([str(run), "--account-dir", str(make_account(metadata=meta))]) == 0
    out = capsys.readouterr().out
    assert "Weight changes only" in out and "`cash`" in out and "`game_name`" in out
    assert pd.read_csv(run / "trade_list.csv")["shares"].isna().all()


def test_cli_stale_prices_block_shares(tmp_path, capsys, make_account):
    run = run_dir(tmp_path, ALLOC)
    prices = pd.read_csv(run / "reference_prices.csv")
    prices["reference_date"] = "2020-01-02"
    prices.to_csv(run / "reference_prices.csv", index=False)
    main([str(run), "--account-dir", str(make_account())])
    assert "`market_data`" in capsys.readouterr().out
