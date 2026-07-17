"""
关键设计决策：AgentMessage 放在 pi_agent.messages 而非此文件，是为了避免与
convert_to_llm 产生循环导入——因为 convert_to_llm 需要引用此文件中的类型，而
AgentMessage 的定义又需要引用 Message，拆分可以打破循环
"""
 
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeAlias, TypeVar

from pi_ai.event_stream import AbortSignal, AssistantMessageEventStream
from pi_ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    ImageContent,
    Message,
    Model,
    SimpleStreamOptions,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
)

# ========== 枚举/别名区 ==========
# 工具执行模式：sequential = 一个接一个执行，parallel = 并行执行所有工具调用
ToolExecutionMode: TypeAlias = Literal["sequential", "parallel"]
# 队列模式：all = 一次入队所有消息，one-at-a-time = 逐条入队
QueueMode: TypeAlias = Literal["all", "one-at-a-time"]

# pi_ai 层的 ThinkingLevel 不含 "off"——Agent层是更高级的抽象，需要表达"关闭思考"的概念
ThinkingLevel: TypeAlias = Literal["off", "minimal", "low", "medium", "high", "xhigh"] # Agent 层的思考级别，包含 "off"

# 直接复用pi_ai.types.ToolCall，只是换个名字让 Agent 层的代码语义更清晰
AgentToolCall: TypeAlias = ToolCall

# 工具结果的泛型参数，每个工具可以自定义 details 的类型
TDetails = TypeVar("TDetails")

# 兼容同步和异步两种返回方式。注释强调契约（同 TS）：
# 不得抛异常：请求/模型/运行时错误不通过 raise 表达
# 错误编码在流中：通过流内的 error/aborted AssistantMessage 传递
StreamFn: TypeAlias = Callable[
    [Model, Context, SimpleStreamOptions | None],
    AssistantMessageEventStream | Awaitable[AssistantMessageEventStream],
]

# ========== 工具结果和工具定义 ==========
@dataclass
class AgentToolResult(Generic[TDetails]):
    """Final or partial result produced by a tool."""

    content: list[TextContent | ImageContent]
    details: TDetails
    # 只有当整批工具全部 finalize 且都设 True 时才停止——单个工具 terminate=True 不够
    terminate: bool = False

# 工具执行过程中的进度回调，传入部分结果（AgentToolResult）
AgentToolUpdateCallback: TypeAlias = Callable[[AgentToolResult[Any]], None]


class AgentTool(ABC):
    """Agent 循环中的工具定义
    为什么不继承 pi_ai.Tool dataclass？因为 dataclass + ABC 组合在 Python
    中很尴尬（dataclass 会生成 __init__，ABC 也需要 __init__，两者冲突）。所以采用手动
    __init__ + ABC，用 as_tool() 方法投影到 LLM 层的 Tool 形状
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    label: str
    execution_mode: ToolExecutionMode | None

    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        label: str,
        execution_mode: ToolExecutionMode | None = None, #该工具的执行模式偏好，覆盖全局默认
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.label = label
        self.execution_mode = execution_mode

    def as_tool(self) -> Tool:
        """投影到 LLM 层的 Tool——只保留 name/description/parameters，丢弃execute/label/execution_mode 这些运行时信息"""
        return Tool(name=self.name, description=self.description, parameters=self.parameters)

    def prepare_arguments(self, args: Any) -> dict[str, Any]:
        """  
        参数预处理hook。LLM 返回的原始参数可能不符合 JSON Schema（比如 DeepSeek 有时返回非
        dict），这里做一次 shim。默认行为：dict 直接通过，非 dict 抛
        TypeError。子类可重写做更复杂的转换.
        """
        if isinstance(args, dict):
            return args
        raise TypeError(f"{self.name}: prepare_arguments expected dict, got {type(args).__name__}")

    @abstractmethod
    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[Any]:
        """工具执行函数，失败时抛出 raise 异常，不把错误编码在content中"""


# ========== Hook 结果和上下文 ==========
# Hook主要是方便后续插入新函数，不使用就默认为None

@dataclass
class BeforeToolCallResult:
    """before_tool_call 钩子的返回值"""
    block: bool = False # 是否执行阻止——True 时跳过 execute()，相当于拦截
    reason: str | None = None #阻止原因


@dataclass
class AfterToolCallResult:
    """ 
    after_tool_call 钩子的返回值——部分覆盖，只修改想修改的字段.
    """

    content: list[TextContent | ImageContent] | None = None
    details: Any = None
    is_error: bool | None = None # 标记结果为错误
    terminate: bool | None = None # 覆盖终止标志

# before_tool_call 钩子收到的上下文
@dataclass
class BeforeToolCallContext:
    assistant_message: AssistantMessage
    tool_call: AgentToolCall
    args: Any
    context: AgentContext

# after_tool_call 钩子的上下文，多了 result 和 is_error——因为工具已经执行完了
@dataclass
class AfterToolCallContext:
    assistant_message: AssistantMessage
    tool_call: AgentToolCall
    args: Any
    result: AgentToolResult[Any]
    is_error: bool
    context: AgentContext

# should_stop_after_turn 钩子的上下文
@dataclass
class ShouldStopAfterTurnContext:
    message: AssistantMessage
    tool_results: list[ToolResultMessage]
    context: AgentContext
    # Messages this loop invocation would return if it exits here.
    new_messages: list[Any]  # list[AgentMessage]; Any avoids circular import

# prepare_next_turn 钩子的返回值——替换运行时状态
@dataclass
class AgentLoopTurnUpdate:
    """允许在每轮之间动态切换模型/思考级别——比如简单问题用便宜模型，复杂问题升级到强模型"""

    context: AgentContext | None = None
    model: Model | None = None
    thinking_level: ThinkingLevel | None = None


PrepareNextTurnContext: TypeAlias = ShouldStopAfterTurnContext


# ========== Agent 上下文和状态 ==========


@dataclass
class AgentContext:
    """传入底层 Agent 循环的上下文快照"""
    # 这是不可变快照——每轮循环创建新的，不直接修改
    system_prompt: str 
    messages: list[Any]  # list[AgentMessage]
    tools: list[AgentTool] | None = None


class AgentState:
    """
    Agent 的公开可观察状态——不是 dataclass，手动控制属性的读写语义
    赋值 tools 或 messages 时会复制顶层列表（与 TS 的 accessor 语义相同），防止调用者通过保留的引用意外修改内部存储
    """

    def __init__(
        self,
        *,
        system_prompt: str = "",
        model: Model,
        thinking_level: ThinkingLevel = "off",
        tools: list[AgentTool] | None = None,
        messages: list[Any] | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.model = model
        self.thinking_level = thinking_level
        self._tools: list[AgentTool] = list(tools) if tools else []
        self._messages: list[Any] = list(messages) if messages else [] #内部副本，list() 复制，防止外部引用篡改
        self._is_streaming = False # 是否正在流式输出
        self._streaming_message: Any | None = None #当前正在流式输出的消息引用
        self._pending_tool_calls: set[str] = set() #正在执行中的工具调用 ID 集合
        self._error_message: str | None = None

    @property # 读时触发 只读属性，不允许赋值
    def tools(self) -> list[AgentTool]:
        return self._tools #返回内部列表的引用（非副本）

    @tools.setter # 写时触发 可读写，读写都可以插入自定义逻辑
    def tools(self, value: list[AgentTool]) -> None:
        self._tools = list(value)

    @property
    def messages(self) -> list[Any]:
        return self._messages

    @messages.setter
    def messages(self, value: list[Any]) -> None:
        self._messages = list(value)

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @property
    def streaming_message(self) -> Any | None:
        return self._streaming_message

    @property
    def pending_tool_calls(self) -> frozenset[str]:
        return frozenset(self._pending_tool_calls)

    @property
    def error_message(self) -> str | None:
        return self._error_message

    # 状态修改权只在 Agent 内部，外部只能读取.
    def _set_streaming(self, value: bool) -> None:
        self._is_streaming = value

    def _set_streaming_message(self, message: Any | None) -> None:
        self._streaming_message = message

    def _set_error_message(self, message: str | None) -> None:
        self._error_message = message

    def _add_pending_tool_call(self, tool_call_id: str) -> None:
        self._pending_tool_calls.add(tool_call_id)

    def _remove_pending_tool_call(self, tool_call_id: str) -> None:
        self._pending_tool_calls.discard(tool_call_id)

    def _clear_pending_tool_calls(self) -> None:
        self._pending_tool_calls.clear()


# ========== Loop config ==========


@dataclass
class AgentLoopConfig:
    """
    底层 Agent 循环的完整配置——是整个 Agent 运行的参数中心
    convert_to_llm 是必选的，所有hooks可选
    """

    #必选
    model: Model
    convert_to_llm: Callable[[list[Any]], list[Message] | Awaitable[list[Message]]]

    # 可选
    stream_fn: StreamFn | None = None #自定义流函数
    reasoning: ThinkingLevel | None = None #思考级别
    temperature: float | None = None #温度
    max_tokens: int | None = None #最大输出token
    signal: AbortSignal | None = None #中断信号
    api_key: str | None = None #API Key

    # 在发给 LLM 前对消息做额外变换(如插入中间系统提示)
    transform_context: (
        Callable[[list[Any], AbortSignal | None], Awaitable[list[Any]]] | None
    ) = None
    # 动态获取key
    get_api_key: Callable[[str], Awaitable[str | None] | str | None] | None = None
    # 每轮后判断是否提前停止循环
    should_stop_after_turn: (
        Callable[[ShouldStopAfterTurnContext], bool | Awaitable[bool]] | None
    ) = None
    # 每轮后修改下一轮的context/model/thinking_level 
    prepare_next_turn: (
        Callable[
            [PrepareNextTurnContext],
            AgentLoopTurnUpdate | None | Awaitable[AgentLoopTurnUpdate | None],
        ]
        | None
    ) = None
    # 获取引导消息（注入方向性提示）
    get_steering_messages: Callable[[], Awaitable[list[Any]]] | None = None
    # 获取跟进消息（补充上下文）
    get_follow_up_messages: Callable[[], Awaitable[list[Any]]] | None = None
    # 工具执行模式：默认为"parallel"
    tool_execution: ToolExecutionMode = "parallel"
    # 工具执行前的拦截hook
    before_tool_call: (
        Callable[
            [BeforeToolCallContext, AbortSignal | None],
            Awaitable[BeforeToolCallResult | None],
        ]
        | None
    ) = None
    # 工具执行后的结果修改hook
    after_tool_call: (
        Callable[
            [AfterToolCallContext, AbortSignal | None],
            Awaitable[AfterToolCallResult | None],
        ]
        | None
    ) = None


# ========== Agent 时间 ==========
"""
10 种事件类型，覆盖 Agent 循环的完整生命周期
"""
@dataclass
class AgentStartEvent:
    """Agent 循环开始。type 固定为 "agent_start"，init=False 表示不参与构造参数"""
    type: str = field(default="agent_start", init=False)

@dataclass
class AgentEndEvent:
    """Agent 循环结束。携带最终消息列表 messages"""
    type: str = field(default="agent_end", init=False)
    messages: list[Any]  # list[AgentMessage]

@dataclass
class TurnStartEvent:
    """一个 LLM 调用轮次开始（一轮 = 一次 LLM 调用 + 可能的工具执行）"""
    type: str = field(default="turn_start", init=False)

@dataclass
class TurnEndEvent:
    """一个轮次结束。携带该轮的 LLM 回复 message 和工具结果 tool_results"""
    type: str = field(default="turn_end", init=False)
    message: Any  # AgentMessage
    tool_results: list[ToolResultMessage]

@dataclass
class MessageStartEvent:
    """LLM 开始生成回复。携带初始 message 对象"""
    type: str = field(default="message_start", init=False)
    message: Any

@dataclass
class MessageUpdateEvent:
    """LLM 流式输出过程中，每次有新 chunk。携带更新后的 message 和原始的assistant_message_event"""
    type: str = field(default="message_update", init=False)
    message: Any
    assistant_message_event: AssistantMessageEvent


@dataclass
class MessageEndEvent:
    """LLM 回复生成完毕"""
    type: str = field(default="message_end", init=False)
    message: Any


@dataclass
class ToolExecutionStartEvent:
    """ 工具开始执行。携带 tool_call_id、tool_name、args"""
    type: str = field(default="tool_execution_start", init=False)
    tool_call_id: str
    tool_name: str
    args: Any


@dataclass
class ToolExecutionUpdateEvent:
    """ 工具执行有部分结果更新。携带 partial_result——对应 AgentToolUpdateCallback 的回调"""
    type: str = field(default="tool_execution_update", init=False)
    tool_call_id: str
    tool_name: str
    args: Any
    partial_result: Any


@dataclass
class ToolExecutionEndEvent:
    """工具执行完毕。携带最终 result 和 is_error 标志"""
    type: str = field(default="tool_execution_end", init=False)
    tool_call_id: str
    tool_name: str
    result: Any
    is_error: bool


AgentEvent: TypeAlias = (
    AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
)
