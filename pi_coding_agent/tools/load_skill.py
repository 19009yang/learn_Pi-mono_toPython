"""Tool that exposes loaded Skill instructions to the model on demand."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from pi_ai.event_stream import AbortSignal
from pi_ai.types import TextContent
from pi_agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi_coding_agent.skills import Skill, format_skill_invocation


class LoadSkillParameters(BaseModel):
    name: str = Field(description="Name of the available Skill to load")


class LoadSkillTool(AgentTool):
    """Return one pre-loaded Skill's full instructions as a tool result."""

    def __init__(self, skills: Sequence[Skill]) -> None:
        self._skills = {skill.name: skill for skill in skills}
        parameters = LoadSkillParameters.model_json_schema()
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            name_property = properties.get("name")
            if isinstance(name_property, dict):
                name_property["enum"] = sorted(self._skills)
        super().__init__(
            name="load_skill",
            label="Load Skill",
            description="Load the full instructions for an available Skill before following it.",
            parameters=parameters,
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[dict[str, str]]:
        if signal is not None and signal.aborted:
            raise RuntimeError("Operation aborted")
        values = LoadSkillParameters.model_validate(params)
        skill = self._skills.get(values.name)
        if skill is None:
            available = ", ".join(sorted(self._skills)) or "(none)"
            raise ValueError(f"Unknown Skill: {values.name}. Available: {available}")
        return AgentToolResult(
            content=[TextContent(text=format_skill_invocation(skill))],
            details={"name": skill.name, "path": str(skill.file_path)},
        )