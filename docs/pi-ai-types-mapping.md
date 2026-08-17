# `pi_ai/types.py` 与 Pi 源码类型对照表

> 基于 Pi 源码 `packages/ai/src/types.ts` 及相关文件的逐类型对照分析。
> Pi 源码仓库：https://github.com/earendil-works/pi

---

## 一、枚举 / Literal 类型

### KnownApi

| Python | Pi 源码 |
|--------|---------|
| `Literal["openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai", "google-vertex", "bedrock-converse-stream", "mistral-conversations", "azure-openai-responses", "openai-codex-responses"]` | `KnownApi = "openai-completions" | "mistral-conversations" | "openai-responses" | "azure-openai-responses" | "openai-codex-responses" | "anthropic-messages" | "bedrock-converse-stream" | "google-generative-ai" | "google-vertex"` |

**差异**：✅ 9个值完全一致。Pi 还定义了 `Api = KnownApi | (string & {})` 允许任意扩展字符串，Python 未实现此扩展能力。

---

### StopReason

| Python | Pi 源码 |
|--------|---------|
| `Literal["stop", "length", "toolUse", "error", "aborted"]` | `StopReason = "stop" | "length" | "toolUse" | "error" | "aborted"` |

**差异**：✅ 完全对应。

---

### ThinkingLevel

| Python | Pi 源码 |
|--------|---------|
| `Literal["minimal", "low", "medium", "high", "xhigh"]` | `ThinkingLevel = "minimal" | "low" | "medium" | "high" | "xhigh" | "max"` |

**差异**：⚠️ Python 缺少 `"max"` 值。

---

### ModelThinkingLevel

| Python | Pi 源码 |
|--------|---------|
| `Literal["off"] | ThinkingLevel` | `ModelThinkingLevel = "off" | ThinkingLevel` |

**差异**：⚠️ 因 ThinkingLevel 缺少 `"max"`，Python 的 ModelThinkingLevel 也缺少 `"max"`。

---

## 二、内容类型 (Content Types)

### TextContent

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `type` | `Literal["text"]` | `"text"` | ✅ |
| `text` | `str` | `string` | ✅ |
| `textSignature` | — *(缺失)* | `textSignature?: string` | ❌ Python 缺少此字段（用于 OpenAI responses 签名/消息元数据） |

---

### ThinkingContent

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `type` | `Literal["thinking"]` | `"thinking"` | ✅ |
| `thinking` | `str` | `string` | ✅ |
| `thinking_signature` | `str | None = None` | `thinkingSignature?: string` | ✅（命名风格转换） |
| `redacted` | `bool = False` | `redacted?: boolean` | ✅ |

**差异**：✅ 基本完全对应。

---

### ImageContent

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `type` | `Literal["image"]` | `"image"` | ✅ |
| `data` | `str` (base64) | `string` (base64) | ✅ |
| `mime_type` | `str` | `mimeType: string` | ✅（snake_case 转换） |

**差异**：✅ 完全对应。

---

### ToolCall

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `type` | `Literal["toolCall"]` | `"toolCall"` | ✅ |
| `id` | `str` | `string` | ✅ |
| `name` | `str` | `string` | ✅ |
| `arguments` | `dict[str, Any]` | `Record<string, any>` | ✅ |
| `thoughtSignature` | — *(缺失)* | `thoughtSignature?: string` | ❌ Python 缺少此字段（Google-specific：复用思考上下文的签名） |

---

## 三、消息类型 (Message Types)

### UserMessage

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `role` | `Literal["user"]` | `"user"` | ✅ |
| `content` | `str | list[TextContent | ImageContent]` | `string | (TextContent | ImageContent)[]` | ✅ |
| `timestamp` | `int` (Unix ms) | `number` (Unix ms) | ✅ |

**差异**：✅ 完全对应。

---

### AssistantMessage

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `role` | `Literal["assistant"]` | `"assistant"` | ✅ |
| `content` | `list[TextContent | ThinkingContent | ToolCall]` | `(TextContent | ThinkingContent | ToolCall)[]` | ✅ |
| `api` | `str` | `Api` (= `KnownApi | string`) | ⚠️ Pi 类型更严格 |
| `provider` | `str` | `ProviderId` (= `KnownProvider | string`, 含 36+ 个已知 Provider) | ⚠️ Pi 类型更严格 |
| `model` | `str` | `string` | ✅ |
| `response_model` | `str | None = None` | `responseModel?: string` | ✅ |
| `responseId` | — *(缺失)* | `responseId?: string` | ❌ 缺少（Provider 响应 ID） |
| `diagnostics` | — *(缺失)* | `diagnostics?: AssistantMessageDiagnostic[]` | ❌ 缺少（诊断信息数组） |
| `usage` | `Usage` | `Usage` | ✅ |
| `stop_reason` | `StopReason` | `StopReason` | ✅ |
| `error_message` | `str | None = None` | `errorMessage?: string` | ✅ |
| `timestamp` | `int` | `number` | ✅ |

---

### ToolResultMessage

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `role` | `Literal["toolResult"]` | `"toolResult"` | ✅ |
| `tool_call_id` | `str` | `toolCallId: string` | ✅（命名转换） |
| `tool_name` | `str` | `toolName: string` | ✅ |
| `content` | `list[TextContent | ImageContent]` | `(TextContent | ImageContent)[]` | ✅ |
| `details` | `Any | None = None` | `details?: TDetails` (泛型) | ⚠️ Pi 支持泛型，Python 用 `Any` |
| `is_error` | `bool = False` | `isError: boolean` | ✅ |
| `timestamp` | `int` | `number` | ✅ |

**差异**：✅ 基本对应。

---

### Message (union)

| Python | Pi 源码 |
|--------|---------|
| `UserMessage | AssistantMessage | ToolResultMessage` | `UserMessage | AssistantMessage | ToolResultMessage` |

**差异**：✅ 完全对应。

---

## 四、Token 使用量 (Usage)

### Usage

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `input` | `int` | `number` | ✅ |
| `output` | `int` | `number` | ✅ |
| `cache_read` | `int` | `cacheRead: number` | ✅ |
| `cache_write` | `int` | `cacheWrite: number` | ✅ |
| `cacheWrite1h` | — *(缺失)* | `cacheWrite1h?: number` | ❌ 缺少（Anthropic 报告的 1h 缓存写入拆分） |
| `reasoning` | `int | None = None` | `reasoning?: number` | ✅ |
| `total_tokens` | `int` | `totalTokens: number` | ✅ |
| `cost` | `CostInfo` (独立 dataclass) | `{ input; output; cacheRead; cacheWrite; total }` (内联) | ✅（结构对应，Python 拆为独立类） |

---

### CostInfo

| 字段 | Python | Pi 源码 (Usage.cost 内联) | 差异 |
|------|--------|---------------------------|------|
| `input` | `float` | `number` | ✅ |
| `output` | `float` | `number` | ✅ |
| `cache_read` | `float` | `cacheRead: number` | ✅ |
| `cache_write` | `float` | `cacheWrite: number` | ✅ |
| `total` | `float` | `total: number` | ✅ |

**差异**：✅ 完全对应。

---

## 五、请求上下文 (Context)

### Tool

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `name` | `str` | `string` | ✅ |
| `description` | `str` | `string` | ✅ |
| `parameters` | `dict` (注释：JSON Schema) | `TParameters` (TypeBox `TSchema`) | ⚠️ 功能对应但类型精度降低 |

---

### Context

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `system_prompt` | `str | None = None` | `systemPrompt?: string` | ✅ |
| `messages` | `list[Message]` | `Message[]` | ✅ |
| `tools` | `list[Tool] | None = None` | `Tool[]?` | ✅ |

**差异**：✅ 完全对应。

---

## 六、流事件类型 (Stream Events)

### Pi 源码的完整事件类型（12种）

```typescript
AssistantMessageEvent =
  | { type: "start"; partial: AssistantMessage }
  | { type: "text_start"; contentIndex: number; partial: AssistantMessage }
  | { type: "text_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
  | { type: "text_end"; contentIndex: number; content: string; partial: AssistantMessage }
  | { type: "thinking_start"; contentIndex: number; partial: AssistantMessage }
  | { type: "thinking_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
  | { type: "thinking_end"; contentIndex: number; content: string; partial: AssistantMessage }
  | { type: "toolcall_start"; contentIndex: number; partial: AssistantMessage }
  | { type: "toolcall_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
  | { type: "toolcall_end"; contentIndex: number; toolCall: ToolCall; partial: AssistantMessage }
  | { type: "done"; reason: "stop"|"length"|"toolUse"; message: AssistantMessage }
  | { type: "error"; reason: "aborted"|"error"; error: AssistantMessage }
```

### Python 已定义的事件类型（5种）

| Python dataclass | Pi 源码对应 | 状态 |
|------------------|-------------|------|
| `StartEvent` | `{ type: "start" }` | ✅ |
| `TextDeltaEvent` | `{ type: "text_delta" }` | ✅ |
| `ToolCallEndEvent` | `{ type: "toolcall_end" }` | ✅ |
| `DoneEvent` | `{ type: "done" }` | ✅ |
| `ErrorEvent` | `{ type: "error" }` | ✅ |

### Python 缺失的事件类型（7种）

| Pi 源码事件 | 说明 |
|-------------|------|
| `text_start` | 文本块开始，含 `contentIndex` + `partial` |
| `text_end` | 文本块结束，含 `contentIndex` + `content` + `partial` |
| `thinking_start` | 思考块开始，含 `contentIndex` + `partial` |
| `thinking_delta` | 思考增量，含 `contentIndex` + `delta` + `partial` |
| `thinking_end` | 思考块结束，含 `contentIndex` + `content` + `partial` |
| `toolcall_start` | 工具调用开始，含 `contentIndex` + `partial` |
| `toolcall_delta` | 工具调用增量，含 `contentIndex` + `delta` + `partial` |

---

## 七、模型定义 (Model)

### Model

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `id` | `str` | `string` | ✅ |
| `name` | `str` | `string` | ✅ |
| `api` | `str` | `TApi extends Api` | ⚠️ Pi 泛型更严格 |
| `provider` | `str` | `ProviderId` | ⚠️ Pi 类型更严格 |
| `base_url` | `str | None = None` | `baseUrl: string` (必填) | ⚠️ Pi 必填，Python 可空 |
| `reasoning` | `bool = False` | `reasoning: boolean` | ✅ |
| `thinking_level_map` | `dict[str, str | None] | None = None` | `thinkingLevelMap?: ThinkingLevelMap` | ✅ |
| `input_text` | `bool = True` | `input: ("text" | "image")[]` | ⚠️ Python 分拆为两个 bool，Pi 用数组 |
| `input_image` | `bool = False` | *(同上)* | ⚠️ 同上 |
| `cost` | `ModelCost` | `ModelCost` | ⚠️ 见 ModelCost 表 |
| `context_window` | `int` | `contextWindow: number` | ✅ |
| `max_tokens` | `int` | `maxTokens: number` | ✅ |
| `headers` | — *(缺失)* | `headers?: Record<string, string>` | ❌ 缺少 |
| `compat` | `dict | None = None` | 类型安全多态（根据 api 选 Compat 类型） | ⚠️ Pi 类型安全，Python 降级为 dict |

---

### ModelCost

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `input_per_m` | `float` | `input: number` | ✅ |
| `output_per_m` | `float` | `output: number` | ✅ |
| `cache_read_per_m` | `float` | `cacheRead: number` | ✅ |
| `cache_write_per_m` | `float` | `cacheWrite: number` | ✅ |
| `tiers` | — *(缺失)* | `tiers?: ModelCostTier[]` | ❌ 缺少（分层定价：当输入 token 超过阈值时使用不同价格） |

---

## 八、流选项 (Stream Options)

### StreamOptions

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| `temperature` | `float | None = None` | `temperature?: number` | ✅ |
| `max_tokens` | `int | None = None` | `maxTokens?: number` | ✅ |
| `signal` | `asyncio.Event | None = None` | `signal?: AbortSignal` | ⚠️ 类型不同 |
| `api_key` | `str | None = None` | `apiKey?: string` | ✅ |
| `transport` | — *(缺失)* | `transport?: Transport` (`"sse"|"websocket"|"websocket-cached"|"auto"`) | ❌ 缺少 |
| `cache_retention` | `str | None = "short"` | `cacheRetention?: CacheRetention` (`"none"|"short"|"long"`) | ⚠️ Pi 更严格 |
| `session_id` | `str | None = None` | `sessionId?: string` | ✅ |
| `onPayload` | — *(缺失)* | `onPayload?: (payload, model) => unknown` | ❌ 缺少（payload 修改回调） |
| `onResponse` | — *(缺失)* | `onResponse?: (response, model) => void` | ❌ 缺少（响应拦截回调） |
| `headers` | `dict[str, str] | None = None` | `headers?: Record<string, string | null>` | ⚠️ Pi 允许 null 值来删除默认头 |
| `timeout_ms` | `int | None = None` | `timeoutMs?: number` | ✅ |
| `websocketConnectTimeoutMs` | — *(缺失)* | `websocketConnectTimeoutMs?: number` | ❌ 缺少 |
| `max_retries` | `int | None = None` | `maxRetries?: number` | ✅ |
| `maxRetryDelayMs` | — *(缺失)* | `maxRetryDelayMs?: number` | ❌ 缺少 |
| `metadata` | `dict | None = None` | `metadata?: Record<string, unknown>` | ✅ |
| `env` | — *(缺失)* | `env?: ProviderEnv` (`Record<string, string>`) | ❌ 缺少（环境覆盖） |

---

### SimpleStreamOptions

| 字段 | Python | Pi 源码 | 差异 |
|------|--------|---------|------|
| *(继承 StreamOptions)* | `extends StreamOptions` | `extends StreamOptions` | ✅ |
| `reasoning` | `ThinkingLevel | None = None` | `reasoning?: ThinkingLevel` | ✅ |
| `thinking_budgets` | `dict | None = None` | `thinkingBudgets?: ThinkingBudgets` | ⚠️ Pi 有独立接口 `ThinkingBudgets { minimal?; low?; medium?; high? }`，Python 用 dict |

---

## 九、Provider 兼容性配置 (Compat)

### OpenAICompletionsCompat

| 字段 | Python | Pi 源码 | 状态 |
|------|--------|---------|------|
| `supports_store` | `bool | None = None` | `supportsStore?: boolean` | ✅ |
| `supports_developer_role` | `bool | None = None` | `supportsDeveloperRole?: boolean` | ✅ |
| `supports_reasoning_effort` | `bool | None = None` | `supportsReasoningEffort?: boolean` | ✅ |
| `max_tokens_field` | `str | None = None` | `maxTokensField?: "max_completion_tokens" | "max_tokens"` | ⚠️ Pi 类型更严格 |
| `requires_tool_result_name` | `bool | None = None` | `requiresToolResultName?: boolean` | ✅ |
| `thinking_format` | `str | None = "openai"` | `thinkingFormat?: (10种具体字符串值)` | ⚠️ Pi 有10种精确值，Python 用 str |
| `supportsUsageInStreaming` | — | `supportsUsageInStreaming?: boolean` | ❌ 缺少 |
| `requiresAssistantAfterToolResult` | — | `requiresAssistantAfterToolResult?: boolean` | ❌ 缺少 |
| `requiresThinkingAsText` | — | `requiresThinkingAsText?: boolean` | ❌ 缺少 |
| `requiresReasoningContentOnAssistantMessages` | — | `requiresReasoningContentOnAssistantMessages?: boolean` | ❌ 缺少 |
| `chatTemplateKwargs` | — | `chatTemplateKwargs?: Record<string, ChatTemplateKwargValue>` | ❌ 缺少 |
| `openRouterRouting` | — | `openRouterRouting?: OpenRouterRouting` | ❌ 缺少 |
| `vercelGatewayRouting` | — | `vercelGatewayRouting?: VercelGatewayRouting` | ❌ 缺少 |
| `zaiToolStream` | — | `zaiToolStream?: boolean` | ❌ 缺少 |
| `supportsStrictMode` | — | `supportsStrictMode?: boolean` | ❌ 缺少 |
| `cacheControlFormat` | — | `cacheControlFormat?: "anthropic"` | ❌ 缺少 |
| `sendSessionAffinityHeaders` | — | `sendSessionAffinityHeaders?: boolean` | ❌ 缺少 |
| `supportsLongCacheRetention` | — | `supportsLongCacheRetention?: boolean` | ❌ 缺少 |

---

### AnthropicMessagesCompat

| 字段 | Python | Pi 源码 | 状态 |
|------|--------|---------|------|
| `supports_eager_tool_input_streaming` | `bool | None = True` | `supportsEagerToolInputStreaming?: boolean` | ✅ |
| `supports_long_cache_retention` | `bool | None = True` | `supportsLongCacheRetention?: boolean` | ✅ |
| `send_session_affinity_headers` | `bool | None = False` | `sendSessionAffinityHeaders?: boolean` | ✅ |
| `supportsCacheControlOnTools` | — | `supportsCacheControlOnTools?: boolean` | ❌ 缺少 |
| `supportsTemperature` | — | `supportsTemperature?: boolean` | ❌ 缺少 |
| `forceAdaptiveThinking` | — | `forceAdaptiveThinking?: boolean` | ❌ 缺少 |
| `allowEmptySignature` | — | `allowEmptySignature?: boolean` | ❌ 缺少 |

---

### OpenAIResponsesCompat — ❌ 完全缺失

Pi 源码定义了 `OpenAIResponsesCompat` 接口：

```typescript
interface OpenAIResponsesCompat {
  supportsDeveloperRole?: boolean;          // default: true
  sendSessionIdHeader?: boolean;            // default: true
  supportsLongCacheRetention?: boolean;     // default: true
}
```

Python `types.py` 中完全没有此类型定义。

---

## 十、Pi 源码中完全缺失于 Python 的类型

以下类型在 Pi 源码 `packages/ai/src/types.ts` 或相关文件中存在，但 Python `types.py` 未包含：

| Pi 源码类型 | 来源文件 | 说明 |
|-------------|----------|------|
| `AssistantMessageDiagnostic` | `utils/diagnostics.ts` | 诊断信息（type, timestamp, error, details） |
| `DiagnosticErrorInfo` | `utils/diagnostics.ts` | 错误详情（name, message, stack, code） |
| `ImagesModel` | `types.ts` | 图像生成模型定义 |
| `ImagesContext` | `types.ts` | 图像生成上下文（input 数组） |
| `ImagesOptions` | `types.ts` | 图像生成选项 |
| `AssistantImages` | `types.ts` | 图像生成结果 |
| `ImagesStopReason` | `types.ts` | `"stop" | "error" | "aborted"` |
| `ImagesInputContent` | `types.ts` | `TextContent | ImageContent` |
| `ImagesOutputContent` | `types.ts` | `TextContent | ImageContent` |
| `KnownProvider` / `ProviderId` | `types.ts` | 36+个已知 Provider 枚举 |
| `KnownImagesProvider` / `ImagesProviderId` | `types.ts` | `"openrouter" | string` |
| `KnownImagesApi` / `ImagesApi` | `types.ts` | `"openrouter-images" | string` |
| `OpenAIResponsesCompat` | `types.ts` | OpenAI Responses API 兼容配置 |
| `OpenRouterRouting` | `types.ts` | OpenRouter 路由偏好（~15个字段） |
| `VercelGatewayRouting` | `types.ts` | Vercel AI Gateway 路由偏好 |
| `ThinkingBudgets` | `types.ts` | `{ minimal?; low?; medium?; high? }` 各 thinking level token 预算 |
| `ModelCostTier` | `types.ts` | 分层定价（继承 ModelCostRates + inputTokensAbove） |
| `ModelCostRates` | `types.ts` | 基础价格（input, output, cacheRead, cacheWrite） |
| `CacheRetention` | `types.ts` | `"none" | "short" | "long"` |
| `Transport` | `types.ts` | `"sse" | "websocket" | "websocket-cached" | "auto"` |
| `ProviderEnv` | `types.ts` | `Record<string, string>` |
| `ProviderHeaders` | `types.ts` | `Record<string, string | null>` |
| `ProviderResponse` | `types.ts` | `{ status: number; headers }` |
| `ChatTemplateKwargValue` | `types.ts` | `string | number | boolean | null | { $var }` |
| `ApiOptionsMap` | `types.ts` | 已知 API → 具体 Option 类型映射 |
| `ApiStreamOptions<TApi>` | `types.ts` | 类型安全的 API 选项 |
| `ProviderStreams` | `types.ts` | Provider 模块接口（stream + streamSimple） |
| `ProviderImages` | `types.ts` | 图像 Provider 模块接口 |
| `StreamFunction` | `types.ts` | `(model, context, options?) => EventStream` |
| `ImagesFunction` | `types.ts` | `(model, context, options?) => Promise<AssistantImages>` |

---

## 十一、设计策略总结

Python `types.py` 对 Pi 源码的转换采用了以下策略：

| 策略 | 说明 | 示例 |
|------|------|------|
| **命名风格转换** | TS `camelCase` → Python `snake_case` | `thinkingSignature` → `thinking_signature` |
| **内联对象拆分** | TS 内联接口 → Python 独立 dataclass | `Usage.cost` → `CostInfo` |
| **类型简化** | 复杂 TS 类型 → Python 基础类型 | `TSchema` → `dict`; `ProviderId` → `str`; 泛型 Compat → `dict | None` |
| **布尔拆分** | TS 数组枚举 → Python 独立 bool 字段 | `input: ("text"|"image")[]` → `input_text + input_image` |
| **可空性转换** | TS `field?: T` → Python `field: T | None = None` | `thinkingSignature?` → `thinking_signature: str | None = None` |
| **事件类型缩减** | 12种 → 5种 | 缺少 `*_start`, `*_end`, `thinking_delta` |
| **Compat 配置缩减** | 18+字段 → 6字段 | 保留核心兼容性字段 |
| **图像类型完全省略** | 图像生成功能未实现 | `ImagesModel`, `ImagesContext` 等全部缺失 |

### 需要补齐的关键缺失项（按优先级）

1. **高优先级**（影响核心流式功能）：
   - `text_start` / `text_end` / `thinking_start` / `thinking_delta` / `thinking_end` / `toolcall_start` / `toolcall_delta` — 7种缺失流事件
   - `ToolCall.thoughtSignature` — Google 思考签名
   - `TextContent.textSignature` — OpenAI 签名
   - `AssistantMessage.responseId` / `diagnostics` — 调试/追踪必需字段
   - `ThinkingLevel` 缺少 `"max"` 值

2. **中优先级**（影响 Provider 适配完整性）：
   - `OpenAIResponsesCompat` — 完全缺失
   - `StreamOptions` 缺失字段：`transport`, `onPayload`, `onResponse`, `env`, `maxRetryDelayMs`, `websocketConnectTimeoutMs`
   - `Model.headers` — Provider 自定义头
   - `OpenAICompletionsCompat` 缺失字段（~12个）
   - `AnthropicMessagesCompat` 缺失字段（4个）
   - `ModelCost.tiers` — 分层定价
   - `Usage.cacheWrite1h` — Anthropic 1h 缓存拆分

3. **低优先级**（图像/路由等扩展功能）：
   - 图像生成全套类型（`ImagesModel`, `ImagesContext`, `AssistantImages` 等）
   - `OpenRouterRouting` / `VercelGatewayRouting`
   - `ProviderEnv`, `ProviderResponse`, `ProviderHeaders`
   - `ThinkingBudgets` 独立接口
   - `ChatTemplateKwargValue`
