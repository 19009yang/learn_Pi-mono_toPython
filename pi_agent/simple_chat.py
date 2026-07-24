"""最简多轮对话 Agent REPL。

将 pi_agent.agent_loop 与用户交互连接起来，实现：
- 多轮对话循环（REPL 模式）
- 实时流式输出（逐字打印 LLM 回复）
- 工具调用展示（打印工具名/参数/结果）
- 上下文记忆（每轮结束后把新消息追加到历史，下一轮传入完整历史）
- 优雅退出（exit / quit / Ctrl+C）

关于上下文记忆：
  agent_loop 内部会创建一个新的 current_context（用 [*context.messages, *prompts]
  拼接），所有消息累积发生在那个新列表上，原始 context.messages 不受影响。
  因此我们必须在每轮结束后，把 agent_loop 返回的 new_messages 追加到自己的
  历史列表中，下一轮构建 context 时带上完整历史，才能实现多轮记忆。
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from pi_ai.models import Models
from pi_ai.types import Model, TextContent, UserMessage
from pi_agent.agent_loop import agent_loop
from pi_agent.messages import AgentMessage, convert_to_llm
from pi_agent.types import (
    AgentContext,
    AgentLoopConfig,
    AgentTool,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


async def run_simple_chat(
    models: Models,
    model: Model,
    tools: list[AgentTool] | None = None,
    system_prompt: str = "You are a helpful assistant.",
) -> None:
    """多轮对话 REPL 主循环。

    Args:
        models: 已注册 provider 的 Models 实例。
        model: 使用的 LLM 模型。
        tools: 可供 Agent 调用的工具列表。
        system_prompt: 系统提示词。
    """
    # Windows 控制台编码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    config = AgentLoopConfig(
        model=model,
        convert_to_llm=convert_to_llm,
        stream_fn=models.stream_simple,
    )

    # 对话历史：每轮结束后把新消息追加到这里，下一轮构建 context 时带上
    # （agent_loop 内部不修改传入的 context.messages，我们需要自己维护）
    history: list[Any] = []

    print("=" * 50)
    print(f"  模型: {model.name} ({model.provider}/{model.id})")
    if tools:
        print(f"  工具: {', '.join(t.name for t in tools)}")
    print("  输入 exit 或 quit 退出")
    print("=" * 50)

    while True:
        # ---- 读取用户输入 ----
        try:
            user_input = input("\n你: ")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input.strip():
            continue
        if user_input.strip().lower() in ("exit", "quit"):
            print("再见！")
            break

        # ---- 构造消息并调用 agent_loop ----
        user_msg = UserMessage(content=user_input.strip(), timestamp=_now_ms())

        # 每轮用完整历史构建 context，确保 LLM 能看到之前的对话
        context = AgentContext(
            system_prompt=system_prompt,
            messages=list(history),  # 传副本，避免被 agent_loop 内部操作影响
            tools=tools or [],
        )

        stream = agent_loop([user_msg], context, config)

        # ---- 消费事件流，实时打印 ----
        print()  # 换行，让 LLM 输出从新行开始
        has_output = False

        async for event in stream:
            etype = event.type

            # LLM 流式输出——逐字打印文本
            if etype == "message_update":
                assistant_event = event.assistant_message_event
                if assistant_event.type == "text_delta":
                    print(assistant_event.delta, end="", flush=True)
                    has_output = True
                elif assistant_event.type == "thinking_delta":
                    # 思考内容可以选择性展示
                    # 默认不展示，避免干扰正常输出
                    pass

            # 工具执行开始
            elif etype == "tool_execution_start":
                args_str = _format_args(event.args)
                print(f"\n🔧 调用工具: {event.tool_name}({args_str})")
                has_output = True

            # 工具执行结束
            elif etype == "tool_execution_end":
                if event.is_error:
                    print(f"   ❌ 工具 {event.tool_name} 执行失败")
                else:
                    result_text = _extract_result_text(event.result)
                    print(f"   ✅ {result_text}")
                has_output = True

        # ---- 确保换行 ----
        if has_output:
            print()  # LLM 输出结束后换行

        # ---- 获取本轮新增的消息，追加到历史 ----
        new_messages = await stream.result()
        history.extend(new_messages)


def _format_args(args: Any) -> str:
    """将工具参数格式化为简短的字符串表示。"""
    if isinstance(args, dict):
        items = []
        for k, v in args.items():
            val_str = repr(v) if len(repr(v)) <= 40 else repr(v)[:37] + "..."
            items.append(f"{k}={val_str}")
        return ", ".join(items)
    return str(args)


def _extract_result_text(result: Any) -> str:
    """从 AgentToolResult 中提取文本摘要。"""
    if result is None:
        return "(no result)"
    content = getattr(result, "content", None)
    if content is None:
        return "(no content)"
    texts = []
    for block in content:
        if isinstance(block, TextContent):
            texts.append(block.text)
    text = " ".join(texts)
    if len(text) > 100:
        return text[:97] + "..."
    return text if text else "(empty)"
