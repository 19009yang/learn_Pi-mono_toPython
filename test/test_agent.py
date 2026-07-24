"""Tests for pi_agent.agent — Agent class (roadmap Phase 2.3)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_ai.event_stream import AbortSignal, EventStream
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
from pi_agent.agent import Agent, AgentOptions, PendingMessageQueue
from pi_agent.messages import AgentMessage
from pi_agent.types import AgentEvent, AgentContext, AgentLoopConfig, AgentTool, AgentToolResult


# ========== Shared helpers ==========


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
    """Simple convert_to_llm that only keeps LLM-native roles."""
    return [
        m
        for m in messages
        if getattr(m, "role", None) in ("user", "assistant", "toolResult")
    ]


class MockAssistantStream(EventStream[Any, AssistantMessage]):
    """Fake stream that immediately pushes a DoneEvent via call_soon."""

    def __init__(self) -> None:
        super().__init__(
            lambda event: event.type in ("done", "error"),
            lambda event: (
                event.message if event.type == "done" else event.error  # type: ignore[union-attr]
            ),
        )


class EchoTool(AgentTool):
    """Simple tool for testing: echoes back the input value."""

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


@pytest.fixture(autouse=True)
def _reset_tool_state() -> None:
    EchoTool.executed = []


# ========== PendingMessageQueue tests ==========


class TestPendingMessageQueue:
    def test_drain_all_mode(self) -> None:
        """In "all" mode, drain returns every queued message."""
        queue = PendingMessageQueue("all")
        m1 = _user("a")
        m2 = _user("b")
        queue.enqueue(m1)
        queue.enqueue(m2)
        drained = queue.drain()
        assert drained == [m1, m2]
        assert not queue.has_items()

    def test_drain_one_at_a_time_mode(self) -> None:
        """In "one-at-a-time" mode, drain returns only the first message."""
        queue = PendingMessageQueue("one-at-a-time")
        m1 = _user("a")
        m2 = _user("b")
        queue.enqueue(m1)
        queue.enqueue(m2)
        first = queue.drain()
        assert first == [m1]
        assert queue.has_items()
        second = queue.drain()
        assert second == [m2]
        assert not queue.has_items()

    def test_drain_empty_queue(self) -> None:
        """Draining an empty queue returns [] regardless of mode."""
        for mode in ("all", "one-at-a-time"):
            queue = PendingMessageQueue(mode)
            assert queue.drain() == []

    def test_clear_removes_all_messages(self) -> None:
        queue = PendingMessageQueue("all")
        queue.enqueue(_user("x"))
        queue.clear()
        assert not queue.has_items()

    def test_mode_can_be_changed(self) -> None:
        queue = PendingMessageQueue("one-at-a-time")
        queue.enqueue(_user("a"))
        queue.enqueue(_user("b"))
        queue.mode = "all"
        assert queue.drain() == [_user("a"), _user("b")]


# ========== Agent constructor tests ==========


class TestAgentConstructor:
    def test_default_options(self) -> None:
        """Agent with no options uses default model and convert_to_llm."""
        agent = Agent()
        assert agent.state.model.id == "unknown"
        assert agent.state.messages == []
        assert agent.state.tools == []
        assert agent.steering_mode == "one-at-a-time"
        assert agent.follow_up_mode == "one-at-a-time"
        assert agent.tool_execution == "parallel"

    def test_custom_options(self) -> None:
        """Agent respects custom initial state and stream_fn."""
        agent = Agent(
            AgentOptions(
                initial_state={
                    "model": _model(),
                    "system_prompt": "You are a helper",
                },
                steering_mode="all",
                follow_up_mode="all",
                tool_execution="sequential",
            )
        )
        assert agent.state.model.id == "mock"
        assert agent.state.system_prompt == "You are a helper"
        assert agent.steering_mode == "all"
        assert agent.follow_up_mode == "all"
        assert agent.tool_execution == "sequential"

    def test_initial_state_tools_and_messages_copied(self) -> None:
        """Tools and messages passed to initial_state are copied on write."""
        tools = [EchoTool()]
        messages = [_user("hi")]
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model(), "tools": tools, "messages": messages}
            )
        )
        # Mutating the original list should not affect the agent
        tools.append(EchoTool())
        messages.append(_user("extra"))
        assert len(agent.state.tools) == 1
        assert len(agent.state.messages) == 1


# ========== Agent.subscribe tests ==========


@pytest.mark.asyncio
class TestAgentSubscribe:
    async def test_subscribe_receives_events(self) -> None:
        """Subscribed listener receives all lifecycle events."""
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )
        events: list[Any] = []

        def on_event(event: AgentEvent, signal: AbortSignal) -> None:
            events.append(event)

        unsub = agent.subscribe(on_event)

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()
            asyncio.get_event_loop().call_soon(
                lambda: stream.push(
                    DoneEvent(reason="stop", message=_assistant([TextContent(text="Hi")]))
                )
            )
            return stream

        agent.stream_fn = stream_fn
        await agent.prompt("Hello")
        await agent.wait_for_idle()

        event_types = [e.type for e in events]
        assert "agent_start" in event_types
        assert "turn_start" in event_types
        assert "message_start" in event_types
        assert "message_end" in event_types
        assert "turn_end" in event_types
        assert "agent_end" in event_types

    async def test_unsubscribe_stops_receiving(self) -> None:
        """After unsubscribing, listener no longer receives events."""
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )
        events: list[Any] = []

        unsub = agent.subscribe(lambda ev, sig: events.append(ev))
        unsub()  # immediately unsubscribe

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()
            asyncio.get_event_loop().call_soon(
                lambda: stream.push(
                    DoneEvent(reason="stop", message=_assistant([TextContent(text="Hi")]))
                )
            )
            return stream

        agent.stream_fn = stream_fn
        await agent.prompt("Hello")
        await agent.wait_for_idle()
        assert events == []


# ========== Agent.prompt tests ==========


@pytest.mark.asyncio
class TestAgentPrompt:
    async def test_prompt_string_input(self) -> None:
        """prompt() with a string creates a UserMessage automatically."""
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()
            asyncio.get_event_loop().call_soon(
                lambda: stream.push(
                    DoneEvent(reason="stop", message=_assistant([TextContent(text="Reply")]))
                )
            )
            return stream

        agent.stream_fn = stream_fn
        await agent.prompt("Hello")
        await agent.wait_for_idle()

        # State should have user message + assistant message
        assert len(agent.state.messages) == 2
        assert agent.state.messages[0].role == "user"
        assert agent.state.messages[1].role == "assistant"

    async def test_prompt_with_tool_calls(self) -> None:
        """prompt() with tool calls produces full lifecycle sequence."""
        EchoTool.executed = []
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model(), "tools": [EchoTool()]},
                convert_to_llm=_identity_convert,
            )
        )
        call_index = 0

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            nonlocal call_index
            stream = MockAssistantStream()

            def push() -> None:
                nonlocal call_index
                if call_index == 0:
                    msg = _assistant(
                        [ToolCall(id="tool-1", name="echo", arguments={"value": "hello"})],
                        stop_reason="toolUse",
                    )
                    stream.push(DoneEvent(reason="toolUse", message=msg))
                else:
                    stream.push(
                        DoneEvent(reason="stop", message=_assistant([TextContent(text="done")]))
                    )
                call_index += 1

            asyncio.get_event_loop().call_soon(push)
            return stream

        agent.stream_fn = stream_fn
        events: list[Any] = []
        agent.subscribe(lambda ev, sig: events.append(ev))

        await agent.prompt("echo hello")
        await agent.wait_for_idle()

        assert EchoTool.executed == ["hello"]
        assert [m.role for m in agent.state.messages] == [
            "user",
            "assistant",
            "toolResult",
            "assistant",
        ]
        # Verify event sequence includes tool execution events
        event_types = [e.type for e in events]
        assert "tool_execution_start" in event_types
        assert "tool_execution_end" in event_types

    async def test_prompt_raises_if_already_running(self) -> None:
        """prompt() raises RuntimeError if an active run exists.

        We run prompt() as a background task with a slow stream, then
        try to call prompt() again while the first is still running.
        """
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )

        # Use a real slow stream: the agent_loop will await the
        # stream's result, so the run stays active until done is pushed.
        stream_started = asyncio.Event()

        def slow_stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()

            async def _delayed_push() -> None:
                stream_started.set()
                await asyncio.sleep(0.5)
                stream.push(DoneEvent(reason="stop", message=_assistant([TextContent(text="done")])))

            asyncio.create_task(_delayed_push())
            return stream

        agent.stream_fn = slow_stream_fn

        # Run prompt in background so the current task can try a second prompt
        prompt_task = asyncio.create_task(agent.prompt("first"))
        # Wait for the slow stream to be started (agent is now running)
        await stream_started.wait()

        # Second prompt should raise
        with pytest.raises(RuntimeError, match="already processing"):
            await agent.prompt("second")

        # Let the first prompt finish
        await prompt_task
        await agent.wait_for_idle()


# ========== Agent.steer / follow_up tests ==========


@pytest.mark.asyncio
class TestAgentSteerFollowUp:
    async def test_steer_queue_mechanism(self) -> None:
        """steer() queues a message that can be drained via the queue.

        Direct queue mechanism test: verify that steer() puts a message
        into the steering queue and drain() retrieves it correctly.
        Integration of steering into the running loop is tested indirectly
        via the agent_loop tests (Phase 2.2).
        """
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
                steering_mode="all",
            )
        )

        msg = _user("steered")
        agent.steer(msg)
        assert agent.has_queued_messages()
        assert agent._steering_queue.has_items()

        # Drain the queue — in "all" mode, returns all messages
        drained = agent._steering_queue.drain()
        assert len(drained) == 1
        assert drained[0] is msg

    async def test_follow_up_queue_drain(self) -> None:
        """follow_up() queues a message for after the inner loop exits."""
        agent = Agent(AgentOptions(initial_state={"model": _model()}, follow_up_mode="all"))

        agent.follow_up(_user("follow up msg"))
        assert agent.has_queued_messages()

        drained = agent._follow_up_queue.drain()
        assert len(drained) == 1
        assert drained[0].role == "user"

    async def test_clear_all_queues(self) -> None:
        """clear_all_queues removes both steering and follow-up messages."""
        agent = Agent(AgentOptions(initial_state={"model": _model()}))
        agent.steer(_user("s"))
        agent.follow_up(_user("f"))
        assert agent.has_queued_messages()

        agent.clear_all_queues()
        assert not agent.has_queued_messages()


# ========== Agent.abort tests ==========


@pytest.mark.asyncio
class TestAgentAbort:
    async def test_signal_none_when_idle(self) -> None:
        """signal property returns None when no run is active."""
        agent = Agent(AgentOptions(initial_state={"model": _model()}))
        assert agent.signal is None

    async def test_abort_signal_accessible_during_run(self) -> None:
        """signal property returns AbortSignal during a running prompt.

        prompt() is blocking, so we run it as a background task to
        access the agent while it's active.
        """
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )

        # Slow stream — delays done event so we can observe the signal
        stream_started = asyncio.Event()

        def slow_stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()

            async def _delayed() -> None:
                stream_started.set()
                await asyncio.sleep(0.3)
                stream.push(DoneEvent(reason="stop", message=_assistant([TextContent(text="done")])))

            asyncio.create_task(_delayed())
            return stream

        agent.stream_fn = slow_stream_fn

        # Run prompt as background task
        prompt_task = asyncio.create_task(agent.prompt("hello"))
        await stream_started.wait()

        # While the task is running, signal should exist and not aborted
        assert agent.signal is not None
        assert not agent.signal.aborted

        # Let the prompt finish naturally
        await prompt_task
        await agent.wait_for_idle()

        # After run completes, signal should be None
        assert agent.signal is None

    async def test_abort_cancels_run(self) -> None:
        """abort() sets the AbortSignal during a running prompt.

        The run completes (cooperative abort), but the signal is marked
        as aborted. We run prompt() as a background task, then abort.
        """
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )
        events: list[Any] = []

        # Subscribe to capture events (including any error/aborted ones)
        agent.subscribe(lambda ev, sig: events.append(ev))

        # Slow stream — long delay so abort fires before done
        stream_started = asyncio.Event()

        def slow_stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()

            async def _delayed() -> None:
                stream_started.set()
                await asyncio.sleep(0.5)
                # Push done even after abort (cooperative cancel
                # doesn't prevent the stream from completing)
                stream.push(DoneEvent(reason="stop", message=_assistant([TextContent(text="late")])))

            asyncio.create_task(_delayed())
            return stream

        agent.stream_fn = slow_stream_fn

        # Run prompt as background task
        prompt_task = asyncio.create_task(agent.prompt("hello"))
        await stream_started.wait()

        # Abort while running
        agent.abort()
        assert agent.signal is not None
        assert agent.signal.aborted

        # Wait for the run to complete (cooperative — it finishes)
        try:
            await prompt_task
        except Exception:
            # abort may cause error events or exceptions
            pass

        await agent.wait_for_idle()
        assert agent.signal is None


# ========== Agent.wait_for_idle tests ==========


@pytest.mark.asyncio
class TestAgentWaitForIdle:
    async def test_wait_for_idle_returns_immediately_when_no_run(self) -> None:
        """wait_for_idle() resolves immediately when no run is active."""
        agent = Agent(AgentOptions(initial_state={"model": _model()}))
        await agent.wait_for_idle()  # should resolve immediately

    async def test_wait_for_idle_resolves_after_run(self) -> None:
        """wait_for_idle() resolves after the run and listeners settle."""
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()
            asyncio.get_event_loop().call_soon(
                lambda: stream.push(
                    DoneEvent(reason="stop", message=_assistant([TextContent(text="Hi")]))
                )
            )
            return stream

        agent.stream_fn = stream_fn
        await agent.prompt("Hello")
        await agent.wait_for_idle()

        # After idle, state should reflect completed run
        assert agent.state.is_streaming is False
        assert agent.state.streaming_message is None
        assert len(agent.state.messages) == 2


# ========== Agent.reset tests ==========


@pytest.mark.asyncio
class TestAgentReset:
    async def test_reset_clears_state(self) -> None:
        """reset() clears messages, streaming state, and queues."""
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()
            asyncio.get_event_loop().call_soon(
                lambda: stream.push(
                    DoneEvent(reason="stop", message=_assistant([TextContent(text="Hi")]))
                )
            )
            return stream

        agent.stream_fn = stream_fn
        await agent.prompt("Hello")
        await agent.wait_for_idle()

        # Add some queued messages
        agent.steer(_user("steer"))
        agent.follow_up(_user("follow"))

        agent.reset()

        assert agent.state.messages == []
        assert agent.state.is_streaming is False
        assert agent.state.streaming_message is None
        assert agent.state.pending_tool_calls == frozenset()
        assert agent.state.error_message is None
        assert not agent.has_queued_messages()


# ========== Agent.continue_loop tests ==========


@pytest.mark.asyncio
class TestAgentContinueLoop:
    async def test_continue_from_tool_result(self) -> None:
        """continue_loop() resumes from a context ending with toolResult."""
        agent = Agent(
            AgentOptions(
                initial_state={
                    "model": _model(),
                    "tools": [EchoTool()],
                    "messages": [
                        _user("hi"),
                        _assistant(
                            [ToolCall(id="t1", name="echo", arguments={"value": "x"})],
                            "toolUse",
                        ),
                        ToolResultMessage(
                            tool_call_id="t1",
                            tool_name="echo",
                            content=[TextContent(text="echoed: x")],
                            is_error=False,
                            timestamp=2,
                        ),
                    ],
                },
                convert_to_llm=_identity_convert,
            )
        )

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()
            asyncio.get_event_loop().call_soon(
                lambda: stream.push(
                    DoneEvent(reason="stop", message=_assistant([TextContent(text="continued")]))
                )
            )
            return stream

        agent.stream_fn = stream_fn
        await agent.continue_loop()
        await agent.wait_for_idle()

        # The assistant message should have been appended
        assert agent.state.messages[-1].role == "assistant"

    async def test_continue_raises_if_last_is_assistant(self) -> None:
        """continue_loop() raises when last message is assistant with no queued messages."""
        agent = Agent(
            AgentOptions(
                initial_state={
                    "model": _model(),
                    "messages": [_user("hi"), _assistant([], "stop")],
                },
                convert_to_llm=_identity_convert,
            )
        )

        with pytest.raises(RuntimeError, match="assistant"):
            await agent.continue_loop()

    async def test_continue_raises_if_already_running(self) -> None:
        """continue_loop() raises RuntimeError if an active run exists.

        Run prompt() as a background task, then try continue_loop()
        concurrently.
        """
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )

        stream_started = asyncio.Event()

        def slow_stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()

            async def _delayed() -> None:
                stream_started.set()
                await asyncio.sleep(0.5)
                stream.push(DoneEvent(reason="stop", message=_assistant([TextContent(text="done")])))

            asyncio.create_task(_delayed())
            return stream

        agent.stream_fn = slow_stream_fn

        # Run prompt as background task
        prompt_task = asyncio.create_task(agent.prompt("hello"))
        await stream_started.wait()

        with pytest.raises(RuntimeError, match="already processing"):
            await agent.continue_loop()

        # Clean up
        await prompt_task
        await agent.wait_for_idle()


# ========== Agent state update during events tests ==========


@pytest.mark.asyncio
class TestAgentStateUpdates:
    async def test_streaming_message_set_and_cleared(self) -> None:
        """streaming_message is set on message_start/update, cleared on end."""
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )

        # Track streaming_message changes
        streaming_snapshots: list[Any] = []

        async def on_event(event: AgentEvent, signal: AbortSignal) -> None:
            streaming_snapshots.append(agent.state.streaming_message)

        agent.subscribe(on_event)

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()
            asyncio.get_event_loop().call_soon(
                lambda: stream.push(
                    DoneEvent(reason="stop", message=_assistant([TextContent(text="Hi")]))
                )
            )
            return stream

        agent.stream_fn = stream_fn
        await agent.prompt("Hello")
        await agent.wait_for_idle()

        # streaming_message should be None after idle
        assert agent.state.streaming_message is None
        # There should have been non-None snapshots during the run
        assert any(s is not None for s in streaming_snapshots)

    async def test_pending_tool_calls_tracked(self) -> None:
        """pending_tool_calls grows on tool_execution_start, shrinks on end."""
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model(), "tools": [EchoTool()]},
                convert_to_llm=_identity_convert,
            )
        )
        call_index = 0
        pending_snapshots: list[frozenset[str]] = []

        async def on_event(event: AgentEvent, signal: AbortSignal) -> None:
            pending_snapshots.append(agent.state.pending_tool_calls)

        agent.subscribe(on_event)

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            nonlocal call_index
            stream = MockAssistantStream()

            def push() -> None:
                nonlocal call_index
                if call_index == 0:
                    msg = _assistant(
                        [ToolCall(id="tool-1", name="echo", arguments={"value": "hello"})],
                        stop_reason="toolUse",
                    )
                    stream.push(DoneEvent(reason="toolUse", message=msg))
                else:
                    stream.push(DoneEvent(reason="stop", message=_assistant([TextContent(text="done")])))
                call_index += 1

            asyncio.get_event_loop().call_soon(push)
            return stream

        agent.stream_fn = stream_fn
        await agent.prompt("echo hello")
        await agent.wait_for_idle()

        # Should have had "tool-1" in pending at some point
        assert any("tool-1" in s for s in pending_snapshots)
        # After idle, pending should be empty
        assert agent.state.pending_tool_calls == frozenset()

    async def test_is_streaming_flag(self) -> None:
        """is_streaming is True during a run, False after idle."""
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )
        streaming_flags: list[bool] = []

        async def on_event(event: AgentEvent, signal: AbortSignal) -> None:
            streaming_flags.append(agent.state.is_streaming)

        agent.subscribe(on_event)

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()
            asyncio.get_event_loop().call_soon(
                lambda: stream.push(
                    DoneEvent(reason="stop", message=_assistant([TextContent(text="Hi")]))
                )
            )
            return stream

        agent.stream_fn = stream_fn
        await agent.prompt("Hello")
        await agent.wait_for_idle()

        # Should have been True during run
        assert True in streaming_flags
        # After idle, should be False
        assert agent.state.is_streaming is False

    async def test_messages_appended_on_message_end(self) -> None:
        """state.messages grows when message_end event fires."""
        agent = Agent(
            AgentOptions(
                initial_state={"model": _model()},
                convert_to_llm=_identity_convert,
            )
        )

        def stream_fn(_model: Model, _ctx: Any, _options: Any = None) -> MockAssistantStream:
            stream = MockAssistantStream()
            asyncio.get_event_loop().call_soon(
                lambda: stream.push(
                    DoneEvent(reason="stop", message=_assistant([TextContent(text="Hi")]))
                )
            )
            return stream

        agent.stream_fn = stream_fn
        await agent.prompt("Hello")
        await agent.wait_for_idle()

        # Should have user + assistant in state.messages
        assert len(agent.state.messages) == 2
        assert agent.state.messages[0].role == "user"
        assert agent.state.messages[1].role == "assistant"
