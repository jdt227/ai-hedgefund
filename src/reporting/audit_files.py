from __future__ import annotations

from datetime import datetime
from pathlib import Path


def create_run_directory(
    base_directory: str | Path = "runs",
    run_date: datetime | None = None,
) -> Path:
    """
    Create and return a dated directory for a single research run.

    Example:
    runs/2026-09-18_1608/
    """
    timestamp = run_date or datetime.now()
    run_id = timestamp.strftime("%Y-%m-%d_%H%M")

    run_directory = Path(base_directory) / run_id
    run_directory.mkdir(parents=True, exist_ok=True)

    return run_directory


def save_weight_prompt(
    run_directory: str | Path,
    portfolio_selection_instructions: str,
    macro_report: str,
    firm_investment_reports: str,
    portfolio_risk_packet: str,
) -> Path:
    """
    Assemble and save the exact prompt submitted to the portfolio model.

    The output is intentionally named weight_prompt.txt to match the
    assignment submission field and to preserve an audit record.
    """
    run_directory = Path(run_directory)

    full_prompt = f"""
{portfolio_selection_instructions.strip()}

============================================================
MACRO REPORT
============================================================
{macro_report.strip()}

============================================================
ELIGIBLE FIRM INVESTMENT REPORTS
============================================================
{firm_investment_reports.strip()}

============================================================
PORTFOLIO CONSTRAINTS AND QUANTITATIVE RISK PACKET
============================================================
{portfolio_risk_packet.strip()}
""".strip()

    output_path = run_directory / "weight_prompt.txt"
    output_path.write_text(full_prompt + "\n", encoding="utf-8")

    return output_path


def save_recommendation_reason(
    run_directory: str | Path,
    model_response_text: str,
) -> Path:
    """
    Save the model's complete, unedited portfolio-selection response.

    The output is intentionally named recommendation_reason.txt to match
    the assignment submission field.
    """
    run_directory = Path(run_directory)

    if not model_response_text or not model_response_text.strip():
        raise ValueError(
            "Model response is empty; recommendation file was not created."
        )

    output_path = run_directory / "recommendation_reason.txt"
    output_path.write_text(
        model_response_text.strip() + "\n",
        encoding="utf-8",
    )

    return output_path