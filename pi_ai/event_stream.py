"""异步事件流
Python 移植自：packages/ai/src/utils/event-stream.ts
================================================================================
设计目标（与 TS 一致）
================================================================================
EventStream 实现「同步返回、异步填充」：
- push(event) 是同步方法：立即入队；若事件是终态（is_complete），同时 resolve 最终结果。
- end(result?) 终止迭代；可选地 resolve 结果（异常终止、无 complete 事件时用）。
- async for / async 迭代产出事件，直到流结束。
- result() 等待并返回最终结果（由 complete 事件或 end() 设置）。

典型用法（lazyStream 模式）：
1. 同步构造 stream = AssistantMessageEventStream() 并立刻返回给调用方；
2. 后台 task 异步拿 auth、连 provider，边收 SSE 边 stream.push(...)；
3. 调用方 async for ev in stream 消费；也可 await stream.result() 只拿终态。

本文件相对 TS 的额外内容
================================================================================
AbortSignal：不在 event-stream.ts 中。Python 版放在此处，供 StreamOptions.signal、
Agent.abort()、bash 工具等做协作式取消（检查 aborted / await wait()）。
TS 侧通常直接使用 DOM/Node 的 AbortSignal，或在 agent 层另封装。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Generic, TypeVar

from pi_ai.types import AssistantMessage, AssistantMessageEvent

# 对应 TS：EventStream<T, R = T>
TEvent = TypeVar("TEvent")  # T：流上的事件类型
TResult = TypeVar("TResult")  # R：终态结果类型（可与 T 不同）


class AbortSignal:
    """协作式取消信号（Python 侧补充；非 event-stream.ts 内容）。

    类比浏览器 / Node 的 AbortSignal：
    - abort()     → 触发取消（内部 asyncio.Event.set）
    - aborted     → 是否已取消（is_set）
    - await wait()→ 阻塞直到被 abort

    使用方在循环里主动检查，例如::

        if signal.aborted:
            stream.end(result=...)
            return

    这是协作式取消，不会自动 cancel Task；与 asyncio.Task.cancel() 互补。
    """

    def __init__(self) -> None:
        # 内部只用一个 Event 当开关；set 后所有 wait() 立即返回
        self._event = asyncio.Event()

    @property
    def aborted(self) -> bool:
        """对应 DOM AbortSignal.aborted。"""
        return self._event.is_set()

    def abort(self) -> None:
        """对应 AbortController.abort()。可安全多次调用（Event.set 幂等）。"""
        self._event.set()

    async def wait(self) -> None:
        """挂起直到 abort()；已 abort 则立即返回。"""
        await self._event.wait()

    def is_set(self) -> bool:
        """与 aborted 同义，便于与裸 asyncio.Event 接口对齐。"""
        return self._event.is_set()


class EventStream(Generic[TEvent, TResult]):
    """通用推送式异步事件流，带最终结果。

    对应 TS：
        export class EventStream<T, R = T> implements AsyncIterable<T>

    Args:
        is_complete: 判断事件是否为「携带最终结果」的终态事件。
            对应构造参数 isComplete。(例：done / error)
        extract_result: 从终态事件取出结果值。
            对应构造参数 extractResult。
    """

    def __init__(
        self,
        is_complete: Callable[[TEvent], bool],
        extract_result: Callable[[TEvent], TResult],
    ) -> None:
        # ---- 对应 TS constructor(isComplete, extractResult) ----
        self._is_complete = is_complete  # TS: this.isComplete
        self._extract_result = extract_result  # TS: this.extractResult

        # 事件缓冲。TS 用 queue: T[] + waiting[]；这里用 Queue 合并两者。
        # None 哨兵：仅由 end() 放入，表示「没有更多事件」（TS end 发 done:true）。
        # 注意：终态业务事件（如 done）会正常 push 进队列被消费者读到，
        # 然后因 _done=True，下一次 __anext__ 在空队列时 StopAsyncIteration。
        # 因此「complete 事件」路径通常不需要再放 None；end() 才用哨兵。
        self._queue: asyncio.Queue[TEvent | None] = asyncio.Queue()

        self._done = False  # TS: private done = false

        # 结果就绪机制。TS：
        #   this.finalResultPromise = new Promise(resolve => {
        #     this.resolveFinalResult = resolve;
        #   });
        # Python：Event 当门铃 + 变量存值；_has_result 模拟 Promise 只 resolve 一次。
        self._result_event = asyncio.Event()
        self._result: TResult | None = None
        self._has_result = False

    # -- 生产者侧（均为同步方法，对应 TS push / end）----------------------

    def push(self, event: TEvent) -> None:
        """同步入队一个事件。

        对应 TS push(event: T): void

        流程（与 TS 相同）：
        1. 若已 done，直接忽略（防重复 push）。
        2. 若 is_complete(event)：置 done，并 resolve 最终结果。
        3. 把事件交给消费者：
           - TS：有 waiting 则直送 waiter，否则 queue.push
           - Python：一律 put_nowait；若有人在 await get()，Queue 会唤醒他

        终态事件本身仍会入队，因此 async for 能收到最后的 done/error 事件，
        然后迭代结束（见 __anext__）。
        """
        if self._done:
            return
        if self._is_complete(event):
            self._done = True
            # TS: this.resolveFinalResult(this.extractResult(event))
            self._set_result(self._extract_result(event))
        # TS: waiter ? waiter({value:event, done:false}) : this.queue.push(event)
        self._queue.put_nowait(event)

    def end(self, result: TResult | None = None) -> None:
        """终止迭代；可选设置最终结果。

        对应 TS end(result?: R): void

        用于异常终止（用户 abort、网络错误等）且没有产出 complete 事件的情况。
        - TS：while waiting → waiter({done:true})
        - Python：put_nowait(None)，__anext__ 见到 None 则 StopAsyncIteration

        注意：Python 用 ``result is not None`` 判断是否设置结果；
        因此无法用 end(None) 表达「结果就是 None」。
        TS 用 ``result !== undefined``，可选参数未传与显式 undefined 同为「不设置」。
        若将来需要「结果可为 None」，应改成 sentinel 或单独的 has_result 参数。
        """
        self._done = True
        if result is not None:
            # TS: if (result !== undefined) this.resolveFinalResult(result)
            self._set_result(result)
        # TS: 唤醒所有 waiting 并 done:true；此处用哨兵等价通知一个（及后续）消费者
        self._queue.put_nowait(None)

    # -- 结果 -----------------------------------------------------------

    def _set_result(self, result: TResult) -> None:
        """内部：只设置一次最终结果并唤醒 result() 等待者。

        TS 的 Promise resolve 多次无效（只第一次生效）；
        这里用 _has_result 显式保证同样语义。
        """
        if self._has_result:
            return
        self._result = result
        self._has_result = True
        self._result_event.set()  # 唤醒所有 await result()；对应 resolveFinalResult

    async def result(self) -> TResult:
        """等待并返回最终结果。

        对应 TS result(): Promise<R>（直接返回已存在的 finalResultPromise）。

        结果来源：
        - push 到 complete 事件时 extract_result；或
        - end(result=...) 显式传入。

        与是否已经 async for 耗尽事件无关：只要结果已 set，这里立即返回。
        """
        await self._result_event.wait()
        assert self._result is not None
        return self._result

    # -- 消费者侧（异步迭代）--------------------------------------------

    def __aiter__(self) -> EventStream[TEvent, TResult]:
        """返回异步迭代器自身。

        TS 通过 implements AsyncIterable<T> + [Symbol.asyncIterator] 实现；
        Python 协议是 __aiter__ 返回实现了 __anext__ 的对象。
        """
        return self

    async def __anext__(self) -> TEvent:
        """产出下一个事件；结束时抛出 StopAsyncIteration。

        对应 TS async *[Symbol.asyncIterator] 生成器循环中的一步::

            while (true) {
              if (this.queue.length > 0) {
                yield this.queue.shift()!;
              } else if (this.done) {
                return;  // 结束迭代
              } else {
                const result = await new Promise(resolve => this.waiting.push(resolve));
                if (result.done) return;
                yield result.value;
              }
            }

        Python 三段式（语义对齐）：
        1. 队列非空 → get_nowait 立刻吐出（含 end 放入的 None 哨兵）。
           这样「同步填满再 async for」不会无谓 await，与 TS 先 shift 队列一致。
        2. 队列空且已 done → 结束（complete 事件已消费完的情况；TS 的 else if done return）。
        3. 否则 await get()，等生产者 push 或 end。
        """
        # ① 先吐已入队的，避免同步预填流在迭代时阻塞
        if not self._queue.empty():
            event = self._queue.get_nowait()
            if event is None:
                # end() 哨兵 → TS waiting 收到 {done:true}
                raise StopAsyncIteration
            return event
        # ② 无缓冲且已结束（例如刚消费完最后一个 done 事件）
        if self._done:
            raise StopAsyncIteration
        # ③ 等待生产者；对应 TS 把 resolve 推入 waiting 再 await
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    """LLM assistant 消息专用事件流。

    对应 TS：
        export class AssistantMessageEventStream
          extends EventStream<AssistantMessageEvent, AssistantMessage>

    终态事件：
    - type == "done"  → 结果为 event.message
    - type == "error" → 结果为 event.error（stop_reason 多为 error/aborted）

    result() 在两种终态下都返回最终的 AssistantMessage。
    """

    def __init__(self) -> None:
        # 对应 TS constructor 里 super(isComplete, extractResult)
        # 继承EventStream类
        super().__init__(
            is_complete=lambda ev: ev.type in ("done", "error"),
            extract_result=_extract_assistant_message,
        )


def _extract_assistant_message(event: AssistantMessageEvent) -> AssistantMessage:
    """从终态 AssistantMessageEvent 提取 AssistantMessage。

    对应 TS AssistantMessageEventStream 构造函数里的内联 extractResult 回调。
    抽成模块级函数便于测试与类型收窄。
    """
    if event.type == "done":
        return event.message
    if event.type == "error":
        return event.error
    raise ValueError(f"Unexpected terminal event type: {event.type}")


def create_assistant_message_event_stream() -> AssistantMessageEventStream:
    """工厂函数，供扩展 / 外部包创建流。

    对应 TS：
        export function createAssistantMessageEventStream(): AssistantMessageEventStream
    """
    return AssistantMessageEventStream()


# 自检代码
if __name__ == "__main__":
    import sys
    # Windows 控制台默认编码常非 UTF-8，避免中文乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from pi_ai.types import (
        CostInfo,
        DoneEvent,
        StartEvent,
        TextContent,
        TextDeltaEvent,
        TextEndEvent,
        TextStartEvent,
        Usage,
    )

    def _ok(name: str) -> None:
        print(f"  PASS  {name}")

    def _make_msg(text: str, *, stop: str = "stop", ts: int = 0) -> AssistantMessage:
        return AssistantMessage(
            content=[TextContent(text=text)] if text else [],
            api="anthropic-messages",
            provider="anthropic",
            model="claude-test",
            usage=Usage(0, 0, 0, 0, 0, CostInfo(0, 0, 0, 0, 0)),
            stop_reason=stop,  # type: ignore[arg-type]
            timestamp=ts,
        )

    def _sync_filled_stream() -> AssistantMessageEventStream:
        """同步填满：start → text_* → done（模拟 provider 已推完）。"""
        stream = create_assistant_message_event_stream()
        empty = _make_msg("")
        stream.push(StartEvent(partial=empty))
        stream.push(TextStartEvent(content_index=0, partial=_make_msg("")))
        stream.push(TextDeltaEvent(content_index=0, delta="H", partial=_make_msg("H")))
        stream.push(TextDeltaEvent(content_index=0, delta="i", partial=_make_msg("Hi")))
        stream.push(TextEndEvent(content_index=0, content="Hi", partial=_make_msg("Hi")))
        final = _make_msg("Hi", ts=1)
        stream.push(DoneEvent(reason="stop", message=final))
        return stream

    async def _case_iterate_and_result() -> None:
        stream = _sync_filled_stream()
        types = [ev.type async for ev in stream]
        assert types == [
            "start",
            "text_start",
            "text_delta",
            "text_delta",
            "text_end",
            "done",
        ]
        msg = await stream.result()
        assert msg.content[0].text == "Hi"  # type: ignore[union-attr]
        _ok("同步填满后 async for + result()")

    async def _case_result_without_drain() -> None:
        stream = _sync_filled_stream()
        msg = await stream.result()  # 不迭代也能拿到结果
        assert msg.content[0].text == "Hi"  # type: ignore[union-attr]
        _ok("不 drain 也能 await result()")

    async def _case_push_after_done_ignored() -> None:
        stream = create_assistant_message_event_stream()
        final = _make_msg("x")
        stream.push(DoneEvent(reason="stop", message=final))
        stream.push(StartEvent(partial=final))  # 应被忽略
        events = [ev async for ev in stream]
        assert len(events) == 1 and events[0].type == "done"
        _ok("done 之后的 push 被忽略")

    async def _case_end_with_abort() -> None:
        stream = create_assistant_message_event_stream()
        aborted = _make_msg("aborted", stop="aborted")
        aborted.error_message = "user aborted"
        stream.push(StartEvent(partial=aborted))
        stream.end(result=aborted)
        types = [ev.type async for ev in stream]
        assert types == ["start"]
        assert await stream.result() is aborted
        _ok("end(result=...) 终止迭代并 resolve")

    async def _case_generic_stream() -> None:
        stream: EventStream[int, int] = EventStream(
            is_complete=lambda ev: ev == 42,
            extract_result=lambda ev: ev * 2,
        )
        stream.push(1)
        stream.push(2)
        stream.push(42)
        assert [ev async for ev in stream] == [1, 2, 42]
        assert await stream.result() == 84
        _ok("泛型 EventStream[int, int]")

    async def _case_async_producer_with_abort() -> None:
        """后台异步 push + AbortSignal 协作取消（对应 lazyStream / Agent.abort）。"""
        stream: EventStream[dict[str, object], str] = EventStream(
            is_complete=lambda ev: ev.get("type") == "done",
            extract_result=lambda ev: str(ev["value"]),
        )
        signal = AbortSignal()

        async def producer() -> None:
            await asyncio.sleep(0.02)
            for i in range(50):
                if signal.aborted:
                    stream.end(result="aborted")
                    return
                stream.push({"type": "tick", "i": i})
                await asyncio.sleep(0.02)
            stream.push({"type": "done", "value": "ok"})

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            signal.abort()

        asyncio.create_task(producer())
        asyncio.create_task(cancel_soon())
        ticks = [ev async for ev in stream]
        assert await stream.result() == "aborted"
        assert signal.aborted
        assert all(ev["type"] == "tick" for ev in ticks)
        assert len(ticks) < 50
        _ok(f"AbortSignal 取消生产者（收到 {len(ticks)} 个 tick）")

    async def _case_abort_signal_wait() -> None:
        sig = AbortSignal()
        assert not sig.aborted

        async def trigger() -> None:
            await asyncio.sleep(0)
            sig.abort()

        asyncio.create_task(trigger())
        await sig.wait()
        assert sig.aborted and sig.is_set()
        _ok("AbortSignal.wait() 被 abort 唤醒")

    async def _main() -> None:
        print("pi_ai.event_stream 自检")
        await _case_iterate_and_result()
        await _case_result_without_drain()
        await _case_push_after_done_ignored()
        await _case_end_with_abort()
        await _case_generic_stream()
        await _case_async_producer_with_abort()
        await _case_abort_signal_wait()
        print("全部通过")

    asyncio.run(_main())