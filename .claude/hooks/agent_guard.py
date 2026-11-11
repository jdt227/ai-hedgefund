#!/usr/bin/env python3
"""PreToolUse guard for committee subagents: limits which files each role can touch.

Usage in an agent's frontmatter:  python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/agent_guard.py" <role>

The current run is named in runs/.current_run (written by run_pipeline --agents).
Agents may read only that run's agent_inputs/ and earlier memos, and write only
their own memo files. The Portfolio Manager may read only weight_prompt.txt and
write only recommendation_reason.txt. Exit code 2 blocks the call.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path

READ_TOOLS = {"Read", "Glob", "Grep"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
OWN_MEMOS = {
    "macro_strategist": ["agents/macro_strategist.md", "agents/macro_strategist.json"],
    "equity_research_analyst": ["agents/equity_*.md", "agents/equity_*.json"],
    "nba_scout": ["agents/nba_scout.md", "agents/nba_scout.json"],
    "politician_tracker": ["agents/politician_tracker.md", "agents/politician_tracker.json"],
    "risk_manager": ["agents/risk_manager.md", "agents/risk_manager.json"],
    "lebron": ["agents/lebron.md", "agents/lebron.json"],
    "portfolio_manager": ["recommendation_reason.txt"],
}
# Earlier memos each role may read, in committee order.
READABLE_MEMOS = {
    "macro_strategist": [],
    "equity_research_analyst": ["agents/macro_strategist.md"],
    "nba_scout": ["agents/macro_strategist.md"],
    "politician_tracker": ["agents/macro_strategist.md"],
    "risk_manager": ["agents/*.md", "agents/*.json"],
    "lebron": ["agents/*.md", "agents/*.json"],
}


def deny(message: str) -> None:
    print(f"agent_guard: {message}", file=sys.stderr)
    sys.exit(2)


def relative_to_run(path: Path, run_dir: Path) -> str | None:
    try:
        return path.resolve().relative_to(run_dir.resolve()).as_posix()
    except ValueError:
        return None


def allowed(role: str, tool: str, rel: str | None) -> bool:
    if rel is None:
        return False
    if role == "portfolio_manager":
        return rel == ("weight_prompt.txt" if tool in READ_TOOLS else "recommendation_reason.txt")
    if tool in WRITE_TOOLS:
        return any(fnmatch.fnmatch(rel, pattern) for pattern in OWN_MEMOS[role])
    if rel == "agent_inputs" or rel.startswith("agent_inputs/"):
        return True
    if role in ("risk_manager", "lebron") and rel == "agents" and tool != "Read":
        return True
    return any(fnmatch.fnmatch(rel, pattern) for pattern in READABLE_MEMOS[role]) and rel != "agents/committee.json"


def main() -> None:
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role not in OWN_MEMOS:
        deny(f"unknown role '{role}'")
    event = json.load(sys.stdin)
    tool = event.get("tool_name", "")
    if tool not in READ_TOOLS | WRITE_TOOLS:
        deny(f"{tool} is not allowed for committee agents")
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    marker = root / "runs" / ".current_run"
    if not marker.exists():
        deny("runs/.current_run is missing; run python -m src.run_pipeline --agents first")
    run_dir = root / "runs" / marker.read_text().strip()
    data = event.get("tool_input", {})
    target = data.get("file_path") or data.get("notebook_path") or data.get("path")
    if not target:
        deny(f"{tool} needs an explicit path inside {run_dir}")
    path = Path(target)
    if not path.is_absolute():
        path = Path(event.get("cwd") or root) / path
    pattern = data.get("pattern", "") if tool == "Glob" else ""
    if pattern.startswith("/") or ".." in Path(pattern).parts:
        deny("Glob patterns must be relative and stay inside the allowed folder")
    if not allowed(role, tool, relative_to_run(path, run_dir)):
        deny(f"{role} may not {'write' if tool in WRITE_TOOLS else 'read'} {target}")
    sys.exit(0)


if __name__ == "__main__":
    main()
