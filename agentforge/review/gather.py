"""Deterministic git + file gather for @review. No LLM."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReviewGather:
    branch: str
    status: str
    log: str
    changed_files: list[str]
    diff: str
    file_contents: dict[str, str]
    preamble: str = ""
    skipped_files: list[tuple[str, str]] = field(default_factory=list)

    def render_preamble(self, target: str, instruction: str) -> str:
        skipped_block = ""
        if self.skipped_files:
            lines = "\n".join(f"- `{path}` ({reason})" for path, reason in self.skipped_files)
            skipped_block = f"\n### Skipped file contents\n{lines}\n"

        content_parts: list[str] = []
        for path, text in self.file_contents.items():
            content_parts.append(f"#### `{path}`\n```\n{text}\n```")
        contents_block = "\n\n".join(content_parts) if content_parts else "(none inlined — use read_file)"

        return f"""## Review Target

Target directory: `{target or "(current working directory)"}`
Current branch: `{self.branch}`
User instruction: {instruction}

## Pre-gathered Git Context (do NOT re-run these — results already below)

### git status
```
{self.status}
```

### Changed files
```
{chr(10).join(self.changed_files) or "(none)"}
```

### Unpushed commits
```
{self.log}
```

### Diff
```
{self.diff or "(empty)"}
```
{skipped_block}
### Changed file contents
{contents_block}

## Your Task

Focus your review on the changed files listed above. Use `read_file`, `grep_text`, and `git_diff` (or `git_blame`) for anything not inlined. Do NOT re-run `git status` or `git branch` — the output is already provided above.
"""


def gather_review_context(
    target: str,
    *,
    instruction: str = "Review all current and unpushed changes",
    max_bytes: int = 400_000,
) -> ReviewGather:
    cwd = target if target and os.path.isdir(target) else None
    branch = _git("git branch --show-current", cwd)
    status = _git("git status", cwd)
    log = _git("git log @{u}..HEAD --oneline 2>/dev/null || git log -10 --oneline", cwd)
    name_out = _git(
        "git diff --name-only @{u}..HEAD 2>/dev/null || git diff --name-only HEAD 2>/dev/null || git status --short",
        cwd,
    )
    untracked = _git("git ls-files --others --exclude-standard", cwd)
    diff = _git("git diff @{u}..HEAD 2>/dev/null || true", cwd)
    wt_diff = _git("git diff HEAD 2>/dev/null || git diff 2>/dev/null || true", cwd)
    if wt_diff and wt_diff not in (diff, "(empty)"):
        diff = (diff + "\n" + wt_diff).strip() if diff and diff != "(empty)" else wt_diff

    changed_files = _parse_changed_files(name_out, untracked)

    file_contents: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    used = 0
    root = Path(cwd) if cwd else Path.cwd()
    for rel in changed_files:
        path = root / rel
        if not path.is_file():
            continue
        reason = _skip_reason(path, max_bytes=max_bytes, used=used)
        if reason:
            skipped.append((rel, reason))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        used += len(text.encode("utf-8"))
        file_contents[rel] = text

    if len(diff.encode("utf-8")) > max_bytes:
        diff = diff.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace") + "\n... [diff truncated]"

    gathered = ReviewGather(
        branch=branch,
        status=status,
        log=log,
        changed_files=changed_files,
        diff=diff if diff != "(empty)" else "",
        file_contents=file_contents,
        skipped_files=skipped,
    )
    gathered.preamble = gathered.render_preamble(target, instruction)
    return gathered


def _git(cmd: str, cwd: str | None) -> str:
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        return (out + ("\n" + err if err else "")).strip() or "(empty)"
    except Exception as exc:
        return f"(could not run: {exc})"


def _parse_name_list(raw: str) -> list[str]:
    if not raw or raw in ("(empty)",) or raw.startswith("(could not run:"):
        return []
    names: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # `git status --short` → " M path" / "?? path"
        if len(line) >= 3 and line[0] in " MADRCU?!" and line[1] in " MADRCU?":
            line = line[2:].strip()
            if " -> " in line:
                line = line.split(" -> ", 1)[1]
        names.append(line.strip().strip('"'))
    return names


def _parse_changed_files(name_out: str, untracked: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in _parse_name_list(name_out) + _parse_name_list(untracked):
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _skip_reason(path: Path, *, max_bytes: int, used: int) -> str | None:
    try:
        size = path.stat().st_size
    except OSError:
        return "unreadable"
    if size == 0:
        return None
    head = path.read_bytes()[:8192]
    if b"\x00" in head:
        return "binary"
    remaining = max_bytes - used
    if size > remaining:
        return f"over gather cap ({size} bytes)"
    return None
