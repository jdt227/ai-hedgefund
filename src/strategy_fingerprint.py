"""A fingerprint of everything that can change a decision.

It covers every public setting in config/settings.py, the universe snapshot's
tickers and sectors, and every .claude/agents/*.md file. Agent prompt edits count
as strategy changes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from config import settings

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / ".claude" / "agents"


def _jsonable(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def settings_snapshot() -> dict:
    """Every uppercase setting that can be written as JSON."""
    snapshot = {}
    for name in sorted(dir(settings)):
        if not name.isupper():
            continue
        value = _jsonable(getattr(settings, name))
        try:
            json.dumps(value)
        except TypeError:
            continue
        snapshot[name] = value
    return snapshot


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def universe_hash() -> str:
    from src.universe import SP500_PATH, load_sp500

    if not SP500_PATH.exists():
        return "missing"
    frame = load_sp500()
    return _digest("\n".join(f"{t},{s}" for t, s in zip(frame["ticker"], frame["sector"])).encode())


def agent_hashes(agents_dir: Path = AGENTS_DIR) -> dict[str, str]:
    if not agents_dir.exists():
        return {}
    return {p.name: _digest(p.read_bytes()) for p in sorted(agents_dir.glob("*.md"))}


def fingerprint(agents_dir: Path = AGENTS_DIR) -> dict:
    """Settings, universe, and agent hashes, plus one combined hash."""
    parts = {"settings": settings_snapshot(), "universe": universe_hash(), "agents": agent_hashes(agents_dir)}
    parts["hash"] = _digest(json.dumps(parts, sort_keys=True).encode())
    return parts


def diff(old: dict, new: dict) -> list[str]:
    """Readable differences between two fingerprints."""
    changes = []
    old_settings, new_settings = old.get("settings", {}), new.get("settings", {})
    for key in sorted(set(old_settings) | set(new_settings)):
        if old_settings.get(key) != new_settings.get(key):
            before, after = json.dumps(old_settings.get(key)), json.dumps(new_settings.get(key))
            if len(before) + len(after) > 300:
                changes.append(f"setting {key} changed")
            else:
                changes.append(f"setting {key}: {before} -> {after}")
    if old.get("universe") != new.get("universe"):
        changes.append("universe snapshot changed (tickers or sectors)")
    old_agents, new_agents = old.get("agents", {}), new.get("agents", {})
    for name in sorted(set(old_agents) | set(new_agents)):
        if name not in old_agents:
            changes.append(f"agent prompt added: {name}")
        elif name not in new_agents:
            changes.append(f"agent prompt removed: {name}")
        elif old_agents[name] != new_agents[name]:
            changes.append(f"agent prompt edited: {name}")
    return changes
