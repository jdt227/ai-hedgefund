import pytest

from src.validate_allocation import extract_block, main, run

GOOD_ROWS = ["AAPL,8", "MSFT,8", "NVDA,8", "JPM,8", "BAC,8", "JNJ,8", "LLY,8", "XOM,8", "CVX,8", "WMT,8", "CASH,20"]


def response(rows, wrapper="CSV"):
    return "Thesis text.\n\n## Final\n" + wrapper + "\nSymbol,Weight\n" + "\n".join(rows) + "\n" + wrapper + "\n"


def write(tmp_path, text):
    path = tmp_path / "recommendation_reason.txt"
    path.write_text(text)
    return path


def failed(checks):
    return {c.name for c in checks if not c.passed}


@pytest.mark.parametrize("wrapper", ["CSV", "```", ""])
def test_valid_allocation_passes_and_writes_file(tmp_path, wrapper):
    passed, checks = run(write(tmp_path, response(GOOD_ROWS, wrapper)))
    assert passed, failed(checks)
    assert (tmp_path / "final_allocation.csv").read_text().startswith("Symbol,Weight\nAAPL,8\n")


def test_eight_holdings_with_more_cash_passes(tmp_path):
    rows = ["AAPL,8", "MSFT,8", "JPM,8", "BAC,8", "JNJ,7", "XOM,7", "CVX,7", "WMT,7", "CASH,40"]
    assert run(write(tmp_path, response(rows)))[0]


@pytest.mark.parametrize("rows,rule", [
    (GOOD_ROWS[:7] + ["CASH,44"], "8-10 positions"),
    (GOOD_ROWS[:-1] + ["PG,1", "CASH,19"], "8-10 positions"),
    (["AAPL,9"] + GOOD_ROWS[1:-1] + ["CASH,19"], "Each stock at most 8%"),
    (["AAPL,7.5", "MSFT,8.5"] + GOOD_ROWS[2:], "Whole-percentage weights"),
    (GOOD_ROWS[:-1] + ["CASH,10"], "CASH row present, at least 20%"),
    (GOOD_ROWS[:-1], "CASH row present, at least 20%"),
    (GOOD_ROWS[:-1] + ["CASH,25"], "Weights sum to exactly 100"),
    (["SPY,8"] + GOOD_ROWS[1:], "Tickers are in the universe"),
    (["AAPL,8", "MSFT,8", "NVDA,8", "AVGO,8"] + GOOD_ROWS[3:9] + ["CASH,20"], "Each sector at most 25%"),
    (["AAPL,8", "AAPL,8"] + GOOD_ROWS[2:], "No duplicate symbols"),
    (["AAPL,abc"] + GOOD_ROWS[1:], "Weights are valid and non-negative"),
    (["AAPL,-8"] + GOOD_ROWS[1:], "Weights are valid and non-negative"),
    (["CASH,100"], "8-10 positions"),
])
def test_broken_allocations_are_rejected(tmp_path, rows, rule):
    passed, checks = run(write(tmp_path, response(rows)))
    assert not passed
    assert rule in failed(checks)
    assert not (tmp_path / "final_allocation.csv").exists()


def test_equity_above_80_is_rejected(tmp_path):
    rows = GOOD_ROWS[:-1] + ["PG,1", "CASH,19"]
    assert "Equity at most 80%" in failed(run(write(tmp_path, response(rows)))[1])


def test_missing_block_is_rejected(tmp_path):
    passed, checks = run(write(tmp_path, "I recommend buying some stocks."))
    assert not passed and "Allocation block found" in failed(checks)


def test_last_block_wins_when_the_model_quotes_an_example(tmp_path):
    text = response(["TICKER,8", "CASH,92"]) + "\nFinal:\n" + response(GOOD_ROWS)
    assert run(write(tmp_path, text))[0]


def test_failed_revalidation_removes_stale_output(tmp_path):
    path = write(tmp_path, response(GOOD_ROWS))
    assert run(path)[0]
    path.write_text(response(GOOD_ROWS[:-1] + ["CASH,10"]))
    assert not run(path)[0]
    assert not (tmp_path / "final_allocation.csv").exists()


def test_percent_signs_are_accepted():
    rows, error = extract_block("Symbol,Weight\nAAPL,8%\nCASH,20%\n")
    assert error is None and rows == [("AAPL", "8%"), ("CASH", "20%")]


def test_cli_exit_codes(tmp_path, capsys):
    assert main([str(write(tmp_path, response(GOOD_ROWS)))]) == 0
    assert main([str(write(tmp_path, response(GOOD_ROWS[:-1])))]) == 1
    assert main([str(tmp_path / "missing.txt")]) == 1
    assert "FAIL" in capsys.readouterr().out
