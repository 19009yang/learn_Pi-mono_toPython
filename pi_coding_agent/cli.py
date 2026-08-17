"""Codex-style command REPL and single-prompt CLI for the Python coding agent."""

from __future__ import annotations

import argparse
import asyncio
from dotenv import load_dotenv
import json
import shlex
import signal
import sys
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path

from pi_agent.agent import Agent, AgentOptions
from pi_agent.harness import AgentHarness
from pi_agent.session import (
    InMemorySessionRepo,
    JsonlSessionRepo,
    Session,
    SessionRepo,
    SQLiteSessionRepo,
    SessionStorageError,
    uuidv7,
)
from pi_agent.types import AgentEvent
from pi_ai.models import MutableModels, create_models
from pi_ai.providers.deepseek import deepseek_provider
from pi_ai.providers.qwen import qwen_provider
from pi_ai.types import Model
from pi_coding_agent.skills import Skill, format_skill_invocation, load_skills
from pi_coding_agent.system_prompt import build_system_prompt
from pi_coding_agent.tools import create_default_tools

DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-v4-pro"


class ReplAction(Enum):
    """Outcome of handling one interactive input line."""

    PROMPT = "prompt"
    CONTINUE = "continue"
    EXIT = "exit"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Python pi coding agent")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, help="Provider ID (default: deepseek)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model ID within the selected provider")
    parser.add_argument("-p", "--prompt", help="Run one prompt and exit")
    parser.add_argument("--skill", help="Explicitly invoke a loaded Skill by name with --prompt")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for tools")
    parser.add_argument(
        "--session",
        help="Session ID to create or resume; interactive mode auto-creates one",
    )
    parser.add_argument(
        "--session-backend",
        choices=("sqlite", "jsonl"),
        default="sqlite",
        help="Persistent session backend (default: sqlite)",
    )
    parser.add_argument(
        "--session-db",
        type=Path,
        help="SQLite session database (default: <cwd>/.pi/sessions.db)",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="JSONL session directory used with --session-backend jsonl",
    )
    parser.add_argument(
        "--skills-dir",
        action="append",
        type=Path,
        default=[],
        help="Directory containing a root SKILL.md; may be provided multiple times",
    )
    return parser.parse_args(argv)


def create_default_models() -> MutableModels:
    """Create the MVP model registry. Add providers here as they are implemented."""
    models = create_models()
    models.set_provider(deepseek_provider())
    models.set_provider(qwen_provider())
    return models


def select_model(models: MutableModels, provider: str, model_id: str) -> Model:
    model = models.get_model(provider, model_id)
    if model is None:
        available = ", ".join(entry.id for entry in models.get_models(provider)) or "(none)"
        raise ValueError(f"Unknown model {provider}/{model_id}. Available: {available}")
    return model


def create_session_repo(
    *,
    cwd: Path,
    session_backend: str,
    session_db: Path | None = None,
    sessions_dir: Path | None = None,
) -> SessionRepo:
    """Create the persistent repository selected by CLI arguments."""

    resolved_cwd = cwd.resolve()
    if session_backend == "sqlite":
        database = (session_db or (resolved_cwd / ".pi" / "sessions.db")).resolve()
        return SQLiteSessionRepo(database)
    if session_backend == "jsonl":
        root = (sessions_dir or (resolved_cwd / ".pi" / "sessions")).resolve()
        return JsonlSessionRepo(root)
    raise ValueError(f"Unknown session backend: {session_backend}")


async def open_or_create_session(repo: SessionRepo, session_id: str) -> Session:
    """Open a named session when present, otherwise create it."""

    known_sessions = {metadata.id for metadata in await repo.list()}
    return (
        await repo.open(session_id)
        if session_id in known_sessions
        else await repo.create(session_id=session_id)
    )


def create_agent(
    *,
    cwd: Path,
    provider: str,
    model_id: str,
    skill_directories: Sequence[Path] = (),
    skills: Sequence[Skill] | None = None,
    models: MutableModels | None = None,
) -> Agent:
    """Compose Models, tools, prompt, and Agent without starting a session."""
    resolved_cwd = cwd.resolve()
    registry = models or create_default_models()
    model = select_model(registry, provider, model_id)
    loaded_skills = list(skills) if skills is not None else load_skills(skill_directories)
    tools = create_default_tools(resolved_cwd,loaded_skills)
    return Agent(
        AgentOptions(
            initial_state={
                "model": model,
                "tools": tools,
                "system_prompt": build_system_prompt(loaded_skills, resolved_cwd, tools),
            },
            stream_fn=registry.stream_simple,
        )
    )


async def create_cli_harness(
    *,
    cwd: Path,
    provider: str,
    model_id: str,
    skill_directories: Sequence[Path] = (),
    skills: Sequence[Skill] | None = None,
    session_id: str | None = None,
    session_backend: str = "sqlite",
    session_db: Path | None = None,
    sessions_dir: Path | None = None,
) -> AgentHarness:
    """Compose the CLI Agent with durable or in-memory Harness state."""

    resolved_cwd = cwd.resolve()
    models = create_default_models()
    agent = create_agent(
        cwd=resolved_cwd,
        provider=provider,
        model_id=model_id,
        skill_directories=skill_directories,
        skills=skills,
        models=models,
    )

    if session_id is None:
        session = await InMemorySessionRepo().create()
    else:
        repo = create_session_repo(
            cwd=resolved_cwd,
            session_backend=session_backend,
            session_db=session_db,
            sessions_dir=sessions_dir,
        )
        session = await open_or_create_session(repo, session_id)

    return await AgentHarness.create(agent, session, models=models)


class CliRepl:
    """Stateful slash-command dispatcher for the interactive CLI."""

    def __init__(
        self,
        *,
        harness: AgentHarness,
        repo: SessionRepo,
        session_id: str,
        session_backend: str,
        cwd: Path,
        write: Callable[[str], None],
        read: Callable[[str], str],
    ) -> None:
        self.harness = harness
        self.repo = repo
        self.session_id = session_id
        self.session_backend = session_backend
        self.cwd = cwd.resolve()
        self.write = write
        self.read = read
        self._commands = {
            "help": self._help,
            "clear": self._clear,
            "new": self._new,
            "resume": self._resume,
            "model": self._model,
            "status": self._status,
            "compact": self._compact,
            "exit": self._exit,
            "quit": self._exit,
        }

    @property
    def prompt_label(self) -> str:
        model = self.harness.agent.state.model
        short_session = self.session_id[:8]
        return f"pi [{model.provider}/{model.id} | {short_session}]> "

    def show_welcome(self) -> None:
        model = self.harness.agent.state.model
        self._line("Python Pi Coding Agent")
        self._line(f"model: {model.provider}/{model.id}")
        self._line(f"session: {self.session_id} ({self.session_backend})")
        self._line("Type /help for available commands.")

    async def dispatch(self, line: str) -> ReplAction:
        """Handle a slash command, or report that the line is an Agent prompt."""

        if not line.startswith("/"):
            return ReplAction.PROMPT
        try:
            parts = shlex.split(line[1:])
        except ValueError as error:
            self._line(f"Invalid command: {error}")
            return ReplAction.CONTINUE
        if not parts:
            return ReplAction.CONTINUE
        command, *arguments = parts
        handler = self._commands.get(command.lower())
        if handler is None:
            self._line(f"Unknown command: /{command}. Type /help for available commands.")
            return ReplAction.CONTINUE
        try:
            return await handler(arguments)
        except (RuntimeError, SessionStorageError, ValueError) as error:
            self._line(f"Command failed: {error}")
            return ReplAction.CONTINUE

    async def _help(self, arguments: list[str]) -> ReplAction:
        self._require_no_arguments("help", arguments)
        self._line(
            "\n".join(
                [
                    "Available commands:",
                    "  /help                         Show this help",
                    "  /clear                        Start a new empty session",
                    "  /new [session-id]             Start a new optionally named session",
                    "  /resume [session-id]          Resume a saved session",
                    "  /model [provider/model]       List or switch models",
                    "  /status                       Show current CLI state",
                    "  /compact                      Compact the current context",
                    "  /exit, /quit                  Exit the CLI",
                ]
            )
        )
        return ReplAction.CONTINUE

    async def _clear(self, arguments: list[str]) -> ReplAction:
        self._require_no_arguments("clear", arguments)
        await self._create_and_switch_session(None)
        self._line(f"Context cleared. New session: {self.session_id}")
        return ReplAction.CONTINUE

    async def _new(self, arguments: list[str]) -> ReplAction:
        if len(arguments) > 1:
            raise ValueError("usage: /new [session-id]")
        requested_id = arguments[0] if arguments else None
        await self._create_and_switch_session(requested_id)
        self._line(f"Started session: {self.session_id}")
        return ReplAction.CONTINUE

    async def _resume(self, arguments: list[str]) -> ReplAction:
        if len(arguments) > 1:
            raise ValueError("usage: /resume [session-id]")
        session_id = arguments[0] if arguments else await self._choose_session()
        if session_id is None:
            return ReplAction.CONTINUE
        session = await self.repo.open(session_id)
        await self._switch_session(session, session_id)
        self._line(
            f"Resumed session: {session_id} "
            f"({len(self.harness.agent.state.messages)} messages)"
        )
        return ReplAction.CONTINUE

    async def _model(self, arguments: list[str]) -> ReplAction:
        model = await self._choose_model(arguments)
        if model is None:
            return ReplAction.CONTINUE
        self.harness.agent.state.model = model
        self._line(f"Switched model: {model.provider}/{model.id}")
        return ReplAction.CONTINUE

    async def _status(self, arguments: list[str]) -> ReplAction:
        self._require_no_arguments("status", arguments)
        model = self.harness.agent.state.model
        self._line(
            "\n".join(
                [
                    f"model: {model.provider}/{model.id}",
                    f"session: {self.session_id}",
                    f"backend: {self.session_backend}",
                    f"messages: {len(self.harness.agent.state.messages)}",
                    f"cwd: {self.cwd}",
                ]
            )
        )
        return ReplAction.CONTINUE

    async def _compact(self, arguments: list[str]) -> ReplAction:
        self._require_no_arguments("compact", arguments)
        if not self.harness.agent.state.messages:
            self._line("Nothing to compact.")
            return ReplAction.CONTINUE
        result = await self.harness.compact()
        if result is None:
            self._line("Context does not need compaction.")
        else:
            self._line(
                f"Compacted context: {result.tokens_before} -> "
                f"{result.tokens_after} tokens"
            )
        return ReplAction.CONTINUE

    async def _exit(self, arguments: list[str]) -> ReplAction:
        self._require_no_arguments("exit", arguments)
        return ReplAction.EXIT

    async def _choose_session(self) -> str | None:
        sessions = await self.repo.list()
        if not sessions:
            self._line("No saved sessions.")
            return None
        self._line("Saved sessions:")
        for index, metadata in enumerate(sessions, start=1):
            marker = " *" if metadata.id == self.session_id else ""
            self._line(f"  {index}. {metadata.id}{marker}")
        choice = self._read_choice("Select session number or ID (blank to cancel): ")
        if not choice:
            return None
        if choice.isdigit():
            index = int(choice)
            if index < 1 or index > len(sessions):
                raise ValueError(f"session selection out of range: {choice}")
            return sessions[index - 1].id
        return choice

    async def _choose_model(self, arguments: list[str]) -> Model | None:
        models = self.harness.models
        if models is None:
            raise RuntimeError("model registry is unavailable")

        if len(arguments) > 2:
            raise ValueError("usage: /model [provider/model] or /model [provider] [model]")
        if len(arguments) == 2:
            return select_model(models, arguments[0], arguments[1])
        if len(arguments) == 1:
            target = arguments[0]
            if "/" in target:
                provider, model_id = target.split("/", 1)
                return select_model(models, provider, model_id)
            provider_models = models.get_models(target)
            if provider_models:
                if len(provider_models) == 1:
                    return provider_models[0]
                return self._select_model_from_list(provider_models)
            matches = [model for model in models.get_models() if model.id == target]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError(f"model ID is ambiguous; use provider/{target}")
            raise ValueError(f"unknown provider or model: {target}")
        return self._select_model_from_list(models.get_models())

    def _select_model_from_list(self, models: Sequence[Model]) -> Model | None:
        if not models:
            self._line("No models are registered.")
            return None
        current = self.harness.agent.state.model
        self._line("Available models:")
        for index, model in enumerate(models, start=1):
            marker = " *" if (model.provider, model.id) == (current.provider, current.id) else ""
            self._line(f"  {index}. {model.provider}/{model.id}{marker}")
        choice = self._read_choice("Select model number (blank to cancel): ")
        if not choice:
            return None
        if not choice.isdigit():
            raise ValueError("model selection must be a number")
        index = int(choice)
        if index < 1 or index > len(models):
            raise ValueError(f"model selection out of range: {choice}")
        return models[index - 1]

    async def _create_and_switch_session(self, session_id: str | None) -> None:
        session = await self.repo.create(session_id=session_id)
        metadata = await session.get_metadata()
        await self._switch_session(session, metadata.id)

    async def _switch_session(self, session: Session, session_id: str) -> None:
        # Decode the target before detaching the working Harness, so a corrupt
        # session leaves the current conversation usable.
        await session.build_context()
        previous = self.harness
        agent = previous.agent
        models = previous.models
        settings = previous.compaction_settings
        await previous.close()
        try:
            self.harness = await AgentHarness.create(
                agent,
                session,
                models=models,
                compaction_settings=settings,
            )
        except Exception:
            self.harness = await AgentHarness.create(
                agent,
                previous.session,
                models=models,
                compaction_settings=settings,
            )
            raise
        self.session_id = session_id

    def _read_choice(self, prompt: str) -> str:
        try:
            return self.read(prompt).strip()
        except EOFError:
            self._line("")
            return ""

    def _require_no_arguments(self, command: str, arguments: list[str]) -> None:
        if arguments:
            raise ValueError(f"usage: /{command}")

    def _line(self, text: str) -> None:
        self.write(f"{text}\n")


def render_event(event: AgentEvent, write: Callable[[str], None]) -> None:
    """Render streamed text, thinking, tool calls, and failures for a plain terminal."""
    if event.type == "message_update":
        update = event.assistant_message_event
        if update.type == "text_delta":
            write(update.delta)
        elif update.type == "thinking_delta":
            write(update.delta)
    elif event.type == "tool_execution_start":
        write(f"\n[tool] {event.tool_name} {json.dumps(event.args, ensure_ascii=False)}\n")
    elif event.type == "tool_execution_end" and event.is_error:
        write(f"[tool error] {event.tool_name}\n")
    elif event.type == "message_end" and getattr(event.message, "role", None) == "assistant":
        error = getattr(event.message, "error_message", None)
        if error:
            write(f"\n[error] {error}\n")


async def run_prompt(
    harness: AgentHarness,
    prompt: str,
    write: Callable[[str], None],
) -> None:
    unsubscribe = harness.agent.subscribe(
        lambda event, _signal: render_event(event, write)
    )
    try:
        await harness.prompt(prompt)
        write("\n")
    finally:
        unsubscribe()


async def run_cli(
    args: argparse.Namespace,
    write: Callable[[str], None] | None = None,
    read: Callable[[str], str] | None = None,
) -> int:
    output = write or (lambda text: print(text, end="", flush=True))
    reader = read or input
    skills = load_skills(args.skills_dir)
    if args.skill is not None and args.prompt is None:
        print("--skill requires -p/--prompt", file=sys.stderr)
        return 2

    interactive = args.prompt is None
    session_id = args.session or (uuidv7() if interactive else None)
    try:
        harness = await create_cli_harness(
            cwd=args.cwd,
            provider=args.provider,
            model_id=args.model,
            skill_directories=args.skills_dir,
            skills=skills,
            session_id=session_id,
            session_backend=args.session_backend,
            session_db=args.session_db,
            sessions_dir=args.sessions_dir,
        )
    except (ValueError, SessionStorageError) as error:
        print(error, file=sys.stderr)
        return 2

    repl: CliRepl | None = None
    if interactive:
        assert session_id is not None
        repo = create_session_repo(
            cwd=args.cwd,
            session_backend=args.session_backend,
            session_db=args.session_db,
            sessions_dir=args.sessions_dir,
        )
        repl = CliRepl(
            harness=harness,
            repo=repo,
            session_id=session_id,
            session_backend=args.session_backend,
            cwd=args.cwd,
            write=output,
            read=reader,
        )

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(
        signal.SIGINT,
        lambda _number, _frame: (repl.harness if repl is not None else harness).agent.abort(),
    )
    try:
        if args.prompt is not None:
            prompt = args.prompt
            if args.skill is not None:
                selected_skill = next((skill for skill in skills if skill.name == args.skill), None)
                if selected_skill is None:
                    print(f"Unknown Skill: {args.skill}", file=sys.stderr)
                    return 2
                prompt = format_skill_invocation(selected_skill, prompt)
            await run_prompt(harness, prompt, output)
            return 0

        assert repl is not None
        repl.show_welcome()
        while True:
            try:
                prompt = reader(repl.prompt_label).strip()
            except EOFError:
                output("\n")
                return 0
            if not prompt:
                continue
            action = await repl.dispatch(prompt)
            if action is ReplAction.EXIT:
                return 0
            if action is ReplAction.PROMPT:
                await run_prompt(repl.harness, prompt, output)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        await (repl.harness if repl is not None else harness).close()


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    return asyncio.run(run_cli(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
