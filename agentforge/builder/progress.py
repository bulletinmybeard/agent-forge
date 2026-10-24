"""Controller-owned progress ledger for a plan workspace.

Sub-agents write their own report files. Only this module updates
``progress.md``, so a restarted ``@build`` can see which tasks finished
without asking the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_STATUSES = {"pending", "running", "complete", "blocked"}
_CHECK_RE = re.compile(r"^- (T\d+(?:\+T\d+)*): (pending|running|complete|blocked)(?: — (.*))?$")


@dataclass(frozen=True)
class ChecklistItem:
    id: str
    status: str
    detail: str


def split_progress(text: str) -> tuple[str, str]:
    """Return the checklist head and the log body."""
    raw = text or ""
    marker = "\n## Log"
    idx = raw.find(marker)
    if idx < 0:
        if raw.startswith("## Log"):
            return "", raw[len("## Log") :].lstrip("\n")
        return raw, ""
    return raw[:idx], raw[idx + len(marker) :].lstrip("\n")


def parse_checklist(text: str) -> list[ChecklistItem]:
    head, _log = split_progress(text)
    items: list[ChecklistItem] = []
    for line in head.splitlines():
        match = _CHECK_RE.match(line.strip())
        if not match:
            continue
        items.append(
            ChecklistItem(
                id=match.group(1),
                status=match.group(2),
                detail=(match.group(3) or "").strip(),
            )
        )
    return items


def has_started(text: str) -> bool:
    """True once any task has left ``pending``."""
    return any(item.status != "pending" for item in parse_checklist(text))


def constituent_ids(task_id: str) -> list[str]:
    return [part for part in (task_id or "").split("+") if part]


def is_complete(text: str, task_id: str) -> bool:
    done = {item.id for item in parse_checklist(text) if item.status == "complete"}
    parts = constituent_ids(task_id)
    return bool(parts) and all(part in done for part in parts)


def remaining_task_ids(text: str, task_ids: list[str]) -> list[str]:
    """Drop ids whose every constituent checklist line is ``complete``.

    ``T1+T2`` is one worker. It stays until both ``T1`` and ``T2`` are complete.
    """
    return [task_id for task_id in task_ids if not is_complete(text, task_id)]


def init_progress(directory_display: str, tasks: list) -> str:
    lines = [f"# Progress — {directory_display}", ""]
    for task in tasks:
        task_id = getattr(task, "id", "") or ""
        title = getattr(task, "title", "") or ""
        lines.append(f"- {task_id}: pending — {title}")
    lines.append("")
    lines.append("## Log")
    lines.append("")
    return "\n".join(lines)


def mark_progress(text: str, task_id: str, status: str, note: str = "") -> str:
    """Set each constituent checklist line and append a log line per id.

    The checklist detail stays the task title. ``note`` (usually the report
    filename) goes on the log line only.
    """
    if status not in _STATUSES:
        raise ValueError(status)
    parts = constituent_ids(task_id)
    if not parts:
        return text or ""
    head, log = split_progress(text)
    lines = head.splitlines()
    index_by_id: dict[str, int] = {}
    for i, line in enumerate(lines):
        match = _CHECK_RE.match(line.strip())
        if match:
            index_by_id[match.group(1)] = i
    for part in parts:
        if part in index_by_id:
            match = _CHECK_RE.match(lines[index_by_id[part]].strip())
            detail = (match.group(3) or "").strip() if match else ""
            lines[index_by_id[part]] = f"- {part}: {status} — {detail}" if detail else f"- {part}: {status}"
        else:
            extra = f" — {note}" if note else ""
            lines.append(f"- {part}: {status}{extra}")
    while lines and lines[-1] == "":
        lines.pop()
    suffix = f" — {note}" if note else ""
    addition = "\n".join(f"- {part}: {status}{suffix}" for part in parts)
    log_body = log.strip("\n")
    log_body = f"{log_body}\n{addition}" if log_body else addition
    return "\n".join(lines) + "\n\n## Log\n\n" + log_body + "\n"
