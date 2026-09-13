"""Markdown plan files under ~/agent-forge/plans/."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_TASK_RE = re.compile(
    r"^###\s+(T\d+)\s*[—\-–:]\s*(.+)$",
    re.MULTILINE,
)
_FILES_RE = re.compile(r"^\s*[-*]\s*Files?:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


@dataclass
class PlanTask:
    id: str
    title: str
    files: list[str] = field(default_factory=list)
    body: str = ""


def plan_dir() -> Path:
    d = Path.home() / "agent-forge" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def plan_filename(slug: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y-%m-%d-%H-%M")
    safe = re.sub(r"[^a-zA-Z0-9]+", "-", slug or "plan").strip("-").lower() or "plan"
    return f"{safe}-{stamp}.md"


def write_plan_file(
    content: str,
    *,
    slug: str,
    when: datetime | None = None,
    unique: bool = False,
) -> Path:
    """Write a timestamped plan file under ~/agent-forge/plans/.

    Do not use write_file(unique=True): that remaps a non-empty ``plans/``
    folder to ``plans_1/`` and we would record a path that does not exist.
    The filename already includes YYYY-MM-DD-HH-MM.
    """
    dest = plan_dir() / plan_filename(slug, when)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if unique:
        stem, suffix = dest.stem, dest.suffix
        n = 0
        while dest.exists():
            n += 1
            dest = dest.with_name(f"{stem}_{n}{suffix}")
    dest.write_text(content, encoding="utf-8")
    return dest


def parse_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(markdown or "")
    if not match:
        return {}, markdown or ""
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        meta[key.strip()] = val.strip()
    body = markdown[match.end() :]
    return meta, body


def stamp_status(markdown: str, status: str) -> str:
    meta, body = parse_frontmatter(markdown)
    meta["status"] = status
    lines = ["---"] + [f"{k}: {v}" for k, v in meta.items()] + ["---", ""]
    return "\n".join(lines) + body.lstrip("\n")


def parse_tasks(body: str) -> list[PlanTask]:
    tasks: list[PlanTask] = []
    matches = list(_TASK_RE.finditer(body or ""))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[match.end() : end]
        files: list[str] = []
        fm = _FILES_RE.search(chunk)
        if fm:
            raw = fm.group(1)
            files = [p.strip().strip("`") for p in re.split(r"[,;]", raw) if p.strip().strip("`")]
        tasks.append(PlanTask(id=match.group(1), title=match.group(2).strip(), files=files, body=chunk.strip()))
    return tasks


def tasks_share_files(tasks: list[PlanTask]) -> bool:
    seen: set[str] = set()
    for task in tasks:
        for f in task.files:
            key = f.lower()
            if key in seen:
                return True
            seen.add(key)
    return False


def merge_overlapping_tasks(tasks: list[PlanTask]) -> list[PlanTask]:
    """Collapse tasks that touch the same files into one worker.

    Separate AgentLoops do not share memory, so T2 will rewrite T1's README.
    """
    if len(tasks) <= 1:
        return list(tasks)
    n = len(tasks)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    filesets = [set(f.lower() for f in t.files if f) for t in tasks]
    for i in range(n):
        for j in range(i + 1, n):
            if filesets[i] and filesets[j] and filesets[i] & filesets[j]:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out: list[PlanTask] = []
    for idxs in sorted(groups.values(), key=lambda xs: xs[0]):
        parts = [tasks[i] for i in idxs]
        if len(parts) == 1:
            out.append(parts[0])
            continue
        files: list[str] = []
        seen: set[str] = set()
        for t in parts:
            for f in t.files:
                k = f.lower()
                if k not in seen:
                    seen.add(k)
                    files.append(f)
        body = "\n\n".join(f"### {t.id} — {t.title}\n{t.body}" for t in parts)
        body += (
            "\n\nDo these in order in this single run. Write each file at most once. "
            "After a successful write, later steps only read or make a small append — "
            "never replace the whole file. Verify last; do not write during verify."
        )
        out.append(
            PlanTask(
                id="+".join(t.id for t in parts),
                title="; ".join(t.title for t in parts),
                files=files,
                body=body,
            )
        )
    return out


def merge_all_tasks(tasks: list[PlanTask]) -> list[PlanTask]:
    """One worker for the whole plan (small plans)."""
    if len(tasks) <= 1:
        return list(tasks)
    merged = merge_overlapping_tasks(
        [
            PlanTask(
                id=t.id,
                title=t.title,
                files=[*(t.files or []), "__plan__"],
                body=t.body,
            )
            for t in tasks
        ]
    )
    for task in merged:
        task.files = [f for f in task.files if not f.startswith("__")]
    return merged


def extract_section(body: str, heading: str) -> str:
    """Return the markdown under ``## heading`` until the next ``##``."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(body or "")
    return (match.group(1) if match else "").strip()


def wrap_plan(
    body: str,
    *,
    status: str,
    target: str,
    branch: str,
    created: str,
) -> str:
    fm = [
        "---",
        f"status: {status}",
        f"target: {target}",
        f"branch: {branch}",
        f"created: {created}",
        "---",
        "",
    ]
    text = body.strip()
    if not text.startswith("#"):
        text = "# Plan\n\n" + text
    return "\n".join(fm) + text + "\n"
