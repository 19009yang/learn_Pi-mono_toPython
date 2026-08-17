"""Production-facing composition layer for Agent, Session, and compaction.

The harness deliberately stays small: ``Agent`` remains responsible for the
runtime loop, while this module owns durable messages, application hooks,
context compaction, and session-tree navigation.
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from pi_agent.agent import Agent
from pi_agent.compaction import (
    CompactionResult,
    CompactionSettings,
    DEFAULT_COMPACTION_SETTINGS,
    compact as compact_messages,
    should_compact,
)
from pi_agent.messages import AgentMessage
from pi_agent.session import Session
from pi_agent.types import (
    AfterToolCallContext,
    AfterToolCallResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
)
from pi_ai.event_stream import AbortSignal
from pi_ai.types import ImageContent


HOOK_NAMES = frozenset(
    {
        "before_agent_start",
        "context",
        "tool_call",
        "tool_result",
        "session_before_flush",
        "session_after_flush",
        "session_before_compact",
        "session_after_compact",
        "session_before_navigate",
        "session_after_navigate",
    }
)


@dataclass(frozen=True)
class BeforeAgentStartContext:
    harness: "AgentHarness"
    input: str | AgentMessage | list[AgentMessage]
    images: list[ImageContent] | None


@dataclass(frozen=True)
class HarnessContext:
    harness: "AgentHarness"
    messages: list[AgentMessage]


@dataclass(frozen=True)
class SessionFlushContext:
    harness: "AgentHarness"
    messages: list[AgentMessage]


@dataclass(frozen=True)
class SessionCompactContext:
    harness: "AgentHarness"
    messages: list[AgentMessage]
    result: CompactionResult | None = None


@dataclass(frozen=True)
class SessionNavigateContext:
    harness: "AgentHarness"
    target_id: str | None
    messages: list[AgentMessage]


@dataclass(frozen=True)
class HarnessRunResult:
    messages: list[AgentMessage]
    new_messages: list[AgentMessage]
    leaf_id: str | None


@dataclass(frozen=True)
class NavigationResult:
    target_id: str | None
    messages: list[AgentMessage]


HookHandler: TypeAlias = Callable[[Any], Any | Awaitable[Any]]


class AgentHarness:
    """Add persistence, hooks, compaction, and navigation around an ``Agent``."""

    def __init__(
        self,
        agent: Agent,
        session: Session,
        *,
        models: Any | None = None,
        compaction_settings: CompactionSettings = DEFAULT_COMPACTION_SETTINGS,
    ) -> None:
        self.agent = agent
        self.session = session
        self.models = models
        self.compaction_settings = compaction_settings
        self._hooks: dict[str, list[HookHandler]] = defaultdict(list)
        self._loaded = False
        self._persisted_message_count = 0
        self._closed = False

        self._agent_before_tool_call = agent.before_tool_call
        self._agent_after_tool_call = agent.after_tool_call
        agent.before_tool_call = self._before_tool_call
        agent.after_tool_call = self._after_tool_call
        self._unsubscribe = agent.subscribe(self._on_agent_event)

    @classmethod
    async def create(
        cls,
        agent: Agent,
        session: Session,
        *,
        models: Any | None = None,
        compaction_settings: CompactionSettings = DEFAULT_COMPACTION_SETTINGS,
    ) -> "AgentHarness":
        """Create a harness and immediately restore its selected session branch."""

        harness = cls(
            agent,
            session,
            models=models,
            compaction_settings=compaction_settings,
        )
        await harness._ensure_loaded()
        return harness

    def on(self, event: str, handler: HookHandler) -> Callable[[], None]:
        """Register an awaited hook and return an idempotent unsubscribe callback."""

        self._ensure_open()
        if event not in HOOK_NAMES:
            raise ValueError(f"Unknown harness hook: {event}")
        handlers = self._hooks[event]
        handlers.append(handler)

        def unsubscribe() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    async def prompt(
        self,
        input: str | AgentMessage | list[AgentMessage],
        images: list[ImageContent] | None = None,
    ) -> HarnessRunResult:
        """Run the agent and durably flush every completed turn."""

        self._ensure_open()
        await self._ensure_loaded()
        await self._emit(
            "before_agent_start",
            BeforeAgentStartContext(self, input, images),
        )
        await self._apply_context_hooks()
        before = len(self.agent.state.messages)
        await self.agent.prompt(input, images)
        # A normal run flushes on turn_end. This final flush also covers a
        # custom/fake Agent that only emits agent_end.
        await self._flush_session()
        generated = list(self.agent.state.messages[before:])
        if (
            self.models is not None
            and should_compact(
                self.agent.state.messages,
                self.agent.state.model,
                self.compaction_settings,
            )
        ):
            await self.compact()
        messages = list(self.agent.state.messages)
        return HarnessRunResult(
            messages=messages,
            new_messages=generated,
            leaf_id=await self.session.get_leaf_id(),
        )

    async def compact(self) -> CompactionResult | None:
        """Compact current context onto a new root while preserving old history."""

        self._ensure_open()
        await self._ensure_loaded()
        if self.agent.state.is_streaming:
            raise RuntimeError("Cannot compact while the agent is running")
        if self.models is None:
            raise RuntimeError("AgentHarness.compact() requires a Models instance")

        current = list(self.agent.state.messages)
        await self._emit(
            "session_before_compact",
            SessionCompactContext(self, current),
        )
        result = await compact_messages(
            current,
            self.models,
            self.agent.state.model,
            self.compaction_settings,
        )
        if result is not None:
            # Append-only storage cannot rewrite an old path. Start a new root
            # containing the compacted context; the old tree remains navigable.
            await self.session.move_to(None)
            for message in result.messages:
                await self.session.append_message(message)
            self.agent.state.messages = result.messages
            self._persisted_message_count = len(result.messages)
        await self._emit(
            "session_after_compact",
            SessionCompactContext(self, list(self.agent.state.messages), result),
        )
        return result

    async def navigate_tree(self, target_id: str | None) -> NavigationResult:
        """Select a session leaf and rebuild the Agent context from that branch."""

        self._ensure_open()
        await self._ensure_loaded()
        if self.agent.state.is_streaming:
            raise RuntimeError("Cannot navigate while the agent is running")
        await self._emit(
            "session_before_navigate",
            SessionNavigateContext(self, target_id, list(self.agent.state.messages)),
        )
        await self.session.move_to(target_id)
        messages = await self.session.build_context()
        self.agent.state.messages = messages
        self._persisted_message_count = len(messages)
        result = NavigationResult(target_id, list(messages))
        await self._emit(
            "session_after_navigate",
            SessionNavigateContext(self, target_id, list(messages)),
        )
        return result

    async def close(self) -> None:
        """Detach listeners and restore hooks owned by the wrapped Agent."""

        if self._closed:
            return
        self._unsubscribe()
        self.agent.before_tool_call = self._agent_before_tool_call
        self.agent.after_tool_call = self._agent_after_tool_call
        self._hooks.clear()
        self._closed = True

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        messages = await self.session.build_context()
        self.agent.state.messages = messages
        self._persisted_message_count = len(messages)
        self._loaded = True

    async def _apply_context_hooks(self) -> None:
        context = HarnessContext(self, list(self.agent.state.messages))
        await self._emit("context", context)

    async def _on_agent_event(self, event: Any, _signal: AbortSignal) -> None:
        if event.type == "turn_end":
            await self._flush_session()

    async def _flush_session(self) -> None:
        pending = list(self.agent.state.messages[self._persisted_message_count :])
        if not pending:
            return
        context = SessionFlushContext(self, pending)
        await self._emit("session_before_flush", context)
        for message in pending:
            await self.session.append_message(message)
            self._persisted_message_count += 1
        await self._emit("session_after_flush", context)

    async def _before_tool_call(
        self,
        context: BeforeToolCallContext,
        signal: AbortSignal | None,
    ) -> BeforeToolCallResult | None:
        base = None
        if self._agent_before_tool_call is not None:
            base = await _maybe_await(self._agent_before_tool_call(context, signal))
            if isinstance(base, BeforeToolCallResult) and base.block:
                return base

        decision = base if isinstance(base, BeforeToolCallResult) else BeforeToolCallResult()
        for result in await self._emit("tool_call", context):
            current = _coerce_before_tool_result(result)
            if current is not None and current.block:
                return current
        return decision if decision.block or decision.reason is not None else None

    async def _after_tool_call(
        self,
        context: AfterToolCallContext,
        signal: AbortSignal | None,
    ) -> AfterToolCallResult | None:
        results: list[Any] = []
        if self._agent_after_tool_call is not None:
            results.append(await _maybe_await(self._agent_after_tool_call(context, signal)))
        results.extend(await self._emit("tool_result", context))

        merged = AfterToolCallResult()
        changed = False
        for value in results:
            current = _coerce_after_tool_result(value)
            if current is None:
                continue
            if current.content is not None:
                merged.content = current.content
                changed = True
            if current.details is not None:
                merged.details = current.details
                changed = True
            if current.is_error is not None:
                merged.is_error = current.is_error
                changed = True
            if current.terminate is not None:
                merged.terminate = current.terminate
                changed = True
        return merged if changed else None

    async def _emit(self, event: str, payload: Any) -> list[Any]:
        results: list[Any] = []
        for handler in tuple(self._hooks.get(event, ())):
            results.append(await _maybe_await(handler(payload)))
        return results

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("AgentHarness is closed")


async def _maybe_await(value: Any | Awaitable[Any]) -> Any:
    return await value if inspect.isawaitable(value) else value


def _coerce_before_tool_result(value: Any) -> BeforeToolCallResult | None:
    if isinstance(value, BeforeToolCallResult):
        return value
    if isinstance(value, Mapping):
        return BeforeToolCallResult(
            block=bool(value.get("block", False)),
            reason=value.get("reason"),
        )
    return None


def _coerce_after_tool_result(value: Any) -> AfterToolCallResult | None:
    if isinstance(value, AfterToolCallResult):
        return value
    if isinstance(value, Mapping):
        return AfterToolCallResult(
            content=value.get("content"),
            details=value.get("details"),
            is_error=value.get("is_error"),
            terminate=value.get("terminate"),
        )
    return None


__all__ = [
    "AgentHarness",
    "BeforeAgentStartContext",
    "HOOK_NAMES",
    "HarnessContext",
    "HarnessRunResult",
    "NavigationResult",
    "SessionCompactContext",
    "SessionFlushContext",
    "SessionNavigateContext",
]
