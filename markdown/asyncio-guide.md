# Python asyncio 学习指导手册

> 目标：掌握 asyncio 核心机制，从而无障碍阅读 `pi_ai/event_stream.py`、`pi_agent/agent_loop.py`、`pi_coding_agent/tools/bash.py` 等源码。
> 每节含可运行示例，最后一节用 asyncio 复现 `EventStream` 的"同步返回、异步填充"模式，直接对照源码。
>
> 运行示例：把代码存成 `.py` 文件后 `python 文件名.py`（需 Python 3.10+）。

---

## 目录

1. [核心概念：事件循环、协程、Awaitable](#1-核心概念事件循环协程awaitable)
2. [async/await 基础](#2-asyncawait-基础)
3. [asyncio.create_task：并发任务](#3-asynciocreate_task并发任务)
4. [asyncio.gather / wait：批量并发控制](#4-asynciogather--wait批量并发控制)
5. [asyncio.Queue：异步队列（EventStream 的地基）](#5-asyncioqueue异步队列eventstream-的地基)
6. [asyncio.Event：事件信号（result 就绪与 AbortSignal）](#6-asyncioevent事件信号result-就绪与-abortsignal)
7. [异步迭代：`async for` / `__aiter__` / `__anext__`](#7-异步迭代async-for--__aiter__--__anext__)
8. [asyncio.create_subprocess_shell：子进程（bash 工具）](#8-asynciocreate_subprocess_shell子进程bash-工具)
9. [超时与取消：`wait_for` / `cancel` / 取消传播](#9-超时与取消wait_for--cancel--取消传播)
10. [综合实战：用 asyncio 复现 EventStream](#10-综合实战用-asyncio-复现-eventstream)
11. [常见陷阱](#11-常见陷阱)
12. [源码对照速查表](#12-源码对照速查表)

---

## 1. 核心概念：事件循环、协程、Awaitable

| 概念 | 一句话解释 | 类比 |
|------|-----------|------|
| **Event Loop（事件循环）** | 单线程调度器，轮流推进多个"暂停中"的协程，并在 I/O 就绪时唤醒对应协程 | 一个服务员同时照看多桌客人 |
| **Coroutine（协程）** | `async def` 定义的函数；调用它返回一个 coroutine 对象，不立即执行 | 一份菜谱，点了还没做 |
| **Awaitable** | 可被 `await` 的对象：协程、Task、Future，或实现了 `__await__` 的对象 | 可上桌的菜 |
| **Task** | 被 event loop 调度的协程包装；`create_task` 后协程才真正"开始跑" | 菜单上正在做的菜 |
| **Future** | 底层"将来会有结果"的占位对象；`asyncio.Event`、`Queue.get()` 内部都基于它 | 取餐号 |

**关键认知**：asyncio 是**单线程并发**。没有新线程，只是在一个线程里切换"正在等 I/O 的协程"。`await` 把控制权交还事件循环，循环借机推进别的协程；等 I/O 就绪再回来。

```python
import asyncio

async def say(text, delay):
    await asyncio.sleep(delay)   # 交还控制权，模拟 I/O 等待
    return text

# asyncio.run 创建事件循环、运行 main 协程、结束后关闭循环
asyncio.run(say("hi", 0.1))
```

> 注意：`asyncio.run()` 是程序入口，整个程序通常只有一个。库代码内部**不要**调用 `asyncio.run`，应由调用方驱动。

---

## 2. async/await 基础

- `async def` 定义协程；函数体内可用 `await`。
- `await x`：暂停当前协程，等 `x` 完成，拿到结果后恢复。期间事件循环可跑别的协程。
- `await` 只能在 `async def` 内（或 REPL 顶层）。
- 直接调用协程函数 `f()` 只产生 coroutine 对象，**不会执行**；必须 `await f()` 或 `asyncio.create_task(f())` 或 `asyncio.run(f())`。

```python
import asyncio

async def fetch_data(name, delay):
    print(f"  [{name}] 开始")
    await asyncio.sleep(delay)          # 模拟网络/磁盘等待
    print(f"  [{name}] 完成")
    return f"{name}的数据"

async def main():
    # 串行：总耗时 = 0.3 + 0.2 = 0.5s
    a = await fetch_data("A", 0.3)
    b = await fetch_data("B", 0.2)
    print("结果:", a, b)

asyncio.run(main())
```

输出（耗时约 0.5s）：

```
  [A] 开始
  [A] 完成
  [B] 开始
  [B] 完成
结果: A的数据 B的数据
```

**常见错误**：忘记 `await`。

```python
async def bad():
    fetch_data("A", 0.1)   # 没 await，没执行，只拿到一个没用的 coroutine 对象
    # Python 会发 RuntimeWarning: coroutine 'fetch_data' was never awaited
```

---

## 3. asyncio.create_task：并发任务

`asyncio.create_task(coro)` 把协程包成 Task 并**立即**交给事件循环调度。之后用 `await task` 取结果。

```python
import asyncio

async def fetch_data(name, delay):
    await asyncio.sleep(delay)
    return f"{name}的数据"

async def main():
    # 并发：两个任务同时跑，总耗时 ≈ max(0.3, 0.2) = 0.3s
    t1 = asyncio.create_task(fetch_data("A", 0.3))
    t2 = asyncio.create_task(fetch_data("B", 0.2))
    a = await t1
    b = await t2
    print("结果:", a, b)

asyncio.run(main())
```

输出（耗时约 0.3s，不是 0.5s）：

```
结果: A的数据 B的数据
```

**为什么能并发**：`create_task` 立即把两个协程挂到循环；`await t1` 时 B 也在跑；B 的 `sleep` 先就绪，循环先唤醒 B，再唤醒 A。

**重要约束**：`create_task` 必须在**运行中的事件循环**里调用（即 `asyncio.run` 启动之后）。如果在模块顶层直接 `asyncio.create_task(...)` 会报错。

**保存 Task 引用**：不被任何变量引用的 Task 可能被 GC 回收而静默取消。实践中存到一个集合里：

```python
tasks = set()
def spawn(coro):
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)
    return t
```

---

## 4. asyncio.gather / wait：批量并发控制

### asyncio.gather

并发运行多个 awaitable，按**输入顺序**返回结果列表。任一抛异常时默认会让整个 gather 抛出。

```python
import asyncio

async def fetch(name, delay):
    await asyncio.sleep(delay)
    return f"{name}({delay})"

async def main():
    # 并发跑 3 个，结果顺序与传入顺序一致
    results = await asyncio.gather(
        fetch("A", 0.3),
        fetch("B", 0.1),
        fetch("C", 0.2),
    )
    print(results)  # ['A(0.3)', 'B(0.1)', 'C(0.2)']

asyncio.run(main())
```

**容错**：`return_exceptions=True` 把异常当结果返回，不让一个失败拖垮全部：

```python
async def boom():
    raise ValueError("炸了")

results = await asyncio.gather(fetch("A", 0.1), boom(), return_exceptions=True)
# results = ['A(0.1)', ValueError('炸了')]
for r in results:
    if isinstance(r, Exception):
        print("某个任务失败:", r)
    else:
        print("成功:", r)
```

### asyncio.wait

更底层：返回 `(done, pending)` 两个集合，可用 `return_when=FIRST_COMPLETED` 在第一个完成时返回（不等全部）。

```python
async def main():
    tasks = [asyncio.create_task(fetch(c, d)) for c, d in [("A", 0.3), ("B", 0.1)]]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    print("最先完成的:", [t.result() for t in done])
    for t in pending:
        t.cancel()   # 不再等其余的，取消掉
```

> 源码关联：`pi_agent/agent_loop.py` 的 `execute_tool_calls` 用 `asyncio.gather` 并行执行多个工具，但结果按 assistant 源序回灌——gather 已保证结果顺序，正好契合。

---

## 5. asyncio.Queue：异步队列（EventStream 的地基）

`asyncio.Queue` 是生产者-消费者桥梁。`EventStream` 正是用它存事件。

| 方法 | 行为 |
|------|------|
| `await queue.put(x)` | 放入元素；无界队列不阻塞，有界队列满时阻塞 |
| `queue.put_nowait(x)` | **同步**放入，不阻塞；满了抛 `QueueFull` |
| `await queue.get()` | 取出元素；空时阻塞等待 |
| `queue.get_nowait()` | 同步取出；空时抛 `QueueEmpty` |
| `queue.empty()` | 是否空（快照） |

### 生产者-消费者示例

```python
import asyncio

async def producer(queue):
    for i in range(3):
        await asyncio.sleep(0.1)
        print(f"生产 {i}")
        await queue.put(i)
    await queue.put(None)   # 哨兵：表示结束

async def consumer(queue):
    while True:
        item = await queue.get()
        if item is None:    # 收到哨兵，退出
            break
        print(f"  消费 {item}")

async def main():
    queue = asyncio.Queue()
    await asyncio.gather(producer(queue), consumer(queue))

asyncio.run(main())
```

输出：

```
生产 0
  消费 0
生产 1
  消费 1
生产 2
  消费 2
```

### put_nowait：同步入队（关键）

`EventStream.push()` 是**同步**方法，它用 `put_nowait` 立即把事件塞进队列，不等任何东西。这让"先同步填满、后异步消费"成为可能：

```python
import asyncio

async def main():
    queue: asyncio.Queue[int | None] = asyncio.Queue()
    # 在不 await 的情况下连续塞入
    for i in range(3):
        queue.put_nowait(i)
    queue.put_nowait(None)

    # 之后异步消费
    while True:
        x = queue.get_nowait() if not queue.empty() else await queue.get()
        if x is None:
            break
        print(x)

asyncio.run(main())
```

> 源码关联：`event_stream.py` 的 `__anext__` 先用 `get_nowait` 把已入队的事件立刻吐出，空了才 `await queue.get()` 阻塞等生产者。这就是 `EventStream` "同步返回、异步填充"能 drain 的原因。

### 无运行循环也能构造

Python 3.10+ 起，`asyncio.Queue()` 和 `asyncio.Event()` 在**没有运行中的事件循环**时也能构造（它们不再在构造时绑定 loop）。这正是 `EventStream.__init__` 能被同步调用的前提——你可以在普通同步代码里 `stream = AssistantMessageEventStream()`，之后再在 async 上下文里 `async for`。参见 `event_stream.py` 顶部注释。

---

## 6. asyncio.Event：事件信号（result 就绪与 AbortSignal）

`asyncio.Event` 是一个"开关"：内部一个布尔标志。

| 方法 | 行为 |
|------|------|
| `event.set()` | 置为已触发；所有 `wait()` 的协程立即被唤醒 |
| `event.clear()` | 复位为未触发 |
| `event.is_set()` | 当前是否已触发 |
| `await event.wait()` | 未触发时阻塞，直到 `set()` |

`set()` 是同步方法，可在同步代码里调用（如 `EventStream.push` 里 push 到 done 时）。

### 基础示例

```python
import asyncio

async def waiter(event):
    print("等待中...")
    await event.wait()
    print("被唤醒了")

async def setter(event):
    await asyncio.sleep(0.2)
    print("触发！")
    event.set()

async def main():
    event = asyncio.Event()
    await asyncio.gather(waiter(event), setter(event))

asyncio.run(main())
```

输出：

```
等待中...
触发！
被唤醒了
```

### 用 Event 实现"结果就绪"

这是 `EventStream.result()` 的核心模式：用 Event 当"结果准备好了"的门铃，用一个变量存结果本身。

```python
import asyncio

class AsyncResult:
    def __init__(self):
        self._event = asyncio.Event()
        self._value = None

    def set_value(self, value):        # 同步设置
        self._value = value
        self._event.set()

    async def get(self):               # 异步等待
        await self._event.wait()
        return self._value

async def main():
    r = AsyncResult()

    async def producer():
        await asyncio.sleep(0.1)
        r.set_value(42)                # 任意时刻同步设置

    async def consumer():
        val = await r.get()            # 阻塞直到被设置
        print("拿到:", val)

    await asyncio.gather(consumer(), producer())

asyncio.run(main())
```

> 源码关联：
> - `EventStream._result_event` + `_result` 就是上面的 `AsyncResult` 模式；`result()` = `await _result_event.wait()` 后返回 `_result`。
> - `AbortSignal`（`event_stream.py`）整个就是 `asyncio.Event` 的薄包装：`abort()` = `set()`，`aborted` = `is_set()`，`await wait()` = `await _event.wait()`。`Agent.abort()` / bash 工具的超时取消都靠它。

### 单次触发语义

`asyncio.Event` 可重复 set/clear。但 `EventStream._set_result` 用 `_has_result` 标志保证结果只设一次——这是"done 已 resolve 就忽略后续 push"的语义来源，不是 Event 本身能保证的。

---

## 7.异步迭代：`async for` / `__aiter__` / `__anext__`

同步迭代用 `__iter__`/`__next__` + `StopIteration`；异步迭代用 `__aiter__`/`__anext__` + `StopAsyncIteration`。

```python
import asyncio

class AsyncRange:
    def __init__(self, n):
        self.n = n
        self.i = 0

    def __aiter__(self):          # 返回异步迭代器（通常返回 self）
        return self

    async def __anext__(self):
        if self.i >= self.n:
            raise StopAsyncIteration   # 结束信号
        await asyncio.sleep(0.05)      # 可在此 await！
        value = self.i
        self.i += 1
        return value

async def main():
    async for x in AsyncRange(3):
        print(x)

asyncio.run(main())
```

输出 `0 1 2`。

### `__anext__` 里可混合同步与异步取数

`EventStream.__anext__` 的精髓：先同步 `get_nowait` 吐已有事件，没有才 `await queue.get()` 阻塞。简化版：

```python
import asyncio

class Stream:
    def __init__(self):
        self._queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._done = False

    def push(self, x):
        self._queue.put_nowait(x)

    def end(self):
        self._done = True
        self._queue.put_nowait(None)   # 哨兵

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._queue.empty():
            item = self._queue.get_nowait()
        elif self._done:
            raise StopAsyncIteration
        else:
            item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

async def main():
    s = Stream()
    for i in range(3):
        s.push(i)          # 同步填充
    s.end()
    async for x in s:      # 异步消费
        print(x)

asyncio.run(main())
```

这就是 `event_stream.py` `__anext__` 的骨架，去掉泛型与 result 后的原型。理解了它就理解了 `AssistantMessageEventStream` 的迭代部分。

---

## 8. asyncio.create_subprocess_shell：子进程（bash 工具）

异步跑 shell 命令，流式读取 stdout/stderr。这是 `pi_coding_agent/tools/bash.py` 的实现基础。

```python
import asyncio

async def run_cmd(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # communicate() 等子进程结束并一次性拿回全部输出
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()

async def main():
    code, out, err = await run_cmd("echo hello && echo world")
    print("exit:", code)
    print("out:", out.strip())

asyncio.run(main())
```

输出：

```
exit: 0
out: hello
world
```

### 流式逐行读（实时输出，不等结束）

bash 工具要实时把输出回传给 agent，用 `proc.stdout` 按行异步读：

```python
import asyncio

async def stream_cmd(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    # async for 直接逐行读子进程输出
    async for line in proc.stdout:
        print("行:", line.decode().rstrip())
    await proc.wait()   # 等退出码

async def main():
    await stream_cmd("echo a; echo b; echo c")

asyncio.run(main())
```

### 超时与取消

`communicate()` 可配合 `asyncio.wait_for` 设超时；超时后应 kill 子进程，避免僵尸：

```python
import asyncio

async def run_with_timeout(cmd, timeout):
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode()
    except asyncio.TimeoutError:
        proc.kill()                       # 杀掉子进程
        await proc.wait()
        raise

async def main():
    try:
        await run_with_timeout("sleep 5", timeout=0.3)
    except asyncio.TimeoutError:
        print("超时，已 kill")

asyncio.run(main())
```

> 源码关联：bash 工具的 `execute()` 接收 `AbortSignal`，在循环里 `await signal.wait()` 或 `await asyncio.wait_for` 来响应取消——一旦 abort 就 `proc.kill()`。

---

## 9. 超时与取消：`wait_for` / `cancel` / 取消传播

### asyncio.wait_for：给任意 awaitable 加超时

```python
import asyncio

async def slow():
    await asyncio.sleep(10)
    return "done"

async def main():
    try:
        await asyncio.wait_for(slow(), timeout=0.2)
    except asyncio.TimeoutError:
        print("超时")
```

超时后 `wait_for` 会取消被等待的协程。

### Task.cancel：主动取消

```python
import asyncio

async def long_task():
    try:
        print("干活中...")
        await asyncio.sleep(10)
        print("完成")            # 不会执行
    except asyncio.CancelledError:
        print("被取消，清理资源")
        raise                     # 惯例：重新抛出，让调用方感知

async def main():
    t = asyncio.create_task(long_task())
    await asyncio.sleep(0.1)
    t.cancel()                    # 请求取消
    try:
        await t
    except asyncio.CancelledError:
        print("main 感知到取消")

asyncio.run(main())
```

输出：

```
干活中...
被取消，清理资源
main 感知到取消
```

### 取消传播

取消一个 Task 会把 `CancelledError` 注入它**正在 await 的那个点**。该协程可在 `except/finally` 里清理（关连接、kill 子进程），然后**应重新 `raise`** 让取消继续传播。若吞掉不 re-raise，取消信号就断了，调用方 `await task` 不会感知 `CancelledError`。

### asyncio.shield：防取消

`await asyncio.shield(coro)` 保护内部操作不被外部 `cancel` 打断（外部仍会收到 CancelledError，但内部继续跑）。用于"取消不要中断必须完成的收尾"。

> 源码关联：`Agent.abort()` 触发 `AbortSignal`，agent_loop 和工具在 await 点检查它。这是**协作式取消**——靠代码主动检查信号，而非强行 cancel task。与 `task.cancel()` 的注入式取消互补。

---

## 10. 综合实战：用 asyncio 复现 EventStream

把第 5、6、7 节组合起来，写一个**与 `pi_ai/event_stream.py` 同构**的最小 EventStream。跑通它，再读源码就一目了然。

```python
import asyncio


class EventStream:
    """最小版：同步 push 入队，async for 消费，result() 等终态结果。"""

    def __init__(self, is_complete, extract_result):
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._queue: asyncio.Queue = asyncio.Queue()       # 存事件
        self._done = False
        self._result_event = asyncio.Event()               # 结果就绪门铃
        self._result = None
        self._has_result = False

    # ---- 生产者（同步）----

    def push(self, event):
        if self._done:
            return
        if self._is_complete(event):
            self._done = True
            self._set_result(self._extract_result(event))
        self._queue.put_nowait(event)          # 同步入队，不阻塞

    def end(self, result=None):
        self._done = True
        if result is not None:
            self._set_result(result)
        self._queue.put_nowait(None)           # 哨兵结束迭代

    # ---- 结果 ----

    def _set_result(self, result):
        if self._has_result:
            return
        self._result = result
        self._has_result = True
        self._result_event.set()               # 唤醒所有 result() 等待者

    async def result(self):
        await self._result_event.wait()
        return self._result

    # ---- 消费者（异步迭代）----

    def __aiter__(self):
        return self

    async def __anext__(self):
        # 先吐已入队的，避免同步填满的流无谓阻塞
        if not self._queue.empty():
            event = self._queue.get_nowait()
        elif self._done:
            raise StopAsyncIteration
        else:
            event = await self._queue.get()    # 等生产者 push
        if event is None:
            raise StopAsyncIteration
        return event


# ---- 演示 ----

async def fake_provider(stream):
    """模拟一个 provider：同步 push 一串事件。"""
    await asyncio.sleep(0.05)                  # 假装在连 auth
    for i in range(3):
        stream.push({"type": "text_delta", "delta": str(i)})
    stream.push({"type": "done", "value": "最终结果"})   # 终态事件


async def main():
    stream = EventStream(
        is_complete=lambda ev: ev["type"] == "done",
        extract_result=lambda ev: ev["value"],
    )

    # 后台跑生产者
    asyncio.create_task(fake_provider(stream))

    # 消费者：边迭代边打印
    async for ev in stream:
        print("事件:", ev)

    # 拿最终结果（done 已 set，立即返回）
    print("结果:", await stream.result())

asyncio.run(main())
```

输出：

```
事件: {'type': 'text_delta', 'delta': '0'}
事件: {'type': 'text_delta', 'delta': '1'}
事件: {'type': 'text_delta', 'delta': '2'}
事件: {'type': 'done', 'value': '最终结果'}
结果: 最终结果
```

**与源码逐点对照**：

| 本示例 | `pi_ai/event_stream.py` | 说明 |
|--------|------------------------|------|
| `self._queue` | `self._queue: asyncio.Queue` | 存事件，None 哨兵结束 |
| `self._result_event` | `self._result_event` | 结果就绪门铃 |
| `push()` | `push()` | 同步入队 + 终态时 `_set_result` |
| `end()` | `end()` | 异常终止，放 None 哨兵 |
| `_set_result()` | `_set_result()` | 只设一次（`_has_result`） |
| `result()` | `result()` | `await _result_event.wait()` 后返回 |
| `__anext__` 三段式 | `__anext__` | 先 `get_nowait`、done 则停、否则 `await get` |
| `is_complete`/`extract_result` 注入 | 同 | 泛型化的终态判定与结果提取 |

把这段示例和 `event_stream.py` 并排读，会发现源码只是加了泛型 `TypeVar`、`AbortSignal`、`AssistantMessageEventStream` 子类和类型注解，骨架完全一致。

### 加上 AbortSignal（取消）

补一个 `AbortSignal`，演示协作式取消如何让一个长生产者提前 `end()`：

```python
import asyncio


class AbortSignal:
    def __init__(self):
        self._event = asyncio.Event()

    @property
    def aborted(self):
        return self._event.is_set()

    def abort(self):
        self._event.set()

    async def wait(self):
        await self._event.wait()


async def producer_until_abort(stream, signal):
    for i in range(100):
        if signal.aborted:                      # 协作式检查
            stream.end(result="aborted")
            return
        stream.push({"type": "tick", "i": i})
        await asyncio.sleep(0.05)


async def main():
    queue: asyncio.Queue = asyncio.Queue()
    signal = AbortSignal()

    # 简化：复用上面的 EventStream 思路，这里只演示信号
    async def cancel_after():
        await asyncio.sleep(0.12)
        signal.abort()
        print(">> 触发 abort")

    asyncio.create_task(cancel_after())
    await producer_until_abort(None, signal)    # 传入真实 stream 即可
    print("aborted?", signal.aborted)

asyncio.run(main())
```

> 这正是 `Agent.abort()` → `AbortSignal` → provider/bash 工具循环里 `if signal.aborted: break` 的机制。

---

## 11. 常见陷阱

1. **忘记 `await`**：`fetch()` 不 `await` 就不执行，只产生一个被遗弃的 coroutine 对象（会有 RuntimeWarning）。
2. **在同步代码里 `await`**：`await` 只能在 `async def` 内。要跨同步/异步边界，用 `asyncio.run`（程序入口）或 `loop.run_until_complete`。
3. **顶层 `create_task`**：必须在运行中的事件循环里；模块加载时调用会报 `RuntimeError: no running event loop`。
4. **Task 被 GC**：没保存引用的 Task 可能被回收而静默取消。存到集合或属性里。
5. **`asyncio.run` 嵌套**：一个程序只能有一个 `asyncio.run`；在已有循环里再 `asyncio.run` 会报错。
6. **吞掉 `CancelledError`**：`except` 后不 `raise` 会阻断取消传播，调用方感知不到。除非确有理由，一律 re-raise。
7. **`Queue` / `Event` 的 loop 绑定（3.9 及以下）**：旧版构造时绑定当前 loop，在无 loop 处构造会报错。本项目要求 3.10+，已无此问题（见 `event_stream.py` 注释）。
8. **并行≠结果顺序**：`gather` 保证结果顺序按输入；但 `as_completed` / `wait(FIRST_COMPLETED)` 是按**完成序**。agent_loop 要求 toolResult 按 assistant 源序，所以用 `gather` 而非 `as_completed`。
9. **阻塞调用困死循环**：在协程里调 `time.sleep(10)`、`requests.get()`（同步）会卡住整个事件循环，所有协程都不动。用 `asyncio.sleep` / `aiohttp` / `loop.run_in_executor`。
10. **子进程不 kill**：`wait_for` 超时只取消 await，子进程仍跑。必须显式 `proc.kill()` + `await proc.wait()`。
11. **`asyncio.run` 关闭后不能再用其创建的对象**：循环关闭后，在该循环里建的 Queue/Event/Task 不可再用。跨循环要重建。

---

## 12. 源码对照速查表

| asyncio 概念 | 本项目用到的地方 | 阅读入口 |
|--------------|-----------------|---------|
| `async def` / `await` | 几乎所有 `pi_ai`/`pi_agent` 模块 | `pi_ai/event_stream.py` `result()` |
| `asyncio.Queue` | `EventStream._queue`（事件缓冲） | `event_stream.py:68` |
| `Queue.put_nowait` / `get_nowait` | `push()`、`__anext__` 先吐已入队 | `event_stream.py:87,123,124` |
| `Queue.get`（await） | `__anext__` 等生产者 | `event_stream.py:132` |
| `asyncio.Event` | `_result_event`、`AbortSignal._event` | `event_stream.py:36,70` |
| `Event.set` / `wait` / `is_set` | `_set_result`、`result()`、`abort()`、`aborted` | `event_stream.py:39-46,107,111` |
| `__aiter__` / `__anext__` / `StopAsyncIteration` | `EventStream` 异步迭代 | `event_stream.py:117-135` |
| `async for` | 消费流式事件（agent_loop、测试） | `tests/test_event_stream.py:89` |
| `asyncio.create_task` | lazyStream 的后台 setup、agent 事件处理 | roadmap Phase 1.1 关键概念 |
| `asyncio.gather` | `execute_tool_calls` 并行执行工具 | roadmap Phase 2.2 |
| `asyncio.create_subprocess_shell` | bash 工具执行命令 | roadmap Phase 3.1 |
| `asyncio.subprocess.PIPE` + `async for line in proc.stdout` | bash 实时输出 | roadmap Phase 3.1 |
| `asyncio.wait_for` / `TimeoutError` | bash 超时、provider 超时 | `types.py` `StreamOptions.timeout_ms` |
| `task.cancel()` / `CancelledError` | 取消长任务 | roadmap Phase 2 自检「abort 能中止」 |
| 协作式取消（`AbortSignal`） | `Agent.abort()`、provider 循环、bash | `event_stream.py:29-49` |

---

## 附：练习清单

读完本手册后，建议动手做以下练习以巩固，并直接服务源码阅读：

1. 写一个 `AsyncCounter(n)` 异步迭代器，每 `async for` 产出一个数并 `sleep(0.01)`。
2. 用 `asyncio.Queue` 写一个"3 生产者 + 1 消费者"程序，生产者结束后消费者自动退出（哨兵计数）。
3. 用 `asyncio.Event` 实现一个 `Latch`：`wait()` 阻塞直到 `release()`，可被多个协程同时等待。
4. 写一个 `timeout_async(coro, t)` 函数：用 `wait_for`，超时返回 `None` 而非抛异常。
5. 用 `create_subprocess_shell` 跑 `ping -c 4 127.0.0.1`，实时逐行打印，并在 1 秒后强制 kill。
6. 把第 10 节的 `EventStream` 扩展成泛型 `EventStream[TEvent, TResult]`，再加一个 `AbortSignal` 让 `fake_provider` 可被取消——完成后与 `pi_ai/event_stream.py` 逐行对照。

做完练习 6，你就能无障碍阅读 `pi_ai/event_stream.py` 全文，并具备阅读 `agent_loop.py` 并行工具执行与 `bash.py` 子进程流的全部 asyncio 前置知识。
