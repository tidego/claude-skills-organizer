#!/usr/bin/env python3
"""Core skill organizer: reconcile, rebalance tiers, regenerate index.

Archives skills by toggling `disable-model-invocation: true` in SKILL.md
frontmatter. Archived skills stay in ~/.claude/skills/ — their `/` command
still works, but Claude no longer auto-matches them from descriptions.

Usage:
    python3 organize.py                    # Dry-run (show what would change)
    python3 organize.py --apply            # Execute changes
    python3 organize.py --pin <name>       # Pin skill to T1
    python3 organize.py --unpin <name>     # Unpin skill
    python3 organize.py --promote <name>   # Force promote to T1
    python3 organize.py --demote <name>    # Force demote to T3
    python3 organize.py --rollback         # Undo last organize
    python3 organize.py --stats            # Show usage statistics
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SKILLS_DIR = Path.home() / ".claude" / "skills"
ARCHIVE_DIR = Path.home() / ".claude" / "skills-archive"
SNAPSHOTS_DIR = ARCHIVE_DIR / "snapshots"
STATS_PATH = ARCHIVE_DIR / "usage-stats.json"
CONFIG_PATH = ARCHIVE_DIR / "config.json"
PLUGIN_CACHE_DIR = Path.home() / ".claude" / "plugins" / "cache"
PLUGIN_STATS_PREFIX = "plugin:"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Path to the index SKILL.md (inside this plugin's skills dir)
INDEX_SKILL_PATH = Path(__file__).parent.parent / "skills" / "skills-organize" / "SKILL.md"

# Frontmatter key used to archive skills
ARCHIVE_KEY = "disable-model-invocation"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Frontmatter Parsing ─────────────────────────────────────────────


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md content.

    Returns (frontmatter_dict, body) where body is everything after
    the closing '---'. If no frontmatter, returns ({}, full_text).
    """
    if not text.startswith("---"):
        return {}, text

    # Find closing ---
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_block = text[4:end]  # skip opening "---\n" (or "---")
    body = text[end + 4:]   # skip "\n---"

    # Simple YAML key: value parser (no nested structures needed)
    fm = {}
    for line in fm_block.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r'^([a-zA-Z_-]+)\s*:\s*(.+)$', line)
        if match:
            key = match.group(1)
            val = match.group(2).strip()
            # Parse booleans
            if val.lower() == "true":
                val = True
            elif val.lower() == "false":
                val = False
            # Parse quoted strings
            elif (val.startswith('"') and val.endswith('"')) or \
                 (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            fm[key] = val

    return fm, body


def build_frontmatter(fm: dict) -> str:
    """Serialize a frontmatter dict back to YAML block string."""
    if not fm:
        return ""
    lines = ["---"]
    for key, val in fm.items():
        if isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


def is_skill_archived(skill_dir: Path) -> bool:
    """Check if a skill has disable-model-invocation: true in frontmatter."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return False
    try:
        text = skill_md.read_text()
    except OSError:
        return False
    fm, _ = parse_frontmatter(text)
    return fm.get(ARCHIVE_KEY) is True


def set_skill_archived(skill_dir: Path, archived: bool, dry_run: bool = False) -> bool:
    """Toggle the disable-model-invocation frontmatter flag.

    Returns True if a change was made (or would be made in dry-run).
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return False

    try:
        text = skill_md.read_text()
    except OSError:
        return False

    fm, body = parse_frontmatter(text)
    currently_archived = fm.get(ARCHIVE_KEY) is True

    if currently_archived == archived:
        return False  # No change needed

    if archived:
        fm[ARCHIVE_KEY] = True
    else:
        fm.pop(ARCHIVE_KEY, None)

    if dry_run:
        return True

    # Rebuild file
    new_fm = build_frontmatter(fm)
    if new_fm:
        new_text = new_fm + "\n" + body.lstrip("\n")
    else:
        new_text = body.lstrip("\n")

    skill_md.write_text(new_text)
    return True


# ── Scanning ──────────────────────────────────────────────────────────


def scan_skills(directory: Path) -> dict[str, Path]:
    """Scan a directory for skill folders (containing SKILL.md)."""
    skills = {}
    if not directory.is_dir():
        return skills
    for entry in directory.iterdir():
        if entry.is_dir() and (entry / "SKILL.md").exists():
            skills[entry.name] = entry
    return skills


def scan_plugin_skills() -> dict[str, list[str]]:
    """Scan plugin cache for plugin skill counts (read-only info)."""
    plugins = {}
    if not PLUGIN_CACHE_DIR.is_dir():
        return plugins
    for author_dir in PLUGIN_CACHE_DIR.iterdir():
        if not author_dir.is_dir():
            continue
        for plugin_dir in author_dir.iterdir():
            if not plugin_dir.is_dir():
                continue
            versions = sorted(plugin_dir.iterdir(), key=lambda p: p.name, reverse=True)
            for ver_dir in versions:
                skills_dir = ver_dir / "skills"
                if skills_dir.is_dir():
                    skill_names = [
                        e.name for e in skills_dir.iterdir()
                        if e.is_dir() and (e / "SKILL.md").exists()
                    ]
                    if skill_names:
                        plugin_name = f"{author_dir.name}/{plugin_dir.name}"
                        plugins[plugin_name] = skill_names
                    break
    return plugins


def scan_installed_plugins() -> dict[str, dict]:
    """Scan plugin cache. Returns {plugin:name: {marketplace, plugin_key, skills, skill_dirs}}."""
    plugins = {}
    if not PLUGIN_CACHE_DIR.is_dir():
        return plugins
    for marketplace_dir in PLUGIN_CACHE_DIR.iterdir():
        if not marketplace_dir.is_dir():
            continue
        marketplace = marketplace_dir.name
        for plugin_dir in marketplace_dir.iterdir():
            if not plugin_dir.is_dir():
                continue
            plugin_name = plugin_dir.name
            # Pick latest version dir
            versions = sorted(
                [v for v in plugin_dir.iterdir() if v.is_dir()],
                key=lambda p: p.name,
                reverse=True,
            )
            for ver_dir in versions:
                skills_dir = ver_dir / "skills"
                if skills_dir.is_dir():
                    skill_names = []
                    skill_dirs = {}
                    for e in skills_dir.iterdir():
                        if e.is_dir() and (e / "SKILL.md").exists():
                            skill_names.append(e.name)
                            skill_dirs[e.name] = e
                    if skill_names:
                        stats_key = f"{PLUGIN_STATS_PREFIX}{plugin_name}"
                        plugins[stats_key] = {
                            "marketplace": marketplace,
                            "plugin_key": f"{plugin_name}@{marketplace}",
                            "skills": skill_names,
                            "skill_dirs": skill_dirs,
                        }
                    break
    return plugins


def get_plugin_description(plugin_info: dict) -> str:
    """Get a combined description from the plugin's skills."""
    skill_dirs = plugin_info.get("skill_dirs", {})
    descriptions = []
    for skill_name, skill_dir in sorted(skill_dirs.items()):
        desc = get_skill_description(skill_dir)
        if desc:
            descriptions.append(desc)
    if not descriptions:
        return f"Plugin with {len(plugin_info.get('skills', []))} skills"
    if len(descriptions) == 1:
        return descriptions[0]
    # Combine: use first description, note how many skills total
    return f"{descriptions[0]} (+{len(descriptions) - 1} more skills)"


def get_skill_description(skill_dir: Path) -> str:
    """Extract first meaningful line from SKILL.md as description."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ""
    try:
        text = skill_md.read_text()
    except OSError:
        return ""
    # Skip frontmatter
    _, body = parse_frontmatter(text)
    for line in body.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        return line[:100]
    return ""


def get_current_tier(name: str) -> int:
    """Determine current tier of a skill.

    T1 = in SKILLS_DIR and NOT archived
    T2/T3 = in SKILLS_DIR and archived (tier stored in stats)
    0 = not found
    """
    skill_dir = SKILLS_DIR / name
    if not (skill_dir / "SKILL.md").exists():
        return 0
    if is_skill_archived(skill_dir):
        return 2  # Default archived tier; actual T2/T3 distinction from stats
    return 1


# ── Reconciliation ───────────────────────────────────────────────────


def reconcile(stats: dict, config: dict) -> dict:
    """Sync stats with actual filesystem state (skills and plugins)."""
    all_skills = scan_skills(SKILLS_DIR)
    installed_plugins = scan_installed_plugins()

    # Build set of all valid names (skills + plugins)
    all_valid = set(all_skills.keys()) | set(installed_plugins.keys())

    # Remove stats for deleted skills/plugins
    removed = []
    for name in list(stats.keys()):
        if name not in all_valid:
            del stats[name]
            removed.append(name)

    # Add new skills with grace period
    added = []
    for name in all_skills:
        if name not in stats:
            stats[name] = {
                "reads": [],
                "first_seen": now_iso(),
                "pinned": False,
            }
            added.append(name)

    # Add new plugins with grace period
    added_plugins = []
    for name in installed_plugins:
        if name not in stats:
            stats[name] = {
                "reads": [],
                "first_seen": now_iso(),
                "pinned": False,
            }
            added_plugins.append(name)

    # Sync pinned status from config
    pinned_list = config.get("pinned", [])
    for name in stats:
        stats[name]["pinned"] = name in pinned_list

    if removed:
        print(f"  Reconcile: removed {len(removed)} orphaned entries: {', '.join(removed)}")
    if added:
        print(f"  Reconcile: added {len(added)} new skills: {', '.join(added)}")
    if added_plugins:
        print(f"  Reconcile: added {len(added_plugins)} new plugins: {', '.join(added_plugins)}")

    return stats


# ── Tier Calculation ─────────────────────────────────────────────────


def count_reads_in_window(reads: list[str], days: int = 15) -> int:
    """Count reads within the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    count = 0
    for ts in reads:
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                count += 1
        except (ValueError, TypeError):
            continue
    return count


def is_in_grace_period(stats_entry: dict, grace_days: int) -> bool:
    """Check if a skill is still in its grace period."""
    first_seen = stats_entry.get("first_seen")
    if not first_seen:
        return False
    try:
        dt = datetime.fromisoformat(first_seen)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt < timedelta(days=grace_days)
    except (ValueError, TypeError):
        return False


def calculate_tiers(stats: dict, config: dict) -> dict[str, int]:
    """Calculate target tier for each skill. Returns {name: tier_number}."""
    thresholds = config.get("thresholds", {})
    t1_min = thresholds.get("t1_min_reads", 3)
    t2_min = thresholds.get("t2_min_reads", 1)
    grace_days = thresholds.get("grace_period_days", 7)

    tiers = {}
    for name, entry in stats.items():
        if entry.get("pinned"):
            tiers[name] = 1
            continue

        if is_in_grace_period(entry, grace_days):
            tiers[name] = 1
            continue

        reads = count_reads_in_window(entry.get("reads", []), days=thresholds.get("window_days", 15))
        if reads >= t1_min:
            tiers[name] = 1
        elif reads >= t2_min:
            tiers[name] = 2
        else:
            tiers[name] = 3

    return tiers


# ── Index Generation ─────────────────────────────────────────────────


def generate_index(stats: dict, config: dict, tiers: dict,
                    installed_plugins: dict | None = None,
                    window_days: int = 15) -> str:
    """Generate the always-loaded T1 index SKILL.md content.

    Hierarchical: T1 index only lists T2 skills and T2 plugins.
    T3 skills/plugins are discovered via the T2 index file.
    """
    t2_skills: list[tuple[str, int, str]] = []
    t2_plugins: list[tuple[str, int, str]] = []
    for name, tier in tiers.items():
        if tier == 2:
            if name.startswith(PLUGIN_STATS_PREFIX):
                plugin_name = name[len(PLUGIN_STATS_PREFIX):]
                desc = ""
                if installed_plugins and name in installed_plugins:
                    desc = get_plugin_description(installed_plugins[name])
                reads = count_reads_in_window(stats.get(name, {}).get("reads", []), window_days)
                t2_plugins.append((plugin_name, reads, desc))
            else:
                skill_dir = SKILLS_DIR / name
                desc = get_skill_description(skill_dir)
                reads = count_reads_in_window(stats.get(name, {}).get("reads", []), window_days)
                t2_skills.append((name, reads, desc))

    t2_skills.sort(key=lambda x: (-x[1], x[0]))
    t2_plugins.sort(key=lambda x: (-x[1], x[0]))

    t1_count = sum(1 for t in tiers.values() if t == 1)
    t2_count = sum(1 for t in tiers.values() if t == 2)
    t3_count = sum(1 for t in tiers.values() if t == 3)

    lines = [
        "# Skills Organizer Index",
        "",
        f"Active: {t1_count} | T2 (warm): {t2_count} | T3 (cold): {t3_count}",
        "",
        "Archived skills have `disable-model-invocation: true` — their `/` command",
        "still works but Claude won't auto-match them. To use one, invoke it directly:",
        "",
        "```",
        "/skill-name",
        "```",
        "",
    ]

    if t2_skills:
        lines.append("## T2 Skills (warm archive)")
        lines.append("")
        lines.append("| Skill | Reads | Description |")
        lines.append("|-------|------:|-------------|")
        for name, reads, desc in t2_skills:
            lines.append(f"| {name} | {reads} | {desc[:60]} |")
        lines.append("")
    else:
        lines.append("No T2 skills. All active or cold-archived.")
        lines.append("")

    if t2_plugins:
        lines.append("## T2 Plugins (warm archive)")
        lines.append("")
        lines.append("| Plugin | Reads | Description |")
        lines.append("|--------|------:|-------------|")
        for name, reads, desc in t2_plugins:
            lines.append(f"| {name} (plugin) | {reads} | {desc[:60]} |")
        lines.append("")

    if t3_count > 0:
        lines.append(f"## T3 Skills ({t3_count} cold-archived)")
        lines.append("")
        lines.append("T3 skills are not listed here. To browse them:")
        lines.append("")
        lines.append("```")
        lines.append("Read ~/.claude/skills-archive/t2-index.md")
        lines.append("```")
        lines.append("")

    lines.append("Run `/skills-organize` to rebalance tiers.")
    lines.append("")

    return "\n".join(lines)


T2_INDEX_PATH = ARCHIVE_DIR / "t2-index.md"


def generate_t2_index(stats: dict, tiers: dict,
                      installed_plugins: dict | None = None,
                      window_days: int = 15) -> str:
    """Generate the T2 index file that lists T3 (cold) skills and plugins.

    Stored at ~/.claude/skills-archive/t2-index.md.
    Claude reads this on demand when it needs a cold skill.
    """
    t3_skills: list[tuple[str, int, str]] = []
    t3_plugins: list[tuple[str, int, str]] = []
    for name, tier in tiers.items():
        if tier == 3:
            if name.startswith(PLUGIN_STATS_PREFIX):
                plugin_name = name[len(PLUGIN_STATS_PREFIX):]
                desc = ""
                if installed_plugins and name in installed_plugins:
                    desc = get_plugin_description(installed_plugins[name])
                reads = count_reads_in_window(stats.get(name, {}).get("reads", []), window_days)
                t3_plugins.append((plugin_name, reads, desc))
            else:
                skill_dir = SKILLS_DIR / name
                desc = get_skill_description(skill_dir)
                reads = count_reads_in_window(stats.get(name, {}).get("reads", []), window_days)
                t3_skills.append((name, reads, desc))

    t3_skills.sort(key=lambda x: (-x[1], x[0]))
    t3_plugins.sort(key=lambda x: (-x[1], x[0]))

    lines = [
        "# T3 Cold Archive Index",
        "",
        "Skills with no recent usage. To use one, invoke it directly:",
        "",
        "```",
        "/skill-name",
        "```",
        "",
    ]

    if t3_skills:
        lines.append("| Skill | Reads | Description |")
        lines.append("|-------|------:|-------------|")
        for name, reads, desc in t3_skills:
            lines.append(f"| {name} | {reads} | {desc[:60]} |")
        lines.append("")
    else:
        lines.append("No T3 skills currently.")
        lines.append("")

    if t3_plugins:
        lines.append("## T3 Plugins (cold archive)")
        lines.append("")
        lines.append("| Plugin | Reads | Description |")
        lines.append("|--------|------:|-------------|")
        for name, reads, desc in t3_plugins:
            lines.append(f"| {name} (plugin) | {reads} | {desc[:60]} |")
        lines.append("")

    return "\n".join(lines)


# ── Actions ──────────────────────────────────────────────────────────


def apply_tier_change(name: str, current_tier: int, target_tier: int,
                      dry_run: bool) -> bool:
    """Apply a tier change by toggling frontmatter archive flag.

    T1 = active (no flag), T2/T3 = archived (flag set).
    """
    skill_dir = SKILLS_DIR / name
    if not (skill_dir / "SKILL.md").exists():
        print(f"  WARNING: {name} not found at {skill_dir}")
        return False

    should_archive = target_tier >= 2

    if dry_run:
        direction = "archive" if should_archive else "activate"
        print(f"  [DRY-RUN] Would {direction} {name}: T{current_tier} -> T{target_tier}")
        return True

    changed = set_skill_archived(skill_dir, should_archive)
    if changed:
        direction = "Archived" if should_archive else "Activated"
        print(f"  {direction} {name}: T{current_tier} -> T{target_tier}")
    return changed


def set_plugin_enabled(plugin_key: str, enabled: bool, dry_run: bool = False) -> bool:
    """Toggle plugin in settings.json enabledPlugins.

    Returns True if a change was made (or would be made in dry-run).
    plugin_key format: "{plugin-name}@{marketplace}"
    """
    settings = load_json(SETTINGS_PATH)
    enabled_plugins = settings.get("enabledPlugins", {})

    # Currently enabled if key is absent or explicitly True
    currently_enabled = enabled_plugins.get(plugin_key, True)

    if currently_enabled == enabled:
        return False  # No change needed

    if dry_run:
        return True

    if enabled:
        # Remove the explicit False (absent = enabled)
        enabled_plugins.pop(plugin_key, None)
    else:
        enabled_plugins[plugin_key] = False

    settings["enabledPlugins"] = enabled_plugins
    save_json(SETTINGS_PATH, settings)
    return True


def apply_plugin_tier_change(name: str, plugin_key: str, current_tier: int,
                             target_tier: int, dry_run: bool) -> bool:
    """Apply a tier change for a plugin by toggling enabledPlugins in settings.json.

    T1 = enabled, T2/T3 = disabled.
    """
    should_disable = target_tier >= 2

    if dry_run:
        direction = "disable" if should_disable else "enable"
        plugin_display = name[len(PLUGIN_STATS_PREFIX):]
        print(f"  [DRY-RUN] Would {direction} plugin {plugin_display}: T{current_tier} -> T{target_tier}")
        return True

    changed = set_plugin_enabled(plugin_key, enabled=not should_disable)
    if changed:
        direction = "Disabled" if should_disable else "Enabled"
        plugin_display = name[len(PLUGIN_STATS_PREFIX):]
        print(f"  {direction} plugin {plugin_display}: T{current_tier} -> T{target_tier}")
    return changed


def is_plugin_disabled(plugin_key: str) -> bool:
    """Check if a plugin is explicitly disabled in settings.json."""
    settings = load_json(SETTINGS_PATH)
    enabled_plugins = settings.get("enabledPlugins", {})
    return enabled_plugins.get(plugin_key, True) is False


def save_snapshot(tiers_before: dict[str, int]) -> str:
    """Save a pre-change snapshot for rollback."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = SNAPSHOTS_DIR / f"{ts}.json"
    save_json(path, {"timestamp": now_iso(), "tiers": tiers_before})
    return str(path)


def save_snapshot_with_plugins(tiers_before: dict[str, int],
                                plugin_states: dict[str, bool]) -> str:
    """Save a pre-change snapshot including plugin states for rollback."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = SNAPSHOTS_DIR / f"{ts}.json"
    save_json(path, {
        "timestamp": now_iso(),
        "tiers": tiers_before,
        "plugin_states": plugin_states,
    })
    return str(path)


def rollback() -> None:
    """Undo the last organize by restoring skills and plugins to previous tiers."""
    if not SNAPSHOTS_DIR.is_dir():
        print("No snapshots found.")
        return

    snapshots = sorted(SNAPSHOTS_DIR.glob("*.json"), reverse=True)
    if not snapshots:
        print("No snapshots found.")
        return

    latest = snapshots[0]
    data = load_json(latest)
    prev_tiers = data.get("tiers", {})
    prev_plugin_states = data.get("plugin_states", {})

    print(f"Rolling back to snapshot: {latest.name}")
    print(f"  Timestamp: {data.get('timestamp', 'unknown')}")

    restored = 0

    # Restore skills
    for name, target_tier in prev_tiers.items():
        if name.startswith(PLUGIN_STATS_PREFIX):
            continue  # Handled via plugin_states below
        skill_dir = SKILLS_DIR / name
        if not (skill_dir / "SKILL.md").exists():
            continue

        should_archive = target_tier >= 2
        currently_archived = is_skill_archived(skill_dir)

        if currently_archived != should_archive:
            set_skill_archived(skill_dir, should_archive)
            state = "archived" if should_archive else "activated"
            print(f"  Restored {name} -> T{target_tier} ({state})")
            restored += 1

    # Restore plugin enabled states
    for plugin_key, was_enabled in prev_plugin_states.items():
        currently_disabled = is_plugin_disabled(plugin_key)
        currently_enabled = not currently_disabled

        if currently_enabled != was_enabled:
            set_plugin_enabled(plugin_key, was_enabled)
            state = "enabled" if was_enabled else "disabled"
            print(f"  Restored plugin {plugin_key} ({state})")
            restored += 1

    print(f"\n  Rolled back {restored} entries.")
    latest.unlink()


def show_stats(stats: dict, window_days: int = 15) -> None:
    """Display usage statistics for skills and plugins."""
    installed_plugins = scan_installed_plugins()

    print(f"\n=== Skill Usage Statistics (last {window_days} days) ===\n")

    skill_entries = []
    plugin_entries = []
    for name, entry in stats.items():
        reads_90d = count_reads_in_window(entry.get("reads", []), window_days)
        pinned = "PIN" if entry.get("pinned") else ""
        grace = "NEW" if is_in_grace_period(entry, 7) else ""

        if name.startswith(PLUGIN_STATS_PREFIX):
            plugin_info = installed_plugins.get(name, {})
            plugin_key = plugin_info.get("plugin_key", "")
            disabled = is_plugin_disabled(plugin_key) if plugin_key else False
            tier = "T2+" if disabled else "T1"
            display_name = name[len(PLUGIN_STATS_PREFIX):] + " (plugin)"
            plugin_entries.append((reads_90d, display_name, tier, pinned, grace))
        else:
            skill_dir = SKILLS_DIR / name
            archived = is_skill_archived(skill_dir)
            tier = "T2+" if archived else "T1"
            skill_entries.append((reads_90d, name, tier, pinned, grace))

    skill_entries.sort(key=lambda x: (-x[0], x[1]))
    plugin_entries.sort(key=lambda x: (-x[0], x[1]))

    all_entries = skill_entries + plugin_entries

    print(f"  {'Name':<40} {'Tier':>4} {'Reads':>5} {'Status':>6}")
    print(f"  {'-'*40} {'-'*4} {'-'*5} {'-'*6}")
    for reads, name, tier, pinned, grace in all_entries:
        status = pinned or grace or ""
        print(f"  {name:<40} {tier:>4} {reads:>5} {status:>6}")

    print(f"\n  Total managed: {len(all_entries)} ({len(skill_entries)} skills, {len(plugin_entries)} plugins)")


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Organize skills into tiers")
    parser.add_argument("--apply", action="store_true", help="Execute changes (default: dry-run)")
    parser.add_argument("--pin", metavar="NAME", help="Pin a skill to T1")
    parser.add_argument("--unpin", metavar="NAME", help="Unpin a skill")
    parser.add_argument("--promote", metavar="NAME", help="Force promote to T1")
    parser.add_argument("--demote", metavar="NAME", help="Force demote to T3")
    parser.add_argument("--rollback", action="store_true", help="Undo last organize")
    parser.add_argument("--stats", action="store_true", help="Show usage statistics")
    parser.add_argument("--clean", action="store_true", help="Delete all T2/T3 skills and disable T2/T3 plugins")
    parser.add_argument("--window", type=int, default=15, metavar="DAYS", help="Usage window in days (default: 15)")
    args = parser.parse_args()

    # Apply window override to config thresholds
    window_days = args.window

    # Ensure archive dir exists
    for d in [ARCHIVE_DIR, SNAPSHOTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # Handle rollback
    if args.rollback:
        rollback()
        return

    # Handle clean (delete T2/T3 skills, disable T2/T3 plugins)
    if args.clean:
        stats = load_json(STATS_PATH)
        config = load_json(CONFIG_PATH)
        config.setdefault("thresholds", {})["window_days"] = window_days
        stats = reconcile(stats, config)
        target_tiers = calculate_tiers(stats, config)
        installed_plugins = scan_installed_plugins()

        skills_to_delete = []
        plugins_to_disable = []
        for name, tier in target_tiers.items():
            if tier >= 2:
                if name.startswith(PLUGIN_STATS_PREFIX):
                    plugin_info = installed_plugins.get(name, {})
                    plugin_key = plugin_info.get("plugin_key", "")
                    if plugin_key:
                        plugins_to_disable.append((name, plugin_key))
                else:
                    skill_dir = SKILLS_DIR / name
                    if skill_dir.is_dir():
                        skills_to_delete.append(name)

        if not skills_to_delete and not plugins_to_disable:
            print("Nothing to clean. All entries are T1.")
            save_json(STATS_PATH, stats)
            return

        print("=== Clean: remove non-T1 entries ===\n")
        if skills_to_delete:
            print(f"  Skills to DELETE ({len(skills_to_delete)}):")
            for name in sorted(skills_to_delete):
                print(f"    - {name} (T{target_tiers[name]})")
        if plugins_to_disable:
            print(f"  Plugins to DISABLE ({len(plugins_to_disable)}):")
            for name, _ in sorted(plugins_to_disable):
                display = name[len(PLUGIN_STATS_PREFIX):]
                print(f"    - {display} (T{target_tiers[name]})")

        if not args.apply:
            print(f"\n  Run with --clean --apply to execute.")
            save_json(STATS_PATH, stats)
            return

        import shutil
        deleted = 0
        disabled = 0

        for name in skills_to_delete:
            skill_dir = SKILLS_DIR / name
            shutil.rmtree(skill_dir)
            # Remove from stats
            stats.pop(name, None)
            deleted += 1
            print(f"  Deleted: {skill_dir}")

        for name, plugin_key in plugins_to_disable:
            set_plugin_enabled(plugin_key, False)
            disabled += 1
            display = name[len(PLUGIN_STATS_PREFIX):]
            print(f"  Disabled plugin: {display}")

        save_json(STATS_PATH, stats)
        print(f"\n  Cleaned: {deleted} skills deleted, {disabled} plugins disabled.")
        return

    # Load data
    stats = load_json(STATS_PATH)
    config = load_json(CONFIG_PATH)
    config.setdefault("thresholds", {})["window_days"] = window_days

    # Handle pin/unpin
    if args.pin:
        pinned = config.setdefault("pinned", [])
        if args.pin not in pinned:
            pinned.append(args.pin)
            save_json(CONFIG_PATH, config)
            print(f"Pinned: {args.pin}")
        else:
            print(f"Already pinned: {args.pin}")
        return

    if args.unpin:
        pinned = config.get("pinned", [])
        if args.unpin in pinned:
            pinned.remove(args.unpin)
            config["pinned"] = pinned
            save_json(CONFIG_PATH, config)
            print(f"Unpinned: {args.unpin}")
        else:
            print(f"Not pinned: {args.unpin}")
        return

    # Reconcile
    print("=== Skills Organizer ===\n")
    stats = reconcile(stats, config)

    # Handle stats display
    if args.stats:
        show_stats(stats, window_days)
        save_json(STATS_PATH, stats)
        return

    # Handle force promote/demote
    if args.promote:
        promote_name = args.promote
        if promote_name.startswith(PLUGIN_STATS_PREFIX):
            # Plugin promote
            plugins = scan_installed_plugins()
            if promote_name not in plugins:
                print(f"Plugin not found: {promote_name}")
                return
            plugin_key = plugins[promote_name]["plugin_key"]
            if not is_plugin_disabled(plugin_key):
                print(f"Already active (T1): {promote_name}")
                return
            if args.apply:
                set_plugin_enabled(plugin_key, True)
                print(f"Promoted plugin to T1: {promote_name}")
            else:
                print(f"[DRY-RUN] Would promote plugin to T1: {promote_name}")
        else:
            # Skill promote
            skill_dir = SKILLS_DIR / promote_name
            if not (skill_dir / "SKILL.md").exists():
                print(f"Skill not found: {promote_name}")
                return
            if not is_skill_archived(skill_dir):
                print(f"Already active (T1): {promote_name}")
                return
            if args.apply:
                set_skill_archived(skill_dir, False)
                print(f"Promoted to T1: {promote_name}")
            else:
                print(f"[DRY-RUN] Would promote to T1: {promote_name}")
        save_json(STATS_PATH, stats)
        return

    if args.demote:
        demote_name = args.demote
        if demote_name.startswith(PLUGIN_STATS_PREFIX):
            # Plugin demote
            plugins = scan_installed_plugins()
            if demote_name not in plugins:
                print(f"Plugin not found: {demote_name}")
                return
            plugin_key = plugins[demote_name]["plugin_key"]
            if is_plugin_disabled(plugin_key):
                print(f"Already disabled: {demote_name}")
                return
            if args.apply:
                set_plugin_enabled(plugin_key, False)
                print(f"Demoted (disabled) plugin: {demote_name}")
            else:
                print(f"[DRY-RUN] Would demote (disable) plugin: {demote_name}")
        else:
            # Skill demote
            skill_dir = SKILLS_DIR / demote_name
            if not (skill_dir / "SKILL.md").exists():
                print(f"Skill not found: {demote_name}")
                return
            if is_skill_archived(skill_dir):
                print(f"Already archived: {demote_name}")
                return
            if args.apply:
                set_skill_archived(skill_dir, True)
                print(f"Demoted (archived): {demote_name}")
            else:
                print(f"[DRY-RUN] Would demote (archive): {demote_name}")
        save_json(STATS_PATH, stats)
        return

    # Calculate target tiers
    target_tiers = calculate_tiers(stats, config)

    # Scan installed plugins for metadata
    installed_plugins = scan_installed_plugins()

    # Determine changes needed
    skill_changes = []
    plugin_changes = []
    for name, target in target_tiers.items():
        if name.startswith(PLUGIN_STATS_PREFIX):
            # Plugin: check enabled/disabled state
            plugin_info = installed_plugins.get(name, {})
            plugin_key = plugin_info.get("plugin_key", "")
            if not plugin_key:
                continue
            currently_disabled = is_plugin_disabled(plugin_key)
            should_disable = target >= 2
            if currently_disabled != should_disable:
                current_tier = 2 if currently_disabled else 1
                plugin_changes.append((name, plugin_key, current_tier, target))
        else:
            # Skill: check archived state
            skill_dir = SKILLS_DIR / name
            if not (skill_dir / "SKILL.md").exists():
                continue
            currently_archived = is_skill_archived(skill_dir)
            should_archive = target >= 2
            if currently_archived != should_archive:
                current_tier = 2 if currently_archived else 1
                skill_changes.append((name, current_tier, target))

    changes = skill_changes + [(n, f, t) for n, _, f, t in plugin_changes]

    # Report current state
    skill_count = sum(1 for n in target_tiers if not n.startswith(PLUGIN_STATS_PREFIX))
    plugin_count = sum(1 for n in target_tiers if n.startswith(PLUGIN_STATS_PREFIX))
    t1_count = sum(1 for t in target_tiers.values() if t == 1)
    t2_count = sum(1 for t in target_tiers.values() if t == 2)
    t3_count = sum(1 for t in target_tiers.values() if t == 3)

    print(f"  Managed: {len(target_tiers)} ({skill_count} skills, {plugin_count} plugins)")
    print(f"    T1 (active):       {t1_count}")
    print(f"    T2 (warm archive): {t2_count}")
    print(f"    T3 (cold archive): {t3_count}")

    # Show changes
    if not changes:
        print("\n  No tier changes needed.")
    else:
        print(f"\n  Changes {'(DRY-RUN)' if not args.apply else ''}:")
        # Save snapshot before applying
        if args.apply:
            # Record current state for skills (archive = T2, active = T1)
            current_tiers = {}
            for name in target_tiers:
                if name.startswith(PLUGIN_STATS_PREFIX):
                    continue
                skill_dir = SKILLS_DIR / name
                if (skill_dir / "SKILL.md").exists():
                    current_tiers[name] = 2 if is_skill_archived(skill_dir) else 1

            # Record current plugin enabled states
            plugin_states = {}
            for name in target_tiers:
                if name.startswith(PLUGIN_STATS_PREFIX):
                    plugin_info = installed_plugins.get(name, {})
                    plugin_key = plugin_info.get("plugin_key", "")
                    if plugin_key:
                        plugin_states[plugin_key] = not is_plugin_disabled(plugin_key)

            snapshot_path = save_snapshot_with_plugins(current_tiers, plugin_states)
            print(f"  Snapshot saved: {snapshot_path}")

        # Categorize all changes
        activations = [(n, f, t) for n, f, t in skill_changes if t == 1]
        archivings = [(n, f, t) for n, f, t in skill_changes if t >= 2]
        plugin_activations = [(n, k, f, t) for n, k, f, t in plugin_changes if t == 1]
        plugin_archivings = [(n, k, f, t) for n, k, f, t in plugin_changes if t >= 2]

        if activations:
            print(f"\n  Skill Activations ({len(activations)}):")
            for name, from_t, to_t in activations:
                apply_tier_change(name, from_t, to_t, dry_run=not args.apply)

        if archivings:
            print(f"\n  Skill Archivings ({len(archivings)}):")
            for name, from_t, to_t in archivings:
                apply_tier_change(name, from_t, to_t, dry_run=not args.apply)

        if plugin_activations:
            print(f"\n  Plugin Activations ({len(plugin_activations)}):")
            for name, plugin_key, from_t, to_t in plugin_activations:
                apply_plugin_tier_change(name, plugin_key, from_t, to_t, dry_run=not args.apply)

        if plugin_archivings:
            print(f"\n  Plugin Archivings ({len(plugin_archivings)}):")
            for name, plugin_key, from_t, to_t in plugin_archivings:
                apply_plugin_tier_change(name, plugin_key, from_t, to_t, dry_run=not args.apply)

    # Regenerate indexes
    if args.apply:
        # T1 index (always loaded, lists T2 + pointer to T3)
        index_content = generate_index(stats, config, target_tiers, installed_plugins, window_days)
        INDEX_SKILL_PATH.parent.mkdir(parents=True, exist_ok=True)
        INDEX_SKILL_PATH.write_text(index_content)
        print(f"\n  T1 index regenerated: {INDEX_SKILL_PATH}")

        # T2 index (on demand, lists T3 cold skills/plugins)
        t2_index_content = generate_t2_index(stats, target_tiers, installed_plugins, window_days)
        T2_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        T2_INDEX_PATH.write_text(t2_index_content)
        print(f"  T2 index regenerated: {T2_INDEX_PATH}")

    # Save updated stats
    save_json(STATS_PATH, stats)

    if not args.apply and changes:
        print(f"\n  Run with --apply to execute these changes.")


if __name__ == "__main__":
    main()
