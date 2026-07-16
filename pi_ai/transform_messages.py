"""在将 pi 的消息发送给 LLM 提供商之前，对消息进行规范化处理"""

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
    """遍历所有消息，按类型分发到对应的转换函数
    -跳过 stop_reason 为 "error" 或 "aborted" 的助手消息——这些是失败/中断的回复，不应回放
    -最后调用 _insert_missing_tool_results 做兜底修补
    """

    transformed: list[Message] = []
    #按照消息类型分别处理
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

#如果模型不支持图片输入（"image" not in model.input），将消息中的 ImageContent替换为文本占位符 (image omitted: ...)
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
                    content.append(block)  #仅 same_model 时保留；跨模型时丢弃（无法解读）
            elif same_model and block.thinking_signature:
                content.append(block)  #仅 same_model 时保留签名；跨模型时丢弃
            elif block.thinking.strip():
                content.append(
                    block if same_model else TextContent(text=block.thinking)
                ) #same_model → 保留原 ThinkingContent；跨模型 → 降级为TextContent（纯文本形式传递思考内容）
        elif isinstance(block, ToolCall):
            content.append(
                block
                if same_model or block.thought_signature is None #same_model → 保留签名；跨模型 → 剔除签名（设为 None）
                else replace(block, thought_signature=None)
            )
        else:
            content.append(block)
    return replace(message, content=content)


#替换图片
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

# 兜底选项：确保每个 ToolCall 都有对应的 ToolResultMessage
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
