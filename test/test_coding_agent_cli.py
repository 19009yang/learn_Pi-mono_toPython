"""Phase 3.3 tests for CLI argument parsing and agent composition."""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_coding_agent import cli
from pi_coding_agent.cli import (
    CliRepl,
    ReplAction,
    create_agent,
    create_cli_harness,
    parse_args,
)
from pi_coding_agent.skills import Skill
from pi_agent.session import (
    InMemorySessionStorage,
    JsonlSessionStorage,
    SQLiteSessionRepo,
    SQLiteSessionStorage,
)
from pi_ai.types import UserMessage


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
    args = parse_args([
        "--provider", "deepseek", "--model", "deepseek-v4-flash",
        "--skill", "hello-file", "--session", "demo", "-p", "hello",
    ])
    assert args.provider == "deepseek"
    assert args.model == "deepseek-v4-flash"
    assert args.prompt == "hello"
    assert args.skill == "hello-file"
    assert args.session == "demo"
    assert args.session_backend == "sqlite"


def test_create_agent_wires_six_tools_and_system_prompt(tmp_path: Path) -> None:
    agent = create_agent(cwd=tmp_path, provider="deepseek", model_id="deepseek-v4-pro")
    assert [tool.name for tool in agent.state.tools] == ["bash", "read", "write", "edit", "grep", "glob", "search"]
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

        def abort(self) -> None:
            return None

    class FakeHarness:
        agent = FakeAgent()

        async def prompt(self, prompt: str) -> None:
            prompts.append(prompt)

        async def close(self) -> None:
            return None

    async def fake_create_harness(**_kwargs: object) -> FakeHarness:
        return FakeHarness()

    monkeypatch.setattr(cli, "create_cli_harness", fake_create_harness)
    args = parse_args(["--skills-dir", str(skill_dir), "--skill", "hello-file", "-p", "Execute it."])

    assert await cli.run_cli(args, write=lambda _text: None) == 0
    assert '<skill name="hello-file"' in prompts[0]
    assert prompts[0].endswith("Execute it.")


@pytest.mark.asyncio
async def test_create_cli_harness_uses_memory_without_session_id(tmp_path: Path) -> None:
    harness = await create_cli_harness(
        cwd=tmp_path,
        provider="deepseek",
        model_id="deepseek-v4-pro",
    )
    try:
        assert isinstance(harness.session.storage, InMemorySessionStorage)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_create_cli_harness_creates_and_restores_jsonl_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    first = await create_cli_harness(
        cwd=tmp_path,
        provider="deepseek",
        model_id="deepseek-v4-pro",
        session_id="resume-demo",
        session_backend="jsonl",
        sessions_dir=sessions_dir,
    )
    message = UserMessage(content="persisted", timestamp=1)
    await first.session.append_message(message)
    await first.close()

    restored = await create_cli_harness(
        cwd=tmp_path,
        provider="deepseek",
        model_id="deepseek-v4-pro",
        session_id="resume-demo",
        session_backend="jsonl",
        sessions_dir=sessions_dir,
    )
    try:
        assert isinstance(restored.session.storage, JsonlSessionStorage)
        assert restored.agent.state.messages == [message]
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_create_cli_harness_creates_and_restores_sqlite_session(tmp_path: Path) -> None:
    database = tmp_path / ".pi" / "sessions.db"
    first = await create_cli_harness(
        cwd=tmp_path,
        provider="deepseek",
        model_id="deepseek-v4-pro",
        session_id="resume-demo",
    )
    message = UserMessage(content="persisted", timestamp=1)
    await first.session.append_message(message)
    await first.close()

    restored = await create_cli_harness(
        cwd=tmp_path,
        provider="deepseek",
        model_id="deepseek-v4-pro",
        session_id="resume-demo",
    )
    try:
        assert isinstance(restored.session.storage, SQLiteSessionStorage)
        assert restored.session.storage.path == database.resolve()
        assert restored.agent.state.messages == [message]
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_repl_switches_model_and_reports_status(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    harness = await create_cli_harness(
        cwd=tmp_path,
        provider="deepseek",
        model_id="deepseek-v4-pro",
        session_id="model-demo",
        session_db=database,
    )
    output: list[str] = []
    repl = CliRepl(
        harness=harness,
        repo=SQLiteSessionRepo(database),
        session_id="model-demo",
        session_backend="sqlite",
        cwd=tmp_path,
        write=output.append,
        read=lambda _prompt: "",
    )
    try:
        assert await repl.dispatch("/model qwen/qwen3.7-plus") is ReplAction.CONTINUE
        assert repl.harness.agent.state.model.provider == "qwen"
        assert repl.harness.agent.state.model.id == "qwen3.7-plus"

        assert await repl.dispatch("/status") is ReplAction.CONTINUE
        rendered = "".join(output)
        assert "model: qwen/qwen3.7-plus" in rendered
        assert "session: model-demo" in rendered
        assert "backend: sqlite" in rendered
    finally:
        await repl.harness.close()


@pytest.mark.asyncio
async def test_repl_clear_preserves_old_session_for_resume(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    harness = await create_cli_harness(
        cwd=tmp_path,
        provider="deepseek",
        model_id="deepseek-v4-pro",
        session_id="old-session",
        session_db=database,
    )
    message = UserMessage(content="remember me", timestamp=1)
    leaf_id = await harness.session.append_message(message)
    await harness.navigate_tree(leaf_id)
    output: list[str] = []
    repl = CliRepl(
        harness=harness,
        repo=SQLiteSessionRepo(database),
        session_id="old-session",
        session_backend="sqlite",
        cwd=tmp_path,
        write=output.append,
        read=lambda _prompt: "",
    )
    try:
        assert await repl.dispatch("/clear") is ReplAction.CONTINUE
        new_session_id = repl.session_id
        assert new_session_id != "old-session"
        assert repl.harness.agent.state.messages == []
        assert {item.id for item in await repl.repo.list()} == {
            "old-session",
            new_session_id,
        }

        assert await repl.dispatch("/resume old-session") is ReplAction.CONTINUE
        assert repl.session_id == "old-session"
        assert repl.harness.agent.state.messages == [message]
        assert "Resumed session: old-session (1 messages)" in "".join(output)
    finally:
        await repl.harness.close()


@pytest.mark.asyncio
async def test_repl_interactive_resume_model_help_and_exit(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    repo = SQLiteSessionRepo(database)
    target = await repo.create(session_id="target-session")
    await target.append_message(UserMessage(content="target", timestamp=2))
    harness = await create_cli_harness(
        cwd=tmp_path,
        provider="deepseek",
        model_id="deepseek-v4-pro",
        session_id="current-session",
        session_db=database,
    )
    choices = iter(["target-session", "1"])
    output: list[str] = []
    repl = CliRepl(
        harness=harness,
        repo=repo,
        session_id="current-session",
        session_backend="sqlite",
        cwd=tmp_path,
        write=output.append,
        read=lambda _prompt: next(choices),
    )
    try:
        assert await repl.dispatch("/resume") is ReplAction.CONTINUE
        assert repl.session_id == "target-session"
        assert repl.harness.agent.state.messages == [UserMessage(content="target", timestamp=2)]

        assert await repl.dispatch("/model qwen") is ReplAction.CONTINUE
        assert repl.harness.agent.state.model.provider == "qwen"
        assert await repl.dispatch("/model") is ReplAction.CONTINUE
        assert repl.harness.agent.state.model.provider == "deepseek"
        assert await repl.dispatch("/help") is ReplAction.CONTINUE
        assert await repl.dispatch("/compact") is ReplAction.CONTINUE
        assert await repl.dispatch("/unknown") is ReplAction.CONTINUE
        assert await repl.dispatch("ordinary prompt") is ReplAction.PROMPT
        assert await repl.dispatch("/quit") is ReplAction.EXIT
        rendered = "".join(output)
        assert "Available commands:" in rendered
        assert "Unknown command: /unknown" in rendered
    finally:
        await repl.harness.close()


@pytest.mark.asyncio
async def test_interactive_cli_auto_creates_persistent_session(tmp_path: Path) -> None:
    commands = iter(["/status", "/exit"])
    output: list[str] = []
    args = parse_args(["--cwd", str(tmp_path)])

    assert await cli.run_cli(
        args,
        write=output.append,
        read=lambda _prompt: next(commands),
    ) == 0

    sessions = await SQLiteSessionRepo(tmp_path / ".pi" / "sessions.db").list()
    assert len(sessions) == 1
    rendered = "".join(output)
    assert "Python Pi Coding Agent" in rendered
    assert f"session: {sessions[0].id}" in rendered
