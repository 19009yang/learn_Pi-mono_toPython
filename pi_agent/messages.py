from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from pi_ai.types import ImageContent, Message, TextContent, UserMessage

# ---这些常量必须与原版 TS 代码精确匹配，保证 Python 版和 TS版生成的上下文文本格式一致---
# 对话历史压缩（compaction）时的 XML 包装。当上下文窗口满了，旧消息被压缩成摘要，用
# <summary> 标签包裹，让 LLM 知道"之前的对话被压缩了"。
COMPACTION_SUMMARY_PREFIX = """The conversation history before this point was compacted into the following summary:

<summary>
"""
COMPACTION_SUMMARY_SUFFIX = """
</summary>"""

# 分支摘要的 XML包装。当用户从一个对话分支切换回来时，
# 该分支的内容被压缩成摘要，同样用 <summary>标签。
BRANCH_SUMMARY_PREFIX = """The following is a summary of a branch that this conversation came back from:

<summary>
"""
BRANCH_SUMMARY_SUFFIX = """</summary>"""

# 自定义消息类型

@dataclass
class BashExecutionMessage:
    """ 记录在 Agent 循环之外执行的 shell 命令（比如用户手动运行的命令）"""
    # 固定为 "bashExecution"，init=False, 表示不参与构造函数参数
    role: Literal["bashExecution"] = field(default="bashExecution", init=False)
    command: str  #执行的命令文本  
    output: str
    exit_code: int | None # 退出码，None 表示命令被取消或未正常结束
    cancelled: bool # 是否被手动取消
    truncated: bool # 输出是否被截断
    timestamp: int  # Unix 毫秒时间戳
    full_output_path: str | None = None
    exclude_from_context: bool = False # 是否从发给 LLM 的上下文中排除（某些命令结果不需要 LLM 看到）


@dataclass
class CustomMessage:
    """应用自定义消息，发送给 LLM 时始终转换为 user 消息"""

    role: Literal["custom"] = field(default="custom", init=False)
    custom_type: str #自定义消息类型标识（如"result"、"info" 等）
    content: str | list[TextContent | ImageContent]
    display: bool #是否在UI上显示给用户
    timestamp: int  # Unix ms
    details: Any = None #任意附加数据，不发给 LLM 


@dataclass
class BranchSummaryMessage:
    """对话分支摘要——当用户从一个分支切换回来时，记录该分支的摘要"""

    role: Literal["branchSummary"] = field(default="branchSummary", init=False)
    summary: str # 分支对话的压缩摘要文本
    from_id: str # 原始分支的 ID 
    timestamp: int  # Unix ms


@dataclass
class CompactionSummaryMessage:
    """上下文压缩摘要——当对话历史过长时，旧消息被压缩成此摘要以释放上下文窗口"""

    role: Literal["compactionSummary"] = field(default="compactionSummary", init=False)
    summary: str
    tokens_before: int
    timestamp: int  # Unix ms


#   联合类型别名，表示 Agent 循环中所有可能出现的消息类型。注释说明：在 TS
#   中可以用声明合并（declaration merging）扩展，Python 中需要通过 widen alias 或
#   registry 来扩展。
AgentMessage: TypeAlias = (
    Message | BashExecutionMessage | CustomMessage | BranchSummaryMessage | CompactionSummaryMessage
)

def _now_ms() -> int:
    # 返回当前ms级时间戳
    return int(time.time() * 1000)


def _parse_timestamp(timestamp: int | float | str | None) -> int:
    """灵活解析时间戳，兼容三种输入格式"""
    if timestamp is None:
        return _now_ms() #使用当前时间
    if isinstance(timestamp, str):
        # fromisoformat handles "2024-01-01T00:00:00" and with offset;
        # Z suffix needs a small normalize for 3.12.
        normalized = timestamp.replace("Z", "+00:00")
        from datetime import datetime

        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    value = float(timestamp)
    # Heuristic: values below year ~2001 in ms are likely seconds.
    if value < 1_000_000_000_000:
        return int(value * 1000)
    return int(value)


# 三个工厂函数，封装 dataclass 的创建逻辑
def create_branch_summary_message(
    summary: str,
    from_id: str,
    timestamp: int | float | str | None = None,
) -> BranchSummaryMessage:
    return BranchSummaryMessage(
        summary=summary,
        from_id=from_id,
        timestamp=_parse_timestamp(timestamp),
    )


def create_compaction_summary_message(
    summary: str,
    tokens_before: int,
    timestamp: int | float | str | None = None,
) -> CompactionSummaryMessage:
    return CompactionSummaryMessage(
        summary=summary,
        tokens_before=tokens_before,
        timestamp=_parse_timestamp(timestamp),
    )


def create_custom_message(
    custom_type: str,
    content: str | list[TextContent | ImageContent],
    display: bool,
    details: Any = None,
    timestamp: int | float | str | None = None,
) -> CustomMessage:
    return CustomMessage(
        custom_type=custom_type,
        content=content,
        display=display,
        details=details,
        timestamp=_parse_timestamp(timestamp),
    )


# ========== 格式化 + convert_to_llm ==========


def bash_execution_to_text(msg: BashExecutionMessage) -> str:
    """将 shell 命令执行记录格式化为 LLM 可读的文本"""
    # 基本格式：Ran <command> + 输出代码块
    text = f"Ran `{msg.command}`\n"
    if msg.output:
        text += f"```\n{msg.output}\n```"
    else:
        text += "(no output)"
    if msg.cancelled: #被取消
        text += "\n\n(command cancelled)"
    elif msg.exit_code is not None and msg.exit_code != 0: #非0退出
        text += f"\n\nCommand exited with code {msg.exit_code}"
    if msg.truncated and msg.full_output_path: #输出被截断时追加完整输出文件路径
        text += f"\n\n[Output truncated. Full output: {msg.full_output_path}]"
    return text


def convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    """ 将 Agent 内部消息列表转换为 LLM 可消费的 Message 列表 """
    out: list[Message] = []
    for m in messages:
        converted = _convert_one(m)
        if converted is not None:
            out.append(converted)
    return out


def _convert_one(m: AgentMessage) -> Message | None:
    role = getattr(m, "role", None)
    if role == "bashExecution":
        assert isinstance(m, BashExecutionMessage)
        if m.exclude_from_context: #需要从LLM上下文中排除
            return None
        return UserMessage(
            content=[TextContent(text=bash_execution_to_text(m))],
            timestamp=m.timestamp,
        )
    if role == "custom":
        assert isinstance(m, CustomMessage)
        content: list[TextContent | ImageContent]
        if isinstance(m.content, str):
            content = [TextContent(text=m.content)]
        else:
            content = list(m.content)
        return UserMessage(content=content, timestamp=m.timestamp)
    if role == "branchSummary":
        assert isinstance(m, BranchSummaryMessage)
        text = BRANCH_SUMMARY_PREFIX + m.summary + BRANCH_SUMMARY_SUFFIX
        return UserMessage(
            content=[TextContent(text=text)],
            timestamp=m.timestamp,
        )
    if role == "compactionSummary":
        assert isinstance(m, CompactionSummaryMessage)
        text = COMPACTION_SUMMARY_PREFIX + m.summary + COMPACTION_SUMMARY_SUFFIX
        return UserMessage(
            content=[TextContent(text=text)],
            timestamp=m.timestamp,
        )
    if role in ("user", "assistant", "toolResult"): #原样返回
        # Already an LLM Message.
        return m  # type: ignore[return-value]
    return None


"""
整体设计要点

1. 双层消息体系：Agent 内部有丰富的消息类型（bash执行、自定义、分支摘要、压缩摘要），但 LLM 只理解标准
Message（user/assistant/toolResult）。convert_to_llm 是两层之间的桥梁。
2. 所有自定义类型 → UserMessage：无论是 bash 执行、自定义消息还是摘要，发给 LLM时都变成 user 角色——因为 LLM 只理解 user/assistant/tool 三种角色。
3. XML 标签隔离摘要：压缩和分支摘要用 <summary> 标签包裹，让 LLM明确区分"这是摘要"和"这是正常对话"。
4. TS 完全对齐：常量格式、工厂函数签名、转换逻辑都标注为与 TS版精确匹配，这是项目学习/移植目标的核心体现。
"""