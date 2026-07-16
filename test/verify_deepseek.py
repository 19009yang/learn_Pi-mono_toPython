import asyncio
import time

from pi_ai.models import create_models
from pi_ai.providers.deepseek import deepseek_provider
from pi_ai.types import (
    Context,
    SimpleStreamOptions,
    TextContent,
    Tool,
    ToolCallEndEvent,
    UserMessage,
)
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    models = create_models()
    models.set_provider(deepseek_provider())

    model = models.get_model("deepseek", "deepseek-v4-flash")
    assert model is not None

    # 验证流式文本
    stream = models.stream_simple(
        model,
        Context(
            system_prompt="你是助手，只给出简短答案。",
            messages=[
                UserMessage(
                    content="算 2+2",
                    timestamp=int(time.time() * 1000),
                )
            ],
        ),
        SimpleStreamOptions(),
    )

    async for event in stream:
        print(event.type)
        if event.type == "text_delta":
            print(event.delta, end="\n", flush=True)

    message = await stream.result()
    assert message.stop_reason != "error", message.error_message

    text = "".join(
        block.text
        for block in message.content
        if isinstance(block, TextContent)
    )
    assert "4" in text
    print("\n文本验证通过")

    # 验证工具调用事件
    tool_stream = models.stream_simple(
        model,
        Context(
            messages=[
                UserMessage(
                    content="请调用 add 工具计算 2+2，不要自己计算。",
                    timestamp=int(time.time() * 1000),
                )
            ],
            tools=[
                Tool(
                    name="add",
                    description="计算两个整数之和",
                    parameters={
                        "type": "object",
                        "properties": {
                            "a": {"type": "integer"},
                            "b": {"type": "integer"},
                        },
                        "required": ["a", "b"],
                    },
                )
            ],
        ),
        SimpleStreamOptions(),
    )

    tool_call = None
    async for event in tool_stream:
        print(event.type)
        if isinstance(event, ToolCallEndEvent):
            tool_call = event.tool_call

    result = await tool_stream.result()
    assert result.stop_reason != "error", result.error_message
    assert tool_call is not None
    assert tool_call.name == "add"
    print("工具调用验证通过：", tool_call)


asyncio.run(main())