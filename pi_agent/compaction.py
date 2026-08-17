"""Conversation-context compaction for long-running agent sessions."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Protocol, Sequence

from pi_agent.messages import AgentMessage, create_compaction_summary_message, convert_to_llm
from pi_ai.types import AssistantMessage, Context, Model, SimpleStreamOptions, TextContent, ThinkingContent, ToolCall, ToolResultMessage, UserMessage


SUMMARIZATION_SYSTEM_PROMPT = """You summarize conversation context for another LLM.
Do not answer the conversation. Return only the requested structured summary."""

SUMMARIZATION_PROMPT = """Create a concise structured checkpoint summary of this conversation.

Use exactly these sections:
## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Next Steps
## Critical Context

Preserve exact file paths, function names, and errors needed to continue."""

UPDATE_SUMMARIZATION_PROMPT = """Update the previous structured checkpoint summary using the new conversation.
Preserve still-relevant facts, move completed work to Done, and keep exact file paths,
function names, and errors. Use the same required section headings."""


@dataclass(frozen=True)
class CompactionSettings:
    enabled: bool = True
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000


DEFAULT_COMPACTION_SETTINGS = CompactionSettings()


@dataclass(frozen=True)
class CutPoint:
    first_kept_index: int
    turn_start_index: int | None
    is_split_turn: bool


@dataclass(frozen=True)
class CompactionPreparation:
    messages_to_summarize: list[AgentMessage]
    turn_prefix_messages: list[AgentMessage]
    retained_tail: list[AgentMessage]
    tokens_before: int
    previous_summary: str | None
    is_split_turn: bool
    settings: CompactionSettings


@dataclass(frozen=True)
class CompactionResult:
    messages: list[AgentMessage]
    summary: str
    tokens_before: int
    tokens_after: int
    retained_tail: list[AgentMessage]


class CompletionModels(Protocol):
    async def complete_simple(
        self, model: Model, context: Context, options: SimpleStreamOptions | None = None
    ) -> AssistantMessage: ...


def estimate_tokens(message: AgentMessage) -> int:
    """Conservatively estimate tokens when no provider usage is available."""

    characters = 0
    if isinstance(message, UserMessage):
        characters = _content_characters(message.content)
    elif isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextContent):
                characters += len(block.text)
            elif isinstance(block, ThinkingContent):
                characters += len(block.thinking)
            elif isinstance(block, ToolCall):
                characters += len(block.name) + len(json.dumps(block.arguments, ensure_ascii=False))
    elif isinstance(message, ToolResultMessage):
        characters = _content_characters(message.content)
    elif hasattr(message, "summary"):
        characters = len(message.summary)  # type: ignore[attr-defined]
    elif hasattr(message, "command"):
        characters = len(message.command) + len(message.output)  # type: ignore[attr-defined]
    elif hasattr(message, "content"):
        content = message.content  # type: ignore[attr-defined]
        characters = len(content) if isinstance(content, str) else _content_characters(content)
    return math.ceil(characters / 4)


def estimate_context_tokens(messages: Sequence[AgentMessage]) -> int:
    """Use the latest provider usage as a baseline plus later local messages."""

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AssistantMessage) and message.stop_reason not in ("error", "aborted"):
            usage = message.usage
            used = usage.total_tokens or (usage.input + usage.output + usage.cache_read + usage.cache_write)
            if used:
                return used + sum(estimate_tokens(item) for item in messages[index + 1 :])
    return sum(estimate_tokens(message) for message in messages)


def should_compact(
    messages_or_tokens: Sequence[AgentMessage] | int,
    model_or_context_window: Model | int,
    settings: CompactionSettings = DEFAULT_COMPACTION_SETTINGS,
) -> bool:
    """Return whether the context has crossed its usable-window threshold."""

    if not settings.enabled:
        return False
    tokens = estimate_context_tokens(messages_or_tokens) if not isinstance(messages_or_tokens, int) else messages_or_tokens
    context_window = model_or_context_window.context_window if isinstance(model_or_context_window, Model) else model_or_context_window
    return tokens > max(0, context_window - settings.reserve_tokens)


def find_turn_start_index(messages: Sequence[AgentMessage], index: int, start_index: int = 0) -> int | None:
    """Find the user-visible message that began the turn containing ``index``."""

    for current in range(index, start_index - 1, -1):
        message = messages[current]
        if isinstance(message, UserMessage) or getattr(message, "role", None) == "bashExecution":
            return current
    return None


def find_cut_point(
    messages: Sequence[AgentMessage],
    keep_recent_tokens: int,
    start_index: int = 0,
) -> CutPoint:
    """Keep a recent suffix without cutting between a tool call and its result."""

    if start_index >= len(messages):
        return CutPoint(start_index, None, False)
    accumulated = 0
    candidate = start_index
    for index in range(len(messages) - 1, start_index - 1, -1):
        accumulated += estimate_tokens(messages[index])
        if accumulated >= keep_recent_tokens:
            candidate = index
            break
    while candidate < len(messages) and isinstance(messages[candidate], ToolResultMessage):
        candidate += 1
    if candidate >= len(messages):
        candidate = len(messages) - 1
    starts_turn = isinstance(messages[candidate], UserMessage) or getattr(messages[candidate], "role", None) == "bashExecution"
    turn_start = None if starts_turn else find_turn_start_index(messages, candidate, start_index)
    return CutPoint(candidate, turn_start, turn_start is not None)


def prepare_compaction(
    messages: Sequence[AgentMessage],
    model: Model,
    settings: CompactionSettings = DEFAULT_COMPACTION_SETTINGS,
) -> CompactionPreparation | None:
    """Split history into summary input and a retained recent tail."""

    current = list(messages)
    if not current or not should_compact(current, model, settings):
        return None
    previous_summary = None
    start_index = 0
    if getattr(current[0], "role", None) == "compactionSummary":
        previous_summary = current[0].summary  # type: ignore[attr-defined]
        start_index = 1
    cut = find_cut_point(current, settings.keep_recent_tokens, start_index)
    history_end = cut.turn_start_index if cut.is_split_turn else cut.first_kept_index
    return CompactionPreparation(
        messages_to_summarize=current[start_index:history_end],
        turn_prefix_messages=current[history_end:cut.first_kept_index] if cut.is_split_turn else [],
        retained_tail=current[cut.first_kept_index:],
        tokens_before=estimate_context_tokens(current),
        previous_summary=previous_summary,
        is_split_turn=cut.is_split_turn,
        settings=settings,
    )


async def generate_summary(
    messages: Sequence[AgentMessage],
    models: CompletionModels,
    model: Model,
    reserve_tokens: int,
    *,
    previous_summary: str | None = None,
) -> str:
    """Ask the configured model for an initial or incremental summary."""

    conversation = _serialize_conversation(convert_to_llm(list(messages)))
    prompt = f"<conversation>\n{conversation}\n</conversation>\n\n"
    if previous_summary:
        prompt += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n{UPDATE_SUMMARIZATION_PROMPT}"
    else:
        prompt += SUMMARIZATION_PROMPT
    response = await models.complete_simple(
        model,
        Context(messages=[UserMessage(content=prompt, timestamp=int(time.time() * 1000))], system_prompt=SUMMARIZATION_SYSTEM_PROMPT),
        SimpleStreamOptions(max_tokens=min(max(1, int(reserve_tokens * 0.8)), model.max_tokens)),
    )
    if response.stop_reason in ("error", "aborted"):
        raise RuntimeError(response.error_message or "Compaction summarization failed")
    return "".join(block.text for block in response.content if isinstance(block, TextContent)).strip()


async def compact(
    messages: Sequence[AgentMessage],
    models: CompletionModels,
    model: Model,
    settings: CompactionSettings = DEFAULT_COMPACTION_SETTINGS,
) -> CompactionResult | None:
    """Replace compacted history with one ``CompactionSummaryMessage`` and a tail."""

    preparation = prepare_compaction(messages, model, settings)
    if preparation is None:
        return None
    summary_input = preparation.messages_to_summarize + preparation.turn_prefix_messages
    if not summary_input and preparation.previous_summary is None:
        return None
    summary = await generate_summary(
        summary_input,
        models,
        model,
        settings.reserve_tokens,
        previous_summary=preparation.previous_summary,
    )
    summary_message = create_compaction_summary_message(summary, preparation.tokens_before)
    compacted = [summary_message, *preparation.retained_tail]
    return CompactionResult(
        messages=compacted,
        summary=summary,
        tokens_before=preparation.tokens_before,
        tokens_after=estimate_context_tokens(compacted),
        retained_tail=preparation.retained_tail,
    )


def _content_characters(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    return sum(len(block.text) if isinstance(block, TextContent) else 4_800 for block in content)  # type: ignore[union-attr]


def _serialize_conversation(messages: Sequence[object]) -> str:
    lines: list[str] = []
    for message in messages:
        role = getattr(message, "role", "message")
        if isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, str) else "\n".join(block.text for block in message.content if isinstance(block, TextContent))
        elif isinstance(message, AssistantMessage):
            content = "\n".join(block.text if isinstance(block, TextContent) else block.thinking if isinstance(block, ThinkingContent) else f"tool call: {block.name}({json.dumps(block.arguments, ensure_ascii=False)})" for block in message.content)
        elif isinstance(message, ToolResultMessage):
            content = "\n".join(block.text for block in message.content if isinstance(block, TextContent))
        else:
            content = str(message)
        lines.append(f"[{role}]\n{content}")
    return "\n\n".join(lines)
