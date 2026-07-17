# Python 版 Pi-Mono MVP 学习路线

> 目标：参照 `packages/` 下的 TypeScript 源码，实现一个 Python 版 MVP，先跑通核心闭环，保留扩展点。
> 配套文档：
> - `pi_learn/Agent/packages-agent-analysis.md`（agent 包逐文件解析）
> - `pi_learn/python-pi-implementation-plan.md`（完整实现方案 + 类型签名）
> - `pi_learn/models-learning-path.md`（Models/Provider/模型目录的 TS 与 Python 对照解读）
> - `pi_learn/agent-types-learning-path.md`（Phase 2.1：Agent 类型与 convert_to_llm）
> 本路线聚焦"学什么、读哪些 TS 文件、写哪些 Python 文件、如何验证、留哪些扩展点"。

---

## 0. 路线设计原则

1. **自底向上，逐层可验证**：每一阶段产出可独立运行/测试的模块，不依赖未实现的下游。
2. **先窄后宽**：每个抽象先只实现一个具体后端（如 Auth 只做 API Key + 环境变量，Provider 只做 Anthropic），跑通后再加第二个来验证抽象是否成立。
3. **保留扩展点**：用抽象基类 + 注册表模式，而非 if/else 分支。扩展时新增子类/条目，不改核心循环。
4. **对照阅读**：每个 Python 模块都对应 1-2 个 TS 文件，先读 TS 理解意图，再写 Python，最后回看 TS 补遗漏。
5. **不照搬 TS 的工程复杂度**：跳过 lazy 动态导入、tree-shaking、30+ provider、OAuth、Bedrock、图片生成等（见各阶段"暂不实现"）。

### 目标 MVP 范围（核心闭环）

```
用户输入 → CLI → AgentHarness/Agent → agentLoop
  → stream_simple(Anthropic) → 流式文本/thinking/toolCall
  → 工具执行(Bash/Read/Write/Edit/Grep/Glob)
  → 结果回灌 → 继续循环 → 输出
```

不在 MVP 范围：会话持久化、压缩、分支摘要、TUI 渲染、多 provider、OAuth、Skills/PromptTemplates 加载（先留接口）。

---

## 1. 前置知识检查（约 0.5 天）

不动手写代码前，确认以下能力到位，否则在对应阶段会卡壳。

| 主题 | 要求 | 自检方式 |
|------|------|---------|
| Python asyncio | `async/await`、`asyncio.create_task`、`asyncio.Queue`、`asyncio.Event`、`asyncio.create_subprocess_shell` | 写一个流式读取子进程 stdout 的小脚本 |
| 类型系统 | `dataclass`、`typing` 联合类型/`Literal`/`Generic`、`Protocol`（结构化接口） | 用 `Protocol` 定义一个 `FileSystem` 抽象 |
| Pydantic / JSON Schema | 用 Pydantic v2 生成 `parameters_schema` 给工具 | 写一个工具参数模型并导出 schema |
| LLM 流式 + tool use | 理解 SSE、`tool_use`/`tool_result` 往返、`stop_reason` | 用 `anthropic` SDK 裸跑一个带工具的流式调用 |
| pi-mono 架构 | 三层结构（ai / agent / coding-agent） | 通读下面两份分析文档 |

**必读**（按顺序）：
1. `pi_learn/Agent/packages-agent-analysis.md`（理解 agent 包的循环引擎与 harness）
2. `pi_learn/python-pi-implementation-plan.md`（理解 Python 化的类型与模块划分）
3. TS 入口：`packages/ai/src/index.ts`、`packages/agent/src/index.ts`（看公共 API 边界）

---

## 2. 总览：四阶段 + 时间估算

| 阶段 | 目标 | 关键产出 | 估算 |
|------|------|---------|------|
| **Phase 1** | LLM 通信跑通 | `pi_ai`：能流式拿到 Anthropic 响应（含工具） | 3-4 天 |
| **Phase 2** | Agent 循环跑通 | `pi_agent`：`Agent.prompt()` 多轮 + 工具调用闭环 | 3-4 天 |
| **Phase 3** | 编码 Agent 可用 | `pi_coding_agent`：6 个工具 + 系统提示 + CLI | 3-4 天 |
| **Phase 4** | 持久化与扩展验证 | 会话 JSONL、压缩、加第二个 provider 验证抽象 | 4-5 天 |

> 估算按"每天 3-4 小时专注"计。可按自身节奏伸缩，但**不要跳过验证点**——每阶段验证不通过就回头补，否则错误会累积到后面爆发。

---

## Phase 1 — pi_ai：LLM 通信层

**目标**：`Models.stream_simple(model, context, options)` 返回 `AssistantMessageEventStream`，能 `async for` 拿到流式事件并以 `DoneEvent` 结束。

### 1.1 类型与事件流（核心地基）

**学习目标**：理解 pi-ai 的类型契约（Message / Context / Tool / Usage / 事件），以及 `EventStream` 的"同步返回、异步填充"语义。

**读 TS**：
- `packages/ai/src/types.ts`（全部核心类型）
- `packages/ai/src/utils/event-stream.ts`（`AssistantMessageEventStream` 实现）

**写 Python**：
- `pi_ai/types.py` — 所有 dataclass（按 `python-pi-implementation-plan.md` §1）
- `pi_ai/event_stream.py` — `EventStream[TEvent, TResult]` 泛型基类 + `AssistantMessageEventStream`

**关键概念**：
- `EventStream` 用 `asyncio.Queue` + `asyncio.Event`：`push()` 同步入队，`__aiter__` 异步消费，`end()` 放入 `None` 哨兵，`result()` 在 done/error 时 resolve。
- TS 的 `lazyStream`（同步返回 stream，内部 task 异步解析 auth 再转发 provider 事件）在 Python 用 `asyncio.create_task(_setup())` + 转发实现。

**验证点**：手写一个假 provider，往 `AssistantMessageEventStream` push `StartEvent` → 几个 `TextDeltaEvent` → `DoneEvent`，用 `async for` 打印，并 `await stream.result()` 拿到完整 `AssistantMessage`。

**扩展点**：`EventStream` 泛型化，后续 agent 层直接复用同一基类发射 `AgentEvent`。

### 1.2 Auth（先做最简）

**学习目标**：理解凭据解析的优先级链（stored → env），以及为何用抽象 `CredentialStore`。

**读 TS**：
- `packages/ai/src/auth/credential-store.ts`、`auth/types.ts`、`auth/helpers.ts`、`auth/resolve.ts`
- `packages/ai/src/env-api-keys.ts`（环境变量映射）

**写 Python**：
- `pi_ai/auth.py` — `CredentialStore` 抽象、`InMemoryCredentialStore`、`ApiKeyAuth`、`AuthResult`、`resolve_provider_auth()`、`env_api_key_auth()`

**暂不实现**：OAuth（`auth/oauth/`）、GitHub Copilot 动态 headers。

**验证点**：设置 `ANTHROPIC_API_KEY` 环境变量，`resolve_provider_auth()` 能返回带 `api_key` 的 `AuthResult`；不设置时返回 `None`。

**扩展点**：`CredentialStore` 抽象让后续可加文件持久化/OAuth store 而不动 resolve 逻辑。

### 1.3 Model 注册与 Provider 抽象

**学习目标**：理解 `Provider` / `Models` 注册表模式，`stream` vs `stream_simple` 的关系。

**读 TS**：
- `packages/ai/src/models.ts`（`Models` / `MutableModels` / `create_provider` / `calculate_cost` / thinking level 工具）
- `packages/ai/src/providers/anthropic.ts`（provider 装配示例）
- `packages/ai/src/providers/anthropic.models.ts`（模型目录数据，只取 Claude Sonnet/Opus 几条即可）

**写 Python**：
- `pi_ai/models.py` — `Provider`、`Models`、`MutableModels`、`create_provider()`、`create_models()`、`calculate_cost()`、thinking level 工具函数
- `pi_ai/providers/model_catalogs.py` — 精简版 Anthropic 模型列表（3-5 条）

**关键概念**：`Models.stream()` 做 auth 解析 + 委托 provider；`stream_simple()` 把 `SimpleStreamOptions` 映射成 `StreamOptions` 后委托 `stream()`。

**验证点**：`models.get_model("anthropic", "claude-sonnet-...")` 返回 `Model`；`calculate_cost(usage, model)` 算出费用。

**扩展点**：`MutableModels.set_provider()` 让加新 provider 不改核心。

### 1.4 第一个 Provider：Anthropic

**学习目标**：把 pi 的内部消息/工具格式翻译成 provider 请求，再把 provider SSE 翻译回 pi 事件。这是整个 ai 层的核心翻译练习。

**读 TS**：
- `packages/ai/src/api/anthropic-messages.ts`（请求构建 + SSE 解析 + 事件发射）
- `packages/ai/src/api/simple-options.ts`（`SimpleStreamOptions` → `StreamOptions` + thinking budget 调整）
- `packages/ai/src/api/transform-messages.ts`（发送前消息规范化——**先读，Phase 1 可只实现最小子集**）

**写 Python**：
- `pi_ai/providers/anthropic.py` — `stream()` + `stream_simple()`
- `pi_ai/simple_options.py` — `build_base_options()`、`adjust_max_tokens_for_thinking()`、`clamp_max_tokens_to_context()`
- `pi_ai/transform_messages.py` — **最小版**：只处理"孤立 toolCall → 合成 error toolResult"和"非视觉模型图片降级"

**关键概念**：
- thinking 配置：`budget_tokens` 按 `ThinkingLevel` 映射（minimal/low/medium/high/xhigh），`type: "enabled"|"disabled"`。
- 事件映射：`message_start`→start，`content_block_*`→text/thinking/toolcall 的 start/delta/end，`message_delta`→stop_reason+usage，最后 push done/error。
- Python `anthropic` SDK 自带流式与事件类型，比 TS 手写 SSE 解析简单——但仍要按 pi 的事件语义重组 `AssistantMessage.content`。

**验证点**（**Phase 1 终点**）：
```python
models = create_models()
stream = models.stream_simple(model, Context(system_prompt="你是助手", messages=[UserMessage("算 2+2")]), SimpleStreamOptions())
async for ev in stream:
    print(ev.type)  # start, text_delta, ..., done
msg = await stream.result()
assert msg.content 文本含 "4"
```
再跑一个带工具定义的请求，确认能收到 `ToolCallEndEvent`。

**扩展点**：`stream()` 接收 `StreamOptions`，新增 provider 只写自己的 `stream()`，`Models` 不变。

### Phase 1 自检清单
- [ ] `EventStream` 泛型可用，假 provider 验证通过
- [ ] API Key 从环境变量解析成功
- [ ] `Models.stream_simple()` 对 Anthropic 返回流式文本
- [ ] 流式响应含 thinking 与 toolCall 事件
- [ ] `transform_messages` 最小子集处理孤立 toolCall

---

## Phase 2 — pi_agent：Agent 循环引擎

**目标**：`Agent.prompt("...")` 能驱动"LLM → 工具调用 → 工具执行 → 结果回灌 → 继续 LLM"的完整多轮循环，并通过事件向外报告进度。

### 2.1 Agent 类型与消息转换

**学习目标**：区分 `Message`（LLM 标准）与 `AgentMessage`（含自定义消息的联合），理解 `convert_to_llm` 的作用。

**详细指南**：`pi_learn/agent-types-learning-path.md`

**读 TS**：
- `packages/agent/src/types.ts`（`AgentTool`、`AgentState`、`AgentEvent`、`AgentLoopConfig`、钩子结果类型）
- `packages/agent/src/harness/messages.ts`（自定义消息 + `convertToLlm`）

**写 Python**：
- `pi_agent/types.py` — `AgentTool` 基类、`AgentToolResult`、`AgentState`、`AgentEvent` 联合、`AgentLoopConfig`、`BeforeToolCallResult`/`AfterToolCallResult`
- `pi_agent/messages.py` — `BashExecutionMessage`/`CompactionSummaryMessage`/`BranchSummaryMessage`/`CustomMessage` + `convert_to_llm()` + 构造器

**验证**：
```bash
uv run pytest tests/test_agent_types.py tests/test_messages.py -v
```

**关键概念**：
- `AgentTool` 用抽象基类：`name`、`description`、`parameters`（JSON Schema）、`execution_mode`、`prepare_arguments()`、`async execute()`；经 `as_tool()` 投影为 `pi_ai.Tool`。
- `convert_to_llm`：自定义消息 → user 消息（bashExecution 文本化、summary 加 TS 同款 `<summary>` 包装）；`exclude_from_context=True` 的跳过。

**扩展点**：`AgentMessage` 用 Union，加新自定义消息类型只需扩 Union + 在 `convert_to_llm` 加一个分支（或用注册表避免 if-else）。

### 2.1 自检清单
- [ ] 能说明 `Message` 与 `AgentMessage` 的受众差异
- [ ] `convert_to_llm` 对四种自定义角色的行为能默写
- [ ] 能实现一个最小 `AgentTool` 子类并通过 `execute` 测试
- [ ] `AgentState` 赋值拷贝语义理解
- [ ] 钩子 `AfterToolCallResult` 字段级覆盖（无深合并）理解

### 2.2 核心循环引擎（最关键）

**学习目标**：理解 `runLoop` 的双层 while 循环、steering/follow-up 队列、工具并行执行 + 源序结果。

**读 TS**：
- `packages/agent/src/agent-loop.ts`（**全文件精读**，重点是 `runLoop`、`streamAssistantResponse`、`executeToolCalls`、`prepareToolCall`）

**写 Python**：
- `pi_agent/agent_loop.py` — `agent_loop()`、`agent_loop_continue()`、内部 `run_loop()`、`stream_assistant_response()`、`execute_tool_calls()`（并行 + 顺序两版）、`prepare_tool_call()`

**关键概念**：
- 外层循环处理 follow-up，内层循环处理 toolCalls + steering。
- `stream_assistant_response`：`transform_context()` → `convert_to_llm()` → `stream_fn()` → 转发事件 → 收集完整 `AssistantMessage`。
- 并行执行：preflight（prepare + beforeToolCall）顺序做，`execute()` 并行（`asyncio.gather`），`tool_execution_end` 按完成序，但 `toolResult` 消息按 assistant 源序排列。
- `stop_reason == "length"` 时工具调用参数可能截断 → 全部标记 error。

**验证点**：写 2-3 个假工具（如 `echo`、`add`），`agent_loop()` 跑一个需要两轮工具调用的任务，断言最终 `messages` 包含 user→assistant(toolCall)→toolResult→assistant(text) 序列。

**扩展点**：`AgentLoopConfig` 的钩子（`before_tool_call`/`after_tool_call`/`should_stop_after_turn`/`prepare_next_turn`）全是可选 callable，应用层按需注入。

### 2.3 Agent 有状态封装

**学习目标**：理解 `Agent` 类相比裸 `agent_loop` 多了什么——状态持有、事件监听、消息队列、barrier-before-tool-preflight 语义。

**读 TS**：
- `packages/agent/src/agent.ts`（`Agent` 类、`MutableAgentState`、`PendingMessageQueue`、`ActiveRun`）

**写 Python**：
- `pi_agent/agent.py` — `Agent` 类：`prompt()`、`continue_loop()`、`subscribe()`、`abort()`、`wait_for_idle()`、`reset()`、`steer()`/`follow_up()`、内部 `_process_events()`、`_run_with_lifecycle()`

**关键概念**：
- `prompt()` → `normalize_prompt_input()` → `run_prompt_messages()` → `run_with_lifecycle()` → `run_agent_loop()`。
- steering 队列默认 `one-at-a-time`，follow-up 默认 `all`。
- `abort()` 用 `asyncio.Event` 信号；`wait_for_idle()` 监听 `agent_end`。

**验证点**（**Phase 2 终点**）：
```python
agent = Agent(state=..., config=...)
agent.subscribe(lambda ev: print(ev.type))
await agent.prompt("用 echo 工具打印 hello 然后总结")
await agent.wait_for_idle()
```
能观察到 `agent_start → turn_start → message_* → tool_execution_* → turn_end → ... → agent_end`。运行中调 `agent.steer(...)` 能在下一轮注入。

**扩展点**：`Agent` 持有 `AgentState`，`tools`/`messages` setter 赋值时拷贝，便于后续 harness 层包装。

### Phase 2 自检清单
- [ ] 假工具多轮循环跑通
- [ ] 事件序列符合预期（start/turn/message/tool/end）
- [ ] 并行工具执行结果按源序回灌
- [ ] steering 运行中注入生效
- [ ] abort 能中止运行

---

## Phase 3 — pi_coding_agent：工具与 CLI

**目标**：命令行运行 `python -m pi_coding_agent`，能与 Anthropic 进行交互式编码对话，agent 可调用 6 个文件/Shell 工具完成真实任务。

### 3.1 内置工具集

**学习目标**：每个工具都是 `AgentTool` 子类，参数用 Pydantic 建模生成 schema，执行用 asyncio。注意"先 Read 才能 Write/Edit"的状态约束。

**读 TS**：
- `packages/coding-agent/src/core/tools/bash.ts`、`read.ts`、`write.ts`、`edit.ts`、`grep.ts`、`find.ts`（glob）
- `packages/coding-agent/src/core/tools/truncate.ts`、`path-utils.ts`、`output-accumulator.ts`

**写 Python**：
- `pi_coding_agent/tools/bash.py` — `asyncio.create_subprocess_shell`，找 bash（Windows: Git Bash → PATH；其他: /bin/bash），实时输出回调，timeout/abort，输出截断（保留尾部 50KB）
- `pi_coding_agent/tools/read.py` — `cat -n` 格式，offset/limit，图片/PDF 先跳过或最小支持
- `pi_coding_agent/tools/write.py` — 自动建父目录，"需先 Read"约束（用一个进程内 `read_files: set` 记录）
- `pi_coding_agent/tools/edit.py` — `old_string` 唯一匹配校验，`replace_all`
- `pi_coding_agent/tools/grep.py` — 优先 `subprocess` 调 `rg`，回退 `re`；output_mode 三种
- `pi_coding_agent/tools/glob.py` — `pathlib.Path.glob`，按 mtime 排序
- `pi_coding_agent/truncate.py` — `truncate_head`/`truncate_tail`/`truncate_line`，行+字节双限制，正确处理 UTF-8

**关键概念**：
- "先 Read 才能 Write/Edit"是 pi 的安全设计，用进程内集合跟踪，跨工具共享。
- Bash 输出二进制清洗：移除控制字符，保留 tab/LF/CR。
- Grep 行截断 500 字符，避免超长行爆上下文。

**验证点**：每个工具写一个最小 pytest：bash 跑 `echo`，read 读自身测试文件，write+read 往返，edit 唯一替换，grep 找到模式，glob 列出 `.py`。

**扩展点**：工具注册表——`AgentTool` 子类自注册，`AgentHarness` 按名加载，加新工具不改 CLI。

### 3.2 系统提示与 Skill 接口

**学习目标**：系统提示定义 agent 行为；Skill 系统先留接口，MVP 不必完整加载。

**读 TS**：
- `packages/coding-agent/src/core/system-prompt.ts`
- `packages/agent/src/harness/system-prompt.ts`（Skill → XML 格式化）
- `packages/agent/src/harness/skills.ts`（加载逻辑——先读懂，可不实现）

**写 Python**：
- `pi_coding_agent/system_prompt.py` — `build_system_prompt(skills, cwd, tools, project_context)`，含身份说明 + 工具使用规则 + Skill XML 块（空列表也能输出）
- `pi_coding_agent/skills.py` — `Skill` dataclass + `load_skills()` **最小版**（只扫指定目录的 `SKILL.md`，解析 YAML frontmatter，不做 .gitignore/递归）；`format_skill_invocation()`

**扩展点**：`build_system_prompt` 接收 `skills` 列表参数，后续完整加载逻辑替换 `load_skills` 实现即可，提示组装不变。

### 3.3 CLI 入口

**学习目标**：串起三层，做交互式 REPL。先做最简行式输入输出，TUI 渲染留给后续。

**读 TS**：
- `packages/coding-agent/src/cli.ts`、`cli/args.ts`、`modes/print-mode.ts`（最简模式参考）、`modes/interactive/interactive-mode.ts`（只看骨架，不照搬 Ink-like TUI）

**写 Python**：
- `pi_coding_agent/cli.py` — `argparse` 解析 `--model`/`--provider`/`--prompt`；初始化 `Models` + `Agent`（注册 6 工具 + 系统提示）；交互循环用 `input()` 读行，订阅 agent 事件实时打印文本/thinking/工具调用；`-p` 单次模式；Ctrl+C → `abort()`

**验证点**（**Phase 3 终点**）：
- `python -m pi_coding_agent -p "读取当前目录的 pyproject.toml 并告诉我项目名"` 能调用 read 工具并回答。
- 交互模式输入"建一个 hello.txt 写入 hello 然后读出来"能走 write→read 闭环。
- Ctrl+C 能中止长时间工具执行。

**扩展点**：CLI 只组装各层，后续可换 TUI 前端（textual/rich）而不动 agent/ai 层。

### Phase 3 自检清单
- [ ] 6 工具各自单测通过
- [ ] 系统提示含工具规则与 Skill 块
- [ ] `pi -p` 单次模式端到端跑通
- [ ] 交互模式多轮对话 + 工具调用
- [ ] Ctrl+C 中止生效

**里程碑：MVP 可用。** 到这里你已经有一个能交互式编码的 Python pi。

---

## Phase 4 — 持久化与扩展验证

**目标**：补齐会话持久化与上下文压缩（长对话必需），并用"加第二个 provider"验证 ai 层抽象是否成立。

### 4.1 会话持久化（JSONL + 内存）

**学习目标**：会话树（parentId 链 + leaf 指针）、append-only JSONL、上下文重建。

**读 TS**：
- `packages/agent/src/harness/session/session.ts`、`jsonl-storage.ts`、`jsonl-repo.ts`、`memory-storage.ts`、`memory-repo.ts`、`uuid.ts`、`repo-utils.ts`

**写 Python**：
- `pi_agent/session/storage.py` — `SessionStorage` 抽象、`InMemorySessionStorage`、`JsonlSessionStorage`（`.open()/.create()/append_entry()/get_path_to_root()/set_leaf_id()`）
- `pi_agent/session/session.py` — `Session` 类：`get_branch()`、`build_context()`、`append_message()`、`move_to()`
- `pi_agent/session/repo.py` — `SessionRepo` 抽象 + 两个实现
- `pi_agent/session/uuid.py` — `uuidv7()`（时间戳前缀 + 单调序列 + 随机尾部）

**验证点**：跑一段对话存成 JSONL，重启进程 `.open()` 后 `build_context()` 能还原相同消息序列；`move_to()` 切分支后上下文变化。

**扩展点**：`SessionStorage`/`SessionRepo` 抽象，后续可换 SQLite/远程存储。

### 4.2 上下文压缩

**学习目标**：何时压缩、如何找切割点、如何生成结构化摘要、split turn 处理。

**读 TS**：
- `packages/agent/src/harness/compaction/compaction.ts`、`utils.ts`
- `packages/coding-agent/src/core/compaction/compaction.ts`（coding-agent 侧的压缩设置与触发）

**写 Python**：
- `pi_agent/compaction.py` — `CompactionSettings`、`should_compact()`、`find_cut_point()`、`find_turn_start_index()`、`prepare_compaction()`、`generate_summary()`（用 `SUMMARIZATION_PROMPT`/`UPDATE_SUMMARIZATION_PROMPT`）、`compact()`
- 复用 `pi_agent/messages.py` 的 `create_compaction_summary_message()`

**验证点**：构造一个接近 context_window 的消息列表，`should_compact()` 返回 True；`compact()` 后早期消息被 `CompactionSummaryMessage` 替代，token 数下降。

**扩展点**：压缩 prompt 与 keep_recent_tokens 都在 `CompactionSettings`，可调。

### 4.3 加第二个 Provider（抽象验证）

**学习目标**：通过实现 OpenAI Completions 检验 Phase 1 的抽象是否足够，重点学 compat 系统。

**读 TS**：
- `packages/ai/src/api/openai-completions.ts`（请求构建 + compat 分支）
- `packages/ai/src/compat.ts`（compat 检测——**MVP 只实现 `openai` 和 `deepseek` 两种 thinking_format**）
- `packages/ai/src/api/transform-messages.ts`（**完整实现**：thinking 跨模型、tool call ID 规范化、assistant 后置消息）

**写 Python**：
- `pi_ai/providers/openai_completions.py`
- 完善 `pi_ai/compat.py`、`pi_ai/transform_messages.py`

**验证点**：同一 `Agent` 切换 model 到 GPT/DeepSeek，对话仍能多轮 + 工具调用。如果需要改 `Models`/`Agent` 才能跑通，说明抽象有缺陷，回头修抽象而非打补丁。

**扩展点**：compat 是 per-model 配置（`Model.compat`），加新 OpenAI 兼容 provider 只加 catalog + 必要的 thinking_format 分支。

### 4.4 AgentHarness（可选，按需）

**学习目标**：harness 在 `Agent` 之上加会话、钩子、压缩、skills 的生产级封装。MVP 可用裸 `Agent` + 手动会话管理，但若要让 CLI 更完整，做一版精简 harness。

**读 TS**：`packages/agent/src/harness/agent-harness.ts`、`harness/types.ts`

**写 Python**：`pi_agent/harness.py` — `AgentHarness` 精简版：`prompt()`、`on(event, handler)` 钩子、turn_end 时 flush 会话写入、`compact()`、`navigate_tree()`。

**扩展点**：钩子事件类型表（`before_agent_start`/`context`/`tool_call`/`tool_result`/`session_*`）让应用层深度介入。

### Phase 4 自检清单
- [ ] JSONL 会话可持久化 + 重启恢复
- [ ] 长对话自动压缩生效
- [ ] 第二个 provider 不改核心即可接入
- [ ] （若做）harness 钩子能 block/override 工具

---

## 3. 跨阶段扩展性约定

为避免"先实现核心、后补扩展"时大改核心，从 Phase 1 起就遵守：

1. **抽象基类 + 注册表**：`Provider`、`CredentialStore`、`SessionStorage`、`SessionRepo`、`AgentTool` 都是抽象基类；`Models`、工具注册表是 dict 注册。新增 = 新子类 + 注册一行。
2. **配置而非分支**：provider 差异用 `Model.compat` 配置驱动（如 `thinking_format`），不在核心写 `if provider == "x"`。
3. **钩子即扩展点**：`AgentLoopConfig` 的 callable 钩子、`AgentHarness.on()` 事件钩子，让应用层介入而不改循环。
4. **类型联合可扩**：`AgentMessage`/`SessionTreeEntry` 用 Union，新类型加进 Union + 在 `convert_to_llm`/`build_context` 加一个分支。
5. **Result 而非异常**（可选但推荐）：FileSystem/Shell/Session 操作返回 `Result[T, E]`（用 `dataclass` 的 `ok` 标记），错误结构化不丢上下文。MVP 可先用异常，Phase 4 重构时再引入。

---

## 4. 推荐工程实践

- **环境**：`uv` + `pyproject.toml`；依赖只加 `anthropic`、`openai`、`pydantic`、`pyyaml`（Skill frontmatter）、`pytest`、`pytest-asyncio`。其余 provider SDK 等 Phase 4 再加。
- **测试**：每个模块配单测；agent 层用假 provider（参考 TS 的 `packages/ai/src/providers/faux.ts` 思路）避免真 API 消耗。`packages/coding-agent/test/suite/harness.ts` 是 TS 测试范式参考。
- **目录结构**：
  ```
  pi_ai/        types.py event_stream.py auth.py models.py simple_options.py
                transform_messages.py compat.py providers/anthropic.py ...
  pi_agent/     types.py messages.py agent_loop.py agent.py
                session/ compaction.py harness.py
  pi_coding_agent/  tools/ system_prompt.py skills.py truncate.py cli.py
  tests/        每个 package 一个子目录
  ```
- **对照阅读节奏**：每写一个 Python 文件，先读对应 TS 文件一遍；写完再读一遍查漏。TS 的命名（`streamSimple`/`agentLoop`/`convertToLlm`）转 Python 用 `stream_simple`/`agent_loop`/`convert_to_llm`。

---

## 5. 学习与实现对照速查表

| Python 模块 | 主参考 TS 文件 | 阶段 |
|------------|--------------|------|
| `pi_ai/types.py` | `ai/src/types.ts` | P1 |
| `pi_ai/event_stream.py` | `ai/src/utils/event-stream.ts` | P1 |
| `pi_ai/auth.py` | `ai/src/auth/*`、`env-api-keys.ts` | P1 |
| `pi_ai/models.py` | `ai/src/models.ts`、`providers/anthropic.ts` | P1 |
| `pi_ai/simple_options.py` | `ai/src/api/simple-options.ts` | P1 |
| `pi_ai/transform_messages.py` | `ai/src/api/transform-messages.ts` | P1 最小 / P4 完整 |
| `pi_ai/providers/anthropic.py` | `ai/src/api/anthropic-messages.ts` | P1 |
| `pi_ai/providers/openai_completions.py` | `ai/src/api/openai-completions.ts`、`compat.ts` | P4 |
| `pi_ai/compat.py` | `ai/src/compat.ts` | P4 |
| `pi_agent/types.py` | `agent/src/types.ts` | P2 |
| `pi_agent/messages.py` | `agent/src/harness/messages.ts` | P2 |
| `pi_agent/agent_loop.py` | `agent/src/agent-loop.ts` | P2 |
| `pi_agent/agent.py` | `agent/src/agent.ts` | P2 |
| `pi_agent/session/*` | `agent/src/harness/session/*` | P4 |
| `pi_agent/compaction.py` | `agent/src/harness/compaction/compaction.ts` | P4 |
| `pi_agent/harness.py` | `agent/src/harness/agent-harness.ts` | P4 可选 |
| `pi_coding_agent/tools/*` | `coding-agent/src/core/tools/*` | P3 |
| `pi_coding_agent/truncate.py` | `coding-agent/src/core/tools/truncate.ts`、`agent/src/harness/utils/truncate.ts` | P3 |
| `pi_coding_agent/system_prompt.py` | `coding-agent/src/core/system-prompt.ts`、`agent/src/harness/system-prompt.ts` | P3 |
| `pi_coding_agent/skills.py` | `agent/src/harness/skills.ts` | P3 最小 |
| `pi_coding_agent/cli.py` | `coding-agent/src/cli.ts`、`modes/print-mode.ts` | P3 |

---

## 6. 常见陷阱（按阶段）

- **P1**：想一次性实现 `transform_messages` 全部规则 → 拖慢进度。先最小子集，P4 再补全。
- **P1**：直接用 SDK 的原生消息类型当 pi 类型 → 后续跨 provider 会乱。严格用 pi 自己的 `Message`，SDK 类型只在 provider 内部边界转换。
- **P2**：并行工具执行时按完成序回灌 toolResult → LLM 看到的顺序不一致会出问题。必须按 assistant 源序排列 toolResult 消息。
- **P2**：`abort()` 不传播到正在跑的工具 → bash 工具会继续跑。`AgentTool.execute()` 要接收并响应取消信号。
- **P3**：忘了"先 Read 才能 Write/Edit"跨工具共享状态 → 用模块级 `set` 或注入一个共享 `ToolState` 对象。
- **P4**：压缩后忘了 `CompactionSummaryMessage` 进 `convert_to_llm` 的分支 → 上下文丢失。新消息类型加进 Union 时同步加转换分支。
- **全局**：为了快用 `Any` 填类型 → 后续重构代价大。宁可先写 `Protocol`/`dataclass` 占位。

---

## 7. 下一步

建议从 Phase 1.1 开始：读 `packages/ai/src/types.ts` 和 `utils/event-stream.ts`，写 `pi_ai/types.py` 和 `pi_ai/event_stream.py`，跑通假 provider 验证点。每完成一节回到本路线勾选自检清单。
