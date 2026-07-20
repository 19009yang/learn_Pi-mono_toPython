"""
Agent loop engine.


"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeAlias

from pi_ai.event_stream import AbortSignal, EventStream
from pi_ai.types import (
    AssistantMessage,
    Context,
    SimpleStreamOptions,
    TextContent,
    ToolResultMessage,
)
from pi_ai.validation import validate_tool_arguments
from pi_agent.messages import AgentMessage
from pi_agent.types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentTool,
    AgentToolCall,
    AgentToolResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
    PrepareNextTurnContext,
    ShouldStopAfterTurnContext,
    StreamFn,
)

AgentEventSink: TypeAlias = Callable[[AgentEvent], Awaitable[None] | None]


# ========== Public entry points ==========


def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    signal: AbortSignal | None = None,
    stream_fn: StreamFn | None = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """使用新的提示词开启一次agent loop"""
    stream = _create_agent_stream()

    async def _runner() -> None:
        messages = await run_agent_loop(prompts, context, config, stream.push, signal, stream_fn)
        stream.end(messages)

    asyncio.create_task(_runner())
    return stream


def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: AbortSignal | None = None,
    stream_fn: StreamFn | None = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """用于 retry：context 里已有 user 或 toolResult，不再注入新 prompt
    约束（与 TS 相同）：
    - `context.messages` 不能为空。
    - 最后一条不能是 `assistant`（否则 provider 无法接话）。
    """
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    last = context.messages[-1]
    if getattr(last, "role", None) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")

    stream = _create_agent_stream()

    async def _runner() -> None:
        messages = await run_agent_loop_continue(context, config, stream.push, signal, stream_fn)
        stream.end(messages)

    asyncio.create_task(_runner())
    return stream


async def run_agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: AbortSignal | None = None,
    stream_fn: StreamFn | None = None,
) -> list[AgentMessage]:
    from pi_agent.types import AgentStartEvent, MessageEndEvent, MessageStartEvent, TurnStartEvent

    new_messages: list[AgentMessage] = list(prompts)
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=[*context.messages, *prompts],
        tools=context.tools,
    )

    await _emit(emit, AgentStartEvent())
    await _emit(emit, TurnStartEvent())
    for prompt in prompts:
        await _emit(emit, MessageStartEvent(message=prompt))
        await _emit(emit, MessageEndEvent(message=prompt))

    await _run_loop(current_context, new_messages, config, signal, emit, stream_fn)
    return new_messages


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: AbortSignal | None = None,
    stream_fn: StreamFn | None = None,
) -> list[AgentMessage]:
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    if getattr(context.messages[-1], "role", None) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")

    new_messages: list[AgentMessage] = []
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=context.messages,
        tools=context.tools,
    )

    from pi_agent.types import AgentStartEvent, TurnStartEvent

    await _emit(emit, AgentStartEvent())
    await _emit(emit, TurnStartEvent())

    await _run_loop(current_context, new_messages, config, signal, emit, stream_fn)
    return new_messages


# ========== Internal loop ==========


def _create_agent_stream() -> EventStream[AgentEvent, list[AgentMessage]]:
    return EventStream[AgentEvent, list[AgentMessage]](
        lambda event: event.type == "agent_end",
        lambda event: event.messages if event.type == "agent_end" else [],
    )


# 核心内部循环逻辑
"""
agent loop pattern：
while True:
    while has_more_tool_calls or pending_messages:
        → 发射 TurnStartEvent
        → 处理 pending/steering messages
        → stream_assistant_response()  # 调用 LLM
        → 如果 stop_reason 是 error/aborted → 终止循环
        → 执行 tool_calls（顺序或并行）
        → prepare_next_turn hook（可修改 context/config）
        → should_stop_after_turn hook
        → 获取 steering_messages
    → get_follow_up_messages → 如果有则继续外层循环
"""

async def _run_loop(
    initial_context: AgentContext, # Agent 上下文：system_prompt + messages + tools
    new_messages: list[AgentMessage], #本次循环产生的所有新消息（外部传入空列表，本函数不断 append）
    initial_config: AgentLoopConfig, # 循环配置：model、hooks、stream_fn 等
    signal: AbortSignal | None, # 中断信号，外部可通过它终止循环
    emit: AgentEventSink, # 事件发射器，向 EventStream 推送事件
    stream_fn: StreamFn | None, # LLM 调用函数，可选（可从 config 中 fallback）
) -> None:
    from pi_agent.types import AgentEndEvent, MessageEndEvent, MessageStartEvent, TurnEndEvent, TurnStartEvent

    current_context = initial_context # 当前轮次的上下文，循环中可被 prepare_next_turn 修改
    config = initial_config # 当前轮次的配置，循环中可被 prepare_next_turn 修改
    first_turn = True # 标记是否是第一轮——第一轮不发 TurnStartEvent（因为run_agent_loop 已发过）
    pending_messages: list[AgentMessage] = [] # 待注入的消息队列（steering / follow-up）
    if config.get_steering_messages is not None:
        pending_messages = list(await config.get_steering_messages())

    while True:
        """
        外层循环，负责处理follow_up_messages，正常情况下一轮 LLM +
        工具执行后就结束，但如果 get_follow_up_messages 返回了新消息，
        则重新进入一轮完整的 LLM 调用
        """
        has_more_tool_calls = True # 判断是否有待处理的工具调用

        while has_more_tool_calls or pending_messages:
            if not first_turn:
                await _emit(emit, TurnStartEvent())
            else:
                first_turn = False

            if pending_messages:
                for message in pending_messages:
                    await _emit(emit, MessageStartEvent(message=message))
                    await _emit(emit, MessageEndEvent(message=message))
                    current_context.messages.append(message)
                    new_messages.append(message)
                pending_messages = []
            #  双写：每条消息同时写入两个列表：
            #  - current_context.messages：LLM 在下一轮 stream_assistant_response 时会读到它
            #  - new_messages：最终通过 AgentEndEvent.messages 返回给调用方

            #调用LLM
            message = await stream_assistant_response(
                current_context, config, signal, emit, stream_fn
            )
            new_messages.append(message)

            #错误/中断终止
            if message.stop_reason in ("error", "aborted"):
                await _emit(emit, TurnEndEvent(message=message, tool_results=[]))
                await _emit(emit, AgentEndEvent(messages=new_messages))
                return

            tool_calls = [c for c in message.content if c.type == "toolCall"]
            tool_results: list[ToolResultMessage] = []
            has_more_tool_calls = False

            if tool_calls:
                if message.stop_reason == "length":
                    # 由于长度截断，不执行工具
                    batch = await _fail_tool_calls_from_truncated_message(tool_calls, emit)
                else:
                    batch = await execute_tool_calls(
                        current_context, message, config, signal, emit
                    )
                tool_results.extend(batch.messages)
                has_more_tool_calls = not batch.terminate
                # 如果所有工具结果都标记 terminate=True → batch.terminate=True → has_more_tool_calls=False →内层循环不再继续
                for result in tool_results:
                    current_context.messages.append(result) #LLM下轮可见
                    new_messages.append(result) #输出列表

            #一轮完整结束：LLM 回复 + 工具结果都已处理，发射 TurnEndEvent 通知外部。即使没有 tool calls，tool_results 也是空列表——保持事件序列完整。
            await _emit(emit, TurnEndEvent(message=message, tool_results=tool_results))

            # ShouldStopAfterTurnContext 的别名，用于动态修改运行状态
            next_turn_context = PrepareNextTurnContext(
                message=message,
                tool_results=tool_results,
                context=current_context, 
                new_messages=new_messages,
            )
            if config.prepare_next_turn is not None:
                snapshot = await _maybe_await(config.prepare_next_turn(next_turn_context))
                if snapshot is not None:
                    current_context, config = _apply_turn_update(current_context, config, snapshot)

            if config.should_stop_after_turn is not None:
                stop_ctx = ShouldStopAfterTurnContext(
                    message=message,
                    tool_results=tool_results,
                    context=current_context,
                    new_messages=new_messages,
                )
                if await _maybe_await(config.should_stop_after_turn(stop_ctx)):
                    await _emit(emit, AgentEndEvent(messages=new_messages))
                    return

            pending_messages = []
            if config.get_steering_messages is not None:
                pending_messages = list(await config.get_steering_messages())

        follow_up_messages: list[AgentMessage] = []
        if config.get_follow_up_messages is not None:
            follow_up_messages = list(await config.get_follow_up_messages())
        if follow_up_messages:
            pending_messages = follow_up_messages
            continue
        break

    await _emit(emit, AgentEndEvent(messages=new_messages))


def _apply_turn_update(
    current_context: AgentContext,
    config: AgentLoopConfig,
    snapshot: AgentLoopTurnUpdate,
) -> tuple[AgentContext, AgentLoopConfig]:
    if snapshot.context is not None:
        current_context = snapshot.context
    if snapshot.model is not None:
        config.model = snapshot.model
    if snapshot.thinking_level is not None:
        config.reasoning = (
            None if snapshot.thinking_level == "off" else snapshot.thinking_level
        )
    return current_context, config


# ========== LLM streaming ==========


async def stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
    stream_fn: StreamFn | None = None,
) -> AssistantMessage:
    from pi_agent.types import MessageEndEvent, MessageStartEvent, MessageUpdateEvent

    messages = context.messages
    if config.transform_context is not None:
        messages = list(await config.transform_context(messages, signal))

    llm_messages = await _maybe_await(config.convert_to_llm(messages))

    llm_tools = [tool.as_tool() for tool in (context.tools or [])]
    llm_context = Context(
        system_prompt=context.system_prompt,
        messages=llm_messages,
        tools=llm_tools or None,
    )

    resolved_fn = stream_fn or config.stream_fn
    if resolved_fn is None:
        from pi_ai.models import Models

        # Lazy default: empty registry; tests inject stream_fn.
        resolved_fn = Models([]).stream_simple  # type: ignore[assignment]

    resolved_api_key: str | None = config.api_key
    if config.get_api_key is not None:
        key = await _maybe_await(config.get_api_key(config.model.provider))
        if key:
            resolved_api_key = key

    reasoning = config.reasoning
    if reasoning == "off":
        reasoning = None

    options = SimpleStreamOptions(
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        signal=signal or config.signal,
        api_key=resolved_api_key,
        reasoning=reasoning,
    )

    response = resolved_fn(config.model, llm_context, options)
    if inspect.isawaitable(response):
        response = await response

    partial_message: AssistantMessage | None = None
    added_partial = False

    async for event in response:
        if event.type == "start":
            partial_message = event.partial
            context.messages.append(partial_message)
            added_partial = True
            await _emit(emit, MessageStartEvent(message=replace(partial_message)))
        elif event.type in (
            "text_start",
            "text_delta",
            "text_end",
            "thinking_start",
            "thinking_delta",
            "thinking_end",
            "toolcall_start",
            "toolcall_delta",
            "toolcall_end",
        ):
            if partial_message is not None:
                partial_message = event.partial
                context.messages[-1] = partial_message
                await _emit(
                    emit,
                    MessageUpdateEvent(
                        assistant_message_event=event,
                        message=replace(partial_message),
                    ),
                )
        elif event.type in ("done", "error"):
            final_message = await response.result()
            if added_partial:
                context.messages[-1] = final_message
            else:
                context.messages.append(final_message)
            if not added_partial:
                await _emit(emit, MessageStartEvent(message=replace(final_message)))
            await _emit(emit, MessageEndEvent(message=final_message))
            return final_message

    final_message = await response.result()
    if added_partial:
        context.messages[-1] = final_message
    else:
        context.messages.append(final_message)
        await _emit(emit, MessageStartEvent(message=replace(final_message)))
    await _emit(emit, MessageEndEvent(message=final_message))
    return final_message


# ========== Tool execution ==========


@dataclass
class ExecutedToolCallBatch:
    messages: list[ToolResultMessage]
    terminate: bool


@dataclass
class PreparedToolCall:
    kind: Literal["prepared"] = "prepared"
    tool_call: AgentToolCall | None = None
    tool: AgentTool | None = None
    args: Any = None


@dataclass
class ImmediateToolCallOutcome:
    kind: Literal["immediate"] = "immediate"
    result: AgentToolResult[Any] | None = None
    is_error: bool = False


@dataclass
class ExecutedToolCallOutcome:
    result: AgentToolResult[Any]
    is_error: bool


@dataclass
class FinalizedToolCallOutcome:
    tool_call: AgentToolCall
    result: AgentToolResult[Any]
    is_error: bool


async def execute_tool_calls(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> ExecutedToolCallBatch:
    tool_calls = [c for c in assistant_message.content if c.type == "toolCall"]
    has_sequential = False
    for tc in tool_calls:
        tool = _find_tool(current_context, tc.name)
        if tool is not None and tool.execution_mode == "sequential":
            has_sequential = True
            break

    if config.tool_execution == "sequential" or has_sequential:
        return await _execute_tool_calls_sequential(
            current_context, assistant_message, tool_calls, config, signal, emit
        )
    return await _execute_tool_calls_parallel(
        current_context, assistant_message, tool_calls, config, signal, emit
    )


def _find_tool(context: AgentContext, name: str) -> AgentTool | None:
    for tool in context.tools or []:
        if tool.name == name:
            return tool
    return None


# 由于达到 max_tokens 限制被截断，生成错误结果，不实际执行工具
async def _fail_tool_calls_from_truncated_message(
    tool_calls: list[AgentToolCall],
    emit: AgentEventSink,
) -> ExecutedToolCallBatch:
    messages: list[ToolResultMessage] = []
    for tool_call in tool_calls:
        await _emit(
            emit,
            _tool_execution_start(tool_call),
        )
        finalized = FinalizedToolCallOutcome(
            tool_call=tool_call,
            result=_create_error_tool_result(
                f'Tool call "{tool_call.name}" was not executed: the response hit the output '
                "token limit, so its arguments may be truncated. Re-issue the tool call with "
                "complete arguments."
            ),
            is_error=True,
        )
        await _emit_tool_execution_end(finalized, emit)
        tool_result_message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(tool_result_message, emit)
        messages.append(tool_result_message)
    return ExecutedToolCallBatch(messages=messages, terminate=False)


async def _execute_tool_calls_sequential(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[AgentToolCall],
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> ExecutedToolCallBatch:
    finalized_calls: list[FinalizedToolCallOutcome] = []
    messages: list[ToolResultMessage] = []

    for tool_call in tool_calls:
        await _emit(emit, _tool_execution_start(tool_call))
        preparation = await prepare_tool_call(
            current_context, assistant_message, tool_call, config, signal
        )
        if preparation.kind == "immediate":
            finalized = FinalizedToolCallOutcome(
                tool_call=tool_call,
                result=preparation.result or _create_error_tool_result("Unknown error"),
                is_error=preparation.is_error,
            )
        else:
            assert preparation.tool is not None and preparation.tool_call is not None
            executed = await _execute_prepared_tool_call(preparation, signal, emit)
            finalized = await _finalize_executed_tool_call(
                current_context,
                assistant_message,
                preparation,
                executed,
                config,
                signal,
            )

        await _emit_tool_execution_end(finalized, emit)
        tool_result_message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(tool_result_message, emit)
        finalized_calls.append(finalized)
        messages.append(tool_result_message)

        if signal is not None and signal.aborted:
            break

    return ExecutedToolCallBatch(
        messages=messages,
        terminate=_should_terminate_tool_batch(finalized_calls),
    )


async def _execute_tool_calls_parallel(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[AgentToolCall],
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> ExecutedToolCallBatch:
    finalized_entries: list[FinalizedToolCallOutcome | Callable[[], Awaitable[FinalizedToolCallOutcome]]] = []

    for tool_call in tool_calls:
        await _emit(emit, _tool_execution_start(tool_call))
        preparation = await prepare_tool_call(
            current_context, assistant_message, tool_call, config, signal
        )
        if preparation.kind == "immediate":
            finalized = FinalizedToolCallOutcome(
                tool_call=tool_call,
                result=preparation.result or _create_error_tool_result("Unknown error"),
                is_error=preparation.is_error,
            )
            await _emit_tool_execution_end(finalized, emit)
            finalized_entries.append(finalized)
            if signal is not None and signal.aborted:
                break
            continue

        assert preparation.tool is not None and preparation.tool_call is not None

        async def _run_parallel(
            prep: PreparedToolCall = preparation,
        ) -> FinalizedToolCallOutcome:
            executed = await _execute_prepared_tool_call(prep, signal, emit)
            finalized = await _finalize_executed_tool_call(
                current_context,
                assistant_message,
                prep,
                executed,
                config,
                signal,
            )
            await _emit_tool_execution_end(finalized, emit)
            return finalized

        finalized_entries.append(_run_parallel)
        if signal is not None and signal.aborted:
            break

    ordered: list[FinalizedToolCallOutcome] = []
    for entry in finalized_entries:
        if callable(entry):
            ordered.append(await entry())
        else:
            ordered.append(entry)

    messages: list[ToolResultMessage] = []
    for finalized in ordered:
        tool_result_message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(tool_result_message, emit)
        messages.append(tool_result_message)

    return ExecutedToolCallBatch(
        messages=messages,
        terminate=_should_terminate_tool_batch(ordered),
    )


async def prepare_tool_call(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_call: AgentToolCall,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
) -> PreparedToolCall | ImmediateToolCallOutcome:
    tool = _find_tool(current_context, tool_call.name)
    if tool is None:
        return ImmediateToolCallOutcome(
            result=_create_error_tool_result(f"Tool {tool_call.name} not found"),
            is_error=True,
        )

    try:
        prepared_tool_call = _prepare_tool_call_arguments(tool, tool_call)
        validated_args = validate_tool_arguments(tool.as_tool(), prepared_tool_call)
        if config.before_tool_call is not None:
            before_result = await _maybe_await(
                config.before_tool_call(
                    BeforeToolCallContext(
                        assistant_message=assistant_message,
                        tool_call=tool_call,
                        args=validated_args,
                        context=current_context,
                    ),
                    signal,
                )
            )
            if signal is not None and signal.aborted:
                return ImmediateToolCallOutcome(
                    result=_create_error_tool_result("Operation aborted"),
                    is_error=True,
                )
            if isinstance(before_result, BeforeToolCallResult) and before_result.block:
                reason = before_result.reason or "Tool execution was blocked"
                return ImmediateToolCallOutcome(
                    result=_create_error_tool_result(reason),
                    is_error=True,
                )
        if signal is not None and signal.aborted:
            return ImmediateToolCallOutcome(
                result=_create_error_tool_result("Operation aborted"),
                is_error=True,
            )
        return PreparedToolCall(
            tool_call=tool_call,
            tool=tool,
            args=validated_args,
        )
    except Exception as error:
        message = str(error)
        return ImmediateToolCallOutcome(
            result=_create_error_tool_result(message),
            is_error=True,
        )


def _prepare_tool_call_arguments(tool: AgentTool, tool_call: AgentToolCall) -> AgentToolCall:
    prepared_arguments = tool.prepare_arguments(tool_call.arguments)
    if prepared_arguments is tool_call.arguments:
        return tool_call
    return replace(tool_call, arguments=prepared_arguments)


async def _execute_prepared_tool_call(
    prepared: PreparedToolCall,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> ExecutedToolCallOutcome:
    from pi_agent.types import ToolExecutionUpdateEvent

    assert prepared.tool is not None and prepared.tool_call is not None
    update_events: list[Awaitable[None]] = []
    accepting_updates = True

    def on_update(partial_result: AgentToolResult[Any]) -> None:
        if not accepting_updates:
            return
        coro = _emit(
            emit,
            ToolExecutionUpdateEvent(
                tool_call_id=prepared.tool_call.id,  # type: ignore[union-attr]
                tool_name=prepared.tool_call.name,  # type: ignore[union-attr]
                args=prepared.tool_call.arguments,  # type: ignore[union-attr]
                partial_result=partial_result,
            ),
        )
        if inspect.isawaitable(coro):
            update_events.append(coro)

    try:
        result = await prepared.tool.execute(
            prepared.tool_call.id,
            prepared.args,
            signal,
            on_update,
        )
        accepting_updates = False
        await asyncio.gather(*update_events)
        return ExecutedToolCallOutcome(result=result, is_error=False)
    except Exception as error:
        accepting_updates = False
        await asyncio.gather(*update_events, return_exceptions=True)
        message = str(error)
        return ExecutedToolCallOutcome(
            result=_create_error_tool_result(message),
            is_error=True,
        )
    finally:
        accepting_updates = False


async def _finalize_executed_tool_call(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    prepared: PreparedToolCall,
    executed: ExecutedToolCallOutcome,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
) -> FinalizedToolCallOutcome:
    assert prepared.tool_call is not None
    result = executed.result
    is_error = executed.is_error

    if config.after_tool_call is not None:
        try:
            after_result = await _maybe_await(
                config.after_tool_call(
                    AfterToolCallContext(
                        assistant_message=assistant_message,
                        tool_call=prepared.tool_call,
                        args=prepared.args,
                        result=result,
                        is_error=is_error,
                        context=current_context,
                    ),
                    signal,
                )
            )
            if isinstance(after_result, AfterToolCallResult):
                result = AgentToolResult(
                    content=after_result.content if after_result.content is not None else result.content,
                    details=after_result.details if after_result.details is not None else result.details,
                    terminate=after_result.terminate if after_result.terminate is not None else result.terminate,
                )
                if after_result.is_error is not None:
                    is_error = after_result.is_error
        except Exception as error:
            message = str(error)
            result = _create_error_tool_result(message)
            is_error = True

    return FinalizedToolCallOutcome(
        tool_call=prepared.tool_call,
        result=result,
        is_error=is_error,
    )


def _should_terminate_tool_batch(finalized_calls: list[FinalizedToolCallOutcome]) -> bool:
    return bool(finalized_calls) and all(f.result.terminate for f in finalized_calls)


def _create_error_tool_result(message: str) -> AgentToolResult[dict[str, Any]]:
    return AgentToolResult(
        content=[TextContent(text=message)],
        details={},
    )


def _create_tool_result_message(finalized: FinalizedToolCallOutcome) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=finalized.tool_call.id,
        tool_name=finalized.tool_call.name,
        content=finalized.result.content or [],
        details=finalized.result.details,
        is_error=finalized.is_error,
        timestamp=int(time.time() * 1000),
    )


async def _emit_tool_execution_end(
    finalized: FinalizedToolCallOutcome,
    emit: AgentEventSink,
) -> None:
    from pi_agent.types import ToolExecutionEndEvent

    await _emit(
        emit,
        ToolExecutionEndEvent(
            tool_call_id=finalized.tool_call.id,
            tool_name=finalized.tool_call.name,
            result=finalized.result,
            is_error=finalized.is_error,
        ),
    )


async def _emit_tool_result_message(
    tool_result_message: ToolResultMessage,
    emit: AgentEventSink,
) -> None:
    from pi_agent.types import MessageEndEvent, MessageStartEvent

    await _emit(emit, MessageStartEvent(message=tool_result_message))
    await _emit(emit, MessageEndEvent(message=tool_result_message))


def _tool_execution_start(tool_call: AgentToolCall) -> AgentEvent:
    from pi_agent.types import ToolExecutionStartEvent

    return ToolExecutionStartEvent(
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
        args=tool_call.arguments,
    )


async def _emit(emit: AgentEventSink, event: AgentEvent) -> None:
    """统一处理同步和异步两种事件接收器"""
    result = emit(event)
    if inspect.isawaitable(result):
        await result


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
