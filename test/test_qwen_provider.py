"""Phase 4.3 tests for the DashScope Qwen provider."""

from __future__ import annotations

from pi_ai.auth import get_api_key_env_vars
from pi_ai.providers.openai_completions import OpenAICompletionsOptions, build_params
from pi_ai.providers.qwen import DASHSCOPE_COMPATIBLE_BASE_URL, qwen_provider
from pi_ai.types import AssistantMessage, Context, CostInfo, Model, ModelCost, TextContent, ToolCall, ToolResultMessage, Usage, UserMessage
from pi_coding_agent.cli import create_default_models


def test_qwen_provider_uses_china_dashscope_endpoint_and_api_key_priority() -> None:
    provider = qwen_provider()
    model = provider.get_models()[0]

    assert provider.id == "qwen"
    assert provider.base_url == DASHSCOPE_COMPATIBLE_BASE_URL
    assert model.id == "qwen3.7-plus"
    assert model.base_url == DASHSCOPE_COMPATIBLE_BASE_URL
    assert get_api_key_env_vars("qwen") == ("DASHSCOPE_API_KEY", "QWEN_API_KEY")


def test_qwen_compat_uses_max_tokens_and_enable_thinking() -> None:
    model = qwen_provider().get_models()[0]
    params = build_params(
        model,
        Context(messages=[UserMessage(content="hello", timestamp=1)]),
        OpenAICompletionsOptions(max_tokens=321, reasoning_effort="high"),
    )

    assert params["max_tokens"] == 321
    assert "max_completion_tokens" not in params
    assert params["extra_body"] == {"enable_thinking": True}


def test_qwen_is_registered_alongside_the_existing_provider() -> None:
    models = create_default_models()

    assert models.get_model("qwen", "qwen3.7-plus") is not None
    assert models.get_model("deepseek", "deepseek-v4-pro") is not None


def test_openai_compat_normalizes_replayed_openai_tool_ids() -> None:
    model = Model("gpt", "GPT", "openai-completions", "openai", "https://api.openai.com/v1", 1000, 100, ModelCost(0, 0, 0, 0))
    call_id = "call|" + "x/" * 30
    history = [
        AssistantMessage([ToolCall(call_id, "read", {})], model.api, model.provider, model.id, Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)), "toolUse", 1),
        ToolResultMessage(call_id, "read", [TextContent("ok")], False, 2),
    ]

    params = build_params(model, Context(messages=history))

    normalized = params["messages"][0]["tool_calls"][0]["id"]  # type: ignore[index]
    assert len(normalized) <= 40
    assert params["messages"][1]["tool_call_id"] == normalized  # type: ignore[index]
