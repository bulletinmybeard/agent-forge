"""UI helpers — ChalkBox wrappers for consistent console output and logging.

Provides reusable display patterns for examples and applications built on
the framework.  All output goes through ChalkBox components so the look
and feel is consistent without every file importing half the library.

Logging is routed through ChalkBox's logging bridge so console output is
styled with Rich and, optionally, written to a structured JSON file.

Usage::

    from agentforge.ui import UI, setup_logging, get_logger

    # In examples — configure the root logger once
    setup_logging(level="INFO")

    # In library modules — get a named logger
    logger = get_logger(__name__)
    logger.info("Tool registered: %s", name)

    # UI helpers (unchanged)
    UI.header("System Analysis Agent")
    UI.config({"Profile": "default", "Model": "mistral-large", "Tools": 33})
    with UI.spinner("Running agent...") as sp:
        result = do_work()
        sp.success("Done in 4.2s")
    UI.result(result)
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from chalkbox import (
    Alert,
    Divider,
    KeyValue,
    Markdown,
    Section,
    Spinner,
    Table,
    get_console,
)
from chalkbox.logging.bridge import (
    StructuredLogger,
    get_logger,
)
from chalkbox.logging.bridge import (
    setup_logging as _chalkbox_setup_logging,
)

__all__ = ["UI", "setup_logging", "get_logger", "StructuredLogger"]


# ---------------------------------------------------------------------------
# Logging setup with clean framework defaults
# ---------------------------------------------------------------------------


def setup_logging(
    level: str = "INFO",
    *,
    json_file: str | None = None,
    show_time: bool = False,
    show_level: bool = False,
    show_path: bool = False,
    rich_tracebacks: bool = True,
    suppress_http: bool = True,
) -> None:
    """Configure logging with clean defaults for framework console output.

    By default timestamps, level badges, and file paths are hidden so
    framework info messages (e.g., ``[ToolPipeline] Starting ...``) print
    as plain, readable lines.  Pass ``show_time=True`` etc. to restore
    the full Rich handler chrome when needed.

    ``suppress_http`` silences the chatty ``httpx`` / ``httpcore`` loggers
    at WARNING level so HTTP round-trips don't clutter the terminal.
    """
    _chalkbox_setup_logging(
        level=level,
        json_file=json_file,
        show_time=show_time,
        show_level=show_level,
        show_path=show_path,
        rich_tracebacks=rich_tracebacks,
    )
    if suppress_http:
        get_logger("httpx", level="WARNING")
        get_logger("httpcore", level="WARNING")


class UI:
    """Static helper methods for common console output patterns."""

    # Active spinner reference — set/cleared by spinner() callers so that
    # interactive prompts (e.g., confirm_tool) can pause the Live display.
    _active_spinner: Spinner | None = None

    # -- Console access -----------------------------------------------------

    @staticmethod
    def console():
        """Get the shared ChalkBox console."""
        return get_console()

    @staticmethod
    def print(*args: Any, **kwargs: Any) -> None:
        """Print via ChalkBox console (respects themes)."""
        get_console().print(*args, **kwargs)

    # -- Headers & Dividers -------------------------------------------------

    @staticmethod
    def header(title: str, subtitle: str | None = None) -> None:
        """Print a prominent section header."""
        get_console().print()
        Divider.double(title).print()
        if subtitle:
            get_console().print(f"  [dim]{subtitle}[/dim]")

    @staticmethod
    def divider(title: str = "") -> None:
        """Print a horizontal divider."""
        Divider(title).print()

    @staticmethod
    def subheader(title: str) -> None:
        """Print a lighter sub-section divider."""
        Divider.light(title, align="left").print()

    # -- Configuration / Key-Value ------------------------------------------

    @staticmethod
    def config(data: dict[str, Any], title: str | None = None) -> None:
        """Display a key-value configuration block."""
        kv = KeyValue(data, title=title)
        get_console().print(kv)

    # -- Sections (panels) --------------------------------------------------

    @staticmethod
    def section(title: str, **kwargs: Any) -> Section:
        """Create a Section context manager for grouped content.

        Usage::

            with UI.section("Results") as s:
                s.add_text("Everything looks good.")
        """
        return Section(title, **kwargs)

    # -- Spinners -----------------------------------------------------------

    @staticmethod
    def spinner(text: str = "Working...", transient: bool = True) -> Spinner:
        """Create a *transient* spinner (disappears when the block exits).

        The intended pattern is to let the spinner vanish on exit and then
        print a static success/error line via :meth:`UI.success` or
        :meth:`UI.error`::

            with UI.spinner("Thinking..."):
                result = slow_call()
            UI.success("Done in 3.2s")

        This avoids the double-print that occurs when ``sp.success()`` is
        called inside a non-transient spinner (ChalkBox renders the status
        via the live display *and* reprints it on ``__exit__``).
        """
        return Spinner(text, transient=transient)

    # -- Tables -------------------------------------------------------------

    @staticmethod
    def table(
        headers: list[str],
        rows: list[list[str]],
        title: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Print a table."""
        t = Table(title=title, headers=headers, **kwargs)
        t.add_rows(rows)
        get_console().print(t)

    @staticmethod
    def tool_summary(iterations: list[Any], elapsed: float) -> None:
        """Print a summary of agent iterations and tool usage.

        Expects ``iterations`` to be a list of ``AgentIteration`` dataclasses.
        """
        all_tools: list[str] = []
        for it in iterations:
            if it.tool_calls:
                for tc in it.tool_calls:
                    all_tools.append(tc["name"])

        console = get_console()

        # Summary line
        console.print()
        Divider.light("Agent Summary").print()
        kv = KeyValue()
        kv.add("Iterations", len(iterations))
        kv.add("Total time", f"{elapsed:.1f}s")
        kv.add("Tools called", f"{len(all_tools)} total")
        console.print(kv)

        if all_tools:
            counts = Counter(all_tools)
            t = Table(headers=["Tool", "Calls"], row_styles="alternate")
            for name, count in counts.most_common():
                t.add_row(name, f"{count}x")
            console.print(t)

    # -- Tool call display --------------------------------------------------

    _tool_call_buffer: list[str] = []

    @staticmethod
    def tool_call(name: str, args: dict[str, Any]) -> None:
        """Buffer a tool call for batched display."""
        arg_pairs = ", ".join(f"'{k}': '{v}'" for k, v in args.items())
        UI._tool_call_buffer.append(f"{name}({arg_pairs})")

    @staticmethod
    def flush_tool_calls() -> None:
        """Render all buffered tool calls as a single green panel, then clear."""
        if not UI._tool_call_buffer:
            return
        from rich.panel import Panel

        body = "\n".join(UI._tool_call_buffer)
        get_console().print(Panel(body, title="Tool Calls", border_style="green", padding=(0, 1)))
        UI._tool_call_buffer.clear()

    # -- Alerts -------------------------------------------------------------

    @staticmethod
    def info(message: str, **kwargs: Any) -> None:
        """Print an info alert."""
        get_console().print(Alert.info(message, **kwargs))

    @staticmethod
    def success(message: str, **kwargs: Any) -> None:
        """Print a success alert."""
        get_console().print(Alert.success(message, **kwargs))

    @staticmethod
    def warning(message: str, **kwargs: Any) -> None:
        """Print a warning alert."""
        get_console().print(Alert.warning(message, **kwargs))

    @staticmethod
    def error(message: str, **kwargs: Any) -> None:
        """Print an error alert."""
        get_console().print(Alert.error(message, **kwargs))

    @staticmethod
    def errors(errors: list[str]) -> None:
        """Print a list of errors as an alert."""
        if not errors:
            return
        body = "\n".join(f"  - {e}" for e in errors)
        get_console().print(Alert(body, level="error", title=f"Errors ({len(errors)})"))

    # -- Confirmation prompts -----------------------------------------------

    @staticmethod
    def confirm_tool(prompt: str) -> bool:
        """Ask the user to confirm a destructive tool operation.

        Uses ChalkBox's Confirm component for a themed yes/no prompt.
        Automatically pauses the active spinner (if any) so the prompt
        can read from stdin without Rich's Live display blocking it.

        Returns True if confirmed, False if declined.
        """
        from chalkbox.components.prompt import Confirm as ChalkConfirm

        # Pause the spinner so the prompt can use stdin
        live = None
        if UI._active_spinner and UI._active_spinner._live:
            live = UI._active_spinner._live
            live.stop()

        try:
            # Flush buffered tool calls first so the user sees what led here
            UI.flush_tool_calls()
            return ChalkConfirm.ask_once(prompt, default=False)
        finally:
            # Resume the spinner regardless of outcome
            if live:
                live.start()

    # -- Markdown result output ---------------------------------------------

    @staticmethod
    def markdown(text: str) -> None:
        """Render markdown text to the console."""
        get_console().print(Markdown(text))

    @staticmethod
    def result(text: str | None, label: str = "Result") -> None:
        """Display an agent/pipeline result.

        If the result looks like markdown (contains # or ``` or ---),
        it's rendered as markdown.  Otherwise, plain text in a section.
        """
        if not text:
            get_console().print(Alert("No result produced.", level="warning"))
            return

        is_md = any(marker in text for marker in ("# ", "```", "---", "**", "| "))

        with Section(label, expand=True) as s:
            if is_md:
                s.add(Markdown(text).__rich__())
            else:
                s.add_text(text)

    # -- Iteration log (for verbose agent output) ---------------------------

    @staticmethod
    def iteration_log(iterations: list[Any]) -> None:
        """Print a detailed per-iteration log.

        Expects ``iterations`` to be a list of ``AgentIteration`` dataclasses.
        """
        console = get_console()
        Divider.light("Iteration Log").print()

        for it in iterations:
            tools = [tc["name"] for tc in (it.tool_calls or [])]
            if tools:
                console.print(
                    f"  [dim]#{it.iteration}[/dim]  Tools: [bold]{', '.join(tools)}[/bold]"
                    f"  [dim]({it.duration:.2f}s)[/dim]"
                )
            else:
                console.print(
                    f"  [dim]#{it.iteration}[/dim]  [green]Final answer[/green]  [dim]({it.duration:.2f}s)[/dim]"
                )
