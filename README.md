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
>按 `provider id` → 环境变量 查「有没有可用的 `ambient API key`」，给 UI / 兼容层判断认证是否已配置。不走 `CredentialStore`，也不负责发请求<>br

将上述核心内容收紧一个auth.py文件，MVP 只做 API Key + 环境变量，OAuth / lazyOAuth / Vertex ADC 等先不做,具体可见`pi_ai\auth.py`<br>
(读auth的内容有些过于折磨了，全是没见过的新用法...)

