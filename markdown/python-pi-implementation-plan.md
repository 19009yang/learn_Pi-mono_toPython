# Python 版 Pi 核心功能实现方案

> 基于 pi 项目（TypeScript）三个核心包的源码分析，规划 Python 版的核心功能实现。

---

## 整体架构（三层结构）

```
┌─────────────────────────────────────────────┐
│              pi_ai (统一 LLM API)             │  ← 第一层：LLM 通信
├─────────────────────────────────────────────┤
│              pi_agent (Agent 运行时)          │  ← 第二层：Agent 循环引擎
├─────────────────────────────────────────────┤
│         pi_coding_agent (交互式 CLI)         │  ← 第三层：用户界面 + 工具
└─────────────────────────────────────────────┘
```

参考 TS 版架构层次：

```
┌─────────────────────────────────────────────────┐
│              应用层 (Application)                 │
│  使用 AgentHarness 或 Agent 进行交互             │
├─────────────────────────────────────────────────┤
│          AgentHarness (生产级封装)                │
│  ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │ Session  │ │  Skills  │ │ PromptTemplates│  │
│  │  (会话)  │ │  (技能)  │ │  (提示模板)    │  │
│  └──────────┘ └──────────┘ ┌────────────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │Compaction│ │BranchSum │ │   Hooks系统     │  │
│  │ (压缩)   │ │ (分支摘要)│ │ (before/after) │  │
│  └──────────┘ └──────────┘ └────────────────┘  │
│  ┌──────────┐ ┌──────────┐                      │
│  │ExecEnv   │ │ Messages │                      │
│  │(执行环境)│ │ (消息转换)│                      │
│  └──────────┘ └──────────┘                      │
├─────────────────────────────────────────────────┤
│             Agent 类 (有状态封装)                 │
│  持有 state、事件监听、steering/followUp 队列    │
│  提供 barrier-before-tool-preflight 语义         │
├─────────────────────────────────────────────────┤
│          agentLoop (低层循环引擎)                 │
│  LLM 流式响应 → 工具调用 → 工具执行               │
│  → steering → followUp → 循环                    │
├─────────────────────────────────────────────────┤
│          @earendil-works/pi-ai (LLM 抽象层)      │
│  Model、Context、streamSimple、EventStream 等     │
└─────────────────────────────────────────────────┘
```

---

## 第一层：pi_ai — 统一多 Provider LLM API

这是最底层，负责与各 LLM 服务通信。

### 1. 核心类型定义 (`types.py`)

```python
# ========== 枚举 ==========

KnownApi = Literal[
    "openai-completions",
    "openai-responses",
    "anthropic-messages",
    "google-generative-ai",
    "google-vertex",
    "bedrock-converse-stream",
    "mistral-conversations",
    "azure-openai-responses",
    "openai-codex-responses",
]

StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]

ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh"]
ModelThinkingLevel = Literal["off"] | ThinkingLevel

# ========== 内容类型 ==========

@dataclass
class TextContent:
    type: Literal["text"]
    text: str

@dataclass
class ThinkingContent:
    type: Literal["thinking"]
    thinking: str
    thinking_signature: str | None = None
    redacted: bool = False

@dataclass
class ImageContent:
    type: Literal["image"]
    data: str  # base64 encoded
    mime_type: str  # "image/jpeg", "image/png"

@dataclass
class ToolCall:
    type: Literal["toolCall"]
    id: str
    name: str
    arguments: dict[str, Any]

# ========== 消息类型 ==========

@dataclass
class UserMessage:
    role: Literal["user"]
    content: str | list[TextContent | ImageContent]
    timestamp: int  # Unix ms

@dataclass
class AssistantMessage:
    role: Literal["assistant"]
    content: list[TextContent | ThinkingContent | ToolCall]
    api: str
    provider: str
    model: str
    response_model: str | None = None
    usage: Usage
    stop_reason: StopReason
    error_message: str | None = None
    timestamp: int

@dataclass
class ToolResultMessage:
    role: Literal["toolResult"]
    tool_call_id: str
    tool_name: str
    content: list[TextContent | ImageContent]
    details: Any | None = None
    is_error: bool = False
    timestamp: int

Message = UserMessage | AssistantMessage | ToolResultMessage

# ========== Token 使用量 ==========

@dataclass
class Usage:
    input: int
    output: int
    cache_read: int
    cache_write: int
    reasoning: int | None = None
    total_tokens: int
    cost: CostInfo

@dataclass
class CostInfo:
    input: float
    output: float
    cache_read: float
    cache_write: float
    total: float

# ========== 请求上下文 ==========

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema (Pydantic 生成)

@dataclass
class Context:
    system_prompt: str | None = None
    messages: list[Message]
    tools: list[Tool] | None = None

# ========== 流事件类型 ==========

AssistantMessageEvent = (
    StartEvent       | TextStartEvent   | TextDeltaEvent    | TextEndEvent
  | ThinkingStartEvent | ThinkingDeltaEvent | ThinkingEndEvent
  | ToolCallStartEvent | ToolCallDeltaEvent | ToolCallEndEvent
  | DoneEvent        | ErrorEvent
)

@dataclass
class StartEvent:
    type: Literal["start"]
    partial: AssistantMessage

@dataclass
class TextDeltaEvent:
    type: Literal["text_delta"]
    content_index: int
    delta: str
    partial: AssistantMessage

@dataclass
class ToolCallEndEvent:
    type: Literal["toolcall_end"]
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage

@dataclass
class DoneEvent:
    type: Literal["done"]
    reason: Literal["stop", "length", "toolUse"]
    message: AssistantMessage

@dataclass
class ErrorEvent:
    type: Literal["error"]
    reason: Literal["aborted", "error"]
    error: AssistantMessage

# ========== 模型定义 ==========

@dataclass
class Model:
    id: str
    name: str
    api: str
    provider: str
    base_url: str | None = None
    reasoning: bool = False
    thinking_level_map: dict[str, str | None] | None = None
    input_text: bool = True
    input_image: bool = False
    cost: ModelCost
    context_window: int
    max_tokens: int
    compat: dict | None = None  # provider-specific overrides

@dataclass
class ModelCost:
    input_per_m: float
    output_per_m: float
    cache_read_per_m: float
    cache_write_per_m: float

# ========== 流选项 ==========

@dataclass
class StreamOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    signal: asyncio.Event | None = None
    api_key: str | None = None
    cache_retention: str | None = "short"
    session_id: str | None = None
    headers: dict[str, str] | None = None
    timeout_ms: int | None = None
    max_retries: int | None = None
    metadata: dict | None = None

@dataclass
class SimpleStreamOptions(StreamOptions):
    reasoning: ThinkingLevel | None = None
    thinking_budgets: dict | None = None

# ========== Provider 兼容性配置 ==========

@dataclass
class OpenAICompletionsCompat:
    supports_store: bool | None = None
    supports_developer_role: bool | None = None
    supports_reasoning_effort: bool | None = None
    max_tokens_field: str | None = None  # "max_completion_tokens" | "max_tokens"
    requires_tool_result_name: bool | None = None
    thinking_format: str | None = "openai"
    # ... 其他字段按需添加

@dataclass
class AnthropicMessagesCompat:
    supports_eager_tool_input_streaming: bool | None = True
    supports_long_cache_retention: bool | None = True
    send_session_affinity_headers: bool | None = False
```

### 2. EventStream 事件流 (`event_stream.py`)

```python
class EventStream(Generic[TEvent, TResult]):
    """异步推送式事件流，消费者通过 async for 迭代。

    核心行为：
    - push() 同步推送事件，如有等待消费者立即交付，否则入队
    - end() 终止流
    - result() 返回 Promise，在 done/error 事件到达时 resolve

    对应 TS 版的 AssistantMessageEventStream。
    Python 实现用 asyncio.Queue + asyncio.Event 组合。
    """
    _queue: asyncio.Queue[TEvent | None]
    _done: asyncio.Event
    _result: TResult | None

    def push(self, event: TEvent) -> None: ...
    def end(self) -> None: ...
    async def result(self) -> TResult: ...

    def __aiter__(self) -> AsyncIterator[TEvent]: ...
    async def __anext__(self) -> TEvent: ...


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    """在 done/error 事件时自动设置 result"""
```

**关键设计：`lazyStream` 模式**

TS 版中 `Models.stream()` 同步返回 EventStream，但 Auth 解析在内部异步完成。Python 中实现方式：

```python
def stream(self, model, context, options=None) -> AssistantMessageEventStream:
    """同步返回 stream，内部启动异步任务解析 auth 和调用 provider"""
    stream = AssistantMessageEventStream()

    async def _setup():
        # 1. 找到 provider
        provider = self._find_provider(model)
        # 2. 解析 auth（可能需要 OAuth refresh）
        auth_result = await resolve_provider_auth(provider, self._credentials)
        # 3. 合合 apiKey/headers/env 到 options
        merged_options = merge_auth_into_options(options, auth_result)
        # 4. 委托给 provider.stream()
        provider_stream = provider.stream(model, context, merged_options)
        # 5. 将 provider_stream 事件转发到我们的 stream
        for event in provider_stream:
            self.push(event)

    asyncio.create_task(_setup())
    return stream  # 同步返回，异步填充
```

### 3. Model 定义与 Provider 注册 (`models.py`)

```python
class Provider:
    """Provider 是具体运行单元，拥有 id/name、auth、model 列表和 stream 行为"""
    id: str
    name: str
    base_url: str | None
    auth: ProviderAuth

    def get_models(self) -> list[Model]: ...
    async def refresh_models(self) -> None: ...  # 动态 provider
    def stream(self, model, context, options) -> AssistantMessageEventStream: ...
    def stream_simple(self, model, context, options) -> AssistantMessageEventStream: ...


class Models:
    """Provider 注册表 + Model 查找 + Auth 解析"""
    _providers: dict[str, Provider]
    _credentials: CredentialStore

    def get_providers(self) -> list[Provider]: ...
    def get_models(self, provider=None) -> list[Model]: ...
    def get_model(self, provider, id) -> Model | None: ...
    async def refresh(self, provider=None) -> None: ...
    async def get_auth(self, model) -> AuthResult | None: ...

    def stream(self, model, context, options) -> AssistantMessageEventStream: ...
    async def complete(self, model, context, options) -> AssistantMessage: ...
    def stream_simple(self, model, context, options) -> AssistantMessageEventStream: ...
    async def complete_simple(self, model, context, options) -> AssistantMessage: ...


class MutableModels(Models):
    def set_provider(self, provider: Provider) -> None: ...
    def delete_provider(self, id: str) -> None: ...
    def clear_providers(self) -> None: ...


def create_provider(id, name, auth, models, api, base_url=None, headers=None) -> Provider:
    """从部件构建 Provider。支持单 API 或 per-API dispatch。
    自动去重并发 refreshModels() 调用。"""


def create_models(credentials=None, auth_context=None) -> MutableModels:
    """创建 Models 实例，默认使用 InMemoryCredentialStore"""


def calculate_cost(usage: Usage, model: Model) -> CostInfo:
    """从 Usage token 数量 + Model.cost 计算实际费用"""


def get_supported_thinking_levels(model: Model) -> list[ThinkingLevel]:
    """从 thinking_level_map 推导支持的 thinking 级别"""


def clamp_thinking_level(requested: ThinkingLevel, model: Model) -> ThinkingLevel | None:
    """将请求的级别 clamp 到模型支持的最近级别"""
```

### 4. Auth 认证 (`auth.py`)

```python
class CredentialStore:
    """凭据存储抽象"""
    def read(self, provider_id: str) -> str | None: ...
    def modify(self, provider_id: str, fn: Callable) -> str | None: ...
    def delete(self, provider_id: str) -> None: ...

class InMemoryCredentialStore(CredentialStore):
    """纯内存实现，用于测试和最简场景"""
    _store: dict[str, str]

class ProviderAuth:
    """至少包含 apiKey 或 oauth"""
    api_key: ApiKeyAuth | None
    oauth: OAuthAuth | None

class ApiKeyAuth:
    """API Key 认证：存储 key 优先 → 环境变量"""
    name: str
    env_vars: list[str]  # 如 ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"]

    def resolve(self, stored_key=None) -> AuthResult | None:
        """解析顺序：stored_key → env_vars[0] → env_vars[1] → ... → None"""

class AuthResult:
    auth: ModelAuth  # { api_key, headers, base_url }
    env: dict | None
    source: str  # 描述来源（"env var", "stored", "oauth" 等）

async def resolve_provider_auth(provider, store) -> AuthResult | None:
    """完整 Auth 解析流程：
    1. 如有请求覆盖 apiKey → 用 synthetic credential
    2. 从 store 读 stored credential
    3. stored OAuth → 检查过期 → refresh → toAuth()
    4. stored API key → resolve()
    5. 无 stored → 尝试环境变量 resolve()
    6. 全失败 → 返回 None
    """

def env_api_key_auth(name: str, env_vars: list[str]) -> ApiKeyAuth:
    """标准 Auth helper：stored key 优先，然后第一个已设置的 env var"""
```

### 5. Provider API 实现 (`providers/`)

#### Anthropic (`providers/anthropic.py`)

```python
def stream(model: Model, context: Context, options: StreamOptions) -> AssistantMessageEventStream:
    """Anthropic Messages API 流式实现

    流程：
    1. 创建 AssistantMessageEventStream
    2. 创建 anthropic.Anthropic client (api_key, base_url, timeout)
    3. 构建 request params (system, messages, tools, max_tokens, thinking)
    4. 处理 thinking 配置：
       - budget_tokens (按 ThinkingLevel 映射)
       - type: "enabled" | "disabled"
    5. 调用 client.messages.create(stream=True)
    6. 解析 SSE 事件 → push 到 EventStream：
       - message_start → start event
       - content_block_start/delta/stop → text/thinking/toolcall events
       - message_delta → stop_reason + usage
    7. push done 或 error event
    """

def stream_simple(model, context, options: SimpleStreamOptions) -> AssistantMessageEventStream:
    """从 SimpleStreamOptions 构建 StreamOptions，然后委托给 stream()"""
```

**关键特性**：
- 支持 thinking（budget_tokens 模式）
- 支持 tool_use（tool streaming beta）
- 支持 cache_control（prompt cache）
- SSE 事件解析（Python SDK 已内置，比 TS 版简单）
- OAuth/Copilot 身份头

#### OpenAI Completions (`providers/openai_completions.py`)

```python
def stream(model, context, options) -> AssistantMessageEventStream:
    """OpenAI Chat Completions API 流式实现

    流程：
    1. 创建 EventStream
    2. 创建 openai.OpenAI client
    3. 根据 compat 配置构建 params：
       - system/developer role 选择
       - max_completion_tokens vs max_tokens 字段选择
       - thinking format (openai, openrouter, deepseek, qwen, etc.)
       - store 字段支持
    4. 处理 thinking 配置（按 thinking_format 适配不同格式）
    5. 调用 client.chat.completions.create(stream=True, stream_options={"include_usage": True})
    6. 解析 SSE chunks → push events
    7. push done/error
    """
```

**关键特性**：
- compat 系统是最复杂部分：根据 base_url 和 provider 自动检测兼容性
- ~10 种 thinking 格式支持
- developer vs system role 选择
- tool_choice / parallel_tool_calls 控制

**初期简化**：只实现 `openai` 和 `deepseek` 两种 thinking_format

#### OpenAI Responses (`providers/openai_responses.py`)

```python
def stream(model, context, options) -> AssistantMessageEventStream:
    """OpenAI Responses API 流式实现

    使用 client.responses.create(stream=True)
    事件格式不同于 Completions：
    - response.created → start
    - response.output_item.added/done → text/toolcall events
    - response.completed → done
    """
```

#### Google (`providers/google.py`)

```python
def stream(model, context, options) -> AssistantMessageEventStream:
    """Google Generative AI (Gemini) 流式实现

    使用 google.genai.Client().aio.models.generate_content_stream()
    转换 pi messages/tools → Google 格式
    处理 thought parts (thinking)
    """
```

### 6. 消息转换 (`transform_messages.py`)

```python
def transform_messages(
    messages: list[Message],
    model: Model,
    compat: dict | None = None,
) -> list[Message]:
    """发送给 LLM 之前的消息规范化：

    1. 降级图片：非视觉模型时将 ImageContent 转为文字描述
    2. 处理孤立 toolCall：如果 assistant 消息有 toolCall 但后续无对应 toolResult，
       插入合成的 error toolResult
    3. 处理 thinking blocks：跨模型切换时，旧 thinking 转为 text 或移除
    4. 处理 tool call ID 规范化：某些 provider 需要特定格式
    5. 插入 assistant 后置消息：某些 provider 要求 toolResult 后必须有 assistant 消息
    """
```

### 7. Thinking/Token 配置 (`simple_options.py`)

```python
def build_base_options(simple_options: SimpleStreamOptions) -> StreamOptions:
    """将 SimpleStreamOptions 映射为 StreamOptions"""

def adjust_max_tokens_for_thinking(
    max_tokens: int,
    thinking_level: ThinkingLevel,
    thinking_budgets: dict | None,
) -> int:
    """为 thinking 调整 max_tokens：thinking 占用 output budget"""

def clamp_max_tokens_to_context(max_tokens: int, context_window: int, messages: list[Message]) -> int:
    """确保 max_tokens + input tokens 不超过 context_window"""
```

---

## 第二层：pi_agent — Agent 运行时

这是核心循环引擎，整个系统的灵魂。

### 1. Agent 类型定义 (`types.py`)

```python
# ========== 工具定义 ==========

@dataclass
class AgentToolResult:
    content: list[TextContent | ImageContent]
    details: Any | None = None
    terminate: bool = False  # 是否终止循环

class AgentTool:
    """Agent 工具定义，包含 schema、执行逻辑和执行模式"""
    name: str
    description: str
    parameters_schema: dict  # JSON Schema
    execution_mode: Literal["sequential", "parallel"] = "parallel"

    async def prepare_arguments(self, args: dict) -> dict: ...
    async def execute(self, args: dict) -> AgentToolResult: ...

# ========== Agent 状态 ==========

@dataclass
class AgentState:
    system_prompt: str | None
    model: Model
    thinking_level: ModelThinkingLevel = "off"
    tools: list[AgentTool]
    messages: list[AgentMessage]  # AgentMessage = Message | CustomMessage
    is_streaming: bool = False

# ========== Agent 事件 ==========

AgentEvent = (
    AgentStartEvent | AgentEndEvent
  | TurnStartEvent  | TurnEndEvent
  | MessageStartEvent | MessageUpdateEvent | MessageEndEvent
  | ToolExecutionStartEvent | ToolExecutionUpdateEvent | ToolExecutionEndEvent
)

# ========== 循环配置 ==========

@dataclass
class AgentLoopConfig:
    model: Model
    stream_fn: StreamFn  # 即 Models.stream_simple
    convert_to_llm: Callable[[list[AgentMessage]], list[Message]]
    before_tool_call: Callable[[AgentToolCall, AgentState], BeforeToolCallResult | None]
    after_tool_call: Callable[[AgentToolCall, AgentToolResult, AgentState], AfterToolCallResult | None]
    should_stop_after_turn: Callable[[ShouldStopAfterTurnContext], bool]
    prepare_next_turn: Callable[[AgentState], AgentState | None]
    steering_queue_mode: Literal["all", "one-at-a-time"] = "one-at-a-time"
    follow_up_queue_mode: Literal["all", "one-at-a-time"] = "all"

@dataclass
class BeforeToolCallResult:
    block: bool = False
    reason: str | None = None

@dataclass
class AfterToolCallResult:
    content: list[TextContent | ImageContent] | None = None
    details: Any | None = None
    is_error: bool | None = None
    terminate: bool | None = None
```

### 2. Agent 循环引擎 (`agent_loop.py`) — 最核心的代码

```python
async def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """启动新循环，返回 EventStream

    双层 while 循环流程：

    外层循环 (while True):
      内层循环 (while has_more_tool_calls or pending_messages):
        1. 排出 pending_messages（steering）
        2. stream_assistant_response() → 获得 AssistantMessage
        3. 检查 stopReason（error/aborted → 退出）
        4. 提取 toolCalls
        5. 执行工具（并行或顺序）
        6. 发射 turn_end
        7. prepareNextTurn → 更新 context/model/thinkingLevel
        8. shouldStopAfterTurn → 优雅退出
        9. 排出 steering 消息 → 下次内层循环
      检查 followUp 消息 → 如有则设置 pending → 重新进入外层循环
      否则 break → agent_end
    """


async def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """从当前上下文继续（不发射 prompt 事件）"""


# ========== 内部核心函数 ==========

async def stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    stream: EventStream,
) -> AssistantMessage | None:
    """调用 streamFn 流式获取 LLM 响应

    流程：
    1. transform_context() → 可修改 context
    2. convert_to_llm() → 将 AgentMessage 转为 LLM Message
    3. 调用 stream_fn(model, llm_context, options)
    4. 迭代 EventStream，发射 message_start/update/end 事件
    5. 返回完整 AssistantMessage
    """


async def execute_tool_calls(
    tool_calls: list[AgentToolCall],
    state: AgentState,
    config: AgentLoopConfig,
    stream: EventStream,
) -> list[AgentToolResult]:
    """根据执行模式选择并行或顺序执行工具

    并行执行：
    - preflight 顺序执行（prepare + beforeToolCall）
    - 具体执行并行（execute）
    - tool_execution_end 按完成序发射
    - toolResult 消息按源序排列

    顺序执行：
    - 逐个 prepare → beforeToolCall → execute → afterToolCall
    """


async def prepare_tool_call(
    tool_call: AgentToolCall,
    state: AgentState,
    config: AgentLoopConfig,
) -> PreparedToolCall | ImmediateToolCallOutcome:
    """工具查找 → 参数准备 → 验证 → beforeToolCall 钩子"""
```

### 3. Agent 类 (`agent.py`) — 有状态封装

```python
class Agent:
    """AgentLoop 的有状态封装，持有对话状态和消息队列

    提供 barrier-before-tool-preflight 语义：
    beforeToolCall 钩子看到的 state 已包含请求该工具调用的 assistant 消息
    """
    _state: MutableAgentState
    _listeners: list[Callable[[AgentEvent], None]]
    _steering_queue: PendingMessageQueue
    _follow_up_queue: PendingMessageQueue
    _active_run: ActiveRun | None  # Promise + AbortController

    # ========== 核心方法 ==========

    async def prompt(self, input: str | AgentMessage | list[AgentMessage]) -> None:
        """提交新提示，启动完整循环

        流程：normalize_prompt_input() → run_prompt_messages() → run_with_lifecycle()
        """

    async def continue_loop(self) -> None:
        """从当前上下文继续（retry 场景）"""

    def subscribe(self, listener: Callable[[AgentEvent], None]) -> Callable[[], None]:
        """注册事件监听器，返回取消函数"""

    def abort(self) -> None:
        """中止当前运行"""

    async def wait_for_idle(self) -> None:
        """等待运行完成"""

    def reset(self) -> None:
        """清空所有状态和队列"""

    # ========== 消息队列 ==========

    def steer(self, message: AgentMessage) -> None:
        """注入 steering 消息（运行中打断）"""

    def follow_up(self, message: AgentMessage) -> None:
        """注入 follow-up 消息（运行后追加）"""

    def clear_steering_queue(self) -> None: ...
    def clear_follow_up_queue(self) -> None: ...
    def clear_all_queues(self) -> None: ...

    # ========== 内部 ==========

    def _process_events(self, events: list[AgentEvent]) -> None:
        """处理循环事件，更新内部状态，触发监听器"""

    async def _run_with_lifecycle(self, run_fn) -> None:
        """包装运行函数，处理 start/end 生命周期"""

    @property
    def state(self) -> AgentState:
        """返回当前 AgentState 快照"""
```

### 4. 自定义消息 + convertToLlm (`messages.py`)

```python
# ========== 自定义消息类型 ==========

@dataclass
class BashExecutionMessage:
    """Shell 命令执行记录"""
    role: Literal["bashExecution"]
    command: str
    output: str
    exit_code: int
    cancelled: bool = False
    truncated: bool = False

@dataclass
class CompactionSummaryMessage:
    """压缩摘要"""
    role: Literal["compactionSummary"]
    summary: str
    tokens_before: int

@dataclass
class BranchSummaryMessage:
    """分支摘要"""
    role: Literal["branchSummary"]
    summary: str
    from_id: str

@dataclass
class CustomMessage:
    """应用自定义消息"""
    role: Literal["custom"]
    custom_type: str
    content: Any
    display: str | None = None
    details: Any | None = None
    exclude_from_context: bool = False

AgentMessage = Message | BashExecutionMessage | CompactionSummaryMessage | BranchSummaryMessage | CustomMessage

# ========== 消息转换 ==========

def convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    """将 AgentMessage 转为 LLM 可理解的 Message

    转换规则：
    - user/assistant/toolResult → 直接透传
    - bashExecution → 转为 user 消息（文本格式化：命令+输出+状态）
      exclude_from_context=True 的跳过
    - custom → 转为 user 消息
    - branchSummary → 转为 user 消息（加 <branch_summary> XML 标签）
    - compactionSummary → 转为 user 消息（加 <compaction_summary> XML 标签）
    """

def bash_execution_to_text(msg: BashExecutionMessage) -> str:
    """格式化 Shell 执行为文本：
    <bash_execution>
    command: {command}
    exit_code: {exit_code}
    output:
    {output}
    </bash_execution>
    """

def create_branch_summary_message(summary: str, from_id: str) -> BranchSummaryMessage: ...
def create_compaction_summary_message(summary: str, tokens_before: int) -> CompactionSummaryMessage: ...
def create_custom_message(custom_type: str, content: Any, display=None, details=None) -> CustomMessage: ...
```

### 5. Session 会话系统 (`session/`)

```python
# ========== 存储抽象 ==========

class SessionStorage:
    """会话存储接口"""
    def get_metadata(self) -> SessionMetadata: ...
    def get_leaf_id(self) -> str: ...
    def set_leaf_id(self, id: str) -> None: ...
    def append_entry(self, entry: SessionTreeEntry) -> None: ...
    def get_path_to_root(self, leaf_id: str) -> list[SessionTreeEntry]: ...
    def get_all_entries(self) -> list[SessionTreeEntry]: ...

class InMemorySessionStorage(SessionStorage):
    """纯内存实现，用于测试和最简场景"""
    _entries: list[SessionTreeEntry]
    _entry_map: dict[str, SessionTreeEntry]
    _metadata: SessionMetadata
    _leaf_id: str

class JsonlSessionStorage(SessionStorage):
    """JSONL 文件存储，支持持久化

    文件格式（每行一条 JSON）：
    {"type":"session","version":3,"id":"...","timestamp":"...","cwd":"..."}
    {"type":"message","id":"abc","parentId":"xyz","timestamp":"...","message":{...}}
    {"type":"leaf","id":"def","parentId":"abc","timestamp":"...","targetId":"xyz"}

    特性：append-only 写入，打开时全量加载到内存
    """
    @staticmethod
    def open(fs, file_path) -> JsonlSessionStorage: ...
    @staticmethod
    def create(fs, file_path, options) -> JsonlSessionStorage: ...

class SessionRepo:
    """会话仓库：CRUD 生命周期管理"""
    def create(self, options) -> Session: ...
    def open(self, metadata) -> Session: ...
    def list(self, options) -> list[SessionMetadata]: ...
    def delete(self, metadata) -> None: ...
    def fork(self, source_metadata, options) -> Session: ...

class InMemorySessionRepo(SessionRepo): ...
class JsonlSessionRepo(SessionRepo): ...

# ========== 会话树条目 ==========

SessionTreeEntry = (
    MessageEntry        | CompactionEntry
  | BranchSummaryEntry  | ModelChangeEntry
  | ThinkingLevelChangeEntry | ActiveToolsChangeEntry
  | CustomEntry         | CustomMessageEntry
  | LabelEntry          | SessionInfoEntry
  | LeafEntry
)

# ========== Session 类 ==========

class Session:
    """会话树操作类，管理条目追加、分支导航和上下文重建"""
    _storage: SessionStorage

    def get_branch(self, from_id=None) -> list[SessionTreeEntry]:
        """获取从 leaf 到 root 的路径"""

    def build_context(self, options) -> SessionContext:
        """从会话树重建对话上下文

        流程：
        1. get_branch() → leaf→root 路径
        2. build_context_entries() → 应用 transforms
        3. session_entry_to_context_messages() → 每个条目转为 AgentMessage
        4. derive_session_context_state() → 推导 thinkingLevel、model、activeToolNames
        """

    def append_message(self, msg: AgentMessage) -> None: ...
    def append_model_change(self, provider: str, model_id: str) -> None: ...
    def append_thinking_level_change(self, level: str) -> None: ...
    def move_to(self, entry_id: str, summary=None) -> None:
        """切换 leaf 位置（导航到不同分支，类似 Git checkout）"""
```

### 6. 上下文压缩 (`compaction.py`) — P2 优先级

```python
@dataclass
class CompactionSettings:
    enabled: bool = True
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000

def should_compact(context_tokens: int, context_window: int, settings: CompactionSettings) -> bool:
    """判断是否需要压缩：context_tokens + reserve_tokens > context_window"""

def prepare_compaction(path_entries, settings) -> CompactionPreparation:
    """准备压缩数据：找切割点、计算保留范围"""

async def generate_summary(messages, models, model, ...) -> str:
    """生成/更新压缩摘要

    首次：使用 SUMMARIZATION_PROMPT (Goal/Constraints/Progress/Decisions/Next Steps/Critical Context)
    更新：使用 UPDATE_SUMMARIZATION_PROMPT (保留已有信息 + 新增)
    """

async def compact(preparation, models, model, ...) -> CompactionResult:
    """执行压缩：将切割点前的内容替换为摘要"""
```

---

## 第三层：pi_coding_agent — 交互式编码 Agent

面向用户的 CLI 层，让系统可用。

### 1. 内置工具 (`tools/`) — 最关键的部分

#### BashTool (`tools/bash.py`)

```python
class BashTool(AgentTool):
    """执行 Shell 命令，捕获输出，支持 timeout 和 abort

    参数 schema：
    {
      "command": str,        # 要执行的命令
      "timeout": int | None, # 超时毫秒数（默认 120000）
      "description": str,    # 命令的简短描述
      "run_in_background": bool  # 是否后台运行
    }

    执行逻辑：
    - 使用 asyncio.create_subprocess_shell 执行
    - 查找 bash（Windows: Git Bash → PATH；其他: /bin/bash）
    - 实时 stdout/stderr 回调
    - 支持 abort signal
    - 输出二进制清洗（移除控制字符）
    - 超过 50KB 时截断，保留尾部
    """
```

#### ReadTool (`tools/read.py`)

```python
class ReadTool(AgentTool):
    """读取文件内容，支持行号偏移和限制

    参数 schema：
    {
      "file_path": str,      # 绝对路径
      "offset": int | None,  # 起始行号
      "limit": int | None,   # 最大行数（默认 2000）
    }

    特性：
    - cat -n 格式输出（带行号）
    - 支持图片文件（PNG/JPG → 视觉展示）
    - 支持 PDF 文件（分页读取）
    - 支持 Jupyter notebook（.ipynb 按单元格解析）
    """
```

#### WriteTool (`tools/write.py`)

```python
class WriteTool(AgentTool):
    """写入文件（创建或完全替换）

    参数 schema：
    {
      "file_path": str,  # 绝对路径
      "content": str,    # 要写入的内容
    }

    特性：
    - 自动创建父目录
    - 需先 Read 过的文件才能 Write（防止意外覆盖）
    """
```

#### EditTool (`tools/edit.py`)

```python
class EditTool(AgentTool):
    """精确字符串替换编辑

    参数 schema：
    {
      "file_path": str,       # 绝对路径
      "old_string": str,      # 要替换的文本（必须精确匹配，包括缩进）
      "new_string": str,      # 替换后的文本
      "replace_all": bool,    # 是否替换所有出现（默认 false）
    }

    特性：
    - old_string 必须在文件中唯一匹配（否则失败）
    - 需先 Read 过的文件才能 Edit
    - replace_all=true 替换所有出现
    """
```

#### GrepTool (`tools/grep.py`)

```python
class GrepTool(AgentTool):
    """内容搜索（基于正则，类似 ripgrep）

    参数 schema：
    {
      "pattern": str,          # 正则表达式
      "path": str | None,      # 搜索目录
      "glob": str | None,      # 文件模式过滤（如 "*.py"）
      "output_mode": str,      # "content" | "files_with_matches" | "count"
      "-i": bool,              # 忽略大小写
      "-n": bool,              # 显示行号
      "head_limit": int | None, # 限制输出条目数
      "context": int | None,   # 上下文行数
      "multiline": bool,       # 多行模式
    }

    特性：
    - Python 中可用 subprocess 调用 rg，或用 re 模块实现
    - 结果带文件链接（文件:行号 格式）
    """
```

#### GlobTool (`tools/glob.py`)

```python
class GlobTool(AgentTool):
    """文件模式匹配搜索

    参数 schema：
    {
      "pattern": str,          # glob 模式（如 "**/*.py"）
      "path": str | None,      # 搜索目录
    }

    特性：
    - 支持 ** 递归匹配
    - 按修改时间排序
    - Python 中可用 pathlib.Path.glob 实现
    """
```

### 2. 系统提示 (`system_prompt.py`)

```python
def build_system_prompt(
    skills: list[Skill],
    cwd: str,
    tools: list[AgentTool],
    project_context: str | None = None,
) -> str:
    """构建 Agent 的系统提示

    内容：
    - 身份说明（你是 pi，一个编码助手）
    - 工具使用规则（何时用哪个工具、注意事项）
    - Skill 列表（XML 格式，符合 agentskills.io 规范）
    - 项目上下文（目录结构、技术栈等）

    Skill XML 格式：
    <available_skills>
      <skill>
        <name>skill-name</name>
        <description>Short description</description>
        <location>/path/to/SKILL.md</location>
      </skill>
    </available_skills>
    """
```

### 3. Skill 加载 (`skills.py`)

```python
@dataclass
class Skill:
    name: str          # 小写字母+数字+连字符，≤64字符
    description: str   # ≤1024字符，必填
    content: str       # Skill 文件的完整内容
    file_path: str     # SKILL.md 的路径
    disable_model_invocation: bool = False

def load_skills(env: ExecutionEnv, dirs: list[str]) -> tuple[list[Skill], list[str]]:
    """从目录列表递归加载 SKILL.md 文件

    加载逻辑：
    1. 递归遍历目录
    2. 优先查找 SKILL.md 文件
    3. 根级目录中的 .md 文件也作为 Skill 加载
    4. 遵守 .gitignore / .ignore 规则
    5. 解析 YAML frontmatter（name, description）
    6. 验证 name 和 description
    7. 无 description 的 Skill 被跳过
    """

def format_skill_invocation(skill: Skill, instructions: str) -> str:
    """格式化 Skill 调用提示"""
```

### 4. 输出截断 (`truncate.py`)

```python
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB

@dataclass
class TruncationResult:
    output: str
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    truncated_by: str | None  # "lines" | "bytes" | None

def truncate_head(content: str, max_lines=DEFAULT_MAX_LINES, max_bytes=DEFAULT_MAX_BYTES) -> TruncationResult:
    """从头部截断（保留前 N 行/字节）"""

def truncate_tail(content: str, max_lines=DEFAULT_MAX_LINES, max_bytes=DEFAULT_MAX_BYTES) -> TruncationResult:
    """从尾部截断（保留末 N 行/字节）"""

def truncate_line(line: str, max_chars=500) -> str:
    """截断单行（加 [truncated] 后缀）"""
```

### 5. CLI 入口 (`cli.py`)

```python
async def main():
    """交互式 REPL 入口

    流程：
    1. 解析命令行参数（--model, --provider, --prompt 等）
    2. 初始化 Models + Provider（加载认证配置）
    3. 初始化 AgentHarness（注册工具、加载 skills）
    4. 进入交互循环：
       - 读取用户输入
       - prompt → 显示响应（文本 + thinking + 工具调用）
       - 支持特殊命令（/help, /compact, /model 等）
    5. Ctrl+C 中止 → abort agent
    """
```

---

## 实现优先级与工作量估算

| 优先级 | 模块 | 文件 | 说明 | 预估代码量 |
|--------|------|------|------|-----------|
| **P0** | `pi_ai/types.py` | 核心类型定义 | 所有数据结构的基础 | ~250行 |
| **P0** | `pi_ai/event_stream.py` | 异步事件流 | LLM 通信的基础设施 | ~100行 |
| **P0** | `pi_ai/auth.py` | API Key 认证 | 连接 LLM 的前提 | ~150行 |
| **P0** | `pi_ai/models.py` | Provider 注册表 | stream_simple 的核心 | ~200行 |
| **P0** | `pi_ai/transform_messages.py` | 消息规范化 | 跨 provider 通信必须 | ~150行 |
| **P0** | `pi_ai/simple_options.py` | Thinking/Token 配置 | thinking 功能必须 | ~80行 |
| **P0** | `pi_ai/providers/anthropic.py` | Anthropic API | 主力 provider | ~250行 |
| **P0** | `pi_ai/providers/openai_completions.py` | OpenAI Completions + compat | 最复杂但最通用 | ~400行 |
| **P0** | `pi_ai/providers/openai_responses.py` | OpenAI Responses | 第二主力 | ~200行 |
| **P0** | `pi_ai/providers/google.py` | Google Gemini | 第三主力 | ~200行 |
| **P0** | `pi_ai/providers/model_catalogs.py` | 模型定义数据 | 3 provider 的模型列表 | ~300行 |
| **P0** | `pi_agent/types.py` | Agent 类型 | Agent 循环的基础 | ~150行 |
| **P0** | `pi_agent/agent_loop.py` | **核心循环引擎** | 最关键，整个系统灵魂 | ~400行 |
| **P0** | `pi_agent/agent.py` | Agent 有状态封装 | 用户交互接口 | ~250行 |
| **P0** | `pi_agent/messages.py` | convertToLlm | Agent→LLM 消息转换 | ~100行 |
| **P1** | `pi_coding_agent/tools/bash.py` | Shell 执行 | 最常用工具 | ~150行 |
| **P1** | `pi_coding_agent/tools/read.py` | 读取文件 | 最常用工具 | ~100行 |
| **P1** | `pi_coding_agent/tools/write.py` | 写入文件 | 基本工具 | ~100行 |
| **P1** | `pi_coding_agent/tools/edit.py` | 编辑文件 | 精确修改工具 | ~150行 |
| **P1** | `pi_coding_agent/tools/grep.py` | 内容搜索 | 代码搜索工具 | ~150行 |
| **P1** | `pi_coding_agent/tools/glob.py` | 文件匹配 | 文件发现工具 | ~80行 |
| **P1** | `pi_coding_agent/system_prompt.py` | 系统提示构建 | Agent 行为定义 | ~100行 |
| **P1** | `pi_coding_agent/skills.py` | Skill 加载 | 扩展能力 | ~100行 |
| **P1** | `pi_coding_agent/truncate.py` | 输出截断 | 工具输出控制 | ~80行 |
| **P1** | `pi_coding_agent/cli.py` | CLI 入口 | 用户交互界面 | ~200行 |
| **P2** | `pi_agent/session/storage.py` | InMemory + JSONL 存储 | 会话持久化 | ~250行 |
| **P2** | `pi_agent/session/session.py` | 会话树操作 | 分支/导航 | ~200行 |
| **P2** | `pi_agent/compaction.py` | 上下文压缩 | 长对话管理 | ~200行 |
| **P2** | `pi_agent/branch_summary.py` | 分支摘要 | 分支切换 | ~150行 |

**总计核心功能约 ~3000 行 Python 代码。**

---

## 建议的实现顺序

### Phase 1：让 LLM 通信跑起来

```
1. pi_ai/types.py          → 定义所有类型
2. pi_ai/event_stream.py   → 实现异步事件流
3. pi_ai/auth.py           → API Key 认证
4. pi_ai/models.py         → Provider 注册表
5. pi_ai/providers/anthropic.py → 第一个 provider
6. pi_ai/providers/model_catalogs.py → Anthropic 模型列表
```

**验证点**：能通过 `Models.stream_simple(model, context)` 获取 Anthropic 的流式响应。

### Phase 2：让 Agent 循环跑起来

```
7. pi_agent/types.py       → Agent 类型定义
8. pi_agent/messages.py    → convertToLlm 消息转换
9. pi_agent/agent_loop.py  → 核心循环引擎（带工具执行）
10. pi_agent/agent.py      → Agent 有状态封装
```

**验证点**：能通过 `Agent.prompt("hello")` 获得带工具调用的多轮对话。

### Phase 3：让编码 Agent 可用

```
11. pi_coding_agent/tools/bash.py    → Shell 工具
12. pi_coding_agent/tools/read.py    → 读文件工具
13. pi_coding_agent/tools/write.py   → 写文件工具
14. pi_coding_agent/tools/edit.py    → 编辑文件工具
15. pi_coding_agent/tools/grep.py    → 搜索工具
16. pi_coding_agent/tools/glob.py    → 文件匹配工具
17. pi_coding_agent/system_prompt.py → 系统提示
18. pi_coding_agent/cli.py           → CLI 入口
```

**验证点**：能在命令行运行 `pi` 并进行交互式编码对话。

### Phase 4：增加更多 Provider 和高级功能

```
19. pi_ai/providers/openai_completions.py → OpenAI + compat 系统
20. pi_ai/providers/openai_responses.py  → OpenAI Responses
21. pi_ai/providers/google.py             → Google Gemini
22. pi_ai/transform_messages.py           → 跨 provider 消息规范化
23. pi_agent/session/                     → 会话持久化
24. pi_agent/compaction.py                → 上下文压缩
```

---

## Python 实现的优势

相比 TS 版，Python 版有几个天然简化点：

1. **SDK 更成熟**：`openai`、`anthropic`、`google-genai` Python SDK 都有完善的流式支持，不需要像 TS 版自己写 SSE decoder。

2. **async/await 更自然**：Python 的 asyncio 比 TS 的 Promise 体系更适合实现 agent_loop 的双层 while 循环。

3. **类型系统更简单**：不需要 TypeScript 的声明合并（`CustomAgentMessages`），Python 用 Union 类型即可。

4. **不需要 tree-shaking**：TS 版的 `lazy.ts` 动态导入是为了按需加载减少包体积。Python 不需要这个——所有模块在运行时加载，没有打包步骤。

5. **文件操作更简单**：Python 的 `pathlib`、`asyncio.create_subprocess_shell` 等内置工具比 TS 版的 Node.js `child_process.spawn` + 跨平台 bash 查找更直接。

---

## 可暂时跳过的功能

以下功能对核心运行不是必须的，可在后续迭代中添加：

- 30+ 个 OpenAI-compatible provider（DeepSeek、xAI、Groq、OpenRouter 等）
- OAuth 登录流程（只支持 API Key + 环境变量）
- Image generation（图片生成 API）
- WebSocket transport（Codex provider）
- Bedrock provider（AWS SDK 复杂度高）
- PromptTemplate 系统
- 分支摘要（branch summary）
- Shell 输出二进制清洗（初期简单处理）
- GitHub Copilot 动态 headers
- 会话 fork 操作
- RPC entry（远程会话）
- 容器化/沙箱支持
