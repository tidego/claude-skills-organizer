#!/usr/bin/env python3
"""Hook handler: parse stdin JSON, extract skill name, append to usage-stats.json.

Called by PreToolUse hooks for Skill and Read tools.
Uses file locking to handle concurrent writes safely.
"""

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

STATS_PATH = os.path.expanduser("~/.claude/skills-archive/usage-stats.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_plugin_name_from_path(path: str) -> Optional[str]:
    """Extract plugin name from a plugin cache SKILL.md path.

    Expected format: ~/.claude/plugins/cache/{marketplace}/{plugin-name}/{version}/skills/{skill}/SKILL.md
    Returns 'plugin:{plugin-name}' or None.
    """
    parts = path.split("/")
    try:
        cache_idx = parts.index("cache")
    except ValueError:
        return None
    # plugin-name is at cache_idx + 2: cache/{marketplace}/{plugin-name}/...
    if cache_idx + 2 < len(parts):
        return f"plugin:{parts[cache_idx + 2]}"
    return None


def extract_skill_name(hook_input: dict) -> Optional[str]:
    """Extract skill name from hook input based on tool type."""
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if tool_name == "Skill":
        name = tool_input.get("skill", "")
        # Plugin skill: has ":" in name (e.g., "oh-my-claudecode:autopilot")
        # Extract namespace before ":" and track as plugin:{namespace}
        if ":" in name:
            namespace = name.split(":")[0]
            return f"plugin:{namespace}" if namespace else None
        return name if name else None

    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        if "SKILL.md" not in path:
            return None
        # Check if this is a plugin cache path
        plugins_cache = os.path.expanduser("~/.claude/plugins/cache/")
        if path.startswith(plugins_cache) or "/.claude/plugins/cache/" in path:
            return extract_plugin_name_from_path(path)
        if "/skills/" not in path and "/skills-archive/" not in path:
            return None
        # Extract skill name: parent directory of SKILL.md
        parts = path.split("/")
        try:
            idx = parts.index("SKILL.md")
            return parts[idx - 1] if idx > 0 else None
        except ValueError:
            return None

    return None


def load_stats() -> dict:
    """Load usage stats with file locking."""
    if not os.path.exists(STATS_PATH):
        return {}
    try:
        with open(STATS_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_stats(stats: dict) -> None:
    """Save usage stats with file locking."""
    os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
    with open(STATS_PATH, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(stats, f, indent=2)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return

    skill_name = extract_skill_name(hook_input)
    if not skill_name:
        return

    stats = load_stats()

    if skill_name not in stats:
        stats[skill_name] = {
            "reads": [],
            "first_seen": now_iso(),
            "pinned": False,
        }

    # Keep only last 100 reads to prevent unbounded growth
    stats[skill_name]["reads"].append(now_iso())
    stats[skill_name]["reads"] = stats[skill_name]["reads"][-100:]

    save_stats(stats)


if __name__ == "__main__":
    main()
