"""Read-only posture gate.

When a run is marked ``read_only`` (the client sends ``overrides.read_only``),
the agent must refuse any tool call that could change state. This module decides
whether a single tool call is read-safe.

Policy — fail CLOSED. A call is allowed only when it is *provably* read-only:

* Known filesystem/structured writers (``code_edit``, ``write_file``, ...) are
  always blocked.
* ``shell`` / ``ssh`` commands are parsed: every segment must lead with a
  read-only verb (or a read-only subcommand of a dual-use tool like ``docker``
  / ``git`` / ``systemctl``), with no output redirection to a real file.
  Anything not recognised as read-only is treated as mutating and blocked.
* Other structured tools are assumed read-only (the known writers are the
  blocklist above). A new mutating structured tool would need adding here.

The gate is OPT-IN: it only runs when the run is read_only, so writable runs and
non-read-only clients are unaffected.
"""

from __future__ import annotations

import shlex

# Structured tools that change state — always blocked under read_only.
MUTATING_TOOLS = frozenset(
    {
        "code_edit",
        "write_file",
        "append_file",
        "create_directory",
        "make_directory",
        "move_file",
        "rename_file",
        "delete_file",
        "remove_file",
        "apply_patch",
        "edit_file",
    }
)

# Leading commands that only read state.
_READ_VERBS = frozenset(
    {
        "ls",
        "dir",
        "cat",
        "bat",
        "head",
        "tail",
        "less",
        "more",
        "stat",
        "file",
        "wc",
        "nl",
        "grep",
        "egrep",
        "fgrep",
        "zgrep",
        "rg",
        "ag",
        "find",
        "fd",
        "tree",
        "pwd",
        "echo",
        "printf",
        "date",
        "cal",
        "uname",
        "hostname",
        "whoami",
        "id",
        "groups",
        "env",
        "printenv",
        "ps",
        "pgrep",
        "top",
        "free",
        "df",
        "du",
        "uptime",
        "who",
        "w",
        "last",
        "lsof",
        "netstat",
        "ss",
        "ip",
        "ifconfig",
        "route",
        "arp",
        "dig",
        "nslookup",
        "host",
        "getent",
        "readlink",
        "realpath",
        "dirname",
        "basename",
        "sort",
        "uniq",
        "cut",
        "tr",
        "column",
        "fold",
        "comm",
        "diff",
        "cmp",
        "jq",
        "yq",
        "xmllint",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "cksum",
        "which",
        "type",
        "command",
        "whereis",
        "locate",
        "lsblk",
        "blkid",
        "lscpu",
        "lsmem",
        "lspci",
        "lsusb",
        "dmesg",
        "vmstat",
        "iostat",
        "nproc",
        "tty",
        "locale",
        "true",
        "false",
    }
)

# Dual-use tools: only these subcommands read state. Any other subcommand (or a
# missing one) is treated as mutating.
_DUAL_USE_READ_SUBCMDS: dict[str, frozenset[str]] = {
    "docker": frozenset(
        {"ps", "logs", "inspect", "images", "stats", "top", "version", "info", "port", "history", "diff", "events"}
    ),
    "git": frozenset(
        {
            "status",
            "log",
            "diff",
            "show",
            "branch",
            "remote",
            "rev-parse",
            "describe",
            "cat-file",
            "ls-files",
            "blame",
            "tag",
            "shortlog",
            "reflog",
            "config",
        }
    ),
    "systemctl": frozenset(
        {
            "status",
            "show",
            "list-units",
            "list-unit-files",
            "is-active",
            "is-enabled",
            "is-failed",
            "cat",
            "list-dependencies",
            "get-default",
        }
    ),
    "kubectl": frozenset({"get", "describe", "logs", "top", "explain", "api-resources", "version", "cluster-info"}),
}
# `docker compose <sub>` — only these read.
_COMPOSE_READ_SUBCMDS = frozenset({"ps", "logs", "top", "config", "ls", "images", "version"})
# Flags that consume the next token as their value — so we don't mistake that
# value (e.g., the file after `-f`) for the subcommand.
_VALUE_FLAGS = frozenset(
    {
        "-f",
        "--file",
        "-p",
        "--project-name",
        "-c",
        "--context",
        "-H",
        "--host",
        "-n",
        "--namespace",
        "--project-directory",
        "-l",
        "--log-level",
    }
)
# `find` with these actions writes/executes — block even though `find` reads.
_FIND_WRITE_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf"})

_INFO_FLAGS = frozenset({"-v", "-V", "--version", "-h", "--help"})

# Segment separators and write redirections.
_SEGMENT_SEPS = ("&&", "||", ";", "|", "\n")


def is_read_only_safe(name: str, args: dict | None) -> bool:
    """True when this tool call cannot change state. Fails closed (False) on
    anything not recognised as read-only."""
    if name in MUTATING_TOOLS:
        return False
    if name in ("shell", "ssh"):
        command = (args or {}).get("command") or ""
        if not command.strip():
            return True  # no-op; nothing to execute
        return _command_is_read_only(command)
    # Other structured tools are reads (known writers are in MUTATING_TOOLS).
    return True


def _command_is_read_only(command: str) -> bool:
    # Output redirection to a real file is a write (allow only /dev/null).
    for token in _redirection_targets(command):
        if token not in ("/dev/null", "/dev/stderr", "/dev/stdout"):
            return False

    for segment in _split_segments(command):
        if not _segment_is_read_only(segment):
            return False
    return True


def _split_segments(command: str) -> list[str]:
    segments = [command]
    for sep in _SEGMENT_SEPS:
        segments = [part for seg in segments for part in seg.split(sep)]
    return [s.strip() for s in segments if s.strip()]


def _redirection_targets(command: str) -> list[str]:
    """Filenames on the right of a `>`/`>>` redirection (best-effort)."""
    targets: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes etc. — can't parse safely, treat as a write target.
        return ["<unparseable>"]
    for i, tok in enumerate(tokens):
        if tok in (">", ">>"):
            if i + 1 < len(tokens):
                targets.append(tokens[i + 1])
        elif tok.startswith(">") and len(tok) > 1:
            targets.append(tok.lstrip(">"))
        elif tok.endswith(">") and not tok.endswith("2>") and tok not in ("2>", "1>"):
            # e.g., `foo>bar` rare; conservative
            pass
    return targets


def _segment_is_read_only(segment: str) -> bool:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    # Skip leading env-assignments (FOO=bar cmd ...) and `sudo`/`env`.
    idx = 0
    while idx < len(tokens) and ("=" in tokens[idx] and not tokens[idx].startswith("-")):
        idx += 1
    if idx < len(tokens) and tokens[idx] in ("sudo", "command", "nice", "ionice", "time"):
        idx += 1
    if idx >= len(tokens):
        return True
    verb = tokens[idx].rsplit("/", 1)[-1]  # strip path: /usr/bin/ls -> ls
    rest = tokens[idx + 1 :]

    if verb == "find":
        return not any(f in rest for f in _FIND_WRITE_FLAGS)

    if rest and _is_info_flags_only(rest):
        return True

    if verb in _DUAL_USE_READ_SUBCMDS:
        sub, after = _subcommand(rest)
        if sub is None:
            return False
        if verb == "docker" and sub == "compose":
            compose_sub, _ = _subcommand(after)
            return compose_sub in _COMPOSE_READ_SUBCMDS
        if sub in ("version", "help") and not after:
            return True
        return sub in _DUAL_USE_READ_SUBCMDS[verb]

    if rest:
        sub, after = _subcommand(rest)
        if sub in ("version", "help") and not after:
            return True

    return verb in _READ_VERBS


def _subcommand(tokens: list[str]) -> tuple[str | None, list[str]]:
    """First non-flag token (the subcommand) and the tokens after it, skipping
    flags and the values consumed by value-taking flags (e.g., `-f file`)."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _VALUE_FLAGS:
            i += 2  # skip the flag and its value
            continue
        if tok.startswith("-"):
            i += 1  # boolean flag (or `--flag=value`)
            continue
        return tok, tokens[i + 1 :]
    return None, []


def _is_info_flags_only(tokens: list[str]) -> bool:
    """True when every token is a pure version/help flag (no other args)."""
    if not tokens:
        return False
    return all(tok in _INFO_FLAGS for tok in tokens)
