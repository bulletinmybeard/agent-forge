"""CLI power tools — wrappers around useful command-line programs.

Each tool delegates to a well-known CLI binary (``jq``, ``yq``, ``tree``,
``gh``, ``ncdu``, ``yt-dlp``) and returns its output.  If the binary is not
installed the tool returns a helpful error with an install hint.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.cli_tools import register_cli_tools

    registry = ToolRegistry()
    register_cli_tools(registry)
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .filesystem import _get_ignored_dirs
from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_result(result: subprocess.CompletedProcess[str]) -> str:
    """Shape a completed process into the stdout/stderr text the tools expect."""
    output = result.stdout.strip()
    if result.returncode != 0 and result.stderr:
        err = result.stderr.strip()
        if output:
            output += f"\nSTDERR: {err}"
        else:
            output = f"Error: {err}"
    return output or "(no output)"


def _run_argv(
    argv: list[str], timeout: int = 60, cwd: str | Path | None = None, env: dict | None = None
) -> str:
    """Run a command as an argv list (shell=False) and return its output."""
    try:
        result = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
        )
        return _format_result(result)
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except Exception as exc:
        return f"Error: {exc}"


def _run_stdin(argv: list[str], stdin_data: str, timeout: int = 60) -> str:
    """Run an argv command (shell=False) feeding *stdin_data* on stdin."""
    try:
        result = subprocess.run(
            argv,
            shell=False,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return _format_result(result)
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except Exception as exc:
        return f"Error: {exc}"


def _run(cmd: str, timeout: int = 60) -> str:
    """Run a shell command and return its stdout (or stderr on failure).

    Only for genuine shell pipelines. Every interpolated value MUST be passed
    through ``shlex.quote()`` by the caller — never interpolate untrusted input
    raw into *cmd*.
    """
    try:
        result = subprocess.run(
            cmd,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return _format_result(result)
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except Exception as exc:
        return f"Error: {exc}"


def _require(binary: str) -> str | None:
    """Return ``None`` if *binary* is on PATH, or an error message if not."""
    if shutil.which(binary):
        return None
    install_hints = {
        "jq": "brew install jq",
        "yq": "brew install yq",
        "tree": "brew install tree",
        "gh": "brew install gh",
        "ncdu": "brew install ncdu",
        "yt-dlp": "brew install yt-dlp",
        "fd": "brew install fd",
    }
    hint = install_hints.get(binary, f"Install {binary} and make sure it's on your PATH")
    return f"Error: '{binary}' is not installed. Install with: {hint}"


# ---------------------------------------------------------------------------
# jq — JSON processor
# ---------------------------------------------------------------------------


@tool
def jq_query(input_path: str, query: str = ".") -> str:
    """Query or transform a JSON file using a jq filter expression.

    When to use: Extract a field, filter an array, or reformat a local JSON file
        without loading the entire file into context.
    When NOT to use: Querying JSON text in memory (use jq_transform),
        YAML/TOML files (use yq_query), plain text search (use grep_text).
    Input: input_path — path to the JSON file.
        query — jq filter expression (default '.' = pretty-print the whole file).
    Output: jq filter result as text.

    Examples:
      jq_query('data.json', '.users[] | .name')
      jq_query('config.json', '.database.host')
      jq_query('results.json', '[.items[] | select(.score > 80)]')
    """
    err = _require("jq")
    if err:
        return err

    p = Path(input_path).expanduser().resolve()
    if not p.exists():
        return f"Error: file not found — {input_path}"

    return _run_argv(["jq", query, str(p)], timeout=30)


@tool
def jq_transform(json_text: str, query: str = ".") -> str:
    """Apply a jq filter to a JSON string that is already in memory.

    When to use: Transform or extract fields from a JSON string you already have
        (e.g., API response, shell command output) without writing it to a file first.
    When NOT to use: JSON stored in a file (use jq_query), YAML content (use yq_query).
    Input: json_text — raw JSON string.
        query — jq filter expression.
    Output: jq filter result as text.

    Examples:
      jq_transform('{"a":1,"b":2}', '.a')
      jq_transform('[1,2,3,4,5]', 'map(. * 2)')
    """
    err = _require("jq")
    if err:
        return err

    # Feed JSON through stdin instead of building a shell pipeline
    return _run_stdin(["jq", query], json_text, timeout=15)


# ---------------------------------------------------------------------------
# yq — YAML/TOML/XML processor (like jq but for YAML)
# ---------------------------------------------------------------------------


@tool
def yq_query(input_path: str, query: str = ".") -> str:
    """Query or transform a YAML, TOML, or XML file using a yq filter expression.

    When to use: Extract values from config files (docker-compose.yml,
        values.yaml, Helm charts, pyproject.toml) without reading the full file.
    When NOT to use: JSON files (use jq_query), converting between formats
        (use yq_convert), plain text search (use grep_text).
    Input: input_path — path to the YAML/TOML/XML file.
        query — yq filter expression (default '.' = pretty-print).
    Output: Filter result as text.

    Examples:
      yq_query('config.yaml', '.database.host')
      yq_query('docker-compose.yml', '.services | keys')
      yq_query('values.yaml', '.replicas')
    """
    err = _require("yq")
    if err:
        return err

    p = Path(input_path).expanduser().resolve()
    if not p.exists():
        return f"Error: file not found — {input_path}"

    return _run_argv(["yq", query, str(p)], timeout=30)


@tool
def yq_convert(input_path: str, output_format: str = "json") -> str:
    """Convert a local config file between YAML, JSON, TOML, and XML formats.

    When to use: Convert a config file from one structured format to another —
        e.g., YAML to JSON for use with a tool that requires JSON.
    When NOT to use: Querying values without format conversion (use yq_query),
        JSON-only processing (use jq_query).
    Input: input_path — path to the source file.
        output_format — target format: 'json', 'yaml', 'toml', or 'xml'.
    Output: Converted file content as a string.
    """
    err = _require("yq")
    if err:
        return err

    p = Path(input_path).expanduser().resolve()
    if not p.exists():
        return f"Error: file not found — {input_path}"

    fmt_flags = {
        "json": "-o=json",
        "yaml": "-o=yaml",
        "toml": "-o=toml",
        "xml": "-o=xml",
    }
    flag = fmt_flags.get(output_format.lower())
    if not flag:
        return f"Error: unsupported format '{output_format}'. Use: json, yaml, toml, xml"

    return _run_argv(["yq", flag, ".", str(p)], timeout=30)


# ---------------------------------------------------------------------------
# tree — directory visualization
# ---------------------------------------------------------------------------


@tool
def tree_view(
    path: str,
    max_depth: int = 3,
    recursive: bool = False,
    dirs_only: bool = False,
    pattern: str = "",
    show_size: bool = True,
) -> str:
    """Display a visual tree of a local directory structure.

    When to use: Get a readable hierarchical overview of a project or directory
        to understand its layout, especially when onboarding to a new codebase.
    When NOT to use: Flat directory listing (use read_dir), searching for specific
        files (use find_files), disk size analysis (use ncdu_report or dir_size).
    Input: path — root directory to display.
        max_depth — levels to show (default 3; ignored when recursive=true).
        recursive — show all levels with no depth limit.
        dirs_only — show only directories, not individual files.
        pattern — glob to filter (e.g., '*.py').
        show_size — include file and directory sizes.
    Output: ASCII tree with optional sizes. Excludes .venv, node_modules,
        .git, __pycache__, etc. automatically.
    """
    err = _require("tree")
    if err:
        return err

    # Type coercion — models often pass numbers/bools as strings
    max_depth = int(max_depth)
    recursive = str(recursive).lower() in ("true", "1", "yes")
    if isinstance(dirs_only, str):
        dirs_only = dirs_only.lower() in ("true", "1", "yes")
    if isinstance(show_size, str):
        show_size = show_size.lower() in ("true", "1", "yes")

    if recursive:
        max_depth = 99

    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return f"Error: not a directory — {path}"

    # Ignored directories from config — each as a distinct -I argv pair
    ignored = _get_ignored_dirs()

    argv = ["tree", "-L", str(max_depth), "--dirsfirst"]
    for d in ignored:
        argv += ["-I", d]
    if dirs_only:
        argv.append("-d")
    if show_size:
        argv.append("-sh")
    if pattern:
        argv += ["-P", pattern]
    argv.append("-F")  # append / to dirs, * to executables, etc.
    argv.append(str(p))

    return _run_argv(argv, timeout=30)


# ---------------------------------------------------------------------------
# gh — GitHub CLI
# ---------------------------------------------------------------------------

_gh_override = threading.local()


def set_gh_token_override(token: str, host: str = "", read_write: bool = True) -> None:
    """Inject a per-connection GitHub token for ``gh_command`` calls on this thread."""
    _gh_override.token = token
    _gh_override.host = host
    _gh_override.read_write = read_write


def clear_gh_token_override() -> None:
    _gh_override.token = None
    _gh_override.host = None
    _gh_override.read_write = None


_GH_READ_OK: dict[str, set[str] | None] = {
    "pr": {"list", "view", "diff", "checks", "status"},
    "issue": {"list", "view", "status"},
    "repo": {"list", "view", "clone"},
    "release": {"list", "view"},
    "run": {"list", "view"},
    "workflow": {"list", "view"},
    "label": {"list"},
    "gist": {"list", "view"},
    "auth": {"status"},
    "search": None,  # whole subcommand is read-only
    "browse": None,
    "status": None,
}


def _gh_readonly_guard(gh_args: list[str]) -> str | None:
    """Return an error if ``gh_args`` is not a read-only-safe gh invocation, else None."""
    if not gh_args:
        return None
    top = gh_args[0]
    if top == "api":
        for i, arg in enumerate(gh_args):
            method = None
            if arg in ("-X", "--method") and i + 1 < len(gh_args):
                method = gh_args[i + 1]
            elif arg.startswith("--method="):
                method = arg.split("=", 1)[1]
            elif arg.startswith("-X") and len(arg) > 2:
                method = arg[2:]
            if method and method.upper() != "GET":
                return "Error: read-only GitHub connection — only GET 'gh api' calls are allowed."
        return None
    if top not in _GH_READ_OK:
        return f"Error: read-only GitHub connection — '{top}' commands are not permitted."
    allowed = _GH_READ_OK[top]
    if allowed is None:
        return None
    sub = gh_args[1] if len(gh_args) > 1 else ""
    if sub not in allowed:
        return f"Error: read-only GitHub connection — '{top} {sub}' is not a permitted read command."
    return None


@tool
def gh_command(args: str, cwd: str = "") -> str:
    """Run a GitHub CLI (gh) command.

    When to use: Interact with GitHub — search or list PRs/issues across repos,
        view PR details, list repos, releases, or make API calls.
    When NOT to use: Git operations on the local repo (use git_status, git_log,
        git_diff), downloading files (use download_file), web searches (use web_search).
    Input: args — the gh subcommand and arguments (without the 'gh' prefix).
        cwd — optional local git repo directory (only needed for repo-scoped
              commands when you don't use --repo).
    Output: Raw gh command output (text or JSON).

    CROSS-REPO SEARCH — use 'gh search' for queries spanning multiple repos.
    Never needs --repo or cwd. Use @me to mean the authenticated user:
      gh_command('search prs --author @me --state open --sort updated --limit 10 --json number,title,repository,url,createdAt')
      gh_command('search prs --author @me --state merged --sort updated --limit 5 --json number,title,repository,url')
      gh_command('search issues --author @me --state open --sort updated --limit 10 --json number,title,repository,url')
    NOTE: 'gh search prs' --json fields are: number, title, state, author,
      createdAt, updatedAt, url, repository (object with name+nameWithOwner),
      labels, isDraft, comments, reviewDecision

    SINGLE-REPO COMMANDS — 'gh pr list', 'gh issue list', 'gh release list'
    ALWAYS need a repo. Use --repo OWNER/REPO (no cwd needed):
      gh_command('pr list --repo owner/repo --state open --json number,title,author,createdAt,url')
      gh_command('pr view 42 --repo owner/repo --json number,title,additions,deletions,changedFiles,reviews,comments,url')
      gh_command('pr diff 42 --repo owner/repo')
      gh_command('issue list --repo owner/repo --state open --label bug --json number,title,url')
      gh_command('release list --repo owner/repo')
    NOTE: 'gh pr list/view' --json fields include: number, title, state,
      author, createdAt, updatedAt, url, headRefName, baseRefName,
      additions, deletions, changedFiles, reviewDecision, reviews,
      comments, labels, milestone, body, isDraft
    NOTE: 'repository' and 'nameWithOwner' are NOT valid --json fields
      for 'gh pr list/view' — use 'gh search prs' if you need repo names.

    REPO LISTING — no repo needed:
      gh_command('repo list --limit 20 --json name,visibility,url,description')
      gh_command('repo list --source --json name,url')
    """
    err = _require("gh")
    if err:
        return err

    # Split the gh args safely instead of interpolating into a shell string
    try:
        gh_args = shlex.split(args)
    except ValueError as exc:
        return f"Error: could not parse gh arguments — {exc}"

    # Connector override: a GitHub connection injects its PAT via GH_TOKEN.
    run_env: dict | None = None
    override_token = getattr(_gh_override, "token", None)
    if override_token:
        if not getattr(_gh_override, "read_write", True):
            guard = _gh_readonly_guard(gh_args)
            if guard:
                return guard
        run_env = {**os.environ, "GH_TOKEN": override_token}
        gh_host = getattr(_gh_override, "host", "") or ""
        if gh_host:
            run_env["GH_HOST"] = gh_host

    # If cwd is provided, run gh inside it so repo-scoped commands work
    if cwd:
        resolved = Path(cwd).expanduser().resolve()
        if not resolved.is_dir():
            return f"Error: directory not found — {cwd}"
        result = _run_argv(["gh", *gh_args], timeout=60, cwd=resolved, env=run_env)
    else:
        result = _run_argv(["gh", *gh_args], timeout=60, env=run_env)

    # Intercept common failure modes and redirect to the correct approach.
    # Only apply repo-context error to commands that actually need a repo
    # (pr, issue, release) — not repo/search/api which work without one.
    _repo_scoped_cmd = any(args.startswith(prefix) for prefix in ("pr ", "issue ", "release "))
    if "not a git repository" in result and _repo_scoped_cmd:
        return (
            "Error: 'gh pr/issue/release' commands require a repo — either pass "
            "--repo OWNER/REPO or cwd= pointing to a local checkout.\n\n"
            "For cross-repo queries (e.g., 'most recent PR across my projects'), "
            "use 'gh search' instead — it works without any repo context:\n"
            "  gh_command('search prs --author @me --state open --sort updated "
            "--limit 10 --json number,title,url,createdAt,repository')\n"
            "  gh_command('search prs --author @me --merged --sort updated "
            "--limit 10 --json number,title,url,closedAt,repository')\n"
            "  gh_command('search issues --author @me --state open --sort updated "
            "--limit 10 --json number,title,url,createdAt,repository')\n"
            "Note: for 'gh search', 'repository' IS a valid --json field and "
            "returns {name, nameWithOwner}. Use --merged (not --state merged) "
            "for merged PRs."
        )

    if "Unknown JSON field" in result and "pr " in args and "search" not in args:
        # Extract which field caused the problem
        bad_field = ""
        if '"' in result:
            try:
                bad_field = result.split('"')[1]
            except IndexError:
                pass
        return (
            f"Error: '{bad_field}' is not a valid --json field for 'gh pr list/view'.\n\n"
            "Valid --json fields for 'gh pr list/view':\n"
            "  number, title, state, author, createdAt, updatedAt, url,\n"
            "  headRefName, baseRefName, additions, deletions, changedFiles,\n"
            "  files, reviewDecision, reviews, comments, labels, milestone, body, isDraft\n"
            "  (files returns array of {path, additions, deletions, changeType})\n\n"
            "Note: 'repository' and 'nameWithOwner' are NOT valid for 'gh pr list/view'.\n"
            "To get the repo name alongside PRs, use 'gh search prs' instead:\n"
            "  gh_command('search prs --author @me --state open --sort updated "
            "--limit 10 --json number,title,url,createdAt,repository')"
        )

    return result


# ---------------------------------------------------------------------------
# ncdu — disk usage analyzer
# ---------------------------------------------------------------------------


@tool
def ncdu_report(path: str, top_n: int = 30, dirs_only: bool = False) -> str:
    """Analyze disk usage of a local directory and list the largest items.

    When to use: Identify which files or subdirectories are consuming the most
        space under a given path. Faster and more accurate than manual du pipelines.
    When NOT to use: System-wide filesystem usage (use disk_usage),
        finding files by name pattern (use find_files),
        quick total size of a directory (use dir_size).
    Input: path — root directory to analyze.
        top_n — number of largest items to return (default 30).
        dirs_only — set true to show only subdirectory totals, not individual files.
    Output: Sorted list of items with sizes. Uses ncdu when available; falls back to du.
    """
    # Type coercion
    top_n = int(top_n)
    if isinstance(dirs_only, str):
        dirs_only = dirs_only.lower() in ("true", "1", "yes")

    # When dirs_only is requested, du -sh gives the cleanest output
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return f"Error: not a directory — {path}"

    # top_n is coerced to int above; quote all path values for the shell pipelines
    q_p = shlex.quote(str(p))

    if dirs_only:
        # Glob (/*/) must stay outside the quotes; pipeline needs a real shell
        output = _run(
            f"du -sh {q_p}/*/ 2>/dev/null | sort -rh | head -{top_n}",
            timeout=60,
        )
        if output and "Error" not in output and output != "(no output)":
            return f"Subdirectory sizes for {p} (top {top_n}):\n\n{output}"
        return f"No subdirectories found under {p}"

    err = _require("ncdu")
    if err:
        # Fall back to du immediately if ncdu isn't installed
        return _run(
            f"du -ah {q_p} 2>/dev/null | sort -rh | head -{top_n}",
            timeout=60,
        )

    # ncdu can export JSON which we parse with jq for a nice summary.
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name

    # Export scan to JSON (no pipeline — run as argv)
    output = _run_argv(["ncdu", "-o", tmp_path, str(p)], timeout=120)

    if "Error" in output:
        # ncdu export failed — fall back to du
        Path(tmp_path).unlink(missing_ok=True)
        return _run(
            f"du -ah {q_p} 2>/dev/null | sort -rh | head -{top_n}",
            timeout=60,
        )

    # If jq is available, parse the ncdu export for a nice summary
    if shutil.which("jq"):
        q_tmp = shlex.quote(tmp_path)
        summary = _run(
            f"jq -r '.. | objects | select(.asize) | "
            f'"\\(.asize)\\t\\(.name)"\' {q_tmp} 2>/dev/null '
            f"| sort -rn | head -{top_n}",
            timeout=30,
        )
        Path(tmp_path).unlink(missing_ok=True)
        if summary and "Error" not in summary:
            return f"Disk usage for {p} (top {top_n}):\n\n{summary}"

    # Fallback: just use du
    Path(tmp_path).unlink(missing_ok=True)
    return _run(
        f"du -ah {q_p} 2>/dev/null | sort -rh | head -{top_n}",
        timeout=60,
    )


# ---------------------------------------------------------------------------
# yt-dlp — video/audio downloader
# ---------------------------------------------------------------------------


@tool
def ytdlp_info(url: str) -> str:
    """Fetch metadata about a video or audio URL without downloading it.

    When to use: Inspect a YouTube, TikTok, Vimeo, or other video URL to get
        the title, duration, uploader, and available formats before deciding
        whether to download.
    When NOT to use: Actually downloading the video (use ytdlp_download),
        listing format options interactively (use ytdlp_list_formats),
        fetching a web page (use web_fetch).
    Input: url — the full video/audio URL.
    Output: Title, uploader, duration, upload date, view count, and description
        excerpt. Supports 1000+ sites via yt-dlp.
    """
    err = _require("yt-dlp")
    if err:
        return err

    # --dump-json gives all metadata as JSON; --no-download ensures nothing is saved
    # stderr is folded into the result so we can report errors like geo-blocks or 404s
    output = _run_argv(
        ["yt-dlp", "--dump-json", "--no-download", url],
        timeout=30,
    )
    if output.startswith("Error:") or output == "(no output)" or "ERROR:" in output:
        return f"Error: could not fetch metadata for {url}.\n{output}"

    # If jq is available, extract key fields for a clean summary
    if shutil.which("jq"):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            tmp.write(output)
            tmp_path = tmp.name
        jq_filter = (
            '"Title: " + .title + "\\n'
            'Uploader: " + (.uploader // "unknown") + "\\n'
            'Duration: " + ((.duration // 0) | tostring) + "s\\n'
            'Upload date: " + (.upload_date // "unknown") + "\\n'
            'View count: " + ((.view_count // 0) | tostring) + "\\n'
            'Description: " + ((.description // "") | .[:500])'
        )
        summary = _run_argv(
            ["jq", "-r", jq_filter, tmp_path],
            timeout=10,
        )
        Path(tmp_path).unlink(missing_ok=True)
        if summary and "Error" not in summary:
            return summary

    # Return raw (truncated) JSON if jq not available
    return output[:3000]


@tool
def ytdlp_download(url: str, output_dir: str = ".", audio_only: bool = False) -> str:
    """Download a video or audio file from a URL to the local machine.

    When to use: Save a YouTube, TikTok, Vimeo, or other video/audio to disk.
    When NOT to use: Just inspecting metadata (use ytdlp_info),
        checking available quality options (use ytdlp_list_formats),
        downloading a non-media file (use download_file).
    Input: url — the full video/audio URL.
        output_dir — local directory to save the file (default: current directory).
        audio_only — set true to extract and save MP3 audio only.
    Output: Confirmation with the output file path and yt-dlp progress summary.
    """
    err = _require("yt-dlp")
    if err:
        return err

    out_path = Path(output_dir).expanduser().resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    argv = ["yt-dlp", "-o", f"{out_path}/%(title)s.%(ext)s"]
    if audio_only:
        argv += ["-x", "--audio-format", "mp3"]
    argv.append(url)

    output = _run_argv(argv, timeout=300)
    if "Error" in output:
        return f"Error downloading: {output}"
    return f"Download complete.\n\n{output}"


@tool
def ytdlp_list_formats(url: str) -> str:
    """List all available download quality and format options for a video URL.

    When to use: Before calling ytdlp_download, inspect which resolutions and
        codecs are available to choose the right quality level.
    When NOT to use: General video metadata (use ytdlp_info),
        downloading immediately (use ytdlp_download).
    Input: url — the full video/audio URL.
    Output: Table of format IDs with resolution, codec, bitrate, and file extension.
    """
    err = _require("yt-dlp")
    if err:
        return err

    return _run_argv(["yt-dlp", "--list-formats", url], timeout=30)


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_cli_tools(registry: ToolRegistry) -> int:
    """Register all CLI power tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "CLI",
        (
            "GitHub CLI (gh_command) rules:\n"
            "- Cross-repo queries (most recent PR, issues across all projects): use "
            "'gh search prs' or 'gh search issues' — these NEVER need --repo or cwd:\n"
            "    gh_command('search prs --author @me --state open --sort updated --limit 10 "
            "--json number,title,url,createdAt,repository')  # open PRs\n"
            "    gh_command('search prs --author @me --merged --sort updated --limit 10 "
            "--json number,title,url,closedAt,repository')   # merged PRs (use --merged, NOT --state merged)\n"
            "- Single-repo commands (pr list, issue list, release list): ALWAYS need "
            "--repo OWNER/REPO or cwd= pointing to a local checkout. Without one of these "
            "gh will fail with 'not a git repository':\n"
            "    gh_command('pr list --repo youruser/your-repo --state open --json number,title,url')\n"
            "- 'repository' is a valid --json field for 'gh search prs/issues' but NOT "
            "for 'gh pr list/view'. For pr list/view use: number, title, state, author, "
            "createdAt, updatedAt, url, additions, deletions, changedFiles, reviews, comments.\n"
            "- To get diff stats for a specific PR: gh_command('pr view NUMBER --repo OWNER/REPO "
            "--json additions,deletions,changedFiles,reviews,comments,url')\n"
            "\n"
            "yt-dlp rules:\n"
            "- ytdlp_download saves video/audio from YouTube and 1000+ other sites.\n"
            "- Always pass output_dir as an absolute path (e.g., '/Users/name/Downloads').\n"
            "- For audio only, set audio_only=True."
        ),
    )
    tools = [
        jq_query,
        jq_transform,
        yq_query,
        yq_convert,
        tree_view,
        gh_command,
        ncdu_report,
        ytdlp_info,
        ytdlp_download,
        ytdlp_list_formats,
    ]
    for func in tools:
        registry.register(func, category="CLI")
    return len(tools)
