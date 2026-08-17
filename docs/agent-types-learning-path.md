# Agent 类型与消息转换学习路径（Phase 2.1）

> 对应学习路线：`learning-roadmap.md` Phase 2.1  
> TypeScript 源码：`packages/agent/src/types.ts`、`packages/agent/src/harness/messages.ts`  
> Python 实现：`pi_agent/types.py`、`pi_agent/messages.py`  
> 配套测试：`tests/test_agent_types.py`、`tests/test_messages.py`

本文不要求你先掌握 Agent 循环。读完后，你应该能解释：

1. `Message`（LLM 标准）和 `AgentMessage`（应用扩展）差在哪里。
2. 为什么每次调 LLM 前必须跑 `convert_to_llm`。
3. `AgentTool` 比 `pi_ai.Tool` 多了什么，为什么要多。
4. `AgentEvent` / `AgentLoopConfig` 各自服务哪一层。
5. 钩子结果（`BeforeToolCallResult` / `AfterToolCallResult`）的合并语义。
6. 加一种新自定义消息，最小要改哪两处。

---

## 1. 先建立整体心智模型

Phase 1（`pi_ai`）解决的是：**如何跟某个 LLM provider 流式对话**。

Phase 2（`pi_agent`）解决的是：**如何把「对话 + 工具 + 应用私有状态」组织成可循环的 Agent**。

2.1 只搭「数据契约」，不跑循环。可以把它想成：

```text
┌─────────────────────────────────────────────────────────────┐
│  应用层可见的对话历史 = list[AgentMessage]                    │
│    = LLM Message（user/assistant/toolResult）                │
│    + 自定义角色（bashExecution / custom / summary…）         │
└───────────────────────────┬─────────────────────────────────┘
                            │ convert_to_llm()
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  LLM 可见的对话历史 = list[Message]                          │
│    只有 user / assistant / toolResult                         │
│    自定义角色被「文本化成 user」或「过滤掉」                  │
└───────────────────────────┬─────────────────────────────────┘
                            │ 交给 Models.stream_simple（Phase 1）
                            ▼
                      AssistantMessage（可能含 toolCall）
```

两套消息并存的原因：

| 受众 | 需要什么 |
|------|----------|
| UI / 会话存储 / 压缩 | 完整历史：含 bash 执行记录、分支摘要、UI-only 通知 |
| LLM API | 严格协议：只有三种标准角色，内容必须可序列化 |

`convert_to_llm` 就是这两套世界之间的**单向适配器**（Agent → LLM）。反向不需要适配器：LLM 吐出的本来就是标准 `Message`。

---

## 2. 文件对应关系

| TypeScript | Python | 用途 |
|---|---|---|
| `packages/agent/src/types.ts` | `pi_agent/types.py` | 工具、状态、事件、循环配置、钩子 |
| `packages/agent/src/harness/messages.ts` | `pi_agent/messages.py` | 自定义消息 + `convert_to_llm` |
| （declaration merging） | `AgentMessage` Union | TS 用模块增强；Python 用显式联合 |
| TypeBox schema | `parameters: dict`（JSON Schema） | MVP 不强制 Pydantic；可后接 |

阅读顺序建议：

1. 先读本节第 3–5 部分（概念）。
2. 打开 `packages/agent/src/types.ts`，对照 `pi_agent/types.py` 扫一遍字段名（TS camelCase ↔ Python snake_case）。
3. 打开 `packages/agent/src/harness/messages.ts`，逐 case 对照 `convert_to_llm`。
4. 跑测试，改一两个断言验证自己的理解。

---

## 3. Message vs AgentMessage

### 3.1 LLM 标准消息（来自 `pi_ai`）

```python
Message = UserMessage | AssistantMessage | ToolResultMessage
```

| role | 谁产生 | 典型内容 |
|------|--------|----------|
| `user` | 用户 / 转换器 | 文本、图片 |
| `assistant` | 模型 | 文本、thinking、toolCall |
| `toolResult` | Agent 循环 | 工具执行结果，挂 `tool_call_id` |

这三种是 provider API 能吃的「合法食物」。

### 3.2 自定义消息（`pi_agent.messages`）

| role | 含义 | 进 LLM 时变成什么 |
|------|------|-------------------|
| `bashExecution` | 用户在 Agent 外跑的 shell | `user` 文本（命令+输出） |
| `custom` | 应用自定义（任意 `custom_type`） | `user`（字符串或 content 块） |
| `branchSummary` | 从某分支返回时的摘要 | `user` + `<summary>` 包装 |
| `compactionSummary` | 上下文压缩后的摘要 | `user` + compaction 包装 |

### 3.3 Python 的 Union vs TS 的 declaration merging

TS：

```typescript
export type AgentMessage = Message | CustomAgentMessages[keyof CustomAgentMessages];
// apps 通过 declare module 往 CustomAgentMessages 里塞新角色
```

Python 没有声明合并，所以直接写：

```python
AgentMessage = (
    Message
    | BashExecutionMessage
    | CustomMessage
    | BranchSummaryMessage
    | CompactionSummaryMessage
)
```

**扩展点**：新自定义角色 = 新 `@dataclass` + 把类型加进 `AgentMessage` + 在 `convert_to_llm` 加一个分支。后续可用「role → converter」注册表去掉 if/else，但 2.1 用显式分支更清晰。

### 3.4 小实验

在 REPL 或临时脚本里：

```python
from pi_ai.types import UserMessage, TextContent
from pi_agent.messages import create_custom_message, convert_to_llm

msgs = [
    UserMessage(content="hi", timestamp=1),
    create_custom_message("hint", "be brief", display=True, timestamp=2),
]
llm = convert_to_llm(msgs)
assert len(llm) == 2
assert all(m.role == "user" for m in llm)
print(llm[1].content[0].text)  # "be brief"
```

自检：若删掉 `convert_to_llm`，直接把 `CustomMessage` 塞进 `Context.messages`，provider 层会怎样？（答：序列化/角色校验失败，或静默丢弃——取决于 provider；反正不是契约允许的路径。）

---

## 4. convert_to_llm 精读

### 4.1 契约（写在 `AgentLoopConfig` 注释里）

- **必须不抛**：转换失败应跳过或返回安全回退，不能打断低层循环的事件序列。
- **可过滤**：UI-only、或 `exclude_from_context=True` 的 bash，返回「不产出 Message」。
- **可异步**（配置类型允许）：Python 签名是 `list[Message] | Awaitable[...]`；当前默认实现是同步的。

### 4.2 分支规则（与 TS 一一对应）

```text
bashExecution + exclude_from_context → 跳过
bashExecution → user{ bash_execution_to_text(...) }
custom        → user{ 字符串包成 TextContent，或原样 content 列表 }
branchSummary → user{ BRANCH_SUMMARY_PREFIX + summary + SUFFIX }
compactionSummary → user{ COMPACTION_SUMMARY_PREFIX + summary + SUFFIX }
user | assistant | toolResult → 原样透传
其它 role → 跳过
```

前缀/后缀常量必须与 TS 字符串**逐字一致**，否则压缩/分支摘要的提示语义会漂。

### 4.3 bash 文本化细节

`bash_execution_to_text` 不是随便 `repr`，而是给模型看的可读记录：

1. `Ran \`command\``
2. 有输出 → 围在 markdown 代码块；无输出 → `(no output)`
3. 取消 → `(command cancelled)`；非 0 退出码 → `Command exited with code N`
4. 截断且有 `full_output_path` → 提示完整输出路径

对照读：`packages/agent/src/harness/messages.ts` 的 `bashExecutionToText`。

### 4.4 和 `transform_context` 的分工（预告 2.2）

| 钩子 | 操作对象 | 典型用途 |
|------|----------|----------|
| `transform_context` | `AgentMessage[]` | 剪枝、注入外部上下文 |
| `convert_to_llm` | → `Message[]` | 角色适配、过滤 |

顺序固定：`transform_context` → `convert_to_llm` → `stream_fn`。2.1 只实现后者的默认函数；前者在 `AgentLoopConfig` 里留字段即可。

---

## 5. AgentTool：从「描述」到「可执行」

### 5.1 两层工具

| 类型 | 所在包 | 能力 |
|------|--------|------|
| `pi_ai.Tool` | pi_ai | 名字、描述、JSON Schema——只够告诉 LLM「有什么工具」 |
| `pi_agent.AgentTool` | pi_agent | 上述 + `label` + `prepare_arguments` + `execute` + `execution_mode` |

LLM 只看见 schema；真正跑代码的是 `execute`。

### 5.2 为什么不用继承 Tool dataclass

Python 的 `@dataclass` 与 `ABC` 叠在一起容易别扭。MVP 做法：

- `AgentTool` 自己持有 `name` / `description` / `parameters`
- 需要塞进 `pi_ai.Context.tools` 时调用 `as_tool()` 投影

### 5.3 `execute` 契约

```python
async def execute(
    self,
    tool_call_id: str,
    params: dict[str, Any],
    signal: AbortSignal | None = None,
    on_update: AgentToolUpdateCallback | None = None,
) -> AgentToolResult[Any]:
```

- **失败用抛异常**，不要把错误塞进 `content` 假装成功（循环层会捕获并生成 error 型 `toolResult`）。
- `on_update`：执行中途推送部分结果（给 UI）；promise settle 之后再调应被忽略（2.2 实现）。
- `terminate=True`：提示「本批工具结束后停」；**仅当本批所有 finalized 结果都为 True 时才真停**（2.2）。

### 5.4 `prepare_arguments`

在 schema 校验前的兼容垫片（例如把旧字段名改成新字段）。默认：要求 `dict` 并原样返回。

### 5.5 最小工具示例

见 `tests/test_agent_types.py` 的 `EchoTool`：

```python
class EchoTool(AgentTool):
    def __init__(self) -> None:
        super().__init__(
            name="echo",
            label="Echo",
            description="Echo a string",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    async def execute(self, tool_call_id, params, signal=None, on_update=None):
        return AgentToolResult(
            content=[TextContent(text=str(params["text"]))],
            details=None,
        )
```

自检：若 `parameters` 里写了 `required: ["text"]`，但模型传来 `{}`，谁负责报错？（答：2.2 的 `prepare_tool_call` / 校验层，不是 `execute` 本身——`execute` 假定已校验。）

---

## 6. AgentState / AgentContext / AgentEvent

### 6.1 AgentContext vs AgentState

| | `AgentContext` | `AgentState` |
|--|----------------|--------------|
| 用途 | 喂给**单次**低层循环的快照 | Agent 类长期持有的可变状态 |
| 字段 | system_prompt、messages、tools | 上述 + model、thinking_level + 运行时只读字段 |
| 生命周期 | 一次 `agent_loop` 调用 | 跨多次 `prompt()` |

运行时只读字段（由 2.3 的 Agent 更新）：

- `is_streaming`：是否在跑（含 `agent_end` 监听器 settle 前）
- `streaming_message`：当前流式半成品
- `pending_tool_calls`：正在执行的 toolCall id
- `error_message`：最近失败/中止原因

### 6.2 tools / messages 赋值拷贝

与 TS accessor 一致：`state.tools = xs` 会 `list(xs)`，避免调用方继续 mutate 原列表影响内部。测试见 `test_agent_state_copies_tools_and_messages_on_assign`。

### 6.3 AgentEvent 生命周期（先认名字）

```text
agent_start
  turn_start
    message_start → (message_update)* → message_end   # 常为 assistant 流
    tool_execution_start → (update)* → tool_execution_end
    ...
  turn_end
  ...
agent_end
```

2.1 只定义事件类型；2.2 才真正 `push` 它们。学的时候记住：**UI 订阅的是 AgentEvent，不是 AssistantMessageEvent**。后者被包在 `message_update.assistant_message_event` 里往上透。

---

## 7. AgentLoopConfig 与钩子

### 7.1 必填 vs 选填

| 字段 | 必填？ | 作用 |
|------|--------|------|
| `model` | 是 | 本轮（及默认后续）用的模型 |
| `convert_to_llm` | 是 | AgentMessage → Message |
| `stream_fn` | 否（2.2 可默认 Models.stream_simple） | 实际流式调用 |
| `transform_context` | 否 | 转换前改写 AgentMessage 列表 |
| `get_api_key` | 否 | 每轮动态取 key（长工具执行后 token 过期） |
| `before_tool_call` / `after_tool_call` | 否 | 拦截/改写工具结果 |
| `should_stop_after_turn` | 否 | turn_end 后优雅停 |
| `prepare_next_turn` | 否 | 替换下一轮 context/model/thinking |
| `get_steering_messages` / `get_follow_up_messages` | 否 | 中途转向 / 收尾追问 |
| `tool_execution` | 默认 `"parallel"` | 并行或顺序执行工具 |

### 7.2 Before / After 合并语义

**before**：`{ block: True, reason? }` → 不执行，循环发一条 error 风格的 toolResult。

**after**：字段级覆盖，**无深合并**：

- 提供了 `content` → 整表替换
- 提供了 `details` → 整值替换
- 提供了 `is_error` / `terminate` → 替换该标志
- 未提供的字段保持 `execute` 原始结果

### 7.3 钩子契约

所有钩子与 `convert_to_llm` 一样：**不要抛**。抛异常会让低层循环来不及走完整事件序列（没有正常的 `agent_end` 等）。

---

## 8. 阶段练习（建议按序）

| 阶段 | 做什么 | 完成标准 |
|------|--------|----------|
| **A** | 精读 TS `types.ts` 的 AgentTool / AgentEvent / AgentLoopConfig | 能用自己的话区分 Tool 与 AgentTool |
| **B** | 精读 TS `messages.ts`，手画 convert 分支图 | 图上含「跳过」边 |
| **C** | 对照 Python，标出 snake_case 与语义差异 | 列出 ≥3 处命名对应 |
| **D** | 跑 `pytest tests/test_agent_types.py tests/test_messages.py` | 全绿 |
| **E** | 自写一个 `AddTool(a,b)`，只测 execute | 返回文本 `"a+b=..."` |
| **F** | 造一条含 bash + custom + user 的列表，断言 convert 后角色全是 LLM 合法角色 | 顺序与过滤正确 |

---

## 9. 常见误区

1. **把 AgentMessage 直接当 Context.messages**  
   `pi_ai.Context` 只要 `Message`。必须先 `convert_to_llm`。

2. **在 convert 里抛错「表示非法消息」**  
   违反契约。应跳过或降级为安全 user 文本。

3. **以为 `CustomMessage.display` 影响 convert**  
   `display` 是给 UI 的；convert 始终把 custom 变成 user（TS 如此）。

4. **`exclude_from_context` 写在 CustomMessage 上**  
   TS 只给了 bashExecution。其它角色要过滤，应在自己的 convert 分支或 `transform_context` 里处理。

5. **AgentToolResult.terminate 单独为 True 就停**  
   必须本批全部 True（2.2）。2.1 只保留字段。

6. **混用 thinking_level**  
   Agent 侧含 `"off"`；`pi_ai` 的流选项用 `reasoning` 且通常不含 off。夹层转换在 2.2/2.3。

---

## 10. 与下一阶段的接口

2.1 结束后，2.2 会消费这些类型：

```text
agent_loop(prompts, context, config)
  → transform_context(messages)
  → convert_to_llm(messages)          # 你刚实现的
  → stream_fn(model, Context(...))  # Phase 1
  → 解析 toolCall → AgentTool.execute
  → 发射 AgentEvent
```

你现在不需要实现循环；但实现时若发现类型不够用，优先**改类型契约**再写循环，而不是在循环里塞临时 `dict`。

---

## 11. 自检问题（写完再对答案）

1. 为什么 `AgentMessage` 不能等于 `Message`？  
2. `convert_to_llm` 对 `assistant` 消息做什么？  
3. `as_tool()` 丢弃了哪些字段？为什么可以丢？  
4. `AfterToolCallResult` 只设 `is_error=True` 时，`content` 会怎样？  
5. 新增 `role="notification"` 且永不送进 LLM，最小改动是什么？

参考答案（折叠思考）：

1. 应用需要存 UI/会话专用角色；LLM 协议不允许这些 role。  
2. 原样透传（已是合法 Message）。  
3. `label` / `execute` / `execution_mode` / `prepare_arguments`——LLM 不需要执行逻辑。  
4. 保持 execute 返回的 content，只把错误标志改为 True。  
5. 新 dataclass + 加入 Union；在 convert 里对该 role 返回 `None`（或根本不加入 Union 而在 transform 阶段丢掉）。

---

## 12. 验证命令

```bash
uv run pytest tests/test_agent_types.py tests/test_messages.py -v
```

全部通过即完成本阶段代码侧验收。概念侧验收：能不看文档画出「AgentMessage → convert_to_llm → Message → stream」箭头图，并向他人讲清为什么中间必须有一步转换。
