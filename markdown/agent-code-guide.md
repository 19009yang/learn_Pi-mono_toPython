# Agent 代码解读

> 针对 `pi_agent/agent.py` 的架构与设计解读文档。

---

## 1. 架构概览

`agent.py` 是 pi 项目中 **Agent 层**的 Python 实现，对应 TS 版本的 `packages/agent/src/agent.ts`。它是一个**有状态的高层包装器**，将底层 `agent_loop`（裸循环逻辑）封装为具备完整生命周期管理的对象。

核心定位：**Agent 不是循环引擎本身，而是循环引擎的"管家"** —— 它管理状态、调度消息、协调取消、通知监听器，但实际的 LLM 调用和工具执行逻辑在 `agent_loop` 中完成。

```
┌─────────────────────────────────────────────────────┐
│                      Agent                          │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  State   │  │ Queues   │  │    Listeners     │  │
│  │(AgentState)│ │(steering │  │(subscribe/unsub) │  │
│  │          │  │ follow_up)│  │                  │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
│                                                     │
│  ┌──────────────────────────────────────────────┐   │
│  │           ActiveRun (运行时追踪)              │   │
│  │  future + AbortSignal + resolve_future       │   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│            ↓ 委托执行 ↓                              │
│  ┌──────────────────────────────────────────────┐   │
│  │              agent_loop                       │   │
│  │  (底层循环：LLM 调用 + 工具执行 + 事件发射)    │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

---

## 2. 核心类一览

### 2.1 PendingMessageQueue — 消息等待队列

| 属性/方法 | 说明 |
|-----------|------|
| `mode` | Drain 模式：`"all"`（一次性取出所有）或 `"one-at-a-time"`（每次仅取一条） |
| `enqueue()` | 将消息加入队列尾部 |
| `drain()` | 按 mode 取出并移除消息 |
| `clear()` | 清空队列 |

**设计要点**：两种 drain 模式对应两种不同的消息注入策略：
- **steering 队列**默认 `"one-at-a-time"` — 每轮 LLM 调用前仅注入一条 steering 消息，避免一次性塞入过多用户干预导致上下文混乱
- **follow-up 队列**也默认 `"one-at-a-time"` — agent 本应停止后，逐条处理后续消息

### 2.2 ActiveRun — 活跃运行追踪

| 字段 | 类型 | 说明 |
|------|------|------|
| `future` | `asyncio.Future[None]` | 运行完成时 resolve，`wait_for_idle()` 等待它 |
| `signal` | `AbortSignal` | 本次运行的中止信号 |
| `_resolve_future` | `Callable` | 内部回调，`finish_run()` 中调用以 resolve future |

**设计要点**：这映射了 TS 中的 `Promise + resolve + AbortController` 模式。Python 没有 TS 的 `new Promise((resolve) => ...)` 语法，因此通过 `asyncio.Future` + 提取 `resolve_future` 回调来实现同等效果。

### 2.3 AgentOptions — 构造选项

`AgentOptions` 是一个 dataclass，将 Agent 的所有可配置项集中管理：

| 类别 | 字段 | 说明 |
|------|------|------|
| 初始状态 | `initial_state` | 初始 AgentState 字典（不含运行时字段） |
| 核心函数覆写 | `convert_to_llm`, `transform_context`, `stream_fn`, `get_api_key` | 替换默认的 LLM 交互逻辑 |
| 钩子回调 | `before_tool_call`, `after_tool_call`, `prepare_next_turn`, `prepare_next_turn_with_context` | 工具调用前后的拦截点 |
| 队列模式 | `steering_mode`, `follow_up_mode` | 控制消息 drain 策略 |
| 流式/提供商选项 | `session_id`, `thinking_budgets`, `transport`, `max_retry_delay_ms`, `tool_execution` | 转发到 `SimpleStreamOptions` |

### 2.4 Agent — 主类

Agent 是整个文件的核心，包含以下职责区域：

| 区域 | 说明 |
|------|------|
| 状态管理 | `_state` (AgentState) — 持有 system_prompt、model、tools、messages |
| 核心函数 | `convert_to_llm`, `transform_context`, `stream_fn`, `get_api_key` |
| 钩子 | `before_tool_call`, `after_tool_call`, `prepare_next_turn` 系列 |
| 队列 | `_steering_queue`, `_follow_up_queue` (PendingMessageQueue) |
| 运行时 | `_active_run` (ActiveRun) — 仅在活跃运行期间存在 |
| 监听器 | `_listeners` (set) — 生命周期事件订阅者 |

---

## 3. 生命周期详解

### 3.1 完整流程图

```
用户调用 prompt() / continue_loop()
    │
    ├─ prompt(): _normalize_prompt_input() → _run_prompt_messages()
    │   └─ continue_loop(): 检查最后消息类型 → _run_continuation()
    │
    ▼
_run_with_lifecycle(executor)
    │
    ├─ 检查：已有 ActiveRun？→ 抛出 RuntimeError
    │
    ├─ 创建 ActiveRun (AbortSignal + Future)
    │
    ├─ 设置 streaming 状态标志
    │
    ├─ 运行 executor(signal)
    │   │
    │   └─ executor 内部：
    │       ├─ 调用 run_agent_loop / run_agent_loop_continue
    │       ├─ 每个事件 → _process_events()
    │       │   ├─ 状态归约（更新 _state）
    │       │   └─ 通知监听器 (event, signal)
    │       ├─ steering/follow-up 队列 drain
    │       └─ 工具执行钩子调用
    │
    ├─ [异常] → _handle_run_failure()
    │   ├─ 生成合成 AssistantMessage（stop_reason: "aborted"/"error"）
    │   ├─ 发出 message_start → message_end → turn_end → agent_end
    │   └─ 监听器看到完整（但失败的）生命周期
    │
    ▼ (finally)
_finish_run()
    ├─ is_streaming → False
    ├─ streaming_message → None
    ├─ pending_tool_calls → 清空
    ├─ resolve idle Future
    └─ active_run → None
    │
    ▼
wait_for_idle() 返回 → Agent 空闲，可接受下一次 prompt()
```

### 3.2 事件类型与状态归约

`_process_events` 是底层 agent_loop 与高层 Agent 状态之间的桥梁。每种事件触发不同的状态更新：

| 事件类型 | 状态更新 |
|----------|----------|
| `message_start` | 设置 `_state.streaming_message` |
| `message_update` | 更新 `_state.streaming_message` |
| `message_end` | 清除 `streaming_message`，追加消息到 `state.messages` |
| `tool_execution_start` | 将 `tool_call_id` 加入 `pending_tool_calls` |
| `tool_execution_end` | 将 `tool_call_id` 从 `pending_tool_calls` 移除 |
| `turn_end` | 检查 assistant 消息的 `error_message`，如有则记录 |
| `agent_end` | 清除 `streaming_message` |

**关键理解**：`agent_end` ≠ 运行结束。`agent_end` 仅表示不再发出循环事件。运行被认为"空闲"是在 `finish_run()` 之后 — 所有监听器已 settle、运行时状态已清除。

---

## 4. 消息注入机制

Agent 提供两种消息注入方式，对应不同的注入时机：

### 4.1 Steering — 运行中注入

```python
agent.steer(message)
```

- **时机**：当前 assistant 轮次之后、下一次 LLM 调用之前
- **Drain 位置**：内循环每次迭代开头（通过 `_create_loop_config` 中的 `get_steering_messages`）
- **默认模式**：`"one-at-a-time"` — 每次迭代仅注入一条
- **场景**：用户想在 agent 正在思考/执行时插入新指令（如"换个方向试试"）

### 4.2 Follow-up — 运行后注入

```python
agent.follow_up(message)
```

- **时机**：内循环退出后（无更多工具调用 + 无待处理 steering）
- **Drain 位置**：外循环，通过 `get_follow_up_messages`
- **默认模式**：`"one-at-a-time"`
- **场景**：agent 本应结束，但用户追加"再检查一下结果"

### 4.3 skip_initial_steering_poll 机制

当 `continue_loop()` 检测到最后一条消息是 assistant 时，它会先 drain steering 队列来创建合法续接点。但这时 drain 的 steering 消息不应被 agent_loop 的首轮再次 drain — 否则同一批消息会被重复注入。

解决方案：`_create_loop_config` 中使用可变容器 `[skip_initial_steering_poll]`（list 包含一个 bool），lambda 可以修改列表元素而非重新绑定局部变量。首次调用 `get_steering_messages` 时若 skip 标志为 True，返回空列表并清除标志；后续调用正常 drain。

```python
_skip_flag = [skip_initial_steering_poll]

async def get_steering_messages() -> list[AgentMessage]:
    if _skip_flag[0]:
        _skip_flag[0] = False
        return []
    return self._steering_queue.drain()
```

这映射了 TS 中 `let skipInitialSteeringPoll` 被 lambda 捕获的行为。Python 闭包捕获值而非引用，因此需要可变容器来模拟 TS 的 `let` 变量修改。

---

## 5. Abort 与取消机制

### 5.1 AbortSignal

Python 版本使用自定义 `AbortSignal`（来自 `pi_ai.event_stream`），而非 DOM 的 `AbortController/AbortSignal`。

- `AbortSignal.abort()` → 设置内部 `asyncio.Event` → 协作式取消
- agent_loop 和工具执行中检查 `signal.aborted` → 主动退出

### 5.2 abort() 流程

```python
agent.abort()  # → active_run.signal.abort()
```

调用 `abort()` 后：
1. AbortSignal 被触发
2. agent_loop 中的协作式检查检测到取消
3. 运行抛出异常（或正常退出）
4. `_handle_run_failure` 生成 stop_reason="aborted" 的合成消息
5. 发出完整的失败生命周期事件序列
6. `_finish_run` 清除运行时状态

---

## 6. 与 TS 版本的设计差异

| 差异点 | TS 版本 | Python 版本 | 原因 |
|--------|---------|-------------|------|
| Abort 机制 | DOM `AbortController/AbortSignal` | 自定义 `AbortSignal`（内部用 `asyncio.Event`) | Python 无 DOM API |
| PendingMessageQueue | 原生数组 `[]` | `list` | 等效，无实际差异 |
| ActiveRun | `{promise, resolve, abortController}` | `{future, resolve_future, signal}` | Python 用 `asyncio.Future` 替代 Promise |
| Promise 构造 | `new Promise((resolve) => ...)` | `Future` + 提取 `resolve_future` 回调 | Python 无 Promise 构造语法 |
| 闭包可变性 | `let` 变量被 lambda 读写 | 可变容器 `[bool]` 被 lambda 修改 | Python 闭包捕获值而非引用 |
| convert_to_llm 默认 | `defaultConvertToLlm`（按角色过滤） | `convert_to_llm`（处理自定义消息类型） | Python 版需要支持更多 AgentMessage 子类型 |
| 事件监听器存储 | 数组 + splice | `set`（Python 3.7+ 保持插入顺序） | 需要高效的 add/remove |
| 事件发射 | 直接 import 各事件类 | `_make_event()` 按类型字符串动态构造 | 避免模块顶层过多 import |

---

## 7. 关键设计决策

### 7.1 写时复制（Copy-on-Write）

`AgentState` 的 `tools` 和 `messages` setter 在赋值时复制顶层列表：

```python
self._state.tools = initial.get("tools")  # → 复制
self._state.messages = initial.get("messages")  # → 复制
```

这防止外部通过保留旧引用来修改 Agent 内部存储。与 TS 的 `MutableAgentState` getter/setter 语义一致。

### 7.2 上下文快照

`_create_context_snapshot()` 对 messages 和 tools 做浅拷贝（切片），使得 agent_loop 可以追加消息而不修改 state，直到 `message_end` 事件正式更新 `_state.messages`。

```python
AgentContext(
    system_prompt=self._state.system_prompt,
    messages=self._state.messages[:],  # 浅拷贝
    tools=self._state.tools[:],        # 浅拷贝
)
```

### 7.3 单线程异步模型

Agent **不是线程安全的**，设计为单 asyncio 任务使用：
- 不要从多个并发任务调用 `prompt()`
- 需要注入消息时使用 `steer()` 或 `follow_up()`
- 需要等待完成时使用 `wait_for_idle()`

### 7.4 失败时合成完整生命周期

`_handle_run_failure` 不只是抛异常 — 它生成一条合成的 assistant 消息，然后发出 `message_start → message_end → turn_end → agent_end` 的完整事件序列。这确保监听器始终看到完整的生命周期，即使运行失败了。

---

## 8. 方法调用关系图

```
Agent
 ├─ prompt(input, images)
 │   ├─ _normalize_prompt_input()
 │   └─ _run_prompt_messages()
 │       └─ _run_with_lifecycle()
 │           ├─ _create_active_run()
 │           ├─ _create_context_snapshot()
 │           ├─ _create_loop_config()
 │           │   ├─ 组合 prepare_next_turn 钩子
 │           │   ├─ get_steering_messages lambda
 │           │   └─ get_follow_up_messages → _async_follow_up_drain
 │           ├─ run_agent_loop (外部)
 │           ├─ _process_events()
 │           │   ├─ 状态归约
 │           │   └─ 通知 _listeners
 │           ├─ _handle_run_failure() [异常时]
 │           └─ _finish_run() [finally]
 │
 ├─ continue_loop()
 │   ├─ 检查最后消息类型
 │   ├─ 尝试 drain steering/follow-up
 │   └─ _run_continuation()
 │       └─ _run_with_lifecycle() → 同上
 │
 ├─ steer(message) → _steering_queue.enqueue()
 ├─ follow_up(message) → _follow_up_queue.enqueue()
 ├─ abort() → active_run.signal.abort()
 ├─ wait_for_idle() → await active_run.future
 ├─ reset() → 清空状态和队列
 ├─ subscribe(listener) → _listeners.add()
 └─ unsubscribe() → _listeners.discard()
```

---

## 9. 数据流向

```
                    用户输入
                       │
                       ▼
              prompt() / steer() / follow_up()
                       │
                       ▼
             ┌─ 消息队列 ──────────────────┐
             │  steering_queue │ follow_up_queue │
             └───────────────────────────────┘
                       │ drain
                       ▼
              agent_loop (底层循环)
                       │
            ┌──────────┼──────────┐
            │          │          │
     LLM 调用    工具执行    事件发射
            │          │          │
            └──────────┼──────────┘
                       │
                       ▼
              _process_events()
                       │
            ┌──────────┼──────────┐
            │                     │
     状态归约(_state)      通知监听器(listeners)
            │                     │
            ▼                     ▼
      AgentState 更新       外部订阅者收到 (event, signal)
            │
            ▼
       finish_run() → Agent 空闲
```

---

## 10. 总结

`agent.py` 的核心设计哲学是 **"管家模式"**：

1. **不自己干活**：LLM 调用和工具执行委托给 `agent_loop`
2. **管理一切周边**：状态、队列、取消、事件、生命周期
3. **保证完整性**：即使失败也合成完整生命周期事件序列
4. **忠实移植**：尽量保持与 TS 版本相同的语义和行为，仅在 Python 语法限制处做适配（如闭包可变性、Future vs Promise）

这种分层设计使得底层循环逻辑保持纯粹（只关注"怎么跑"），而 Agent 层关注"什么时候跑、跑什么、出了问题怎么办"——职责清晰，便于独立测试和替换。
