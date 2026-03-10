# /skills-organize

Manage the three-tier skill hierarchy to reduce context noise.

## Usage

When the user runs `/skills-organize`, execute the organize script:

```bash
python3 ~/.claude/plugins/cache/*/claude-skills-organizer/*/scripts/organize.py
```

### Common operations

- **Show current state (dry-run)**: `python3 organize.py`
- **Apply changes**: `python3 organize.py --apply`
- **Show usage stats**: `python3 organize.py --stats`
- **Pin a skill**: `python3 organize.py --pin <name>`
- **Unpin a skill**: `python3 organize.py --unpin <name>`
- **Force promote**: `python3 organize.py --promote <name> --apply`
- **Force demote**: `python3 organize.py --demote <name> --apply`
- **Rollback last change**: `python3 organize.py --rollback`
- **Clean (delete T2/T3)**: `python3 organize.py --clean --apply`

## How It Works

User skills are archived by adding `disable-model-invocation: true` to their SKILL.md
frontmatter. Plugins are archived by setting `enabledPlugins` to `false` in
`~/.claude/settings.json`. Plugins are treated as a whole unit — all skill reads
within a plugin are aggregated for tier calculation.

## Triggers

Use this skill when:
- User says "organize skills", "manage skills", "skill tiers"
- User says "too many skills", "reduce context", "skill cleanup"
- User says "pin skill", "unpin skill", "promote skill", "demote skill"
- User says "organize plugins", "disable unused plugins", "plugin cleanup"
- User says "clean skills", "delete unused skills", "remove cold skills"
