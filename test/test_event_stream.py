
from __future__ import annotations

import asyncio

import pytest

from pi_ai.event_stream import AbortSignal, AssistantMessageEventStream, EventStream
from pi_ai.types import (
    AssistantMessage,
    CostInfo,
    DoneEvent,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    Usage,
)


def _make_partial(content: list) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="anthropic-messages",
        provider="anthropic",
        model="claude-test",
        usage=Usage(
            input=10,
            output=0,
            cache_read=0,
            cache_write=0,
            total_tokens=10,
            cost=CostInfo(0, 0, 0, 0, 0),
        ),
        stop_reason="stop",
        timestamp=0,
    )


def _fake_provider_stream() -> AssistantMessageEventStream:
    """Push a start -> text_start -> 2x text_delta -> text_end -> done sequence."""
    stream = AssistantMessageEventStream()

    partial_empty = _make_partial([])
    stream.push(StartEvent(partial=partial_empty))

    partial_text = _make_partial([TextContent(text="")])
    stream.push(TextStartEvent(content_index=0, partial=partial_text))

    partial_h = _make_partial([TextContent(text="H")])
    stream.push(TextDeltaEvent(content_index=0, delta="H", partial=partial_h))

    partial_hi = _make_partial([TextContent(text="Hi")])
    stream.push(TextDeltaEvent(content_index=0, delta="i", partial=partial_hi))

    partial_final = _make_partial([TextContent(text="Hi")])
    stream.push(TextEndEvent(content_index=0, content="Hi", partial=partial_final))

    final_message = AssistantMessage(
        content=[TextContent(text="Hi")],
        api="anthropic-messages",
        provider="anthropic",
        model="claude-test",
        usage=Usage(
            input=10,
            output=2,
            cache_read=0,
            cache_write=0,
            total_tokens=12,
            cost=CostInfo(0.0003, 0.0003, 0, 0, 0.0006),
        ),
        stop_reason="stop",
        timestamp=1,
    )
    stream.push(DoneEvent(reason="stop", message=final_message))
    return stream


@pytest.mark.asyncio
async def test_async_iteration_yields_events_in_order() -> None:
    stream = _fake_provider_stream()
    types = [ev.type async for ev in stream]
    assert types == [
        "start",
        "text_start",
        "text_delta",
        "text_delta",
        "text_end",
        "done",
    ]


@pytest.mark.asyncio
async def test_result_returns_final_assistant_message() -> None:
    stream = _fake_provider_stream()
    # Drain events first (as a real consumer would).
    _ = [ev async for ev in stream]
    message = await stream.result()
    assert isinstance(message, AssistantMessage)
    assert message.stop_reason == "stop"
    assert len(message.content) == 1
    assert isinstance(message.content[0], TextContent)
    assert message.content[0].text == "Hi"
    assert message.usage.output == 2
    assert message.usage.cost.total == pytest.approx(0.0006)


@pytest.mark.asyncio
async def test_result_resolves_even_without_draining() -> None:
    """result() must resolve once a terminal event is pushed, independent of iteration."""
    stream = _fake_provider_stream()
    message = await stream.result()
    assert message.content[0].text == "Hi"


@pytest.mark.asyncio
async def test_push_after_done_is_ignored() -> None:
    stream = AssistantMessageEventStream()
    final = _make_partial([TextContent(text="x")])
    final = AssistantMessage(
        content=[TextContent(text="x")],
        api="anthropic-messages",
        provider="anthropic",
        model="claude-test",
        usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)),
        stop_reason="stop",
        timestamp=0,
    )
    stream.push(DoneEvent(reason="stop", message=final))
    # Late push must not raise and must not enqueue.
    stream.push(StartEvent(partial=final))
    events = [ev async for ev in stream]
    assert len(events) == 1
    assert events[0].type == "done"


@pytest.mark.asyncio
async def test_end_terminates_iteration_with_result() -> None:
    stream = AssistantMessageEventStream()
    final = AssistantMessage(
        content=[TextContent(text="aborted")],
        api="anthropic-messages",
        provider="anthropic",
        model="claude-test",
        usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)),
        stop_reason="aborted",
        timestamp=0,
        error_message="user aborted",
    )
    stream.push(StartEvent(partial=final))
    stream.end(result=final)
    events = [ev async for ev in stream]
    assert [e.type for e in events] == ["start"]
    assert await stream.result() is final


@pytest.mark.asyncio
async def test_generic_event_stream_result() -> None:
    """The generic EventStream should resolve result from a custom complete event."""
    stream: EventStream[int, int] = EventStream(
        is_complete=lambda ev: ev == 42,
        extract_result=lambda ev: ev * 2,
    )
    stream.push(1)
    stream.push(2)
    stream.push(42)
    events = [ev async for ev in stream]
    assert events == [1, 2, 42]
    assert await stream.result() == 84


def test_abort_signal_set_and_check() -> None:
    sig = AbortSignal()
    assert not sig.aborted
    sig.abort()
    assert sig.aborted
    assert sig.is_set()


@pytest.mark.asyncio
async def test_abort_signal_wait() -> None:
    sig = AbortSignal()

    async def trigger() -> None:
        await asyncio.sleep(0)
        sig.abort()

    asyncio.create_task(trigger())
    await sig.wait()
    assert sig.aborted
