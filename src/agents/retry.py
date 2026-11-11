"""Save a failed PM attempt and give the PM the validator's feedback.

    python -m src.agents.retry runs/<run_id>

Copies weight_prompt.txt, recommendation_reason.txt, and the validator table to
pm_attempts/attempt_<n>/, then rewrites weight_prompt.txt as the assembled prompt
plus a VALIDATOR FEEDBACK section. The final weight_prompt.txt is therefore
exactly the prompt of the attempt that produced the final response.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from config import settings  # noqa: E402
from src.validate_allocation import print_checks, run  # noqa: E402

BASE = "weight_prompt_assembled.txt"


def retry(run_dir: Path) -> int:
    attempts = run_dir / "pm_attempts"
    used = len(list(attempts.glob("attempt_*"))) if attempts.exists() else 0
    if used >= settings.AGENT_PM_MAX_RETRIES:
        print(f"Retry limit reached ({settings.AGENT_PM_MAX_RETRIES}). Report the validator failure.")
        return 1
    response = run_dir / "recommendation_reason.txt"
    passed, checks = run(response)
    if passed:
        print("The response already passes. No retry needed.")
        return 1
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        print_checks(checks)
    table = buffer.getvalue().strip()
    attempt = attempts / f"attempt_{used + 1}"
    attempt.mkdir(parents=True)
    shutil.copy(run_dir / "weight_prompt.txt", attempt / "weight_prompt.txt")
    shutil.copy(response, attempt / "recommendation_reason.txt")
    (attempt / "validator.txt").write_text(table + "\n", encoding="utf-8")
    base = run_dir / BASE
    if not base.exists():
        shutil.copy(run_dir / "weight_prompt.txt", base)
    feedback = ("\n\n============================================================\nVALIDATOR FEEDBACK\n"
                "============================================================\n"
                f"Your previous response (attempt {used + 1}) failed these checks. Fix every FAIL and return a "
                "complete new response in the required format.\n\n" + table + "\n")
    (run_dir / "weight_prompt.txt").write_text(base.read_text(encoding="utf-8").rstrip("\n") + feedback,
                                               encoding="utf-8")
    response.unlink()
    print(f"Saved {attempt}. weight_prompt.txt now includes VALIDATOR FEEDBACK. Rerun the portfolio-manager.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    return retry(parser.parse_args(argv).run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
