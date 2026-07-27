"""Phase 3.3 tests for CLI argument parsing and agent composition."""

from __future__ import annotations

from pathlib import Path

from pi_coding_agent.cli import create_agent, parse_args


def test_parse_args_supports_single_prompt_and_model_selection() -> None:
    args = parse_args(["--provider", "deepseek", "--model", "deepseek-v4-flash", "-p", "hello"])
    assert args.provider == "deepseek"
    assert args.model == "deepseek-v4-flash"
    assert args.prompt == "hello"


def test_create_agent_wires_six_tools_and_system_prompt(tmp_path: Path) -> None:
    agent = create_agent(cwd=tmp_path, provider="deepseek", model_id="deepseek-v4-pro")
    assert [tool.name for tool in agent.state.tools] == ["bash", "read", "write", "edit", "grep", "glob"]
    assert "Working directory:" in agent.state.system_prompt
    assert agent.state.model.id == "deepseek-v4-pro"
