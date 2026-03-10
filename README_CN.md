# claude-skills-organizer

[English](README.md) | [中文](README_CN.md)

一个 Claude Code 插件，通过三层分级管理用户 skills 和插件，减少上下文噪音。

## 问题

随着安装的 Claude Code skills 和插件越来越多，每个 skill 的描述都会加载到每次对话中——即使你很少使用它们。当 skills 数量达到 50-100+ 时，会给 Claude 的上下文增加噪音，干扰 skill 匹配，同时也会影响模型的整体性能。

## 快速开始

```bash
/plugin marketplace add tidego/claude-skills-organizer
/plugin install claude-skills-organizer
```

安装后**全自动运行**——无需额外操作。使用追踪钩子会立即开始记录，层级根据你的实际使用模式动态调整。如需手动控制，请参考下方[使用方法](#使用方法)部分，但自动追踪已能处理大多数场景，包括动态升降级。

所有功能均经过完整测试——包括层级重平衡、置顶/取消置顶、升级/降级、回滚、清理，以及 30 天使用模拟验证。

## 方案

本插件根据使用模式自动归档不常用的 skills 和插件：

**用户 skills** 通过切换 SKILL.md frontmatter 中的 `disable-model-invocation: true` 来归档：
- 保留在 `~/.claude/skills/` 中——不移动文件
- 保留 `/skill-name` 命令——仍可直接调用
- 从 Claude 的自动匹配中移除——更少噪音，更好的匹配

**插件** 作为整体管理——插件内所有 skill 的读取次数聚合计算：
- 通过在 `~/.claude/settings.json` 中设置 `enabledPlugins` 为 `false` 来归档
- 需要时可以立即重新启用
- 适用相同的分级规则（3+ 次读取 → T1，1-2 次 → T2，0 次 → T3）

### 三层分级

| 层级 | 状态 | 是否加载？ | 标准（15 天窗口） |
|------|------|-----------|-------------------|
| **T1**（活跃） | 无 frontmatter 标记 | 始终加载 | 3+ 次读取 |
| **T2**（温存） | `disable-model-invocation: true` | `/` 调用时加载 | 1-2 次读取 |
| **T3**（冷存） | `disable-model-invocation: true` | `/` 调用时加载 | 0 次读取 |

### 分层索引——Claude 可以逐级发现

插件维护一个两级索引，让 Claude 可以逐步发现已归档的 skills：

```
T1 索引（始终加载到上下文）
  └── 列出 T2 温存 skills 及描述
        └── 指向 T2 索引文件
              └── T2 索引（按需读取）
                    └── 列出 T3 冷存 skills 及描述
```

- **T1 索引** (`skills-organize/SKILL.md`)：始终在 Claude 的上下文中。展示 T2 skills 和插件 + 指向 T2 索引的链接。
- **T2 索引** (`~/.claude/skills-archive/t2-index.md`)：Claude 按需读取。列出所有 T3 条目。

这意味着当你的请求匹配索引中的描述时，Claude 可以**主动发现**已归档的 skills——无需记住精确的 skill 名称。

## 使用方法

### 斜杠命令

```
/skills-organize              # 预览：显示将要发生的变更
/skills-organize --apply       # 执行层级变更
/skills-organize --stats       # 显示使用统计
```

### 置顶 / 取消置顶

```
/skills-organize --pin my-skill          # 让某个 skill 始终保持在 T1
/skills-organize --pin plugin:my-plugin  # 让某个插件始终保持在 T1
/skills-organize --unpin my-skill        # 取消置顶
```

### 强制升级 / 降级

```
/skills-organize --promote my-skill --apply   # 激活（移除归档标记）
/skills-organize --demote my-skill --apply     # 归档（添加标记）
```

### 清理（破坏性操作）

```
/skills-organize --clean              # 预览：显示将要删除的内容
/skills-organize --clean --apply      # 删除所有 T2/T3 skills，禁用 T2/T3 插件
```

### 回滚

```
/skills-organize --rollback    # 撤销上次操作
```

## 工作原理

### 自动追踪

三个钩子静默记录 skill 使用情况：

- **Skill 钩子** (`PreToolUse:Skill`)：当 Claude 自动调用 skill 时触发（插件 skill 如 `/plugin:skill` 按插件聚合）
- **Read 钩子** (`PreToolUse:Read`)：当 Claude 读取 `SKILL.md` 文件时触发（插件 skill 的读取按插件聚合）
- **Prompt 钩子** (`UserPromptSubmit`)：当你手动输入 `/skill-name` 时触发——检测命令，校验是否为已安装的 skill/插件，记录使用

使用数据存储在 `~/.claude/skills-archive/usage-stats.json`。

### 层级重平衡

当你运行 `/skills-organize --apply` 时：

1. **对账**：扫描 `~/.claude/skills/` 和 `~/.claude/plugins/cache/`，移除孤立统计，注册新条目
2. **计算**：统计 15 天窗口内每个 skill/插件 的读取次数，确定目标层级
3. **快照**：保存当前状态用于回滚（skill frontmatter + 插件启用状态）
4. **切换**：Skills → SKILL.md 中的 `disable-model-invocation: true`；插件 → settings.json 中的 `enabledPlugins: false`
5. **索引**：重新生成分层索引（T1 索引 → T2 skills 和插件，T2 索引 → T3 skills 和插件）

### 安全特性

- **默认预览模式**：显示变更但不执行
- **保护期**：新 skills 在 7 天内不会被归档
- **置顶**：强制任何 skill 保持活跃，不受使用量影响
- **快照**：每次 `--apply` 都会创建回滚快照
- **非破坏性**：Skills → 仅切换 frontmatter；插件 → 仅切换 settings。不移动或删除文件

### 发现机制

```
用户："帮我写一篇研究论文"

Claude 在上下文中看到 T1 索引：
  → T2 表中有 "research-paper-writer" 及描述
  → Claude 读取 T2 索引查找更多选项
  → 在 T3 中找到 "academic-research-writer"
  → 调用 /research-paper-writer 或 /academic-research-writer
```

即使已归档的 skills 也只需**一次读取**即可使用。分层索引确保 Claude 始终能找到合适的 skill。

## 配置

编辑 `~/.claude/skills-archive/config.json`：

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

## 项目结构

```
claude-skills-organizer/
├── plugin/                          # Marketplace 插件内容
│   ├── .claude-plugin/plugin.json   # 插件清单
│   ├── skills/skills-organize/      # T1 索引 skill（始终加载）
│   │   └── SKILL.md
│   ├── commands/skills-organize.md  # /skills-organize 斜杠命令
│   ├── hooks/hooks.json             # PreToolUse 钩子（用于追踪）
│   └── scripts/
│       ├── organize.py              # 核心重平衡逻辑
│       ├── track.py                 # 钩子处理器（使用追踪）
│       └── setup.py                 # 安装后初始化
├── tests/
│   └── test_organize.py             # 103 个单元测试
├── LICENSE                          # MIT
└── README.md
```

### 运行时数据

```
~/.claude/skills-archive/
├── usage-stats.json    # 每个 skill 的读取时间戳
├── config.json         # 置顶列表 + 阈值
├── t2-index.md         # T2 索引 → 列出 T3 冷存 skills
└── snapshots/          # 回滚快照
```

## 环境要求

- Python 3.9+
- 支持插件的 Claude Code
- pytest（用于运行测试）

## 测试

```bash
python3 -m pytest tests/ -v
```

## `disable-model-invocation` 原理

这是一个 Claude Code frontmatter 标记。当在 SKILL.md 文件中设置为 `true` 时：

```yaml
---
disable-model-invocation: true
---
# My Skill
...
```

- Skill 的**描述从 Claude 的上下文中移除**（节省 token，减少噪音）
- Skill 的 **`/skill-name` 命令仍然有效**（用户可手动调用）
- Skill **保留在 `~/.claude/skills/` 中**（不移动文件）

本插件根据使用模式自动切换此标记。

## 管理范围

本插件管理：
- `~/.claude/skills/` 中的**用户 skills** — 通过 SKILL.md frontmatter 切换归档
- `~/.claude/plugins/cache/` 中的**插件** — 通过 settings.json 中的 `enabledPlugins` 切换归档（插件内所有 skills 作为整体管理）

## 许可证

MIT
