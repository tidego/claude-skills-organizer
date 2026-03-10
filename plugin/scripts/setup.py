#!/usr/bin/env python3
"""Setup: create archive directory and initialize config.

Run automatically via SessionStart hook on every Claude Code session.
Skips quickly if already initialized.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILLS_DIR = Path.home() / ".claude" / "skills"
ARCHIVE_DIR = Path.home() / ".claude" / "skills-archive"
SNAPSHOTS_DIR = ARCHIVE_DIR / "snapshots"
STATS_PATH = ARCHIVE_DIR / "usage-stats.json"
CONFIG_PATH = ARCHIVE_DIR / "config.json"

DEFAULT_CONFIG = {
    "pinned": [],
    "thresholds": {
        "t1_min_reads": 3,
        "t2_min_reads": 1,
        "window_days": 15,
        "grace_period_days": 7,
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def scan_user_skills() -> set[str]:
    """Scan ~/.claude/skills/ for user-installed skills."""
    skills = set()
    if not SKILLS_DIR.is_dir():
        return skills
    for entry in SKILLS_DIR.iterdir():
        if entry.is_dir() and (entry / "SKILL.md").exists():
            skills.add(entry.name)
    return skills


def main() -> None:
    # Quick check: if both config and stats exist, already initialized
    if CONFIG_PATH.exists() and STATS_PATH.exists():
        return

    # Create directories
    for d in [ARCHIVE_DIR, SNAPSHOTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # Initialize config if not exists
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)

    # Initialize usage stats if not exists
    if not STATS_PATH.exists():
        user_skills = scan_user_skills()
        stats = {}
        for name in user_skills:
            stats[name] = {
                "reads": [],
                "first_seen": now_iso(),
                "pinned": False,
            }
        with open(STATS_PATH, "w") as f:
            json.dump(stats, f, indent=2)

    # Generate initial indexes
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from organize import (
            INDEX_SKILL_PATH,
            T2_INDEX_PATH,
            calculate_tiers,
            generate_index,
            generate_t2_index,
            load_json,
        )

        stats = load_json(STATS_PATH)
        config = load_json(CONFIG_PATH)
        tiers = calculate_tiers(stats, config)

        index_content = generate_index(stats, config, tiers)
        INDEX_SKILL_PATH.parent.mkdir(parents=True, exist_ok=True)
        INDEX_SKILL_PATH.write_text(index_content)

        t2_index_content = generate_t2_index(stats, tiers)
        T2_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        T2_INDEX_PATH.write_text(t2_index_content)
    except Exception:
        pass  # Non-fatal: user can run /cso:skills-organize --apply manually


if __name__ == "__main__":
    main()
