"""Minimal CLI — run a single agent query.

    agentforge "list the markdown files under the current directory"
    agentforge --core-tools-only --profile fast "what is 2+2?"

Reads ``config.yaml`` from the current directory unless ``--config`` is given.
"""

from __future__ import annotations

import argparse
import sys

from agentforge.agent import AgentLoop
from agentforge.client import AIClient
from agentforge.tools import ToolRegistry, register_all_tools, register_core_tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentforge", description="Run an AgentForge agent query.")
    parser.add_argument("query", nargs="+", help="The task or question for the agent.")
    parser.add_argument("--profile", default=None, help="AI profile (default: config's default_profile).")
    parser.add_argument("--config", default=None, help="Path to config.yaml (default: ./config.yaml).")
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--no-tools", action="store_true", help="Run without registering any tools.")
    parser.add_argument("--core-tools-only", action="store_true", help="Skip third-party tool plugins.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print tool activity to stderr.")
    args = parser.parse_args(argv)

    client = AIClient(profile=args.profile, config_path=args.config)

    registry = ToolRegistry()
    if not args.no_tools:
        (register_core_tools if args.core_tools_only else register_all_tools)(registry)

    # Wire interactive confirmation for destructive / sudo commands. Without a
    # handler the registry fails closed (refuses), so prompt on a TTY and
    # decline on a non-TTY (scripted/piped) rather than run unconfirmed.
    def _confirm(prompt: str) -> bool:
        if not sys.stdin.isatty():
            print(f"\n{prompt}\n[declined: no interactive terminal]", file=sys.stderr)
            return False
        try:
            from agentforge.ui import UI

            return UI.confirm_tool(prompt)
        except Exception:
            return input(f"\n{prompt} [y/N] ").strip().lower() in ("y", "yes")

    registry.set_confirm_handler(_confirm)

    from agentforge.tools.cli_sudo_provider import CliSudoProvider
    from agentforge.tools.shell import set_sudo_secret_provider

    set_sudo_secret_provider(CliSudoProvider())

    # Migration: sudo_password is no longer read; warn if it's still configured.
    try:
        from agentforge.config import get_config

        if get_config()._raw.get("tools", {}).get("shell", {}).get("sudo_password"):
            print(
                "warning: tools.shell.sudo_password is no longer used — sudo now "
                "prompts interactively. Remove it from config.yaml.",
                file=sys.stderr,
            )
    except Exception:
        pass

    def on_event(kind: str, data: dict) -> None:
        if args.verbose:
            print(f"[{kind}] {data}", file=sys.stderr)

    agent = AgentLoop(
        client,
        registry,
        max_iterations=args.max_iterations,
        verbose=args.verbose,
        on_event=on_event,
    )
    ctx = agent.run(" ".join(args.query))
    print(ctx.result or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
