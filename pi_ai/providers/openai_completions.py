"""OpenAI-compatible Chat Completions streaming used by DeepSeek."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from openai import AsyncOpenAI

from pi_ai.event_stream import AssistantMessageEventStream
from pi_ai.models import calculate_cost, clamp_thinking_level
from pi_ai.transform_messages import transform_messages
from pi_ai.types import (
    AssistantMessage,
    Context,
    CostInfo,
    DoneEvent,
    ErrorEvent,
    ImageContent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StopReason,
    StreamOptions,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    Tool,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultMessage,
    Usage,
)


@dataclass
class OpenAICompletionsOptions(StreamOptions):
    """Extra options understood by OpenAI-compatible chat completions."""

    tool_choice: str | dict[str, object] | None = None
    reasoning_effort: str | None = None


@dataclass
class _StreamingToolCall:
    block: ToolCall
    arguments_json: str


_client_factory = AsyncOpenAI


class OpenAICompletionsStreams:
    """ProviderStreams implementation for OpenAI-compatible endpoints."""

    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        resolved = (
            options
            if isinstance(options, OpenAICompletionsOptions)
            else _to_completions_options(options)
        )
        return stream(model, context, resolved)

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        return stream_simple(model, context, options)


def stream_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    """Map unified options to chat-completions options and start streaming."""

    base = _to_completions_options(options)
    base.max_tokens = options.max_tokens if options and options.max_tokens else model.max_tokens
    if options and options.reasoning:
        level = clamp_thinking_level(options.reasoning, model)
        base.reasoning_effort = None if level == "off" else level
    return stream(model, context, base)


def stream(
    model: Model,
    context: Context,
    options: OpenAICompletionsOptions | None = None,
) -> AssistantMessageEventStream:
    """Stream an OpenAI-compatible response into pi assistant events."""

    event_stream = AssistantMessageEventStream()
    output = _empty_assistant_message(model)

    async def run() -> None:
        client: AsyncOpenAI | None = None
        try:
            if _is_aborted(options):
                raise RuntimeError("Request was aborted")
            if not options or not options.api_key:
                raise ValueError(f"No API key for provider: {model.provider}")

            headers = {
                key: value
                for key, value in {**(model.headers or {}), **(options.headers or {})}.items()
                if value is not None
            }
            client = _client_factory(
                api_key=options.api_key,
                base_url=model.base_url,
                default_headers=headers,
                timeout=(options.timeout_ms / 1000 if options.timeout_ms else None),
                max_retries=options.max_retries or 0,
            )
            params = build_params(model, context, options)
            chunks = await client.chat.completions.create(**params)
            event_stream.push(StartEvent(partial=output))
            await _consume_chunks(
                cast(AsyncIterator[object], chunks),
                model,
                output,
                event_stream,
                options,
            )
        except Exception as error:
            output.stop_reason = "aborted" if _is_aborted(options) else "error"
            output.error_message = str(error)
            event_stream.push(
                ErrorEvent(reason=output.stop_reason, error=output)
            )
        finally:
            if client is not None:
                await client.close()

    asyncio.create_task(run())
    return event_stream


def build_params(
    model: Model,
    context: Context,
    options: OpenAICompletionsOptions | None = None,
) -> dict[str, object]:
    """Build a DeepSeek/OpenAI-compatible streaming request payload."""

    options = options or OpenAICompletionsOptions()
    params: dict[str, object] = {
        "model": model.id,
        "messages": convert_messages(model, context),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if options.max_tokens is not None:
        params["max_completion_tokens"] = options.max_tokens
    if options.temperature is not None and not (
        model.compat or {}
    ).get("supportsTemperature") is False:
        params["temperature"] = options.temperature
    if context.tools:
        params["tools"] = convert_tools(context.tools)
    elif _has_tool_history(context):
        params["tools"] = []
    if options.tool_choice is not None:
        params["tool_choice"] = options.tool_choice

    if model.reasoning and (model.compat or {}).get("thinkingFormat") == "deepseek":
        extra_body: dict[str, object] = {
            "thinking": {
                "type": "enabled" if options.reasoning_effort else "disabled"
            }
        }
        if options.reasoning_effort:
            extra_body["reasoning_effort"] = (
                model.thinking_level_map or {}
            ).get(options.reasoning_effort) or options.reasoning_effort
        params["extra_body"] = extra_body
    return params


def convert_messages(model: Model, context: Context) -> list[dict[str, object]]:
    """Convert pi messages to OpenAI Chat Completions message dictionaries."""

    result: list[dict[str, object]] = []
    if context.system_prompt:
        result.append({"role": "system", "content": context.system_prompt})

    requires_reasoning = bool(
        (model.compat or {}).get("requiresReasoningContentOnAssistantMessages")
    )
    for message in transform_messages(context.messages, model):
        if message.role == "user":
            if isinstance(message.content, str):
                result.append({"role": "user", "content": message.content})
                continue
            parts: list[dict[str, object]] = []
            for block in message.content:
                if isinstance(block, TextContent):
                    parts.append({"type": "text", "text": block.text})
                elif isinstance(block, ImageContent) and "image" in model.input:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{block.mime_type};base64,{block.data}"
                            },
                        }
                    )
                else:
                    parts.append(
                        {
                            "type": "text",
                            "text": "(image omitted: model does not support images)",
                        }
                    )
            if parts:
                result.append({"role": "user", "content": parts})
            continue

        if message.role == "assistant":
            text = "".join(
                block.text
                for block in message.content
                if isinstance(block, TextContent)
            )
            thinking_blocks = [
                block
                for block in message.content
                if isinstance(block, ThinkingContent) and block.thinking
            ]
            tool_calls = [
                block for block in message.content if isinstance(block, ToolCall)
            ]
            converted: dict[str, object] = {
                "role": "assistant",
                "content": text or None,
            }
            if thinking_blocks:
                converted["reasoning_content"] = "\n".join(
                    block.thinking for block in thinking_blocks
                )
            elif requires_reasoning:
                converted["reasoning_content"] = ""
            if tool_calls:
                converted["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments, ensure_ascii=False
                            ),
                        },
                    }
                    for call in tool_calls
                ]
            if text or tool_calls:
                result.append(converted)
            continue

        if isinstance(message, ToolResultMessage):
            text = "\n".join(
                block.text
                if isinstance(block, TextContent)
                else "(tool image omitted: model does not support images)"
                for block in message.content
            )
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": text or "(no tool output)",
                }
            )
    return result


def convert_tools(tools: Sequence[Tool]) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": False,
            },
        }
        for tool in tools
    ]


def _has_tool_history(context: Context) -> bool:
    for message in context.messages:
        if isinstance(message, ToolResultMessage):
            return True
        if isinstance(message, AssistantMessage) and any(
            isinstance(block, ToolCall) for block in message.content
        ):
            return True
    return False


async def _consume_chunks(
    chunks: AsyncIterator[object],
    model: Model,
    output: AssistantMessage,
    event_stream: AssistantMessageEventStream,
    options: OpenAICompletionsOptions,
) -> None:
    text_block: TextContent | None = None
    thinking_block: ThinkingContent | None = None
    tool_calls: dict[int, _StreamingToolCall] = {}
    has_finish_reason = False

    async for chunk in chunks:
        if _is_aborted(options):
            raise RuntimeError("Request was aborted")

        response_id = _string_field(chunk, "id")
        if response_id and output.response_id is None:
            output.response_id = response_id
        response_model = _string_field(chunk, "model")
        if response_model and response_model != model.id and output.response_model is None:
            output.response_model = response_model

        raw_usage = _field(chunk, "usage")
        if raw_usage is not None:
            output.usage = _parse_usage(raw_usage, model)

        choices = _sequence_field(chunk, "choices")
        if not choices:
            continue
        choice = choices[0]
        finish_reason = _string_field(choice, "finish_reason")
        if finish_reason:
            output.stop_reason, output.error_message = _map_stop_reason(finish_reason)
            has_finish_reason = True

        delta = _field(choice, "delta")
        if delta is None:
            continue

        text_delta = _string_field(delta, "content")
        if text_delta:
            if text_block is None:
                text_block = TextContent(text="")
                output.content.append(text_block)
                event_stream.push(
                    TextStartEvent(
                        content_index=output.content.index(text_block),
                        partial=output,
                    )
                )
            text_block.text += text_delta
            event_stream.push(
                TextDeltaEvent(
                    content_index=output.content.index(text_block),
                    delta=text_delta,
                    partial=output,
                )
            )

        reasoning_delta = next(
            (
                value
                for name in ("reasoning_content", "reasoning", "reasoning_text")
                if (value := _string_field(delta, name))
            ),
            None,
        )
        if reasoning_delta:
            if thinking_block is None:
                thinking_block = ThinkingContent(
                    thinking="",
                    thinking_signature="reasoning_content",
                )
                output.content.append(thinking_block)
                event_stream.push(
                    ThinkingStartEvent(
                        content_index=output.content.index(thinking_block),
                        partial=output,
                    )
                )
            thinking_block.thinking += reasoning_delta
            event_stream.push(
                ThinkingDeltaEvent(
                    content_index=output.content.index(thinking_block),
                    delta=reasoning_delta,
                    partial=output,
                )
            )

        for raw_call in _sequence_field(delta, "tool_calls"):
            index = _int_field(raw_call, "index") or 0
            current = tool_calls.get(index)
            function = _field(raw_call, "function")
            if current is None:
                block = ToolCall(
                    id=_string_field(raw_call, "id") or "",
                    name=_string_field(function, "name") if function else "",
                    arguments={},
                )
                current = _StreamingToolCall(block=block, arguments_json="")
                tool_calls[index] = current
                output.content.append(block)
                event_stream.push(
                    ToolCallStartEvent(
                        content_index=output.content.index(block),
                        partial=output,
                    )
                )
            call_id = _string_field(raw_call, "id")
            name = _string_field(function, "name") if function else None
            arguments = _string_field(function, "arguments") if function else None
            if call_id and not current.block.id:
                current.block.id = call_id
            if name and not current.block.name:
                current.block.name = name
            if arguments:
                current.arguments_json += arguments
                current.block.arguments = _parse_partial_json(current.arguments_json)
            event_stream.push(
                ToolCallDeltaEvent(
                    content_index=output.content.index(current.block),
                    delta=arguments or "",
                    partial=output,
                )
            )

    tools_by_block = {id(current.block): current for current in tool_calls.values()}
    for index, block in enumerate(output.content):
        if isinstance(block, TextContent):
            event_stream.push(
                TextEndEvent(
                    content_index=index,
                    content=block.text,
                    partial=output,
                )
            )
        elif isinstance(block, ThinkingContent):
            event_stream.push(
                ThinkingEndEvent(
                    content_index=index,
                    content=block.thinking,
                    partial=output,
                )
            )
        elif isinstance(block, ToolCall):
            current = tools_by_block[id(block)]
            block.arguments = _parse_partial_json(current.arguments_json)
            event_stream.push(
                ToolCallEndEvent(
                    content_index=index,
                    tool_call=block,
                    partial=output,
                )
            )

    if not has_finish_reason:
        raise RuntimeError("Stream ended without finish_reason")
    if output.stop_reason == "error":
        raise RuntimeError(output.error_message or "Provider returned an error")
    event_stream.push(
        DoneEvent(
            reason=cast(str, output.stop_reason),
            message=output,
        )
    )


def _to_completions_options(
    options: StreamOptions | SimpleStreamOptions | None,
) -> OpenAICompletionsOptions:
    if options is None:
        return OpenAICompletionsOptions()
    return OpenAICompletionsOptions(
        temperature=options.temperature,
        max_tokens=options.max_tokens,
        signal=options.signal,
        api_key=options.api_key,
        transport=options.transport,
        cache_retention=options.cache_retention,
        session_id=options.session_id,
        headers=options.headers,
        timeout_ms=options.timeout_ms,
        websocket_connect_timeout_ms=options.websocket_connect_timeout_ms,
        max_retries=options.max_retries,
        max_retry_delay_ms=options.max_retry_delay_ms,
        metadata=options.metadata,
        env=options.env,
        extra=dict(options.extra),
    )


def _empty_assistant_message(model: Model) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)),
        stop_reason="stop",
        timestamp=int(time.time() * 1000),
    )


def _parse_usage(raw: object, model: Model) -> Usage:
    prompt_tokens = _int_field(raw, "prompt_tokens") or 0
    completion_tokens = _int_field(raw, "completion_tokens") or 0
    prompt_details = _field(raw, "prompt_tokens_details")
    completion_details = _field(raw, "completion_tokens_details")
    cache_read = (
        _int_field(prompt_details, "cached_tokens")
        if prompt_details is not None
        else None
    ) or (_int_field(raw, "prompt_cache_hit_tokens") or 0)
    cache_write = (
        _int_field(prompt_details, "cache_write_tokens")
        if prompt_details is not None
        else None
    ) or 0
    usage = Usage(
        input=max(0, prompt_tokens - cache_read - cache_write),
        output=completion_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        total_tokens=prompt_tokens + completion_tokens,
        reasoning=(
            _int_field(completion_details, "reasoning_tokens")
            if completion_details is not None
            else None
        ),
        cost=CostInfo(0, 0, 0, 0, 0),
    )
    calculate_cost(usage, model)
    return usage


def _map_stop_reason(reason: str) -> tuple[StopReason, str | None]:
    if reason in ("stop", "end"):
        return "stop", None
    if reason == "length":
        return "length", None
    if reason in ("function_call", "tool_calls"):
        return "toolUse", None
    return "error", f"Provider finish_reason: {reason}"


def _parse_partial_json(value: str) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_aborted(options: StreamOptions | None) -> bool:
    signal = options.signal if options else None
    return bool(signal and getattr(signal, "aborted", False))


def _field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _string_field(value: object, name: str) -> str | None:
    field = _field(value, name)
    return field if isinstance(field, str) and field else None


def _int_field(value: object, name: str) -> int | None:
    field = _field(value, name)
    return field if isinstance(field, int) else None


def _sequence_field(value: object, name: str) -> Sequence[object]:
    field = _field(value, name)
    if isinstance(field, Sequence) and not isinstance(field, (str, bytes)):
        return field
    return ()
