"""多轮对话 Demo 入口。

运行方式：
    uv run python demo/chat.py

前置条件：
    .env 文件中设置 DEEPSEEK_API_KEY=<your-key>
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

from pi_ai.models import MutableModels
from pi_ai.providers.deepseek import deepseek_provider
from pi_ai.types import Model, TextContent
from pi_agent.simple_chat import run_simple_chat
from pi_agent.types import AgentTool, AgentToolResult
from typing import Any


# ============================================================
# Demo 工具定义
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
        return AgentToolResult(
            content=[TextContent(text=f"echoed: {value}")],
            details=None,
        )


class AddTool(AgentTool):
    """加法工具：接收 a, b，返回 a+b"""

    def __init__(self) -> None:
        super().__init__(
            name="add",
            label="Add",
            description="Add two integers and return the sum.",
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
        return AgentToolResult(
            content=[TextContent(text=str(result))],
            details=None,
        )


# ============================================================
# 入口
# ============================================================


def main() -> None:
    # Windows 控制台编码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # 加载 .env
    load_dotenv()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("❌ 未找到 DEEPSEEK_API_KEY 环境变量！")
        print("   请在 .env 文件中设置: DEEPSEEK_API_KEY=<your-key>")
        return

    print(f"✓ DEEPSEEK_API_KEY 已加载 (长度: {len(api_key)} chars)")

    # 注册 Provider
    models = MutableModels()
    models.set_provider(deepseek_provider())

    # 选取模型
    model = models.get_model("deepseek", "deepseek-v4-flash")
    if model is None:
        print("❌ deepseek-v4-flash 模型未找到")
        return

    # 启动多轮对话
    asyncio.run(
        run_simple_chat(
            models=models,
            model=model,
            tools=[EchoTool(), AddTool()],
            system_prompt="You are a helpful assistant. When the user asks you to use tools, call them. After getting results, respond in Chinese (简体中文).",
        )
    )


if __name__ == "__main__":
    main()
