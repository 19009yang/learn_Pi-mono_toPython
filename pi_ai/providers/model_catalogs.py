"""模型目录"""

from __future__ import annotations

from pi_ai.types import Model, ModelCost

_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def _anthropic_model(
    *,
    id: str,
    name: str,
    input_cost: float,
    output_cost: float,
    cache_read_cost: float,
    cache_write_cost: float,
    context_window: int,
    max_tokens: int,
    thinking_level_map: dict[str, str | None] | None = None,
    compat: dict[str, object] | None = None,
) -> Model:
    return Model(
        id=id,
        name=name,
        api="anthropic-messages",
        provider="anthropic",
        base_url=_ANTHROPIC_BASE_URL,
        reasoning=True,
        thinking_level_map=thinking_level_map,
        input=["text", "image"],
        cost=ModelCost(
            input=input_cost,
            output=output_cost,
            cache_read=cache_read_cost,
            cache_write=cache_write_cost,
        ),
        context_window=context_window,
        max_tokens=max_tokens,
        compat=compat,
    )


# Deliberately small Phase 1 catalog. Values mirror anthropic.models.ts.
ANTHROPIC_MODELS: dict[str, Model] = {
    "claude-haiku-4-5": _anthropic_model(
        id="claude-haiku-4-5",
        name="Claude Haiku 4.5 (latest)",
        input_cost=1,
        output_cost=5,
        cache_read_cost=0.1,
        cache_write_cost=1.25,
        context_window=200_000,
        max_tokens=64_000,
    ),
    "claude-sonnet-4-5": _anthropic_model(
        id="claude-sonnet-4-5",
        name="Claude Sonnet 4.5 (latest)",
        input_cost=3,
        output_cost=15,
        cache_read_cost=0.3,
        cache_write_cost=3.75,
        context_window=1_000_000,
        max_tokens=64_000,
    ),
    "claude-sonnet-4-6": _anthropic_model(
        id="claude-sonnet-4-6",
        name="Claude Sonnet 4.6",
        input_cost=3,
        output_cost=15,
        cache_read_cost=0.3,
        cache_write_cost=3.75,
        context_window=1_000_000,
        max_tokens=128_000,
        compat={"forceAdaptiveThinking": True},
    ),
    "claude-opus-4-6": _anthropic_model(
        id="claude-opus-4-6",
        name="Claude Opus 4.6",
        input_cost=5,
        output_cost=25,
        cache_read_cost=0.5,
        cache_write_cost=6.25,
        context_window=1_000_000,
        max_tokens=128_000,
        thinking_level_map={"xhigh": "max"},
        compat={"forceAdaptiveThinking": True},
    ),
    "claude-opus-4-8": _anthropic_model(
        id="claude-opus-4-8",
        name="Claude Opus 4.8",
        input_cost=5,
        output_cost=25,
        cache_read_cost=0.5,
        cache_write_cost=6.25,
        context_window=1_000_000,
        max_tokens=128_000,
        thinking_level_map={"xhigh": "xhigh"},
        compat={
            "forceAdaptiveThinking": True,
            "supportsTemperature": False,
        },
    ),
}


def get_anthropic_models() -> list[Model]:
    """Return the Anthropic catalog in stable display order."""

    return list(ANTHROPIC_MODELS.values())


DEEPSEEK_MODELS: dict[str, Model] = {
    "deepseek-v4-flash": Model(
        id="deepseek-v4-flash",
        name="DeepSeek V4 Flash",
        api="openai-completions",
        provider="deepseek",
        base_url=_DEEPSEEK_BASE_URL,
        reasoning=True,
        thinking_level_map={
            "minimal": None,
            "low": None,
            "medium": None,
            "high": "high",
            "xhigh": "max",
        },
        input=["text"],
        cost=ModelCost(
            input=0.14,
            output=0.28,
            cache_read=0.0028,
            cache_write=0,
        ),
        context_window=1_000_000,
        max_tokens=384_000,
        compat={
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "requiresReasoningContentOnAssistantMessages": True,
            "thinkingFormat": "deepseek",
        },
    ),
    "deepseek-v4-pro": Model(
        id="deepseek-v4-pro",
        name="DeepSeek V4 Pro",
        api="openai-completions",
        provider="deepseek",
        base_url=_DEEPSEEK_BASE_URL,
        reasoning=True,
        thinking_level_map={
            "minimal": None,
            "low": None,
            "medium": None,
            "high": "high",
            "xhigh": "max",
        },
        input=["text"],
        cost=ModelCost(
            input=0.435,
            output=0.87,
            cache_read=0.003625,
            cache_write=0,
        ),
        context_window=1_000_000,
        max_tokens=384_000,
        compat={
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "requiresReasoningContentOnAssistantMessages": True,
            "thinkingFormat": "deepseek",
        },
    ),
}


def get_deepseek_models() -> list[Model]:
    """Return the DeepSeek catalog in stable display order."""

    return list(DEEPSEEK_MODELS.values())
