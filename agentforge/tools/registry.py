"""Tool registry — register, discover, and execute tools by name.

Tools are plain Python functions.  They are registered either explicitly
via ``registry.register(func)`` or with the ``@tool`` decorator.

The registry is the single source of truth for which tools exist.  Pipeline
steps (ToolDetector, ToolExecutor) use it to map tool names to callables
and to generate Ollama tool specs for the model.
"""

from __future__ import annotations

import asyncio
import inspect
import typing
from collections.abc import Callable
from typing import Any

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------

# Module-level list that collects functions decorated with @tool.
# A ToolRegistry can sweep this up with ``registry.register_decorated()``.
_decorated_tools: list[Callable] = []


def tool(
    func: Callable | None = None,
    *,
    hint: str | None = None,
    confirm: str | None = None,
    confirm_condition: Callable | None = None,
    locality: str = "local",
) -> Callable:
    """Mark a function as a tool so it can be auto-registered.

    Usage::

        @tool
        def calculator(expression: str) -> str:
            \"\"\"Evaluate a maths expression.\"\"\"
            return str(eval(expression))

        @tool(hint="Always use --tail 200 for log commands.")
        def ssh(host: str, command: str) -> str:
            ...

        @tool(confirm="Delete '{path}'? This cannot be undone.")
        def delete_file(path: str) -> str:
            ...

        @tool(locality="remote")
        def web_search(query: str) -> str:
            ...

    The ``locality`` parameter controls which worker executes the tool:

    - ``"local"``  (default) — runs on the local-role worker on your machine (filesystem, CLI, etc.)
    - ``"remote"`` — runs on the remote worker in Docker (web fetch, APIs, etc.)

    The ``confirm`` parameter is a template string with ``{arg_name}``
    placeholders.  Before execution, the registry formats the template
    with the actual arguments and calls the registered confirm handler.
    If the handler returns False, execution is skipped and a cancellation
    message is returned instead.

    Then, from your setup code::

        registry = ToolRegistry()
        registry.register_decorated()   # picks up all @tool functions
    """

    def _wrap(fn: Callable) -> Callable:
        _decorated_tools.append(fn)
        fn._is_tool = True  # type: ignore[attr-defined]
        fn._locality = locality  # type: ignore[attr-defined]
        if hint:
            fn._model_hint = hint  # type: ignore[attr-defined]
        if confirm:
            fn._confirm_template = confirm  # type: ignore[attr-defined]
        if confirm_condition:
            fn._confirm_condition = confirm_condition  # type: ignore[attr-defined]
        return fn

    # Support both @tool and @tool(hint="...", confirm="...")
    if func is not None:
        # Called as bare @tool (no parentheses)
        return _wrap(func)
    # Called as @tool(hint="...", confirm="...") — return the inner wrapper
    return _wrap


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Name → callable mapping with helpers for Ollama tool specs.

    Usage::

        registry = ToolRegistry()
        registry.register(my_function)
        registry.register(another, name="custom_name")

        # From @tool decorated functions
        registry.register_decorated()

        # List available tools
        registry.list_tools()          # → ["my_function", "custom_name"]

        # Get Ollama tool specs (for passing to AIClient.chat)
        specs = registry.tool_specs()  # list of dicts

        # Execute a tool by name
        result = registry.execute("my_function", {"arg1": "value"})
    """

    # Type map for generating JSON schema from Python type hints
    _TYPE_MAP: dict[type, str] = {
        int: "integer",
        float: "number",
        str: "string",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    def __init__(self) -> None:
        self._tools: dict[str, Callable] = {}
        self._tool_locality: dict[str, str] = {}  # tool_name → "local" | "remote"
        self._category_hints: dict[str, str] = {}  # category → hint text
        self._tool_categories: dict[str, str] = {}  # tool_name → category
        self._on_tool_call: Callable[[str, dict], None] | None = None  # per-call callback
        self._on_tools_complete: Callable[[], None] | None = None  # batch-flush callback
        self._on_confirm: Callable[[str], bool] | None = None  # confirm callback
        self._on_file_diff: Callable[[dict], None] | None = None  # preview-diff callback
        # Tools that MUST run in the registering process and never cross-dispatch
        # (runtime closures bound to live state, e.g., connector tools holding a
        # connection's credentials). The role map would otherwise route them to a
        # worker whose registry never had them. See agent._dispatch_tool.
        self._in_process_tools: set[str] = set()

    def set_tool_call_handler(self, handler: Callable[[str, dict], None]) -> None:
        """Set an optional callback invoked before each tool execution.

        The handler receives ``(tool_name, arguments)`` and is intended for
        buffering tool call info for display.  It does **not** affect
        execution — errors in the handler are silently ignored.
        """
        self._on_tool_call = handler

    def set_tools_complete_handler(self, handler: Callable[[], None]) -> None:
        """Set an optional callback invoked after all tools in a batch execute.

        Intended for flushing buffered tool call displays as a single panel.
        """
        self._on_tools_complete = handler

    def set_confirm_handler(self, handler: Callable[[str], bool]) -> None:
        """Set a callback for destructive-tool confirmation prompts.

        The handler receives a formatted prompt string (e.g.,
        ``"Delete '/tmp/foo.txt'? This cannot be undone."``) and must
        return ``True`` to proceed or ``False`` to cancel.

        When no handler is set, tools with a ``confirm`` template execute
        without prompting (useful for non-interactive / headless runs).
        """
        self._on_confirm = handler

    def set_file_diff_handler(self, handler: Callable[[dict], None]) -> None:
        """Set a callback that emits a file-diff preview card to the client.

        The handler receives a plain dict of file.diff fields (tool, action,
        path, additions, deletions, diff_text, ...). Used by the diff-preview
        confirm flow to show a diff *before* the write is confirmed.
        """
        self._on_file_diff = handler

    def supports_preview_confirm(self) -> bool:
        """True when both a confirm handler and a diff emitter are wired.

        The diff-preview confirm flow only makes sense in an interactive
        session (where the user can see the diff and answer). Headless /
        worker contexts return False and fall back to plain execution.
        """
        return self._on_confirm is not None and self._on_file_diff is not None

    def run_confirm(self, prompt: str) -> bool:
        """Ask the wired confirm handler. True (proceed) when none is set."""
        if self._on_confirm is None:
            return True
        try:
            return bool(self._on_confirm(prompt))
        except Exception:
            return True  # fail-open, matches _check_confirm

    def emit_file_diff(self, payload: dict) -> None:
        """Send a file-diff preview card if a handler is wired (best-effort)."""
        if self._on_file_diff is None:
            return
        try:
            self._on_file_diff(payload)
        except Exception:
            pass  # display errors must never break execution

    def notify_tools_complete(self) -> None:
        """Signal that a batch of tool calls has finished executing."""
        if self._on_tools_complete:
            try:
                self._on_tools_complete()
            except Exception:
                pass

    # -- registration -------------------------------------------------------

    def register(
        self,
        func: Callable,
        *,
        name: str | None = None,
        category: str | None = None,
        in_process: bool = False,
    ) -> None:
        """Register a callable as a tool.

        ``in_process=True`` pins the tool to the registering process — it is
        executed directly and never cross-dispatched to a worker, regardless of
        the role map. Use for runtime closures bound to live state (connector
        tools holding a connection's credentials).
        """
        tool_name = name or func.__name__
        self._tools[tool_name] = func
        self._tool_locality[tool_name] = getattr(func, "_locality", "local")
        if category:
            self._tool_categories[tool_name] = category
        if in_process:
            self._in_process_tools.add(tool_name)
        logger.debug("Registered tool: %s (locality=%s)", tool_name, self._tool_locality[tool_name])

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if it existed."""
        if name in self._tools:
            del self._tools[name]
            self._tool_locality.pop(name, None)
            self._tool_categories.pop(name, None)
            self._in_process_tools.discard(name)
            logger.debug("Unregistered tool: %s", name)
            return True
        return False

    def is_in_process(self, name: str) -> bool:
        """Return True if the tool must run in the registering process."""
        return name in self._in_process_tools

    def register_decorated(self) -> int:
        """Sweep up all functions decorated with ``@tool`` and register them.

        Returns the number of newly registered tools.
        """
        count = 0
        for func in _decorated_tools:
            name = func.__name__
            if name not in self._tools:
                self.register(func)
                count += 1
        return count

    # -- lookup -------------------------------------------------------------

    def get(self, name: str) -> Callable | None:
        """Return the tool callable, or *None* if not found."""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """Return sorted list of registered tool names."""
        return sorted(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # -- model hints --------------------------------------------------------

    def register_category_hint(self, category: str, hint: str) -> None:
        """Register a model instruction hint for an entire tool category.

        These are injected into the system prompt so the model knows how to
        use a group of tools correctly (e.g., "Docker tools only work locally").
        """
        self._category_hints[category] = hint

    def get_model_hints(self) -> str:
        """Collect all category and per-tool hints into a formatted block.

        Returns a string ready to be appended to the system prompt.
        Returns an empty string if no hints are registered.
        """
        sections: list[str] = []

        # Gather per-tool hints grouped by category
        # First, collect tools that have hints
        tool_hints: dict[str, list[tuple[str, str]]] = {}  # category → [(name, hint)]
        for tool_name, func in self._tools.items():
            hint = getattr(func, "_model_hint", None)
            if hint:
                cat = self._tool_categories.get(tool_name, "General")
                tool_hints.setdefault(cat, []).append((tool_name, hint))

        # Build sections: one per category that has either a category hint or tool hints
        all_categories = sorted(set(list(self._category_hints.keys()) + list(tool_hints.keys())))

        if not all_categories:
            return ""

        for cat in all_categories:
            lines: list[str] = []
            lines.append(f"### {cat}")

            # Category-level hint
            cat_hint = self._category_hints.get(cat)
            if cat_hint:
                lines.append(cat_hint)

            # Per-tool hints
            for tool_name, hint in tool_hints.get(cat, []):
                lines.append(f"- {tool_name}: {hint}")

            sections.append("\n".join(lines))

        return "## Tool guidance\n\n" + "\n\n".join(sections)

    def get_model_hints_condensed(self) -> str:
        """Category headers with tool counts only -- no per-tool detail.

        Used for condensed system prompts on iteration 2+ where the model
        has already seen the full hints.
        """
        # Count tools per category
        cat_counts: dict[str, int] = {}
        for tool_name in self._tools:
            cat = self._tool_categories.get(tool_name, "General")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

        if not cat_counts:
            return ""

        lines = ["## Available tools"]
        for cat in sorted(cat_counts):
            lines.append(f"- {cat} ({cat_counts[cat]} tools)")
        return "\n".join(lines)

    # -- guard & confirmation ------------------------------------------------

    def _classify_guard(self, func: Callable, args: dict[str, Any]) -> dict | None:
        """Run the CommandGuard for the shell + ssh tools (if applicable).

        ssh runs an arbitrary command on a remote host, so a destructive clause
        there is exactly as dangerous as in a local shell — classify both. The
        ssh host allowlist limits *where*; the guard limits *what*.

        Returns a dict with guard metadata:
            {
              "source":       "fast-path"|"llm"|"fail-open",
              "destructive":  bool,
              "sudo_only":    bool,   # needs sudo but is non-destructive
              "auto_confirmed": bool, # auto-sudo was applied (no user prompt)
              "verdict":      "destructive"|"sudo"|"safe",
            }
        or None if the guard doesn't apply (tool has no command to classify).
        """
        if func.__name__ not in ("shell", "ssh") or not args.get("command"):
            return None

        try:
            from .command_guard import get_guard

            guard = get_guard()
            verdict = guard.classify(args["command"])

            result = {
                "source": guard.last_source,
                "destructive": verdict == "destructive",
                "sudo_only": verdict == "sudo",
                "auto_confirmed": False,
                "verdict": verdict,
            }

            # Auto-confirm sudo-only commands when auto_sudo is enabled
            if verdict == "sudo" and self._is_auto_sudo_enabled():
                result["auto_confirmed"] = True
                logger.info(
                    "Auto-confirming sudo-only command: %s",
                    str(args.get("command", ""))[:80],
                )

            return result
        except ImportError:
            return None
        except Exception as exc:
            # Fail CLOSED: a guard error must not silently downgrade to "safe".
            # Mark destructive so _check_confirm requires confirmation.
            logger.warning("CommandGuard error (failing closed): %s", exc)
            return {
                "source": "fail-closed",
                "destructive": True,
                "sudo_only": False,
                "auto_confirmed": False,
                "verdict": "destructive",
            }

    @staticmethod
    def _is_auto_sudo_enabled() -> bool:
        """Check if auto_sudo is enabled in config.yaml → tools.shell.auto_sudo."""
        try:
            from agentforge.config import get_config

            cfg = get_config()
            return cfg._raw.get("tools", {}).get("shell", {}).get("auto_sudo", False)
        except Exception:
            return False

    def execute_with_role(self, name: str, args: dict[str, Any]) -> str:
        """Execute a tool honouring its YAML-routed role.

        Same semantics as the Agent's ``_execute_tool_with_role`` — run
        directly when the tool's role matches the current worker's role,
        otherwise enqueue on the other worker's queue via cross-dispatch
        and block until the result arrives.

        Use this wherever you'd otherwise call ``registry.execute("shell", ...)``
        outside of the agent loop — parallel / discovery steps, etc.
        """
        from .routing import my_role

        tool_role = self.get_role(name)
        worker_role = my_role()

        if tool_role == worker_role:
            return str(self.execute(name, args))

        # Confirm (destructive / sudo) on THIS side — remote workers don't have
        # a confirm handler. Matches framework.agent._execute_tool_with_role.
        cancelled, _guard = self.check_confirmation(name, args)
        if cancelled:
            return cancelled

        logger.info(
            "[Registry] Cross-role dispatch: '%s' (tool=%s, worker=%s)",
            name,
            tool_role,
            worker_role,
        )
        try:
            from web.server.queue.dispatch_compat import saq_dispatch_tool

            return str(saq_dispatch_tool(name, args, tool_role))
        except ImportError:
            logger.error(
                "[Registry] dispatch_compat unavailable — running '%s' locally on '%s' worker despite tool_role='%s'",
                name,
                worker_role,
                tool_role,
            )
            return str(self.execute(name, args))

    def check_confirmation(self, name: str, args: dict[str, Any]) -> tuple[str | None, dict | None]:
        """Public entry point: run guard + confirm for a tool *without* executing it.

        Intended for callers that dispatch the tool out-of-process (cross-locality
        workers) and need the user prompt to happen on the agent side where the
        confirm handler is wired. Returns ``(cancellation_message, guard_result)``:

        - ``cancellation_message`` is a user-facing string when denied, else None.
        - ``guard_result`` is the CommandGuard metadata (or None), useful for
          forwarding to the remote worker so ``tool.call`` events carry the
          right badge and the worker can skip its own check.
        """
        func = self._tools.get(name)
        if func is None:
            return None, None
        coerced = self._coerce_args(func, args or {})
        guard_result = self._classify_guard(func, coerced)
        cancelled = self._check_confirm(func, coerced, guard_result)
        return cancelled, guard_result

    def _check_confirm(self, func: Callable, args: dict[str, Any], guard_result: dict | None = None) -> str | None:
        """Check if a tool requires user confirmation before execution.

        Returns a cancellation message string if the user declined,
        or None if execution should proceed.

        Two confirmation paths:
        1. Static: ``@tool(confirm="...")`` — template-based (e.g., delete_file)
        2. Dynamic: CommandGuard result (shell tool only)
        """
        # --- Path 1a: dynamic @tool(confirm_condition=...) ---
        # A callable that receives the tool args and returns a confirm
        # prompt string (triggers confirmation) or None (no confirm needed).
        condition_fn = getattr(func, "_confirm_condition", None)
        if condition_fn and self._on_confirm:
            try:
                prompt = condition_fn(**args)
            except Exception:
                prompt = None
            if prompt:
                try:
                    confirmed = self._on_confirm(prompt)
                except Exception:
                    return None  # fail-open
                if not confirmed:
                    logger.info("Tool '%s' cancelled by user (condition)", func.__name__)
                    return "Operation cancelled by user."

        # --- Path 1b: static @tool(confirm=...) template ---
        template = getattr(func, "_confirm_template", None)
        if template and self._on_confirm:
            try:
                prompt = template.format(**args)
            except (KeyError, IndexError):
                prompt = template
            try:
                confirmed = self._on_confirm(prompt)
            except Exception:
                return None  # fail-open
            if not confirmed:
                logger.info("Tool '%s' cancelled by user", func.__name__)
                return "Operation cancelled by user."

        # --- Path 2: dynamic CommandGuard for the shell tool ---
        # Sudo-only commands are gated downstream by the interactive password
        # prompt (the prompt IS the consent), so we do NOT y/n-confirm them here.
        # Only destructive commands still require the confirm gate.
        needs_destructive = bool(guard_result and guard_result.get("destructive"))
        if needs_destructive:
            cmd = args.get("command", "???")
            if self._on_confirm is None:
                logger.warning("Refusing destructive shell command without a confirm handler: %s", str(cmd)[:80])
                return (
                    "Refused: this command requires confirmation but no confirmation handler is "
                    "available in this context. Run it from an interactive session, or have a "
                    "trusted caller pass skip_confirm=True."
                )
            prompt = f"! This command may be destructive:\n  $ {cmd}\nExecute anyway?"
            try:
                confirmed = self._on_confirm(prompt)
            except Exception as exc:
                logger.warning("Confirm handler error (refusing): %s", exc)
                return "Operation cancelled (confirmation handler error)."
            if not confirmed:
                logger.info("Shell command cancelled by user: %s", str(cmd)[:80])
                return "Operation cancelled by user."

        return None

    # -- argument coercion ---------------------------------------------------

    @staticmethod
    def _coerce_args(func: Callable, args: dict[str, Any]) -> dict[str, Any]:
        """Coerce string arguments to the types declared in the function signature.

        LLMs frequently send numbers as strings (e.g., ``"10"`` instead of ``10``).
        This inspects the function's type hints and converts where safe.
        """
        hints = typing.get_type_hints(func)
        coerced = {}
        for key, value in args.items():
            expected = hints.get(key)
            if expected and isinstance(value, str):
                try:
                    if expected is int:
                        value = int(value)
                    elif expected is float:
                        value = float(value)
                    elif expected is bool:
                        value = value.lower() in ("true", "1", "yes")
                except (ValueError, AttributeError):
                    pass  # leave as-is if conversion fails
            coerced[key] = value
        return coerced

    # -- execution ----------------------------------------------------------

    def has_tool(self, name: str) -> bool:
        """Return True if a tool with *name* is registered."""
        return name in self._tools

    def get_role(self, name: str) -> str:
        """Return the role this tool runs on, per ``tool_routing.yaml``.

        YAML wins. Falls back to the ``@tool(locality=...)`` decorator value
        (translated through the legacy locality->role translation) only
        when the YAML has no rule for *name* — in practice every tool is
        covered by the catch-all ``"*"`` rule, but the fallback keeps the
        registry usable without a YAML present (e.g., in unit tests).
        """
        from .routing import _LEGACY_LOCALITY_MAP, get_role_for_tool

        try:
            return get_role_for_tool(name)
        except Exception:
            # Tool routing layer unavailable (e.g., import-time test) — fall
            # back to the decorator value with legacy translation.
            locality = self._tool_locality.get(name, "local")
            return _LEGACY_LOCALITY_MAP.get(locality, locality)

    def check_routing_drift(self) -> list[tuple[str, str, str]]:
        """Compare every registered tool's decorator value against the YAML.

        Logs a warning per mismatch and returns the drift list. Intended for
        startup observability — not a hard failure.
        """
        from .routing import check_decorator_drift

        return check_decorator_drift(dict(self._tool_locality))

    @staticmethod
    def _missing_required_args(func: Callable, args: dict[str, Any]) -> list[str]:
        """Required parameters of *func* absent from *args*. Lets execute() return
        a clear message instead of letting ``func(**args)`` raise an opaque
        TypeError (the model sometimes omits a required arg — e.g., a large value
        like write_file's ``content`` it couldn't emit, or ssh's ``command``)."""
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return []
        missing = []
        for pname, p in params.items():
            if pname == "self":
                continue
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if p.default is inspect.Parameter.empty and pname not in args:
                missing.append(pname)
        return missing

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        skip_confirm: bool = False,
        skip_events: bool = False,
    ) -> Any:
        """Execute a tool synchronously by name.

        If the underlying function is a coroutine, it is run in a new event loop.

        ``skip_confirm`` / ``skip_events`` let an orchestrator run a tool without
        the generic confirm gate or the tool_call badge — used by the diff-preview
        confirm flow, which runs code_edit's internal propose/apply passes itself
        and drives its own confirmation.
        """
        func = self._tools.get(name)
        if func is None:
            raise KeyError(f"Tool '{name}' not found. Available: {self.list_tools()}")

        args = self._coerce_args(func, arguments or {})

        # The model occasionally omits a required argument (e.g., write_file with no
        # content, or a command-less ssh). Return a corrective message so it
        # retries, instead of raising a TypeError surfaced as an opaque tool error.
        missing = self._missing_required_args(func, args)
        if missing:
            logger.info("Tool '%s' missing required arg(s): %s", name, ", ".join(missing))
            return (
                f"Error: tool '{name}' is missing required argument(s): {', '.join(missing)}. "
                "Provide them and call the tool again."
            )

        arg_pairs = ", ".join(f"'{k}': '{v}'" for k, v in args.items())
        logger.debug("%s(%s)", name, arg_pairs)

        # Run guard BEFORE emitting tool_call so the UI can show the badge
        guard_result = self._classify_guard(func, args)

        if self._on_tool_call and not skip_events:
            try:
                self._on_tool_call(name, args, guard_result)
            except TypeError:
                # Fallback for handlers that don't accept guard_result
                try:
                    self._on_tool_call(name, args)
                except Exception:
                    pass
            except Exception:
                pass  # display errors must never break execution

        # Confirmation check for destructive tools
        if not skip_confirm:
            cancelled = self._check_confirm(func, args, guard_result)
            if cancelled:
                return cancelled

        try:
            if inspect.iscoroutinefunction(func):
                # Async function — run it synchronously
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        result = pool.submit(asyncio.run, func(**args)).result()
                else:
                    result = asyncio.run(func(**args))
            else:
                result = func(**args)

            logger.debug("Tool '%s' returned: %s", name, str(result)[:200])
            return result
        except Exception as exc:
            logger.error("Tool '%s' failed: %s", name, exc)
            raise

    async def aexecute(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Execute a tool asynchronously by name."""
        func = self._tools.get(name)
        if func is None:
            raise KeyError(f"Tool '{name}' not found. Available: {self.list_tools()}")

        args = self._coerce_args(func, arguments or {})

        # The model occasionally omits a required argument (e.g., write_file with no
        # content, or a command-less ssh). Return a corrective message so it
        # retries, instead of raising a TypeError surfaced as an opaque tool error.
        missing = self._missing_required_args(func, args)
        if missing:
            logger.info("Tool '%s' missing required arg(s): %s", name, ", ".join(missing))
            return (
                f"Error: tool '{name}' is missing required argument(s): {', '.join(missing)}. "
                "Provide them and call the tool again."
            )

        arg_pairs = ", ".join(f"'{k}': '{v}'" for k, v in args.items())
        logger.debug("%s(%s)", name, arg_pairs)

        # Run guard BEFORE emitting tool_call so the UI can show the badge
        guard_result = self._classify_guard(func, args)

        if self._on_tool_call:
            try:
                self._on_tool_call(name, args, guard_result)
            except TypeError:
                try:
                    self._on_tool_call(name, args)
                except Exception:
                    pass
            except Exception:
                pass

        # Confirmation check for destructive tools
        cancelled = self._check_confirm(func, args, guard_result)
        if cancelled:
            return cancelled

        try:
            if inspect.iscoroutinefunction(func):
                result = await func(**args)
            else:
                result = func(**args)
            logger.debug("Tool '%s' returned: %s", name, str(result)[:200])
            return result
        except Exception as exc:
            logger.error("Tool '%s' failed: %s", name, exc)
            raise

    # -- Ollama tool specs --------------------------------------------------

    def tool_specs(self, names: list[str] | None = None) -> list[dict]:
        """Generate Ollama tool spec dicts for the given tools (or all).

        These are the dicts you pass to ``AIClient.chat(tools=...)``.
        """
        targets = names or list(self._tools)
        specs = []
        for name in targets:
            func = self._tools.get(name)
            if func:
                specs.append(self._func_to_spec(name, func))
        return specs

    def as_callables(self, names: list[str] | None = None) -> list[Callable]:
        """Return the raw callables (for passing to ``AIClient.chat(tools=...)``)."""
        targets = names or list(self._tools)
        return [self._tools[n] for n in targets if n in self._tools]

    # -- spec generation (mirrors AIClient._func_to_tool_spec) ---------------

    @classmethod
    def _func_to_spec(cls, name: str, func: Callable) -> dict:
        """Convert a function to an Ollama tool specification dict."""
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or ""
        description = doc.split("\n")[0] if doc else name

        properties: dict[str, dict] = {}
        required: list[str] = []

        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            # Skip *args and **kwargs — they aren't real schema parameters
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            # Underscore-prefixed params are internal (e.g., code_edit's
            # _propose / _apply_token used by the diff-preview confirm flow).
            # Hide them from the model — only the orchestrator sets them.
            if pname.startswith("_"):
                continue

            ptype = cls._TYPE_MAP.get(param.annotation, "string")
            prop: dict[str, Any] = {"type": ptype}

            # Pull description from docstring (Google-style: "  arg_name: description")
            for line in doc.split("\n"):
                stripped = line.strip()
                if stripped.startswith(f"{pname}:") or stripped.startswith(f"{pname} :"):
                    prop["description"] = stripped.split(":", 1)[-1].strip()
                    break

            properties[pname] = prop

            if param.default is param.empty:
                required.append(pname)

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }
