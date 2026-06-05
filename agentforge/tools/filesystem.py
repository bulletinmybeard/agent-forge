"""Filesystem I/O tools — read, write, copy, move, delete, find, and inspect.

These tools give agents the ability to interact with the local filesystem.
Each function is decorated with ``@tool`` for auto-registration and can also
be registered explicitly via :func:`register_filesystem_tools`.

Performance-critical operations use native CLI commands for speed:
``fd`` (find), ``tree`` (dir listing), ``cat`` (read), ``grep`` (search),
``du`` (size), ``cp``, ``mv``, ``rm``, ``mkdir``.
Falls back gracefully when a tool is not installed.

Directories listed in ``config.yaml → tools.ignored_dirs`` (e.g., .venv,
node_modules, __pycache__) are automatically excluded from search operations.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.filesystem import register_filesystem_tools

    registry = ToolRegistry()
    register_filesystem_tools(registry)
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Platform helpers & shell runner
# ---------------------------------------------------------------------------


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _run(cmd: list[str], timeout: int = 30) -> str:
    """Run a command (argv list, no shell) and return stdout (or stderr on failure).

    Runs with ``shell=False`` so arguments are passed verbatim — no shell
    interpretation, no injection. Callers that need a pipeline must use
    :func:`_run_shell` instead.
    """
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\nSTDERR: {result.stderr.strip()}"
        return output or "(no output)"
    except FileNotFoundError as exc:
        # Tool not installed — mirror the old "command not found" behavior so
        # fd/find/tree fallbacks still trigger on "Error" in the output.
        return f"Error: {exc}"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except Exception as exc:
        return f"Error: {exc}"


def _run_shell(cmd: str, timeout: int = 30) -> str:
    """Run a shell command string and return stdout (or stderr on failure).

    Only for invocations that genuinely need shell features (pipelines,
    redirects). Every interpolated user value MUST be passed through
    :func:`shlex.quote` before reaching this function.
    """
    try:
        result = subprocess.run(
            cmd,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\nSTDERR: {result.stderr.strip()}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Config-driven ignored directories
# ---------------------------------------------------------------------------

_DEFAULT_IGNORED_DIRS: list[str] = [
    ".venv",
    ".venv_dir",
    ".env",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".eggs",
    "*.egg-info",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".cache",
    ".turbo",
    ".parcel-cache",
    "bower_components",
    "vendor",
    ".terraform",
    ".DS_Store",
]


def _get_ignored_dirs() -> list[str]:
    """Return the ignored_dirs list from config, falling back to defaults."""
    try:
        from ..config import get_config

        cfg = get_config()
        dirs = cfg.get("tools.ignored_dirs")
        if isinstance(dirs, list) and dirs:
            return dirs
    except Exception:
        pass
    return _DEFAULT_IGNORED_DIRS


def _fd_exclude_flags() -> list[str]:
    """Build fd --exclude flags (argv) for all ignored directories."""
    dirs = _get_ignored_dirs()
    flags: list[str] = []
    for d in dirs:
        flags += ["--exclude", d]
    return flags


def _find_prune_clause() -> list[str]:
    """Build a find -prune clause (argv) to skip ignored directories."""
    dirs = _get_ignored_dirs()
    if not dirs:
        return []
    clause: list[str] = ["("]
    for i, d in enumerate(dirs):
        if i:
            clause.append("-o")
        clause += ["-name", d]
    clause += [")", "-prune", "-o"]
    return clause


def _find_prune_clause_shell() -> str:
    """Build a find -prune clause as a shell string for pipeline callers.

    Dir names come from static config, not from tool arguments; still quoted
    via shlex for safety.
    """
    parts = _find_prune_clause()
    if not parts:
        return ""
    # Translate argv tokens to a shell-safe string: ( and ) must be escaped.
    out: list[str] = []
    for tok in parts:
        if tok == "(":
            out.append("\\(")
        elif tok == ")":
            out.append("\\)")
        elif tok in ("-o", "-prune", "-name"):
            out.append(tok)
        else:
            out.append(shlex.quote(tok))
    return " ".join(out)


def _grep_exclude_flags() -> list[str]:
    """Build grep --exclude-dir flags (argv) for all ignored directories."""
    dirs = _get_ignored_dirs()
    return [f"--exclude-dir={d}" for d in dirs]


def _get_default_search_depth() -> int:
    """Return the default search depth from config (0 = unlimited)."""
    try:
        from ..config import get_config

        cfg = get_config()
        return int(cfg.get("tools.default_search_depth", 0))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


@tool
def read_file(
    path: str,
    max_chars: int = 50_000,
    offset: int = 0,
    limit: int = 0,
) -> str:
    """Read and return the contents of a local file.

    When to use: Read source code, config files, log files, text files,
        or PDFs that already exist on the local filesystem.
    When NOT to use: Directory listing (use read_dir), searching for text
        across files (use grep_text), searching by filename (use find_files),
        remote files (use ssh to read them), web pages (use web_fetch).
    Input: path — absolute or relative path to the file.
        max_chars — character limit (default 50000; 0 = no limit).
            Ignored when offset/limit are used.
        offset — first line to read, 1-indexed (default 0 = start of file).
        limit — number of lines to read from offset (default 0 = all lines).
    Output: File contents as a string, with a truncation note if cut short.
        PDF files are converted to text with page markers automatically.
    Hint: Use offset + limit to page through large files, e.g.,
        read_file(path, offset=100, limit=100) reads lines 100–199.
    """
    # Coerce — models sometimes pass floats like 1.0 instead of 1
    offset = int(float(offset)) if offset else 0
    limit = int(float(limit)) if limit else 0
    max_chars = int(float(max_chars)) if max_chars else 50_000

    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: file not found — {path}"
        if not p.is_file():
            return f"Error: not a file — {path}"

        # iCloud Drive: trigger download if file is a cloud-only stub
        _ensure_icloud_downloaded(p)

        # PDF extraction — use pdfplumber for structured text + tables
        if p.suffix.lower() == ".pdf":
            return _read_pdf(p, max_chars)

        # Line-range mode: use sed for efficiency
        if offset or limit:
            start = max(1, offset)
            if limit:
                end = start + limit - 1
                output = _run(["sed", "-n", f"{start},{end}p", str(p)], timeout=10)
            else:
                output = _run(["tail", "-n", f"+{start}", str(p)], timeout=10)
            if output.startswith("Error:"):
                return output
            total_lines = _run(["wc", "-l", str(p)], timeout=5).split()[0]
            return output + f"\n\n[Lines {start}–{start + output.count(chr(10))} of {total_lines} total]"

        # Use cat for the common case (fast, handles large files well).
        # Fall back to Python only when truncation is needed.
        if not max_chars:
            output = _run(["cat", str(p)], timeout=10)
            if output.startswith("Error:"):
                return output
            return output

        # Check file size first — if under limit, cat it directly
        try:
            file_size = p.stat().st_size
        except OSError:
            file_size = 0

        if file_size <= max_chars:
            output = _run(["cat", str(p)], timeout=10)
            if output.startswith("Error:"):
                return output
            return output

        # File is large — use head -c for truncation + report total size
        output = _run(["head", "-c", str(max_chars), str(p)], timeout=10)
        if output.startswith("Error:"):
            return output
        return output + f"\n\n... (truncated, {file_size:,} chars total)"
    except Exception as exc:
        return f"Error reading file: {exc}"


def _ensure_icloud_downloaded(path: Path) -> None:
    """Trigger iCloud Drive download if the file is a cloud-only stub.

    Uses ``brctl download`` to materialize the file, then polls until
    the content is available (up to 15 seconds). No-op for non-iCloud paths.
    """
    if "Mobile Documents/com~apple~CloudDocs" not in str(path):
        return
    try:
        import subprocess as _sp
        import time as _time

        _sp.run(["brctl", "download", str(path)], timeout=30, capture_output=True)
        for _ in range(15):
            try:
                with open(path, "rb") as f:
                    if f.read(1):
                        return
            except OSError:
                pass  # sync lock, keep waiting
            _time.sleep(1)
    except Exception:
        pass  # best-effort


def _read_pdf(path: Path, max_chars: int = 50_000) -> str:
    """Extract text from a PDF file using pdfplumber.

    Returns page-separated text with table detection.
    Falls back to a CLI ``pdftotext`` attempt if pdfplumber is not installed.
    """
    work_path = path

    # Try pdfplumber first (best quality, table support)
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None

    if pdfplumber:
        try:
            pages: list[str] = []
            with pdfplumber.open(work_path) as pdf:
                for i, page in enumerate(pdf.pages, 1):
                    # Extract tables as structured text
                    tables = page.extract_tables()
                    table_text = ""
                    if tables:
                        for table in tables:
                            rows = []
                            for row in table:
                                cells = [str(c).strip() if c else "" for c in row]
                                rows.append(" | ".join(cells))
                            table_text += "\n".join(rows) + "\n"

                    # Extract regular text
                    text = page.extract_text() or ""

                    # Combine: prefer table extraction for pages with tables
                    content = table_text.strip() if table_text.strip() else text.strip()
                    if content:
                        pages.append(f"--- Page {i} ---\n{content}")

            if not pages:
                return f"PDF has {len(pdf.pages)} page(s) but no extractable text (may be scanned/image-only)."

            result = f"[PDF: {path.name}, {len(pages)} page(s)]\n\n" + "\n\n".join(pages)

            if max_chars and len(result) > max_chars:
                return result[:max_chars] + f"\n\n... (truncated, {len(result):,} chars total)"
            return result
        except Exception as exc:
            return f"Error extracting PDF with pdfplumber: {exc}"

    # Fallback: try CLI pdftotext (poppler-utils)
    output = _run(["pdftotext", str(work_path), "-"], timeout=15)

    if output.startswith("Error:") or not output.strip():
        return (
            f"Cannot read PDF — install pdfplumber (`pip install pdfplumber`) "
            f"or poppler-utils (`brew install poppler` / `apt install poppler-utils`). "
            f"Raw error: {output}"
        )

    result = f"[PDF: {path.name}]\n\n{output.strip()}"
    if max_chars and len(result) > max_chars:
        return result[:max_chars] + f"\n\n... (truncated, {len(result):,} chars total)"
    return result


@tool
def read_dir(path: str, recursive: bool = False, pattern: str = "") -> str:
    """List files and directories at a given local path.

    When to use: Browse the contents of a directory to understand structure
        or find files when you already know roughly where to look.
    When NOT to use: Deep recursive exploration with filtering (use find_files),
        searching inside file contents (use grep_text), reading file contents
        (use read_file), rich visual tree (use tree_view from cli_tools).
    Input: path — directory path to list.
        recursive — set true for a recursive listing (uses tree or fd).
        pattern — optional glob to filter results (e.g., '*.py', '*.log').
    Output: List of filenames with sizes, or a tree structure if recursive.
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: directory not found — {path}"
        if not p.is_dir():
            return f"Error: not a directory — {path}"

        if recursive:
            # tree gives a beautiful recursive view with sizes
            excludes = _get_ignored_dirs()
            tree_cmd = ["tree", "-ahF", "--du", "--dirsfirst"]
            for d in excludes:
                tree_cmd += ["-I", d]
            if pattern:
                tree_cmd += ["-P", pattern]
            tree_cmd.append(str(p))
            output = _run(tree_cmd, timeout=30)
            if "Error" not in output and output != "(no output)":
                return f"Directory: {p}\n\n{output}"
            # tree not available — fall back to fd
            pat = pattern or "*"
            fd_cmd = ["fd", "--no-ignore", "--hidden", *_fd_exclude_flags(), pat, str(p)]
            output = _run(fd_cmd, timeout=30)
            if "Error" not in output and output != "(no output)":
                return f"Directory: {p}\n\n{output}"

        # Shallow listing: fd --max-depth 1
        if pattern:
            cmd = ["fd", "--no-ignore", "--hidden", "--max-depth", "1", "--glob", pattern, str(p)]
        else:
            cmd = ["fd", "--no-ignore", "--hidden", "--max-depth", "1", "", str(p)]
        output = _run(cmd, timeout=15)

        if "Error" not in output and output != "(no output)":
            count = len(output.splitlines())
            return f"Directory: {p}\nEntries: {count}\n\n{output}"

        # fd not available — fall back to ls
        output = _run(["ls", "-lah", str(p)], timeout=15)
        return f"Directory: {p}\n\n{output}"
    except Exception as exc:
        return f"Error listing directory: {exc}"


@tool
def file_info(path: str) -> str:
    """Get detailed metadata for a local file or directory.

    When to use: Check size, permissions, modification time, owner, line count,
        or whether a path exists before reading or writing it.
    When NOT to use: Reading file contents (use read_file), listing a directory
        (use read_dir), disk space across a full tree (use dir_size).
    Input: path — absolute or relative path to any file or directory.
    Output: Type, size (human-readable and bytes), permissions (octal), owner UID,
        timestamps (modified/created/accessed), extension, and line count for text files.
    """
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"Error: not found — {path}"

        stat = p.stat()
        kind = "directory" if p.is_dir() else "file"
        info = [
            f"Path: {p}",
            f"Type: {kind}",
            f"Size: {_human_size(stat.st_size)} ({stat.st_size:,} bytes)",
            f"Permissions: {oct(stat.st_mode)[-3:]}",
            f"Owner UID: {stat.st_uid}",
            f"Modified: {_format_timestamp(stat.st_mtime)}",
            f"Created: {_format_timestamp(stat.st_ctime)}",
            f"Accessed: {_format_timestamp(stat.st_atime)}",
        ]

        if p.is_file():
            info.append(f"Extension: {p.suffix or '(none)'}")
            # Count lines for text files
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").count("\n")
                info.append(f"Lines: {lines:,}")
            except Exception:
                pass

        if p.is_symlink():
            info.append(f"Symlink target: {p.readlink()}")

        return "\n".join(info)
    except Exception as exc:
        return f"Error getting file info: {exc}"


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


def _unique_path(p: Path) -> Path:
    """Return *p* if it doesn't exist, otherwise append _1, _2, … before the extension."""
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    parent = p.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _unique_dir(d: Path) -> Path:
    """Return *d* if it doesn't exist or is empty, otherwise append _1, _2, …"""
    if not d.exists():
        return d
    # Empty or only contains hidden files (like .placeholder) → reuse
    if not any(f for f in d.iterdir() if not f.name.startswith(".")):
        return d
    stem = d.name
    parent = d.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}"
        if not candidate.exists():
            return candidate
        # Exists but empty → reuse
        if not any(f for f in candidate.iterdir() if not f.name.startswith(".")):
            return candidate
        n += 1


# Cache: maps original resolved parent dir → actual (possibly suffixed) dir.
# Cleared between agent runs via clear_dir_remap().
_dir_remap: dict[Path, Path] = {}


def clear_dir_remap() -> None:
    """Clear the directory remap cache.  Call between agent runs."""
    _dir_remap.clear()


def _resolve_parent(p: Path) -> Path:
    """If *p*'s parent directory already has content, remap it to a unique
    suffixed directory.  All files targeting the same original parent within
    the same agent run will land in the same remapped directory.

    Directories that are "well-known" (home, /tmp, Downloads, Desktop, etc.)
    are never themselves suffixed — only their sub-folders are.
    """

    parent = p.parent
    home = Path(os.path.expanduser("~")).resolve()

    # Well-known top-level directories that should never be suffixed
    well_known = {
        home,
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
        Path("/tmp").resolve(),
        Path("/var/tmp").resolve(),
    }
    if parent in well_known:
        # File sits directly in a well-known dir → only dedup the file itself
        return p

    # Check the cache first — all files in the same batch reuse the mapping
    if parent in _dir_remap:
        return _dir_remap[parent] / p.name

    # Parent exists and has real (non-hidden) content → suffix it
    actual_parent = _unique_dir(parent)
    _dir_remap[parent] = actual_parent
    return actual_parent / p.name


@tool
def write_file(path: str, content: str) -> str:
    """Write text content to a local file, creating parent directories as needed.

    When to use: Create a new file or completely replace the contents of an
        existing file — scripts, configs, reports, generated code, etc.
    When NOT to use: Appending to an existing file (use append_file), making
        structural edits to an existing file (use code_edit), remote file writes
        (use ssh + write via shell or scp).
    Input: path — target file path. content — full text content to write.
    Output: Confirmation with the actual path written and character count.
        If the requested path already exists, the file is saved with a numeric
        suffix instead (e.g., report_1.md, report_2.md). The suffixed siblings
        are intentional versioned outputs — do NOT delete them after writing.
    """
    try:
        p = Path(path).expanduser().resolve()
        p = _resolve_parent(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Final file-level dedup (for files in well-known dirs)
        p = _unique_path(p)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content):,} chars to {p}"
    except Exception as exc:
        return f"Error writing file: {exc}"


@tool
def append_file(path: str, content: str) -> str:
    """Append text content to a local file, creating it if it does not exist.

    When to use: Add lines to an existing log, notes file, or config without
        replacing what is already there.
    When NOT to use: Overwriting a file (use write_file), making targeted edits
        inside a file (use code_edit).
    Input: path — target file path. content — text to append.
    Output: Confirmation with the actual path and appended character count.
    """
    try:
        p = Path(path).expanduser().resolve()
        _run(["mkdir", "-p", str(p.parent)])

        # Use Python for content writing — shell escaping multi-line content
        # is error-prone and content may contain special characters.
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content):,} chars to {p}"
    except Exception as exc:
        return f"Error appending to file: {exc}"


@tool
def create_directory(path: str) -> str:
    """Create a local directory and any missing parent directories.

    When to use: Set up a destination folder before writing multiple files into it.
    When NOT to use: write_file already creates missing parents automatically,
        so you only need this when you want to ensure the directory exists first.
    Input: path — directory path to create (nested paths are created in one call).
    Output: Path of the directory that was created (may have a numeric suffix
        if the original path already contains files from a previous run).
    """
    try:
        p = Path(path).expanduser().resolve()
        p = _unique_dir(p)
        # Cache the mapping so subsequent write_file calls use the same dir
        original = Path(path).expanduser().resolve()
        _dir_remap[original] = p
        p.mkdir(parents=True, exist_ok=True)
        return f"Directory created: {p}"
    except Exception as exc:
        return f"Error creating directory: {exc}"


# ---------------------------------------------------------------------------
# Copy / Move / Delete
# ---------------------------------------------------------------------------


@tool
def copy_file(source: str, destination: str) -> str:
    """Copy a local file or directory to a new location.

    When to use: Duplicate a file or directory on the local machine (backup,
        template copy, staging copy before edit, etc.).
    When NOT to use: Moving/renaming (use move_file), copying between local
        and remote (use scp or rsync), large directory syncs (use rsync).
    Input: source — path to file or directory to copy.
        destination — target path (file or directory).
    Output: Confirmation showing source → destination.
    """
    try:
        src = Path(source).expanduser().resolve()
        dst = Path(destination).expanduser().resolve()

        if not src.exists():
            return f"Error: source not found — {source}"

        # Ensure destination parent exists
        _run(["mkdir", "-p", str(dst.parent)])

        # cp -pR preserves metadata and handles both files and directories
        output = _run(["cp", "-pR", str(src), str(dst)])
        if "Error" in output:
            return f"Error copying: {output}"

        kind = "directory" if src.is_dir() else "file"
        return f"Copied {kind} {src} → {dst}"
    except Exception as exc:
        return f"Error copying: {exc}"


@tool
def move_file(source: str, destination: str) -> str:
    """Move or rename a local file or directory.

    When to use: Rename a file, move it to a different directory, or both
        at once — all on the local machine.
    When NOT to use: Copying while keeping the original (use copy_file),
        moving between local and remote (use scp or rsync).
    Input: source — current file or directory path.
        destination — new path (rename if same directory, move if different).
    Output: Confirmation showing source → destination.
    """
    try:
        src = Path(source).expanduser().resolve()
        dst = Path(destination).expanduser().resolve()

        if not src.exists():
            return f"Error: source not found — {source}"

        _run(["mkdir", "-p", str(dst.parent)])
        output = _run(["mv", str(src), str(dst)])
        if "Error" in output:
            return f"Error moving: {output}"
        return f"Moved {src} → {dst}"
    except Exception as exc:
        return f"Error moving: {exc}"


@tool(confirm="Delete '{path}'? This cannot be undone.")
def delete_file(path: str, recursive: bool = False) -> str:
    """Delete a local file or directory (requires confirmation).

    When to use: Remove a file or directory tree that is no longer needed.
        Always requires user confirmation before executing.
    When NOT to use: Moving files out of the way (use move_file), remote
        file deletion (use ssh + shell to remove remotely).
    Input: path — file or directory path to delete.
        recursive — must be true to delete a non-empty directory and all its contents.
    Output: Confirmation of what was deleted.
    Hint: This is irreversible. The confirmation prompt protects against
        accidental deletion — never bypass it.
    """
    try:
        p = Path(path).expanduser().resolve()

        if not p.exists():
            return f"Error: not found — {path}"

        if p.is_dir():
            if not recursive:
                return f"Error: {path} is a directory. Set recursive=true to delete it and all its contents."
            output = _run(["rm", "-rf", str(p)])
            if "Error" in output:
                return f"Error deleting directory: {output}"
            return f"Deleted directory and all contents: {p}"
        else:
            output = _run(["rm", "-f", str(p)])
            if "Error" in output:
                return f"Error deleting file: {output}"
            return f"Deleted file: {p}"
    except Exception as exc:
        return f"Error deleting: {exc}"


# ---------------------------------------------------------------------------
# Search / Discovery
# ---------------------------------------------------------------------------


@tool
def find_files(path: str, pattern: str, max_depth: int = 0) -> str:
    """Find local files matching a glob pattern under a directory.

    When to use: Locate files by name pattern — e.g., find all Python files,
        all config files, or any file matching a naming convention.
    When NOT to use: Searching file contents for text (use grep_text),
        listing a single directory (use read_dir), finding the largest files
        (use find_large_files).
    Input: path — root directory to search from.
        pattern — glob pattern such as '*.py', '*.log', 'test_*.py', 'Dockerfile'.
        max_depth — depth limit (0 = unlimited).
    Output: Matched file paths with sizes, relative to the search root.
        Automatically excludes .venv, .venv_dir, node_modules, .git, __pycache__, etc.
    Hint: Uses fd when available (fast); falls back to find. Ignored dirs come
        from config.yaml tools.ignored_dirs. Always prefer this over raw find/shell
        when filtering by filename — it respects the full exclusion list including
        custom dirs like .venv_dir.
    """
    try:
        # Type coercion — models often pass numbers as strings
        max_depth = int(max_depth)

        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            return f"Error: not a directory — {path}"

        # Apply config default depth when caller doesn't specify
        if max_depth <= 0:
            max_depth = _get_default_search_depth()

        excludes = _fd_exclude_flags()
        depth_flag = ["--max-depth", str(max_depth)] if max_depth > 0 else []

        # --- fd: fast, cross-platform, sane defaults ----------------------
        cmd = ["fd", "--no-ignore", "--hidden", "--glob", pattern, *depth_flag, *excludes, str(root)]
        output = _run(cmd, timeout=30)

        # If fd isn't available, fall back to find
        if "Error" in output or output == "(no output)":
            find_depth = ["-maxdepth", str(max_depth)] if max_depth > 0 else []
            prune = _find_prune_clause()
            cmd = ["find", str(root), *find_depth, *prune, "-name", pattern, "-print"]
            output = _run(cmd, timeout=30)

        if "Error" in output and "timed out" in output:
            return "Error: search timed out (30s limit) — try a narrower path"

        # Parse output lines into results
        matches = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line == "(no output)":
                continue
            try:
                fp = Path(line)
                if not fp.exists():
                    continue
                size = _human_size(fp.stat().st_size) if fp.is_file() else "DIR"
                rel = fp.relative_to(root) if fp.is_relative_to(root) else fp
                matches.append(f"  {size:>10}  {rel}")
            except (OSError, ValueError):
                matches.append(f"  {'?':>10}  {line}")

        header = f"Search: {pattern} in {root}\nMatches: {len(matches)}\n"
        return header + "\n".join(matches) if matches else header + "(no matches)"
    except Exception as exc:
        return f"Error searching: {exc}"


@tool
def find_large_files(path: str, min_size_mb: int = 100, top_n: int = 20) -> str:
    """Find the largest files under a local directory, sorted by size.

    When to use: Diagnose disk usage or identify bulky assets when a directory
        is unexpectedly large.
    When NOT to use: Finding files by name pattern (use find_files), measuring
        total directory size (use dir_size), general file search (use find_files).
    Input: path — root directory to search.
        min_size_mb — minimum file size in MB to include (default 100).
        top_n — how many results to return (default 20).
    Output: Sorted list of matching files with human-readable sizes.
        Excludes common build/cache directories automatically.
    """
    try:
        # Type coercion — models often pass numbers as strings
        min_size_mb = int(min_size_mb)
        top_n = int(top_n)

        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            return f"Error: not a directory — {path}"

        min_bytes = min_size_mb * 1024 * 1024
        # fd exclude flags as a shell-quoted string for the pipeline below
        excludes = " ".join(shlex.quote(tok) for tok in _fd_exclude_flags())
        q_root = shlex.quote(str(root))

        # fd --size for size filtering + --exec to get exact byte count
        if _is_macos():
            stat_fmt = "stat -f '%z %N'"
        else:
            stat_fmt = "stat --format='%s %n'"

        # Try fd first (pipeline: needs shell for sort/head)
        cmd = (
            f"fd --no-ignore --hidden --type f --size +{min_size_mb}m {excludes} '' {q_root} "
            f"--exec {stat_fmt} 2>/dev/null "
            f"| sort -rn | head -{top_n}"
        )
        output = _run_shell(cmd, timeout=60)

        # Fall back to find if fd not available
        if "Error" in output or output == "(no output)":
            prune = _find_prune_clause_shell()
            cmd = (
                f"find {q_root} {prune} -type f -size +{min_bytes}c "
                f"-exec {stat_fmt} {{}} + 2>/dev/null "
                f"| sort -rn | head -{top_n}"
            )
            output = _run_shell(cmd, timeout=60)

        if "Error" in output and "timed out" in output:
            return "Error: search timed out — try a smaller directory"

        # Parse "size path" lines
        results = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line == "(no output)":
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                try:
                    size = int(parts[0])
                    fpath = Path(parts[1])
                    rel = fpath.relative_to(root) if fpath.is_relative_to(root) else fpath
                    results.append(f"  {_human_size(size):>10}  {rel}")
                except (ValueError, TypeError):
                    results.append(f"  {'?':>10}  {line}")

        if not results:
            return f"No files >= {min_size_mb}MB found under {root}"

        header = f"Largest files under {root} (>= {min_size_mb}MB):\n"
        return header + "\n".join(results)
    except Exception as exc:
        return f"Error finding large files: {exc}"


@tool
def dir_size(path: str) -> str:
    """Calculate the total size of a local directory with a subdirectory breakdown.

    When to use: Understand how much disk space a directory and its children
        consume, and identify which subdirectories are largest.
    When NOT to use: Finding large individual files (use find_large_files),
        system-wide disk usage across mount points (use disk_usage from system tools),
        directory contents listing (use read_dir).
    Input: path — directory path to measure.
    Output: Total size, file and subdirectory counts, and a breakdown of the
        largest immediate subdirectories.
    """
    try:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            return f"Error: not a directory — {path}"

        q_root = shlex.quote(str(root))

        # Total size via du -sh
        total_output = _run(["du", "-sh", str(root)])
        total_str = total_output.split()[0] if total_output and "Error" not in total_output else "?"

        # File and directory counts (excluding ignored dirs) — pipeline via shell
        prune = _find_prune_clause_shell()
        file_count = _run_shell(f"find {q_root} {prune} -type f -print 2>/dev/null | wc -l").strip()
        dir_count = _run_shell(f"find {q_root} {prune} -type d -print 2>/dev/null | wc -l").strip()

        lines = [
            f"Directory: {root}",
            f"Total size: {total_str}",
            f"Files: {file_count}",
            f"Subdirectories: {dir_count}",
        ]

        # Subdirectory breakdown via du -sh */ (depth 1) — glob + pipeline via shell
        sub_output = _run_shell(f"du -sh {q_root}/*/ 2>/dev/null | sort -rh | head -20")
        if sub_output and "Error" not in sub_output and sub_output != "(no output)":
            lines.append("\nSubdirectory breakdown:")
            for line in sub_output.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    size_str = parts[0]
                    sub_path = Path(parts[1].rstrip("/"))
                    name = sub_path.name
                    lines.append(f"  {size_str:>10}  {name}/")

        return "\n".join(lines)
    except Exception as exc:
        return f"Error calculating directory size: {exc}"


@tool
def grep_text(path: str, pattern: str, recursive: bool = True, max_results: int = 50) -> str:
    """Search for a text string inside local files (case-insensitive).

    When to use: Find where a specific string, function name, variable, or
        keyword appears across source files or a single file.
    When NOT to use: Finding files by filename pattern (use find_files),
        reading a whole file (use read_file), searching inside archives or
        remote hosts (use ssh + grep for remote).
    Input: path — file or directory to search.
        pattern — plain text string to search for (not a regex).
        recursive — search recursively into subdirectories (default true).
        max_results — cap on matching lines returned (default 50).
    Output: Matching lines with file names and line numbers, relative to the
        search root. Excludes .git, node_modules, __pycache__, etc.
    Hint: Search is case-insensitive. For regex patterns use shell('grep -rE …').
    """
    try:
        # Type coercion
        max_results = int(max_results)

        target = Path(path).expanduser().resolve()
        if not target.exists():
            return f"Error: not found — {path}"

        # Native grep: -i case-insensitive, -n line numbers, -F fixed string
        flags = "-inF"
        if recursive and target.is_dir():
            flags += "r"

        # Exclude configured directories (e.g., __pycache__, .git, node_modules)
        exclude_flags = " ".join(shlex.quote(f) for f in _grep_exclude_flags())

        # Pipeline (grep | head): needs shell — quote every interpolated value
        cmd = (
            f"grep {flags} {exclude_flags} -m {max_results} "
            f"{shlex.quote(pattern)} {shlex.quote(str(target))} 2>/dev/null | head -{max_results}"
        )
        output = _run_shell(cmd, timeout=30)

        if "Error" in output and "timed out" in output:
            return "Error: search timed out (30s limit) — try a narrower path"

        # Parse grep output into formatted results
        results = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line == "(no output)":
                continue
            # Make paths relative to target for readability
            if target.is_dir():
                line = line.replace(str(target) + "/", "", 1)
            results.append(f"  {line[:250]}")

        header = f"Search for '{pattern}' in {target}\nMatches: {len(results)}"
        if len(results) >= max_results:
            header += f" (limited to {max_results})"
        header += "\n"
        return header + "\n".join(results) if results else header + "(no matches)"
    except Exception as exc:
        return f"Error searching: {exc}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _human_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} B"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} PB"


def _format_timestamp(ts: float) -> str:
    """Format a Unix timestamp to a readable string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_filesystem_tools(registry: ToolRegistry) -> int:
    """Register all filesystem tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "Filesystem",
        "Filesystem tools operate on the LOCAL machine only. For remote file operations, use ssh.",
    )

    tools = [
        read_file,
        read_dir,
        file_info,
        write_file,
        append_file,
        create_directory,
        copy_file,
        move_file,
        delete_file,
        find_files,
        find_large_files,
        dir_size,
        grep_text,
    ]
    for func in tools:
        registry.register(func, category="Filesystem")
    return len(tools)
