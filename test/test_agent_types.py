"""Tests for pi_agent.types"""

from __future__ import annotations

from typing import Any

import pytest

from pi_ai.types import Model, ModelCost, TextContent
from pi_agent.types import (
    AgentLoopConfig,
    AgentState,
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    AfterToolCallResult,
)


def _model() -> Model:
    return Model(
        id="test",
        name="Test",
        api="openai-completions",
        provider="test",
        base_url="https://example.com",
        context_window=128_000,
        max_tokens=4096,
        cost=ModelCost(0, 0, 0, 0),
    )


class EchoTool(AgentTool):
    def __init__(self) -> None:
        super().__init__(
            name="echo",
            label="Echo",
            description="Echo a string",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult[None]:
        text = str(params.get("text", ""))
        if on_update is not None:
            on_update(AgentToolResult(content=[TextContent(text=text[:1])], details=None))
        return AgentToolResult(content=[TextContent(text=text)], details=None)


def test_agent_tool_as_tool_projection() -> None:
    tool = EchoTool()
    llm_tool = tool.as_tool()
    assert llm_tool.name == "echo"
    assert llm_tool.description == "Echo a string"
    assert "properties" in llm_tool.parameters
    assert not hasattr(llm_tool, "execute")


def test_prepare_arguments_default_passthrough() -> None:
    tool = EchoTool()
    assert tool.prepare_arguments({"text": "hi"}) == {"text": "hi"}
    with pytest.raises(TypeError):
        tool.prepare_arguments("not-a-dict")


@pytest.mark.asyncio
async def test_echo_tool_execute() -> None:
    tool = EchoTool()
    result = await tool.execute("call-1", {"text": "hello"})
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "hello"
    assert result.terminate is False


def test_agent_state_copies_tools_and_messages_on_assign() -> None:
    state = AgentState(model=_model())
    tools = [EchoTool()]
    messages: list[Any] = []
    state.tools = tools
    state.messages = messages
    tools.append(EchoTool())
    messages.append(object())
    assert len(state.tools) == 1
    assert len(state.messages) == 0


def test_agent_state_readonly_runtime_fields() -> None:
    state = AgentState(model=_model(), system_prompt="sys")
    assert state.is_streaming is False
    assert state.streaming_message is None
    assert state.pending_tool_calls == frozenset()
    assert state.error_message is None
    state._set_streaming(True)
    state._add_pending_tool_call("t1")
    assert state.is_streaming is True
    assert "t1" in state.pending_tool_calls


def test_agent_loop_config_requires_convert_to_llm() -> None:
    def identity(msgs: list[Any]) -> list[Any]:
        return msgs

    cfg = AgentLoopConfig(model=_model(), convert_to_llm=identity)
    assert cfg.tool_execution == "parallel"
    assert cfg.before_tool_call is None
    assert cfg.after_tool_call is None


def test_hook_result_defaults() -> None:
    assert BeforeToolCallResult().block is False
    assert BeforeToolCallResult(block=True, reason="nope").reason == "nope"
    override = AfterToolCallResult(is_error=True, terminate=True)
    assert override.content is None
    assert override.is_error is True
    assert override.terminate is True
