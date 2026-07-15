"""Normalize pi messages before sending them to a provider."""

from __future__ import annotations

import time
from dataclasses import replace

from pi_ai.types import (
    AssistantMessage,
    ImageContent,
    Message,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

_USER_IMAGE_PLACEHOLDER = "(image omitted: model does not support images)"
_TOOL_IMAGE_PLACEHOLDER = "(tool image omitted: model does not support images)"


def transform_messages(messages: list[Message], model: Model) -> list[Message]:
    """Prepare history for replay across models and provider constraints."""

    transformed: list[Message] = []
    for message in messages:
        if isinstance(message, UserMessage):
            transformed.append(_transform_user(message, model))
        elif isinstance(message, ToolResultMessage):
            transformed.append(_transform_tool_result(message, model))
        elif isinstance(message, AssistantMessage):
            if message.stop_reason in ("error", "aborted"):
                continue
            transformed.append(_transform_assistant(message, model))

    return _insert_missing_tool_results(transformed)


def _transform_user(message: UserMessage, model: Model) -> UserMessage:
    if isinstance(message.content, str) or "image" in model.input:
        return message
    return replace(
        message,
        content=_replace_images(message.content, _USER_IMAGE_PLACEHOLDER),
    )


def _transform_tool_result(
    message: ToolResultMessage,
    model: Model,
) -> ToolResultMessage:
    if "image" in model.input:
        return message
    return replace(
        message,
        content=_replace_images(message.content, _TOOL_IMAGE_PLACEHOLDER),
    )


def _transform_assistant(
    message: AssistantMessage,
    model: Model,
) -> AssistantMessage:
    same_model = (
        message.provider == model.provider
        and message.api == model.api
        and message.model == model.id
    )
    content: list[TextContent | ThinkingContent | ToolCall] = []
    for block in message.content:
        if isinstance(block, ThinkingContent):
            if block.redacted:
                if same_model:
                    content.append(block)
            elif same_model and block.thinking_signature:
                content.append(block)
            elif block.thinking.strip():
                content.append(
                    block if same_model else TextContent(text=block.thinking)
                )
        elif isinstance(block, ToolCall):
            content.append(
                block
                if same_model or block.thought_signature is None
                else replace(block, thought_signature=None)
            )
        else:
            content.append(block)
    return replace(message, content=content)


def _replace_images(
    content: list[TextContent | ImageContent],
    placeholder: str,
) -> list[TextContent]:
    result: list[TextContent] = []
    previous_was_placeholder = False
    for block in content:
        if isinstance(block, ImageContent):
            if not previous_was_placeholder:
                result.append(TextContent(text=placeholder))
            previous_was_placeholder = True
        else:
            result.append(block)
            previous_was_placeholder = block.text == placeholder
    return result


def _insert_missing_tool_results(messages: list[Message]) -> list[Message]:
    result: list[Message] = []
    pending: list[ToolCall] = []
    result_ids: set[str] = set()

    def flush_pending() -> None:
        nonlocal pending, result_ids
        for call in pending:
            if call.id not in result_ids:
                result.append(
                    ToolResultMessage(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        content=[TextContent(text="No result provided")],
                        is_error=True,
                        timestamp=int(time.time() * 1000),
                    )
                )
        pending = []
        result_ids = set()

    for message in messages:
        if isinstance(message, AssistantMessage):
            flush_pending()
            pending = [
                block
                for block in message.content
                if isinstance(block, ToolCall)
            ]
            result.append(message)
        elif isinstance(message, ToolResultMessage):
            result_ids.add(message.tool_call_id)
            result.append(message)
        elif isinstance(message, UserMessage):
            flush_pending()
            result.append(message)

    flush_pending()
    return result
