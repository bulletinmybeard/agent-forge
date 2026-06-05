"""Git tools — clone, inspect, and manage Git repositories.

Wraps the ``git`` CLI to provide common repository operations.
All tools operate on the LOCAL filesystem.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.git_tools import register_git_tools

    registry = ToolRegistry()
    register_git_tools(registry)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 120, cwd: str | None = None) -> str:
    """Run a git command (argv list, no shell) and return stdout (or stderr on failure)."""
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            stderr = result.stderr.strip()
            if output:
                output += f"\nSTDERR: {stderr}"
            else:
                output = f"Error: {stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except FileNotFoundError:
        return "Error: git command not found. Is Git installed?"
    except Exception as exc:
        return f"Error: {exc}"


def _resolve_repo(path: str) -> Path:
    """Expand and resolve a repository path."""
    return Path(path).expanduser().resolve()


def _is_git_repo(path: Path) -> bool:
    """Check if a path is inside a Git repository."""
    return (path / ".git").is_dir()


# ---------------------------------------------------------------------------
# git_clone
# ---------------------------------------------------------------------------


@tool
def git_clone(url: str, destination: str = "") -> str:
    """Clone a Git repository from a URL to the local machine.

    When to use: Download a remote repository to work with it locally.
    When NOT to use: Inspecting an existing local repo (use git_status or git_log),
        updating an already-cloned repo (use shell('git pull', cwd=path)),
        reading files from a remote repo without cloning (use web_fetch on raw URLs).
    Input: url — repository URL (HTTPS or SSH, e.g., https://github.com/user/repo.git).
        destination — local directory to clone into (default: current directory).
    Output: Confirmation with local path and file count, or a clear error message
        for auth failures, missing repos, or existing non-empty directories.
    """
    # Resolve destination
    if destination:
        dest_path = _resolve_repo(destination)
    else:
        dest_path = Path.cwd()

    # Ensure parent directory exists
    dest_path.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone", url, str(dest_path)]

    # If destination already contains the repo name, clone into parent
    # and let git create the directory
    repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git")
    expected_dir = dest_path

    # If the user gave a directory that doesn't end with the repo name,
    # clone into that directory (git will create a subfolder)
    if dest_path.name != repo_name:
        cmd = ["git", "clone", url]
        expected_dir = dest_path / repo_name

    output = _run(cmd, timeout=180, cwd=str(dest_path))

    if output.startswith("Error:"):
        lower = output.lower()
        if "already exists and is not an empty directory" in lower:
            return (
                f"Error: Directory '{expected_dir}' already exists and is not empty. "
                f"Delete it first or choose a different destination."
            )
        if "could not resolve host" in lower:
            return f"Error: Could not resolve host. Check the URL: {url}"
        if "repository not found" in lower or "not found" in lower:
            return f"Error: Repository not found: {url}"
        if "authentication failed" in lower or "permission denied" in lower:
            return (
                f"Error: Authentication failed for '{url}'. Check the URL is correct and the repository is accessible."
            )
        return output

    # Verify clone succeeded
    if expected_dir.is_dir() and _is_git_repo(expected_dir):
        # Count files for a nice summary
        file_count = sum(1 for _ in expected_dir.rglob("*") if _.is_file())
        return f"Cloned '{url}' to {expected_dir} ({file_count} files)"

    return f"Clone output:\n{output}"


# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------


@tool
def git_status(path: str) -> str:
    """Show the working tree status of a local Git repository.

    When to use: Check which files are modified, untracked, staged, or deleted,
        and whether the branch is ahead/behind its remote.
    When NOT to use: Full diff of changes (use git_diff), commit history
        (use git_log), remote repo status (use ssh + git status remotely).
    Input: path — path to the Git repository root.
    Output: Current branch, ahead/behind counts, and a short-format list of
        changed/untracked/deleted files.
    """
    repo = _resolve_repo(path)

    if not repo.is_dir():
        return f"Error: Directory does not exist: {repo}"
    if not _is_git_repo(repo):
        return f"Error: Not a Git repository: {repo}"

    # Branch info
    branch = _run(["git", "branch", "--show-current"], cwd=str(repo))
    status = _run(["git", "status", "--short"], cwd=str(repo))

    # Ahead/behind (fails with an Error string when no upstream; guarded below)
    upstream = _run(
        ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
        cwd=str(repo),
    )

    lines = [f"Repository: {repo}", f"Branch: {branch}"]

    if upstream and not upstream.startswith("Error:"):
        parts = upstream.split()
        if len(parts) == 2:
            ahead, behind = parts
            if ahead != "0":
                lines.append(f"Ahead of remote: {ahead} commit(s)")
            if behind != "0":
                lines.append(f"Behind remote: {behind} commit(s)")

    if status and status != "(no output)":
        # Count changes by type
        modified = status.count("\n M ") + status.count("\nM ") + (1 if status.startswith((" M", "M ")) else 0)
        added = status.count("??")
        deleted = status.count(" D ") + status.count("D ")

        lines.append("\nChanges:")
        if modified:
            lines.append(f"  Modified: {modified}")
        if added:
            lines.append(f"  Untracked: {added}")
        if deleted:
            lines.append(f"  Deleted: {deleted}")
        lines.append(f"\n{status}")
    else:
        lines.append("\nWorking tree clean")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# git_log
# ---------------------------------------------------------------------------


@tool
def git_log(path: str, count: int = 10, include_files: bool = False) -> str:
    """Show recent commit history of a local Git repository.

    When to use: Review recent commits, understand what changed recently,
        or find a commit hash for reference.
        Set include_files=True to include the list of files changed per commit
        — essential for hotspot analysis (e.g., which files change most often).
    When NOT to use: Viewing actual file diffs (use git_diff or git_show), checking
        working tree status (use git_status).
    Input: path — path to the Git repository root.
        count — number of recent commits to show (default: 10, capped at 200).
        include_files — if True, list all files changed in each commit
            (uses --name-only; ideal for frequency/hotspot analysis).
    Output: One line per commit: short hash, author, relative time, and subject.
        If include_files=True, each commit is followed by its changed file paths.
        Total commit count shown in the header.
    """
    repo = _resolve_repo(path)

    if not repo.is_dir():
        return f"Error: Directory does not exist: {repo}"
    if not _is_git_repo(repo):
        return f"Error: Not a Git repository: {repo}"

    count = int(count)
    count = min(count, 200)  # higher cap for file-include / hotspot analysis

    if include_files:
        # --name-only appends the list of changed files after each commit header
        output = _run(
            ["git", "log", f"-{count}", "--format=--- %h  %an  %ar  %s", "--name-only"],
            cwd=str(repo),
        )
    else:
        output = _run(
            ["git", "log", f"-{count}", "--format=%h  %an  %ar  %s"],
            cwd=str(repo),
        )

    if output.startswith("Error:"):
        return output

    # Get total commit count
    total = _run(["git", "rev-list", "--count", "HEAD"], cwd=str(repo))
    total_str = f" (of {total} total)" if total and not total.startswith("Error:") else ""

    files_note = " (with changed files)" if include_files else ""
    return f"Last {count} commits{total_str}{files_note} in {repo}:\n\n{output}"


# ---------------------------------------------------------------------------
# git_show
# ---------------------------------------------------------------------------


@tool
def git_show(path: str, commit: str = "HEAD", name_only: bool = False) -> str:
    """Show the details of a specific commit — metadata, changed files, and diff.

    When to use: Inspect what a specific commit changed. Pass commit=<hash>
        to examine any historical commit. Use name_only=True to get just the
        list of changed files without the diff content (faster, less output).
        This is the correct tool for per-commit file inspection — do NOT use
        git_diff for this (git_diff only shows working-tree changes).
    When NOT to use: Viewing commit history (use git_log), checking working-tree
        changes (use git_diff), getting file lists for many commits at once
        (use git_log with include_files=True — much more efficient).
    Input: path — path to the Git repository root.
        commit — commit hash or ref to inspect (default: HEAD).
        name_only — if True, show only the changed file names, not the diff.
    Output: Commit metadata (hash, author, date, message) plus changed files.
        If name_only=False, also includes the unified diff (truncated at 6000 chars).
    """
    repo = _resolve_repo(path)

    if not repo.is_dir():
        return f"Error: Directory does not exist: {repo}"
    if not _is_git_repo(repo):
        return f"Error: Not a Git repository: {repo}"

    stat_format = "--format=commit %H%nAuthor: %an <%ae>%nDate: %ad%n%n    %s%n"
    if name_only:
        output = _run(
            ["git", "show", "--stat", stat_format, commit],
            cwd=str(repo),
        )
    else:
        # Header + stat summary
        header = _run(
            ["git", "show", "--stat", stat_format, commit],
            cwd=str(repo),
        )
        # Full diff (truncated)
        diff = _run(["git", "show", "--no-stat", commit], cwd=str(repo))
        max_chars = 6000
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n\n... (diff truncated, {len(diff)} chars total)"
        output = f"{header}\n\n{diff}"

    if output.startswith("Error:"):
        return output

    return output


# ---------------------------------------------------------------------------
# git_diff
# ---------------------------------------------------------------------------


@tool
def git_diff(path: str, staged: bool = False) -> str:
    """Show uncommitted changes (diff) in a local Git repository.

    When to use: See exactly what lines changed in modified files before
        committing, or review what is staged.
    When NOT to use: Checking which files changed without seeing the diff
        (use git_status), viewing commit history (use git_log).
    Input: path — path to the Git repository root.
        staged — set true to diff staged (index vs HEAD) changes;
        false (default) shows unstaged (working tree vs index) changes.
    Output: Diff stat summary plus unified diff. Truncated at 8000 chars
        with a note if the diff is large.
    """
    repo = _resolve_repo(path)

    if not repo.is_dir():
        return f"Error: Directory does not exist: {repo}"
    if not _is_git_repo(repo):
        return f"Error: Not a Git repository: {repo}"

    flags = ["--cached"] if staged else []
    output = _run(["git", "diff", *flags, "--stat"], cwd=str(repo))

    if output.startswith("Error:"):
        return output

    if not output or output == "(no output)":
        label = "staged" if staged else "unstaged"
        return f"No {label} changes in {repo}"

    # Also get the actual diff (limited to avoid flooding)
    full_diff = _run(["git", "diff", *flags], cwd=str(repo))

    # Truncate if too large
    max_chars = 8000
    if len(full_diff) > max_chars:
        full_diff = full_diff[:max_chars] + f"\n\n... (truncated, {len(full_diff)} chars total)"

    label = "Staged" if staged else "Unstaged"
    return f"{label} changes in {repo}:\n\n{output}\n\n{full_diff}"


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_git_tools(registry: ToolRegistry) -> int:
    """Register all Git tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "Git",
        "Git tools operate on the LOCAL filesystem. Use ssh for Git operations on remote hosts.",
    )

    tools = [
        git_clone,
        git_status,
        git_log,
        git_show,
        git_diff,
    ]
    for func in tools:
        registry.register(func, category="Git")
    return len(tools)
