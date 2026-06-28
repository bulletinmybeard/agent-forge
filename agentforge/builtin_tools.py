"""Built-in tools — ready to register with a ToolRegistry.

These are plain functions that can be registered individually or all at once
via ``register_builtin_tools(registry)``.
"""

from __future__ import annotations

import ast
import operator
import subprocess
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .tools import tool

if TYPE_CHECKING:
    from .tools import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------


# Cap to stop resource-exhaustion via huge expressions / exponents.
_MAX_EXPR_LEN = 500
_MAX_EXPONENT = 1000

_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval(node: ast.AST, names: dict) -> object:
    """Walk an arithmetic AST, allowing only literals, math operators,
    parentheses and calls to the allowed names. Anything else raises
    ValueError so the caller can surface it as an error string."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, names)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"disallowed constant: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _BINARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"disallowed operator: {type(node.op).__name__}")
        left = _safe_eval(node.left, names)
        right = _safe_eval(node.right, names)
        if isinstance(node.op, ast.Pow):
            # Guard against resource exhaustion (e.g., 9**9**9).
            if isinstance(right, (int, float)) and right > _MAX_EXPONENT:
                raise ValueError(f"exponent too large (max {_MAX_EXPONENT})")
        return op(left, right)  # ty: ignore[no-matching-overload]
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"disallowed operator: {type(node.op).__name__}")
        operand = _safe_eval(node.operand, names)
        if not isinstance(operand, (int, float)):
            raise ValueError(f"disallowed unary operand: {operand!r}")
        return op(operand)
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise ValueError(f"unknown name: {node.id}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in names:
            raise ValueError("disallowed function call")
        if node.keywords:
            raise ValueError("keyword arguments not allowed")
        func = names[node.func.id]
        return func(*(_safe_eval(arg, names) for arg in node.args))
    raise ValueError(f"disallowed expression: {type(node).__name__}")


@tool
def calculator(expression: str) -> str:
    """Evaluate a mathematical expression and return the result."""
    # Restricted evaluator — only allow math operations
    allowed_names = {
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
        "sum": sum,
        "pow": pow,
        "int": int,
        "float": float,
    }
    try:
        if len(expression) > _MAX_EXPR_LEN:
            raise ValueError(f"expression too long (max {_MAX_EXPR_LEN} chars)")
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree, allowed_names)
        return str(result)
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Shell command
# ---------------------------------------------------------------------------


@tool
def shell(command: str) -> str:
    """Execute a shell command and return its output."""
    try:
        result = subprocess.run(
            command,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\nSTDERR: {result.stderr.strip()}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out (120s limit)"
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Read file
# ---------------------------------------------------------------------------


@tool
def read_file(path: str) -> str:
    """Read and return the contents of a text file."""
    try:
        from pathlib import Path

        p = Path(path).expanduser()
        if not p.exists():
            return f"Error: file not found — {path}"
        if not p.is_file():
            return f"Error: not a file — {path}"

        content = p.read_text(encoding="utf-8", errors="replace")
        # Truncate very large files to avoid filling the context window
        if len(content) > 50_000:
            return content[:50_000] + f"\n\n... (truncated, {len(content)} chars total)"
        return content
    except Exception as exc:
        return f"Error reading file: {exc}"


# ---------------------------------------------------------------------------
# Write file
# ---------------------------------------------------------------------------


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file, creating it if it doesn't exist.

    If the file is inside a sub-folder that already contains files from a
    previous run, a new folder with a numeric suffix (folder_1/) is created.
    For single files in well-known directories, the file is suffixed instead.
    """
    try:
        from pathlib import Path

        from agentforge.tools.filesystem import _resolve_parent, _unique_path

        p = Path(path).expanduser().resolve()
        p = _resolve_parent(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p = _unique_path(p)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {p}"
    except Exception as exc:
        return f"Error writing file: {exc}"


# ---------------------------------------------------------------------------
# Web search (delegated to src.tools.web_search for full implementation)
# ---------------------------------------------------------------------------

# NOTE: The full web search implementation lives in src/tools/web_search.py
# (Ollama Cloud API backend).  This builtin version is kept for standalone
# usage of builtin_tools.py without the full tool registry.


@tool
def web_search(query: str) -> str:
    """Search the web and return results."""
    try:
        from agentforge.tools.web_search import web_search as _real_search

        return _real_search(query)
    except ImportError:
        return (
            "Web search not available. Use the full tool registry "
            "(src.tools.web_search) for Ollama Cloud web search support."
        )


# ---------------------------------------------------------------------------
# Convenience: register all built-in tools at once
# ---------------------------------------------------------------------------


def register_builtin_tools(registry: ToolRegistry) -> int:
    """Register all built-in tools with the given registry.

    Returns the number of tools registered.
    """
    builtins = [calculator, shell, read_file, write_file, web_search]
    for func in builtins:
        registry.register(func)
    return len(builtins)
