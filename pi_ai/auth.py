"""凭据存储与解析（pi_ai）。

Python 移植自 packages/ai/src/auth/（types / credential-store / helpers / resolve / context）
及 packages/ai/src/env-api-keys.ts 中的环境变量映射（MVP 子集）。

设计要点（与 TS 一致）：
- CredentialStore 按 Provider.id 存一条凭据；modify 是唯一写路径并串行化。
- resolve_provider_auth 优先级：
  1. 请求覆盖 api_key
  2. 已存储凭据（有存档则不再静默回退 env）
  3. 无存档时走 ambient（环境变量等）
- 本阶段不实现 OAuth refresh；存了 oauth 且无 handler 时返回 None。

roadmap 验证点：设置 ANTHROPIC_API_KEY 后 resolve 得到 AuthResult；未设置返回 None。
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pi_ai.types import Model, ProviderEnv, ProviderHeaders

# ---------------------------------------------------------------------------
# 请求侧 auth / 解析结果
# ---------------------------------------------------------------------------

"""
@dataclass用法：
  # 普通 class 
  class Point:
      def __init__(self, x, y):
          self.x = x
          self.y = y
      def __repr__(self):
          return f"Point(x={self.x}, y={self.y})"
      def __eq__(self, other):
          return self.x == other.x and self.y == other.y

  # dataclass 
  @dataclass
  class Point:
      x: float
      y: float
"""

@dataclass
class ModelAuth:
    """
    单次请求可用的认证信息,对应`auth/types.py`中的

    export interface ModelAuth {
        apiKey?: string;
        headers?: ProviderHeaders;
        baseUrl?: string;
    }
    """
    api_key: str | None = None
    headers: ProviderHeaders | None = None
    base_url: str | None = None


@dataclass
class AuthResult:
    """resolve 成功后的结果。对应 TS AuthResult
    export interface AuthResult {
        auth: ModelAuth;
        env?: ProviderEnv;
        source?: string;
    }
    """
    auth: ModelAuth
    env: ProviderEnv | None = None
    source: str | None = None  # 如 "ANTHROPIC_API_KEY" / "stored credential"


# ---------------------------------------------------------------------------
# 存储侧凭据
# ---------------------------------------------------------------------------

@dataclass
class ApiKeyCredential:
    """已存储的 API Key。对应 TS ApiKeyCredential
    export interface ApiKeyCredential {
        type: "api_key";
        key?: string;
        env?: ProviderEnv;
    }
    """
    type: Literal["api_key"] = "api_key" # 类型约束：这个字段的值只能是字符串 "api_key" ，不传参时自动设为 "api_key" 
    key: str | None = None
    env: ProviderEnv | None = None


@dataclass
class OAuthCredential:
    """OAuth 凭据占位（本阶段不实现 refresh / login）。

    对应 TS OAuthCredential；字段对齐 utils/oauth/types，便于后续扩展
    export interface OAuthCredential extends OAuthCredentials {
        type: "oauth";
    }
    export type OAuthCredentials = {
        refresh: string;
        access: string;
        expires: number;
        [key: string]: unknown;
    };
    """
    type: Literal["oauth"] = "oauth"
    access: str = ""
    refresh: str = ""
    expires: int = 0  # Unix ms


Credential = ApiKeyCredential | OAuthCredential
# 等效 Credential = Union[ApiKeyCredential, OAuthCredential]


class CredentialStore(ABC):
    """应用拥有的凭据存储。对应 TS CredentialStore，按 provider_id 一条凭据。read 缺失返回 None；仅存储失败时抛错。
    export interface CredentialStore {
        read(providerId: string): Promise<Credential | undefined>;
        modify(
                providerId: string,
                fn: (current: Credential | undefined) => Promise<Credential | undefined>,
            ): Promise<Credential | undefined>;
        delete(providerId: string): Promise<void>;
    }
    Promise<...>表示异步结果
    """

    @abstractmethod #装饰器，子类必须实现，否则实例化时报错
    async def read(self, provider_id: str) -> Credential | None: ...
    #  ... 作为函数体，意思是这个方法暂未实现，只声明签名，具体实现在继承子类中

    @abstractmethod
    async def modify(
        self,
        provider_id: str,
        fn: Callable[[Credential | None], Awaitable[Credential | None]],
        # Callable表示"可以被调用的东西"（函数、方法、带 __call__ 的类实例等）
        # 这里表示接收 Credential | None，返回异步结果的函数 Awaitable[Credential | None]
        # Callable[[X], Awaitable[Y]] 表示： 接收 X 返回异步结果的函数 对应操作： await fn(x) 调用并等待
    ) -> Credential | None:
        """串行 read-modify-write。fn 返回 None 表示不改动条目。"""
        ...

    @abstractmethod
    async def delete(self, provider_id: str) -> None: ...


class InMemoryCredentialStore(CredentialStore):
    """默认内存store，对应 TS InMemoryCredentialStore，
    
    在pi中使用enqueue实现：按 provider 用 Promise 链做异步互斥锁；失败不影响后续排队，但错误仍返回给本次调用者
    
    private chains = new Map<string, Promise<unknown>>();
    // 储存每个 provider 上「写操作链」的末端 Promise
    //  Map<string, Promise<unknown>>
    //   │              │
    // providerId    上一次 modify/delete（经 .catch 后）的 Promise

    private enqueue<T>(providerId: string, task: () => Promise<T>): Promise<T> 
    // T是泛型占位名，表示函数返回结果为类型T；第一个参数：按哪个 provider 排队；第二个参数：一个无参函数，调用后返回 Promise<T>
    {
        const previous = this.chains.get(providerId) ?? Promise.resolve();
        //this.chains.get(providerId)表示取出该provider的末端，没有对应 key 时得到 undefined；
        //?? Promise.resolve() 空值合并：左边是 null/undefined 时，用右边；Promise.resolve()表示制造一个「已经成功」的空 Promise，当作无需等待的占位
        const next = (async () => {
            await previous.catch(() => {});
            return task();
        })();
        // ()=>{} 为函数定义:(参数) => {语句}
        // await previous.catch(() => {})表示无论previous是否成功，都继续往下走（返回task()）
        // 如果成功则为正常的await previous，否则调用.catch(() => {})，由于里面是空函数，不会卡死(错误被忽略，值设置为undefined)，而是继续往下走
        // 返回传入task的调用结果(注：传入的是task，并没有被调用，而返回的是task()，实际返回的是Promise<T>)

        this.chains.set(
            providerId,
            next.catch(() => {}),
        );
        // 在排队簿上更新：这个 provider 的「最新一单」
        // 注：此处记录的是next.catch(() => {})，跟上述previous.catch(() => {})效果一致，不关心具体是否成功

        return next;
        // 返回的是具体的next
    }
    """

    def __init__(self) -> None:
        self._credentials: dict[str, Credential] = {}
        # 对应 private credentials = new Map<string, Credential>();
        self._locks: dict[str, asyncio.Lock] = {}
        # 对应 private chains = new Map<string, Promise<unknown>>(); Promise<unknown表示一个结果为unknow的异步结果

    # 按键分配锁，每个provider_id分配一个lock，确保操作不同的provider时互相之间不被阻塞
    def _lock_for(self, provider_id: str) -> asyncio.Lock:
        lock = self._locks.get(provider_id)
        if lock is None:
            lock = asyncio.Lock() #如果没有锁就分配一个专用新锁
            self._locks[provider_id] = lock
        return lock

    # 读取 .get() —— 键不存在时返回 None（安全）
    async def read(self, provider_id: str) -> Credential | None:
        return self._credentials.get(provider_id)
    
    async def modify(
        self,
        provider_id: str,
        fn: Callable[[Credential | None], Awaitable[Credential | None]],
    ) -> Credential | None:
        async with self._lock_for(provider_id): # 分配锁
            current = self._credentials.get(provider_id) #读取当前值
            nxt = await fn(current) #用外部函数，让它根据当前值决定下一步
            if nxt is not None: 
                self._credentials[provider_id] = nxt  #只有 fn 返回了非 None 时才写入。如果 fn 返回 None，什么都不改
            return nxt if nxt is not None else current

    # 删除
    async def delete(self, provider_id: str) -> None:
        # 防御性加锁：未来如果扩展（比如删除前做检查），也可能涉及多步操作，所以也加了锁保持一致性
        async with self._lock_for(provider_id):
            self._credentials.pop(provider_id, None)


# ---------------------------------------------------------------------------
# AuthContext（可注入，便于测试）
# ---------------------------------------------------------------------------


class AuthContext(Protocol):
    """对应 TS AuthContext
    export interface AuthContext {
        env(name: string): Promise<string | undefined>;
        fileExists(path: string): Promise<boolean>;
    }
    """
    async def env(self, name: str) -> str | None: ...
    async def file_exists(self, path: str) -> bool: ...


@dataclass
class DefaultAuthContext:
    """默认实现：读 os.environ；file_exists 支持 ~ 展开。
    对应 TS defaultProviderAuthContext()。
    @dataclass等价：  
    def __init__(self, _environ=None):
        if _environ is None:
            _environ = dict(os.environ)
        else:
            self._environ = _environ
    """

    _environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    # default_factory 是一个函数，每次创建实例时才调用,确保每个实例都有自己的字典
    # 为什么 dict(os.environ) 而不是直接用 os.environ？
    # os.environ 是一个特殊的 Mapping 对象，直接引用它意味着实例会和全局环境变量绑定——外部修改 os.environ
    # 会影响实例内部状态。dict(os.environ) 做了一个快照副本，实例创建后不再受外部变化影响。

    async def env(self, name: str) -> str | None:
        # 签名——异步方法，接收环境变量名，返回值或 None。对应 AuthContext 协议中的 env 方法。
        # 虽然内部没有 await，但声明为 async 是为了对齐协议签名：
        value = self._environ.get(name)
        if isinstance(value, str) and value.strip():
            # value.strip()——检查值不是空字符串或纯空白，否则返回None
            return value
        return None

    async def file_exists(self, path: str) -> bool:
        resolved = path
        if resolved.startswith("~"):
            #  检查路径是否以 ~ 开头。~ 在 Unix/Linux 中代表用户主目录（如 /home/user），在 Windows 中代表 C:\Users\用户名
            resolved = str(Path.home()) + resolved[1:]
            # 等效写法：resolved = str(Path.home() / resolved[2:])  # 用 / 拼接路径
            # 路径展开，将 ~ 替换为实际主目录
        return Path(resolved).exists() #Path(resolved).exists() 检查文件/目录是否存在，返回 bool
# _environ 的可注入设计，测试时可以完全隔离外部依赖：

def default_provider_auth_context(
    environ: dict[str, str] | None = None,
) -> DefaultAuthContext:
    """构造默认 AuthContext；可传入假环境做单测。"""
    if environ is None:
        # 如果没有传入environ,使用默认值os.environ
        return DefaultAuthContext()
    return DefaultAuthContext(_environ=dict(environ))


# ---------------------------------------------------------------------------
# Provider 侧 auth 处理器
# ---------------------------------------------------------------------------


"""
Protocol——这是 Python 的协议类（结构化子类型 / 鸭子类型的类型检查版本）
任何类只要：
  1. 有一个 name: str 属性
  2. 有一个签名匹配的 resolve 异步方法
就自动被类型检查器认为是 ApiKeyAuth，无需声明 class X(ApiKeyAuth)：
"""
class ApiKeyAuth(Protocol):
    """API Key 认证处理器。对应 TS ApiKeyAuth。"""

    name: str

    # 方法签名
    async def resolve(
        self,
        *, # "*"说明后续的参数必须使用关键字形式传递，不能使用位置参数
        model: Model, # 模型信息
        ctx: AuthContext, # 认证上下文——提供 env() 和 file_exists() 方法
        credential: ApiKeyCredential | None = None, # 已存储的凭据——从 CredentialStore 中读出来的值
    ) -> AuthResult | None: ...


@dataclass
class ProviderAuth:
    """Provider 的认证能力。对应 TS ProviderAuth。至少应有 api_key（MVP）。"""
    api_key: ApiKeyAuth | None = None
    oauth: Any | None = None  # 预留；本阶段不用

class ProviderLike(Protocol):
    """resolve 只需要 id + auth，不依赖完整 Provider。"""
    id: str
    auth: ProviderAuth


@dataclass
class AuthResolutionOverrides:
    """单次请求覆盖。对应 TS AuthResolutionOverrides。"""
    api_key: str | None = None
    env: ProviderEnv | None = None


class ModelsError(Exception):
    """对应 TS ModelsError（auth 相关子集）。"""

    def __init__(self, code: str, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.__cause__ = cause


# ---------------------------------------------------------------------------
# env_api_key_auth + 环境变量映射
# ---------------------------------------------------------------------------

# MVP：常用 provider → 候选环境变量（摘自 env-api-keys.ts）
# anthropic：OAuth token 优先于 API key（与 TS 一致）
_ENV_API_KEY_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
}


def get_api_key_env_vars(provider_id: str) -> tuple[str, ...] | None:
    """查 provider 默认环境变量名。对应 env-api-keys.ts getApiKeyEnvVars。"""
    return _ENV_API_KEY_VARS.get(provider_id)


@dataclass
class _EnvApiKeyAuth:
    """envApiKeyAuth 的具体实现：stored key → 第一个非空 env var。"""

    name: str
    env_vars: tuple[str, ...]

    async def resolve(
        self,
        *,
        model: Model,
        ctx: AuthContext,
        credential: ApiKeyCredential | None = None,
    ) -> AuthResult | None:
        del model  # 标准 api-key 解析不依赖 model；签名对齐 TS
        if credential is not None and credential.key:
            return AuthResult(
                auth=ModelAuth(api_key=credential.key),
                source="stored credential",
            )
        for env_var in self.env_vars:
            value = await ctx.env(env_var)
            if value:
                return AuthResult(auth=ModelAuth(api_key=value), source=env_var)
        return None


def env_api_key_auth(name: str, env_vars: Sequence[str]) -> ApiKeyAuth:
    """标准 API Key auth helper。对应 TS envApiKeyAuth。

    解析顺序：credential.key → env_vars 中第一个已设置的变量 → None。
    """
    return _EnvApiKeyAuth(name=name, env_vars=tuple(env_vars))


# ---------------------------------------------------------------------------
# resolve_provider_auth
# ---------------------------------------------------------------------------


def _overlay_env_auth_context(base: AuthContext, env: ProviderEnv) -> AuthContext:
    """请求级 env 覆盖优先于 ambient。对应 TS overlayEnvAuthContext。"""

    class _Overlay:
        async def env(self, name: str) -> str | None:
            if name in env and env[name]:
                return env[name]
            return await base.env(name)

        async def file_exists(self, path: str) -> bool:
            return await base.file_exists(path)

    return _Overlay()


async def _read_credential(credentials: CredentialStore, provider_id: str) -> Credential | None:
    try:
        return await credentials.read(provider_id)
    except Exception as error:
        raise ModelsError("auth", f"Credential store read failed for {provider_id}", cause=error) from error


async def _resolve_api_key(
    auth_context: AuthContext,
    api_key: ApiKeyAuth,
    model: Model,
    credential: ApiKeyCredential | None,
) -> AuthResult | None:
    try:
        return await api_key.resolve(model=model, ctx=auth_context, credential=credential)
    except Exception as error:
        raise ModelsError(
            "auth",
            f"API key auth failed for provider {model.provider}",
            cause=error,
        ) from error


async def resolve_provider_auth(
    provider: ProviderLike,
    model: Model,
    credentials: CredentialStore,
    auth_context: AuthContext,
    overrides: AuthResolutionOverrides | None = None,
) -> AuthResult | None:
    """解析某次请求的 auth。对应 TS resolveProviderAuth。

    优先级：
    1. overrides.api_key（合成 stored credential）
    2. store 中已有凭据（api_key → resolve；oauth → MVP 返回 None）
    3. 无存档 → ambient（env）resolve
    """
    overrides = overrides or AuthResolutionOverrides()
    request_ctx: AuthContext = (
        _overlay_env_auth_context(auth_context, overrides.env)
        if overrides.env is not None
        else auth_context
    )

    # 1) 显式覆盖
    if overrides.api_key is not None and provider.auth.api_key is not None:
        return await _resolve_api_key(
            request_ctx,
            provider.auth.api_key,
            model,
            ApiKeyCredential(key=overrides.api_key, env=overrides.env),
        )

    # 2) 已存储
    stored = await _read_credential(credentials, provider.id)
    if stored is not None:
        if stored.type == "oauth":
            # 暂不实现 OAuth；有 oauth 存档但无 handler 时与 TS「类型不匹配」一样返回 None
            if provider.auth.oauth is not None:
                raise ModelsError(
                    "oauth",
                    f"OAuth is not implemented yet for provider {provider.id}",
                )
            return None
        if stored.type == "api_key" and provider.auth.api_key is not None:
            credential = stored
            if overrides.env is not None:
                merged_env = {**(stored.env or {}), **overrides.env}
                credential = ApiKeyCredential(key=stored.key, env=merged_env)
            return await _resolve_api_key(
                request_ctx, provider.auth.api_key, model, credential
            )
        return None

    # 3) ambient
    if provider.auth.api_key is not None:
        return await _resolve_api_key(request_ctx, provider.auth.api_key, model, None)
    return None


# ---------------------------------------------------------------------------
# 直接运行：python -m pi_ai.auth
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from dataclasses import dataclass as _dataclass

    from pi_ai.types import ModelCost

    def _ok(name: str) -> None:
        print(f"  PASS  {name}")

    @_dataclass
    class _P:
        id: str
        auth: ProviderAuth

    async def _run() -> None:
        print("pi_ai.auth 自检")
        vars_ = get_api_key_env_vars("anthropic")
        assert vars_ == ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
        provider = _P(id="anthropic", auth=ProviderAuth(api_key=env_api_key_auth("Anthropic", vars_)))
        model = Model(
            id="m",
            name="m",
            api="anthropic-messages",
            provider="anthropic",
            base_url="https://api.anthropic.com",
            context_window=1,
            max_tokens=1,
            cost=ModelCost(0, 0, 0, 0),
        )
        store = InMemoryCredentialStore()

        # 无 env → None
        r = await resolve_provider_auth(
            provider, model, store, default_provider_auth_context({})
        )
        assert r is None
        _ok("未设置环境变量 → None")

        # 有 ANTHROPIC_API_KEY
        r = await resolve_provider_auth(
            provider,
            model,
            store,
            default_provider_auth_context({"ANTHROPIC_API_KEY": "sk-demo"}),
        )
        assert r is not None and r.auth.api_key == "sk-demo"
        assert r.source == "ANTHROPIC_API_KEY"
        _ok("ANTHROPIC_API_KEY → AuthResult")

        # stored 优先于 env
        async def _set(_c: Credential | None) -> Credential | None:
            return ApiKeyCredential(key="sk-stored")

        await store.modify("anthropic", _set)
        r = await resolve_provider_auth(
            provider,
            model,
            store,
            default_provider_auth_context({"ANTHROPIC_API_KEY": "sk-env"}),
        )
        assert r is not None and r.auth.api_key == "sk-stored"
        _ok("stored credential 优先于 env")

        print("全部通过")

    asyncio.run(_run())
