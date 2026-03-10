#!/usr/bin/env python3
"""Hook handler for UserPromptSubmit: track manual /skill-name invocations.

When a user types /skill-name, this records a read for that skill.
Plugin skills like /plugin-name:skill are aggregated by plugin.
"""

import json
import os
import sys
from pathlib import Path

# Reuse track.py's save/load logic
sys.path.insert(0, str(Path(__file__).parent))
from track import load_stats, save_stats, now_iso

SKILLS_DIR = Path.home() / ".claude" / "skills"
PLUGIN_CACHE_DIR = Path.home() / ".claude" / "plugins" / "cache"


def get_known_skills() -> set[str]:
    """Get all known user skill names."""
    skills = set()
    if SKILLS_DIR.is_dir():
        for entry in SKILLS_DIR.iterdir():
            if entry.is_dir() and (entry / "SKILL.md").exists():
                skills.add(entry.name)
    return skills


def get_known_plugins() -> set[str]:
    """Get all known plugin namespaces."""
    plugins = set()
    if PLUGIN_CACHE_DIR.is_dir():
        for marketplace in PLUGIN_CACHE_DIR.iterdir():
            if marketplace.is_dir():
                for plugin in marketplace.iterdir():
                    if plugin.is_dir():
                        plugins.add(plugin.name)
    return plugins


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return

    # Extract prompt text
    prompt = hook_input.get("prompt", "").strip()
    if not prompt.startswith("/"):
        return

    # Extract skill name from /skill-name or /plugin:skill-name
    # Remove the leading /
    command = prompt[1:].split()[0] if prompt[1:].split() else ""
    if not command:
        return

    # Skip our own command
    if command.startswith("skills-organize"):
        return

    # Check for plugin skill: /plugin-name:skill-name
    if ":" in command:
        namespace = command.split(":")[0]
        known_plugins = get_known_plugins()
        if namespace in known_plugins:
            skill_name = f"plugin:{namespace}"
        else:
            return
    else:
        # Check if it's a known user skill
        known_skills = get_known_skills()
        if command not in known_skills:
            return
        skill_name = command

    stats = load_stats()

    if skill_name not in stats:
        stats[skill_name] = {
            "reads": [],
            "first_seen": now_iso(),
            "pinned": False,
        }

    stats[skill_name]["reads"].append(now_iso())
    stats[skill_name]["reads"] = stats[skill_name]["reads"][-100:]

    save_stats(stats)


if __name__ == "__main__":
    main()
