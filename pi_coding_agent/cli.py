"""Minimal line-oriented CLI for the Python coding agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from pi_agent.agent import Agent, AgentOptions
from pi_agent.types import AgentEvent
from pi_ai.models import MutableModels, create_models
from pi_ai.providers.deepseek import deepseek_provider
from pi_ai.types import Model
from pi_coding_agent.skills import load_skills
from pi_coding_agent.system_prompt import build_system_prompt
from pi_coding_agent.tools import create_default_tools

DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-v4-pro"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Python pi coding agent")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, help="Provider ID (default: deepseek)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model ID within the selected provider")
    parser.add_argument("-p", "--prompt", help="Run one prompt and exit")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for tools")
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
    return models


def select_model(models: MutableModels, provider: str, model_id: str) -> Model:
    model = models.get_model(provider, model_id)
    if model is None:
        available = ", ".join(entry.id for entry in models.get_models(provider)) or "(none)"
        raise ValueError(f"Unknown model {provider}/{model_id}. Available: {available}")
    return model


def create_agent(
    *,
    cwd: Path,
    provider: str,
    model_id: str,
    skill_directories: Sequence[Path] = (),
    models: MutableModels | None = None,
) -> Agent:
    """Compose Models, tools, prompt, and Agent without starting a session."""
    resolved_cwd = cwd.resolve()
    registry = models or create_default_models()
    model = select_model(registry, provider, model_id)
    tools = create_default_tools(resolved_cwd)
    skills = load_skills(skill_directories)
    return Agent(
        AgentOptions(
            initial_state={
                "model": model,
                "tools": tools,
                "system_prompt": build_system_prompt(skills, resolved_cwd, tools),
            },
            stream_fn=registry.stream_simple,
        )
    )


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


async def run_prompt(agent: Agent, prompt: str, write: Callable[[str], None]) -> None:
    unsubscribe = agent.subscribe(lambda event, _signal: render_event(event, write))
    try:
        await agent.prompt(prompt)
        write("\n")
    finally:
        unsubscribe()


async def run_cli(args: argparse.Namespace, write: Callable[[str], None] | None = None) -> int:
    output = write or (lambda text: print(text, end="", flush=True))
    try:
        agent = create_agent(
            cwd=args.cwd,
            provider=args.provider,
            model_id=args.model,
            skill_directories=args.skills_dir,
        )
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda _number, _frame: agent.abort())
    try:
        if args.prompt is not None:
            await run_prompt(agent, args.prompt, output)
            return 0
        while True:
            try:
                prompt = input("pi> ").strip()
            except EOFError:
                output("\n")
                return 0
            if not prompt:
                continue
            await run_prompt(agent, prompt, output)
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run_cli(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
