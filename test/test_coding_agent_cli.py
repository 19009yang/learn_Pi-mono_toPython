"""Phase 3.3 tests for CLI argument parsing and agent composition."""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_coding_agent import cli
from pi_coding_agent.cli import create_agent, parse_args
from pi_coding_agent.skills import Skill


def test_create_agent_adds_skill_loader_for_loaded_skills(tmp_path: Path) -> None:
    skill = Skill("review", "Review code", "Read first.", tmp_path / "SKILL.md")
    agent = create_agent(
        cwd=tmp_path,
        provider="deepseek",
        model_id="deepseek-v4-pro",
        skills=[skill],
    )
    assert agent.state.tools[-1].name == "load_skill"
    
def test_parse_args_supports_single_prompt_and_model_selection() -> None:
    args = parse_args(["--provider", "deepseek", "--model", "deepseek-v4-flash", "--skill", "hello-file", "-p", "hello"])
    assert args.provider == "deepseek"
    assert args.model == "deepseek-v4-flash"
    assert args.prompt == "hello"
    assert args.skill == "hello-file"


def test_create_agent_wires_six_tools_and_system_prompt(tmp_path: Path) -> None:
    agent = create_agent(cwd=tmp_path, provider="deepseek", model_id="deepseek-v4-pro")
    assert [tool.name for tool in agent.state.tools] == ["bash", "read", "write", "edit", "grep", "glob"]
    assert "Working directory:" in agent.state.system_prompt
    assert agent.state.model.id == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_run_cli_injects_the_explicitly_selected_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "hello-file"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: hello-file\ndescription: Create a hello file\n---\nWrite then read hello.txt.",
        encoding="utf-8",
    )
    prompts: list[str] = []

    class FakeAgent:
        def subscribe(self, _listener: object) -> object:
            return lambda: None

        async def prompt(self, prompt: str) -> None:
            prompts.append(prompt)

        def abort(self) -> None:
            return None

    monkeypatch.setattr(cli, "create_agent", lambda **_kwargs: FakeAgent())
    args = parse_args(["--skills-dir", str(skill_dir), "--skill", "hello-file", "-p", "Execute it."])

    assert await cli.run_cli(args, write=lambda _text: None) == 0
    assert '<skill name="hello-file"' in prompts[0]
    assert prompts[0].endswith("Execute it.")
