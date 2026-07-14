"""Core type definitions for pi_ai.

Python port of packages/ai/src/types.ts. These are pi's own internal
representation; provider modules translate between these and each SDK's
native types. Snake_case is used throughout for Pythonic correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeAlias, TypeVar

# ========== APIs and providers ==========

KnownApi: TypeAlias = Literal[
    "openai-completions",
    "mistral-conversations",
    "openai-responses",
    "azure-openai-responses",
    "openai-codex-responses",
    "anthropic-messages",
    "bedrock-converse-stream",
    "google-generative-ai",
    "google-vertex",
]
Api: TypeAlias = str  # KnownApi | custom string
ProviderId: TypeAlias = str

# ========== Thinking ==========

ThinkingLevel: TypeAlias = Literal["minimal", "low", "medium", "high", "xhigh"]
ModelThinkingLevel: TypeAlias = Literal["off"] | ThinkingLevel
# Maps each thinking level to a provider-specific value, or None to mark it
# unsupported. Missing keys fall back to provider defaults.
ThinkingLevelMap = dict[str, str | None]


@dataclass
class ThinkingBudgets:
    """Token budgets for each thinking level (token-based providers only)."""

    minimal: int | None = None
    low: int | None = None
    medium: int | None = None
    high: int | None = None


# ========== Shared option primitives ==========

CacheRetention: TypeAlias = Literal["none", "short", "long"]
Transport: TypeAlias = Literal["sse", "websocket", "websocket-cached", "auto"]
ProviderEnv = dict[str, str]
ProviderHeaders = dict[str, str | None]


@dataclass
class ProviderResponse:
    status: int
    headers: dict[str, str]


@dataclass
class StreamOptions:
    """Options shared by all provider stream calls."""

    temperature: float | None = None
    max_tokens: int | None = None
    signal: "AbortSignal | None" = None  # asyncio.Event-like; defined in event_stream
    api_key: str | None = None
    transport: Transport | None = None
    cache_retention: CacheRetention | None = "short"
    session_id: str | None = None
    headers: ProviderHeaders | None = None
    timeout_ms: int | None = None
    websocket_connect_timeout_ms: int | None = None
    max_retries: int | None = None
    max_retry_delay_ms: int | None = None
    metadata: dict[str, Any] | None = None
    env: ProviderEnv | None = None
    # Provider-specific extra fields (e.g. AnthropicOptions additions).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimpleStreamOptions(StreamOptions):
    """Stream options with reasoning/thinking controls (stream_simple)."""

    reasoning: ThinkingLevel | None = None
    thinking_budgets: ThinkingBudgets | None = None


# ========== Content blocks ==========


@dataclass
class TextContent:
    type: str = field(default="text", init=False)
    text: str
    text_signature: str | None = None  # legacy id or TextSignatureV1 JSON


@dataclass
class ThinkingContent:
    type: str = field(default="thinking", init=False)
    thinking: str
    thinking_signature: str | None = None
    redacted: bool = False


@dataclass
class ImageContent:
    type: str = field(default="image", init=False)
    data: str  # base64 encoded
    mime_type: str  # e.g. "image/jpeg", "image/png"


@dataclass
class ToolCall:
    type: str = field(default="toolCall", init=False)
    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: str | None = None  # Google-specific


# ========== Usage and cost ==========


@dataclass
class CostInfo:
    input: float
    output: float
    cache_read: float
    cache_write: float
    total: float


@dataclass
class Usage:
    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int
    cost: CostInfo
    cache_write_1h: int | None = None
    # Subset of output; None when provider doesn't report a reasoning breakdown.
    reasoning: int | None = None


# ========== Stop reasons ==========

StopReason: TypeAlias = Literal["stop", "length", "toolUse", "error", "aborted"]

# ========== Messages ==========

UserContent: TypeAlias = str | list[TextContent | ImageContent]
AssistantContent: TypeAlias = list[TextContent | ThinkingContent | ToolCall]
ToolResultContent: TypeAlias = list[TextContent | ImageContent]


@dataclass
class UserMessage:
    role: str = field(default="user", init=False)
    content: UserContent
    timestamp: int  # Unix ms


@dataclass
class AssistantMessage:
    role: str = field(default="assistant", init=False)
    content: AssistantContent
    api: Api
    provider: ProviderId
    model: str
    usage: Usage
    stop_reason: StopReason
    timestamp: int  # Unix ms
    response_model: str | None = None
    response_id: str | None = None
    error_message: str | None = None


@dataclass
class ToolResultMessage:
    role: str = field(default="toolResult", init=False)
    tool_call_id: str
    tool_name: str
    content: ToolResultContent
    is_error: bool
    timestamp: int  # Unix ms
    details: Any | None = None


Message: TypeAlias = UserMessage | AssistantMessage | ToolResultMessage

# ========== Tools and context ==========


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema


@dataclass
class Context:
    messages: list[Message]
    system_prompt: str | None = None
    tools: list[Tool] | None = None


# ========== Model ==========


@dataclass
class ModelCost:
    input: float  # $/million tokens
    output: float
    cache_read: float
    cache_write: float


@dataclass
class Model:
    id: str
    name: str
    api: Api
    provider: ProviderId
    base_url: str
    context_window: int
    max_tokens: int
    cost: ModelCost
    input: list[str] = field(default_factory=lambda: ["text"])  # "text" | "image"
    reasoning: bool = False
    thinking_level_map: ThinkingLevelMap | None = None
    headers: dict[str, str] | None = None
    # Provider-specific compatibility overrides (typed per-API in TS; kept as
    # a dict here for simplicity, expanded in Phase 4 compat work).
    compat: dict[str, Any] | None = None
"""
重要字段：
- `id`：发给 provider 的模型 ID。
- `provider`：用于在 `Models` 注册表中找到所属 provider。
- `api`：provider 内部选择哪一种协议实现。
- `base_url`：请求地址。
- `context_window`：输入和输出可用的总 token 上限。
- `max_tokens`：单次最大输出 token 数。
- `cost`：每一百万 token 的美元价格。
- `input`：支持 `"text"` 或 `"image"`。
- `reasoning`：是否支持 thinking/reasoning。
- `thinking_level_map`：通用 thinking 等级到 provider 参数的映射。
- `compat`：暂存 provider 特殊兼容配置。
"""

# ========== Stream events ==========
#
# Mirrors AssistantMessageEvent from types.ts. Streams emit `start` before
# partial updates, then terminate with `done` (success) or `error`.

TEvent = TypeVar("TEvent")
TResult = TypeVar("TResult")


@dataclass
class StartEvent:
    type: str = field(default="start", init=False)
    partial: AssistantMessage


@dataclass
class TextStartEvent:
    type: str = field(default="text_start", init=False)
    content_index: int
    partial: AssistantMessage


@dataclass
class TextDeltaEvent:
    type: str = field(default="text_delta", init=False)
    content_index: int
    delta: str
    partial: AssistantMessage


@dataclass
class TextEndEvent:
    type: str = field(default="text_end", init=False)
    content_index: int
    content: str
    partial: AssistantMessage


@dataclass
class ThinkingStartEvent:
    type: str = field(default="thinking_start", init=False)
    content_index: int
    partial: AssistantMessage


@dataclass
class ThinkingDeltaEvent:
    type: str = field(default="thinking_delta", init=False)
    content_index: int
    delta: str
    partial: AssistantMessage


@dataclass
class ThinkingEndEvent:
    type: str = field(default="thinking_end", init=False)
    content_index: int
    content: str
    partial: AssistantMessage


@dataclass
class ToolCallStartEvent:
    type: str = field(default="toolcall_start", init=False)
    content_index: int
    partial: AssistantMessage


@dataclass
class ToolCallDeltaEvent:
    type: str = field(default="toolcall_delta", init=False)
    content_index: int
    delta: str
    partial: AssistantMessage


@dataclass
class ToolCallEndEvent:
    type: str = field(default="toolcall_end", init=False)
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage


@dataclass
class DoneEvent:
    type: str = field(default="done", init=False)
    reason: "stop" | "length" | "toolUse"
    message: AssistantMessage


@dataclass
class ErrorEvent:
    type: str = field(default="error", init=False)
    reason: "aborted" | "error"
    error: AssistantMessage


AssistantMessageEvent: TypeAlias = (
    StartEvent
    | TextStartEvent
    | TextDeltaEvent
    | TextEndEvent
    | ThinkingStartEvent
    | ThinkingDeltaEvent
    | ThinkingEndEvent
    | ToolCallStartEvent
    | ToolCallDeltaEvent
    | ToolCallEndEvent
    | DoneEvent
    | ErrorEvent
)
