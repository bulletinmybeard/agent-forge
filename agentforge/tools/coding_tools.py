"""Coding-mode tools — deterministic discovery + narrowing (Phase 2).

Real implementations for ``code_find`` / ``code_narrow`` / ``code_verify``.
``code_transform`` and ``code_apply`` are still stubs — they land in
phases 3 and 4 respectively.

Shells out to ``rg --json`` for discovery (bulletproof parsing, no regex
gymnastics on filename separators). No LLM calls in any tool in this
phase.

See ``.claude/2026-04-24-coding-mode-design.md`` for the full tool contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts" / "coding"


# ---------------------------------------------------------------------------
# Type aliases — plain dicts in Phase 2. Phase 3 introduces proper
# dataclasses once the shape stabilises.
# ---------------------------------------------------------------------------

Hit = dict  # {file, line, text, ctx_before, ctx_after}
ProposedChange = dict  # {file, before_hash, unified_diff, error?}


# ---------------------------------------------------------------------------
# Path confinement — every write must stay at-or-below cwd. Mirrors the
# _validate_root check in coding/named_ops/_sg.py. Guards both the
# planner-supplied change["file"] and the Redis-sourced snapshot path,
# which we treat as untrusted (the key can be tampered with).
# ---------------------------------------------------------------------------


def _confine_to_cwd(file_path: str) -> tuple[Path | None, str | None]:
    """Resolve ``file_path`` and assert it stays within cwd.

    Returns ``(resolved_path, None)`` on success, or ``(None, reason)`` if
    the target escapes cwd or a path component is a symlink pointing
    outside the root. ``resolve()`` follows symlinks, so a symlinked
    parent/target that lands outside cwd fails the ``relative_to`` check.
    """
    if not file_path:
        return None, "empty path"
    cwd = Path.cwd().resolve()
    try:
        resolved = Path(file_path).expanduser().resolve()
    except OSError as exc:
        return None, f"path resolve failed: {exc}"
    try:
        resolved.relative_to(cwd)
    except ValueError:
        return None, f"path {file_path!r} resolves outside project root {cwd} — refusing for safety"
    return resolved, None


# ---------------------------------------------------------------------------
# code_find — rg --json wrapper
# ---------------------------------------------------------------------------


def _strip_trailing_newline(text: str) -> str:
    """Drop the single trailing newline rg emits; leave other whitespace."""
    if text.endswith("\n"):
        return text[:-1]
    return text


def _parse_rg_ndjson(raw_output: str) -> list[Hit]:
    """Parse ``rg --json`` newline-delimited output into structured hits.

    rg streams alternating ``begin`` / ``context`` / ``match`` / ``end``
    records per file. Context lines appear before or after a match based on
    their ``line_number`` — we key lines by number within each file and
    attribute them to the nearest match on each side.

    With overlapping matches (two matches within the context window), a
    single context line can sit between them; we attribute it to both,
    which matches intuition ("show me what's around this match").
    """
    # file -> list of {"type": "match"|"context", "line": int, "text": str}
    per_file: dict[str, list[dict]] = defaultdict(list)

    for line in raw_output.splitlines():
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON rg output line: %r", line[:120])
            continue
        rtype = rec.get("type")
        if rtype not in ("match", "context"):
            continue
        data = rec.get("data") or {}
        path = (data.get("path") or {}).get("text") or ""
        lines_obj = data.get("lines") or {}
        text = lines_obj.get("text", "")
        line_no = data.get("line_number")
        if not path or line_no is None:
            continue
        per_file[path].append(
            {
                "type": rtype,
                "line": int(line_no),
                "text": _strip_trailing_newline(text),
            }
        )

    hits: list[Hit] = []
    for path, entries in per_file.items():
        # Sort by line_number so attribution windows are deterministic.
        entries.sort(key=lambda e: e["line"])
        matches = [e for e in entries if e["type"] == "match"]
        for m in matches:
            ctx_before = [(e["line"], e["text"]) for e in entries if e["type"] == "context" and e["line"] < m["line"]]
            ctx_after = [(e["line"], e["text"]) for e in entries if e["type"] == "context" and e["line"] > m["line"]]
            # Trim to the nearest `context` window on each side — an entry
            # that's many lines away from this match belongs to a different
            # match's window (overlapping matches case).
            # We don't need to know the exact ctx size here; the windows
            # were bounded by rg's -A/-B already. Just drop anything that
            # isn't contiguous with the match (gap > 1 between consecutive
            # lines means we've crossed into another match's window).
            ctx_before = _contiguous_tail(ctx_before, m["line"])
            ctx_after = _contiguous_head(ctx_after, m["line"])

            hits.append(
                {
                    "file": path,
                    "line": m["line"],
                    "text": m["text"],
                    "ctx_before": ctx_before,
                    "ctx_after": ctx_after,
                }
            )

    return hits


def _contiguous_tail(entries: list[tuple[int, str]], anchor: int) -> list[tuple[int, str]]:
    """From the END of ``entries`` walk back while lines are adjacent to ``anchor``.

    Anchor is the match line. We want entries whose line numbers form an
    unbroken descent from anchor-1, anchor-2, … Stop on the first gap.
    Returns the tail in original (ascending) order.
    """
    if not entries:
        return []
    # entries is ascending; walk from the end downwards.
    out: list[tuple[int, str]] = []
    expected = anchor - 1
    for ln, txt in reversed(entries):
        if ln == expected:
            out.append((ln, txt))
            expected -= 1
        else:
            break
    return list(reversed(out))


def _contiguous_head(entries: list[tuple[int, str]], anchor: int) -> list[tuple[int, str]]:
    """From the START of ``entries`` walk forward while adjacent to ``anchor``."""
    if not entries:
        return []
    out: list[tuple[int, str]] = []
    expected = anchor + 1
    for ln, txt in entries:
        if ln == expected:
            out.append((ln, txt))
            expected += 1
        else:
            break
    return out


def code_find(
    pattern: str,
    glob: str,
    path: str,
    context: int = 10,
) -> list[Hit]:
    """Find ``pattern`` in ``path`` restricted by ``glob``, with N lines of context.

    Returns a list of hits, each with ``ctx_before`` / ``ctx_after`` as
    ``[(line_no, text), …]``. Empty list on no matches or if ``rg`` isn't
    installed. Regex patterns supported (rg default).

    Security: args are passed as a list — no shell expansion, safe against
    injection.
    """
    # subprocess gets no shell, so a user-supplied ~ or $VAR in path never
    # expands on its own — do it here, or rg searches a literal '~' directory
    # and silently returns 0 hits (every @coding run on a ~/path no-ops).
    path = os.path.expanduser(os.path.expandvars(path))
    cmd = [
        "rg",
        "--json",
        "-A",
        str(context),
        "-B",
        str(context),
    ]
    if glob:
        cmd.extend(["--glob", glob])
    cmd.extend([pattern, path])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,  # rg exits 1 on no matches — not an error
            timeout=60,
        )
    except FileNotFoundError:
        logger.error(
            "[code_find] 'rg' (ripgrep) is not installed on this worker. "
            "Install it or route @coding to a worker that has it.",
        )
        return []
    except subprocess.TimeoutExpired:
        logger.warning("[code_find] timed out after 60s: pattern=%r path=%r", pattern, path)
        return []

    # rg exit codes: 0 = matches, 1 = no matches, 2 = error.
    if proc.returncode == 2:
        logger.warning(
            "[code_find] rg exit=2 stderr=%s",
            (proc.stderr or "").strip()[:200],
        )
        return []

    hits = _parse_rg_ndjson(proc.stdout)
    logger.info(
        "[code_find] pattern=%r glob=%r path=%r → %d hits across %d files",
        pattern,
        glob,
        path,
        len(hits),
        len({h["file"] for h in hits}),
    )
    return hits


# ---------------------------------------------------------------------------
# code_narrow — regex filter over hit.text
# ---------------------------------------------------------------------------


def code_narrow(
    hits: list[Hit],
    predicate_regex: str,
    invert: bool = False,
) -> list[Hit]:
    """Filter ``hits`` whose ``text`` matches ``predicate_regex``.

    ``invert=True`` keeps non-matches instead. Regex is compiled once per
    call; uses ``re.search`` (not ``fullmatch``) so patterns don't need
    `^…$` wrappers.

    Indentation-tolerant: rg preserves leading whitespace on each match
    line, so user-written regexes like ``^<Grid>$`` would otherwise fail
    against a real JSX line like ``      <Grid>``. We match the regex
    against ``text`` first, and also against the lstrip'd version — a
    hit is kept if EITHER matches. This makes anchor-based predicates
    work without needing the caller to prepend ``\\s*`` everywhere.

    Invalid regex → logs warning and returns hits unchanged (fail-open;
    narrowing is refining, not gating).
    """
    try:
        rx = re.compile(predicate_regex)
    except re.error as exc:
        logger.warning("[code_narrow] invalid regex %r: %s — returning input unchanged", predicate_regex, exc)
        return list(hits)

    def _hit_matches(h: Hit) -> bool:
        text = h.get("text", "")
        return bool(rx.search(text) or rx.search(text.lstrip()))

    if invert:
        out = [h for h in hits if not _hit_matches(h)]
    else:
        out = [h for h in hits if _hit_matches(h)]

    logger.info(
        "[code_narrow] predicate=%r invert=%s in=%d out=%d",
        predicate_regex,
        invert,
        len(hits),
        len(out),
    )
    return out


# ---------------------------------------------------------------------------
# code_transform — stubbed through Phase 3
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# code_transform — per-file LLM burst (Phase 3)
# ---------------------------------------------------------------------------


_DIFF_BLOCK_RE = re.compile(r"```diff\s*\n(.*?)```", re.DOTALL)


def _load_transform_prompt() -> str:
    """Read the per-file transform system prompt from disk."""
    return (_PROMPT_DIR / "transform.md").read_text(encoding="utf-8")


def _extract_unified_diff(response: str) -> str:
    """Pull the unified diff out of a fenced ```diff …``` block.

    The prompt instructs the model to emit nothing outside that block, but
    real models drift. We extract the first fenced block and return its
    contents (possibly empty — meaning the model decided no change applies).
    """
    m = _DIFF_BLOCK_RE.search(response)
    if not m:
        return ""
    return m.group(1).strip("\n")


def _normalise_hit_texts(hits: list[Hit]) -> list[str]:
    """Return lstrip/rstrip hit texts, dropping blanks."""
    out: list[str] = []
    for h in hits:
        t = (h.get("text") or "").lstrip().rstrip()
        if t:
            out.append(t)
    return out


def _hunk_deletes_touch_hits(
    hunk_body: str,
    norm_hits: list[str],
) -> bool:
    """True if any ``-`` line in this hunk body overlaps with a hit text.

    Pure-insertion hunks (no ``-`` lines) always pass — they legitimately
    add nearby code without claiming to edit a matched site.

    Overlap rule (either direction): a normalised hit text is a substring
    of some delete line, or vice versa. Either direction handles the
    common cases where the hit was a broader match-line and the model
    emitted just the tight pivot (or vice versa).
    """
    deletes: list[str] = []
    for line in hunk_body.splitlines():
        if not line or line.startswith("---"):
            continue
        if line.startswith("-"):
            text = line[1:].lstrip().rstrip()
            if text:
                deletes.append(text)
    if not deletes:
        return True
    if not norm_hits:
        # No hit text to compare against — can't rule the hunk out.
        return True
    for hit in norm_hits:
        for d in deletes:
            if hit in d or d in hit:
                return True
    return False


def _filter_hunks_by_hit_overlap(
    diff: str,
    hits: list[Hit],
) -> tuple[str, int]:
    """Drop hunks whose delete lines don't mention any hit.

    Returns ``(filtered_diff, dropped_count)``. Catches per-hunk
    hallucinations (model legitimately edits the `<Grid>` sites but
    also silently mutates unrelated `<Divider>` components in the same
    diff) without throwing away the whole file's work. When every
    hunk survives, the original diff shape is returned unchanged.
    """
    norm_hits = _normalise_hit_texts(hits)
    if not diff:
        return diff, 0

    lines = diff.splitlines(keepends=False)

    # Collect the file-header block (lines before the first `@@`) and a
    # list of (header_line, body_lines) per hunk.
    header_block: list[str] = []
    hunks: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_body: list[str] = []
    for line in lines:
        if line.startswith("@@"):
            if current_header is not None:
                hunks.append((current_header, current_body))
            current_header = line
            current_body = []
        else:
            if current_header is None:
                header_block.append(line)
            else:
                current_body.append(line)
    if current_header is not None:
        hunks.append((current_header, current_body))

    if not hunks:
        return diff, 0

    kept: list[tuple[str, list[str]]] = []
    dropped = 0
    for header, body in hunks:
        body_text = "\n".join(body)
        if _hunk_deletes_touch_hits(body_text, norm_hits):
            kept.append((header, body))
        else:
            dropped += 1

    if dropped == 0:
        return diff, 0

    out_lines: list[str] = list(header_block)
    for header, body in kept:
        out_lines.append(header)
        out_lines.extend(body)
    return "\n".join(out_lines), dropped


def _format_file_windows(file_path: str, hits_in_file: list[Hit]) -> str:
    """Build the user-message body for a single file burst.

    Concatenates every matched site's context-before / matched-line /
    context-after as numbered lines. The edit-target line number is
    announced in the site header — NOT inline on the line itself —
    so the model never sees annotation text next to the file content.
    (Earlier versions appended ``<-- MATCH`` after the matched line's
    text; models copied that marker into their diff output, making
    the delete line unfindable against the real file.)
    """
    lines: list[str] = [f"File: {file_path}", ""]
    for idx, h in enumerate(hits_in_file, start=1):
        lines.append(f"--- Site {idx}: edit line {h['line']} ---")
        for ln, txt in h.get("ctx_before", []):
            lines.append(f"{ln:>5}  {txt}")
        lines.append(f"{h['line']:>5}  {h['text']}")
        for ln, txt in h.get("ctx_after", []):
            lines.append(f"{ln:>5}  {txt}")
        lines.append("")
    return "\n".join(lines)


def _hash_file(path: str) -> str:
    """Return a short content hash for the file at ``path``.

    Written before dispatch so Phase 4's ``code_apply`` can reject writes
    when the file has changed under us between preview and apply.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:16]


def _split_for_fallback(
    hits_in_file: list[Hit],
    max_sites: int,
    max_ctx_lines: int,
) -> list[list[Hit]]:
    """Chunk a file's hits when a single burst call would be too big.

    Splits when either the site count or the total context-line count
    exceeds the thresholds. Keeps contiguous site order so the model
    sees the file in linear order across chunks.
    """
    if max_sites <= 0 and max_ctx_lines <= 0:
        return [list(hits_in_file)]

    chunks: list[list[Hit]] = []
    current: list[Hit] = []
    current_ctx = 0

    def _ctx_count(h: Hit) -> int:
        return len(h.get("ctx_before", [])) + len(h.get("ctx_after", [])) + 1

    for h in hits_in_file:
        hc = _ctx_count(h)
        too_many_sites = max_sites > 0 and len(current) >= max_sites
        too_much_ctx = max_ctx_lines > 0 and current_ctx + hc > max_ctx_lines
        if current and (too_many_sites or too_much_ctx):
            chunks.append(current)
            current, current_ctx = [], 0
        current.append(h)
        current_ctx += hc
    if current:
        chunks.append(current)
    return chunks


def _build_burst_messages(system_prompt: str, instruction: str):
    """Return a builder that turns a (file, hits) tuple into LLM messages."""

    def _build(item: tuple[str, list[Hit]]):
        file_path, hits_in_file = item
        user = f"Instruction: {instruction.strip()}\n\n{_format_file_windows(file_path, hits_in_file)}"
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ]

    return _build


def _group_hits_by_file(hits: list[Hit]) -> dict[str, list[Hit]]:
    """Group hits by their ``file`` field, preserving source order within each group."""
    grouped: dict[str, list[Hit]] = defaultdict(list)
    for h in hits:
        grouped[h.get("file", "")].append(h)
    for f in grouped:
        grouped[f].sort(key=lambda h: h.get("line", 0))
    return dict(grouped)


def _file_progress_adapter(on_file_progress: Any):
    """Wrap a (kind, file, index, total, error?) caller callback into the
    ``on_item(kind, index=, total=, item=, result=)`` shape that
    ``run_burst_async`` expects.

    ``on_file_progress`` is called with positional ``(kind, file, index,
    total)`` plus a keyword ``error`` on failure. Returns ``None`` when
    the caller didn't provide a hook — run_burst_async treats None as
    "no-op".
    """
    if on_file_progress is None:
        return None

    def _adapter(kind: str, *, index: int, total: int, item=None, result=None, **_):
        # ``item`` is a (file_path, chunk) tuple from the coding burst.
        file_path = item[0] if isinstance(item, tuple) and item else ""
        err = getattr(result, "error", None) if result is not None else None
        try:
            on_file_progress(kind, file_path, index, total, err)
        except Exception:
            logger.debug(
                "[coding_tools] on_file_progress adapter raised",
                exc_info=True,
            )

    return _adapter


def code_transform(
    hits: list[Hit],
    instruction: str,
    profile: str = "coding",
    *,
    max_sites_per_file: int | None = None,
    max_ctx_lines_per_chunk: int = 300,
    max_files_per_burst: int | None = None,
    client_factory: Any = None,
    temperature: float | None = None,
    on_file_progress: Any = None,
) -> list[ProposedChange]:
    """Per-file LLM burst — produces a unified diff for each affected file.

    Groups ``hits`` by file, then fans out one LLM call per file-group via
    ``agentforge.coding.burst.run_burst`` with the provider-cap concurrency
    knob. Files with too many sites or too much total context are split
    into chunks (thresholds from the ``coding:`` config section or the
    ``max_sites_per_file`` / ``max_ctx_lines_per_chunk`` kwargs).

    Returns ``[{file, before_hash, unified_diff, error?}, …]`` — one entry
    per file-chunk processed. When a chunk fails (LLM error, unparseable
    response), the ``error`` key carries the reason and ``unified_diff``
    is the empty string.

    ``client_factory`` + ``temperature`` are passed through to the burst
    helper so tests can inject a stub without patching globals.
    """
    if not hits:
        return []

    # Read thresholds from config when not overridden.
    if max_sites_per_file is None:
        try:
            from agentforge.config import get_config

            cfg = get_config()
            max_sites_per_file = int(cfg.get("coding.max_sites_per_file", 20) or 20)
            if max_files_per_burst is None:
                max_files_per_burst = int(cfg.get("coding.max_files_per_burst", 50) or 50)
        except Exception as exc:
            logger.debug("[code_transform] config lookup failed: %s", exc)
            max_sites_per_file = max_sites_per_file or 20
            max_files_per_burst = max_files_per_burst or 50

    grouped = _group_hits_by_file(hits)
    items: list[tuple[str, list[Hit]]] = []
    for file_path, file_hits in grouped.items():
        for chunk in _split_for_fallback(
            file_hits,
            max_sites=max_sites_per_file,
            max_ctx_lines=max_ctx_lines_per_chunk,
        ):
            items.append((file_path, chunk))

    if not items:
        return []

    system_prompt = _load_transform_prompt()
    build = _build_burst_messages(system_prompt, instruction)

    # burst module imports AIClient lazily — keep it that way by deferring
    # the import here so cheap tool-only test paths don't pull the SDKs.
    from agentforge.coding.burst import run_burst_async

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Re-enter path: the runner is already inside an event loop.
            # Start the burst on the SAME loop using a threadsafe schedule
            # so we don't nest asyncio.run(). Callers that care about
            # async semantics should use run_burst_async directly.
            future = asyncio.run_coroutine_threadsafe(
                run_burst_async(
                    items,
                    build,
                    profile=profile,
                    max_files_per_burst=max_files_per_burst,
                    client_factory=client_factory,
                    temperature=temperature,
                    on_item=_file_progress_adapter(on_file_progress),
                ),
                loop,
            )
            results = future.result()
        else:
            results = loop.run_until_complete(
                run_burst_async(
                    items,
                    build,
                    profile=profile,
                    max_files_per_burst=max_files_per_burst,
                    client_factory=client_factory,
                    temperature=temperature,
                    on_item=_file_progress_adapter(on_file_progress),
                )
            )
    except RuntimeError:
        # No event loop available on this thread — create a throwaway
        # one so we don't close the shared default loop (asyncio.run
        # does that as a side effect and breaks subsequent callers in
        # the same process).
        _new_loop = asyncio.new_event_loop()
        try:
            results = _new_loop.run_until_complete(
                run_burst_async(
                    items,
                    build,
                    profile=profile,
                    max_files_per_burst=max_files_per_burst,
                    client_factory=client_factory,
                    temperature=temperature,
                    on_item=_file_progress_adapter(on_file_progress),
                )
            )
        finally:
            _new_loop.close()

    proposed: list[ProposedChange] = []
    for (file_path, chunk), res in zip(items, results):
        before_hash = _hash_file(file_path)
        if res.error:
            proposed.append(
                {
                    "file": file_path,
                    "before_hash": before_hash,
                    "unified_diff": "",
                    "error": res.error,
                }
            )
            continue
        diff = _extract_unified_diff(res.content)
        if not diff:
            proposed.append(
                {
                    "file": file_path,
                    "before_hash": before_hash,
                    "unified_diff": "",
                    "error": "empty or unparseable diff block in model response",
                }
            )
            continue
        # Hunk-level sanity filter: models sometimes emit a diff that
        # mixes legitimate edits with hallucinated ones (e.g., the Grid
        # hunks the user asked for PLUS unrelated `<Divider>` tweaks).
        # Drop any hunk whose delete lines don't overlap with the
        # searched-for hit texts; keep the rest.
        diff, dropped = _filter_hunks_by_hit_overlap(diff, chunk)
        if dropped:
            logger.info(
                "[code_transform] dropped %d hallucinated hunk(s) from %s (delete lines didn't reference any site hit)",
                dropped,
                file_path,
            )
        if not diff.strip() or not any(line.startswith("@@") for line in diff.splitlines()):
            proposed.append(
                {
                    "file": file_path,
                    "before_hash": before_hash,
                    "unified_diff": "",
                    "error": (
                        "diff rejected: every hunk edited code that wasn't a "
                        "matched site — model likely hallucinated the edit target"
                    ),
                }
            )
            continue
        proposed.append(
            {
                "file": file_path,
                "before_hash": before_hash,
                "unified_diff": diff,
            }
        )

    logger.info(
        "[code_transform] %d chunks → %d proposed (%d errors), profile=%s",
        len(items),
        len(proposed),
        sum(1 for p in proposed if p.get("error")),
        profile,
    )
    return proposed


# ---------------------------------------------------------------------------
# code_verify — deterministic re-check via code_find
# ---------------------------------------------------------------------------


def code_verify(
    proposed: list[ProposedChange],
    reverify_pattern: str | None,
    reverify_path: str,
    reverify_glob: str = "",
) -> dict:
    """Re-run ``code_find`` to confirm the target pattern is gone.

    Intended to run AFTER ``code_apply`` has written changes to disk — any
    surviving matches mean the transform missed a site. Before apply, it's
    still a useful sanity check that the new files haven't grown new
    matches of the target pattern.

    When ``reverify_pattern`` is ``None`` we skip the check and return
    ``{"ok": True, "surviving_sites": []}`` — some transforms don't have a
    reusable "before" pattern (e.g., extract-to-function).

    Scope: when ``proposed`` lists the files that were touched, we scan
    ONLY those files. This avoids a full-repo rg (including node_modules,
    build artefacts, .git) when the planner didn't pass a ``reverify_glob``
    to scope the broad scan. When ``proposed`` is empty we fall back to
    the broader ``code_find`` against ``reverify_path`` + ``reverify_glob``.
    """
    if reverify_pattern is None:
        logger.info("[code_verify] no reverify_pattern — skipping check")
        return {"ok": True, "surviving_sites": []}

    reverify_path = os.path.expanduser(os.path.expandvars(reverify_path))
    proposed_files = [p["file"] for p in proposed if p.get("file")]

    surviving: list[Hit]
    if proposed_files:
        # Re-check each touched file individually. One rg subprocess per
        # file (~10-20 ms on a single file) is faster than one broad
        # scan of an unbounded tree, AND honours the "only check what we
        # might have broken" semantic.
        surviving = []
        for file_path in proposed_files:
            surviving.extend(
                code_find(
                    pattern=reverify_pattern,
                    glob="",  # path IS a file; glob filtering is a no-op
                    path=file_path,
                    context=0,
                )
            )
        logger.info(
            "[code_verify] scoped re-check: pattern=%r files=%d → surviving=%d",
            reverify_pattern,
            len(proposed_files),
            len(surviving),
        )
    else:
        # No proposed files — fall back to the broader scan. In practice
        # this branch only fires in tests or edge cases; a normal run
        # that reaches verify always has proposed changes to check.
        surviving = code_find(
            pattern=reverify_pattern,
            glob=reverify_glob,
            path=reverify_path,
            context=0,
        )
        logger.info(
            "[code_verify] broad re-check: pattern=%r path=%r → surviving=%d",
            reverify_pattern,
            reverify_path,
            len(surviving),
        )

    # Belt-and-suspenders filter: even in the scoped path, reject any
    # survivor whose file somehow isn't in the proposed set (defensive
    # against rg paths that don't realpath-normalise the same way).
    proposed_real = {os.path.realpath(p["file"]) for p in proposed if p.get("file")}
    if proposed_real:
        surviving = [h for h in surviving if os.path.realpath(h["file"]) in proposed_real]

    ok = len(surviving) == 0
    return {"ok": ok, "surviving_sites": surviving}


# ---------------------------------------------------------------------------
# code_apply — stubbed through Phase 4
# ---------------------------------------------------------------------------


def code_apply(
    proposed: list[ProposedChange],
    burst_id: str,
    session_id: str,
    *,
    ttl_seconds: int | None = None,
    _snapshot_save: Any = None,  # test seam
    _rollback_store: Any = None,  # test seam
) -> dict:
    """Apply proposed unified diffs, snapshot each file, register undo map.

    For each change with a non-empty ``unified_diff``:

    1. Read the current file content.
    2. Sanity-check against ``before_hash`` — skip if the file has changed
       under us since the dry-run (avoids clobbering concurrent edits).
    3. Save a snapshot via the existing ``_file_snapshots`` store.
    4. Apply the diff in memory.
    5. Write the patched content back.
    6. Collect the snapshot ID (== pre_hash) for the burst registry.

    After all files are processed, the snapshot IDs are RPUSH'd into
    Redis under ``coding:burst:{session_id}:{burst_id}`` with a
    per-config TTL so ``@coding undo <burst_id>`` can walk the set.
    """
    from agentforge.coding.patch import PatchError, apply_unified_diff_tolerant
    from agentforge.tools._file_snapshots import save_snapshot

    if _snapshot_save is None:
        _snapshot_save = save_snapshot

    if ttl_seconds is None:
        try:
            from agentforge.config import get_config

            ttl_seconds = int(get_config().get("coding.snapshot_ttl_seconds", 86400) or 86400)
        except Exception:
            ttl_seconds = 86400

    applied: list[dict] = []
    failed: list[dict] = []
    snapshot_ids: list[str] = []

    # Group by file: ``code_transform`` can split a dense file into
    # multiple chunks, each producing its own ProposedChange with the
    # same before_hash. We must read / hash / snapshot / write ONCE per
    # file; otherwise chunk #1's write mutates the file and chunk #2's
    # hash check fails with a spurious "file changed since preview"
    # error.
    grouped: dict[str, list[ProposedChange]] = {}
    order: list[str] = []
    upstream_errors: list[tuple[str, str]] = []  # (file_path, reason)
    for change in proposed:
        file_path = change.get("file", "")
        if change.get("error") or not change.get("unified_diff") or not file_path:
            upstream_errors.append(
                (
                    file_path,
                    change.get("error") or "no diff to apply",
                )
            )
            continue
        if file_path not in grouped:
            order.append(file_path)
            grouped[file_path] = []
        grouped[file_path].append(change)

    for file_path, ref_err in upstream_errors:
        failed.append({"file": file_path, "reason": ref_err})

    for file_path in order:
        changes = grouped[file_path]

        p, confine_err = _confine_to_cwd(file_path)
        if confine_err or p is None:
            failed.append({"file": file_path, "reason": confine_err or "path confinement failed"})
            continue

        try:
            original = p.read_text(encoding="utf-8")
        except OSError as exc:
            failed.append({"file": file_path, "reason": f"read failed: {exc}"})
            continue

        current_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()

        # Drift check — the FIRST chunk's before_hash is what the LLM
        # saw; every chunk from the same burst will carry the same one
        # (code_transform hashes before dispatching any burst), so any
        # chunk is fine to use.
        expected = changes[0].get("before_hash") or ""
        if expected and not current_hash.startswith(expected):
            failed.append(
                {
                    "file": file_path,
                    "reason": (
                        f"file changed since preview: expected hash prefix {expected!r}, got {current_hash[:16]!r}"
                    ),
                }
            )
            continue

        try:
            snapshot_saved = _snapshot_save(
                pre_hash=current_hash,
                path=str(p.resolve()),
                content=original,
                tool="code_apply",
                session_id=session_id,
            )
        except Exception as exc:
            failed.append({"file": file_path, "reason": f"snapshot failed: {exc}"})
            continue

        # Concatenate every chunk's diff into a single unified diff.
        # apply_unified_diff_tolerant walks hunks in source order with
        # offset tracking, so cumulative application is safe — provided
        # chunks don't reorder hunks, which _split_for_fallback never does.
        combined_diff = "\n".join(c["unified_diff"].rstrip("\n") for c in changes)

        try:
            new_content, skipped_hunks = apply_unified_diff_tolerant(
                original,
                combined_diff,
            )
        except PatchError as exc:
            failed.append({"file": file_path, "reason": f"patch failed: {exc}"})
            continue

        if new_content == original:
            reason = skipped_hunks[0]["reason"] if skipped_hunks else "no hunks applied"
            failed.append({"file": file_path, "reason": f"patch failed: {reason}"})
            continue

        try:
            p.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            failed.append({"file": file_path, "reason": f"write failed: {exc}"})
            continue

        post_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
        applied.append(
            {
                "file": file_path,
                "pre_hash": current_hash,
                "post_hash": post_hash,
                "snapshot_id": current_hash,
                "snapshot_saved": bool(snapshot_saved),
                # Per-hunk partial-apply info across ALL chunks for this file.
                "skipped_hunks": skipped_hunks,
                # Combined diff so downstream renderers (result card, UI)
                # can show one "written" event per file instead of per chunk.
                "combined_diff": combined_diff,
                "chunk_count": len(changes),
            }
        )
        if snapshot_saved:
            snapshot_ids.append(current_hash)

    # Register the burst for undo. Only runs when we actually wrote
    # something — an all-failed batch doesn't need a rollback entry.
    if snapshot_ids:
        try:
            from agentforge.coding.rollback import get_rollback_store

            store = _rollback_store or get_rollback_store()
            store.store_burst(
                session_id=session_id,
                burst_id=burst_id,
                snapshot_ids=snapshot_ids,
                ttl_seconds=ttl_seconds,
            )
        except Exception as exc:
            # Don't fail the whole apply if only the Redis bookkeeping
            # stumbled — the files are already written, and a missing
            # burst entry just means undo won't work for this run.
            logger.warning("[code_apply] rollback-store write failed: %s", exc)

    logger.info(
        "[code_apply] burst=%s session=%s proposed_in=%d applied=%d failed=%d",
        burst_id,
        session_id,
        len(proposed),
        len(applied),
        len(failed),
    )
    return {"applied": applied, "failed": failed, "burst_id": burst_id}


def code_undo(
    session_id: str,
    burst_id: str,
    *,
    _snapshot_load: Any = None,
    _rollback_store: Any = None,
) -> dict:
    """Revert every file touched by a prior ``code_apply`` for this burst.

    Walks the Redis-stored snapshot ID list and restores each snapshot's
    original content. Idempotent — a second call is a no-op (the burst
    entry is deleted after the first successful undo).
    """
    from agentforge.coding.rollback import get_rollback_store
    from agentforge.tools._file_snapshots import load_snapshot

    if _snapshot_load is None:
        _snapshot_load = load_snapshot

    store = _rollback_store or get_rollback_store()

    snapshot_ids = store.load_burst(session_id, burst_id)

    if not snapshot_ids:
        return {
            "reverted": [],
            "failed": [],
            "note": f"no burst registered for {burst_id!r} (expired or never applied)",
        }

    reverted: list[dict] = []
    failed: list[dict] = []

    for sid in snapshot_ids:
        env = _snapshot_load(sid)
        if not env:
            failed.append({"snapshot_id": sid, "reason": "snapshot not found"})
            continue
        path = env.get("path")
        content = env.get("content", "")
        if not path:
            failed.append({"snapshot_id": sid, "reason": "snapshot has no path"})
            continue
        # Redis snapshot path is untrusted — the burst key could be tampered
        # with to point a restore write outside the repo.
        target, confine_err = _confine_to_cwd(path)
        if confine_err or target is None:
            failed.append({"snapshot_id": sid, "reason": confine_err or "path confinement failed"})
            continue
        try:
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            failed.append({"snapshot_id": sid, "reason": f"write failed: {exc}"})
            continue
        reverted.append({"snapshot_id": sid, "path": path})

    store.delete_burst(session_id, burst_id)

    logger.info(
        "[code_undo] session=%s burst=%s reverted=%d failed=%d",
        session_id,
        burst_id,
        len(reverted),
        len(failed),
    )
    return {"reverted": reverted, "failed": failed}


__all__: list[Any] = [
    "Hit",
    "ProposedChange",
    "code_find",
    "code_narrow",
    "code_transform",
    "code_verify",
    "code_apply",
    "code_undo",
]
