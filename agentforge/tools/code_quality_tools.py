"""Code quality tools — lint, format, and type-check runner.

Provides the ``linter_run`` tool that detects the project language, selects
the appropriate tool, and runs it.  Supports Python (ruff, black, isort,
mypy, flake8, bandit) and Node (eslint, prettier) with auto-detection of
package managers (poetry/pip for Python, npm/yarn for Node).

Configuration lives in ``config.yaml → linter``.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path

from .registry import tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading (from config.yaml → linter section)
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "languages": ["python", "node"],
    "use_npx": True,
    "python": {
        "lint": ["ruff check"],
        "format": ["ruff format --check"],
        "type": ["mypy"],
        "security": ["bandit -r -ll"],
    },
    "node": {
        "lint": ["eslint"],
        "format": ["prettier --check"],
    },
}


def _load_linter_config() -> dict:
    """Load linter config from config.yaml, falling back to defaults."""
    try:
        import yaml

        config_path = Path(__file__).resolve().parents[2] / "config.yaml"
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("linter", _DEFAULT_CONFIG)
    except Exception:
        pass
    return _DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Language and package manager detection
# ---------------------------------------------------------------------------


def _detect_language(path: str) -> str | None:
    """Detect the project language from marker files."""
    p = Path(path).resolve()
    search_dir = p if p.is_dir() else p.parent

    # Walk up to find project root markers
    for d in [search_dir] + list(search_dir.parents)[:5]:
        if (d / "pyproject.toml").exists() or (d / "requirements.txt").exists() or (d / "setup.py").exists():
            return "python"
        if (d / "package.json").exists():
            return "node"

    # Fall back to file extension
    if p.is_file():
        ext = p.suffix.lower()
        if ext in (".py", ".pyi"):
            return "python"
        if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            return "node"

    return None


def _detect_python_runner(path: str) -> str:
    """Detect the Python package manager runner prefix."""
    p = Path(path).resolve()
    search_dir = p if p.is_dir() else p.parent

    for d in [search_dir] + list(search_dir.parents)[:5]:
        if (d / "pyproject.toml").exists():
            # Check if it's a Poetry project
            try:
                content = (d / "pyproject.toml").read_text()
                if "[tool.poetry]" in content:
                    return "poetry run "
            except Exception:
                pass
        if (d / "requirements.txt").exists() and not (d / "pyproject.toml").exists():
            return ""  # plain pip — run directly

    return ""  # default: run directly


def _detect_node_runner(path: str, use_npx: bool = True) -> str:
    """Detect the Node package manager runner prefix."""
    p = Path(path).resolve()
    search_dir = p if p.is_dir() else p.parent

    for d in [search_dir] + list(search_dir.parents)[:5]:
        if (d / "yarn.lock").exists():
            return "yarn "
        if (d / "package-lock.json").exists() or (d / "package.json").exists():
            return "npx " if use_npx else "npm exec -- "

    return "npx " if use_npx else ""


def _find_project_root(path: str) -> Path:
    """Find the project root directory (for cwd)."""
    p = Path(path).resolve()
    search_dir = p if p.is_dir() else p.parent

    markers = {"pyproject.toml", "package.json", "requirements.txt", "setup.py", ".git"}
    for d in [search_dir] + list(search_dir.parents)[:5]:
        if any((d / m).exists() for m in markers):
            return d

    return search_dir


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use linter_run to check code quality. Supports Python (ruff, mypy, bandit) "
        "and Node (eslint, prettier). Auto-detects language and package manager. "
        "Groups: lint, format, type, security. Or specify a specific tool name. "
        "Set fix=true to auto-fix issues where supported."
    ),
)
def linter_run(path: str, group: str = "lint", tool_name: str = "", fix: bool = False) -> str:
    """Run a linter, formatter, or type checker on a file or directory.

    Auto-detects the language (Python or Node) and package manager
    (poetry/pip for Python, npm/yarn for Node).

    path: File or directory to check
    group: Tool group to run — "lint", "format", "type", "security", or "all" (default: "lint")
    tool_name: Specific tool to run (e.g., "ruff", "eslint") — overrides group
    fix: If true, run in auto-fix mode where supported (e.g., ruff check --fix)
    """
    target = Path(path).resolve()
    if not target.exists():
        return f"ERROR: Path does not exist: {path}"

    # Detect language
    lang = _detect_language(path)
    if not lang:
        return (
            f"ERROR: Could not detect language for {path}.\n"
            "Supported: Python (pyproject.toml / requirements.txt) and "
            "Node (package.json). Ensure the file is inside a project."
        )

    config = _load_linter_config()
    project_root = _find_project_root(path)

    # Build the runner prefix
    if lang == "python":
        runner = _detect_python_runner(path)
    else:
        use_npx = config.get("use_npx", True)
        runner = _detect_node_runner(path, use_npx=use_npx)

    # Resolve which commands to run
    lang_config = config.get(lang, {})
    commands: list[str] = []

    if tool_name:
        # Specific tool requested — find it in any group
        for grp_cmds in lang_config.values():
            for cmd in grp_cmds if isinstance(grp_cmds, list) else [grp_cmds]:
                if tool_name.lower() in cmd.lower().split()[0]:
                    commands.append(cmd)
                    break
            if commands:
                break
        if not commands:
            available = []
            for grp, cmds in lang_config.items():
                for c in cmds if isinstance(cmds, list) else [cmds]:
                    available.append(f"  {grp}: {c}")
            return f"ERROR: Tool '{tool_name}' not found for {lang}.\nAvailable tools:\n" + "\n".join(available)
    elif group == "all":
        for grp_cmds in lang_config.values():
            commands.extend(grp_cmds if isinstance(grp_cmds, list) else [grp_cmds])
    else:
        cmds = lang_config.get(group, [])
        commands = cmds if isinstance(cmds, list) else [cmds]
        if not commands:
            available = ", ".join(lang_config.keys())
            return f"ERROR: Group '{group}' not configured for {lang}. Available: {available}"

    # Determine the target path relative to project root
    if target.is_file():
        rel_target = str(target.relative_to(project_root)) if target.is_relative_to(project_root) else str(target)
    else:
        rel_target = str(target.relative_to(project_root)) if target.is_relative_to(project_root) else str(target)

    # Execute each command
    results: list[str] = []
    results.append(f"Language: {lang} | Runner: {runner.strip() or 'direct'} | Root: {project_root}")
    results.append(f"Target: {rel_target}")
    results.append("=" * 60)

    for cmd_template in commands:
        # Apply fix flag
        full_cmd = cmd_template
        if fix:
            if "ruff check" in full_cmd:
                full_cmd = full_cmd.replace("ruff check", "ruff check --fix")
            elif "ruff format --check" in full_cmd:
                full_cmd = full_cmd.replace("ruff format --check", "ruff format")
            elif "eslint" in full_cmd:
                full_cmd += " --fix"
            elif "prettier --check" in full_cmd:
                full_cmd = full_cmd.replace("prettier --check", "prettier --write")
            elif "isort" in full_cmd and "--check" in full_cmd:
                full_cmd = full_cmd.replace("--check", "")
            elif "black" in full_cmd and "--check" in full_cmd:
                full_cmd = full_cmd.replace("--check", "")

        # Build argv: runner tokens + command tokens + target (no shell)
        argv = shlex.split(runner) + shlex.split(full_cmd) + [rel_target]
        results.append(f"\n--- {full_cmd.split()[0]} ---")
        results.append(f"$ {shlex.join(argv)}")

        try:
            proc = subprocess.run(
                argv,
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1"},
            )
            output = (proc.stdout + proc.stderr).strip()
            if proc.returncode == 0:
                results.append(output if output else "(no issues found)")
            else:
                results.append(output if output else f"(exit code {proc.returncode})")
        except subprocess.TimeoutExpired:
            results.append("ERROR: Command timed out after 120 seconds")
        except Exception as e:
            results.append(f"ERROR: {e}")

    return "\n".join(results)


def register_code_quality_tools(registry) -> int:
    """Register code quality tools with the given registry."""
    registry.register_category_hint(
        "Code Quality",
        "Run linters, formatters, type checkers, and security scanners. "
        "Supports Python (ruff, mypy, bandit) and Node (eslint, prettier). "
        "Auto-detects language and package manager.",
    )
    tools = [linter_run]
    for func in tools:
        registry.register(func, category="Code Quality")
    return len(tools)
