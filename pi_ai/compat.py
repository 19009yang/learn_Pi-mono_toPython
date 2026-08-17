"""Compatibility defaults for OpenAI Chat Completions providers.

Provider implementations read this module instead of branching on a provider
name.  A model may override any default through ``Model.compat``.
"""

from __future__ import annotations

from dataclasses import dataclass

from pi_ai.types import Model


@dataclass(frozen=True)
class OpenAICompletionsCompat:
    """Resolved OpenAI-compatible request and history settings."""

    max_tokens_field: str = "max_completion_tokens"
    supports_temperature: bool = True
    requires_reasoning_content_on_assistant_messages: bool = False
    requires_assistant_after_tool_result: bool = False
    requires_tool_result_name: bool = False
    thinking_format: str = "openai"


def resolve_openai_completions_compat(model: Model) -> OpenAICompletionsCompat:
    """Return defaults for an OpenAI-compatible model plus explicit overrides."""

    base = OpenAICompletionsCompat()
    values = model.compat or {}
    return OpenAICompletionsCompat(
        max_tokens_field=str(values.get("maxTokensField", base.max_tokens_field)),
        supports_temperature=values.get("supportsTemperature", base.supports_temperature)
        is not False,
        requires_reasoning_content_on_assistant_messages=bool(
            values.get(
                "requiresReasoningContentOnAssistantMessages",
                base.requires_reasoning_content_on_assistant_messages,
            )
        ),
        requires_assistant_after_tool_result=bool(
            values.get("requiresAssistantAfterToolResult", base.requires_assistant_after_tool_result)
        ),
        requires_tool_result_name=bool(
            values.get("requiresToolResultName", base.requires_tool_result_name)
        ),
        thinking_format=str(values.get("thinkingFormat", base.thinking_format)),
    )
