# `pi_agent/agent_loop.py` 信息流通流程图

---

## 1. 宏观数据流：两条贯穿全程的管道

```
┌─────────────────────────────────────────────────────────────────┐
│  整个 agent_loop 有两条核心数据管道，所有操作都围绕它们展开：      │
│                                                                 │
│  ┌──────────────────────┐    ┌──────────────────────┐           │
│  │  current_context     │    │  new_messages        │           │
│  │  .messages           │    │  (本次 run 增量)      │           │
│  │                      │    │                      │           │
│  │  LLM 每轮读取它      │    │  AgentEndEvent 返回它 │           │
│  │  → stream_fn 输入    │    │  → 调用方拿到它       │           │
│  └──────────────────────┘    └──────────────────────┘           │
│                                                                 │
│  关系：每条新增消息都「双写」到两个管道                           │
│  - context.messages.append(m) + new_messages.append(m)          │
│                                                                 │
│  continue 模式：context.messages 与传入 context 共享同一 list    │
│  → assistant 响应会 append 到原 context                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. 入口分流

```
         调用方
           │
           ├──────────────────────────┐
           │                          │
    agent_loop()              agent_loop_continue()
    "新对话/新 prompt"         "retry / 从断点续接"
           │                          │
           ▼                          ▼
   ┌─────────────────┐      ┌──────────────────────┐
   │ run_agent_loop  │      │ run_agent_loop_continue│
   │                 │      │                        │
   │ new_messages    │      │ new_messages = []      │
   │ = list(prompts) │      │ (不注入新 prompt)      │
   │                 │      │                        │
   │ current_context │      │ current_context        │
   │ .messages =     │      │ .messages =            │
   │ [*ctx, *prompts]│      │  context.messages      │
   │                 │      │  (共享引用！)           │
   │                 │      │                        │
   │ emit:           │      │ emit:                  │
   │  AgentStart     │      │  AgentStart            │
   │  TurnStart      │      │  TurnStart             │
   │  MessageStart/  │      │  (无 prompt 事件)      │
   │  End × N        │      │                        │
   └─────────────────┘      └──────────────────────┘
           │                          │
           └──────────┬───────────────┘
                      ▼
               _run_loop(...)
```

---

## 3. 核心双层循环（_run_loop）

```
 _run_loop 入口
    │
    ▼
 ┌─ current_context = initial_context
 │  config = initial_config
 │  first_turn = True
 │  pending_messages ← get_steering_messages()
 │
 │                    ┌──────────────────────────────────────────┐
 │                    │          外层 while True                  │
 │                    │  "follow-up 驱动：内层退出后还有新消息？"  │
 │                    │                                          │
 │  has_more_tool_calls = True                                    │
 │                    │                                          │
 │                    │    ┌──────────────────────────────────┐  │
 │                    │    │    内层 while                     │  │
 │                    │    │    has_more or pending_messages   │  │
 │                    │    │                                  │  │
 │                    │    │  ① first_turn?                   │  │
 │                    │    │     ─ 否 → emit TurnStart        │  │
 │                    │    │     ─ 是 → first_turn=False      │  │
 │                    │    │                                  │  │
 │                    │    │  ② pending_messages?             │  │
 │                    │    │     ─ 逐条双写：                  │  │
 │                    │    │       context.msg += msg          │  │
 │                    │    │       new_messages += msg         │  │
 │                    │    │       emit MessageStart/End       │  │
 │                    │    │     ─ 清空 pending                │  │
 │                    │    │                                  │  │
 │                    │    │  ③ stream_assistant_response ──► │  │──► LLM
 │                    │    │     ↓ 返回 AssistantMessage       │  │◄── LLM
 │                    │    │     双写 new_messages += msg      │  │
 │                    │    │                                  │  │
 │                    │    │  ④ stop_reason?                  │  │
 │                    │    │     ─ error/aborted               │  │
 │                    │    │       emit TurnEnd + AgentEnd     │  │
 │                    │    │       return ← 退出整个循环       │  │
 │                    │    │                                  │  │
 │                    │    │  ⑤ tool_calls?                   │  │
 │                    │    │     ─ length截断                  │  │
 │                    │    │       _fail_tool_calls...         │  │
 │                    │    │     ─ 正常 → execute_tool_calls  │  │──► 工具
 │                    │    │       ↓ ExecutedToolCallBatch     │  │◄── 工具
 │                    │    │       双写 toolResults            │  │
 │                    │    │       has_more = !batch.terminate │  │
 │                    │    │                                  │  │
 │                    │    │  ⑥ emit TurnEnd                  │  │
 │                    │    │                                  │  │
 │                    │    │  ⑦ prepare_next_turn hook?       │  │
 │                    │    │     → 可替换 context / config    │  │
 │                    │    │                                  │  │
 │                    │    │  ⑧ should_stop_after_turn?       │  │
 │                    │    │     → True: emit AgentEnd,return │  │
 │                    │    │                                  │  │
 │                    │    │  ⑨ pending ← steering_messages   │  │
 │                    │    │                                  │  │
 │                    │    └─── 内层循环结束 ────────────────┘  │
 │                    │                                          │
 │                    │  follow_up ← get_follow_up_messages?    │
 │                    │     ─ 有 → pending = follow_up          │
 │                    │       continue 外层                      │
 │                    │     ─ 无 → break                         │
 │                    └──────────────────────────────────────────┘
 │
 └── emit AgentEnd(messages=new_messages)
```

---

## 4. stream_assistant_response 内部信息流

这是 **AgentMessage 与 LLM Message 的边界**——全程唯一一处做类型转换。

```
 stream_assistant_response(context, config, signal, emit, stream_fn)
    │
    ▼
 ┌────────────────────────────────────────────────────────────┐
 │  AgentMessage[] → Message[] 转换管线                       │
 │                                                            │
 │  context.messages ─────► (transform_context hook?)         │
 │                          │  可选：对 AgentMessage[] 做变换  │
 │                          ▼                                 │
 │                    convert_to_llm()                         │
 │                          │  AgentMessage[] → Message[]     │
 │                          │  必选：跨越 Agent/LLM 边界      │
 │                          ▼                                 │
 │                    构建 pi_ai.Context                       │
 │                          │  system_prompt + llm_messages   │
 │                          │  + tools (as_tool投影)          │
 │                          ▼                                 │
 │                    stream_fn(model, ctx, options)           │
 │                          │  ← 调用 LLM provider            │
 │                          ▼                                 │
 │                    async for event in response:             │
 │                                                            │
 │    ┌── start ──► partial_message = event.partial           │
 │    │            context.messages.append(partial) ← 双写①  │
 │    │            emit MessageStart(message=copy)             │
 │    │                                                       │
 │    ├── *_delta ─► partial_message = event.partial          │
 │    │            context.messages[-1] = partial ← 双写②    │
 │    │            emit MessageUpdate(event, message=copy)    │
 │    │                                                       │
 │    ├── done ───► final_message = response.result()         │
 │    │  error      context.messages[-1] = final ← 双写③    │
 │    │            emit MessageEnd(message=final)              │
 │    │            return final_message                        │
 │    │                                                       │
 │    └── 循环结束兜底 ─► 同 done 路径                        │
 │                                                            │
 │  关键：partial 在 context.messages 中是「原地更新」        │
 │  - start 时 append (占一个槽位)                           │
 │  - delta 时 replace [-1] (更新同一槽位)                   │
 │  - done 时 replace [-1] (final 替换 partial)              │
 │                                                            │
 │  对外 emit 的 message 用 replace()/copy，防止外部引用      │
 │  看到后续被覆盖的 partial                                  │
 └────────────────────────────────────────────────────────────┘
```

---

## 5. 工具执行管线三阶段

```
 prepare_tool_call ───► execute ───► finalize
     (preflight)          (运行)       (后处理)

 ┌──────────────────────────────────────────────────────────────┐
 │  Phase 1: prepare_tool_call                                  │
 │                                                              │
 │  tool_call ──► _find_tool(context, name)                     │
 │                  │                                           │
 │                  └─ 找不到 → ImmediateToolCallOutcome         │
 │                    (error: "Tool X not found")               │
 │                  │                                           │
 │                  └─ 找到 ↓                                   │
 │                                                              │
 │  _prepare_tool_call_arguments(tool, tool_call)               │
 │      │  tool.prepare_arguments(raw_args)                     │
 │      │  → 可能修正非 dict 参数                               │
 │      ▼                                                       │
 │  validate_tool_arguments(tool.as_tool(), prepared)            │
 │      │  JSON Schema 校验 (required + type)                   │
 │      │  → 校验失败 → exception → Immediate (error)          │
 │      ▼                                                       │
 │  before_tool_call hook?                                      │
 │      │                                                       │
 │      ├─ signal.aborted → Immediate (error: "aborted")        │
 │      ├─ block=True → Immediate (error: reason)               │
 │      ├─ 正常通过 ↓                                           │
 │      │                                                       │
 │      ▼                                                       │
 │  PreparedToolCall(tool_call, tool, args=validated)            │
 │                                                              │
 │  Immediate 结果：不调用 execute，直接跳到 finalize 格式化      │
 └──────────────────────────────────────────────────────────────┘

 ┌──────────────────────────────────────────────────────────────┐
 │  Phase 2: _execute_prepared_tool_call                        │
 │                                                              │
 │  prepared.tool.execute(id, args, signal, on_update)          │
 │      │                                                       │
 │      │  on_update 回调：                                      │
 │      │    → emit ToolExecutionUpdateEvent                    │
 │      │    → 收集 awaitable emit 到 update_events             │
 │      │    → execute 结束后 await gather(update_events)       │
 │      │                                                       │
 │      ├─ 成功 → ExecutedToolCallOutcome(result, isError=False)│
 │      ├─ 异常 → ExecutedToolCallOutcome(error_result, True)   │
 │      │  finally: accepting_updates = False                   │
 └──────────────────────────────────────────────────────────────┘

 ┌──────────────────────────────────────────────────────────────┐
 │  Phase 3: _finalize_executed_tool_call                       │
 │                                                              │
 │  after_tool_call hook?                                       │
 │      │                                                       │
 │      ├─ 返回 AfterToolCallResult                             │
 │      │   → 部分覆盖：content/details/terminate/is_error      │
 │      │   → None 字段保留原值（?? 语义）                       │
 │      │                                                       │
 │      ├─ hook 异常 → error result, is_error=True              │
 │      │                                                       │
 │      ▼                                                       │
 │  FinalizedToolCallOutcome(tool_call, result, is_error)        │
 └──────────────────────────────────────────────────────────────┘
```

---

## 6. 顺序 vs 并行执行模式

```
 execute_tool_calls(context, msg, config, signal, emit)
    │
    ├─ 检查：config.tool_execution == "sequential"
    │        OR 任何 tool.execution_mode == "sequential"?
    │
    ├─ Yes ─► _execute_tool_calls_sequential
    │          │
    │          │  for each tool_call (源序):
    │          │    ┌─────────────────────────────────────┐
    │          │    │ emit tool_execution_start            │
    │          │    │ ↓                                   │
    │          │    │ prepare → execute → finalize         │
    │          │    │ ↓                                   │
    │          │    │ emit tool_execution_end               │
    │          │    │ ↓                                   │
    │          │    │ emit toolResult message_start/end    │
    │          │    │ ↓                                   │
    │          │    │ signal.aborted? → break              │
    │          │    └─────────────────────────────────────┘
    │          │
    │          │  terminate = all(finalized.terminate)
    │          ▼
    │
    └─ No ─► _execute_tool_calls_parallel
              │
              │  Phase A: 顺序 preflight（对每个 tool_call）:
              │    ┌─────────────────────────────────────┐
              │    │ emit tool_execution_start            │
              │    │ ↓                                   │
              │    │ prepare_tool_call                    │
              │    │   │                                 │
              │    │   ├─ immediate → emit end,           │
              │    │   │   finalized_entries.append(val) │
              │    │   │                                 │
              │    │   └─ prepared → finalized_entries   │
              │    │      .append(_run_parallel 函数)    │
              │    │                                 │
              │    │ signal.aborted? → break              │
              │    └─────────────────────────────────────┘
              │
              │  Phase B: 并行执行（asyncio.gather）:
              │    ┌─────────────────────────────────────┐
              │    │ coros = [                            │
              │    │   callable → entry()  (启动协程)     │
              │    │   immediate → _const(e) (包装)       │
              │    │ ]                                    │
              │    │ ↓                                   │
              │    │ ordered = gather(*coros)              │
              │    │   ↑ 所有 execute 并行跑              │
              │    │   ↑ end 事件：完成序（谁先完谁先 emit）│
              │    │   ↑ 结果列表：源序（gather 保留顺序） │
              │    └─────────────────────────────────────┘
              │
              │  Phase C: 源序 emit toolResult 消息:
              │    ┌─────────────────────────────────────┐
              │    │ for finalized in ordered:            │
              │    │   emit MessageStart/End(toolResult)  │
              │    └─────────────────────────────────────┘
              │
              │  terminate = all(finalized.terminate)
              ▼
```

---

## 7. length 截断的特殊路径

```
  stop_reason == "length"
      │
      ▼
  _fail_tool_calls_from_truncated_message(tool_calls, emit)
      │
      │  for each tool_call:
      │    emit tool_execution_start
      │    ↓
      │    FinalizedToolCallOutcome(
      │      result = error("arguments may be truncated"),
      │      is_error = True
      │    )
      │    ↓
      │    emit tool_execution_end
      │    ↓
      │    emit toolResult message_start/end
      │
      │  terminate = False ← 关键！不终止，让 LLM 重新生成
      ▼
  → has_more_tool_calls = True (继续内层循环)
  → LLM 下一轮看到 error toolResult，可能重新发出完整参数
```

---

## 8. terminate 语义与循环退出条件

```
  batch.terminate = ?
      │
      ├─ 所有 finalized.result.terminate == True
      │    → terminate = True
      │    → has_more_tool_calls = False
      │    → 内层循环退出条件满足（前提：pending 也为空）
      │
      ├─ 任一 finalized.result.terminate == False (或 None)
      │    → terminate = False
      │    → has_more_tool_calls = True
      │    → 内层循环继续（LLM 再调一次，可能会纯文本回复）
      │
      ├─ 没有 tool_calls (纯文本回复)
      │    → tool_results = []
      │    → has_more_tool_calls = False (初始值)
      │    → 内层退出（前提：pending 为空）
      │
      ▼
  内层退出后 → 查 follow_up
     ├─ 有 → pending = follow_up, continue 外层
     └─ 无 → break, emit AgentEnd
```

---

## 9. 事件时序全景

以一个典型两轮工具调用为例（user → assistant(toolUse) → toolResult → assistant(text)）：

```
  时间线 ──────────────────────────────────────────────────────►

  agent_loop([user_msg], context, config)
    │
    │  AgentStartEvent
    │  TurnStartEvent
    │  MessageStart(user_msg)  MessageEnd(user_msg)
    │                          │ ← 内层循环第 1 轮
    │  stream_assistant_response ───► LLM
    │                          │
    │  MessageStart(assistant_partial)
    │  MessageUpdate × N (text_delta / toolcall_delta)
    │  MessageEnd(assistant_final)
    │                          │ ← stop_reason = "toolUse"
    │  ToolExecutionStart(echo, args)
    │  [ToolExecutionUpdate × N]  ← 工具进度回调
    │  ToolExecutionEnd(echo, result)
    │  MessageStart(toolResult)  MessageEnd(toolResult)
    │                          │ ← 双写 toolResult
    │  TurnEndEvent
    │                          │ ← prepare_next_turn / should_stop
    │                          │ ← steering (空)
    │                          │ ← has_more = !terminate → False? → 内层退出
    │                          │ ← follow_up (空) → 外层退出
    │                          │
    │                          │ ─ 但 terminate=False → has_more=True
    │                            ← 内层继续第 2 轮
    │
    │  TurnStartEvent
    │  stream_assistant_response ───► LLM
    │  MessageStart(assistant_partial)
    │  MessageUpdate × N
    │  MessageEnd(assistant_final)
    │                          │ ← stop_reason = "stop", 无 tool_calls
    │  TurnEndEvent
    │                          │ ← has_more=False, pending=空 → 内层退出
    │                          │ ← follow_up=空 → 外层退出
    │  AgentEndEvent(messages = new_messages)
```

---

## 10. 数据容器生命周期

```
 ┌─────────────────────────────────────────────────────────────────┐
 │  current_context.messages (完整 transcript)                     │
 │                                                                 │
 │  初始: [*原有消息, *prompts]  (agent_loop)                      │
 │       context.messages         (agent_loop_continue, 共享引用)   │
 │                                                                 │
 │  每轮增长:                                                      │
 │    + pending_messages (steering/follow-up)                      │
 │    + AssistantMessage (partial → final, 原地更新同一槽位)       │
 │    + ToolResultMessage × N                                      │
 │                                                                 │
 │  → stream_fn 每轮都读完整的 context.messages                    │
 │  → prepare_next_turn 可替换整个 context                         │
 │                                                                 │
 ├─────────────────────────────────────────────────────────────────┤
 │  new_messages (本次 run 增量)                                   │
 │                                                                 │
 │  初始: list(prompts)  (agent_loop)                              │
 │       []               (agent_loop_continue)                    │
 │                                                                 │
 │  每轮增长: 同上，但不含 run 前的已有消息                         │
 │                                                                 │
 │  → AgentEndEvent.messages = new_messages                        │
 │  → stream.end(new_messages) → 调用方 await stream.result()     │
 └─────────────────────────────────────────────────────────────────┘
```
