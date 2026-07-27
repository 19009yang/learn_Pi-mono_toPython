# Python pi Skills 系统设计（MVP）

本文说明 `pi_coding_agent` 当前的最小 Skills 设计、调用方式与已知边界。

## 目标与范围

Skills 为特定任务提供可复用的 Markdown 指令。它们不直接执行代码，也不新增工具；Skill 内容作为上下文交给 Agent，由模型决定如何调用已有的 Read、Write、Edit、Bash、Grep、Glob 工具。

当前实现刻意保持最小：显式目录加载、系统提示中的技能目录、一次性显式调用。它不包含会话持久化、自动目录发现、`.gitignore` 规则或 slash-command TUI。

## 组成

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| `Skill` | `pi_coding_agent/skills.py` | 保存名称、描述、完整 Markdown、文件位置和禁用标记。 |
| `load_skills()` | `pi_coding_agent/skills.py` | 读取传入目录根部的 `SKILL.md`。 |
| `build_system_prompt()` | `pi_coding_agent/system_prompt.py` | 将可供模型选择的 Skill 输出为 `<available_skills>` XML。 |
| `format_skill_invocation()` | `pi_coding_agent/skills.py` | 把指定 Skill 的完整内容包成 `<skill>` 块。 |
| CLI | `pi_coding_agent/cli.py` | 处理 `--skills-dir` 发现和 `--skill` 显式调用。 |

## Skill 文件格式

每个显式加载目录只读取它自身根部的 `SKILL.md`。目录不递归扫描，因此子目录中的 `SKILL.md` 不会自动生效。

```md
---
name: hello-file
description: Create a hello.txt file containing hello, then read it back.
disable-model-invocation: false
---

# Hello file

1. Write `hello.txt`.
2. Read it back.
```

`description` 必填，用于模型匹配任务。省略 `name` 时会使用 Skill 目录名。`disable-model-invocation: true` 会保留文件可被程序加载的能力，但不把它列入系统提示。

MVP frontmatter 解析器支持常用的标量值、布尔值、引号字符串，以及 `|`/`>` 多行文本；它不是完整 YAML 实现。复杂 YAML 应留到后续引入 `PyYAML` 后处理。

## 调用流程

```text
--skills-dir <目录>
        │
        ▼
load_skills() ──► Skill[] ──► build_system_prompt()
                                  │
                                  ▼
                         <available_skills> XML

--skill <名称> -p <请求>
        │
        ▼
format_skill_invocation() ──► <skill>完整内容 + 用户请求 ──► Agent.prompt()
```

有两种使用模式：

1. 发现模式：只传 `--skills-dir`。模型会看到每个未禁用 Skill 的名称、描述和绝对位置，并在任务匹配时应使用 Read 工具读取完整 `SKILL.md`。
2. 显式模式：同时传 `--skill <名称>` 和 `-p/--prompt`。CLI 立即把该 Skill 的完整内容注入本次请求，因此不依赖模型先自行读取文件。

## 示例：hello-file

示例位于 `pi_learn/skills/hello-file/SKILL.md`，要求模型创建 `hello.txt` 后读取验证。

Windows PowerShell：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe -m pi_coding_agent `
  --cwd . `
  --skills-dir .\pi_learn\skills\hello-file `
  --skill hello-file `
  -p "请按该 Skill 执行。"
```

运行时，CLI 会先加载 `hello-file`，再把它的完整指令和用户请求一起发送给 Agent。若模型决定执行工具，预期链路为 `write` → `read` → 文本回答。

## 行为与安全边界

- Skill 是提示词约束，不是权限系统；它不能绕过工具的 Read-before-Write/Edit 限制。
- 发现模式依赖模型遵循系统提示，因此不保证模型一定读取或执行匹配 Skill。
- 显式模式确保完整 Skill 内容进入当前请求，但仍不能保证模型一定调用工具。
- `--skill` 在 MVP 中只支持与单次 `-p/--prompt` 一起使用；交互式会话级激活可在后续阶段加入。
- 未找到目录、缺少 `SKILL.md` 或没有 `description` 的 Skill 会被跳过；显式指定不存在的名称会以退出码 2 失败。

## 后续扩展

可以在不改变 `Skill`、`build_system_prompt()` 和 `format_skill_invocation()` 接口的前提下增加：递归发现与 `.gitignore`、完整 YAML 解析、多个来源及优先级、slash command、会话持久化和用户确认策略。
