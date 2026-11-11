"""Draft the class strategy-change form from the changelog and a config diff.

    python -m src.strategy_form                 # draft against the last logged form
    python -m src.strategy_form --effective-date 2026-11-12

Writes docs/forms/<date>_<version>.md (committed) and prints it to paste. Each form
stores its fingerprint, so the next form shows only what changed since.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.strategy_fingerprint import ROOT, diff, fingerprint  # noqa: E402

FORMS_DIR = ROOT / "docs" / "forms"
CHANGELOG = ROOT / "docs" / "strategy_changelog.md"
MARKER = "<!-- fingerprint -->"


def changelog_entries(text: str) -> list[tuple[str, str]]:
    """(heading, body) for each '## ' entry, in file order."""
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    return [(p.splitlines()[0].strip(), "\n".join(p.splitlines()[1:]).strip()) for p in parts]


def last_form(forms_dir: Path = FORMS_DIR) -> tuple[Path | None, dict | None]:
    forms = sorted(forms_dir.glob("*.md")) if forms_dir.exists() else []
    if not forms:
        return None, None
    text = forms[-1].read_text(encoding="utf-8")
    block = text.split(MARKER, 1)[1].split("```json", 1)[1].split("```", 1)[0]
    return forms[-1], json.loads(block)


def changed_since_last_form(forms_dir: Path = FORMS_DIR, current: dict | None = None) -> list[str]:
    """Changes not yet covered by a form. Empty when the last form is current."""
    current = current or fingerprint()
    _, previous = last_form(forms_dir)
    if previous is None:
        return ["no strategy form has been logged yet"]
    return diff(previous["fingerprint"], current)


def build_form(effective: date, forms_dir: Path = FORMS_DIR, changelog: Path = CHANGELOG,
               current: dict | None = None) -> tuple[str, str] | None:
    """Return (file name, Markdown), or None when nothing changed since the last form."""
    current = current or fingerprint()
    path, previous = last_form(forms_dir)
    entries = changelog_entries(changelog.read_text(encoding="utf-8")) if changelog.exists() else []
    seen = set(previous.get("changelog_headings", [])) if previous else set()
    new_entries = [(h, b) for h, b in entries if h not in seen]
    changes = diff(previous["fingerprint"], current) if previous else []
    if previous and not changes and not new_entries:
        return None
    version = f"weekly-{current['hash']}"
    lines = [f"# Strategy change form draft: {version}", "",
             f"**Effective date:** {effective.isoformat()}", f"**Strategy version:** `{version}`",
             f"**Previous form:** {path.name if path else 'none (first form)'}", "",
             "Recommend-only system: trades are placed by hand in the class game. Edit this draft before submitting.",
             "", "## What changed, why, and the evidence", ""]
    if new_entries:
        for heading, body in new_entries:
            lines += [f"### {heading}", "", body, ""]
    else:
        lines += ["No new changelog entry. Add one to docs/strategy_changelog.md explaining the change below.", ""]
    lines += ["## Configuration and agent changes", ""]
    if previous is None:
        lines.append("First form: this records the full current configuration (hash below).")
    else:
        lines += [f"- {c}" for c in changes] or ["- None (changelog only)."]
    record = {"version": version, "effective_date": effective.isoformat(), "fingerprint": current,
              "changelog_headings": [h for h, _ in entries]}
    lines += ["", MARKER, "```json", json.dumps(record, indent=1, sort_keys=True), "```", ""]
    return f"{effective.isoformat()}_{version}.md", "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--effective-date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)
    result = build_form(args.effective_date)
    if result is None:
        print("No strategy change since the last form. Nothing written.")
        return 0
    name, text = result
    FORMS_DIR.mkdir(parents=True, exist_ok=True)
    (FORMS_DIR / name).write_text(text, encoding="utf-8")
    print(text.split(MARKER)[0])
    print(f"Wrote docs/forms/{name}. Submit the class form, then commit this file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
