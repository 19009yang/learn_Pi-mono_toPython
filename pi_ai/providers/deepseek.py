"""DeepSeek provider registration."""

from __future__ import annotations

from pi_ai.auth import ProviderAuth, env_api_key_auth
from pi_ai.models import Provider, create_provider
from pi_ai.providers.model_catalogs import get_deepseek_models
from pi_ai.providers.openai_completions import OpenAICompletionsStreams


def deepseek_provider() -> Provider:
    """Create a DeepSeek provider using its OpenAI-compatible endpoint."""

    return create_provider(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        auth=ProviderAuth(
            api_key=env_api_key_auth(
                "DeepSeek API key",
                ["DEEPSEEK_API_KEY"],
            )
        ),
        models=get_deepseek_models(),
        api=OpenAICompletionsStreams(),
    )