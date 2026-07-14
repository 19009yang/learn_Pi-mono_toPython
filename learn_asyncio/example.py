from __future__ import annotations
# import asyncio

# # 协程（Coroutine）通过 async def 关键字定义，并通过 await 关键字暂停执行，等待异步操作完成
# async def say_hello():
#     print("Hello")
#     await asyncio.sleep(10)
#     print("World")



# # 事件循环（Event Loop）事件循环是 asyncio 的核心组件，负责调度和执行协程。它不断地检查是否有任务需要执行，并在任务完成后调用相应的回调函数
# async def main():
#     await say_hello()

# asyncio.run(main())

# # 任务（Task）任务是对协程的封装，表示一个正在执行或将要执行的协程。可以通过 asyncio.create_task() 函数创建任务，并将其添加到事件循环中。
# async def main():
#     task = asyncio.create_task(say_hello())
#     await task

# # Future Future 是一个表示异步操作结果的对象。它通常用于底层 API，表示一个尚未完成的操作。你可以通过 await 关键字等待 Future 完成。
# async def main():
#     future = asyncio.Future()
#     await future

# # 同步版本
# import time
# import requests

# def fetch_url(url):
#     """模拟一个耗时的网络请求（同步版本）"""
#     print(f"开始获取: {url}")
#     time.sleep(2)  # 模拟 2 秒网络延迟
#     print(f"完成获取: {url}")
#     return f"来自 {url} 的数据"

# def main_syncTB():
#     urls = ['https://example.com/1', 'https://example.com/2', 'https://example.com/3']
#     results = []
#     start = time.time()
    
#     for url in urls:
#         result = fetch_url(url)  # 必须等上一个完成才能开始下一个
#         results.append(result)
    
#     end = time.time()
#     print(f"同步版本总耗时: {end - start:.2f} 秒")
#     print(f"结果: {results}")


# # 异步版本
# import asyncio
# import aiohttp
# import time

# async def fetch_url_async(session, url):
#     """模拟一个耗时的网络请求（异步版本）"""
#     print(f"开始异步获取: {url}")
#     # 注意：这里我们使用 aiohttp 的异步 get 方法，并用 await 等待
#     async with session.get(url) as response:
#         # 模拟处理响应也需要时间
#         await asyncio.sleep(2)  # 使用 asyncio.sleep 模拟 I/O 等待，它不会阻塞线程
#         text = await response.text()
#         print(f"完成异步获取: {url}")
#         return f"来自 {url} 的数据 (长度: {len(text)})"

# async def main_async():
#     urls = ['https://httpbin.org/get', 'https://httpbin.org/delay/1', 'https://httpbin.org/headers']
    
#     async with aiohttp.ClientSession() as session:  # 创建异步 HTTP 会话
#         # 为每个 URL 创建一个任务（Task）
#         tasks = []
#         for url in urls:
#             # create_task 会将协程加入事件循环，立即开始调度
#             task = asyncio.create_task(fetch_url_async(session, url))
#             tasks.append(task)
        
#         print("所有任务已创建，开始并发执行...")
        
#         # 使用 asyncio.gather 并发运行所有任务，并等待它们全部完成
#         # gather 返回一个结果列表，顺序与传入的任务顺序一致
#         results = await asyncio.gather(*tasks)
        
#         return results


# if __name__ == "__main__":
#     main_syncTB()

#     start = time.time()
#     # asyncio.run() 是启动事件循环并运行顶层协程的简便方法
#     final_results = asyncio.run(main_async())
#     end = time.time()
    
#     print(f"\n异步版本总耗时: {end - start:.2f} 秒")
#     for res in final_results:
#         print(res)


# import asyncio

# async def fetch_data(name, delay):
#     print(f"  [{name}] 开始")
#     await asyncio.sleep(delay)          # 模拟网络/磁盘等待
#     print(f"  [{name}] 完成")
#     return f"{name}的数据"

# async def main():
#     # 串行：总耗时 = 0.3 + 0.2 = 0.5s
#     a = await fetch_data("A", 0.3)
#     b = await fetch_data("B", 0.2)
#     print("结果:", a, b)

# # 需要await
# async def bad():
#     fetch_data("A", 0.1)   # 没 await，没执行，只拿到一个没用的 coroutine 对象
#     # Python 会发 RuntimeWarning: coroutine 'fetch_data' was never awaited


# # asyncio.run(main()) 

# import time

# async def main():
#     # 并发：两个任务同时跑，总耗时 ≈ max(A, B)
#     start = time.time()
#     t1=asyncio.create_task(fetch_data("A",3))
#     t2=asyncio.create_task(fetch_data("B",5))
#     a = await t1
#     b = await t2
#     end = time.time()
#     print("结果",a,b)
#     print(f"\n总耗时: {end - start:.2f} 秒")

# # asyncio.run(main())


# import asyncio

# async def fetch(name, delay):
#     await asyncio.sleep(delay)
#     return f"{name}({delay})"

# async def main():
#     # 并发跑 3 个，结果顺序与传入顺序一致
#     results = await asyncio.gather(
#         fetch("A", 0.3),
#         fetch("B", 0.1),
#         fetch("C", 0.2),
#     )
#     print(results)  # ['A(0.3)', 'B(0.1)', 'C(0.2)']

#     results = await asyncio.gather(fetch("A", 0.1), boom(), return_exceptions=True)
#     for r in results:
#         if isinstance(r, Exception):
#             print("某个任务失败:", r)
#         else:
#             print("成功:", r)

# async def boom():
#     raise ValueError("炸了")

# # asyncio.run(main())

# async def main():
#     tasks = [asyncio.create_task(fetch(c, d)) for c, d in [("A", 0.3), ("B", 0.1)]]
#     done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
#     print("done:",done)
#     print("pending:",pending)
#     print("最先完成的:", [t.result() for t in done])
#     for t in pending:
#         t.cancel()   # 不再等其余的，取消掉

# asyncio.run(main())


"""
`asyncio.Queue` 是生产者-消费者桥梁。`EventStream` 正是用它存事件。

| 方法 | 行为 |
|------|------|
| `await queue.put(x)` | 放入元素；无界队列不阻塞，有界队列满时阻塞 |
| `queue.put_nowait(x)` | **同步**放入，不阻塞；满了抛 `QueueFull` |
| `await queue.get()` | 取出元素；空时阻塞等待 |
| `queue.get_nowait()` | 同步取出；空时抛 `QueueEmpty` |
| `queue.empty()` | 是否空（快照） |
"""

# import asyncio

# async def producer(queue):
#     for i in range(3):
#         await asyncio.sleep(0.1)
#         print(f"生产 {i}")
#         await queue.put(i)
#     await queue.put(None)   # 哨兵：表示结束

# async def consumer(queue):
#     while True:
#         item = await queue.get()
#         if item is None:    # 收到哨兵，退出
#             break
#         print(f"  消费 {item}")

# async def main():
#     queue = asyncio.Queue()
#     await asyncio.gather(producer(queue), consumer(queue))

# asyncio.run(main())

# """
# put_nowait：同步入队（关键）
# EventStream.push() 是同步方法，它用 put_nowait 立即把事件塞进队列，
# 不等任何东西。这让"先同步填满、后异步消费"成为可能：event_stream.py 
# 的 __anext__ 先用 get_nowait 把已入队的事件立刻吐出，
# 空了才 await queue.get() 阻塞等生产者。
# 这就是 EventStream "同步返回、异步填充"能 drain 的原因。
# """

# import asyncio
# async def main():
#     queue: asyncio.Queue[int | None] = asyncio.Queue()
#     # 在不 await 的情况下连续塞入
#     for i in range(3):
#         queue.put_nowait(i)
#     queue.put_nowait(None)
#     # 之后异步消费
#     while True:
#         x = queue.get_nowait() if not queue.empty() else await queue.get()
#         if x is None:
#             break
#         print(x)
# asyncio.run(main())


"""
asyncio.Event：事件信号（result 就绪与 AbortSignal）

`asyncio.Event` 是一个"开关"：内部一个布尔标志。

| 方法 | 行为 |
|------|------|
| `event.set()` | 置为已触发；所有 `wait()` 的协程立即被唤醒 |
| `event.clear()` | 复位为未触发 |
| `event.is_set()` | 当前是否已触发 |
| `await event.wait()` | 未触发时阻塞，直到 `set()` |
"""

# import asyncio
# async def waiter(event):
#     print("等待中...")
#     await event.wait()
#     print("被唤醒了")

# async def setter(event):
#     await asyncio.sleep(0.2)
#     print("触发！")
#     event.set()

# async def main():
#     event = asyncio.Event()
#     await asyncio.gather(waiter(event), setter(event))

# asyncio.run(main())

# 用 Event 实现"结果就绪"
# 这是 EventStream.result() 的核心模式：用 Event 当"结果准备好了"的门铃，用一个变量存结果本身。



# import asyncio

# class AsyncResult:
#     def __init__(self):
#         self._event = asyncio.Event()
#         self._value = None

#     def set_value(self, value):        # 同步设置
#         self._value = value
#         self._event.set()

#     async def get(self):               # 异步等待
#         await self._event.wait()
#         return self._value

# async def main():
#     r = AsyncResult()

#     async def producer():
#         await asyncio.sleep(1)
#         r.set_value(42)                # 任意时刻同步设置

#     async def consumer():
#         val = await r.get()            # 阻塞直到被设置
#         print("拿到:", val)

#     await asyncio.gather(consumer(), producer())

# asyncio.run(main())


# import asyncio

# class AsyncRange:
#     def __init__(self, n):
#         self.n = n
#         self.i = 0

#     def __aiter__(self):          # 返回异步迭代器（通常返回 self）
#         return self

#     async def __anext__(self):
#         if self.i >= self.n:
#             raise StopAsyncIteration   # 结束信号
#         await asyncio.sleep(0.05)      # 可在此 await！
#         value = self.i
#         self.i += 1
#         return value

# async def main():
#     async for x in AsyncRange(3):
#         print(x)

# asyncio.run(main())

"""
async for x in AsyncRange(3) 的执行流程

  async for 是 Python 对异步迭代协议的语法糖，等价于以下手动写法：

  iter_obj = AsyncRange(3)          # ① 创建 AsyncRange 实例
  aiter = iter_obj.__aiter__()      # ② 调用 __aiter__()，获取异步迭代器（这里返回 self）
  try:
      while True:
          x = await aiter.__anext__()  # ③ 每次循环 await 调用 __anext__()
          print(x)                      # ④ 使用返回值
  except StopAsyncIteration:            # ⑤ __anext__ 抛出 StopAsyncIteration 时循环结束
      pass
"""


# EventStream.__anext__ 的精髓：先同步 get_nowait 吐已有事件，没有才 await queue.get() 阻塞。简化版：


"""
  __aiter__：定义入口

  def __aiter__(self):
      return self   # 返回一个拥有 __anext__ 方法的对象

  - 当你写 async for x in obj 时，Python 首先调用 obj.__aiter__()
  - 返回的对象必须拥有 __anext__ 方法——这个对象就是"异步迭代器"
  - 通常直接返回 self（因为类本身就实现了 __anext__）

__anext__：产出每个值
  async def __anext__(self):   # 注意：必须是 async def
      if self.i >= self.n:
          raise StopAsyncIteration   # 结束信号
      value = self.i
      self.i += 1
      return value                 # 每次迭代拿到的值

  - 必须是 async def——因为调用方会用 await 来获取返回值
  - 每次循环执行一次，return 的值就是 async for 中 x 拿到的值
  - 没有更多值时，抛出 StopAsyncIteration 终止循环
"""


# import asyncio

# class Stream:
#     def __init__(self):
#         self._queue: asyncio.Queue[int | None] = asyncio.Queue()
#         self._done = False

#     def push(self, x):
#         self._queue.put_nowait(x)

#     def end(self):
#         self._done = True
#         self._queue.put_nowait(None)   # 哨兵

#     def __aiter__(self):
#         return self

#     async def __anext__(self):
#         if not self._queue.empty():
#             item = self._queue.get_nowait()
#         elif self._done:
#             raise StopAsyncIteration
#         else:
#             item = await self._queue.get()
#         if item is None:
#             raise StopAsyncIteration
#         return item

# async def main():
#     async def producer(s):
#         for i in range(3):
#             await asyncio.sleep(1)  # 每秒产出一个
#             s.push(i)
#         s.end()

#     async def consumer(s):
#         async for x in s:
#             print(x)  # ← 这里会进入 else 分支，await 等待 producer 产出

#     s = Stream()
#     await asyncio.gather(producer(s), consumer(s))

# asyncio.run(main())


"""
asyncio.create_subprocess_shell：子进程（bash 工具）
异步跑 shell 命令，流式读取 stdout/stderr。这是 pi_coding_agent/tools/bash.py 的实现基础
"""

# import asyncio

# async def run_cmd(cmd):
#     proc = await asyncio.create_subprocess_shell(
#         cmd,
#         stdout=asyncio.subprocess.PIPE,
#         stderr=asyncio.subprocess.PIPE,
#     )
#     # communicate() 等子进程结束并一次性拿回全部输出
#     stdout, stderr = await proc.communicate()
#     return proc.returncode, stdout.decode(), stderr.decode()

# async def main():
#     code, out, err = await run_cmd("echo hello && echo world")
#     print("exit:", code)
#     print("out:", out.strip())

# asyncio.run(main())

"""
流式逐行读（实时输出，不等结束）
bash 工具要实时把输出回传给 agent，用 proc.stdout 按行异步读：
"""

# import asyncio

# async def stream_cmd(cmd):
#     proc = await asyncio.create_subprocess_shell(
#         cmd,
#         stdout=asyncio.subprocess.PIPE,
#     )
#     assert proc.stdout is not None
#     # async for 直接逐行读子进程输出
#     async for line in proc.stdout:
#         print("行:", line.decode().rstrip())
#     await proc.wait()   # 等退出码

# async def main():
#     await stream_cmd("echo a; echo b; echo c")

# asyncio.run(main())

"""
communicate() 可配合 asyncio.wait_for 设超时；超时后应 kill 子进程，避免僵尸：
源码关联：bash 工具的 execute() 接收 AbortSignal，
在循环里 await signal.wait() 或 await asyncio.wait_for 来响应取消
一旦 abort 就 proc.kill()
"""

# import asyncio
# async def run_with_timeout(cmd, timeout):
#     proc = await asyncio.create_subprocess_shell(
#         cmd, stdout=asyncio.subprocess.PIPE
#     )
#     try:
#         stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
#         return stdout.decode()
#     except asyncio.TimeoutError:
#         proc.kill()                       # 杀掉子进程
#         await proc.wait()
#         raise

# async def main():
#     try:
#           await run_with_timeout("python -c \"import time; time.sleep(5)\"", timeout=0.3)
#     except asyncio.TimeoutError:
#         print("超时，已 kill")

# asyncio.run(main())

"""
asyncio.wait_for：给任意 awaitable 加超时，Task.cancel：主动取消
"""

# import asyncio
# import time

# async def slow():
#     await asyncio.sleep(10)
#     return "done"

# async def main():
#     try:
#         await asyncio.wait_for(slow(), timeout=0.2)
#     except asyncio.TimeoutError:
#         print("超时")

# asyncio.run(main())

# import asyncio

# async def long_task():
#     try:
#         print("干活中...")
#         await asyncio.sleep(10)
#         print("完成")            # 不会执行
#     except asyncio.CancelledError:
#         print("被取消，清理资源")
#         raise                     # 惯例：重新抛出，让调用方感知

# async def main():
#     t = asyncio.create_task(long_task())
#     await asyncio.sleep(0.1)
#     t.cancel()                    # 请求取消
#     try:
#         await t
#     except asyncio.CancelledError:
#         print("main 感知到取消")

# asyncio.run(main())

"""
用 asyncio 复现最小 EventStream
生产者（push）是同步的，保证填入数据时不被中断；消费者（__anext__
  、result）是异步的，在没数据时安静等待，不浪费 CPU
"""

# import asyncio

# class EventStream:
#     """最小版：同步 push 入队，async for 消费，result() 等终态结果。"""

#     def __init__(self, is_complete, extract_result):
#         self._is_complete = is_complete
#         self._extract_result = extract_result
#         self._queue: asyncio.Queue = asyncio.Queue()       # 存事件
#         self._done = False
#         self._result_event = asyncio.Event()               # 结果就绪门铃
#         self._result = None
#         self._has_result = False

#     # ---- 生产者（同步）----

#     def push(self, event):
#         if self._done:
#             return
#         if self._is_complete(event):
#             self._done = True
#             self._set_result(self._extract_result(event))
#         self._queue.put_nowait(event)          # 同步入队，不阻塞

#     def end(self, result=None):
#         self._done = True
#         if result is not None:
#             self._set_result(result)
#         self._queue.put_nowait(None)           # 哨兵结束迭代

#     # ---- 结果 ----

# # _set_result是幂等的结果设置器，负责将终态结果写入 EventStream 并通知所有等待者
#     def _set_result(self, result):
#         # 由于push和end都可能调用_set_result，需要先判断是否被调用过，如果被调用则跳过
#         if self._has_result:
#             return
#         self._result = result
#         self._has_result = True
#         self._result_event.set()               # 唤醒所有 result() 等待者

#     async def result(self):
#         await self._result_event.wait()     # ① 挂起，等结果就绪（如果_result_event.set() 还没被调用，即未到终态事件，则挂起）
#         return self._result                 # ② 返回已存储的结果（由 _set_result 写入的值） 

#     # ---- 消费者（异步迭代）----

#     def __aiter__(self):
#         return self

#     async def __anext__(self):
#         # 先吐已入队的，避免同步填满的流无谓阻塞
#         if not self._queue.empty():
#             event = self._queue.get_nowait()
#         elif self._done:
#             raise StopAsyncIteration
#         else:
#             event = await self._queue.get()    # 等生产者 push
#         if event is None:
#             raise StopAsyncIteration
#         return event


# # ---- 演示 ----

# async def fake_provider(stream):
#     """模拟一个 provider：同步 push 一串事件。"""
#     await asyncio.sleep(0.05)                  # 假装在连 auth
#     for i in range(3):
#         stream.push({"type": "text_delta", "delta": str(i)})
#     stream.push({"type": "done", "value": "最终结果"})   # 终态事件


# async def main():
#     # is_complete是判断函数，当前是否是终点
#     # extract_result是提取函数
#     stream = EventStream(
#         is_complete=lambda ev: ev["type"] == "done",
#         extract_result=lambda ev: ev["value"],
#     )

#     # 后台跑生产者
#     asyncio.create_task(fake_provider(stream))

#     # 消费者：边迭代边打印
#     async for ev in stream:
#         print("事件:", ev)

#     # 拿最终结果（done 已 set，立即返回）
#     print("结果:", await stream.result())

# asyncio.run(main())

"""
补一个 AbortSignal，演示协作式取消如何让一个长生产者提前 end()：
"""

# import asyncio

# class EventStream:
#     """最小版：同步 push 入队，async for 消费，result() 等终态结果。"""

#     def __init__(self, is_complete, extract_result):
#         self._is_complete = is_complete
#         self._extract_result = extract_result
#         self._queue: asyncio.Queue = asyncio.Queue()       # 存事件
#         self._done = False
#         self._result_event = asyncio.Event()               # 结果就绪门铃
#         self._result = None
#         self._has_result = False

#     # ---- 生产者（同步）----

#     def push(self, event):
#         if self._done:
#             return
#         if self._is_complete(event):
#             self._done = True
#             self._set_result(self._extract_result(event))
#         self._queue.put_nowait(event)          # 同步入队，不阻塞

#     def end(self, result=None):
#         self._done = True
#         if result is not None:
#             self._set_result(result)
#         self._queue.put_nowait(None)           # 哨兵结束迭代

#     # ---- 结果 ----

# # _set_result是幂等的结果设置器，负责将终态结果写入 EventStream 并通知所有等待者
#     def _set_result(self, result):
#         # 由于push和end都可能调用_set_result，需要先判断是否被调用过，如果被调用则跳过
#         if self._has_result:
#             return
#         self._result = result
#         self._has_result = True
#         self._result_event.set()               # 唤醒所有 result() 等待者

#     async def result(self):
#         await self._result_event.wait()     # ① 挂起，等结果就绪（如果_result_event.set() 还没被调用，即未到终态事件，则挂起）
#         return self._result                 # ② 返回已存储的结果（由 _set_result 写入的值） 

#     # ---- 消费者（异步迭代）----

#     def __aiter__(self):
#         return self

#     async def __anext__(self):
#         # 先吐已入队的，避免同步填满的流无谓阻塞
#         if not self._queue.empty():
#             event = self._queue.get_nowait()
#         elif self._done:
#             raise StopAsyncIteration
#         else:
#             event = await self._queue.get()    # 等生产者 push
#         if event is None:
#             raise StopAsyncIteration
#         return event


# class AbortSignal:
#     def __init__(self):
#         self._event = asyncio.Event()

#     """
#     没有 @property 时：
#     if signal.aborted():          # 需要加括号调用
#         ...
#     print(signal.aborted())       # 看起来像函数调用

#     有 @property 时：
#     if signal.aborted:            # 直接读取，像访问普通属性
#         ...
#     print(signal.aborted)         # 语法更直观
#     """
#     @property   #将 aborted 从一个方法调用变成一个属性访问，使语法更自然：
#     def aborted(self):
#         return self._event.is_set()

#     def abort(self):
#         self._event.set()

#     async def wait(self):
#         await self._event.wait()


# async def producer_until_abort(stream, signal):
#     for i in range(100):
#         if signal.aborted:                      # 协作式检查
#             stream.end(result="aborted")
#             return
#         stream.push({"type": "tick", "i": i})
#         await asyncio.sleep(0.05)


# async def main():
#     queue: asyncio.Queue = asyncio.Queue()
#     signal = AbortSignal()

#     # 简化：复用上面的 EventStream 思路，这里只演示信号
#     async def cancel_after():
#         await asyncio.sleep(0.12)
#         signal.abort()
#         print(">> 触发 abort")

#     asyncio.create_task(cancel_after())
#     stream = EventStream(
#         is_complete=lambda ev: ev["type"] == "done",
#         extract_result=lambda ev: ev["value"],
#     )
#     await producer_until_abort(stream, signal)    # 传入真实 stream 即可
#     print("aborted?", signal.aborted)

# asyncio.run(main())


# 练习清单
"""
1. 写一个 AsyncCounter(n) 异步迭代器，每 async for 产出一个数并 sleep(0.01)
"""
# import asyncio

# class AsyncCounter:
#     def __init__(self,n:int):
#         self.n=n
#         self.i=0
    
#     def __aiter__(self):
#         return self

#     async def __anext__(self):
#         if self.i>self.n:
#             raise StopAsyncIteration
#         else:
#             await asyncio.sleep(1)
#             value=self.i
#             self.i+=1
#             return value
    

# async def count_number():
#       print("counter 开始")
#       async for x in AsyncCounter(10):
#           print(f"  counter: {x}")
#       print("counter 结束")

# # 并发任务
# async def heartbeat():
#     """并发任务：每 0.5 秒打印一次心跳"""
#     for i in range(10):
#         await asyncio.sleep(1)
#         print(f"♥ heartbeat #{i}")

# async def main():
#     await asyncio.gather(count_number(),heartbeat())

# if __name__=="__main__":
#     asyncio.run(main())
        
"""
2. 用 asyncio.Queue 写一个"3 生产者 + 1 消费者"程序，生产者结束后消费者自动退出（哨兵计数）
"""

# import asyncio
# async def produce(queue):
#     for i in range(3):
#         await asyncio.sleep(0.5)
#         print(f"生产：{i}")
#         await queue.put(i)
#     await queue.put(None) #哨兵

# async def consumer(queue):
#     while True:
#         item=await queue.get()    
#         if item is None:
#             break
#         print(f"消费: {item}")

# async def main():
#     queue=asyncio.Queue()
#     await asyncio.gather(produce(queue),consumer(queue))

# asyncio.run(main())

"""
3. 用 asyncio.Event 实现一个 Latch：wait() 阻塞直到 release()，可被多个协程同时等待。
"""

# import asyncio

# # Latch 实际为一次性的asynico.Event
# class Latch:
#     def __init__(self):
#         self._event=asyncio.Event()
    
#     def release(self):
#         self._event.set()
    
#     async def wait(self):
#         await self._event.wait()
    
#     @property
#     def is_released(self):
#         return self._event.is_set()

# async def worker(latch,wid):
#       print(f"  worker{wid}: 等待门闩...")
#       await latch.wait()
#       print(f"  worker{wid}: 门闩已开，开始工作")


# async def main():
#     latch=Latch()

#     workers=[asyncio.create_task(worker(latch,i)) for i in range(5)]

#     await asyncio.sleep(2)
#     print(">> release!")
#     latch.release()

#     await asyncio.gather(*workers)
#     print("全部完成")

# asyncio.run(main())

"""
4.写一个 timeout_async(coro, t) 函数：用 wait_for，超时返回 None 而非抛异常
"""
# import asyncio

# async def timeout_async(coro,t):
#     try:
#         return await asyncio.wait_for(coro,timeout=t)
#     except asyncio.TimeoutError:
#         return None

# async def slow_task():
#     await asyncio.sleep(5)
#     return "done"

# async def fast_task():
#     await asyncio.sleep(1)
#     return "done"

# async def main():
#     result1 = await timeout_async(slow_task(),2)
#     print(f"slow_task:{result1}")
#     result2 = await timeout_async(fast_task(),5)
#     print(f"fast_task:{result2}")

# asyncio.run(main())


"""
5. 用 create_subprocess_shell 跑 ping -n 4 127.0.0.1，实时逐行打印，并在 1 秒后强制 kill。
(
注：Windows不能使用create_subprocess_shell:
create_subprocess_shell 在 Windows 上实际上创建了一个 cmd.exe 进程来执行你的命令。关系链是：

  你的Python父进程
    └─ cmd.exe (proc 真正引用的进程)
         └─ python子进程 (实际执行 tick 循环的进程)

  当你调用 proc.kill() 时，它只 TerminateProcess 了 cmd.exe，而真正运行的 Python
  子进程不受影响——它继承了 stdout pipe，继续往里写数据。

  所以你看到了：
  1. >> 1秒到了 —— cmd.exe 被 kill 了
  2. >> 进程退出码: 1 —— proc.wait() 等的是 cmd.exe，它确实已退出
  3. 但 tick 2 ~ tick 19 全都还在输出 —— 因为 Python 子进程还在跑
  )
"""
# import asyncio
# import sys

# # python -u 强制无缓冲
# async def ping_with_kill():
#     proc = await asyncio.create_subprocess_exec(
#           sys.executable,  # ← 用当前正在运行的 Python 解释器的完整路径
#           "-u", "-c",
#           "import time; [print(f'tick {i}') or time.sleep(0.5) for i in range(20)]",
#           stdout=asyncio.subprocess.PIPE,
#       )

#     assert proc.stdout is not None

#     async def read_lines():
#         async for line in proc.stdout:
#             print("line:",line.decode("gbk").strip())
    
#     async def kill_after(delay):
#         await asyncio.sleep(delay)
#         print(f">> {delay}秒到了，准备 kill，进程状态: running={proc.returncode is None}")
#         proc.kill()
#         await proc.wait()
#         print(f">> 进程退出码: {proc.returncode}")
    
#     reader=asyncio.create_task(read_lines())
#     killer=asyncio.create_task(kill_after(1))

#     done, pending = await asyncio.wait(
#         [reader, killer],
#         return_when=asyncio.ALL_COMPLETED,
#     )

#     # kill 触发后 reader 会因 stdout 关闭而自然结束，但保险起见取消
#     for t in pending:
#         t.cancel()

# asyncio.run(ping_with_kill())

"""
6. 把之前的 EventStream 扩展成泛型 EventStream[TEvent, TResult]，再加一个 AbortSignal 让 fake_provider 可被取消
"""
import asyncio
from typing import TypeVar,Generic,Callable

TEvent=TypeVar("TEvent")
TResult=TypeVar("TResult")
"""本质：泛型不改变运行行为，只让类型系统理解"这个类的某个方法返回什么，取决于创建时传入什么类型参数"""

class EventStream(Generic[TEvent,TResult]):
    def __init__(
            self, 
            is_complete:Callable[[TEvent],bool], 
            extract_result:Callable[[TEvent],TResult],
            )->None:
        #传入参数：1.当前流是否结束；2.提取最后流的值
        self._is_complete=is_complete
        self._extract_result=extract_result
        #容器
        self._result=None
        self._queue=asyncio.Queue()
        #门铃
        self._result_event=asyncio.Event()
        #flag
        self._done=False
        self._has_result=False
    
    # 生产者，同步放入
    def push(self,event:TEvent)->None:
        if self._done:
            return
        if self._is_complete(event):
            self._done=True
            self._set_result(self._extract_result(event))
        self._queue.put_nowait(event)
        # 终止事件入队后紧接着插入哨兵，让消费者无需额外等待
        if self._done:
              self._queue.put_nowait(None)
    
    # 插入哨兵
    def end(self,result:TResult|None=None)->None:
        self._done=True
        if result is not None:
             self._set_result(result)
        self._queue.put_nowait(None)

    """
    为什么 push 使用 self._is_complete(event) 判断，
    而 end 中使用 result 非空判断？

    push 处理的是「流中的事件」——只有当事件本身携带了足够的信息
    （is_complete 判定 + extract_result 提取）才能解析结果；
    事件可能只是中间片段，没有结果可提取。

    end 处理的是「外部强制终止」——调用方已经知道流该结束了，
    并且可能已经手握一个现成的 result，直接传入即可；
    如果调用方没有结果（比如异常中止），传 None 只停迭代、不解析结果。
    """

    # 输出最终结果
    def _set_result(self,result:TResult):
        # 如果已经调用过，直接跳过
        if self._has_result:
            return
        self._result=result
        self._has_result=True
        self._result_event.set()  # 唤醒所有 result() 等待者
    
    async def result(self)->TResult:
        # 先挂起，待唤醒后返回result
        await self._result_event.wait()
        assert self._result is not None
        return self._result

    # ---- 消费者（异步迭代）----
    def __aiter__(self)->EventStream[TEvent,TResult]:
        return self

    async def __anext__(self)->TEvent:
        # 先吐已入队的，避免同步填满的流无谓阻塞
        if not self._queue.empty():
            event=self._queue.get_nowait()
        elif self._done:
            raise StopAsyncIteration
        else:
            event=await self._queue.get() #等待生产者
        if event is None:
            raise StopAsyncIteration
        return event

class AbortSignal:
    """轻量取消信号，模拟 Web AbortSignal 的核心语义。"""

    def __init__(self)->None:
        self._event=asyncio.Event()
    
    @property
    def aborted(self)->bool:
        # 是否已经取消
        return self._event.is_set()

    def abort(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def is_set(self) -> bool:
        return self._event.is_set()



# # 演示
# async def fake_provider(stream,signal):
#     """模拟一个 provider：同步 push 一串事件。"""
#     await asyncio.sleep(2) #假装在连 auth
#     for i in range(3):
#         stream.push({"type": "text_delta", "delta": str(i)})
#     stream.push({"type": "done", "value": "最终结果"})   # 终态事件

# async def main():
#     stream=EventStream(
#         is_complete=lambda ev:ev["type"]=="done",
#         extract_result=lambda ev:ev["value"]
#     )

#     # 后台跑生产者
#     asyncio.create_task(fake_provider(stream)) 

#         # 消费者：边迭代边打印
#     async for ev in stream:
#         print("事件:", ev)

#     # 拿最终结果（done 已 set，立即返回）
#     print("结果:", await stream.result())

# asyncio.run(main())

# ---- 演示用事件类型（dict，便于练习；源码用 TypedDict/dataclass）----

Event = dict[str, object]

async def fake_provider(
    stream: EventStream[Event, str],
    signal: AbortSignal | None = None,
    n: int = 3,
) -> None:
    """模拟 provider：推送 text_delta；若收到 abort 则 end(aborted)。"""
    await asyncio.sleep(0.05)
    for i in range(n):
        if signal is not None and signal.aborted:
            stream.end(result="aborted")
            return
        stream.push({"type": "text_delta", "delta": str(i)})
        await asyncio.sleep(0.05)
    stream.push({"type": "done", "value": "最终结果"})


async def demo_normal() -> None:
    print("=== 正常完成 ===")
    stream: EventStream[Event, str] = EventStream(
        is_complete=lambda ev: ev["type"] == "done",
        extract_result=lambda ev: str(ev["value"]),
    )
    asyncio.create_task(fake_provider(stream, n=3))
    async for ev in stream:
        print("事件:", ev)
    print("结果:", await stream.result())


async def demo_abort() -> None:
    print("\n=== AbortSignal 取消 ===")
    stream: EventStream[Event, str] = EventStream(
        is_complete=lambda ev: ev["type"] == "done",
        extract_result=lambda ev: str(ev["value"]),
    )
    signal = AbortSignal()

    async def cancel_after() -> None:
        await asyncio.sleep(0.12)
        signal.abort()
        print(">> 触发 abort")

    asyncio.create_task(cancel_after())
    # n 很大，正常跑不完；0.12s 后 abort，只吐出部分 delta
    asyncio.create_task(fake_provider(stream, signal, n=100))

    async for ev in stream:
        print("事件:", ev)
    print("结果:", await stream.result())
    print("aborted?", signal.aborted)


async def main() -> None:
    await demo_normal()
    await demo_abort()


if __name__ == "__main__":
    asyncio.run(main())
    