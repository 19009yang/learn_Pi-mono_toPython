# 工具系统设计解读

> 针对 `pi_coding_agent/tools/` 目录的工具注册架构与设计解读。  
> 核心文件：`pi_agent/types.py`（AgentTool 抽象基类）、`pi_coding_agent/tools/base.py`（CodingTool）、`pi_coding_agent/tools/__init__.py`（注册中心）  
> 配套测试：`test/test_coding_tools.py`

---

## 1. 整体架构

工具系统分三层，从抽象到具体逐层收窄：

```
pi_agent/types.py          ← 层 1：抽象契约（AgentTool）
    │
    │  继承
    ▼
pi_coding_agent/tools/base.py  ← 层 2：文件系统工具共享基类（CodingTool + ToolState）
    │
    │  继承
    ▼
pi_coding_agent/tools/*.py    ← 层 3：具体工具实现（ReadTool, BashTool, GrepTool ...）
    │
    │  实例化 + 组装
    ▼
pi_coding_agent/tools/__init__.py  ← 注册中心（create_default_tools）
    │
    │  注入 Agent
    ▼
pi_coding_agent/cli.py       ← 入口（AgentOptions.initial_state["tools"]）
```

**设计意图**：

- **层 1** 定义的是 Agent 循环与工具的通用契约——任何工具都必须有 `name`/`description`/`parameters`/`execute()`，但不关心工具做什么。
- **层 2** 是 coding-agent 的"私有抽象"——文件系统工具共享 `cwd`（工作目录）和 `ToolState`（读-写安全追踪），非文件系统工具不需要这些。
- **层 3** 是具体实现——每个工具定义自己的 Pydantic 参数模型和 `execute()` 逻辑。
- **注册中心** 把所有工具实例组装成 `list[AgentTool]`，一次性注入 Agent。

---

## 2. AgentTool 抽象基类

定义在 `pi_agent/types.py:67-117`，是整个工具系统的契约入口。

```python
class AgentTool(ABC):
    name: str                          # LLM 调用时使用的工具名（如 "bash", "read"）
    description: str                   # 给 LLM 看的工具描述
    parameters: dict[str, Any]         # JSON Schema，描述工具接受的参数
    label: str                         # 人类可读的短标签（如 "Bash", "Read"）
    execution_mode: ToolExecutionMode | None  # "sequential" 或 "parallel"，覆盖全局默认

    def __init__(self, *, name, description, parameters, label, execution_mode=None) -> None:
        ...

    def as_tool(self) -> Tool:
        """投影到 LLM 层——只保留 name/description/parameters，丢弃运行时信息"""
        return Tool(name=self.name, description=self.description, parameters=self.parameters)

    def prepare_arguments(self, args: Any) -> dict[str, Any]:
        """参数预处理 hook。LLM 返回的原始参数可能不是 dict，这里做 shim"""
        if isinstance(args, dict):
            return args
        raise TypeError(...)

    @abstractmethod
    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[Any]:
        """核心执行方法——子类必须实现"""
```

### 为什么是 ABC + 手动 `__init__` 而非 dataclass？

注释（`pi_agent/types.py:69-71`）解释了：Python 中 `dataclass + ABC` 组合很尴尬——dataclass 会生成 `__init__`，ABC 也需要 `__init__`，两者冲突。所以采用手动 `__init__` + ABC。

### `as_tool()` 的投影语义

`AgentTool` 是运行时对象（有 `execute()`），但 LLM 只需要知道"有哪些工具、接受什么参数"——`as_tool()` 把运行时对象投影为纯声明式 `Tool`（`pi_ai.types.Tool`），只保留 `name/description/parameters`，丢弃 `execute/label/execution_mode` 等运行时信息。这是**关注点分离**：LLM 侧不需要知道工具怎么执行。

### `execute()` 的参数

| 参数 | 类型 | 含义 |
|---|---|---|
| `tool_call_id` | `str` | LLM 返回的工具调用 ID，用于匹配请求和响应 |
| `params` | `dict[str, Any]` | LLM 返回的原始参数（由 `prepare_arguments` 预处理后传入） |
| `signal` | `AbortSignal | None` | 中断信号——Agent 被用户取消时设置 |
| `on_update` | `AgentToolUpdateCallback | None` | 进度回调——工具执行过程中推送部分结果（如 BashTool 的实时输出流） |

### `AgentToolResult` 的结构

```python
@dataclass
class AgentToolResult(Generic[TDetails]):
    content: list[TextContent | ImageContent]  # 工具输出内容
    details: TDetails                           # 结构化元信息（如 exit_code, truncation 等）
    terminate: bool = False                     # 是否终止 Agent 循环
```

- `content` 是给 LLM 看的文本/图片——会被包装成 `ToolResultMessage` 加入对话历史。
- `details` 是给调用者看的结构化信息——不影响 LLM，仅供 Agent 内部使用。
- `terminate=True` 表示"工具认为任务完成了，Agent 循环应该停止"。但只有**整批工具全部 terminate=True** 时才真正停止——单个工具 terminate 不够。

---

## 3. CodingTool 基类与 ToolState

定义在 `pi_coding_agent/tools/base.py`，是文件系统工具的共享基础设施。

```python
class ToolState:
    """进程级安全状态——追踪哪些文件已被读取"""
    read_files: set[Path]  # 已读取文件的绝对路径集合

    def mark_read(self, path: Path) -> None: ...  # 标记已读
    def was_read(self, path: Path) -> bool: ...   # 检查是否已读


class CodingTool(AgentTool):
    """文件系统工具的共享基类"""
    cwd: Path        # 工作目录（绝对路径）
    state: ToolState # 共享的读-写安全状态

    def __init__(self, *, cwd, state, name, label, description, parameters) -> None:
        super().__init__(name=name, label=label, description=description, parameters=parameters)
        self.cwd = Path(cwd).resolve()
        self.state = state

    @staticmethod
    def _check_abort(signal: AbortSignal | None) -> None:
        if signal is not None and signal.aborted:
            raise RuntimeError("Operation aborted")

    async def execute(self, ...) -> AgentToolResult:
        raise NotImplementedError  # 子类必须实现
```

### ToolState 的安全语义

Agent 循环有一条规则："**修改文件前必须先读取**"。`ToolState.read_files` 就是这条规则的状态载体：

- `ReadTool` 在读取文件后调用 `state.mark_read(path)`——登记"我读过这个文件"
- `WriteTool` / `EditTool` 在修改前调用 `state.was_read(path)`——检查"我是否读过这个文件"

这套机制在 `system_prompt.py` 的提示词中也有对应约束：

```
Tool use rules:
- Read an existing file before overwriting or editing it.
```

### 辅助函数

| 函数 | 作用 |
|---|---|
| `resolve_path(cwd, value)` | 把相对路径 + cwd 转为绝对路径，支持 `~` 展开 |
| `text_result(text, details)` | 快捷构造 `AgentToolResult(content=[TextContent(text=text)], details=details)` |
| `_check_abort(signal)` | 检查中断信号，如果已 abort 则抛 `RuntimeError` |

---

## 4. 参数校验：Pydantic BaseModel

每个工具都用 Pydantic `BaseModel` 定义参数模型，承担两个职责：

1. **生成 JSON Schema**——`model_json_schema()` 输出标准 JSON Schema，传给 LLM 让它知道"这个工具接受什么参数"
2. **校验传入参数**——`model_validate(params)` 把 LLM 返回的 dict 校验并转为强类型对象

以 `GrepTool` 为例：

```python
class GrepParameters(BaseModel):
    pattern: str = Field(description="Regular expression or literal search text")
    path: str = Field(default=".", description="File or directory to search")
    glob: str | None = Field(default=None, description="Optional file glob filter")
    ignore_case: bool = Field(default=False, description="Case-insensitive search")
    literal: bool = Field(default=False, description="Treat pattern as literal text")
    output_mode: GrepOutputMode = Field(default="content", description="content, files_with_matches, or count")
    limit: int = Field(default=100, ge=1, le=10_000, description="Maximum matches or files")
```

- `Field(description=...)` 中的 `description` 会进入 JSON Schema，**LLM 看到这个描述来决定怎么传参**
- `default=...` 表示可选参数，LLM 可以不传
- `ge=1, le=10_000` 等约束用于 **校验 LLM 返回值**，越界会抛 `ValidationError`

在 `execute()` 中使用：

```python
async def execute(self, tool_call_id, params, signal=None, on_update=None):
    values = GrepParameters.model_validate(params)  # 校验 + 转强类型
    root = resolve_path(self.cwd, values.path)       # 现在可以用 values.path（str）
    ...
```

---

## 5. 注册流程

完整的注册链路：

```
① 工具类定义（tools/*.py）
    ↓ __init__ 时传入 name/label/description/parameters
② 实例化（tools/__init__.py → create_default_tools）
    ↓ 返回 list[AgentTool]
③ 注入 Agent（cli.py → AgentOptions.initial_state["tools"]）
    ↓ Agent 内部存储到 AgentState._tools
④ 每轮 LLM 请求时
    ↓ agent_loop 取出 tools，逐个调用 tool.as_tool()
    ↓ 投影为 list[Tool]（只有 name/description/parameters）
    ↓ 放入 Context.tools，发给 LLM
⑤ LLM 返回 tool_call
    ↓ agent_loop 匹配 tool.name → 找到 AgentTool 实例
    ↓ 调用 tool.execute(tool_call_id, params, signal, on_update)
    ↓ 结果包装为 ToolResultMessage，加入对话历史
```

关键代码——`create_default_tools`（`__init__.py:17`）：

```python
def create_default_tools(cwd: str | Path) -> list[AgentTool]:
    """Create the six 3.1 tools with one shared read-before-mutation state."""
    state = ToolState()
    return [
        BashTool(cwd, state),
        ReadTool(cwd, state),
        WriteTool(cwd, state),
        EditTool(cwd, state),
        GrepTool(cwd, state),
        GlobTool(cwd, state),
    ]
```

**注意**：所有文件系统工具共享同一个 `ToolState` 实例——这是 `ReadTool` 的读取登记能被 `WriteTool` 看到的前提。

注入 Agent——`cli.py:69`：

```python
Agent(
    AgentOptions(
        initial_state={
            "model": model,
            "tools": tools,                         # ← list[AgentTool]
            "system_prompt": build_system_prompt(skills, resolved_cwd, tools),
        },
        stream_fn=registry.stream_simple,
    )
)
```

---

## 6. 工具与系统提示词的联动

`build_system_prompt`（`system_prompt.py:53`）接收 `tools` 参数，把每个工具的 `name` 和 `description` 渲染到系统提示词中：

```python
rendered_tools = [
    f"- {name}: {description}" if description else f"- {name}"
    for name, description in (_tool_description(tool) for tool in tools)
]
```

最终效果——LLM 在系统提示词中看到：

```
Available tools:
- bash: Run a command in bash. Output is streamed and retains the final 50KB if truncated.
- read: Read a UTF-8 text file with cat -n style line numbers. ...
- write: ...
- edit: ...
- grep: ...
- glob: ...
```

**新增工具后，只要把工具加入 `create_default_tools` 的返回列表，系统提示词会自动包含它——不需要手动修改提示词。**

---

## 7. 以 SearchTool 为例：如何注册新工具

下面以 DuckDuckGo 网页搜索工具为例，完整演示注册新工具的步骤。

### 7.1 判断继承哪个基类

| 情况 | 继承 |
|---|---|
| 工具涉及文件系统操作（需要 cwd + ToolState） | `CodingTool` |
| 工具不涉及文件系统（网络请求、计算等） | `AgentTool` |

搜索工具调用外部 API，不需要 `cwd` 和 `ToolState`——**直接继承 `AgentTool`**。

### 7.2 创建工具文件

新建 `pi_coding_agent/tools/search.py`：

```python
"""Web search tool using DuckDuckGo Instant Answer API."""

from __future__ import annotations

import aiohttp

from pydantic import BaseModel, Field

from pi_ai.event_stream import AbortSignal
from pi_ai.types import TextContent
from pi_agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback


# ── Step 1: 定义参数模型 ──────────────────────────────────

class SearchParameters(BaseModel):
    query: str = Field(description="Search query text")
    max_results: int = Field(
        default=5, ge=1, le=20,
        description="Maximum number of results to return",
    )


# ── Step 2: 定义工具类 ────────────────────────────────────

class SearchTool(AgentTool):
    def __init__(self) -> None:
        super().__init__(
            name="search",                              # LLM 调用时用的名字
            label="Search",                             # 人类可读短标签
            description=(
                "Search the web using DuckDuckGo. "
                "Returns a list of relevant results with titles, URLs, and snippets."
            ),
            parameters=SearchParameters.model_json_schema(),  # 从 Pydantic 模型生成 JSON Schema
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[dict[str, object] | None]:

        # Step 3: 校验参数
        values = SearchParameters.model_validate(params)

        # Step 4: 中断检查（与现有工具保持一致）
        if signal is not None and signal.aborted:
            raise RuntimeError("Operation aborted")

        # Step 5: 核心逻辑——调用 DuckDuckGo API
        url = "https://api.duckduckgo.com/"
        query_params = {
            "q": values.query,
            "format": "json",
            "no_html": 1,
            "no_redirect": 1,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=query_params) as response:
                data = await response.json()

        # Step 6: 解析结果
        results = []
        # 优先取摘要（DuckDuckGo 的 Instant Answer）
        abstract = data.get("Abstract")
        if abstract:
            results.append({
                "title": data.get("Heading", values.query),
                "url": data.get("AbstractURL", ""),
                "snippet": abstract,
            })
        # 然后取 RelatedTopics
        for topic in data.get("RelatedTopics", [])[:values.max_results]:
            if isinstance(topic, dict) and "Text" in topic:
                results.append({
                    "title": topic.get("Text", "").split(" - ")[0],
                    "url": topic.get("FirstURL", ""),
                    "snippet": topic.get("Text", ""),
                })

        results = results[:values.max_results]

        # Step 7: 格式化输出文本（给 LLM 看的）
        if not results:
            text = f"No results found for: {values.query}"
        else:
            lines = [f"Search results for: {values.query}\n"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['title']}")
                if r["url"]:
                    lines.append(f"   URL: {r['url']}")
                lines.append(f"   {r['snippet']}\n")
            text = "\n".join(lines)

        # Step 8: 返回 AgentToolResult
        return AgentToolResult(
            content=[TextContent(text=text)],
            details={"query": values.query, "result_count": len(results)},
        )
```

### 7.3 注册到工具列表

修改 `pi_coding_agent/tools/__init__.py`——只需两处改动：

**改动 1：新增导入**

```python
from pi_coding_agent.tools.search import SearchTool
```

**改动 2：在 `create_default_tools` 中追加实例**

```python
def create_default_tools(cwd: str | Path) -> list[AgentTool]:
    """Create the six 3.1 tools with one shared read-before-mutation state."""
    state = ToolState()
    return [
        BashTool(cwd, state),
        ReadTool(cwd, state),
        WriteTool(cwd, state),
        EditTool(cwd, state),
        GrepTool(cwd, state),
        GlobTool(cwd, state),
        SearchTool(),                      # ← 新增：不需要 cwd 和 state
    ]
```

**改动 3：追加到 `__all__`**

```python
__all__ = [
    "BashTool",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "ReadTool",
    "SearchTool",     # ← 新增
    "ToolState",
    "WriteTool",
    "create_default_tools",
]
```

### 7.4 完成——无需修改其他文件

注册完成后，以下流程**自动生效**，不需要手动改任何代码：

| 自动生效的环节 | 原因 |
|---|---|
| LLM 看到工具描述 | `build_system_prompt` 遍历 `tools` 列表，自动把 SearchTool 的 `name/description` 渲染到系统提示词 |
| LLM 知道工具参数 | `as_tool()` 投影时自动包含 `SearchParameters.model_json_schema()` 生成的 JSON Schema |
| Agent 循环分发调用 | `agent_loop` 按 `tool.name` 匹配，自动找到 `"search"` → `SearchTool.execute()` |
| 中断信号传递 | `agent_loop` 把 `AbortSignal` 传给 `execute()`，SearchTool 内的 `_check_abort` 正常工作 |

---

## 8. 注册新工具的 Checklist

每次新增工具，按以下清单操作：

| # | 步骤 | 文件 |
|---|---|---|
| 1 | 判断继承 `AgentTool` 还是 `CodingTool` | — |
| 2 | 定义 Pydantic 参数模型（`XxxParameters(BaseModel)`） | `tools/xxx.py` |
| 3 | 定义工具类，`__init__` 传入 `name/label/description/parameters` | `tools/xxx.py` |
| 4 | 实现 `execute()` 异步方法 | `tools/xxx.py` |
| 5 | 导入新工具类 | `tools/__init__.py` |
| 6 | 在 `create_default_tools()` 返回列表中追加实例 | `tools/__init__.py` |
| 7 | 追加到 `__all__` | `tools/__init__.py` |

**不需要修改的文件**：`cli.py`、`system_prompt.py`、`agent.py`、`pi_agent/types.py`——这些文件通过遍历 `tools` 列表自动适配。

---

## 9. 关键设计决策总结

| 决策 | 原因 |
|---|---|
| `AgentTool` 是 ABC + 手动 `__init__` | Python 中 dataclass + ABC 组合有冲突 |
| `as_tool()` 投影方法 | LLM 只需要声明式信息，运行时信息（execute/label）不应暴露 |
| Pydantic `BaseModel` 定义参数 | 同时服务两个职责：生成 JSON Schema 给 LLM + 校验 LLM 返回值 |
| `CodingTool` 中间基类 | 文件系统工具共享 `cwd` 和 `ToolState`，避免每个工具重复初始化 |
| `ToolState` 共享实例 | 读-写安全追踪需要跨工具可见（ReadTool 登记 → WriteTool 检查） |
| `create_default_tools` 统一组装 | 一个函数集中管理所有工具的实例化和依赖注入 |
| `AgentToolResult.content` vs `details` | `content` 给 LLM 看（进入对话历史），`details` 给 Agent 内部看（不影响对话） |
| `terminate=True` 需要"全票" | 单个工具 terminate 不足以停止循环——防止某个工具误判完成 |
