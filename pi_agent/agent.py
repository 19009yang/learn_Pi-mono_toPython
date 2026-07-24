"""Agent：底层 agent loop 的有状态包装器。

packages/agent/src/agent.ts 的 Python 移植版。

Agent 类持有当前对话记录、发出生命周期事件、执行工具，
并提供 steering 和 follow-up 消息的排队 API。它对裸 agent_loop 的封装包括：
  - 状态持有（AgentState）
  - 事件订阅（subscribe/unsubscribe）
  - Steering 和 follow-up 消息队列，支持可配置的 drain 模式
  - Abort signal 管理（AbortSignal，而非 DOM AbortController）
  - 生命周期管理（runWithLifecycle 确保即使出错也能清理）

================================================================================
与 TS 版本的关键设计差异
================================================================================
1. TS 使用 DOM AbortController/AbortSignal；Python 使用我们自己的 AbortSignal
   （来自 pi_ai.event_stream）。Agent.abort() 方法调用 Python AbortSignal 的
   signal.abort()，后者设置一个内部的 asyncio.Event。

2. TS 的 PendingMessageQueue 使用普通数组；Python 使用 list（等效）。
   drain 语义（"all" vs "one-at-a-time")完全相同。

3. TS 的 ActiveRun 持有 {promise, resolve, abortController}；Python 持有
   {future, resolve_future, signal}，其中 future 是 asyncio.Future
   （或使用 asyncio.Event + resolve 回调的手动 Promise 模式）。

4. TS 的 processEvents 接收 DOM AbortSignal；Python 传入我们的 AbortSignal。
   监听器同样接收 (event, signal) 参数，与 TS 一致。

5. TS 的 defaultConvertToLlm 按角色过滤；Python 的 convert_to_llm（来自
   pi_agent.messages）处理自定义消息类型。Agent 构造函数在未提供时
   默认使用 messages 模块的 convert_to_llm。

================================================================================
生命周期流程（与 TS 一致）
================================================================================
prompt(input) / continue_loop()
  → normalize_prompt_input()（仅用于 prompt）
  → run_prompt_messages() / run_continuation()
  → run_with_lifecycle(executor)           # 创建 ActiveRun，设置状态
    → executor(signal)                     # 调用 runAgentLoop / runAgentLoopContinue
      → processEvents(event)               # 更新状态 + 通知监听器
    → handle_run_failure()（出错时）        # 向监听器发出错误事件
    → finish_run()                         # 清除运行时状态，resolve idle promise
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeAlias, Union

from pi_ai.event_stream import AbortSignal
from pi_ai.types import (
    AssistantMessage,
    CostInfo,
    ImageContent,
    Message,
    Model,
    SimpleStreamOptions,
    TextContent,
    Usage,
    UserMessage,
)
from pi_agent.agent_loop import run_agent_loop, run_agent_loop_continue
from pi_agent.messages import AgentMessage, convert_to_llm
from pi_agent.types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentState,
    AgentTool,
    BeforeToolCallContext,
    BeforeToolCallResult,
    PrepareNextTurnContext,
    QueueMode,
    StreamFn,
    ThinkingLevel,
    ToolExecutionMode,
)


# ========== 常量 ==========

# 用于 handleRunFailure 产生的错误/中止 assistant 消息的空 Usage。
EMPTY_USAGE = Usage(
    input=0,
    output=0,
    cache_read=0,
    cache_write=0,
    total_tokens=0,
    cost=CostInfo(input=0, output=0, cache_read=0, cache_write=0, total=0),
)

# 默认模型占位符 — 当 initialState 中未提供 model 时使用。
DEFAULT_MODEL = Model(
    id="unknown",
    name="unknown",
    api="unknown",
    provider="unknown",
    base_url="",
    context_window=0,
    max_tokens=0,
    cost=CostInfo(input=0, output=0, cache_read=0, cache_write=0, total=0),
)


# ========== PendingMessageQueue ==========  # 消息等待队列


class PendingMessageQueue:
    """具有可配置 drain 语义的消息队列。

    对应 TS 的 PendingMessageQueue 类。

    两种 drain 模式：
    - "all": drain() 一次性返回所有排队消息（在 TS 中默认用于 follow-up，
      尽管 Agent 将 followUpMode 默认设为 "one-at-a-time")。
    - "one-at-a-time": drain() 仅返回第一条排队消息，
      其余消息留给后续 drain 调用（默认用于 steering）。

    这与 TS 实现匹配：
      - mode === "all" → 返回完整切片，清空队列
      - otherwise → 返回 [messages[0]]，从队列头部移除
    """

    def __init__(self, mode: QueueMode) -> None:
        self._messages: list[AgentMessage] = []
        self.mode: QueueMode = mode

    def enqueue(self, message: AgentMessage) -> None:
        """将消息加入队列。"""
        self._messages.append(message)

    def has_items(self) -> bool:
        """检查队列是否包含消息。"""
        return len(self._messages) > 0

    def drain(self) -> list[AgentMessage]:
        """根据队列的 drain 模式移除并返回消息。

        - "all": 返回所有排队消息并清空队列。
        - "one-at-a-time": 仅返回第一条消息并将其从队列移除。
          如果队列已空，返回 []。
        """
        if self.mode == "all":
            drained = self._messages[:]
            self._messages = []
            return drained

        # one-at-a-time: 仅返回第一个元素
        if not self._messages:
            return []
        first = self._messages[0]
        self._messages = self._messages[1:]
        return [first]

    def clear(self) -> None:
        """移除队列中的所有消息。"""
        self._messages = []


# ========== ActiveRun ==========  # 活跃运行追踪

@dataclass
class ActiveRun:
    """追踪正在进行的 agent 运行。

    对应 TS 类型 ActiveRun = { promise, resolve, abortController }。

    - future: 一个 asyncio.Future，在运行结束时 resolve
      （包括所有 agent_end 监听器完成之后）。wait_for_idle()
      会等待此 future。
    - signal: 本次运行的 AbortSignal。abort() 设置它，
      在循环和工具中触发协作式取消。
    - _resolve_future: 在 finishRun() 被调用时 resolve future 的内部回调。
    """

    future: asyncio.Future[None]
    signal: AbortSignal
    _resolve_future: Callable[[], None]


def _create_active_run() -> ActiveRun:
    """创建一个带有新 AbortSignal 和未 resolve future 的 ActiveRun。

    future 通过调用 active_run._resolve_future() 来 resolve，
    这发生在 finish_run() 中。这映射了 TS 中创建 Promise
    并提取 resolve 函数的模式。
    """
    signal = AbortSignal() #为本次Agent运行创建一个独立的协作式取消信号，AbortSignal()内部只有一个asyncio.Event()作为开关
    future: asyncio.Future[None] = asyncio.get_event_loop().create_future()
    # Future 是 asyncio中的"占位结果"。它初始状态是未完成（pending），后续有人调用 future.set_result(None)后才变为已完成（resolved）
    # [None]说明resolve时不携带结果，只作为“完成通知”

    resolve_future: Callable[[], None] = lambda: (
        future.set_result(None) if not future.done() else None
    )
    return ActiveRun(future=future, signal=signal, _resolve_future=resolve_future)


# ========== AgentOptions ==========  # Agent 配置选项


@dataclass
class AgentOptions:
    """Agent 构造选项。

    对应 TS 接口 AgentOptions。值为 None 的字段
    将在 Agent 构造函数中使用默认值填充。
    """

    # 初始状态字段（不含 pendingToolCalls / isStreaming 等）
    initial_state: dict[str, Any] | None = None

    # 核心函数覆写
    convert_to_llm: Callable[[list[AgentMessage]], list[Message] | Awaitable[list[Message]]] | None = None
    transform_context: Callable[[list[AgentMessage], AbortSignal | None], Awaitable[list[AgentMessage]]] | None = None
    stream_fn: StreamFn | None = None
    get_api_key: Callable[[str], Awaitable[str | None] | str | None] | None = None

    # 钩子回调
    before_tool_call: Callable[[BeforeToolCallContext, AbortSignal | None], Awaitable[BeforeToolCallResult | None]] | None = None
    after_tool_call: Callable[[AfterToolCallContext, AbortSignal | None], Awaitable[AfterToolCallResult | None]] | None = None
    prepare_next_turn: Callable[[AbortSignal | None], Awaitable[AgentLoopTurnUpdate | None] | AgentLoopTurnUpdate | None] | None = None
    prepare_next_turn_with_context: Callable[[PrepareNextTurnContext, AbortSignal | None], Awaitable[AgentLoopTurnUpdate | None] | AgentLoopTurnUpdate | None] | None = None

    # 队列 drain 模式
    steering_mode: QueueMode | None = None  # 默认: "one-at-a-time"
    follow_up_mode: QueueMode | None = None  # 默认: "one-at-a-time"

    # 流式/提供商选项，转发至 SimpleStreamOptions
    session_id: str | None = None
    thinking_budgets: Any | None = None  # ThinkingBudgets; Any 避免循环导入
    transport: str | None = None  # Transport 字面量; 默认 "auto"
    max_retry_delay_ms: int | None = None
    tool_execution: ToolExecutionMode | None = None  # 默认: "parallel"


# ========== Agent 类 ==========


class Agent:
    """底层 agent loop 的有状态包装器。

    Agent 类为运行多轮 agent loop 提供了高级接口，包括：
      - 跨运行持久化的状态（AgentState）
      - 通过 subscribe() / unsubscribe() 进行事件订阅
      - 通过 steer() 和 follow_up() 进行消息排队
      - 通过 abort() / signal 进行协作式中止
      - 通过 wait_for_idle() 等待空闲

    生命周期：
      1. 用户调用 prompt() 或 continue_loop()
      2. Agent 创建 ActiveRun（含 AbortSignal + idle future）
      3. Agent 运行底层 agent_loop，将事件转发给
         _process_events()，后者更新状态并通知监听器
      4. 完成时（或出错时），finish_run() 清除运行时状态
         并 resolve idle future
      5. wait_for_idle() 返回，表示 agent 已准备好
         接收下一个 prompt()

    线程安全：Agent 设计为单线程异步使用。
    不要从多个并发任务调用 prompt()；使用 steer()
    或 follow_up() 向正在运行的 agent 注入消息。
    """

    def __init__(self, options: AgentOptions | None = None) -> None:
        opts = options or AgentOptions()

        # ---- 状态 ----
        # 从选项或默认值构建初始 AgentState。
        # 工具和消息在赋值时被复制（写时复制），

        initial = opts.initial_state or {}
        self._state = AgentState(
            system_prompt=initial.get("system_prompt", ""),
            model=initial.get("model", DEFAULT_MODEL),
            thinking_level=initial.get("thinking_level", "off"),
            tools=initial.get("tools"),
            messages=initial.get("messages"),
        )

        # ---- 核心函数覆写 ----
        # convert_to_llm 默认使用 messages 模块的 convert_to_llm，
        # 后者处理所有自定义 AgentMessage 类型（bashExecution、custom、
        # branchSummary、compactionSummary）。
        self.convert_to_llm: Callable[[list[AgentMessage]], list[Message] | Awaitable[list[Message]]] = (
            opts.convert_to_llm or convert_to_llm
        )
        self.transform_context: Callable[[list[AgentMessage], AbortSignal | None], Awaitable[list[AgentMessage]]] | None = (
            opts.transform_context
        )
        # stream_fn 默认为 None — 调用者必须提供，或依赖
        # agent_loop 的惰性默认值（Models.stream_simple）。
        self.stream_fn: StreamFn | None = opts.stream_fn
        self.get_api_key: Callable[[str], Awaitable[str | None] | str | None] | None = opts.get_api_key

        # ---- 钩子回调 ----
        self.before_tool_call: Callable[[BeforeToolCallContext, AbortSignal | None], Awaitable[BeforeToolCallResult | None]] | None = opts.before_tool_call
        self.after_tool_call: Callable[[AfterToolCallContext, AbortSignal | None], Awaitable[AfterToolCallResult | None]] | None = opts.after_tool_call
        self.prepare_next_turn: Callable[[AbortSignal | None], Awaitable[AgentLoopTurnUpdate | None] | AgentLoopTurnUpdate | None] | None = opts.prepare_next_turn
        self.prepare_next_turn_with_context: Callable[[PrepareNextTurnContext, AbortSignal | None], Awaitable[AgentLoopTurnUpdate | None] | AgentLoopTurnUpdate | None] | None = (
            opts.prepare_next_turn_with_context
        )

        # ---- 消息队列 ----
        # TS 默认值: steering "one-at-a-time", followUp "one-at-a-time"
        # (TS 构造函数注释说两者均为 "one-at-a-time"。)
        self._steering_queue = PendingMessageQueue(opts.steering_mode or "one-at-a-time")
        self._follow_up_queue = PendingMessageQueue(opts.follow_up_mode or "one-at-a-time")

        # ---- 流式/提供商选项 ----
        self.session_id: str | None = opts.session_id
        self.thinking_budgets: Any | None = opts.thinking_budgets
        self.transport: str = opts.transport or "auto"
        self.max_retry_delay_ms: int | None = opts.max_retry_delay_ms
        self.tool_execution: ToolExecutionMode = opts.tool_execution or "parallel"

        # ---- 运行时状态（仅在活跃运行期间设置）----
        self._active_run: ActiveRun | None = None

        # ---- 事件监听器 ----
        # 以 set 存储，实现 O(1) 添加/移除。监听器在 _process_events 中
        # 按插入顺序调用（我们遍历 set；Python 3.7+ 的 set 保持插入顺序）。
        self._listeners: set[Callable[[AgentEvent, AbortSignal], Awaitable[None] | None]] = set()

    # ========== 公共属性 ==========

    @property
    def state(self) -> AgentState:
        """当前 agent 状态。

        赋值 state.tools 或 state.messages 时会复制顶层列表
        （AgentState setter 语义），因此调用者无法通过保留
        赋值引用来修改内部存储。
        """
        return self._state

    """@property 与 @steering_mode.setter封装内部值的读取和写入操作，装饰器将显示调用变成属性调用：
            mode = agent.steering_mode       # 读
            agent.steering_mode = "all"      # 写
        与下面普通函数调用效果一致
            mode = agent.get_steering_mode()       # 读
            agent.set_steering_mode("all")         # 写
    """

    @property
    def steering_mode(self) -> QueueMode:
        """排队的 steering 消息如何被 drain。"""
        return self._steering_queue.mode

    @steering_mode.setter
    def steering_mode(self, mode: QueueMode) -> None:
        self._steering_queue.mode = mode

    @property
    def follow_up_mode(self) -> QueueMode:
        """排队的 follow-up 消息如何被 drain。"""
        return self._follow_up_queue.mode

    @follow_up_mode.setter
    def follow_up_mode(self, mode: QueueMode) -> None:
        self._follow_up_queue.mode = mode

    @property
    def signal(self) -> AbortSignal | None:
        """当前活跃运行的 abort signal（如果有）。

        无活跃运行时返回 None。检查 signal.aborted 可知
        运行是否已被取消。
        """
        return self._active_run.signal if self._active_run else None

    # ========== 事件订阅 ==========

    def subscribe(
        self,
        listener: Callable[[AgentEvent, AbortSignal], Awaitable[None] | None],
    ) -> Callable[[], None]:
        """订阅 agent 生命周期事件。

        监听器接收 (event, signal)，其中 signal 是活跃
        运行的 AbortSignal。监听器的 promise 按订阅顺序被
        await，并包含在当前运行的结算中。

        返回一个 unsubscribe 函数，用于移除该监听器。

        对应 TS Agent.subscribe(listener)。
        """
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener) #discard是幂等操作，意思是做一次和做多次的结果一样，不怕重复操作
        #如果listener不存在，静默忽略：取消订阅时监听器可能已经被其他途径移除了，discard 不会报错，更安全

    # ========== 队列 API ==========

    def steer(self, message: AgentMessage) -> None:
        """
        将消息排队，在当前 assistant 轮次之后注入。
        Steering 消息在每个内循环迭代开始时被 drain，
        在下一次 LLM 调用之前。在 "one-at-a-time" 模式下
        （默认），每次迭代仅注入一条 steering 消息。
        对应 TS Agent.steer(message)。
        """
        self._steering_queue.enqueue(message)

    def follow_up(self, message: AgentMessage) -> None:
        """将消息排队，仅在 agent 本应停止后才运行。

        Follow-up 消息在内循环退出后被 drain（没有更多
        工具调用且没有待处理的 steering）。它们使外循环
        继续一个新的迭代。

        对应 TS Agent.followUp(message)。
        """
        self._follow_up_queue.enqueue(message)

    def clear_steering_queue(self) -> None:
        """移除所有排队的 steering 消息。"""
        self._steering_queue.clear()

    def clear_follow_up_queue(self) -> None:
        """移除所有排队的 follow-up 消息。"""
        self._follow_up_queue.clear()

    def clear_all_queues(self) -> None:
        """移除所有排队的 steering 和 follow-up 消息。"""
        self.clear_steering_queue()
        self.clear_follow_up_queue()

    def has_queued_messages(self) -> bool:
        """当任一队列仍包含待处理消息时返回 True。"""
        return self._steering_queue.has_items() or self._follow_up_queue.has_items()

    # ========== 中止 / 等待空闲 ==========

    def abort(self) -> None:
        """中止当前运行（如果存在活跃运行）。

        设置 AbortSignal，在 agent loop 和工具执行中触发
        协作式取消。运行将发出 error/aborted 事件后结束。

        无活跃运行时安全调用（无操作）。

        对应 TS Agent.abort()，其调用
        this.activeRun?.abortController.abort()。
        """
        if self._active_run is not None: 
            self._active_run.signal.abort() #调用_event.set()让signal.aborted 变为 True，实际上signal.aborted()是event.is_set()

    async def wait_for_idle(self) -> None:
        """当前运行和所有被 await 的事件监听器完成后 resolve。

        在 agent_end 监听器完成之后 resolve（而非 agent_end
        首次发出时），与 TS 行为一致。

        无活跃运行时立即 resolve。

        对应 TS Agent.waitForIdle()，其返回
        this.activeRun?.promise ?? Promise.resolve()。
        """
        if self._active_run is not None:
            await self._active_run.future
        # No active run → already idle

    # ========== 重置 ==========

    def reset(self) -> None:
        """清除对话记录状态、运行时状态和排队消息。

        不会中止活跃运行 — 如果有运行正在进行，
        先调用 abort() + wait_for_idle()。

        对应 TS Agent.reset()。
        """
        self._state.messages = []
        self._state._set_streaming(False)
        self._state._set_streaming_message(None)
        self._state._clear_pending_tool_calls()
        self._state._set_error_message(None)
        self.clear_follow_up_queue()
        self.clear_steering_queue()

    # ========== Prompt 入口 ==========

    async def prompt(
        self,
        input: str | AgentMessage | list[AgentMessage],
        images: list[ImageContent] | None = None,
    ) -> None:
        """从文本、单条消息或一批消息启动新 prompt。

        若已有活跃运行则抛出错误 — 使用 steer() 或
        follow_up() 向运行中的 agent 注入消息，
        或在 prompt() 前调用 wait_for_idle()。

        对应 TS Agent.prompt(input, images?)。
        """
        if self._active_run is not None:
            raise RuntimeError(
                "Agent is already processing a prompt. Use steer() or "
                "follow_up() to queue messages, or wait for completion."
            )
        messages = self._normalize_prompt_input(input, images)
        await self._run_prompt_messages(messages)

    async def continue_loop(self) -> None:
        """从当前对话记录继续，不添加新的 prompt 消息。

        state.messages 中的最后一条消息必须是 user 或 toolResult
        消息（不能是 assistant）。如果最后一条是 assistant，
        方法会先尝试 drain 排队的 steering/follow-up；如果两个
        队列都空，则抛出错误。

        对应 TS Agent.continue()。
        """
        if self._active_run is not None:
            raise RuntimeError(
                "Agent is already processing. Wait for completion before continuing."
            )

        last_message = self._state.messages[-1] if self._state.messages else None
        if not last_message:
            raise RuntimeError("No messages to continue from")

        # 如果最后一条消息是 assistant，先尝试 drain 队列来创建
        # 合法的续接点（最后一条必须是 user/toolResult）。
        if getattr(last_message, "role", None) == "assistant":
            # 先尝试 steering（skipInitialSteeringPoll 避免重新 drain
            # 刚刚已经 drain 的消息）
            queued_steering = self._steering_queue.drain()
            if queued_steering:
                await self._run_prompt_messages(
                    queued_steering, skip_initial_steering_poll=True
                )
                return

            # 然后尝试 follow-up
            queued_follow_ups = self._follow_up_queue.drain()
            if queued_follow_ups:
                await self._run_prompt_messages(queued_follow_ups)
                return

            raise RuntimeError("Cannot continue from message role: assistant")

        await self._run_continuation()

    # ========== 内部: prompt 输入规范化 ==========

    def _normalize_prompt_input(
        self,
        input: str | AgentMessage | list[AgentMessage],
        images: list[ImageContent] | None = None,
    ) -> list[AgentMessage]:
        """将 prompt 输入转换为 AgentMessage 列表。

        - list[AgentMessage]: 直接返回。
        - 单个 AgentMessage: 包装为列表。
        - 字符串: 转换为带可选 images 的 UserMessage。

        对应 TS Agent.normalizePromptInput(input, images)。
        """
        if isinstance(input, list):
            return input

        # 单个 AgentMessage 对象（非字符串）
        if not isinstance(input, str):
            return [input]

        # 字符串输入 → UserMessage
        content: list[TextContent | ImageContent] = [TextContent(text=input)]
        if images:
            content.extend(images)
        return [UserMessage(content=content, timestamp=int(time.time() * 1000))]

    # ========== 内部: 运行编排 ==========

    async def _run_prompt_messages(
        self,
        messages: list[AgentMessage],
        skip_initial_steering_poll: bool = False,
    ) -> None:
        """使用给定的 prompt 消息运行 agent_loop。

        从当前状态创建上下文快照，构建循环配置，
        并委托给 run_with_lifecycle 管理 ActiveRun。

        对应 TS Agent.runPromptMessages(messages, options)。
        """
        await self._run_with_lifecycle(
            lambda signal: run_agent_loop(
                messages,
                self._create_context_snapshot(),
                self._create_loop_config(
                    skip_initial_steering_poll=skip_initial_steering_poll
                ),
                lambda event: self._process_events(event),
                signal,
                self.stream_fn,
            )
        )

    async def _run_continuation(self) -> None:
        """从当前对话记录运行 agent_loop_continue。

        对应 TS Agent.runContinuation()。
        """
        await self._run_with_lifecycle(
            lambda signal: run_agent_loop_continue(
                self._create_context_snapshot(),
                self._create_loop_config(),
                lambda event: self._process_events(event),
                signal,
                self.stream_fn,
            )
        )

    def _create_context_snapshot(self) -> AgentContext:
        """将当前状态快照为 AgentContext 以供循环使用。

        消息浅拷贝（切片），使得循环可以追加
        而不修改 state，直到 message_end 更新它。

        对应 TS Agent.createContextSnapshot()。
        """
        return AgentContext(
            system_prompt=self._state.system_prompt,
            messages=self._state.messages[:],
            tools=self._state.tools[:],
        )

    def _create_loop_config(
        self,
        skip_initial_steering_poll: bool = False,
    ) -> AgentLoopConfig:
        """从当前 Agent 设置构建 AgentLoopConfig。

        配置将钩子委托给 Agent 自身的回调，
        并连接 steering/follow-up 队列 drain。
        skip_initial_steering_poll 标志防止重新 drain
        已由 continue_loop() drain 的 steering 消息。

        关键连接：
        - convert_to_llm → self.convert_to_llm
        - transform_context → self.transform_context
        - get_api_key → self.get_api_key
        - before_tool_call → self.before_tool_call
        - after_tool_call → self.after_tool_call
        - prepare_next_turn → 组合 prepareNextTurnWithContext
          和 prepareNextTurn，传入活跃 signal
        - get_steering_messages → drain self._steering_queue
          （带 skip_initial_steering_poll 保护）
        - get_follow_up_messages → drain self._follow_up_queue

        对应 TS Agent.createLoopConfig(options)。

        关于 skip_initial_steering_poll 的说明：TS 使用一个
        由 getSteeringMessages lambda 捕获的局部可变变量。
        在 Python 中，我们使用可变容器（包含一个 bool 的 list）
        使 lambda 能读写它，因为 lambda 中捕获的普通 bool
        是只读的（闭包捕获值而非引用）。
        """
        # skip 标志的可变容器 — lambda 可以修改列表元素
        # 但不能重新绑定局部变量。这匹配 TS 的
        # let skipInitialSteeringPoll = ...，lambda 读取它。
        _skip_flag = [skip_initial_steering_poll]

        # 组合 prepareNextTurnWithContext 和 prepareNextTurn：
        # 如果 prepareNextTurnWithContext 存在，优先调用它并
        # 传入完整上下文；否则 prepareNextTurn 仅接收 signal。
        prepare_next_turn_hook: Callable[[PrepareNextTurnContext], Awaitable[AgentLoopTurnUpdate | None] | AgentLoopTurnUpdate | None] | None = None
        if self.prepare_next_turn_with_context is not None or self.prepare_next_turn is not None:
            async def prepare_next_turn_hook_impl(
                context: PrepareNextTurnContext,
            ) -> AgentLoopTurnUpdate | None:
                # prepareNextTurnWithContext 接收 context + signal
                if self.prepare_next_turn_with_context is not None:
                    result = self.prepare_next_turn_with_context(
                        context, self.signal
                    )
                    if inspect.isawaitable(result):
                        return await result
                    return result
                # prepareNextTurn 仅接收 signal
                result = self.prepare_next_turn(self.signal)
                if inspect.isawaitable(result):
                    return await result
                return result

            prepare_next_turn_hook = prepare_next_turn_hook_impl

        # Thinking level 映射: "off" → None 用于循环配置
        reasoning: ThinkingLevel | None = None
        if self._state.thinking_level != "off":
            reasoning = self._state.thinking_level

        # Steering drain: 首次调用在 skip 标志为 True 时返回 [],
        # 然后清除标志，后续调用正常 drain。
        # 这匹配 TS getSteeringMessages: async () => {
        #   if (skipInitialSteeringPoll) { ... return []; }
        #   return this.steeringQueue.drain();
        # }
        async def get_steering_messages() -> list[AgentMessage]:
            if _skip_flag[0]:
                _skip_flag[0] = False
                return []
            return self._steering_queue.drain()

        return AgentLoopConfig(
            model=self._state.model,
            convert_to_llm=self.convert_to_llm,
            stream_fn=self.stream_fn,
            reasoning=reasoning,
            temperature=None,
            max_tokens=None,
            signal=None,  # signal 通过 run_with_lifecycle 提供
            api_key=None,  # 由 get_api_key 钩子 resolve
            transform_context=self.transform_context,
            get_api_key=self.get_api_key,
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            prepare_next_turn=prepare_next_turn_hook,
            get_steering_messages=get_steering_messages,
            get_follow_up_messages=lambda: self._async_follow_up_drain(),
            tool_execution=self.tool_execution,
        )

    # ========== 内部: 生命周期管理 ==========

    async def _run_with_lifecycle(
        self,
        executor: Callable[[AbortSignal], Awaitable[list[AgentMessage]]],
    ) -> None:
        """用完整生命周期管理包装 executor。

        1. 创建 ActiveRun（AbortSignal + idle Future）
        2. 设置 streaming 状态标志
        3. 运行 executor(signal)
        4. 出错时: 通过 handle_run_failure 发出错误事件
        5. 最后: finish_run（清除运行时状态，resolve idle Future）

        对应 TS Agent.runWithLifecycle(executor)。
        """
        if self._active_run is not None:
            raise RuntimeError("Agent is already processing.")

        active_run = _create_active_run()
        self._active_run = active_run

        # 标记 streaming 状态（镜像 TS runWithLifecycle）
        self._state._set_streaming(True)
        self._state._set_streaming_message(None)
        self._state._set_error_message(None)

        try:
            await executor(active_run.signal)
        except Exception as error:
            await self._handle_run_failure(error, active_run.signal.aborted)
        finally:
            self._finish_run()

    async def _handle_run_failure(self, error: Exception, aborted: bool) -> None:
        """运行抛出异常时发出错误事件。

        生成一个 stop_reason 为 "aborted" 或 "error" 的
        合成 assistant 消息，然后发出 message_start、message_end、
        turn_end 和 agent_end，使监听器看到完整（但失败的）
        生命周期。

        对应 TS Agent.handleRunFailure(error, aborted)。
        """
        # 构建合成的错误消息（匹配 TS EMPTY_USAGE + failure 形状）
        failure_message = AssistantMessage(
            content=[TextContent(text="")],
            api=self._state.model.api,
            provider=self._state.model.provider,
            model=self._state.model.id,
            usage=EMPTY_USAGE,
            stop_reason="aborted" if aborted else "error",
            timestamp=int(time.time() * 1000),
            error_message=str(error),
        )

        # 为失败发出生命周期事件（监听器看到完整序列）
        await self._process_events(
            _make_event("message_start", message=failure_message)
        )
        await self._process_events(
            _make_event("message_end", message=failure_message)
        )
        await self._process_events(
            _make_event("turn_end", message=failure_message, tool_results=[])
        )
        await self._process_events(
            _make_event("agent_end", messages=[failure_message])
        )

    def _finish_run(self) -> None:
        """清除运行时状态并 resolve idle Future。

        在 run_with_lifecycle 的 finally 块中调用，在
        所有 agent_end 监听器完成之后。这意味着：
        - is_streaming → False
        - streaming_message → None
        - pending_tool_calls → 清空
        - active_run → None
        - idle future → resolved

        对应 TS Agent.finishRun()。
        """
        self._state._set_streaming(False)
        self._state._set_streaming_message(None)
        self._state._clear_pending_tool_calls()

        # resolve idle future 使 wait_for_idle() 返回
        if self._active_run is not None:
            self._active_run._resolve_future()
            self._active_run = None

    # ========== 内部: 异步队列 drain ==========

    async def _async_follow_up_drain(self) -> list[AgentMessage]:
        """follow-up 队列 drain 的异步包装器。

        AgentLoopConfig 期望 get_follow_up_messages 返回
        Awaitable，但 PendingMessageQueue.drain() 是同步的。
        此包装器使其兼容异步契约。
        """
        return self._follow_up_queue.drain()

    # ========== 内部: 事件处理 ==========

    async def _process_events(self, event: AgentEvent) -> None:
        """对循环事件进行内部状态归约，然后 await 监听器。

        这是底层 agent_loop 与 Agent 高层状态管理之间的桥梁。
        对每种事件类型：
        - message_start: 设置 streaming_message
        - message_update: 更新 streaming_message
        - message_end: 清除 streaming_message，追加到 state.messages
        - tool_execution_start: 将 tool_call_id 加入待处理集合
        - tool_execution_end: 将 tool_call_id 从待处理集合移除
        - turn_end: 检查 error_message
        - agent_end: 清除 streaming_message

        状态归约后，所有订阅的监听器以 (event, signal) 被调用。
        监听器的协程按订阅顺序被 await。

        重要：agent_end 仅表示不再发出循环事件。
        运行被认为空闲是在之后 — 所有监听器完成且
        finishRun() 清除运行时状态之后。

        对应 TS Agent.processEvents(event)。
        """
        # ---- 状态归约 ----
        if event.type == "message_start":
            self._state._set_streaming_message(event.message)

        elif event.type == "message_update":
            self._state._set_streaming_message(event.message)

        elif event.type == "message_end":
            self._state._set_streaming_message(None)
            self._state.messages.append(event.message)

        elif event.type == "tool_execution_start":
            self._state._add_pending_tool_call(event.tool_call_id)

        elif event.type == "tool_execution_end":
            self._state._remove_pending_tool_call(event.tool_call_id)

        elif event.type == "turn_end":
            # 如果 assistant 消息包含 error_message，记录它
            if getattr(event.message, "role", None) == "assistant" and getattr(event.message, "error_message", None):
                self._state._set_error_message(event.message.error_message)

        elif event.type == "agent_end":
            self._state._set_streaming_message(None)

        # ---- 通知监听器 ----
        # 必须有活跃运行（signal）— 监听器不应在
        # 运行生命周期之外被调用。
        signal = self._active_run.signal if self._active_run else None
        if signal is None:
            raise RuntimeError("Agent listener invoked outside active run")

        for listener in self._listeners:
            result = listener(event, signal)
            if inspect.isawaitable(result):
                await result


# ========== 辅助工具: 合成事件构造 ==========


def _make_event(type_str: str, **kwargs: Any) -> AgentEvent:
    """根据类型字符串构造 AgentEvent dataclass。

    用于 handle_run_failure 发出合成的生命周期事件，
    而无需在模块级别导入所有事件类。

    根据 type_str 返回相应的 dataclass 实例。
    """
    from pi_agent.types import (
        AgentEndEvent,
        AgentStartEvent,
        MessageEndEvent,
        MessageStartEvent,
        MessageUpdateEvent,
        TurnEndEvent,
        TurnStartEvent,
        ToolExecutionEndEvent,
        ToolExecutionStartEvent,
        ToolExecutionUpdateEvent,
    )

    event_map = {
        "agent_start": AgentStartEvent,
        "agent_end": AgentEndEvent,
        "turn_start": TurnStartEvent,
        "turn_end": TurnEndEvent,
        "message_start": MessageStartEvent,
        "message_update": MessageUpdateEvent,
        "message_end": MessageEndEvent,
        "tool_execution_start": ToolExecutionStartEvent,
        "tool_execution_end": ToolExecutionEndEvent,
        "tool_execution_update": ToolExecutionUpdateEvent,
    }
    cls = event_map.get(type_str)
    if cls is None:
        raise ValueError(f"Unknown event type: {type_str}")
    return cls(**kwargs)
