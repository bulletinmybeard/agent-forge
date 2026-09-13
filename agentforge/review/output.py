"""Parse a user-requested review output path and write the report there."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

_SAVE_RE = re.compile(
    r"(?:store|save|write|dump)\b.{0,80}?\b(?:in|to|into|at)\s+"
    r"[`'\"]?([~./][^\s`'\"),;]+)[`'\"]?",
    re.IGNORECASE | re.DOTALL,
)

_CONTAINER_HOMES = frozenset({"/root", "/var/root"})
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")


def parse_review_output_path(query: str, target: str = "") -> str | None:
    """Return a dest path if the prompt asked to store the review, else None.

    Ignores the review target itself and ticket/attachment paths that are not
    introduced by store/save/write/dump.
    """
    match = _SAVE_RE.search(query or "")
    if not match:
        return None
    dest = match.group(1).rstrip(".,;:")
    target_norm = (target or "").rstrip("/")
    if dest.rstrip("/") == target_norm:
        return None
    return dest


def usable_branch_name(git_branch: str, prompt_branch: str = "") -> str:
    """Pick a filename-safe ref: git output if it looks like a ref, else prompt."""
    for candidate in ((git_branch or "").strip().splitlines()[0], prompt_branch):
        name = (candidate or "").strip()
        if _looks_like_git_ref(name):
            return name
    return ""


def resolve_review_output_file(
    dest: str,
    *,
    branch: str = "",
    when: datetime | None = None,
) -> Path:
    """Turn a dest (file or directory) into a concrete markdown path.

    Directory dests get ``review-<branch>-YYYY-MM-DD-HH-MM.md`` (local time,
    no seconds) so same-day reruns don't collide.

    ``~/...`` dests are left unexpanded when this process's home is container
    root, so a later host-worker dispatch can resolve them against the user's
    home instead of ``/root/Downloads``.
    """
    p = _dest_path(dest)
    if _looks_like_directory(p, dest):
        slug = _branch_slug(branch)
        stamp = (when or datetime.now()).strftime("%Y-%m-%d-%H-%M")
        return p / f"review-{slug}-{stamp}.md"
    return p


def write_review_output(path: Path, content: str) -> str:
    """Write *content* via ``write_file`` (unique suffix if the dest exists).

    Tilde paths on a container-root worker are dispatched to the host
    filesystem role in split mode. In-process container workers cannot reach
    the user's Downloads — refuse rather than silently write under ``/root``.
    """
    raw = str(path)
    if _is_tilde_on_container(raw):
        return _write_via_host_or_refuse(raw, content)
    from agentforge.tools.filesystem import write_file

    return write_file(raw, content, unique=True)


def _dest_path(dest: str) -> Path:
    if dest.startswith("~") and _is_container_home():
        return Path(dest)
    return Path(dest).expanduser()


def _is_container_home() -> bool:
    home = os.path.expanduser("~")
    try:
        return Path(home).resolve() in {Path(h) for h in _CONTAINER_HOMES}
    except OSError:
        return home in _CONTAINER_HOMES


def _is_tilde_on_container(raw: str) -> bool:
    return raw.startswith("~") and _is_container_home()


def _write_via_host_or_refuse(raw: str, content: str) -> str:
    try:
        from agentforge.tools.routing import dispatch_mode, get_role_for_tool
    except Exception:
        return _refuse_container_write(raw)

    if dispatch_mode() != "split":
        return _refuse_container_write(raw)

    try:
        from web.server.queue.dispatch_compat import saq_dispatch_tool

        role = get_role_for_tool("write_file")
        return str(saq_dispatch_tool("write_file", {"path": raw, "content": content, "unique": True}, role))
    except Exception as exc:
        return f"Could not write review to {raw}: {exc}"


def _refuse_container_write(raw: str) -> str:
    return (
        f"Could not write review to {raw}: worker home is container-root "
        "and has no host filesystem. The review is in the chat."
    )


def _looks_like_directory(path: Path, raw: str) -> bool:
    if raw.endswith("/") or raw.endswith("\\"):
        return True
    if not str(path).startswith("~"):
        if path.exists():
            return path.is_dir()
    return path.suffix == ""


def _looks_like_git_ref(name: str) -> bool:
    if not name or name.startswith("(") or " " in name:
        return False
    lowered = name.lower()
    if lowered.startswith("fatal:") or "not a git repository" in lowered:
        return False
    if lowered in {"(empty)", "empty", "head"}:
        return False
    return bool(_GIT_REF_RE.fullmatch(name))


def _branch_slug(branch: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", branch or "").strip("-").lower()
    return slug or "review"
