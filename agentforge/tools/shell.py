"""Shell tool — execute commands on the LOCAL machine.

Provides a general-purpose ``shell`` tool for running any shell command
locally, similar to how ``ssh`` works for remote hosts.

An optional allowlist in ``config.yaml → tools.shell.allowed_commands`` can
restrict which executables may be invoked.  When the list is empty (default),
all commands are permitted.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.shell import register_shell_tools

    registry = ToolRegistry()
    register_shell_tools(registry)
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from agentforge.tools.command_policy import evaluate
from agentforge.tools.command_policy_store import get_effective_policy

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Cancel event — set by the agent loop to signal subprocess termination
# ---------------------------------------------------------------------------

_shell_cancel_event: threading.Event | None = None


def set_shell_cancel_event(event: threading.Event | None) -> None:
    """Wire up the cancel event from the agent loop."""
    global _shell_cancel_event
    _shell_cancel_event = event


# --- Sudo secret provider (set by the surface that drives the agent) -------
# A provider exposes get(label)->str|None (cache-or-prompt) and invalidate(label).
# None when unwired (headless) -> sudo commands fail cleanly.
# Two layers so concurrent worker jobs can't clobber each other:
#  - the module global is the in-process default (set once per session; it must
#    be a global because the agent runs parallel tool calls via a
#    ThreadPoolExecutor whose threads do NOT inherit context vars).
#  - the ContextVar override is for the split-dispatch worker, where every tool
#    job runs in its own task/`asyncio.to_thread` context. Setting it there keeps
#    each job's provider isolated, so a finishing job can't null another's.
# The ContextVar wins when set; otherwise we fall back to the global.
_sudo_secret_provider = None  # type: ignore[var-annotated]
_sudo_secret_provider_ctx: ContextVar = ContextVar("sudo_secret_provider_ctx", default=None)


def set_sudo_secret_provider(provider) -> None:  # noqa: ANN001
    """Wire the process-wide sudo provider (in-process dispatch, one per session)."""
    global _sudo_secret_provider
    _sudo_secret_provider = provider


def set_sudo_secret_provider_ctx(provider):  # noqa: ANN001, ANN201
    """Wire a context-isolated sudo provider for the current task (worker jobs).

    Returns a token; pass it to ``reset_sudo_secret_provider_ctx`` in a finally.
    """
    return _sudo_secret_provider_ctx.set(provider)


def reset_sudo_secret_provider_ctx(token) -> None:  # noqa: ANN001
    try:
        _sudo_secret_provider_ctx.reset(token)
    except Exception:
        pass


def _active_sudo_provider():  # noqa: ANN202
    return _sudo_secret_provider_ctx.get() or _sudo_secret_provider


def _request_sudo_password(label: str = "localhost") -> str | None:
    """Return a sudo password for *label*, or None when none can be obtained."""
    provider = _active_sudo_provider()
    if provider is None:
        return None
    try:
        secret = provider.get(label)
    except Exception as exc:  # provider/transport failure must not run unconfirmed
        logger.warning("[shell] sudo secret provider error: %s", exc)
        return None
    return secret or None


def _invalidate_sudo_password(label: str = "localhost") -> None:
    provider = _active_sudo_provider()
    if provider is not None:
        try:
            provider.invalidate(label)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_default_timeout() -> int:
    """Return default timeout from config.yaml → tools.shell.timeout."""
    try:
        from agentforge.config import get_config

        cfg = get_config()
        return int(cfg._raw.get("tools", {}).get("shell", {}).get("timeout", 600))
    except Exception:
        return 600


def _auto_sudo_enabled() -> bool:
    """Whether ``tools.shell.auto_sudo`` permits auto-prepending ``sudo``.

    Defaults to False: without this, a command targeting a root-owned path is
    silently elevated to root — privilege escalation the guard classified
    *before* the rewrite never sees. Off by default; opt in explicitly.
    """
    try:
        from agentforge.config import get_config

        cfg = get_config()
        return bool(cfg._raw.get("tools", {}).get("shell", {}).get("auto_sudo", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sudo detection — stat-based ownership check
# ---------------------------------------------------------------------------

_SUDO_RE = re.compile(r"^\s*sudo\s+", re.IGNORECASE)

_SUDO_AUTH_FAIL_RE = re.compile(
    r"(sorry, try again|incorrect password attempt|[0-9]+ incorrect password|"
    r"sudo:.*authentication failure)",
    re.IGNORECASE,
)


def _looks_like_sudo_auth_failure(output: str) -> bool:
    return bool(_SUDO_AUTH_FAIL_RE.search(output or ""))


# Commands that modify files — only these get auto-sudo.
_MUTATING_RE = re.compile(
    r"^\s*(rm|mv|cp|chmod|chown|mkdir|rmdir|ln|touch|tee|install)\b",
    re.IGNORECASE,
)


def _needs_sudo(command: str) -> bool:
    """Check if a command starts with sudo."""
    return bool(_SUDO_RE.match(command))


def _extract_paths(command: str) -> list[str]:
    """Extract file/directory path arguments from a shell command.

    Skips the command verb itself and any flags (``-x``, ``--flag``).
    Expands ``~`` to the real home directory.

    Uses ``shlex.split`` so that quoted paths containing spaces (e.g.,
    ``"/Users/foo/Mobile Documents/file.pdf"``) are kept intact rather than
    fragmented on the space.  Falls back to plain ``.split()`` if the command
    string is not valid POSIX shell (e.g., contains unmatched quotes from a
    partially formed command).
    """
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        # Unmatched quotes or other shlex parse error — fall back to whitespace split
        parts = command.strip().split()

    if not parts:
        return []

    paths: list[str] = []
    skip_next = False
    for i, part in enumerate(parts):
        if skip_next:
            skip_next = False
            continue
        # Skip flags; some flags consume the next token as a value.
        if part.startswith("-"):
            if part in ("-o", "-g", "-m", "--mode", "--owner", "--group", "--target-directory", "-t", "-T"):
                skip_next = True
            continue
        # First non-flag token is the command verb — skip it.
        if i == 0:
            continue
        # Anything that looks like a path (absolute, home-relative, or
        # dot-relative) is a candidate.
        expanded = os.path.expanduser(part)
        if expanded.startswith(("/", ".")):
            paths.append(expanded)
    return paths


def _find_existing_ancestor(path: str) -> str | None:
    """Walk up from *path* to the nearest existing file or directory.

    When the target doesn't exist yet (e.g., ``rm`` on a file that the model
    expects to be there, or ``mkdir`` for a new dir), we check the parent
    directory's ownership instead.

    Relative paths are resolved against cwd first so that ``./foo`` doesn't
    accidentally walk all the way up to ``/``.
    """
    # Resolve relative paths to absolute so the walk-up stops at the
    # correct user-owned directory instead of reaching /.
    p = os.path.normpath(os.path.abspath(path))
    while p and p != os.sep:
        if os.path.exists(p):
            return p
        p = os.path.dirname(p)
    return os.sep if os.path.exists(os.sep) else None


def _is_root_owned(path: str) -> bool:
    """Return True if *path* (or its nearest existing ancestor) is owned by root
    AND is not world-writable.

    Uses ``os.stat`` to read the real uid/gid — no hardcoded path list needed.

    World-writable root-owned directories (e.g., /tmp → /private/tmp on macOS)
    do not require sudo — the current user can write to them directly.
    """
    target = _find_existing_ancestor(path)
    if target is None:
        return False
    try:
        st = os.stat(target)
        if st.st_uid != 0:
            return False
        # World-writable (o+w) means no sudo needed even if root owns it
        world_writable = bool(st.st_mode & 0o002)
        return not world_writable
    except OSError:
        return False


def _should_auto_sudo(command: str) -> bool:
    """Decide whether to auto-prepend ``sudo`` based on file ownership.

    Returns True when **all** of the following are true:

    1. The command does NOT already start with ``sudo``.
    2. The command is a mutating operation (rm, mv, cp, chmod, …).
    3. At least one path argument (or its nearest existing ancestor) is
       owned by root (uid 0).

    This replaces the old hardcoded ``_SYSTEM_PATHS`` regex with a real
    ``stat``-based ownership check, so it works for *any* root-owned
    location on the filesystem — not just a predefined list.
    """
    if _needs_sudo(command):
        return False  # already has sudo

    if not _MUTATING_RE.match(command.strip()):
        return False

    paths = _extract_paths(command)
    if not paths:
        return False

    for p in paths:
        if _is_root_owned(p):
            logger.debug("[shell] Path '%s' is root-owned — will auto-sudo", p)
            return True
    return False


# ---------------------------------------------------------------------------
# Platform compatibility — LLM-powered GNU → BSD rewriting on macOS
# ---------------------------------------------------------------------------

_IS_MACOS = platform.system() == "Darwin"

_PLATFORM_FIX_PROMPT = """\
You are a shell command translator. The user is on macOS (BSD userland).
The following command may use GNU/Linux-specific syntax that won't work on macOS.

Rewrite the command to be macOS-compatible. Common issues:
- sed -i needs sed -i '' (BSD requires backup suffix argument)
- xargs -d is not available (use tr + xargs -0)
- readlink -f doesn't exist (use realpath)
- date -d doesn't exist (use date -j -f)
- stat -c doesn't exist (use stat -f)
- grep -P not available (use grep -E or perl)
- cp --parents not available (use rsync or cpio)
- timeout command not available (use gtimeout from coreutils or perl)

If the command is ALREADY macOS-compatible, return it UNCHANGED.
Return ONLY the (possibly rewritten) command. No explanation, no markdown, \
no code fences — just the raw shell command on a single line."""


def _needs_platform_fix(command: str) -> bool:
    """Quick heuristic: does this command contain anything that MIGHT be GNU-specific?

    This is intentionally broad — the LLM will decide if a fix is actually needed.
    We only use this to skip the LLM call for obviously-fine commands.
    """
    if not _IS_MACOS:
        return False

    # Keywords that are often used with GNU-specific flags
    _CANDIDATES = (
        "sed ",
        "xargs ",
        "readlink ",
        "date ",
        "stat ",
        "grep -P",
        "cp --",
        "timeout ",
        "mktemp ",
        "realpath ",
        "sort -V",
    )
    cmd_lower = command.lower()
    return any(kw in cmd_lower for kw in _CANDIDATES)


_ICLOUD_MARKERS = (
    "Mobile Documents/com~apple~CloudDocs",
    "com~apple~CloudDocs",
)

# Shell commands that read file content and will deadlock on iCloud paths.
_ICLOUD_READ_CMDS = re.compile(
    r"^\s*(pdftotext|pdfinfo|pdfimages|cat|head|tail|wc|strings|file|hexdump|xxd)\b",
    re.IGNORECASE,
)


def _contains_icloud_path(command: str) -> bool:
    """Return True if the command references an iCloud Drive path."""
    return any(marker in command for marker in _ICLOUD_MARKERS)


def _rewrite_icloud_command(command: str) -> tuple[str, str]:
    """Rewrite file-reading commands that target iCloud paths to copy-then-read.

    iCloud Drive files may be stubs or held by a sync lock.  Direct reads
    produce [Errno 11] Resource deadlock avoided or silently return empty
    output.  The fix: extract iCloud file arguments, copy each to /tmp, and
    rewrite the command to reference the /tmp copies instead.

    Returns (rewritten_command, description) — description is empty if no
    rewrite was needed.
    """
    if not _ICLOUD_READ_CMDS.match(command.strip()):
        return command, ""
    if not _contains_icloud_path(command):
        return command, ""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command, ""

    rewritten_tokens = []
    copies: list[str] = []  # "cp 'src' 'dst'" statements to prepend
    cleanups: list[str] = []  # "rm -f 'dst'" statements to append

    for token in tokens:
        if any(marker in token for marker in _ICLOUD_MARKERS):
            # Securely create the temp target (mode 0600, O_EXCL) so a local
            # attacker can't pre-create or symlink a predictable path. We only
            # need the path; the copy below overwrites the (empty) file.
            suffix = os.path.splitext(token)[1] or ""
            fd, tmp_name = tempfile.mkstemp(prefix="_agentforge_", suffix=suffix)
            os.close(fd)
            copies.append(f"cp {shlex.quote(token)} {shlex.quote(tmp_name)}")
            cleanups.append(f"rm -f {shlex.quote(tmp_name)}")
            rewritten_tokens.append(tmp_name)
        else:
            rewritten_tokens.append(shlex.quote(token) if " " in token else token)

    if not copies:
        return command, ""

    copy_block = " && ".join(copies)
    read_cmd = " ".join(rewritten_tokens)
    cleanup_block = "; ".join(cleanups)
    rewritten = f"{{ {copy_block} && {read_cmd}; {cleanup_block}; }}"
    return rewritten, f"iCloud copy-then-read rewrite applied ({len(copies)} file(s))"


def _fix_platform_compat(command: str) -> tuple[str, str]:
    """Rewrite GNU-isms to their BSD/macOS equivalents using an LLM.

    Returns (fixed_command, fix_description).
    On Linux or when no fix is needed, returns the command unchanged.
    """
    if not _IS_MACOS or not _needs_platform_fix(command):
        return command, ""

    try:
        from ollama import Client as OllamaClient

        from agentforge.config import get_config

        cfg = get_config()
        fast = cfg.get_profile("fast")
        host = fast.host
        model = fast.model

        client = OllamaClient(host=host)
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _PLATFORM_FIX_PROMPT},
                {"role": "user", "content": command},
            ],
            options={"temperature": 0.0, "num_predict": 500},
        )

        fixed = response["message"]["content"].strip()

        # Strip code fences if the model wrapped them
        if fixed.startswith("```"):
            lines = fixed.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            fixed = "\n".join(lines).strip()

        # If the model returned the same command, no fix was needed
        if fixed == command or not fixed:
            return command, ""

        logger.info("[shell] Platform fix: %s → %s", command[:80], fixed[:80])
        return fixed, f"rewritten for macOS: {command[:60]} → {fixed[:60]}"

    except Exception as exc:
        logger.warning("[shell] Platform fix LLM call failed (using original): %s", exc)
        return command, ""


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------


# Names of agent tool functions that the LLM occasionally tries to invoke as
# CLI binaries via shell(). The first word of the shell command is checked
# against this set; on match, the call is rejected with a corrective hint
# pointing the model at the proper tool_call mechanism.
#
# Pattern coverage prefers exact names (less false-positive risk) and a few
# narrow prefix patterns (git_*,
# docker_*, image_*, ytdlp_*, archive_*, qdrant_*, redis_*, web_*, code_*,
# k6_*, audio_*, ardour_*) for groups that share a common prefix.
_AGENT_TOOL_NAMES = frozenset(
    {
        # filesystem
        "read_file",
        "write_file",
        "append_file",
        "create_directory",
        "find_files",
        "grep_text",
        "file_info",
        "read_dir",
        "copy_file",
        "move_file",
        "delete_file",
        # remote / network
        "ssh",
        "health_check",
        "download_file",
        # logs / data
        "analyze_logs",
        "diff_files",
        # notifications
        "notify",
        "notify_list",
        "notify_remove",
        # parsers
        "jq_query",
        "yq_query",
        # cli wrappers — these DO have CLI equivalents but the agent tool
        # produces structured output; the LLM should prefer the tool form
        "gh_command",
        "test_runner",
        # tmdb
        "multi_search",
        "trending_media",
    }
)

_AGENT_TOOL_PREFIXES = (
    "git_",
    "docker_",
    "image_",
    "video_",
    "ytdlp_",
    "archive_",
    "qdrant_",
    "redis_",
    "web_",
    "code_",
    "k6_",
    "audio_",
    "ardour_",
    "movie_",
    "tv_",
    "person_",
    "generate_",
)


def _detect_agent_tool_misuse(command: str) -> str | None:
    """If the shell command's first word is an agent tool name, return a
    corrective error string; otherwise return None.

    Detects e.g., ``shell("find_files --root . > out.txt")`` —
    the model thinks it's a CLI binary, the shell can't find it, and the
    "command not found" error ends up captured in the user's output file.
    """
    parts = command.strip().split()
    if not parts:
        return None
    first = parts[0]
    # Strip an env var prefix like FOO=bar baz_tool ...
    while "=" in first and len(parts) > 1:
        parts = parts[1:]
        first = parts[0]
    # Tools may have been wrapped in path-like prefixes (./tool, /usr/local/bin/tool);
    # strip down to the basename for matching.
    base = first.rsplit("/", 1)[-1]
    if base in _AGENT_TOOL_NAMES or base.startswith(_AGENT_TOOL_PREFIXES):
        return (
            f"BLOCKED: '{base}' is an agent tool function, not a CLI command. "
            f"You attempted to run it via shell — that won't work. "
            f"Invoke it as a structured tool_call with named arguments instead. "
            f"For example, use a real tool_call like {base}(parent_id=..., max_items=...) "
            f"rather than shell('{base} --parent_id 0 > out.txt'). "
            f"To save a tool's output to a file, capture the return value of the "
            f"tool_call in your reasoning, then call write_file(path=..., content=<that value>) "
            f"as a separate tool_call — never use shell redirection (`>`) for this."
        )
    return None


def _validate_command(command: str) -> str | None:
    """Validate a command against the effective command policy.

    Returns an error string if denied, None if OK.
    """
    policy = get_effective_policy("shell")
    verdict = evaluate("shell", command, policy)
    if verdict.action == "deny":
        return f"Error: {verdict.reason}"
    return None


# ---------------------------------------------------------------------------
# shell — general-purpose local command execution
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use this tool to run LOCAL shell commands — package managers (npm, pip, "
        "poetry), runtimes (python, node), build tools, scripts, and any CLI. "
        "For REMOTE hosts, use ssh instead. "
        "IMPORTANT: For long-running commands like 'npx create-react-app', "
        "'npm install', 'poetry install', or any project scaffolding/build, "
        "pass timeout=600 (or higher) to avoid premature timeout. "
        "The default timeout is 600s which is enough for most commands but "
        "NOT for very large package installs or project creation (use 900+). "
        "SUDO: Commands that modify files in system paths (/var, /etc, /usr, "
        "/System, /Library, /opt) require 'sudo'. Always use 'sudo rm ...', "
        "'sudo mv ...', etc. for these paths. A safety system handles the "
        "password automatically — just include sudo in the command. "
        "For removing directories always use 'rm -rf <dir>' (never plain 'rm -r') "
        "because read-only files inside (e.g., .git objects) cause 'rm -r' to hang "
        "waiting for interactive confirmation in a non-interactive shell. "
        "For removing single files use plain 'rm <file>' without -f so missing-file "
        "errors are still visible. "
        "When running commands that produce a lot of output (logs, find, etc.), "
        "pipe through head/tail to limit output size."
    )
)
def shell(command: str, cwd: str = "", timeout: int = 0) -> str:
    """Run a shell command on the LOCAL machine and return its output.

    When to use: Any local command — package managers (npm, pip, poetry, cargo),
        runtimes (python, node), build tools, version checks, scripts, file searches,
        CLI commands. For long-running commands (installs, scaffolding), pass timeout=300+.
    When NOT to use: Remote hosts (use ssh instead), file reading (use read_file),
        file searches (use find_files), git operations (use git_* tools), Docker (use
        docker_* tools).

    command: the shell command to execute (e.g., 'npm --version', 'poetry install')
    cwd: working directory to run in (default: current directory)
    timeout: max execution time in seconds (0 = use default 600s). Use 900+ for very large installs/scaffolding.

    Examples:
      shell('npm --version')
      shell('node --version')
      shell('python --version')
      shell('poetry install', cwd='~/www/my-project', timeout=600)
      shell('npx create-react-app test-app', cwd='~/www', timeout=600)
      shell('npm install', cwd='~/www/my-app', timeout=600)
      shell('pip list --outdated')
      shell('find . -name "*.py" | head -20', cwd='~/www/project')
      shell('cat package.json | jq .version', cwd='~/www/my-app')
      shell('make build', cwd='~/www/my-project')
      shell('n exec 20.04 node app.js', cwd='~/www/test-project')
    """
    if not command or not command.strip():
        return "Error: No command provided."

    # Intercept agent tool names misused as CLI commands. Devstral-Small in
    # particular has been observed emitting shell calls like
    #   "find_files --root . > out.txt 2>&1"
    # confusing the agent's tool function with a real CLI binary. The shell
    # then reports "command not found" and the model captures that into the
    # output file. Catch this early and route the model back to the proper
    # tool_call mechanism.
    tool_misuse = _detect_agent_tool_misuse(command)
    if tool_misuse:
        return tool_misuse

    # Validate against allowlist/blocklist
    err = _validate_command(command)
    if err:
        return err

    # Resolve working directory
    work_dir = None
    if cwd:
        expanded = os.path.expanduser(cwd)
        work_dir = Path(expanded).resolve()
        if not work_dir.is_dir():
            return f"Error: Working directory '{cwd}' does not exist."

    # Resolve timeout
    if timeout <= 0:
        timeout = _get_default_timeout()

    # Enforce a hard minimum of 600s regardless of what the caller (or LLM) passed.
    # The LLM routinely passes conservative values like timeout=30 or timeout=60
    # which are far too short for many real-world commands (brew deps, find on large
    # trees, etc.).  We never allow less than 600s — callers who need a shorter
    # deadline should use subprocess directly.
    _MIN_TIMEOUT = 600
    if timeout < _MIN_TIMEOUT:
        logger.debug("[shell] Enforcing minimum timeout: %ds → %ds", timeout, _MIN_TIMEOUT)
        timeout = _MIN_TIMEOUT

    # Auto-extend timeout for known very-long-running commands beyond the minimum.
    _LONG_RUNNING = re.compile(
        r"\b(create-react-app|create-next-app|create-vite|"
        r"npm\s+(install|ci|create|init)|"
        r"npx\s+create-|"
        r"yarn\s+(install|create)|"
        r"pnpm\s+(install|create)|"
        r"poetry\s+(install|update|lock)|"
        r"pip\s+install|"
        r"cargo\s+(build|install)|"
        r"make\s|cmake\s|"
        r"docker\s+build)\b",
        re.IGNORECASE,
    )
    if _LONG_RUNNING.search(command) and timeout < 900:
        logger.info("[shell] Auto-extending timeout %ds → 900s for long-running command", timeout)
        timeout = 900

    # Normalise rm flags for non-interactive use:
    #
    # • rm -r (recursive) MUST have -f so it never prompts on read-only files
    #   (e.g., .git objects are always read-only; without -f the command hangs
    #   waiting for confirmation and times out in a non-interactive shell).
    #   rm -r → rm -rf, rm -r --no-preserve-root → rm -rf --no-preserve-root
    #
    # • rm without -r should NOT have -f so "no such file" errors still surface
    #   via a non-zero exit code (rm -f silently returns 0 for missing files).
    #   rm -f somefile → rm somefile
    _RM_RE = re.compile(r"\brm\s+(-[a-zA-Z]*(?:\s+-[a-zA-Z]*)*)", re.IGNORECASE)

    def _normalise_rm(m: re.Match) -> str:
        flags_str = m.group(1)
        all_flags: set[str] = set()
        for token in flags_str.split():
            if token.startswith("-") and not token.startswith("--"):
                all_flags.update(token.lstrip("-"))
        if "r" in all_flags:
            # Recursive: ensure -f is present so no interactive prompts
            all_flags.add("f")
            return "rm -" + "".join(sorted(all_flags))
        else:
            # Non-recursive: strip -f so missing-file errors are visible
            all_flags.discard("f")
            return ("rm -" + "".join(sorted(all_flags))) if all_flags else "rm"

    run_command_safe = _RM_RE.sub(_normalise_rm, command)
    if run_command_safe != command:
        logger.info("[shell] Normalised rm flags for non-interactive use: %s → %s", command, run_command_safe)
        command = run_command_safe

    # Platform compatibility: LLM-powered GNU → BSD rewriting on macOS
    command, compat_note = _fix_platform_compat(command)
    if compat_note:
        logger.info("[shell] Platform fix applied: %s", compat_note)

    # iCloud path rewrite: transparently copy-then-read for file-reading commands
    # that target iCloud Drive paths, which cause [Errno 11] Resource deadlock.
    command, icloud_note = _rewrite_icloud_command(command)
    if icloud_note:
        logger.info("[shell] iCloud path rewrite: %s", icloud_note)

    # Auto-sudo: if the command targets a system path but the model forgot to
    # include sudo, prepend it automatically. Gated behind tools.shell.auto_sudo
    # (default off) — otherwise this silently elevates to root a command the
    # guard already classified in its non-sudo form.
    if _auto_sudo_enabled() and _should_auto_sudo(command):
        command = f"sudo {command}"
        logger.info("[shell] Auto-prepended sudo for system path: %s", command[:120])

    logger.debug("[shell] $ %s%s", command, f"  (in {work_dir})" if work_dir else "")

    # Sudo handling: if the command starts with `sudo` and a password is
    # configured, rewrite to `sudo -S` and pipe the password via stdin.
    # NOTE: the guard/confirmation in the registry ran on the ORIGINAL command
    # string, before any auto-sudo prepend above. So when auto_sudo is enabled
    # and elevated this command, the elevated form was NOT separately classified
    # or confirmed — that's the trade-off of opting into auto_sudo.
    sudo_pw = ""
    run_command = command
    needs_sudo = _needs_sudo(command)
    if needs_sudo:
        sudo_pw = _request_sudo_password("localhost")
        if not sudo_pw:
            return (
                "Error: this command needs sudo but no password was provided "
                "(cancelled, or no interactive session to prompt). Re-run from an "
                "interactive session."
            )
        # -S so sudo reads the password from stdin
        run_command = _SUDO_RE.sub("sudo -S ", command, count=1)
        logger.info("[shell] sudo detected — password obtained via interactive provider")

    merged = f"{run_command} 2>&1"

    try:
        # Use Popen + polling instead of subprocess.run so we can:
        # 1. Kill the process on cancellation (via _shell_cancel_event)
        # 2. Kill the process on timeout without leaving zombies
        proc = subprocess.Popen(
            merged,
            shell=True,  # noqa: S602
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if sudo_pw else None,
            text=True,
            cwd=str(work_dir) if work_dir else None,
            preexec_fn=os.setsid,  # new process group for clean kill
        )

        # Send sudo password if needed, then close stdin
        if sudo_pw and proc.stdin is not None:
            try:
                proc.stdin.write(f"{sudo_pw}\n")
                proc.stdin.flush()
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        # Poll loop: check for completion, timeout, and cancellation
        deadline = time.time() + timeout
        _POLL_INTERVAL = 0.5
        while True:
            retcode = proc.poll()
            if retcode is not None:
                break  # process finished

            if time.time() > deadline:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    proc.kill()
                proc.wait(timeout=5)
                return f"Error: Command timed out after {timeout}s — `{command}`"

            if _shell_cancel_event and _shell_cancel_event.is_set():
                logger.info("[shell] Cancel requested — killing subprocess")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    proc.kill()
                proc.wait(timeout=5)
                return f"[cancelled] $ {command}"

            time.sleep(_POLL_INTERVAL)

        output = ((proc.stdout.read() if proc.stdout is not None else "") or "").strip()
        stderr_out = ((proc.stderr.read() if proc.stderr is not None else "") or "").strip()

        # Strip the sudo password prompt from output if present
        if sudo_pw and output.startswith("[sudo]"):
            lines = output.split("\n", 1)
            output = lines[1].strip() if len(lines) > 1 else ""

        if needs_sudo and sudo_pw and _looks_like_sudo_auth_failure(output):
            _invalidate_sudo_password("localhost")
            return "Error: sudo rejected the password. It was discarded; re-run to be prompted again."

        if retcode != 0:
            parts = []
            if output:
                parts.append(output)
            if stderr_out and stderr_out != output:
                parts.append(f"STDERR: {stderr_out}")
            error_detail = "\n".join(parts) if parts else "(no output)"
            return f"[exit {retcode}] $ {command}\n\n{error_detail}"

        return f"$ {command}\n\n{output}" if output else f"$ {command}\n\n(completed successfully, no output)"

    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_shell_tools(registry: ToolRegistry) -> int:
    """Register all shell tools with the given *registry*."""
    count = 0
    for _name, func in list(globals().items()):
        if callable(func) and hasattr(func, "_is_tool") and not _name.startswith("_"):
            registry.register(func, category="Shell")
            count += 1
            logger.debug("Registered shell tool: %s", func.__name__)
    return count
