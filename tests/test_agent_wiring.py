import json
import subprocess
import sys
from pathlib import Path

import pytest

from config import settings

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".claude" / "hooks" / "agent_guard.py"
AGENTS = sorted((ROOT / ".claude" / "agents").glob("*.md"))


def frontmatter(path):
    text = path.read_text()
    assert text.startswith("---\n")
    return text.split("---\n")[1], text.split("---\n", 2)[2]


def test_seven_roles_with_read_glob_grep_write_only():
    assert {p.stem for p in AGENTS} == {"macro-strategist", "equity-research-analyst", "nba-scout",
                                        "politician-tracker", "risk-manager", "lebron", "portfolio-manager"}
    for path in AGENTS:
        head, _ = frontmatter(path)
        assert "\ntools: Read, Glob, Grep, Write\n" in "\n" + head
        assert "agent_guard.py" in head and "model:" in head


def test_strongest_models_for_risk_and_pm():
    for name in ("risk-manager", "portfolio-manager"):
        assert "model: opus" in frontmatter(ROOT / ".claude" / "agents" / f"{name}.md")[0]


def test_objective_and_memo_rules_in_every_committee_prompt():
    for path in AGENTS:
        if path.stem == "portfolio-manager":
            continue
        body = frontmatter(path)[1]
        assert settings.OBJECTIVE_STATEMENT in body.replace("\n", " ")
        assert "Data used / data missing" in body and "UNTRUSTED" in body


def test_lebron_guardrails_and_disclaimer():
    body = frontmatter(ROOT / ".claude" / "agents" / "lebron.md")[1]
    assert "Never claim to be the real LeBron James" in body and "Never invent quotes" in body
    assert "not affiliated with or endorsed by him" in body


def test_pm_prompt_is_minimal_and_reads_only_the_weight_prompt():
    body = frontmatter(ROOT / ".claude" / "agents" / "portfolio-manager.md")[1]
    assert "Read no other file" in body and len(body.split()) < 80


def test_skill_is_user_invoked_only():
    head = (ROOT / ".claude" / "skills" / "weekly-picks" / "SKILL.md").read_text().split("---\n")[1]
    assert "name: weekly-picks" in head and "disable-model-invocation: true" in head


# --- guard ------------------------------------------------------------------------
@pytest.fixture
def project(tmp_path):
    run = tmp_path / "runs" / "2030-01-15_1200"
    (run / "agent_inputs").mkdir(parents=True)
    (run / "agents").mkdir()
    (tmp_path / "runs" / ".current_run").write_text("2030-01-15_1200\n")
    return tmp_path, run


def guard(project_dir, role, tool, path=None, pattern=None):
    payload = {"tool_name": tool, "tool_input": {}, "cwd": str(project_dir)}
    if path is not None:
        payload["tool_input"]["file_path" if tool not in ("Glob", "Grep") else "path"] = str(path)
    if pattern is not None:
        payload["tool_input"]["pattern"] = pattern
    result = subprocess.run([sys.executable, str(GUARD), role], input=json.dumps(payload), capture_output=True,
                            text=True, env={"CLAUDE_PROJECT_DIR": str(project_dir), "PATH": ""})
    return result.returncode


@pytest.mark.parametrize("role,tool,rel,allowed", [
    ("macro_strategist", "Read", "agent_inputs/macro_packet.md", True),
    ("macro_strategist", "Write", "agents/macro_strategist.md", True),
    ("macro_strategist", "Write", "agents/risk_manager.json", False),
    ("macro_strategist", "Read", "agents/risk_manager.md", False),
    ("macro_strategist", "Read", "weight_prompt.txt", False),
    ("equity_research_analyst", "Write", "agents/equity_AAPL.json", True),
    ("equity_research_analyst", "Read", "agents/macro_strategist.md", True),
    ("risk_manager", "Read", "agents/nba_scout.json", True),
    ("risk_manager", "Read", "agents/committee.json", False),
    ("risk_manager", "Write", "recommendation_reason.txt", False),
    ("lebron", "Glob", "agents", True),
    ("portfolio_manager", "Read", "weight_prompt.txt", True),
    ("portfolio_manager", "Read", "agent_inputs/risk_packet.md", False),
    ("portfolio_manager", "Read", "agents/lebron.md", False),
    ("portfolio_manager", "Write", "recommendation_reason.txt", True),
    ("portfolio_manager", "Write", "weight_prompt.txt", False),
])
def test_guard_paths(project, role, tool, rel, allowed):
    root, run = project
    assert (guard(root, role, tool, run / rel) == 0) is allowed


def test_guard_blocks_other_runs_private_files_and_escapes(project):
    root, run = project
    other = root / "runs" / "2020-01-01_0000" / "agent_inputs" / "macro_packet.md"
    assert guard(root, "macro_strategist", "Read", other) == 2
    assert guard(root, "macro_strategist", "Read", root / "private" / "account" / "holdings.csv") == 2
    assert guard(root, "macro_strategist", "Read", run / "agent_inputs" / ".." / ".." / ".." / ".env") == 2
    assert guard(root, "macro_strategist", "Glob", run / "agent_inputs", pattern="../../*") == 2
    assert guard(root, "macro_strategist", "Glob", None, pattern="**/*") == 2


def test_guard_blocks_other_tools_and_unknown_roles(project):
    root, run = project
    assert guard(root, "risk_manager", "Bash", run / "agent_inputs") == 2
    assert guard(root, "risk_manager", "WebFetch", run / "agent_inputs") == 2
    assert guard(root, "someone", "Read", run / "agent_inputs" / "x.md") == 2


def test_guard_requires_current_run_marker(project):
    root, run = project
    (root / "runs" / ".current_run").unlink()
    assert guard(root, "macro_strategist", "Read", run / "agent_inputs" / "macro_packet.md") == 2


def test_relative_paths_resolve_against_cwd(project):
    root, run = project
    assert guard(root, "macro_strategist", "Read", "runs/2030-01-15_1200/agent_inputs/macro_packet.md") == 0


# --- retry -----------------------------------------------------------------------------
def test_retry_saves_attempt_and_appends_feedback(tmp_path):
    from src.agents.retry import retry
    run = tmp_path / "r"
    run.mkdir()
    (run / "weight_prompt.txt").write_text("PROMPT\n")
    (run / "recommendation_reason.txt").write_text("Symbol,Weight\nAAPL,50\nCASH,50\n")
    assert retry(run) == 0
    prompt = (run / "weight_prompt.txt").read_text()
    assert prompt.startswith("PROMPT\n\n====") and "VALIDATOR FEEDBACK" in prompt and "FAIL" in prompt
    assert (run / "pm_attempts" / "attempt_1" / "weight_prompt.txt").read_text() == "PROMPT\n"
    assert not (run / "recommendation_reason.txt").exists()
    (run / "recommendation_reason.txt").write_text("Symbol,Weight\nAAPL,60\nCASH,40\n")
    assert retry(run) == 0
    prompt = (run / "weight_prompt.txt").read_text()
    assert prompt.count("VALIDATOR FEEDBACK") == 1 and "attempt 2" in prompt
    (run / "recommendation_reason.txt").write_text("Symbol,Weight\nAAPL,70\nCASH,30\n")
    assert retry(run) == 1  # limit reached


def test_agents_flag_writes_current_run_marker(monkeypatch, tmp_path):
    from src import run_pipeline
    from test_weekly_pipeline import NOW, RUN, universe_prices
    from datetime import date, datetime

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return RUN

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_pipeline, "date", FrozenDate)
    monkeypatch.setattr(run_pipeline, "datetime", FrozenDatetime)
    monkeypatch.setattr(run_pipeline, "download_price_data", lambda start: universe_prices())
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    run_pipeline.main(["--agents"])
    current = (tmp_path / "runs" / ".current_run").read_text().strip()
    run_dir = tmp_path / "runs" / current
    assert (run_dir / "agent_inputs" / "manifest.json").exists() and not (run_dir / "weight_prompt.txt").exists()
