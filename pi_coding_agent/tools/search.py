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