"""Phase 4.2 tests for context compaction."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pi_agent.compaction import CompactionSettings, compact, find_cut_point, prepare_compaction, should_compact
from pi_agent.messages import CompactionSummaryMessage
from pi_ai.types import AssistantMessage, Context, CostInfo, Model, ModelCost, TextContent, Usage, UserMessage


def _model() -> Model:
    return Model(
        id="summary-model", name="Summary model", api="openai-completions", provider="test",
        base_url="https://example.test/v1", context_window=600, max_tokens=256,
        cost=ModelCost(0, 0, 0, 0),
    )


@dataclass
class _FakeModels:
    calls: list[Context]

    async def complete_simple(self, model: Model, context: Context, options: object = None) -> AssistantMessage:
        del options
        self.calls.append(context)
        return AssistantMessage(
            content=[TextContent(text="## Goal\nKeep the recent work.\n\n## Progress\n### Done\n- [x] Earlier work")],
            api=model.api, provider=model.provider, model=model.id,
            usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)), stop_reason="stop", timestamp=1,
        )


def _long_history() -> list[UserMessage | AssistantMessage]:
    return [
        UserMessage(content="old request " * 48, timestamp=1),
        AssistantMessage(content=[TextContent(text="old implementation details " * 48)], api="openai-completions", provider="test", model="summary-model", usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)), stop_reason="stop", timestamp=2),
        UserMessage(content="recent request " * 24, timestamp=3),
        AssistantMessage(content=[TextContent(text="recent progress " * 24)], api="openai-completions", provider="test", model="summary-model", usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)), stop_reason="stop", timestamp=4),
    ]


def test_should_compact_and_prepare_keep_a_recent_turn() -> None:
    messages = _long_history()
    settings = CompactionSettings(reserve_tokens=100, keep_recent_tokens=100)

    assert should_compact(messages, _model(), settings)
    prepared = prepare_compaction(messages, _model(), settings)

    assert prepared is not None
    assert prepared.messages_to_summarize == messages[:2]
    assert prepared.retained_tail == messages[2:]
    assert not prepared.is_split_turn


def test_find_cut_point_marks_a_split_turn() -> None:
    cut = find_cut_point(_long_history(), keep_recent_tokens=50)

    assert cut.first_kept_index == 3
    assert cut.turn_start_index == 2
    assert cut.is_split_turn


@pytest.mark.asyncio
async def test_compact_replaces_early_history_with_a_summary() -> None:
    messages = _long_history()
    fake_models = _FakeModels(calls=[])
    result = await compact(messages, fake_models, _model(), CompactionSettings(reserve_tokens=100, keep_recent_tokens=100))

    assert result is not None
    assert isinstance(result.messages[0], CompactionSummaryMessage)
    assert result.messages[1:] == messages[2:]
    assert result.tokens_after < result.tokens_before
    assert len(fake_models.calls) == 1
    assert "<conversation>" in fake_models.calls[0].messages[0].content
