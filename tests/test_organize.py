#!/usr/bin/env python3
"""Unit tests for claude-skills-organizer (frontmatter-based approach).

Tests cover:
- Frontmatter parsing and modification
- Tier calculation (T1/T2/T3 based on read counts)
- Grace period for new skills
- Pin mechanism
- Reconciliation (add new, remove deleted)
- Archive/activate via frontmatter toggle
- Index generation
- Track script (skill name extraction)
- Rollback snapshots
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Add scripts dir to path
sys.path.insert(0, str(Path(__file__).parent.parent / "plugin" / "scripts"))

from organize import (
    apply_tier_change,
    apply_plugin_tier_change,
    build_frontmatter,
    calculate_tiers,
    count_reads_in_window,
    generate_index,
    generate_t2_index,
    get_current_tier,
    get_skill_description,
    is_in_grace_period,
    is_skill_archived,
    parse_frontmatter,
    reconcile,
    save_snapshot,
    scan_installed_plugins,
    scan_skills,
    set_plugin_enabled,
    set_skill_archived,
)
from track import extract_skill_name


# ── Fixtures ─────────────────────────────────────────────────────────


def now_iso(offset_days: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=offset_days)
    return dt.isoformat()


@pytest.fixture
def temp_dirs(tmp_path):
    """Create temporary skill directories mimicking the real structure."""
    skills_dir = tmp_path / "skills"
    archive_dir = tmp_path / "archive"
    snapshots_dir = archive_dir / "snapshots"

    for d in [skills_dir, archive_dir, snapshots_dir]:
        d.mkdir(parents=True)

    plugin_cache_dir = tmp_path / "plugins" / "cache"
    plugin_cache_dir.mkdir(parents=True)
    settings_path = tmp_path / "settings.json"

    # Patch the module-level paths
    import organize
    orig = {
        "SKILLS_DIR": organize.SKILLS_DIR,
        "SNAPSHOTS_DIR": organize.SNAPSHOTS_DIR,
        "ARCHIVE_DIR": organize.ARCHIVE_DIR,
        "PLUGIN_CACHE_DIR": organize.PLUGIN_CACHE_DIR,
        "SETTINGS_PATH": organize.SETTINGS_PATH,
    }
    organize.SKILLS_DIR = skills_dir
    organize.SNAPSHOTS_DIR = snapshots_dir
    organize.ARCHIVE_DIR = archive_dir
    organize.PLUGIN_CACHE_DIR = plugin_cache_dir
    organize.SETTINGS_PATH = settings_path

    yield {
        "skills": skills_dir,
        "snapshots": snapshots_dir,
        "plugin_cache": plugin_cache_dir,
        "settings": settings_path,
        "root": tmp_path,
    }

    # Restore
    for k, v in orig.items():
        setattr(organize, k, v)


def create_skill(base_dir: Path, name: str, content: str = None) -> Path:
    """Create a fake skill directory with SKILL.md."""
    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    if content is None:
        content = f"# {name}\n\nA skill for {name} tasks.\n"
    skill_md.write_text(content)
    return skill_dir


# ── parse_frontmatter ────────────────────────────────────────────────


class TestParseFrontmatter:
    def test_no_frontmatter(self):
        fm, body = parse_frontmatter("# Title\n\nContent here.\n")
        assert fm == {}
        assert body == "# Title\n\nContent here.\n"

    def test_simple_frontmatter(self):
        text = "---\ndisable-model-invocation: true\n---\n# Title\n"
        fm, body = parse_frontmatter(text)
        assert fm["disable-model-invocation"] is True
        assert "# Title" in body

    def test_multiple_keys(self):
        text = "---\nname: my-skill\ndisable-model-invocation: false\n---\n# Body\n"
        fm, body = parse_frontmatter(text)
        assert fm["name"] == "my-skill"
        assert fm["disable-model-invocation"] is False

    def test_empty_frontmatter(self):
        text = "---\n---\n# Body\n"
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert "# Body" in body

    def test_no_closing_delimiter(self):
        text = "---\nkey: value\n# No closing\n"
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert text == body

    def test_quoted_string_values(self):
        text = '---\nname: "my skill"\n---\nBody\n'
        fm, body = parse_frontmatter(text)
        assert fm["name"] == "my skill"


# ── build_frontmatter ────────────────────────────────────────────────


class TestBuildFrontmatter:
    def test_empty_dict(self):
        assert build_frontmatter({}) == ""

    def test_boolean_values(self):
        result = build_frontmatter({"disable-model-invocation": True})
        assert "disable-model-invocation: true" in result
        assert result.startswith("---")
        assert result.endswith("---")

    def test_string_values(self):
        result = build_frontmatter({"name": "test"})
        assert "name: test" in result


# ── is_skill_archived / set_skill_archived ───────────────────────────


class TestArchiveFlag:
    def test_not_archived_by_default(self, tmp_path):
        create_skill(tmp_path, "normal")
        assert is_skill_archived(tmp_path / "normal") is False

    def test_archived_with_flag(self, tmp_path):
        create_skill(tmp_path, "archived",
                     "---\ndisable-model-invocation: true\n---\n# Archived\n")
        assert is_skill_archived(tmp_path / "archived") is True

    def test_not_archived_with_false_flag(self, tmp_path):
        create_skill(tmp_path, "active",
                     "---\ndisable-model-invocation: false\n---\n# Active\n")
        assert is_skill_archived(tmp_path / "active") is False

    def test_nonexistent_skill(self, tmp_path):
        assert is_skill_archived(tmp_path / "ghost") is False

    def test_set_archived_adds_flag(self, tmp_path):
        create_skill(tmp_path, "to-archive", "# My Skill\n\nDescription.\n")
        changed = set_skill_archived(tmp_path / "to-archive", True)
        assert changed is True
        assert is_skill_archived(tmp_path / "to-archive") is True
        # Verify content preserved
        text = (tmp_path / "to-archive" / "SKILL.md").read_text()
        assert "# My Skill" in text
        assert "Description." in text

    def test_set_active_removes_flag(self, tmp_path):
        create_skill(tmp_path, "to-activate",
                     "---\ndisable-model-invocation: true\n---\n# Skill\n\nDesc.\n")
        changed = set_skill_archived(tmp_path / "to-activate", False)
        assert changed is True
        assert is_skill_archived(tmp_path / "to-activate") is False
        text = (tmp_path / "to-activate" / "SKILL.md").read_text()
        assert "disable-model-invocation" not in text
        assert "# Skill" in text

    def test_set_archived_no_change(self, tmp_path):
        create_skill(tmp_path, "already",
                     "---\ndisable-model-invocation: true\n---\n# Skill\n")
        changed = set_skill_archived(tmp_path / "already", True)
        assert changed is False

    def test_set_active_no_change(self, tmp_path):
        create_skill(tmp_path, "already-active", "# Skill\n")
        changed = set_skill_archived(tmp_path / "already-active", False)
        assert changed is False

    def test_dry_run_no_modification(self, tmp_path):
        create_skill(tmp_path, "dry", "# Skill\n\nDesc.\n")
        changed = set_skill_archived(tmp_path / "dry", True, dry_run=True)
        assert changed is True  # Would change
        assert is_skill_archived(tmp_path / "dry") is False  # But didn't

    def test_preserves_other_frontmatter(self, tmp_path):
        create_skill(tmp_path, "multi",
                     "---\nname: multi\ncustom: value\n---\n# Multi\n")
        set_skill_archived(tmp_path / "multi", True)
        text = (tmp_path / "multi" / "SKILL.md").read_text()
        fm, _ = parse_frontmatter(text)
        assert fm["name"] == "multi"
        assert fm["custom"] == "value"
        assert fm["disable-model-invocation"] is True


# ── count_reads_in_window ────────────────────────────────────────────


class TestCountReads:
    def test_empty_reads(self):
        assert count_reads_in_window([], 90) == 0

    def test_all_within_window(self):
        reads = [now_iso(-1), now_iso(-10), now_iso(-30)]
        assert count_reads_in_window(reads, 90) == 3

    def test_some_outside_window(self):
        reads = [now_iso(-1), now_iso(-10), now_iso(-100)]
        assert count_reads_in_window(reads, 90) == 2

    def test_all_outside_window(self):
        reads = [now_iso(-91), now_iso(-120)]
        assert count_reads_in_window(reads, 90) == 0

    def test_boundary_exactly_90_days(self):
        reads = [now_iso(-89), now_iso(-91)]
        assert count_reads_in_window(reads, 90) == 1

    def test_invalid_timestamps_ignored(self):
        reads = ["not-a-date", now_iso(-1), ""]
        assert count_reads_in_window(reads, 90) == 1


# ── is_in_grace_period ───────────────────────────────────────────────


class TestGracePeriod:
    def test_new_skill_in_grace(self):
        entry = {"first_seen": now_iso(-3), "reads": [], "pinned": False}
        assert is_in_grace_period(entry, 7) is True

    def test_old_skill_past_grace(self):
        entry = {"first_seen": now_iso(-10), "reads": [], "pinned": False}
        assert is_in_grace_period(entry, 7) is False

    def test_exactly_at_boundary(self):
        entry = {"first_seen": now_iso(-7), "reads": [], "pinned": False}
        assert is_in_grace_period(entry, 7) is False

    def test_no_first_seen(self):
        entry = {"reads": [], "pinned": False}
        assert is_in_grace_period(entry, 7) is False

    def test_invalid_first_seen(self):
        entry = {"first_seen": "garbage", "reads": [], "pinned": False}
        assert is_in_grace_period(entry, 7) is False


# ── calculate_tiers ──────────────────────────────────────────────────


class TestCalculateTiers:
    def make_config(self, t1=3, t2=1, window=90, grace=7):
        return {"thresholds": {
            "t1_min_reads": t1,
            "t2_min_reads": t2,
            "window_days": window,
            "grace_period_days": grace,
        }}

    def test_high_usage_stays_t1(self):
        stats = {"skill-a": {
            "reads": [now_iso(-1), now_iso(-10), now_iso(-20)],
            "first_seen": now_iso(-60),
            "pinned": False,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["skill-a"] == 1

    def test_medium_usage_goes_t2(self):
        stats = {"skill-b": {
            "reads": [now_iso(-5)],
            "first_seen": now_iso(-60),
            "pinned": False,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["skill-b"] == 2

    def test_no_usage_goes_t3(self):
        stats = {"skill-c": {
            "reads": [],
            "first_seen": now_iso(-60),
            "pinned": False,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["skill-c"] == 3

    def test_pinned_always_t1(self):
        stats = {"skill-d": {
            "reads": [],
            "first_seen": now_iso(-60),
            "pinned": True,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["skill-d"] == 1

    def test_grace_period_keeps_t1(self):
        stats = {"skill-e": {
            "reads": [],
            "first_seen": now_iso(-3),
            "pinned": False,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["skill-e"] == 1

    def test_past_grace_with_no_usage_goes_t3(self):
        stats = {"skill-f": {
            "reads": [],
            "first_seen": now_iso(-10),
            "pinned": False,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["skill-f"] == 3

    def test_old_reads_outside_window_not_counted(self):
        stats = {"skill-g": {
            "reads": [now_iso(-91), now_iso(-100), now_iso(-120)],
            "first_seen": now_iso(-200),
            "pinned": False,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["skill-g"] == 3

    def test_mixed_skills_correct_tiers(self):
        stats = {
            "hot": {
                "reads": [now_iso(-1), now_iso(-2), now_iso(-3)],
                "first_seen": now_iso(-60), "pinned": False,
            },
            "warm": {
                "reads": [now_iso(-15)],
                "first_seen": now_iso(-60), "pinned": False,
            },
            "cold": {
                "reads": [],
                "first_seen": now_iso(-60), "pinned": False,
            },
            "new": {
                "reads": [],
                "first_seen": now_iso(-2), "pinned": False,
            },
            "pinned-cold": {
                "reads": [],
                "first_seen": now_iso(-60), "pinned": True,
            },
        }
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers == {
            "hot": 1,
            "warm": 2,
            "cold": 3,
            "new": 1,
            "pinned-cold": 1,
        }


# ── reconcile ────────────────────────────────────────────────────────


class TestReconcile:
    def test_removes_orphaned_stats(self, temp_dirs):
        create_skill(temp_dirs["skills"], "alive")
        stats = {
            "alive": {"reads": [], "first_seen": now_iso(-10), "pinned": False},
            "deleted": {"reads": [], "first_seen": now_iso(-10), "pinned": False},
        }
        result = reconcile(stats, {})
        assert "alive" in result
        assert "deleted" not in result

    def test_adds_new_skills(self, temp_dirs):
        create_skill(temp_dirs["skills"], "existing")
        create_skill(temp_dirs["skills"], "brand-new")
        stats = {
            "existing": {"reads": [], "first_seen": now_iso(-10), "pinned": False},
        }
        result = reconcile(stats, {})
        assert "brand-new" in result
        assert result["brand-new"]["first_seen"]
        assert result["brand-new"]["reads"] == []

    def test_syncs_pinned_from_config(self, temp_dirs):
        create_skill(temp_dirs["skills"], "my-skill")
        stats = {
            "my-skill": {"reads": [], "first_seen": now_iso(-10), "pinned": False},
        }
        config = {"pinned": ["my-skill"]}
        result = reconcile(stats, config)
        assert result["my-skill"]["pinned"] is True

    def test_only_scans_skills_dir(self, temp_dirs):
        """All skills are in SKILLS_DIR now — no tier2/tier3 dirs to scan."""
        create_skill(temp_dirs["skills"], "t1-skill")
        stats = {}
        result = reconcile(stats, {})
        assert "t1-skill" in result


# ── scan_skills ──────────────────────────────────────────────────────


class TestScanSkills:
    def test_finds_skills_with_skill_md(self, tmp_path):
        create_skill(tmp_path, "good-skill")
        (tmp_path / "bad-dir").mkdir()
        result = scan_skills(tmp_path)
        assert "good-skill" in result
        assert "bad-dir" not in result

    def test_empty_directory(self, tmp_path):
        result = scan_skills(tmp_path)
        assert result == {}

    def test_nonexistent_directory(self, tmp_path):
        result = scan_skills(tmp_path / "nope")
        assert result == {}


# ── get_current_tier ─────────────────────────────────────────────────


class TestGetCurrentTier:
    def test_active_skill_is_t1(self, temp_dirs):
        create_skill(temp_dirs["skills"], "active")
        assert get_current_tier("active") == 1

    def test_archived_skill_is_t2(self, temp_dirs):
        create_skill(temp_dirs["skills"], "archived",
                     "---\ndisable-model-invocation: true\n---\n# Archived\n")
        assert get_current_tier("archived") == 2

    def test_not_found(self, temp_dirs):
        assert get_current_tier("nonexistent") == 0


# ── apply_tier_change ────────────────────────────────────────────────


class TestApplyTierChange:
    def test_archive_skill(self, temp_dirs):
        create_skill(temp_dirs["skills"], "demote-me", "# Skill\n\nDesc.\n")
        result = apply_tier_change("demote-me", 1, 3, dry_run=False)
        assert result is True
        assert is_skill_archived(temp_dirs["skills"] / "demote-me") is True

    def test_activate_skill(self, temp_dirs):
        create_skill(temp_dirs["skills"], "promote-me",
                     "---\ndisable-model-invocation: true\n---\n# Skill\n")
        result = apply_tier_change("promote-me", 3, 1, dry_run=False)
        assert result is True
        assert is_skill_archived(temp_dirs["skills"] / "promote-me") is False

    def test_dry_run_no_change(self, temp_dirs):
        create_skill(temp_dirs["skills"], "stay", "# Skill\n")
        result = apply_tier_change("stay", 1, 3, dry_run=True)
        assert result is True
        assert is_skill_archived(temp_dirs["skills"] / "stay") is False

    def test_nonexistent_fails(self, temp_dirs):
        result = apply_tier_change("ghost", 1, 3, dry_run=False)
        assert result is False

    def test_skill_stays_in_place(self, temp_dirs):
        """Archived skills remain in ~/.claude/skills/, not moved."""
        create_skill(temp_dirs["skills"], "stays-here", "# Skill\n\nDesc.\n")
        apply_tier_change("stays-here", 1, 3, dry_run=False)
        assert (temp_dirs["skills"] / "stays-here" / "SKILL.md").exists()


# ── get_skill_description ────────────────────────────────────────────


class TestGetSkillDescription:
    def test_extracts_first_content_line(self, tmp_path):
        create_skill(tmp_path, "desc-test", "# Title\n\nThis is the description.\n")
        desc = get_skill_description(tmp_path / "desc-test")
        assert desc == "This is the description."

    def test_skips_headers_and_blanks(self, tmp_path):
        create_skill(tmp_path, "header-test", "# H1\n## H2\n\n---\n\nActual content here.\n")
        desc = get_skill_description(tmp_path / "header-test")
        assert desc == "Actual content here."

    def test_empty_skill_md(self, tmp_path):
        create_skill(tmp_path, "empty-test", "")
        desc = get_skill_description(tmp_path / "empty-test")
        assert desc == ""

    def test_nonexistent_returns_empty(self, tmp_path):
        desc = get_skill_description(tmp_path / "nope")
        assert desc == ""

    def test_skips_frontmatter(self, tmp_path):
        create_skill(tmp_path, "fm-test",
                     "---\ndisable-model-invocation: true\n---\n# Title\n\nReal description.\n")
        desc = get_skill_description(tmp_path / "fm-test")
        assert desc == "Real description."


# ── generate_index ───────────────────────────────────────────────────


class TestGenerateIndex:
    def test_no_archived_skills(self):
        index = generate_index({}, {}, {"active": 1})
        assert "No T2 skills" in index

    def test_t1_index_only_lists_t2(self, temp_dirs):
        create_skill(temp_dirs["skills"], "warm1", "# warm1\n\nWarm skill.\n")
        create_skill(temp_dirs["skills"], "cold1", "# cold1\n\nCold skill.\n")
        stats = {
            "warm1": {"reads": [now_iso(-1)], "first_seen": now_iso(-60), "pinned": False},
            "cold1": {"reads": [], "first_seen": now_iso(-60), "pinned": False},
        }
        tiers = {"warm1": 2, "cold1": 3}
        index = generate_index(stats, {}, tiers)
        # T2 listed directly
        assert "warm1" in index
        # T3 NOT listed directly, only as count + pointer
        cold_lines = [l for l in index.split("\n") if l.startswith("| ") and "cold1" in l]
        assert len(cold_lines) == 0
        assert "1 cold-archived" in index
        assert "t2-index.md" in index

    def test_t2_sorted_by_reads(self, temp_dirs):
        create_skill(temp_dirs["skills"], "low", "# low\n\nLow.\n")
        create_skill(temp_dirs["skills"], "high", "# high\n\nHigh.\n")
        stats = {
            "low": {"reads": [now_iso(-1)], "first_seen": now_iso(-60), "pinned": False},
            "high": {"reads": [now_iso(-1), now_iso(-2)], "first_seen": now_iso(-60), "pinned": False},
        }
        tiers = {"low": 2, "high": 2}
        index = generate_index(stats, {}, tiers)
        lines = index.split("\n")
        data_lines = [l for l in lines if l.startswith("| ") and not l.startswith("|-") and "Skill" not in l]
        assert len(data_lines) == 2
        assert "high" in data_lines[0]
        assert "low" in data_lines[1]

    def test_t1_skills_not_in_index(self):
        stats = {"active": {"reads": [now_iso(-1)] * 5, "first_seen": now_iso(-60), "pinned": False}}
        tiers = {"active": 1}
        index = generate_index(stats, {}, tiers)
        assert "No T2 skills" in index

    def test_shows_tier_counts(self):
        tiers = {"active1": 1, "active2": 1, "warm": 2, "cold": 3}
        index = generate_index({}, {}, tiers)
        assert "Active: 2" in index
        assert "T2 (warm): 1" in index
        assert "T3 (cold): 1" in index

    def test_t2_index_lists_t3_skills(self, temp_dirs):
        create_skill(temp_dirs["skills"], "cold-a", "# cold-a\n\nCold A.\n")
        create_skill(temp_dirs["skills"], "cold-b", "# cold-b\n\nCold B.\n")
        stats = {
            "cold-a": {"reads": [], "first_seen": now_iso(-60), "pinned": False},
            "cold-b": {"reads": [now_iso(-80)], "first_seen": now_iso(-60), "pinned": False},
        }
        tiers = {"cold-a": 3, "cold-b": 3}
        t2_index = generate_t2_index(stats, tiers)
        assert "cold-a" in t2_index
        assert "cold-b" in t2_index
        assert "T3 Cold Archive" in t2_index

    def test_t2_index_empty(self):
        t2_index = generate_t2_index({}, {})
        assert "No T3 skills" in t2_index


# ── extract_skill_name (track.py) ────────────────────────────────────


class TestExtractSkillName:
    def test_skill_tool_user_skill(self):
        hook = {"tool_name": "Skill", "tool_input": {"skill": "my-custom-skill"}}
        assert extract_skill_name(hook) == "my-custom-skill"

    def test_skill_tool_plugin_skill_tracked(self):
        hook = {"tool_name": "Skill", "tool_input": {"skill": "oh-my-claudecode:autopilot"}}
        assert extract_skill_name(hook) == "plugin:oh-my-claudecode"

    def test_skill_tool_plugin_skill_different_namespace(self):
        hook = {"tool_name": "Skill", "tool_input": {"skill": "claude-mem:search"}}
        assert extract_skill_name(hook) == "plugin:claude-mem"

    def test_skill_tool_plugin_empty_namespace_ignored(self):
        hook = {"tool_name": "Skill", "tool_input": {"skill": ":something"}}
        assert extract_skill_name(hook) is None

    def test_read_tool_skill_md(self):
        hook = {"tool_name": "Read", "tool_input": {
            "file_path": "/Users/me/.claude/skills/my-skill/SKILL.md"
        }}
        assert extract_skill_name(hook) == "my-skill"

    def test_read_tool_archive_skill_md(self):
        hook = {"tool_name": "Read", "tool_input": {
            "file_path": "/Users/me/.claude/skills-archive/tier2/archived-skill/SKILL.md"
        }}
        assert extract_skill_name(hook) == "archived-skill"

    def test_read_tool_non_skill_file_ignored(self):
        hook = {"tool_name": "Read", "tool_input": {
            "file_path": "/Users/me/project/src/main.py"
        }}
        assert extract_skill_name(hook) is None

    def test_read_tool_skill_md_in_wrong_path_ignored(self):
        hook = {"tool_name": "Read", "tool_input": {
            "file_path": "/Users/me/project/SKILL.md"
        }}
        assert extract_skill_name(hook) is None

    def test_unknown_tool_ignored(self):
        hook = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        assert extract_skill_name(hook) is None

    def test_empty_skill_name_ignored(self):
        hook = {"tool_name": "Skill", "tool_input": {"skill": ""}}
        assert extract_skill_name(hook) is None

    def test_read_tool_plugin_cache_skill_md(self):
        hook = {"tool_name": "Read", "tool_input": {
            "file_path": "/Users/me/.claude/plugins/cache/marketplace/oh-my-claudecode/1.0.0/skills/autopilot/SKILL.md"
        }}
        assert extract_skill_name(hook) == "plugin:oh-my-claudecode"

    def test_read_tool_plugin_cache_different_plugin(self):
        hook = {"tool_name": "Read", "tool_input": {
            "file_path": "/Users/me/.claude/plugins/cache/community/claude-mem/2.1.0/skills/search/SKILL.md"
        }}
        assert extract_skill_name(hook) == "plugin:claude-mem"

    def test_read_tool_plugin_cache_short_path_ignored(self):
        hook = {"tool_name": "Read", "tool_input": {
            "file_path": "/Users/me/.claude/plugins/cache/SKILL.md"
        }}
        # cache is at the end, cache_idx + 2 would be out of bounds
        assert extract_skill_name(hook) is None


# ── save_snapshot ────────────────────────────────────────────────────


class TestSnapshot:
    def test_creates_snapshot_file(self, temp_dirs):
        tiers_before = {"skill-a": 1, "skill-b": 2}
        path = save_snapshot(tiers_before)
        assert os.path.exists(path)
        data = json.loads(Path(path).read_text())
        assert data["tiers"] == tiers_before
        assert "timestamp" in data


# ── End-to-End Scenario ──────────────────────────────────────────────


class TestEndToEnd:
    """Simulate a full organize cycle with frontmatter approach."""

    def test_full_cycle(self, temp_dirs):
        """
        Setup: 5 skills in SKILLS_DIR with varying usage.
        Expected: hot stays active, warm -> archived, cold -> archived,
                  new stays active (grace), pinned stays active.
        """
        create_skill(temp_dirs["skills"], "hot-skill", "# hot-skill\n\nHot.\n")
        create_skill(temp_dirs["skills"], "warm-skill", "# warm-skill\n\nWarm.\n")
        create_skill(temp_dirs["skills"], "cold-skill", "# cold-skill\n\nCold.\n")
        create_skill(temp_dirs["skills"], "new-skill", "# new-skill\n\nNew.\n")
        create_skill(temp_dirs["skills"], "pinned-skill", "# pinned-skill\n\nPinned.\n")

        stats = {
            "hot-skill": {
                "reads": [now_iso(-1), now_iso(-10), now_iso(-20)],
                "first_seen": now_iso(-60), "pinned": False,
            },
            "warm-skill": {
                "reads": [now_iso(-15)],
                "first_seen": now_iso(-60), "pinned": False,
            },
            "cold-skill": {
                "reads": [],
                "first_seen": now_iso(-60), "pinned": False,
            },
            "new-skill": {
                "reads": [],
                "first_seen": now_iso(-2), "pinned": False,
            },
            "pinned-skill": {
                "reads": [],
                "first_seen": now_iso(-60), "pinned": True,
            },
        }
        config = {"pinned": ["pinned-skill"], "thresholds": {
            "t1_min_reads": 3, "t2_min_reads": 1, "window_days": 90, "grace_period_days": 7,
        }}

        # Calculate tiers
        stats = reconcile(stats, config)
        tiers = calculate_tiers(stats, config)

        assert tiers["hot-skill"] == 1
        assert tiers["warm-skill"] == 2
        assert tiers["cold-skill"] == 3
        assert tiers["new-skill"] == 1
        assert tiers["pinned-skill"] == 1

        # Apply changes
        for name, target in tiers.items():
            should_archive = target >= 2
            set_skill_archived(temp_dirs["skills"] / name, should_archive)

        # Verify: all still in skills dir
        for name in tiers:
            assert (temp_dirs["skills"] / name / "SKILL.md").exists()

        # Verify archive flags
        assert is_skill_archived(temp_dirs["skills"] / "hot-skill") is False
        assert is_skill_archived(temp_dirs["skills"] / "warm-skill") is True
        assert is_skill_archived(temp_dirs["skills"] / "cold-skill") is True
        assert is_skill_archived(temp_dirs["skills"] / "new-skill") is False
        assert is_skill_archived(temp_dirs["skills"] / "pinned-skill") is False

        # T1 index should contain warm (T2) but NOT cold (T3) directly
        index = generate_index(stats, config, tiers)
        assert "warm-skill" in index
        cold_lines = [l for l in index.split("\n") if l.startswith("| ") and "cold-skill" in l]
        assert len(cold_lines) == 0

        # T2 index should contain cold (T3) skill
        t2_index = generate_t2_index(stats, tiers)
        assert "cold-skill" in t2_index

    def test_promotion_after_usage(self, temp_dirs):
        """An archived skill gets used enough to be promoted back to active."""
        create_skill(temp_dirs["skills"], "rising-star",
                     "---\ndisable-model-invocation: true\n---\n# rising-star\n\nRising.\n")

        assert is_skill_archived(temp_dirs["skills"] / "rising-star") is True

        stats = {
            "rising-star": {
                "reads": [now_iso(-1), now_iso(-5), now_iso(-10)],
                "first_seen": now_iso(-60), "pinned": False,
            },
        }
        config = {"thresholds": {
            "t1_min_reads": 3, "t2_min_reads": 1, "window_days": 90, "grace_period_days": 7,
        }}

        tiers = calculate_tiers(stats, config)
        assert tiers["rising-star"] == 1

        set_skill_archived(temp_dirs["skills"] / "rising-star", False)
        assert is_skill_archived(temp_dirs["skills"] / "rising-star") is False
        # Still in same directory
        assert (temp_dirs["skills"] / "rising-star" / "SKILL.md").exists()

    def test_rollback_restores_state(self, temp_dirs):
        """Rollback should restore previous archive states."""
        create_skill(temp_dirs["skills"], "was-active", "# was-active\n\nDesc.\n")
        create_skill(temp_dirs["skills"], "was-archived",
                     "---\ndisable-model-invocation: true\n---\n# was-archived\n")

        # Save snapshot with original state
        from organize import save_snapshot as _save, rollback as _rollback
        _save({"was-active": 1, "was-archived": 2})

        # Simulate changes (opposite of original)
        set_skill_archived(temp_dirs["skills"] / "was-active", True)
        set_skill_archived(temp_dirs["skills"] / "was-archived", False)

        assert is_skill_archived(temp_dirs["skills"] / "was-active") is True
        assert is_skill_archived(temp_dirs["skills"] / "was-archived") is False

        # Rollback
        _rollback()

        assert is_skill_archived(temp_dirs["skills"] / "was-active") is False
        assert is_skill_archived(temp_dirs["skills"] / "was-archived") is True


# ── Plugin Helpers ───────────────────────────────────────────────────


def create_plugin_cache(cache_dir: Path, marketplace: str, plugin_name: str,
                        version: str, skill_names: list[str]) -> Path:
    """Create a fake plugin cache structure with SKILL.md files."""
    for skill_name in skill_names:
        skill_dir = cache_dir / marketplace / plugin_name / version / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill_name}\n\nA {skill_name} skill.\n")
    return cache_dir / marketplace / plugin_name


def write_settings(settings_path: Path, data: dict) -> None:
    """Write a settings.json file."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(data, f, indent=2)


def read_settings(settings_path: Path) -> dict:
    """Read a settings.json file."""
    if not settings_path.exists():
        return {}
    with open(settings_path) as f:
        return json.load(f)


# ── TestScanInstalledPlugins ─────────────────────────────────────────


class TestScanInstalledPlugins:
    def test_scans_plugin_cache(self, temp_dirs):
        create_plugin_cache(
            temp_dirs["plugin_cache"], "marketplace", "my-plugin",
            "1.0.0", ["autopilot"],
        )
        result = scan_installed_plugins()
        assert "plugin:my-plugin" in result
        info = result["plugin:my-plugin"]
        assert info["marketplace"] == "marketplace"
        assert "autopilot" in info["skills"]
        assert "autopilot" in info["skill_dirs"]

    def test_empty_cache(self, temp_dirs):
        result = scan_installed_plugins()
        assert result == {}

    def test_multiple_versions_picks_latest(self, temp_dirs):
        create_plugin_cache(
            temp_dirs["plugin_cache"], "marketplace", "versioned-plugin",
            "1.0.0", ["old-skill"],
        )
        create_plugin_cache(
            temp_dirs["plugin_cache"], "marketplace", "versioned-plugin",
            "2.0.0", ["new-skill"],
        )
        result = scan_installed_plugins()
        assert "plugin:versioned-plugin" in result
        info = result["plugin:versioned-plugin"]
        # Sorted reverse, so 2.0.0 is picked first
        assert "new-skill" in info["skills"]

    def test_multiple_plugins(self, temp_dirs):
        create_plugin_cache(
            temp_dirs["plugin_cache"], "marketplace", "plugin-a",
            "1.0.0", ["skill-a"],
        )
        create_plugin_cache(
            temp_dirs["plugin_cache"], "community", "plugin-b",
            "0.5.0", ["skill-b"],
        )
        result = scan_installed_plugins()
        assert "plugin:plugin-a" in result
        assert "plugin:plugin-b" in result
        assert result["plugin:plugin-a"]["marketplace"] == "marketplace"
        assert result["plugin:plugin-b"]["marketplace"] == "community"


# ── TestPluginReconcile ──────────────────────────────────────────────


class TestPluginReconcile:
    def test_adds_new_plugins(self, temp_dirs):
        create_plugin_cache(
            temp_dirs["plugin_cache"], "marketplace", "new-plugin",
            "1.0.0", ["some-skill"],
        )
        stats = {}
        result = reconcile(stats, {})
        assert "plugin:new-plugin" in result
        assert result["plugin:new-plugin"]["reads"] == []
        assert result["plugin:new-plugin"]["first_seen"]

    def test_removes_orphaned_plugins(self, temp_dirs):
        # No plugin in cache, but stats has an entry
        stats = {
            "plugin:gone-plugin": {
                "reads": [now_iso(-5)],
                "first_seen": now_iso(-30),
                "pinned": False,
            },
        }
        result = reconcile(stats, {})
        assert "plugin:gone-plugin" not in result

    def test_preserves_existing_plugin_stats(self, temp_dirs):
        create_plugin_cache(
            temp_dirs["plugin_cache"], "marketplace", "existing-plugin",
            "1.0.0", ["a-skill"],
        )
        reads = [now_iso(-1), now_iso(-10)]
        stats = {
            "plugin:existing-plugin": {
                "reads": reads,
                "first_seen": now_iso(-60),
                "pinned": False,
            },
        }
        result = reconcile(stats, {})
        assert "plugin:existing-plugin" in result
        assert result["plugin:existing-plugin"]["reads"] == reads


# ── TestPluginTierCalculation ────────────────────────────────────────


class TestPluginTierCalculation:
    def make_config(self, t1=3, t2=1, window=90, grace=7):
        return {"thresholds": {
            "t1_min_reads": t1,
            "t2_min_reads": t2,
            "window_days": window,
            "grace_period_days": grace,
        }}

    def test_plugin_tier_t1_with_reads(self):
        stats = {"plugin:hot-plugin": {
            "reads": [now_iso(-1), now_iso(-10), now_iso(-20)],
            "first_seen": now_iso(-60),
            "pinned": False,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["plugin:hot-plugin"] == 1

    def test_plugin_tier_t2_with_some_reads(self):
        stats = {"plugin:warm-plugin": {
            "reads": [now_iso(-5)],
            "first_seen": now_iso(-60),
            "pinned": False,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["plugin:warm-plugin"] == 2

    def test_plugin_tier_t3_no_reads(self):
        stats = {"plugin:cold-plugin": {
            "reads": [],
            "first_seen": now_iso(-60),
            "pinned": False,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["plugin:cold-plugin"] == 3

    def test_plugin_pinned_stays_t1(self):
        stats = {"plugin:pinned-plugin": {
            "reads": [],
            "first_seen": now_iso(-60),
            "pinned": True,
        }}
        tiers = calculate_tiers(stats, self.make_config())
        assert tiers["plugin:pinned-plugin"] == 1


# ── TestSetPluginEnabled ─────────────────────────────────────────────


class TestSetPluginEnabled:
    def test_disable_plugin(self, temp_dirs):
        write_settings(temp_dirs["settings"], {
            "enabledPlugins": {"my-plugin@marketplace": True},
        })
        changed = set_plugin_enabled("my-plugin@marketplace", False)
        assert changed is True
        data = read_settings(temp_dirs["settings"])
        assert data["enabledPlugins"]["my-plugin@marketplace"] is False

    def test_enable_plugin(self, temp_dirs):
        write_settings(temp_dirs["settings"], {
            "enabledPlugins": {"my-plugin@marketplace": False},
        })
        changed = set_plugin_enabled("my-plugin@marketplace", True)
        assert changed is True
        data = read_settings(temp_dirs["settings"])
        # Enabling removes the False entry (absent = enabled)
        assert "my-plugin@marketplace" not in data["enabledPlugins"]

    def test_preserves_other_settings(self, temp_dirs):
        write_settings(temp_dirs["settings"], {
            "enabledPlugins": {"other-plugin@marketplace": False},
            "someOtherKey": "preserved-value",
        })
        set_plugin_enabled("new-plugin@community", False)
        data = read_settings(temp_dirs["settings"])
        assert data["someOtherKey"] == "preserved-value"
        assert data["enabledPlugins"]["other-plugin@marketplace"] is False
        assert data["enabledPlugins"]["new-plugin@community"] is False

    def test_creates_enabled_plugins_key(self, temp_dirs):
        write_settings(temp_dirs["settings"], {"existingKey": 42})
        set_plugin_enabled("fresh-plugin@marketplace", False)
        data = read_settings(temp_dirs["settings"])
        assert "enabledPlugins" in data
        assert data["enabledPlugins"]["fresh-plugin@marketplace"] is False
        assert data["existingKey"] == 42


# ── TestPluginInIndex ────────────────────────────────────────────────


class TestPluginInIndex:
    def make_config(self):
        return {"thresholds": {
            "t1_min_reads": 3,
            "t2_min_reads": 1,
            "window_days": 90,
            "grace_period_days": 7,
        }}

    def test_t2_plugin_in_t1_index(self, temp_dirs):
        create_plugin_cache(
            temp_dirs["plugin_cache"], "marketplace", "warm-plugin",
            "1.0.0", ["a-skill"],
        )
        installed = scan_installed_plugins()
        stats = {"plugin:warm-plugin": {
            "reads": [now_iso(-5)],
            "first_seen": now_iso(-60),
            "pinned": False,
        }}
        tiers = {"plugin:warm-plugin": 2}
        index = generate_index(stats, self.make_config(), tiers, installed)
        assert "warm-plugin" in index
        assert "T2 Plugins" in index

    def test_t3_plugin_in_t2_index(self, temp_dirs):
        create_plugin_cache(
            temp_dirs["plugin_cache"], "marketplace", "cold-plugin",
            "1.0.0", ["b-skill"],
        )
        installed = scan_installed_plugins()
        stats = {"plugin:cold-plugin": {
            "reads": [],
            "first_seen": now_iso(-60),
            "pinned": False,
        }}
        tiers = {"plugin:cold-plugin": 3}
        t2_index = generate_t2_index(stats, tiers, installed)
        assert "cold-plugin" in t2_index
        assert "T3 Plugins" in t2_index

    def test_t1_plugin_not_in_index(self, temp_dirs):
        stats = {"plugin:active-plugin": {
            "reads": [now_iso(-1), now_iso(-2), now_iso(-3)],
            "first_seen": now_iso(-60),
            "pinned": False,
        }}
        tiers = {"plugin:active-plugin": 1}
        index = generate_index(stats, self.make_config(), tiers)
        # T1 plugins should not appear in T2 tables
        plugin_lines = [
            l for l in index.split("\n")
            if "active-plugin" in l and l.startswith("| ")
        ]
        assert len(plugin_lines) == 0


# ── TestPluginApplyTierChange ────────────────────────────────────────


class TestPluginApplyTierChange:
    def test_archive_plugin_disables_in_settings(self, temp_dirs):
        write_settings(temp_dirs["settings"], {})
        changed = apply_plugin_tier_change(
            "plugin:my-plugin", "my-plugin@marketplace", 1, 2, dry_run=False,
        )
        assert changed is True
        data = read_settings(temp_dirs["settings"])
        assert data["enabledPlugins"]["my-plugin@marketplace"] is False

    def test_activate_plugin_enables_in_settings(self, temp_dirs):
        write_settings(temp_dirs["settings"], {
            "enabledPlugins": {"my-plugin@marketplace": False},
        })
        changed = apply_plugin_tier_change(
            "plugin:my-plugin", "my-plugin@marketplace", 2, 1, dry_run=False,
        )
        assert changed is True
        data = read_settings(temp_dirs["settings"])
        assert "my-plugin@marketplace" not in data["enabledPlugins"]

    def test_dry_run_no_settings_change(self, temp_dirs):
        write_settings(temp_dirs["settings"], {})
        changed = apply_plugin_tier_change(
            "plugin:my-plugin", "my-plugin@marketplace", 1, 3, dry_run=True,
        )
        assert changed is True
        data = read_settings(temp_dirs["settings"])
        # Settings should be unchanged (no enabledPlugins key added)
        assert "enabledPlugins" not in data
