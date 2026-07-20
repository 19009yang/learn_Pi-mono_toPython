"""Tests for pi_agent.agent_loop (roadmap Phase 2.2)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from pi_ai.event_stream import EventStream
from pi_ai.types import (
    AssistantMessage,
    CostInfo,
    DoneEvent,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from pi_agent.agent_loop import agent_loop, agent_loop_continue
from pi_agent.messages import AgentMessage
from pi_agent.types import AgentContext, AgentLoopConfig, AgentTool, AgentToolResult


def _model() -> Model:
    return Model(
        id="mock",
        name="mock",
        api="openai-completions",
        provider="test",
        base_url="https://example.invalid",
        context_window=8192,
        max_tokens=2048,
        cost=ModelCost(0, 0, 0, 0),
    )


def _usage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cache_read=0,
        cache_write=0,
        total_tokens=0,
        cost=CostInfo(0, 0, 0, 0, 0),
    )


def _assistant(
    content: list[Any],
    stop_reason: str = "stop",
) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="test",
        model="mock",
        usage=_usage(),
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=1,
    )


def _user(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=1)


def _identity_convert(messages: list[AgentMessage]) -> list[AgentMessage]:
    return [
        m
        for m in messages
        if getattr(m, "role", None) in ("user", "assistant", "toolResult")
    ]


class MockAssistantStream(EventStream[Any, AssistantMessage]):
    def __init__(self) -> None:
        super().__init__(
            lambda event: event.type in ("done", "error"),
            lambda event: (
                event.message if event.type == "done" else event.error  # type: ignore[union-attr]
            ),
        )


class EchoTool(AgentTool):
    executed: list[str] = []

    def __init__(self) -> None:
        super().__init__(
            name="echo",
            label="Echo",
            description="Echo a string",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult[None]:
        EchoTool.executed.append(str(params["value"]))
        return AgentToolResult(content=[TextContent(text=f"echoed: {params['value']}")], details=None)


class AddTool(AgentTool):
    executed: list[tuple[int, int]] = []

    def __init__(self) -> None:
        super().__init__(
            name="add",
            label="Add",
            description="Add two numbers",
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult[None]:
        a, b = int(params["a"]), int(params["b"])
        AddTool.executed.append((a, b))
        return AgentToolResult(content=[TextContent(text=str(a + b))], details=None)


@pytest.fixture(autouse=True)
def _reset_tool_state() -> None:
    EchoTool.executed = []
    AddTool.executed = []
    SlowTool.executed_times = []


@pytest.mark.asyncio
async def test_agent_loop_text_only() -> None:
    context = AgentContext(system_prompt="sys", messages=[], tools=[])
    config = AgentLoopConfig(model=_model(), convert_to_llm=_identity_convert)

    def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
        stream = MockAssistantStream()
        asyncio.get_event_loop().call_soon(
            lambda: stream.push(
                DoneEvent(reason="stop", message=_assistant([TextContent(text="Hi")]))
            )
        )
        return stream

    events: list[Any] = []
    stream = agent_loop([_user("Hello")], context, config, stream_fn=stream_fn)
    async for event in stream:
        events.append(event)
    messages = await stream.result()

    assert [m.role for m in messages] == ["user", "assistant"]
    assert {e.type for e in events} >= {
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    }


@pytest.mark.asyncio
async def test_agent_loop_two_round_tool_calls() -> None:
    """roadmap 验证点：user → assistant(toolCall) → toolResult → assistant(text)."""
    EchoTool.executed = []
    context = AgentContext(system_prompt="", messages=[], tools=[EchoTool()])
    config = AgentLoopConfig(model=_model(), convert_to_llm=_identity_convert)
    call_index = 0

    def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
        nonlocal call_index
        stream = MockAssistantStream()

        def push() -> None:
            nonlocal call_index
            if call_index == 0:
                msg = _assistant(
                    [
                        ToolCall(
                            id="tool-1",
                            name="echo",
                            arguments={"value": "hello"},
                        )
                    ],
                    stop_reason="toolUse",
                )
                stream.push(DoneEvent(reason="toolUse", message=msg))
            else:
                stream.push(
                    DoneEvent(
                        reason="stop",
                        message=_assistant([TextContent(text="done")]),
                    )
                )
            call_index += 1

        asyncio.get_event_loop().call_soon(push)
        return stream

    stream = agent_loop([_user("echo hello")], context, config, stream_fn=stream_fn)
    async for _ in stream:
        pass
    messages = await stream.result()

    assert EchoTool.executed == ["hello"]
    assert [m.role for m in messages] == [
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ]
    assert messages[1].content[0].type == "toolCall"
    assert messages[2].tool_name == "echo"
    assert messages[3].content[0].text == "done"


@pytest.mark.asyncio
async def test_length_truncated_tool_calls_not_executed() -> None:
    EchoTool.executed = []
    context = AgentContext(system_prompt="", messages=[], tools=[EchoTool()])
    config = AgentLoopConfig(model=_model(), convert_to_llm=_identity_convert)
    call_index = 0

    def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
        nonlocal call_index
        stream = MockAssistantStream()

        def push() -> None:
            nonlocal call_index
            if call_index == 0:
                msg = _assistant(
                    [ToolCall(id="tool-1", name="echo", arguments={"value": "hel"})],
                    stop_reason="length",
                )
                stream.push(DoneEvent(reason="length", message=msg))
            else:
                stream.push(
                    DoneEvent(
                        reason="stop",
                        message=_assistant([TextContent(text="done")]),
                    )
                )
            call_index += 1

        asyncio.get_event_loop().call_soon(push)
        return stream

    events: list[Any] = []
    stream = agent_loop([_user("echo")], context, config, stream_fn=stream_fn)
    async for event in stream:
        events.append(event)
    messages = await stream.result()

    assert EchoTool.executed == []
    assert call_index == 2
    tool_end = next(e for e in events if e.type == "tool_execution_end")
    assert tool_end.is_error is True
    assert messages[-1].role == "assistant"


@pytest.mark.asyncio
async def test_parallel_tool_results_in_source_order() -> None:
    context = AgentContext(system_prompt="", messages=[], tools=[EchoTool()])
    config = AgentLoopConfig(
        model=_model(),
        convert_to_llm=_identity_convert,
        tool_execution="parallel",
    )
    call_index = 0

    def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
        nonlocal call_index
        stream = MockAssistantStream()

        def push() -> None:
            nonlocal call_index
            if call_index == 0:
                msg = _assistant(
                    [
                        ToolCall(id="t1", name="echo", arguments={"value": "first"}),
                        ToolCall(id="t2", name="echo", arguments={"value": "second"}),
                    ],
                    stop_reason="toolUse",
                )
                stream.push(DoneEvent(reason="toolUse", message=msg))
            else:
                stream.push(
                    DoneEvent(
                        reason="stop",
                        message=_assistant([TextContent(text="done")]),
                    )
                )
            call_index += 1

        asyncio.get_event_loop().call_soon(push)
        return stream

    stream = agent_loop([_user("echo both")], context, config, stream_fn=stream_fn)
    async for _ in stream:
        pass
    messages = await stream.result()

    assert [m.role for m in messages] == [
        "user",
        "assistant",
        "toolResult",
        "toolResult",
        "assistant",
    ]
    assert messages[2].tool_call_id == "t1"
    assert messages[3].tool_call_id == "t2"


@pytest.mark.asyncio
async def test_agent_loop_continue_from_tool_result() -> None:
    context = AgentContext(
        system_prompt="",
        messages=[
            _user("hi"),
            _assistant([ToolCall(id="t1", name="echo", arguments={"value": "x"})], "toolUse"),
            ToolResultMessage(
                tool_call_id="t1",
                tool_name="echo",
                content=[TextContent(text="echoed: x")],
                is_error=False,
                timestamp=2,
            ),
        ],
        tools=[EchoTool()],
    )
    config = AgentLoopConfig(model=_model(), convert_to_llm=_identity_convert)

    def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
        stream = MockAssistantStream()
        asyncio.get_event_loop().call_soon(
            lambda: stream.push(
                DoneEvent(
                    reason="stop",
                    message=_assistant([TextContent(text="continued")]),
                )
            )
        )
        return stream

    with pytest.raises(ValueError, match="assistant"):
        agent_loop_continue(
            AgentContext(system_prompt="", messages=[_user("x"), _assistant([], "stop")], tools=[]),
            AgentLoopConfig(model=_model(), convert_to_llm=_identity_convert),
        )

    events: list[Any] = []
    stream = agent_loop_continue(context, config, stream_fn=stream_fn)
    async for event in stream:
        events.append(event)
    messages = await stream.result()

    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert messages[0].content[0].text == "continued"
    assert context.messages[-1].role == "assistant"
    assert context.messages[-1].content[0].text == "continued"


@pytest.mark.asyncio
async def test_add_tool_second_round() -> None:
    """Two tools: echo then add across two LLM rounds."""
    context = AgentContext(system_prompt="", messages=[], tools=[EchoTool(), AddTool()])
    config = AgentLoopConfig(model=_model(), convert_to_llm=_identity_convert)
    call_index = 0

    def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
        nonlocal call_index
        stream = MockAssistantStream()

        def push() -> None:
            nonlocal call_index
            if call_index == 0:
                msg = _assistant(
                    [ToolCall(id="t1", name="add", arguments={"a": 2, "b": 3})],
                    stop_reason="toolUse",
                )
                stream.push(DoneEvent(reason="toolUse", message=msg))
            else:
                stream.push(
                    DoneEvent(
                        reason="stop",
                        message=_assistant([TextContent(text="sum done")]),
                    )
                )
            call_index += 1

        asyncio.get_event_loop().call_soon(push)
        return stream

    stream = agent_loop([_user("add 2 and 3")], context, config, stream_fn=stream_fn)
    async for _ in stream:
        pass
    messages = await stream.result()

    assert AddTool.executed == [(2, 3)]
    assert messages[2].content[0].text == "5"


class SlowTool(AgentTool):
    """Tool that sleeps for a fixed duration to verify parallel execution."""

    executed_times: list[float] = []

    def __init__(self, name: str, sleep_s: float) -> None:
        super().__init__(
            name=name,
            label=name,
            description=f"Sleeps {sleep_s}s",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )
        self._sleep_s = sleep_s

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult[None]:
        start = time.monotonic()
        await asyncio.sleep(self._sleep_s)
        elapsed = time.monotonic() - start
        SlowTool.executed_times.append(elapsed)
        return AgentToolResult(
            content=[TextContent(text=f"done after {elapsed:.2f}s")],
            details=None,
        )


@pytest.mark.asyncio
async def test_parallel_execution_is_concurrent() -> None:
    """Verify that parallel tool calls run concurrently (not sequentially).

    Two SlowTool calls with 0.1s each should complete in ~0.1s total
    (parallel) rather than ~0.2s (sequential).
    """
    SlowTool.executed_times = []
    slow_a = SlowTool(name="slow_a", sleep_s=0.1)
    slow_b = SlowTool(name="slow_b", sleep_s=0.1)
    context = AgentContext(system_prompt="", messages=[], tools=[slow_a, slow_b])
    config = AgentLoopConfig(
        model=_model(),
        convert_to_llm=_identity_convert,
        tool_execution="parallel",
    )
    call_index = 0

    def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
        nonlocal call_index
        stream = MockAssistantStream()

        def push() -> None:
            nonlocal call_index
            if call_index == 0:
                msg = _assistant(
                    [
                        ToolCall(id="t1", name="slow_a", arguments={"value": "a"}),
                        ToolCall(id="t2", name="slow_b", arguments={"value": "b"}),
                    ],
                    stop_reason="toolUse",
                )
                stream.push(DoneEvent(reason="toolUse", message=msg))
            else:
                stream.push(
                    DoneEvent(
                        reason="stop",
                        message=_assistant([TextContent(text="done")]),
                    )
                )
            call_index += 1

        asyncio.get_event_loop().call_soon(push)
        return stream

    start_wall = time.monotonic()
    stream = agent_loop([_user("run both")], context, config, stream_fn=stream_fn)
    async for _ in stream:
        pass
    messages = await stream.result()
    wall_elapsed = time.monotonic() - start_wall

    # 并行执行：wall-clock 应 < 0.3s（两个 0.1s 任务并行 + overhead）
    # 顺序执行：wall-clock 应 >= 0.2s
    assert wall_elapsed < 0.3, f"Parallel execution took {wall_elapsed:.2f}s (should be < 0.3s)"
    assert len(SlowTool.executed_times) == 2

    # toolResult 仍按源序排列
    assert [m.role for m in messages] == [
        "user",
        "assistant",
        "toolResult",
        "toolResult",
        "assistant",
    ]
    assert messages[2].tool_call_id == "t1"
    assert messages[3].tool_call_id == "t2"
