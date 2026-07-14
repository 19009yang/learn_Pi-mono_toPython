"""Model registry, provider construction, auth application, and cost helpers."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Protocol, TypeAlias, cast

from pi_ai.auth import (
    AuthContext,
    AuthResolutionOverrides,
    AuthResult,
    CredentialStore,
    InMemoryCredentialStore,
    ModelsError,
    ProviderAuth,
    default_provider_auth_context,
    resolve_provider_auth,
)
from pi_ai.event_stream import AssistantMessageEventStream
from pi_ai.types import (
    AssistantMessage,
    Context,
    CostInfo,
    ErrorEvent,
    Model,
    ModelThinkingLevel,
    ProviderHeaders,
    SimpleStreamOptions,
    StreamOptions,
    Usage,
)

# 接口层，不关心底层的实现和具体Provider的API差异
"""
- stream() — 完整流式调用，接受 StreamOptions，可能包含更复杂的配置（如 tool use、多轮对话上下文等）
- stream_simple() — 简化流式调用，接受 SimpleStreamOptions，用于更简单的场景（如纯文本补全，无需复杂选项）
"""
class ProviderStreams(Protocol):
    """Streaming implementation supplied by one provider API."""

    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> AssistantMessageEventStream: ...

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessageEventStream: ...


RefreshModels: TypeAlias = Callable[[], Awaitable[Sequence[Model]]]
# 一个无参数的异步函数，调用后返回一组Model对象，它的作用是定义一个刷新/更新模型列表的回调函数签名

class Provider(ABC):
    """Concrete runtime unit owning models, auth, and stream behavior."""
    id: str #Provider 的唯一标识符，如 "openai"、"anthropic" 
    name: str #人类可读的名称，如 "OpenAI"、"Anthropic"
    base_url: str | None
    headers: ProviderHeaders | None # 请求时附加的自定义 HTTP 头（可选），某些 Provider 需要额外头部  
    auth: ProviderAuth #认证配置（必选），携带 API Key、OAuth 等认证信息

    @abstractmethod
    def get_models(self) -> list[Model]: ...
    # 每个Provider返回自己支持的模型列表，这是从本地/缓存获取，不是网络请求，所以是同步方法
    # 如 OpenAI 返回 [gpt-4o, gpt-4o-mini, ...]

    async def refresh_models(self) -> None:
        """  
        - 非抽象方法，已有默认实现（空操作 / no-op）
        - 设计意图：有些 Provider 的模型列表是静态的（固定不变），它们不需要刷新，直接继承这个空方法就行
        - 有些 Provider 模型列表是动态的（API 会增删模型），它们覆盖（override） 这个方法，从 API
        拉取最新列表并更新内部缓存
        - 异步是因为网络请求需要 await
        """


    # 每个 Provider 必须提供自己的流式响应能力
    @abstractmethod
    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> AssistantMessageEventStream: ...

    @abstractmethod
    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessageEventStream: ...


#构造函数 — 把所有零件组装起来，并且构造上述基类函数的具体实现
class _CreatedProvider(Provider):
    def __init__(
        self,
        *, #关键词传参
        provider_id: str,
        name: str,
        base_url: str | None,
        headers: ProviderHeaders | None,
        auth: ProviderAuth,
        models: Sequence[Model],
        api: ProviderStreams | Mapping[str, ProviderStreams],
        refresh_models: RefreshModels | None,
    ) -> None:
        self.id = provider_id
        self.name = name
        self.base_url = base_url
        self.headers = headers
        self.auth = auth
        self._models = list(models)
        self._api = api
        self._refresh = refresh_models
        self._inflight_refresh: asyncio.Task[None] | None = None

    def get_models(self) -> list[Model]:
        return list(self._models) #返回简单副本，防止外部修改内部状态

    #重点：并发安全的刷新机制
    async def refresh_models(self) -> None:
        if self._refresh is None:
            return  # ① 静态 Provider，直接跳过

        task = self._inflight_refresh #如果有正在进行的_inflight_refresh，先等待任务完成
        if task is None:    # ② 没有正在进行的刷新 → 启动新任务
            async def update() -> None:
                refreshed = await self._refresh()
                self._models = list(refreshed)

            task = asyncio.create_task(update())
            self._inflight_refresh = task

        try:
            await task # ③ 等待任务完成
        finally:
            if self._inflight_refresh is task: # ④ 清理引用（只有当前task仍然是最新的才清理）
                self._inflight_refresh = None

    # API 实现的路由
    def _streams_for(self, model: Model) -> ProviderStreams | None:
        if _is_provider_streams(self._api): # ← 判断是单一对象还是字典
            return self._api                 # 单一对象 → 直接返回
        return self._api.get(model.api)     # 字典 → 按 key 查找

    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        streams = self._streams_for(model)
        if streams is None:
            return _failed_stream(
                model,
                ModelsError(
                    "stream",
                    f'Provider {self.id} has no API implementation for "{model.api}"',
                ),
            )
        return streams.stream(model, context, options)

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        streams = self._streams_for(model)
        if streams is None:
            return _failed_stream(
                model,
                ModelsError(
                    "stream",
                    f'Provider {self.id} has no API implementation for "{model.api}"',
                ),
            )
        return streams.stream_simple(model, context, options)


# 判断 api 参数是单一 ProviderStreams 对象还是映射字典 Mapping[str, ProviderStreams]
def _is_provider_streams(
    value: ProviderStreams | Mapping[str, ProviderStreams],
) -> bool:
    return callable(getattr(value, "stream", None)) and callable(
        getattr(value, "stream_simple", None)
    )


class Models:
    """Provider registry with model lookup, auth resolution, and streaming."""

    def __init__(
        self,
        *,
        credentials: CredentialStore | None = None,
        auth_context: AuthContext | None = None,
    ) -> None:
        self._providers: dict[str, Provider] = {}
        self._credentials = credentials or InMemoryCredentialStore()
        self._auth_context = auth_context or default_provider_auth_context()

    def get_providers(self) -> list[Provider]:
        return list(self._providers.values())

    def get_provider(self, provider_id: str) -> Provider | None:
        return self._providers.get(provider_id)

    def get_models(self, provider: str | None = None) -> list[Model]:
        if provider is not None:
            entry = self._providers.get(provider)
            if entry is None:
                return []
            try:
                return entry.get_models()
            except Exception:
                return []

        models: list[Model] = []
        for entry in self._providers.values():
            try:
                models.extend(entry.get_models())
            except Exception:
                continue
        return models

    def get_model(self, provider: str, model_id: str) -> Model | None:
        return next(
            (model for model in self.get_models(provider) if model.id == model_id),
            None,
        )

    async def refresh(self, provider: str | None = None) -> None:
        if provider is not None:
            entry = self._providers.get(provider)
            if entry is None:
                return
            try:
                await entry.refresh_models()
            except ModelsError:
                raise
            except Exception as error:
                raise ModelsError(
                    "model_source",
                    f"Model refresh failed for {provider}",
                    cause=error,
                ) from error
            return

        await asyncio.gather(
            *(entry.refresh_models() for entry in self._providers.values()),
            return_exceptions=True,
        )

    async def get_auth(self, model: Model) -> AuthResult | None:
        provider = self._providers.get(model.provider)
        if provider is None:
            return None
        return await resolve_provider_auth(
            provider,
            model,
            self._credentials,
            self._auth_context,
        )

    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        async def setup() -> AssistantMessageEventStream:
            provider = self._require_provider(model)
            request_model, request_options = await self._apply_auth(
                provider, model, options, simple=False
            )
            return provider.stream(
                request_model,
                context,
                cast(StreamOptions | None, request_options),
            )

        return _lazy_stream(model, setup)

    async def complete(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> AssistantMessage:
        return await self.stream(model, context, options).result()

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        async def setup() -> AssistantMessageEventStream:
            provider = self._require_provider(model)
            request_model, request_options = await self._apply_auth(
                provider, model, options, simple=True
            )
            return provider.stream_simple(
                request_model,
                context,
                cast(SimpleStreamOptions | None, request_options),
            )

        return _lazy_stream(model, setup)

    async def complete_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessage:
        return await self.stream_simple(model, context, options).result()

    def _require_provider(self, model: Model) -> Provider:
        provider = self._providers.get(model.provider)
        if provider is None:
            raise ModelsError("provider", f"Unknown provider: {model.provider}")
        return provider

    async def _apply_auth(
        self,
        provider: Provider,
        model: Model,
        options: StreamOptions | SimpleStreamOptions | None,
        *,
        simple: bool,
    ) -> tuple[Model, StreamOptions | SimpleStreamOptions | None]:
        resolution = await resolve_provider_auth(
            provider,
            model,
            self._credentials,
            self._auth_context,
            AuthResolutionOverrides(
                api_key=options.api_key if options is not None else None,
                env=options.env if options is not None else None,
            ),
        )
        if resolution is None:
            return model, options

        auth = resolution.auth
        request_model = (
            replace(model, base_url=auth.base_url) if auth.base_url else model
        )
        api_key = (
            options.api_key
            if options is not None and options.api_key is not None
            else auth.api_key
        )
        option_headers = options.headers if options is not None else None
        headers = (
            {**(auth.headers or {}), **(option_headers or {})}
            if auth.headers or option_headers
            else None
        )
        option_env = options.env if options is not None else None
        env = (
            {**(resolution.env or {}), **(option_env or {})}
            if resolution.env or option_env
            else None
        )

        if options is None:
            option_type = SimpleStreamOptions if simple else StreamOptions
            request_options = option_type(api_key=api_key, headers=headers, env=env)
        else:
            request_options = replace(
                options,
                api_key=api_key,
                headers=headers,
                env=env,
            )
        return request_model, request_options


class MutableModels(Models):
    """Models collection whose provider registry can be changed at runtime."""

    def set_provider(self, provider: Provider) -> None:
        self._providers[provider.id] = provider

    def delete_provider(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def clear_providers(self) -> None:
        self._providers.clear()


def create_provider(
    *,
    id: str,
    auth: ProviderAuth,
    models: Sequence[Model],
    api: ProviderStreams | Mapping[str, ProviderStreams],
    name: str | None = None,
    base_url: str | None = None,
    headers: ProviderHeaders | None = None,
    refresh_models: RefreshModels | None = None,
) -> Provider:
    """Build a provider from static metadata and one or more API streams."""

    return _CreatedProvider(
        provider_id=id,
        name=name or id,
        base_url=base_url,
        headers=headers,
        auth=auth,
        models=models,
        api=api,
        refresh_models=refresh_models,
    )


def create_models(
    credentials: CredentialStore | None = None,
    auth_context: AuthContext | None = None,
) -> MutableModels:
    """Create an empty mutable provider registry."""

    return MutableModels(credentials=credentials, auth_context=auth_context)


def calculate_cost(usage: Usage, model: Model) -> CostInfo:
    """Populate and return usage cost from per-million-token model prices."""

    long_write = usage.cache_write_1h or 0
    short_write = usage.cache_write - long_write
    usage.cost.input = model.cost.input * usage.input / 1_000_000
    usage.cost.output = model.cost.output * usage.output / 1_000_000
    usage.cost.cache_read = model.cost.cache_read * usage.cache_read / 1_000_000
    usage.cost.cache_write = (
        model.cost.cache_write * short_write + model.cost.input * 2 * long_write
    ) / 1_000_000
    usage.cost.total = (
        usage.cost.input
        + usage.cost.output
        + usage.cost.cache_read
        + usage.cost.cache_write
    )
    return usage.cost


_EXTENDED_THINKING_LEVELS: tuple[ModelThinkingLevel, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)


def get_supported_thinking_levels(model: Model) -> list[ModelThinkingLevel]:
    if not model.reasoning:
        return ["off"]

    supported: list[ModelThinkingLevel] = []
    for level in _EXTENDED_THINKING_LEVELS:
        mapped = (model.thinking_level_map or {}).get(level)
        if mapped is None and level in (model.thinking_level_map or {}):
            continue
        if level == "xhigh" and mapped is None:
            continue
        supported.append(level)
    return supported


def has_api(model: Model, api: str) -> bool:
    """Return whether a dynamically looked-up model uses the requested API."""

    return model.api == api


def clamp_thinking_level(
    level: ModelThinkingLevel,
    model: Model,
) -> ModelThinkingLevel:
    supported = get_supported_thinking_levels(model)
    if level in supported:
        return level

    requested_index = _EXTENDED_THINKING_LEVELS.index(level)
    for candidate in _EXTENDED_THINKING_LEVELS[requested_index:]:
        if candidate in supported:
            return candidate
    for candidate in reversed(_EXTENDED_THINKING_LEVELS[:requested_index]):
        if candidate in supported:
            return candidate
    return supported[0] if supported else "off"


def models_are_equal(model_a: Model | None, model_b: Model | None) -> bool:
    return (
        model_a is not None
        and model_b is not None
        and model_a.id == model_b.id
        and model_a.provider == model_b.provider
    )


SetupStream = Callable[[], Awaitable[AssistantMessageEventStream]]


def _lazy_stream(model: Model, setup: SetupStream) -> AssistantMessageEventStream:
    outer = AssistantMessageEventStream()

    async def run() -> None:
        try:
            inner = await setup()
            async for event in inner:
                outer.push(event)
            outer.end()
        except Exception as error:
            message = _setup_error_message(model, error)
            outer.push(ErrorEvent(reason="error", error=message))

    asyncio.create_task(run())
    return outer


def _failed_stream(model: Model, error: BaseException) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    message = _setup_error_message(model, error)
    stream.push(ErrorEvent(reason="error", error=message))
    return stream


def _setup_error_message(model: Model, error: BaseException) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(
            input=0,
            output=0,
            cache_read=0,
            cache_write=0,
            total_tokens=0,
            cost=CostInfo(0, 0, 0, 0, 0),
        ),
        stop_reason="error",
        timestamp=int(time.time() * 1000),
        error_message=str(error),
    )
