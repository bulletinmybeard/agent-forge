"""Hybrid code context enrichment (Option C).

At query time, enriches code-type search results with:
1. Source code snippets — reads N lines around the target line number
2. Usage sites — greps for symbol imports/calls across the project

The enriched data is attached to result payloads as ``_source_snippet``
and ``_usage_sites`` fields, which the response refiner then includes
in the context block sent to the LLM.

Controlled by the ``code_context`` section in config.yaml.  When
``enabled: false`` (default), this module is a no-op.
"""

import logging
import subprocess
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# Chunk types that carry code metadata worth enriching.
_CODE_CHUNK_TYPES = {"code_class", "code_function", "code_module"}


# ── Snippet reader ──────────────────────────────────────────────────────────


def read_snippet(
    source_root: str,
    file_path: str,
    line_number: int,
    context_lines: int | None = None,
) -> str | None:
    """Read a snippet of source code around a target line."""
    if context_lines is None:
        context_lines = settings.code_context.snippet_lines

    full_path = Path(source_root) / file_path
    if not full_path.is_file():
        logger.debug("Snippet: file not found — %s", full_path)
        return None

    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.warning("Snippet: cannot read %s — %s", full_path, exc)
        return None

    if not lines:
        return None

    # Clamp to file bounds (line_number is 1-based).
    start = max(0, line_number - 1 - context_lines)
    end = min(len(lines), line_number - 1 + context_lines + 1)

    numbered = []
    for i in range(start, end):
        marker = "→" if i == line_number - 1 else " "
        numbered.append(f"{marker} {i + 1:4d} │ {lines[i]}")

    return "\n".join(numbered)


def grep_usages(
    symbol_name: str,
    source_root: str,
    file_extensions: list[str] | None = None,
    max_results: int | None = None,
) -> str | None:
    """Find where a symbol (class/function) is used across the project.

    Uses ``grep -rnw`` to search for the symbol name as a whole word in files matching the configured extensions.
    Filters out the definition line itself.
    """
    if file_extensions is None:
        file_extensions = settings.code_context.file_extensions
    if max_results is None:
        max_results = settings.code_context.max_grep_results

    root = Path(source_root)
    if not root.is_dir():
        logger.debug("Grep: source root not found — %s", root)
        return None

    # Build grep --include flags for each extension.
    include_flags: list[str] = []
    for ext in file_extensions:
        include_flags.extend(["--include", f"*{ext}"])

    cmd = [
        "grep",
        "-rnw",
        *include_flags,
        "--exclude-dir=.git",
        "--exclude-dir=__pycache__",
        "--exclude-dir=node_modules",
        "--exclude-dir=.venv",
        "--exclude-dir=migrations",
        symbol_name,
        str(root),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Grep failed for '%s': %s", symbol_name, exc)
        return None

    if result.returncode not in (0, 1):  # 1 = no matches
        logger.debug("Grep returned %d for '%s'", result.returncode, symbol_name)
        return None

    raw_lines = result.stdout.strip().splitlines()
    if not raw_lines:
        return None

    # Strip the source_root prefix for readability, filter noise.
    root_str = str(root)
    cleaned: list[str] = []
    for line in raw_lines:
        # Make paths relative.
        if line.startswith(root_str):
            line = line[len(root_str) :].lstrip("/")

        # Skip lines that are just the class/function definition itself
        # (e.g., "class Foo:" or "def foo(").  We want *usages*, not defs.
        stripped = line.split(":", 2)[-1].strip() if ":" in line else line
        if stripped.startswith(f"class {symbol_name}") or stripped.startswith(f"def {symbol_name}"):
            continue

        cleaned.append(line)
        if len(cleaned) >= max_results:
            break

    if not cleaned:
        return None

    return "\n".join(cleaned)


async def enrich_results(results: list[dict]) -> list[dict]:
    """Enrich code-type search results with source snippets and usages.

    Modifies results in-place by adding ``_source_snippet`` and ``_usage_sites`` keys
    to code chunk payloads. Non-code results are left untouched.

    This is a no-op when ``settings.code_context.enabled`` is ``False`` or when ``source_roots`` is empty.
    """
    if not settings.code_context.enabled:
        return results

    source_roots = settings.code_context.source_roots
    if not source_roots:
        logger.debug("Code context enabled but no source_roots configured")
        return results

    enriched_count = 0
    code_count = 0

    for result in results:
        payload = result.get("payload", {})
        chunk_type = payload.get("chunk_type", "")

        if chunk_type not in _CODE_CHUNK_TYPES:
            continue

        code_count += 1
        source_name = payload.get("source_name", "")
        source_root = source_roots.get(source_name)
        if not source_root:
            continue

        file_path = payload.get("file_path", "")
        line_number = payload.get("line_number", 0)

        # 1. Read source snippet around the definition.
        if file_path and line_number > 0:
            snippet = read_snippet(source_root, file_path, line_number)
            if snippet:
                payload["_source_snippet"] = snippet
                enriched_count += 1

        # 2. Grep for usages of the symbol.
        symbol = payload.get("class_name") or payload.get("function_name") or ""
        if symbol:
            usages = grep_usages(symbol, source_root)
            if usages:
                payload["_usage_sites"] = usages

    if code_count:
        logger.info("Code context: enriched %d/%d code results (of %d total)", enriched_count, code_count, len(results))

    return results
