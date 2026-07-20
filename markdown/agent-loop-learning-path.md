# Agent 核心循环引擎学习路径（Phase 2.2）

> 对应学习路线：`learning-roadmap.md` Phase 2.2  
> TypeScript 源码：`packages/agent/src/agent-loop.ts`（**全文件精读**）  
> Python 实现：`pi_agent/agent_loop.py`、`pi_ai/validation.py`  
> 配套测试：`tests/test_agent_loop.py`

读完后，你应该能解释：

1. 外层 / 内层 `while` 各自处理什么（follow-up vs toolCalls + steering）。
2. `stream_assistant_response` 如何把 Agent 消息转成 LLM 请求并转发流式事件。
3. 工具执行的 preflight → execute → finalize 三阶段，以及并行 vs 顺序的差异。
4. 为什么 `stop_reason == "length"` 时所有 toolCall 必须失败而不执行。
5. `agent_loop` 与 `agent_loop_continue` 的入口差异。
6. `new_messages` 与 `context.messages` 各自代表什么。

---

## 1. 整体心智模型

Phase 2.1 搭好了**数据契约**（类型 + `convert_to_llm`）。  
Phase 2.2 是**行为引擎**：把契约跑起来。

```text
agent_loop(prompts, context, config)
  │
  ├─ 同步返回 EventStream[AgentEvent, list[AgentMessage]]
  └─ 后台 asyncio task:
       run_agent_loop → _run_loop（双层 while）
         ├─ stream_assistant_response（调 LLM）
         ├─ execute_tool_calls（跑工具）
         ├─ steering / follow-up 队列注入
         └─ 钩子：prepare_next_turn / should_stop_after_turn
```

与 TS 一致：**AgentMessage 贯穿全程**，只在调 LLM 前 `convert_to_llm`。

---

## 2. 文件对应关系

| TypeScript | Python | 用途 |
|---|---|---|
| `agent-loop.ts` | `agent_loop.py` | 循环、流式、工具执行 |
| `validateToolArguments` | `pi_ai/validation.py` | 工具参数 JSON Schema 校验（MVP 子集） |
| `EventStream<AgentEvent, ...>` | `EventStream` | 同步返回、后台填充 |
| `agent-loop.test.ts` | `test_agent_loop.py` | 假 stream_fn + 假工具 |

---

## 3. 入口：`agent_loop` vs `agent_loop_continue`

### 3.1 `agent_loop`

```python
stream = agent_loop([user_msg], context, config, stream_fn=fake_stream)
async for event in stream:
    ...
new_messages = await stream.result()
```

流程：

1. 创建 `EventStream`，`asyncio.create_task` 跑后台循环。
2. `run_agent_loop`：把 `prompts` 追加到 `context.messages`，对每条 prompt 发 `message_start/end`。
3. 进入 `_run_loop`。
4. 结束时 `stream.end(new_messages)`。

`new_messages` = **本次 run 新增**的消息（prompt + assistant + toolResult + …），不含 run 之前 context 里已有的。

### 3.2 `agent_loop_continue`

用于 retry：context 里已有 user 或 toolResult，**不再**注入新 prompt。

约束（与 TS 相同）：

- `context.messages` 不能为空。
- 最后一条不能是 `assistant`（否则 provider 无法接话）。

```python
# 合法：最后是 toolResult
context.messages = [user, assistant(toolCall), toolResult]
stream = agent_loop_continue(context, config, stream_fn=...)
```

**注意**：`continue` 时 `current_context.messages` 与传入的 `context.messages` **共享同一 list**（浅拷贝），assistant 响应会 append 到原 context。

---

## 4. 双层 while：`_run_loop`

### 4.1 结构

```text
pending_messages ← get_steering_messages()（循环开始前 + 每 turn 后）

外层 while True:                    # follow-up 驱动
  has_more_tool_calls = True
  内层 while has_more_tool_calls or pending_messages:
    ① 若有 pending_messages → 注入 context，发 message 事件
    ② stream_assistant_response → AssistantMessage
    ③ 若 stop_reason 为 error/aborted → turn_end + agent_end，退出
    ④ 若有 toolCall → execute_tool_calls → toolResult 入 context
    ⑤ turn_end
    ⑥ prepare_next_turn / should_stop_after_turn
    ⑦ pending_messages ← get_steering_messages()
    ⑧ has_more_tool_calls = not batch.terminate（有 tool 且未全 terminate）

  follow_up ← get_follow_up_messages()
  若有 follow_up → pending_messages = follow_up，continue 外层
  否则 break

agent_end
```

### 4.2 关键变量

| 变量 | 含义 |
|------|------|
| `has_more_tool_calls` | 内层是否还要再调 LLM（通常因为刚执行完 tool 且未 terminate） |
| `pending_messages` | steering：在**下一次** assistant 响应**之前**插入 |
| `follow_up_messages` |  agent 本来要停时，外层再拉一轮 |

### 4.3 `terminate` 语义

工具结果上的 `terminate=True` 表示「希望本批结束后停」。  
**只有本批所有 finalized 结果都为 `terminate=True` 时**，`has_more_tool_calls` 才变 false，内层才停。

---

## 5. `stream_assistant_response`

顺序固定：

```text
transform_context(messages)     # 可选，仍操作 AgentMessage[]
    ↓
convert_to_llm(messages)        # → Message[]
    ↓
构建 pi_ai.Context（tools 用 tool.as_tool()）
    ↓
stream_fn(model, context, options)
    ↓
async for event in response:
  start        → message_start，partial 入 context.messages
  *\_delta/... → message_update（AssistantMessageEvent 原样透传）
  done/error   → message_end，返回最终 AssistantMessage
```

`stream_fn` 契约（与 Phase 1 相同）：

- 不抛异常表示请求失败；失败编码在流里的 error/aborted message。
- 测试里用 `MockAssistantStream` + `call_soon` 模拟异步 provider。

`reasoning` 映射：Agent 侧 `"off"` → 传给 provider 时用 `None`。

---

## 6. 工具执行管线

### 6.1 `prepare_tool_call`

```text
找工具 → prepare_arguments → validate_tool_arguments
    → before_tool_call（可 block）
    → PreparedToolCall 或 ImmediateToolCallOutcome（错误/拦截）
```

校验失败、工具不存在、hook block：走 **immediate** 路径，不调用 `execute`。

### 6.2 `execute_tool_calls` 模式选择

```python
if config.tool_execution == "sequential" or any(tool.execution_mode == "sequential"):
    sequential(...)
else:
    parallel(...)
```

### 6.3 顺序执行

对每个 toolCall（assistant 源序）：

```text
tool_execution_start
→ prepare → execute → finalize(after_tool_call)
→ tool_execution_end
→ toolResult message_start/end
```

### 6.4 并行执行（重点）

```text
对每个 toolCall（顺序 preflight）:
  tool_execution_start
  prepare
  若 immediate → tool_execution_end（立刻）
  否则 → 登记 async 任务（内部 execute + finalize + tool_execution_end）

await 所有任务（execute 并行）
按 assistant 源序 emit toolResult message
```

| 事件 | 顺序 |
|------|------|
| `tool_execution_end` | **完成序**（谁先跑完谁先 end） |
| `toolResult` 消息 | **源序**（与 assistant 里 toolCall 排列一致） |

### 6.5 `stop_reason == "length"`

输出被 token 上限截断时，toolCall 参数可能「看起来合法但不完整」。  
**全部**走 `_fail_tool_calls_from_truncated_message`，不调用 `execute`，返回 error toolResult，循环继续以便模型重发。

---

## 7. 钩子在本阶段的触达点

| 钩子 | 触发时机 |
|------|----------|
| `transform_context` | 每次 LLM 请求前 |
| `convert_to_llm` | transform 之后（必填） |
| `before_tool_call` | prepare 校验后、execute 前 |
| `after_tool_call` | execute 后、tool_execution_end 前 |
| `prepare_next_turn` | turn_end 后 |
| `should_stop_after_turn` | prepare 之后；True 则 agent_end |
| `get_steering_messages` | 循环开始 + 每 turn 后 |
| `get_follow_up_messages` | 内层循环退出后、外层 break 前 |

契约：**钩子不应抛异常**（与 2.1 相同）。

---

## 8. 动手实验（建议顺序）

### 实验 A：纯文本一轮

跑 `test_agent_loop_text_only`，对照事件类型集合。

### 实验 B：两轮工具（roadmap 验证点）

读 `test_agent_loop_two_round_tool_calls`：

- 第 1 次 `stream_fn` 返回 `toolUse` + `ToolCall`
- 第 2 次返回纯文本 `stop`
- 断言 `messages` 角色序列：`user → assistant → toolResult → assistant`

### 实验 C：length 截断

`test_length_truncated_tool_calls_not_executed`：`EchoTool.executed` 必须为空。

### 实验 D：并行源序

`test_parallel_tool_results_in_source_order`：两个 toolResult 的 `tool_call_id` 仍为 t1、t2。

### 实验 E：自写假 stream_fn

在 REPL 或临时脚本里：

```python
import asyncio
from pi_agent import agent_loop, AgentContext, AgentLoopConfig, convert_to_llm
# 注入 stream_fn，打印 async for 的 event.type
```

---

## 9. 常见误区

1. **把 `context.messages` 当 LLM 消息**  
   只有 `convert_to_llm` 之后的 `Message[]` 才能给 provider。

2. **以为 `agent_loop_continue` 会执行 pending toolCall**  
   continue 只从「可接话的末尾」继续调 LLM；未执行的 toolCall 需先由别的方式产生 toolResult。

3. **混淆 `new_messages` 与 `context.messages`**  
   `new_messages` 仅本次 run 增量；`context.messages` 是完整 transcript（continue 时与传入 list 共享引用）。

4. **并行模式下 tool_execution_end 顺序 = toolResult 顺序**  
   错。end 是完成序，toolResult 是源序。

5. **单个 `terminate=True` 就停**  
   必须整批都为 True。

---

## 10. 与 2.3 的衔接

2.3 的 `Agent` 类会在本引擎外包一层：

- 持有 `AgentState`
- `subscribe()` 监听 `AgentEvent`
- `steer()` / `follow_up()` 实现 `get_steering_messages` / `get_follow_up_messages`
- `abort()` → `AbortSignal`

你现在可以直接用裸 `agent_loop` 验证工具闭环；2.3 只是状态 + 队列 + 生命周期的糖。

---

## 11. 自检问题

1. 内层循环何时退出？外层何时 `continue`？  
2. `stream_assistant_response` 里 partial message 何时 `append`、何时 `replace` 最后一项？  
3. `before_tool_call` 返回 `{block: True}` 后，还会调 `execute` 吗？  
4. 为什么 parallel 的 preflight 仍要顺序做？  
5. `agent_end.messages` 是什么？

参考答案：

1. 内层：无更多 tool 且 pending steering 空；外层：有 follow-up 则 continue。  
2. `start` 事件 append；后续 delta 更新 `messages[-1]`；done 时写回 final。  
3. 不会，走 immediate error toolResult。  
4. 参数校验与 before hook 有副作用，且需 deterministic 拦截顺序。  
5. 本次 run 的 `new_messages` 列表。

---

## 12. 验证命令

```bash
uv run pytest test/test_agent_loop.py test/test_agent_types.py -v
```

全部通过即完成 Phase 2.2 代码验收。
