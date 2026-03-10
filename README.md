# claude-skills-organizer

[English](README.md) | [中文](README_CN.md)

A Claude Code plugin that manages user skills and plugins in a three-tier hierarchy to reduce context noise.

## Problem

As you install more Claude Code skills and plugins, every skill's description is loaded into every conversation — even skills you rarely use. With 50-100+ skills across user skills and plugins, this adds noise to Claude's context, interferes with skill matching, and degrades model performance.

## Quick Start

```bash
/plugin marketplace add tidego/claude-skills-organizer
/plugin install claude-skills-organizer
```

Once installed, the plugin works **fully automatically** — no extra setup needed. Usage tracking hooks start recording immediately, and tiers adjust dynamically based on your actual usage patterns. For manual control, see the [Usage](#usage) section below, but automatic tracking already handles most scenarios including dynamic promotion and demotion.

All features have been fully tested — including tier rebalancing, pin/unpin, promote/demote, rollback, clean, and 30-day usage simulation.

## Solution

This plugin automatically archives rarely-used skills and plugins based on usage patterns:

**User skills** are archived by toggling `disable-model-invocation: true` in their SKILL.md frontmatter:
- Stay in `~/.claude/skills/` — no files are moved
- Keep their `/skill-name` command — you can still invoke them directly
- Are removed from Claude's auto-matching — less noise, better matching

**Plugins** are treated as a whole unit — all skill reads within a plugin are aggregated:
- Archived by setting `enabledPlugins` to `false` in `~/.claude/settings.json`
- Can be re-enabled instantly when needed
- Same tier rules apply (3+ reads → T1, 1-2 → T2, 0 → T3)

### Three Tiers

| Tier | State | Loaded? | Criteria (15-day window) |
|------|-------|---------|--------------------------|
| **T1** (active) | No frontmatter flag | Always | 3+ reads |
| **T2** (warm) | `disable-model-invocation: true` | On `/` invoke | 1-2 reads |
| **T3** (cold) | `disable-model-invocation: true` | On `/` invoke | 0 reads |

### Hierarchical Index — Claude Can Dig Deeper

The plugin maintains a two-level index so Claude can progressively discover archived skills:

```
T1 Index (always loaded in context)
  └── Lists T2 warm skills with descriptions
        └── Points to T2 Index file
              └── T2 Index (read on demand)
                    └── Lists T3 cold skills with descriptions
```

- **T1 index** (`skills-organize/SKILL.md`): Always in Claude's context. Shows T2 skills and plugins + a pointer to the T2 index.
- **T2 index** (`~/.claude/skills-archive/t2-index.md`): Read by Claude on demand when it needs a cold skill or plugin. Lists all T3 entries.

This means Claude can **proactively discover** archived skills when your request matches a description in the index — no need to remember exact skill names.

## Usage

### Slash Command

```
/skills-organize              # Dry-run: show what would change
/skills-organize --apply       # Execute tier changes
/skills-organize --stats       # Show usage statistics
```

### Pin / Unpin

```
/skills-organize --pin my-skill          # Keep a skill always in T1
/skills-organize --pin plugin:my-plugin  # Keep a plugin always in T1
/skills-organize --unpin my-skill        # Remove pin
```

### Force Promote / Demote

```
/skills-organize --promote my-skill --apply   # Activate (remove archive flag)
/skills-organize --demote my-skill --apply     # Archive (add flag)
```

### Clean (Destructive)

```
/skills-organize --clean              # Preview: show what would be deleted
/skills-organize --clean --apply      # Delete all T2/T3 skills, disable T2/T3 plugins
```

### Custom Window

```
/skills-organize --window 7              # Use 7-day window instead of default 15
/skills-organize --window 30 --apply     # Apply with 30-day window
```

The `--window` parameter sets the usage window in days (default: 15). Skills with no reads within this window get archived. A shorter window archives more aggressively; a longer window keeps more skills active.

### Rollback

```
/skills-organize --rollback    # Undo last organize
```

## How It Works

### Automatic Tracking

Three hooks silently record skill usage:

- **Skill hook** (`PreToolUse:Skill`): Fires when Claude autonomously invokes a skill (plugin skills like `/plugin:skill` are aggregated by plugin)
- **Read hook** (`PreToolUse:Read`): Fires when Claude reads a `SKILL.md` file (plugin skill reads are aggregated by plugin)
- **Prompt hook** (`UserPromptSubmit`): Fires when you manually type `/skill-name` — detects the command, validates it against installed skills/plugins, and records usage

Usage data is stored in `~/.claude/skills-archive/usage-stats.json`.

### Tier Rebalancing

When you run `/skills-organize --apply`:

1. **Reconcile**: Scans `~/.claude/skills/` and `~/.claude/plugins/cache/`, removes orphaned stats, registers new entries
2. **Calculate**: Counts reads per skill/plugin in the 15-day window, determines target tier
3. **Snapshot**: Saves current state for rollback (skill frontmatter + plugin enabled states)
4. **Toggle**: Skills → `disable-model-invocation: true` in SKILL.md; Plugins → `enabledPlugins: false` in settings.json
5. **Index**: Regenerates hierarchical indexes (T1 index → T2 skills & plugins, T2 index → T3 skills & plugins)

### Safety Features

- **Dry-run by default**: Shows changes without executing
- **Grace period**: New skills stay active for 7 days before being eligible for archiving
- **Pinning**: Force any skill to stay active regardless of usage
- **Snapshots**: Every `--apply` creates a rollback snapshot
- **Non-destructive**: Skills → frontmatter toggle only; Plugins → settings toggle only. No files moved or deleted

### How Discovery Works

```
User: "help me write a research paper"

Claude sees T1 index in context:
  → T2 table has "research-paper-writer" with description
  → Claude reads T2 index for more options
  → Finds "academic-research-writer" in T3
  → Invokes /research-paper-writer or /academic-research-writer
```

Even archived skills are **one read away** from being used. The hierarchical index ensures Claude can always find the right skill.

## Configuration

Edit `~/.claude/skills-archive/config.json`:

```json
{
  "pinned": ["my-important-skill", "plugin:my-important-plugin"],
  "thresholds": {
    "t1_min_reads": 3,
    "t2_min_reads": 1,
    "window_days": 15,
    "grace_period_days": 7
  }
}
```

## Project Structure

```
claude-skills-organizer/
├── plugin/                          # Marketplace plugin content
│   ├── .claude-plugin/plugin.json   # Plugin manifest
│   ├── skills/skills-organize/      # T1 index skill (always loaded)
│   │   └── SKILL.md
│   ├── commands/skills-organize.md  # /skills-organize slash command
│   ├── hooks/hooks.json             # PreToolUse hooks for tracking
│   └── scripts/
│       ├── organize.py              # Core rebalancing logic
│       ├── track.py                 # Hook handler for usage tracking
│       └── setup.py                 # Post-install initialization
├── tests/
│   └── test_organize.py             # 103 unit tests
├── LICENSE                          # MIT
└── README.md
```

### Runtime Data

```
~/.claude/skills-archive/
├── usage-stats.json    # Per-skill read timestamps
├── config.json         # Pinned list + thresholds
├── t2-index.md         # T2 index → lists T3 cold skills
└── snapshots/          # Rollback snapshots
```

## Requirements

- Python 3.9+
- Claude Code with plugin support
- pytest (for running tests)

## Testing

```bash
python3 -m pytest tests/ -v
```

## How `disable-model-invocation` Works

This is a Claude Code frontmatter flag. When set to `true` in a SKILL.md file:

```yaml
---
disable-model-invocation: true
---
# My Skill
...
```

- The skill's **description is removed from Claude's context** (saves tokens, reduces noise)
- The skill's **`/skill-name` command still works** (user can invoke manually)
- The skill **stays in `~/.claude/skills/`** (no file movement)

This plugin simply toggles this flag based on usage patterns.

## Scope

This plugin manages:
- **User skills** in `~/.claude/skills/` — archived via SKILL.md frontmatter toggle
- **Plugins** in `~/.claude/plugins/cache/` — archived via `enabledPlugins` toggle in settings.json (all skills in a plugin are treated as one unit)

## License

MIT
