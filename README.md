# Pi Python：从 TypeScript Pi 学习 Agent 架构

这是一个以学习为目的的 Python 项目，尝试移植并理解
[earendil-works/pi](https://github.com/earendil-works/pi) 的核心设计。它不是上游项目的官方实现，也不追求当前阶段的完整功能对等；重点是把 Pi 的分层、流式事件、工具调用与编码 Agent 工作流，用 Python `asyncio` 和类型化数据结构重新实现。

## 当前状态

项目已经可以运行一个最小的命令行 Coding Agent，默认通过 DeepSeek 的 OpenAI 兼容接口调用模型，并支持流式输出与工具调用。

已实现：

- `pi_ai`：统一消息、模型、认证、流式事件与 Provider 注册表；包含 DeepSeek、Qwen Provider、OpenAI Completions 流式适配，以及模型目录与消息转换基础设施。
- `pi_agent`：有状态 Agent、Agent Loop、多轮工具调用、会话树、内存/JSONL/SQLite 存储、上下文压缩和 AgentHarness。
- `pi_coding_agent`：交互式 CLI、系统提示词组装、Skills 发现与加载，以及 Bash、读取、写入、精确编辑、Grep、Glob、Web Search 等工具。
- 自动化测试：覆盖事件流、认证、模型、Agent Loop、工具、CLI 与 Skills。
- 真实联调脚本：使用 `DEEPSEEK_API_KEY` 验证文本流和多轮工具调用。

当前仍是学习型实现，生产级并发控制、数据库迁移和更多 Provider 兼容规则仍可继续完善。具体路线见 [`docs/learning-roadmap.md`](docs/learning-roadmap.md) 和 [`docs/python-pi-implementation-plan.md`](docs/python-pi-implementation-plan.md)。

## 架构概览

```text
pi_coding_agent                 面向用户的 CLI、系统提示词、Skills 与编码工具
        │
        ▼
pi_agent                        Agent 状态机、事件生命周期、工具调用循环
        │
        ▼
pi_ai                           统一类型、认证、模型注册、Provider 流式适配
        │
        ▼
DeepSeek API                    当前 CLI 默认注册的 Provider
```

三层之间通过统一的消息和事件类型协作：`pi_ai` 将 Provider 的流式响应转换为 `AssistantMessageEvent`；`pi_agent` 消费这些事件、执行模型请求的工具并将结果反馈给下一轮；`pi_coding_agent` 负责把它包装成可交互的本地编码助手。

## 目录结构

```text
pi_ai/                          LLM Provider 抽象与基础设施
  providers/                    DeepSeek、OpenAI Completions、模型目录等
pi_agent/                       Agent、Agent Loop、消息转换与类型
pi_coding_agent/                CLI、Skills、系统提示词和内置工具
  tools/                        bash / read / write / edit / grep / glob / search
  skills/                       示例 Skill
test/                           单元测试与真实 Provider 验证脚本
markdown/                       Pi 架构笔记、学习路线与实现计划
demo/                           小型实验和演示代码
```

## 环境要求

- Python 3.12 或更高版本（见 [`.python-version`](.python-version)）
- [uv](https://docs.astral.sh/uv/) 用于依赖和虚拟环境管理
- DeepSeek API Key（仅在运行真实模型调用时需要）

所有依赖都由 `uv` 管理；请不要使用 `pip` 单独安装项目依赖。

## 安装与配置

在仓库根目录执行：

```powershell
uv sync
Copy-Item .env.example .env
```

然后在 `.env` 中配置自己的密钥：

```dotenv
DEEPSEEK_API_KEY=your_api_key
DASHSCOPE_API_KEY=your_api_key
```

`.env` 已被 Git 忽略，请勿提交真实密钥。也可以不创建 `.env`，而在当前 PowerShell 会话中设置环境变量：

```powershell
$env:DEEPSEEK_API_KEY = "your_api_key"
$env:DASHSCOPE_API_KEY = "your_api_key"
```

## 运行 Coding Agent

启动交互式 REPL：

```powershell
uv run python -m pi_coding_agent
```

交互模式会自动创建本地 SQLite 会话，并在提示符中显示当前模型和会话 ID。普通文本发送给 Agent，以 `/` 开头的输入作为 CLI 命令处理：

```text
/help                         显示命令帮助
/clear                        清空上下文并创建新会话；旧会话仍可恢复
/new [session-id]             创建新的空白会话
/resume [session-id]          恢复会话；不带 ID 时显示会话选择列表
/model [provider/model]       切换模型；不带参数时显示模型选择列表
/status                       显示当前模型、会话、后端和上下文大小
/compact                      手动压缩当前上下文
/exit                         退出 CLI（/quit 同义）
```

例如：

```text
/model qwen/qwen3.7-plus
/model deepseek/deepseek-v4-flash
/resume project-demo
```

单次执行一个提示后退出：

```powershell
uv run python -m pi_coding_agent -p "解释这个项目的三层架构"
```

常用参数：

```powershell
# 使用较快的已注册 DeepSeek 模型
uv run python -m pi_coding_agent --model deepseek-v4-flash

# 使用 Qwen 3.7 Plus（DashScope 中国大陆兼容接口）
uv run python -m pi_coding_agent --provider qwen --model qwen3.7-plus -p "解释当前项目"

# 通过 AgentHarness 持久化并自动恢复会话（默认 SQLite：<cwd>/.pi/sessions.db）
uv run python -m pi_coding_agent --session project-demo

# 使用指定的 SQLite 数据库
uv run python -m pi_coding_agent --session project-demo --session-db .\data\sessions.db

# 兼容原有 JSONL 会话目录
uv run python -m pi_coding_agent --session project-demo --session-backend jsonl --sessions-dir .\sessions

# 指定工具工作目录
uv run python -m pi_coding_agent --cwd .\demo -p "列出当前目录中的 Python 文件"

# 递归加载 Skill，并在单次提示中显式调用一个 Skill
uv run python -m pi_coding_agent --skills-dir .\pi_coding_agent\skills --skill hello-file -p "创建问候文件"
```

默认模型是 `deepseek/deepseek-v4-pro`。可通过启动参数或交互式 `/model` 选择已注册的 DeepSeek 或 Qwen 模型。交互模式默认创建 SQLite 会话；`-p` 单次模式未指定 `--session` 时使用内存会话。

## 测试与验证

运行单元测试：

```powershell
uv run pytest
```

运行需要真实 DeepSeek 服务的验证脚本：

```powershell
uv run python test\verify_deepseek.py
uv run python test\verify_two_round_tool_calls.py
```

后两项会消耗 API 配额，并要求已配置 `DEEPSEEK_API_KEY`。

## 建议的阅读顺序

1. 从 [`pi_ai/types.py`](pi_ai/types.py) 和 [`pi_ai/event_stream.py`](pi_ai/event_stream.py) 了解核心数据模型与异步事件流。
2. 阅读 [`pi_ai/models.py`](pi_ai/models.py) 与 [`pi_ai/providers/deepseek.py`](pi_ai/providers/deepseek.py)，理解模型注册、认证和 Provider 路由。
3. 阅读 [`pi_agent/agent_loop.py`](pi_agent/agent_loop.py) 与 [`pi_agent/agent.py`](pi_agent/agent.py)，跟踪「模型输出工具调用 → 执行工具 → 工具结果回填 → 下一轮模型调用」的循环。
4. 阅读 [`pi_coding_agent/cli.py`](pi_coding_agent/cli.py) 与 [`pi_coding_agent/tools/`](pi_coding_agent/tools/)，了解如何将底层 Agent 组装成产品入口。
5. 结合 [`markdown/agent-loop-learning-path.md`](markdown/agent-loop-learning-path.md)、[`markdown/tool-system-guide.md`](markdown/tool-system-guide.md) 和 [`markdown/pi-ai-types-mapping.md`](markdown/pi-ai-types-mapping.md) 对照上游 TypeScript 设计。

## 开发约定

- 使用 `uv` 管理依赖、运行脚本和测试。
- 优先保持 Python 命名与异步惯例，同时在关键类型、事件和消息格式上与上游 Pi 对齐。
- 新增 Provider 或 Agent 能力时，应先补充对应的单元测试，再进行真实 API 联调。
