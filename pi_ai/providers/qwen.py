"""Qwen provider registration for DashScope's China-mainland compatibility API."""

from __future__ import annotations

from pi_ai.auth import ProviderAuth, env_api_key_auth
from pi_ai.models import Provider, create_provider
from pi_ai.providers.model_catalogs import get_qwen_models
from pi_ai.providers.openai_completions import OpenAICompletionsStreams


DASHSCOPE_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def qwen_provider() -> Provider:
    """Create the Qwen provider backed by Alibaba Cloud DashScope (China)."""

    return create_provider(
        id="qwen",
        name="Qwen (DashScope China)",
        base_url=DASHSCOPE_COMPATIBLE_BASE_URL,
        auth=ProviderAuth(
            api_key=env_api_key_auth(
                "DashScope API key",
                ["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
            )
        ),
        models=get_qwen_models(),
        api=OpenAICompletionsStreams(),
    )
