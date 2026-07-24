"""
验证点脚本：使用真实 LLM 的两轮工具调用 agent_loop

不使用假的 MockAssistantStream，而是通过项目中已有的 Models 注册体系
调用 DeepSeek API，让 LLM 自主决定何时调工具、调哪个工具。

场景：用户要求 "请先用 echo 回显 hello，然后用 add 计算 2+3，最后总结结果"
- LLM 第 1 轮：调用 echo("hello") 和 add(2,3) 两个工具
- 工具执行后 LLM 第 2 轮：拿到 toolResult 后返回纯文本总结

断言：最终 messages 包含 user→assistant(toolCall)→toolResult→...→assistant(text) 序列

运行方式：
    uv run python test/verify_two_round_tool_calls.py
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from dotenv import load_dotenv

from pi_ai.models import Models, MutableModels, create_provider
from pi_ai.providers.deepseek import deepseek_provider
from pi_ai.providers.model_catalogs import get_deepseek_models
from pi_ai.types import Model, TextContent, UserMessage
from pi_agent.agent_loop import agent_loop
from pi_agent.messages import AgentMessage, convert_to_llm
from pi_agent.types import AgentContext, AgentLoopConfig, AgentTool, AgentToolResult


def _user(text: str) -> UserMessage:
    """创建一个 UserMessage，时间戳用当前毫秒"""
    import time
    return UserMessage(content=text, timestamp=int(time.time() * 1000))


# ============================================================
# 加载环境变量（.env 中的 DEEPSEEK_API_KEY）
# ============================================================

load_dotenv()


# ============================================================
# 工具定义
# ============================================================


class EchoTool(AgentTool):
    """回显工具：接收字符串，返回 echoed: <value>"""

    def __init__(self) -> None:
        super().__init__(
            name="echo",
            label="Echo",
            description="Echo a string back. Use this when you need to repeat or confirm a text value.",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult[None]:
        value = params["value"]
        print(f"    🔧 EchoTool.execute(value='{value}')")
        return AgentToolResult(
            content=[TextContent(text=f"echoed: {value}")],
            details=None,
        )


class AddTool(AgentTool):
    """加法工具：接收 a, b，返回 a+b 的字符串"""

    def __init__(self) -> None:
        super().__init__(
            name="add",
            label="Add",
            description="Add two integers and return the sum. Use this when you need to compute a + b.",
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult[None]:
        result = int(params["a"]) + int(params["b"])
        print(f"    🔧 AddTool.execute(a={params['a']}, b={params['b']}) → {result}")
        return AgentToolResult(
            content=[TextContent(text=str(result))],
            details=None,
        )


class GreetTool(AgentTool):
    """问候工具：接收名字，返回 Hello, <name>!"""

    def __init__(self) -> None:
        super().__init__(
            name="greet",
            label="Greet",
            description="Greet a person by name. Returns 'Hello, <name>!'",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult[None]:
        name = params["name"]
        print(f"    🔧 GreetTool.execute(name='{name}')")
        return AgentToolResult(
            content=[TextContent(text=f"Hello, {name}!")],
            details=None,
        )


# ============================================================
# 构建真实 LLM 调用链
# ============================================================


def _build_models() -> Models:
    """注册 DeepSeek provider，返回 Models 实例"""
    models = MutableModels()
    models.set_provider(deepseek_provider())
    return models


def _get_model(models: Models) -> Model:
    """选取最便宜的模型 deepseek-v4-flash"""
    model = models.get_model("deepseek", "deepseek-v4-flash")
    if model is None:
        raise RuntimeError("deepseek-v4-flash 模型未找到，请确认 Models 注册是否正确")
    return model


# ============================================================
# 场景 1：两轮工具调用（echo + add）
# ============================================================


async def verify_two_round_tool_calls() -> None:
    """
    用户要求 LLM 先 echo 回显 hello，再 add 计算 2+3，最后总结。
    期望 LLM 在一轮中发出两个 toolCall，拿到结果后第二轮给出纯文本总结。

    断言：
      - messages 包含 user 角色和 assistant 角色
      - 至少有一个 assistant 包含 toolCall
      - 至少有一个 toolResult
      - 最后一条消息是 assistant 且为纯文本（无 toolCall）
      - 角色序列符合 user→assistant(toolCall)→toolResult→...→assistant(text) 模式
    """
    print("=" * 60)
    print("场景 1：两轮工具调用（echo + add）——真实 LLM")
    print("=" * 60)

    models = _build_models()
    model = _get_model(models)

    context = AgentContext(
        system_prompt="You are a helpful assistant that uses tools when asked. Always call the requested tools, then summarize the results in a final text response.",
        messages=[],
        tools=[EchoTool(), AddTool()],
    )
    config = AgentLoopConfig(
        model=model,
        convert_to_llm=convert_to_llm,
        stream_fn=models.stream_simple,
    )

    # 收集所有事件
    events: list[Any] = []
    stream = agent_loop(
        [_user("请先用 echo 回显 hello，然后用 add 计算 2+3，最后用文字总结所有结果。")],
        context,
        config,
    )
    async for event in stream:
        events.append(event)
    messages = await stream.result()

    # ---- 打印结果 ----
    print("\n最终 messages 列表：")
    for i, m in enumerate(messages):
        role = m.role
        if role == "assistant":
            content_desc = []
            for c in m.content:
                if c.type == "toolCall":
                    content_desc.append(f"toolCall({c.name}, args={c.arguments})")
                elif c.type == "text":
                    content_desc.append(f"text('{c.text[:80]}')")
                elif c.type == "thinking":
                    content_desc.append(f"thinking({len(c.thinking)} chars)")
            print(f"  [{i}] assistant: {', '.join(content_desc)}")
        elif role == "toolResult":
            text = m.content[0].text if m.content else ""
            print(f"  [{i}] toolResult(tool_name={m.tool_name}, text='{text}')")
        elif role == "user":
            text = m.content if isinstance(m.content, str) else str(m.content)[:80]
            print(f"  [{i}] user: '{text[:80]}'")
        else:
            print(f"  [{i}] {role}: {m}")

    print(f"\n事件类型集合: {sorted({e.type for e in events})}")

    # ---- 断言 ----
    role_sequence = [m.role for m in messages]

    # 1. 第一条必须是 user
    assert role_sequence[0] == "user", f"第一条消息应为 user，实际为 {role_sequence[0]}"
    print(f"✅ 第一条消息角色: user")

    # 2. 必须有 assistant 包含 toolCall
    assistant_with_tools = [
        m for m in messages
        if m.role == "assistant" and any(c.type == "toolCall" for c in m.content)
    ]
    assert len(assistant_with_tools) >= 1, "应至少有一个 assistant 包含 toolCall"
    tool_names = [c.name for c in assistant_with_tools[0].content if c.type == "toolCall"]
    print(f"✅ 第一个带 toolCall 的 assistant: toolCall 名称 = {tool_names}")

    # 3. 必须有 toolResult
    tool_results = [m for m in messages if m.role == "toolResult"]
    assert len(tool_results) >= 1, "应至少有一个 toolResult"
    print(f"✅ toolResult 数量: {len(tool_results)}")

    # 4. 最后一条必须是 assistant 且为纯文本（无 toolCall）
    last_msg = messages[-1]
    assert last_msg.role == "assistant", f"最后一条应为 assistant，实际为 {last_msg.role}"
    has_tool_call_in_last = any(c.type == "toolCall" for c in last_msg.content)
    assert not has_tool_call_in_last, "最后一条 assistant 不应包含 toolCall（应为纯文本总结）"
    last_text = "".join(c.text for c in last_msg.content if c.type == "text")
    print(f"✅ 最后一条 assistant (纯文本): '{last_text[:100]}'")

    # 5. 角色序列模式：user → assistant(toolCall) → toolResult → ... → assistant(text)
    #    至少包含：user, assistant, toolResult, assistant 四种角色
    required_roles = {"user", "assistant", "toolResult"}
    actual_roles = set(role_sequence)
    assert required_roles.issubset(actual_roles), f"角色集合不完整：期望包含 {required_roles}，实际 {actual_roles}"
    print(f"✅ 角色序列断言通过：{role_sequence}")

    # 6. 检查工具执行结果的正确性
    echo_results = [tr for tr in tool_results if tr.tool_name == "echo"]
    if echo_results:
        echo_text = echo_results[0].content[0].text
        assert "hello" in echo_text.lower(), f"echo 结果应包含 hello，实际: {echo_text}"
        print(f"✅ echo toolResult 包含 'hello': {echo_text}")

    add_results = [tr for tr in tool_results if tr.tool_name == "add"]
    if add_results:
        add_text = add_results[0].content[0].text
        assert add_text.strip() == "5", f"add 结果应为 5，实际: {add_text}"
        print(f"✅ add toolResult: {add_text}")

    print("\n🎉 场景 1 全部断言通过！")


# ============================================================
# 场景 2：三轮工具调用（greet → echo → text）
# ============================================================


async def verify_three_round_tool_calls() -> None:
    """
    用户要求 LLM 先 greet Alice，然后 echo Bye，最后总结。
    期望 LLM 可能在一轮或两轮中发出 toolCall，最终给出纯文本。

    断言：最终 messages 包含 greet 和 echo 的 toolResult，
    且最后一条为 assistant 纯文本。
    """
    print("\n" + "=" * 60)
    print("场景 2：多轮工具调用（greet + echo）——真实 LLM")
    print("=" * 60)

    models = _build_models()
    model = _get_model(models)

    context = AgentContext(
        system_prompt="You are a helpful assistant. When the user asks you to use tools, call them one by one. After getting all results, write a final text summary.",
        messages=[],
        tools=[GreetTool(), EchoTool()],
    )
    config = AgentLoopConfig(
        model=model,
        convert_to_llm=convert_to_llm,
        stream_fn=models.stream_simple,
    )

    events: list[Any] = []
    stream = agent_loop(
        [_user("请先问候 greet Alice，然后回显 echo Bye，最后用文字总结。")],
        context,
        config,
    )
    async for event in stream:
        events.append(event)
    messages = await stream.result()

    # ---- 打印结果 ----
    print("\n最终 messages 列表：")
    for i, m in enumerate(messages):
        role = m.role
        if role == "assistant":
            content_desc = []
            for c in m.content:
                if c.type == "toolCall":
                    content_desc.append(f"toolCall({c.name}, args={c.arguments})")
                elif c.type == "text":
                    content_desc.append(f"text('{c.text[:80]}')")
                elif c.type == "thinking":
                    content_desc.append(f"thinking({len(c.thinking)} chars)")
            print(f"  [{i}] assistant: {', '.join(content_desc)}")
        elif role == "toolResult":
            text = m.content[0].text if m.content else ""
            print(f"  [{i}] toolResult(tool_name={m.tool_name}, text='{text}')")
        elif role == "user":
            text = m.content if isinstance(m.content, str) else str(m.content)[:80]
            print(f"  [{i}] user: '{text[:80]}'")
        else:
            print(f"  [{i}] {role}: {m}")

    # ---- 断言 ----
    role_sequence = [m.role for m in messages]

    tool_results = [m for m in messages if m.role == "toolResult"]
    tool_result_names = [tr.tool_name for tr in tool_results]

    assert "greet" in tool_result_names, f"greet 工具应被调用，实际调用: {tool_result_names}"
    print(f"✅ greet 工具被调用")

    assert "echo" in tool_result_names, f"echo 工具应被调用，实际调用: {tool_result_names}"
    print(f"✅ echo 工具被调用")

    # 最后一条是 assistant 纯文本
    last_msg = messages[-1]
    assert last_msg.role == "assistant", f"最后一条应为 assistant"
    has_tool_call_in_last = any(c.type == "toolCall" for c in last_msg.content)
    assert not has_tool_call_in_last, "最后一条不应含 toolCall"
    print(f"✅ 最后一条 assistant 为纯文本总结")

    print(f"✅ 角色序列: {role_sequence}")
    print("\n🎉 场景 2 全部断言通过！")


# ============================================================
# 主入口
# ============================================================


async def main() -> None:
    # Windows 控制台编码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("╔════════════════════════════════════════════════════════════╗")
    print("║  agent_loop 两轮工具调用验证点（真实 LLM）                ║")
    print("║  验证：user→assistant(toolCall)→toolResult→assistant(text)║")
    print("║  Provider: DeepSeek V4 Flash                              ║")
    print("╚════════════════════════════════════════════════════════════╝")

    # 检查 API Key 是否可用
    import os
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("\n❌ 未找到 DEEPSEEK_API_KEY 环境变量！")
        print("   请在 .env 文件中设置: DEEPSEEK_API_KEY=<your-key>")
        return

    print(f"\n✓ DEEPSEEK_API_KEY 已加载 (长度: {len(api_key)} chars)")

    await verify_two_round_tool_calls()
    await verify_three_round_tool_calls()

    print("\n" + "=" * 60)
    print("✅ 全部场景验证通过！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
