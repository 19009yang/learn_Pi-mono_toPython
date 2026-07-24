## 记录和claude code的对话内容
1. prompt：根据项目下的文件，如果我想实现一个python版本的pi，目前只要实现核心功能，我需要实现哪些代码 

    claude code: 详见`markdown\python-pi-implementation-plan.md`

2. pi_ai\types.py中的类型定义分别是什么，对应pi源码中的什么https://github.com/earendil-works/pi 

    claude code: 详见`markdown\pi-ai-types-mapping.md`

## 学习路径
一、根据AI的建议，先完成type.py与event_stream.py
type.py先直接由AI生成，主要学习event_stream.py的写法

读type.ts注意到的一些语法：<br>
1. `Record<string, string>` 等价 `{ [key: string]: string }`,任意字符串键，值必须为字符串<br>
2. `minimal?: number` 表示minimal可以不存在，如果存在必须是number类型<br>
3. `[key: string]: unknown` 表示任意字符串都可当属性名，这些额外属性的值类型是，用于添加位置索引名，但已知类型的额外字段<br>
4. `export interface`声明一个对象形状（可带方法签名），例如
```ts
export interface ModelAuth {
  apiKey?: string;
  headers?: ProviderHeaders;
  baseUrl?: string;
}
```
`export type` 给任意类型表达式起别名,例如
```ts
export type Credential = ApiKeyCredential | OAuthCredential;  // 联合
export type ProviderEnv = Record<string, string>;               // 工具类型
export type Api = KnownApi | (string & {});                     // 复杂表达式
export type AuthPrompt = { signal?: AbortSignal } & ( ... );    // 交叉 + 联合
```
5. `KnownApi | (string & {})` 字面量联合 + 开放自定义字符串，type Bad = "a" | "b" | string;  // 实际就是 string，字面量被吸收掉了；type Good = "a" | "b" | (string & {});// 联合仍保留 "a" | "b"，同时接受任意自定义字符串<br>
6. `{ signal?: AbortSignal } & ( ... )`表示可选signal和必须选择其一的（...）<br>
公共部分          ∩          四种之一（discriminated union）<br>
{ signal?: ... }  &  ( text | secret | select | manual_code )<br>
7. 下面的extends表示`OAuthCredential` = `OAuthCredentials` 的全部字段 加上 `type: "oauth"`
```ts
export interface OAuthCredential extends OAuthCredentials {
	type: "oauth";
}
```

### 遇到的第一个问题：学习pytest用法
>似乎只是一个快捷验证的写法，不是主要内容
```python
"""测试计算器功能的基础用例"""
# 定义待测试的普通函数（模拟实际项目中的业务逻辑）
def add(a, b):
    """加法函数"""
    return a + b

def sub(a, b):
    """减法函数"""
    return a - b

# 测试函数：验证加法功能
def test_add():
    # 步骤1：准备测试数据
    num1 = 10
    num2 = 20
    expected = 30
    
    # 步骤2：执行待测试函数
    actual = add(num1, num2)
    
    # 步骤3：断言结果是否符合预期（核心）
    assert actual == expected, f"加法计算错误！预期{expected}，实际{actual}"

# 测试类：组织多个相关测试方法
class TestCalculate:
    # 测试减法功能（正常场景）
    def test_sub_normal(self):
        actual = sub(50, 20)
        assert actual == 30, f"减法计算错误！预期30，实际{actual}"
    
    # 测试减法功能（边界场景：负数）
    def test_sub_negative(self):
        actual = sub(10, 30)
        assert actual == -20, f"负数减法错误！预期-20，实际{actual}"
        
# 方式1：运行当前目录所有用例（显示详细结果）
pytest -v

# 方式2：指定文件运行（精准执行，推荐）
pytest test_calculate.py -v -s
```

### 遇到的第二个问题：学习asyncio库的用法
发现源码中用到了大量的异步处理，需要先学习asyncio的内容`https://www.runoob.com/python3/python-asyncio.html`
让AI帮我生成了一个学习路线图：`markdown\asyncio-guide.md`,完整的手敲代码以及练习题在`learn_asyncio\example.py`
>虽然跟着手敲了一遍，感觉还是一知半解...
`pi_ai\event_stream.py`已经同构完成，下一步需要完成auth、Model 注册与 Provider 抽象<br>
(今天就这样了...)

二、根据pi源码中的`packages\ai\src\auth`，编写对应的auth.py
首先需要读下源码中相关的文件<br>
1.`packages/ai/src/auth/credential-store.ts`<br>
>作用：提供按 provider 串行读写的默认内存凭证存储，供 app 日后换成持久化实现

**核心代码**：<br>
```ts
private credentials = new Map<string, Credential>();
private chains = new Map<string, Promise<unknown>>();
```
```credentials```:每个 provider 一条凭证<br>
```chains```:每个 provider 一条 promise 尾巴，保证写操作排队<br>
**串行队列**：
```ts
private enqueue<T>(providerId: string, task: () => Promise<T>): Promise<T> {
  const previous = this.chains.get(providerId) ?? Promise.resolve();
  const next = (async () => {
    await previous.catch(() => {});  // 等上一个；上一个失败也继续
    return task();
  })();
  this.chains.set(providerId, next.catch(() => {})); // 链本身不 reject
  return next; // 调用方仍能收到本次的结果/错误
}
```
**读/写/删操作**
```ts
async read(providerId) {
  return this.credentials.get(providerId);  // 不进队列，可拿稍旧值
}

modify(providerId, fn) {
  return this.enqueue(providerId, async () => {
    const current = this.credentials.get(providerId);
    const next = await fn(current);
    if (next !== undefined) this.credentials.set(providerId, next); // undefined = 不改
    return next ?? current;
  });
}

delete(providerId) {
  return this.enqueue(providerId, async () => {
    this.credentials.delete(providerId);
  });
}
```
|方法|作用|
|---|---|
|```read```|看当前凭证（可能过期），不参与互斥|
|```modify```|唯一写路径：锁内读 → 回调算新值 → 有新值才写入|
|```delete```|logout，与 modify 同一队列|

2.`auth/types.ts`<br>
>作用：`types.ts` 是 auth 子系统的类型契约：定义“存什么、怎么存、怎么解析成请求凭据、登录时怎么交互”。本身无运行时逻辑

3.`auth/helpers.ts`<br>
>`helpers.ts` 给 `provider` 提供两个工厂函数，避免每个 `ApiKeyAuth / OAuthAuth` 都手写一遍

4.`auth/resolve.ts`<br>
>`auth`的真正解析入口：根据 `provider`、已存凭证和环境，产出一次请求要用的 `AuthResult`。`Models / ImagesModels` 共用它

5.`packages/ai/src/env-api-keys.ts`<br>
>按 `provider id` → 环境变量 查「有没有可用的 `ambient API key`」，给 UI / 兼容层判断认证是否已配置。不走 `CredentialStore`，也不负责发请求<br>

将上述核心内容收紧一个auth.py文件，MVP 只做 API Key + 环境变量，OAuth / lazyOAuth / Vertex ADC 等先不做,具体可见`pi_ai\auth.py`<br>
(读auth的内容有些过于折磨了，全是没见过的新用法...)

三、实现**Model**注册与**Provider**抽象，具体文件在`pi_ai\models.py`和`pi_ai\providers\model_catalogs.py`<br>
**models.py**介绍：
---
这个文件是整个项目的模型层核心枢纽，承担四大职责：
1. Provider 注册与管理
- Provider（抽象基类） — 定义一个 AI 供应商的通用契约：有 id、认证、模型列表、流式调用能力
- _CreatedProvider — 具体实现类，封装了模型列表、API 路由（单一或按 model.api 字典分发）、并发安全的刷新机制
- Models — Provider 注册中心，负责聚合查找（按 provider、按 model_id）、批量刷新、认证解析、流式调用
- MutableModels — 在 Models 之上暴露 set_provider / delete_provider / clear_providers，提供运行时动态注册能力
- create_provider / create_models — 声明式工厂函数，隐藏内部类，对外提供简洁的构建 API
2. 认证解析与参数合并
_apply_auth 是关键流程：当发起一次流式调用时，它会：
1. 调用 resolve_provider_auth 解析认证（API Key、base_url、headers、env）
2. 将认证结果与用户传入的 options 合并（用户显式指定的优先级更高）
3. 构造最终的 request_model（可能带认证提供的 base_url）和 request_options
这让上层调用者无需关心认证细节，只需传入 model 和 context 即可。
3. 流式调用编排
Models.stream / stream_simple 是对外的主入口，它们：
- 同步返回一个 AssistantMessageEventStream（通过 _lazy_stream）
- 延迟执行真正的认证 + Provider 调用（封装在 setup() 闭包中）
- 如果 Provider 不存在或 API 未实现，返回 _failed_stream 而不是抛异常
complete / complete_simple 则是便捷方法：流式调用后等待最终结果，适合只关心最终 AssistantMessage 的场景。
4. 工具函数
- calculate_cost — 按 model 的每百万 token 价格计算用量成本（区分 cache_read/cache_write 的长短写入）
- get_supported_thinking_levels / clamp_thinking_level —
推理/思考级别管理：查询模型支持哪些思考深度，将不支持的级别钳位到最接近的支持级别（优先向上找）
- models_are_equal — 判断两个模型是否相同（id + provider 双键）
- has_api — 判断模型是否属于某个 API 类型
---
一句话总结：这个文件是项目的模型层编排中心——它将 Provider 注册、认证解析、流式调用路由、成本计算、思考级别管理等能力串联起来，为上层提供一个统一的、声明式的、错误安全的模型调用接口，上层只需 models.stream(model, context)即可完成一次完整的 AI 请求，无需关心底层是哪个 Provider、如何认证、如何处理错误。<br>
`pi_ai\providers\model_catalogs.py`集中保存各 Provider 可用模型的静态元数据<br>

>直白来说，对于不同的LLM都必须实现其对应的provider类，例如deepseek通过`pi_ai\providers\deepseek.py`中的`deepseek_provider()->create_provider()->_CreatedProvider(api:OpenAICompletionsStreams())->OpenAICompletionsStreams()->OpenAICompletionsStreams().stream()`才是真正创建LLM客户端并调用流式响应的底层实现，有意思的是，这几个类下面都有一个叫stream()的函数实现，属于是stream封装stream封装的stream...<br>


四、新增消息规范化文件`pi_ai\transform_messages.py`,在把对话历史发给 provider 之前做归一化，避免跨模型/跨 provider 回放时出错。<br>
主要功能：
·**丢弃失败消息** — 跳过 stop_reason 为 error / aborted 的 assistant 消息（否则会打断 tool_use → tool_result 链）
·**无视觉模型降级图片** — 用户/工具结果里的图片换成占位文本
·**thinking 块处理** — 同模型保留（含 signature）；跨模型时：redacted 丢弃，普通 thinking 转成文本
·**清掉跨模型的 thought_signature** — 避免另一家 API 拒收
·**补缺失的 tool result** — 有 tool call 但没有对应 result 时，插入 “No result provided” 的错误结果
>写了个简单的验证文件`test\verify_deepseek.py`,这部分应该没啥问题了...(因为只有DeepSeek的Key)

五、这部分该写Agent-Loop了，具体新增文件有：`pi_ai\validation.py`、`test\test_agent_loop.py`<br>
_emit函数解释
```python
async def _emit(emit: AgentEventSink, event: AgentEvent) -> None:
    """统一处理同步和异步两种事件接收器"""
    result = emit(event)
    if inspect.isawaitable(result):
        await result
```
>这个函数在loop中大量出现，最初看到时相当令我费解

其参数如下：
**emit**: AgentEventSink = Callable[[AgentEvent], Awaitable[None] | None]
含义: 一个回调函数，接收一个事件对象，可以同步返回 None 或异步返回 Awaitable[None]
**event**: AgentEvent（Union 类型）
含义: 要发射的事件对象(代码中定义为10类，具体在type:410)<br>

这个函数不输出任何值，内部只调用`emit(event)` —— 把事件交给回调函数<br>
如果回调返回的是 awaitable（异步函数），就 await 它；如果返回的是同步的 None，直接结束<br>
在同步时效果实际上和调用`stream.push(event)`一样，将事件`put_nowait`到内部 `asyncio.Queue` 中<br>
>为什么需要一个 _emit 包装？

1.统一同步/异步：`AgentEventSink` 类型允许回调是同步或异步的。如果将来 `emit` 换成一个异步的UI渲染回调，`_emit` 能自动处理，`agent_loop` 内部代码不需要改动<br>
2.抽象隔离：`agent_loop` 内部不直接依赖 `EventStream` 的 `push` 方法，只依赖一个抽象的"发射器"接口。这样 emit的实现可以替换（例如打印日志、写文件、发 `WebSocket`），循环逻辑本身不变<br>

六、这部分将底层 `agent_loop`（裸循环逻辑）封装为具备完整生命周期管理的对象，具体实现在`pi_agent\agent.py`<br>
>AI根据原始ts代码生成的py代码中，几乎每个函数都会嵌套各种类，类中又有其它的函数和类，封装看得我头疼...

