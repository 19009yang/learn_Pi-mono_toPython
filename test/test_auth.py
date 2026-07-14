"""Tests for pi_ai.auth (roadmap Phase 1.2)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pi_ai.auth import (
    ApiKeyCredential,
    AuthResolutionOverrides,
    InMemoryCredentialStore,
    ProviderAuth,
    default_provider_auth_context,
    env_api_key_auth,
    get_api_key_env_vars,
    resolve_provider_auth,
)
from pi_ai.types import Model, ModelCost


def _model(provider: str = "anthropic") -> Model:
    return Model(
        id="claude-test",
        name="Claude Test",
        api="anthropic-messages",
        provider=provider,
        base_url="https://api.anthropic.com",
        context_window=200_000,
        max_tokens=8192,
        cost=ModelCost(0, 0, 0, 0),
    )


@dataclass
class _FakeProvider:
    id: str
    auth: ProviderAuth


def _anthropic_provider() -> _FakeProvider:
    vars_ = get_api_key_env_vars("anthropic")
    assert vars_ is not None
    return _FakeProvider(
        id="anthropic",
        auth=ProviderAuth(api_key=env_api_key_auth("Anthropic API key", vars_)),
    )


@pytest.mark.asyncio
async def test_resolve_from_anthropic_api_key_env() -> None:
    """roadmap 验证点：设置 ANTHROPIC_API_KEY → 得到带 api_key 的 AuthResult。"""
    provider = _anthropic_provider()
    store = InMemoryCredentialStore()
    ctx = default_provider_auth_context({"ANTHROPIC_API_KEY": "sk-test-env"})

    result = await resolve_provider_auth(provider, _model(), store, ctx)

    assert result is not None
    assert result.auth.api_key == "sk-test-env"
    assert result.source == "ANTHROPIC_API_KEY"


@pytest.mark.asyncio
async def test_resolve_returns_none_without_env() -> None:
    """roadmap 验证点：未设置 → None。"""
    provider = _anthropic_provider()
    store = InMemoryCredentialStore()
    ctx = default_provider_auth_context({})

    result = await resolve_provider_auth(provider, _model(), store, ctx)
    assert result is None


@pytest.mark.asyncio
async def test_oauth_token_env_wins_over_api_key() -> None:
    provider = _anthropic_provider()
    store = InMemoryCredentialStore()
    ctx = default_provider_auth_context(
        {
            "ANTHROPIC_OAUTH_TOKEN": "oauth-tok",
            "ANTHROPIC_API_KEY": "sk-should-not-win",
        }
    )

    result = await resolve_provider_auth(provider, _model(), store, ctx)
    assert result is not None
    assert result.auth.api_key == "oauth-tok"
    assert result.source == "ANTHROPIC_OAUTH_TOKEN"


@pytest.mark.asyncio
async def test_stored_credential_beats_env() -> None:
    """有存档时不静默回退到 env。"""
    provider = _anthropic_provider()
    store = InMemoryCredentialStore()
    await store.modify("anthropic", lambda _c: _async_cred("sk-stored"))
    ctx = default_provider_auth_context({"ANTHROPIC_API_KEY": "sk-env"})

    result = await resolve_provider_auth(provider, _model(), store, ctx)
    assert result is not None
    assert result.auth.api_key == "sk-stored"
    assert result.source == "stored credential"


@pytest.mark.asyncio
async def test_override_api_key() -> None:
    provider = _anthropic_provider()
    store = InMemoryCredentialStore()
    ctx = default_provider_auth_context({"ANTHROPIC_API_KEY": "sk-env"})

    result = await resolve_provider_auth(
        provider,
        _model(),
        store,
        ctx,
        AuthResolutionOverrides(api_key="sk-override"),
    )
    assert result is not None
    assert result.auth.api_key == "sk-override"
    assert result.source == "stored credential"


@pytest.mark.asyncio
async def test_empty_store_credential_key_falls_through_to_env_only_when_no_store_entry() -> None:
    """无 store 条目时才读 env；空 key 的 api_key 存档仍算「有存档」。"""
    provider = _anthropic_provider()
    store = InMemoryCredentialStore()
    await store.modify("anthropic", lambda _c: _async_cred(None))
    ctx = default_provider_auth_context({"ANTHROPIC_API_KEY": "sk-env"})

    # 有存档（哪怕 key 为空）→ 走 stored 分支；resolve 在无 key 时再查 env
    result = await resolve_provider_auth(provider, _model(), store, ctx)
    assert result is not None
    assert result.auth.api_key == "sk-env"
    assert result.source == "ANTHROPIC_API_KEY"


async def _async_cred(key: str | None) -> ApiKeyCredential:
    return ApiKeyCredential(key=key)
