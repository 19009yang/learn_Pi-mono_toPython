"""Tests for the Phase 1.3 model registry and catalog."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pi_ai.auth import ProviderAuth, default_provider_auth_context, env_api_key_auth
from pi_ai.event_stream import AssistantMessageEventStream
from pi_ai.models import (
    calculate_cost,
    clamp_thinking_level,
    create_models,
    create_provider,
    get_supported_thinking_levels,
    has_api,
)
from pi_ai.providers.model_catalogs import ANTHROPIC_MODELS
from pi_ai.types import (
    AssistantMessage,
    Context,
    CostInfo,
    DoneEvent,
    Model,
    ModelCost,
    SimpleStreamOptions,
    StreamOptions,
    Usage,
)


class _FakeStreams:
    def __init__(self) -> None:
        self.last_options: StreamOptions | SimpleStreamOptions | None = None

    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        return self._finish(model, options)

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        return self._finish(model, options)

    def _finish(
        self,
        model: Model,
        options: StreamOptions | SimpleStreamOptions | None,
    ) -> AssistantMessageEventStream:
        self.last_options = options
        message = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)),
            stop_reason="stop",
            timestamp=0,
        )
        stream = AssistantMessageEventStream()
        stream.push(DoneEvent(reason="stop", message=message))
        return stream


def _provider(streams: _FakeStreams):
    return create_provider(
        id="anthropic",
        name="Anthropic",
        auth=ProviderAuth(
            api_key=env_api_key_auth("Anthropic API key", ["ANTHROPIC_API_KEY"])
        ),
        models=list(ANTHROPIC_MODELS.values()),
        api=streams,
        base_url="https://api.anthropic.com",
    )


def test_catalog_lookup() -> None:
    models = create_models()
    models.set_provider(_provider(_FakeStreams()))

    model = models.get_model("anthropic", "claude-sonnet-4-6")

    assert model is ANTHROPIC_MODELS["claude-sonnet-4-6"]
    assert model.context_window == 1_000_000
    assert model.input == ["text", "image"]
    assert has_api(model, "anthropic-messages")
    assert not has_api(model, "openai-responses")


async def test_stream_simple_applies_environment_auth() -> None:
    streams = _FakeStreams()
    models = create_models(
        auth_context=default_provider_auth_context(
            {"ANTHROPIC_API_KEY": "sk-test"}
        )
    )
    models.set_provider(_provider(streams))
    model = ANTHROPIC_MODELS["claude-sonnet-4-6"]

    message = await models.complete_simple(model, Context(messages=[]))

    assert message.model == model.id
    assert isinstance(streams.last_options, SimpleStreamOptions)
    assert streams.last_options.api_key == "sk-test"


def test_calculate_cost_including_one_hour_cache_write() -> None:
    model = Model(
        id="test",
        name="Test",
        api="test",
        provider="test",
        base_url="https://example.com",
        context_window=100,
        max_tokens=10,
        cost=ModelCost(input=2, output=4, cache_read=0.2, cache_write=2.5),
    )
    usage = Usage(
        input=1_000_000,
        output=500_000,
        cache_read=100_000,
        cache_write=300_000,
        cache_write_1h=100_000,
        total_tokens=1_900_000,
        cost=CostInfo(0, 0, 0, 0, 0),
    )

    cost = calculate_cost(usage, model)

    assert cost.input == 2
    assert cost.output == 2
    assert cost.cache_read == 0.02
    assert cost.cache_write == pytest.approx(0.9)
    assert cost.total == pytest.approx(4.92)


def test_thinking_level_support_and_clamping() -> None:
    model = ANTHROPIC_MODELS["claude-opus-4-6"]

    assert get_supported_thinking_levels(model) == [
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert clamp_thinking_level("xhigh", model) == "xhigh"

    without_high = replace(model, thinking_level_map={"high": None, "xhigh": "max"})
    assert clamp_thinking_level("high", without_high) == "xhigh"
