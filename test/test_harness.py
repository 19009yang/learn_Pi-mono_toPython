"""Phase 4.4 tests for the AgentHarness composition layer."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
import asyncio

import pytest

from pi_agent.compaction import CompactionSettings
from pi_agent.agent import Agent, AgentOptions
from pi_agent.harness import AgentHarness
from pi_agent.messages import CompactionSummaryMessage
from pi_agent.session import InMemorySessionStorage, Session
from pi_agent.types import AfterToolCallResult, BeforeToolCallResult
from pi_ai.event_stream import AbortSignal, AssistantMessageEventStream
from pi_ai.types import AssistantMessage, Context, CostInfo, DoneEvent, Model, ModelCost, TextContent, Usage, UserMessage


def _model() -> Model:
    return Model(
        id="fake", name="Fake", api="openai-completions", provider="fake",
        base_url="https://example.test/v1", context_window=600, max_tokens=256,
        cost=ModelCost(0, 0, 0, 0),
    )


def _assistant(text: str, timestamp: int = 2) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)], api="openai-completions", provider="fake", model="fake",
        usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)), stop_reason="stop", timestamp=timestamp,
    )


@dataclass
class _State:
    model: Model
    messages: list[Any]
    is_streaming: bool = False


class _FakeAgent:
    def __init__(self, messages: list[Any] | None = None) -> None:
        self.state = _State(_model(), list(messages or []))
        self.before_tool_call = None
        self.after_tool_call = None
        self._listeners: list[Any] = []

    def subscribe(self, listener: Any) -> Any:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def prompt(self, value: Any, images: Any = None) -> None:
        del images
        message = value if not isinstance(value, str) else UserMessage(content=value, timestamp=1)
        self.state.messages.append(message)
        self.state.messages.append(_assistant("done"))
        event = SimpleNamespace(type="turn_end")
        signal = AbortSignal()
        for listener in tuple(self._listeners):
            await listener(event, signal)


@dataclass
class _FakeModels:
    calls: int = 0

    async def complete_simple(self, model: Model, context: Context, options: object = None) -> AssistantMessage:
        del context, options
        self.calls += 1
        return AssistantMessage(
            content=[TextContent(text="## Goal\nContinue the compacted task")],
            api=model.api, provider=model.provider, model=model.id,
            usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)), stop_reason="stop", timestamp=10,
        )


@pytest.mark.asyncio
async def test_prompt_flushes_messages_and_restores_them() -> None:
    storage = await InMemorySessionStorage.create(session_id="harness-prompt")
    session = Session(storage)
    harness = await AgentHarness.create(_FakeAgent(), session)  # type: ignore[arg-type]
    events: list[str] = []
    harness.on("session_after_flush", lambda _context: events.append("flushed"))

    result = await harness.prompt("hello")

    assert [message.role for message in result.new_messages] == ["user", "assistant"]
    assert await session.build_context() == result.messages
    assert events == ["flushed"]

    restored = await AgentHarness.create(_FakeAgent(), session)  # type: ignore[arg-type]
    assert restored.agent.state.messages == result.messages


@pytest.mark.asyncio
async def test_real_agent_lifecycle_flushes_at_turn_end() -> None:
    def stream_fn(_model: Model, _context: Context, _options: object = None) -> AssistantMessageEventStream:
        stream = AssistantMessageEventStream()
        asyncio.get_running_loop().call_soon(
            lambda: stream.push(DoneEvent(reason="stop", message=_assistant("real agent reply")))
        )
        return stream

    agent = Agent(AgentOptions(initial_state={"model": _model()}, stream_fn=stream_fn))
    session = Session(await InMemorySessionStorage.create(session_id="harness-real-agent"))
    harness = await AgentHarness.create(agent, session)

    result = await harness.prompt("hello")

    assert [message.role for message in result.messages] == ["user", "assistant"]
    assert await session.build_context() == result.messages


@pytest.mark.asyncio
async def test_tool_hooks_can_block_and_override() -> None:
    session = Session(await InMemorySessionStorage.create(session_id="harness-hooks"))
    agent = _FakeAgent()
    harness = await AgentHarness.create(agent, session)  # type: ignore[arg-type]
    harness.on("tool_call", lambda _context: {"block": True, "reason": "not allowed"})
    harness.on(
        "tool_result",
        lambda _context: AfterToolCallResult(
            content=[TextContent(text="redacted")], is_error=True, details={"hook": True}
        ),
    )

    before = await agent.before_tool_call(SimpleNamespace(), None)
    after = await agent.after_tool_call(SimpleNamespace(), None)

    assert before == BeforeToolCallResult(block=True, reason="not allowed")
    assert after is not None
    assert after.content == [TextContent(text="redacted")]
    assert after.is_error is True
    assert after.details == {"hook": True}


@pytest.mark.asyncio
async def test_compaction_creates_a_new_root_and_old_branch_remains_navigable() -> None:
    session = Session(await InMemorySessionStorage.create(session_id="harness-compact"))
    history = [
        UserMessage(content="old request " * 48, timestamp=1),
        _assistant("old details " * 80, 2),
        UserMessage(content="recent request " * 24, timestamp=3),
        _assistant("recent progress " * 24, 4),
    ]
    old_ids = [await session.append_message(message) for message in history]
    models = _FakeModels()
    harness = await AgentHarness.create(
        _FakeAgent(), session, models=models,  # type: ignore[arg-type]
        compaction_settings=CompactionSettings(reserve_tokens=100, keep_recent_tokens=100),
    )

    compacted = await harness.compact()

    assert compacted is not None
    assert isinstance(harness.agent.state.messages[0], CompactionSummaryMessage)
    assert await session.build_context() == compacted.messages
    assert models.calls == 1

    navigation = await harness.navigate_tree(old_ids[-1])
    assert navigation.messages == history
    assert harness.agent.state.messages == history


@pytest.mark.asyncio
async def test_prompt_automatically_compacts_when_threshold_is_crossed() -> None:
    session = Session(await InMemorySessionStorage.create(session_id="harness-auto-compact"))
    history = [
        UserMessage(content="old request " * 48, timestamp=1),
        _assistant("old details " * 80, 2),
        UserMessage(content="recent request " * 24, timestamp=3),
        _assistant("recent progress " * 24, 4),
    ]
    for message in history:
        await session.append_message(message)
    models = _FakeModels()
    harness = await AgentHarness.create(
        _FakeAgent(), session, models=models,  # type: ignore[arg-type]
        compaction_settings=CompactionSettings(reserve_tokens=100, keep_recent_tokens=100),
    )

    result = await harness.prompt("continue")

    assert isinstance(result.messages[0], CompactionSummaryMessage)
    assert models.calls == 1
    assert await session.build_context() == result.messages


@pytest.mark.asyncio
async def test_close_restores_wrapped_agent_hooks() -> None:
    async def original_before(_context: Any, _signal: Any) -> None:
        return None

    agent = _FakeAgent()
    agent.before_tool_call = original_before
    session = Session(await InMemorySessionStorage.create(session_id="harness-close"))
    harness = await AgentHarness.create(agent, session)  # type: ignore[arg-type]

    await harness.close()

    assert agent.before_tool_call is original_before
    with pytest.raises(RuntimeError, match="closed"):
        harness.on("context", lambda _context: None)
