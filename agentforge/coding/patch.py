"""Unified-diff applier for ``@coding`` mode — content-anchored + fuzzy.

Parses the diff shape ``code_transform`` emits and applies it to an original file in memory.
Uses **content anchoring**: each hunk's context+delete lines are looked up in the current file by content,
with the diff's line number as a sorting hint. This makes the applier robust against LLM-generated diffs
whose line numbers are wrong but whose context/edit payloads are correct (the common failure mode).

Drift detection still works — if the anchor content doesn't appear anywhere in the file,
``PatchError`` is raised. Only the "line numbers are off by N" failure mode is now papered over.

Not a replacement for ``patch(1)``. Known limitations (acceptable in v1):

- ``\\ No newline at end of file`` markers on the diff side are consumed and otherwise ignored — we always preserve the original file's final newline (or lack thereof).
- Binary files and renames aren't supported; diffs touching them fall through and raise on the first missing hunk header.
- When an anchor appears MULTIPLE times in the file, we pick the occurrence closest to the hint line — correct for the typical LLM failure mode, wrong if the model intentionally hunks an identical block further down. Acceptable trade-off for v1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)


_HUNK_HEADER_RE = re.compile(r"^@@ -(?P<o>\d+)(?:,(?P<oc>\d+))? \+(?P<n>\d+)(?:,(?P<nc>\d+))? @@")

# Defensive strip: some models have copied site-window annotations into
# the diff body, e.g., ``<Grid>  <-- MATCH``. The site-window format no
# longer uses that marker, but the safety net stays in case another
# model or a tweak to the format reintroduces the same failure mode.
_ANNOTATION_STRIP_RE = re.compile(r"\s*<--\s*MATCH\s*$")


def _scrub_annotations(text: str) -> str:
    """Strip trailing site-window annotations a model may have leaked."""
    return _ANNOTATION_STRIP_RE.sub("", text)


class PatchError(Exception):
    """Raised when a unified diff doesn't apply cleanly to the original content."""


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


@dataclass
class _HunkOp:
    op: str  # ' ', '-', '+'
    text: str


@dataclass
class _Hunk:
    orig_start: int
    orig_count: int
    new_start: int
    new_count: int
    ops: list[_HunkOp] = field(default_factory=list)


def _parse_hunks(diff: str) -> list[_Hunk]:
    """Parse a unified diff string into a list of hunks.

    Ignores file headers (``---`` / ``+++``) since code_apply routes each
    diff to a specific pre-known file. Everything after the first
    ``@@`` line is body until the next ``@@`` or end-of-diff.
    """
    hunks: list[_Hunk] = []
    current: _Hunk | None = None
    for line in diff.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if current is not None:
                hunks.append(current)
            current = _Hunk(
                orig_start=int(m.group("o")),
                orig_count=int(m.group("oc") or 1),
                new_start=int(m.group("n")),
                new_count=int(m.group("nc") or 1),
            )
            continue
        if current is None:
            # Garbage before the first hunk — ignore.
            continue
        if line.startswith("\\"):
            # "\ No newline at end of file" — absorb, don't propagate.
            continue
        if not line:
            # Blank line between hunks? Treat as context (an empty context line).
            current.ops.append(_HunkOp(op=" ", text=""))
            continue
        op = line[0]
        if op not in (" ", "-", "+"):
            raise PatchError(f"unexpected op {op!r} in diff line: {line!r}")
        current.ops.append(_HunkOp(op=op, text=_scrub_annotations(line[1:])))
    if current is not None:
        hunks.append(current)
    return hunks


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _find_anchor(
    lines: list[str],
    anchor: list[str],
    hint: int,
) -> int | None:
    """Find ``anchor`` as a contiguous subsequence of ``lines``.

    Returns the 0-indexed position closest to ``hint`` where it matches,
    or None when the anchor doesn't appear in the file at all.

    Pure-insertion hunks have an empty anchor — we anchor those at
    ``hint`` directly (clamped to the bounds of the file).
    """
    n = len(lines)
    alen = len(anchor)
    if alen == 0:
        return max(0, min(hint, n))
    positions = [p for p in range(n - alen + 1) if lines[p : p + alen] == anchor]
    if not positions:
        return None
    # Pick the occurrence closest to the hint — matches "line numbers are
    # off by N" failure mode. Ties broken by smaller position.
    positions.sort(key=lambda p: (abs(p - hint), p))
    return positions[0]


def apply_unified_diff(original: str, diff: str) -> str:
    """Strict variant: raise ``PatchError`` on any hunk failure.

    See ``apply_unified_diff_tolerant`` for the full matching strategy.
    This wrapper preserves the historical all-or-nothing contract for
    callers that prefer to fail fast (e.g., test fixtures).
    """
    text, failed = apply_unified_diff_tolerant(original, diff)
    if failed:
        # Keep the legacy message shape so existing `match="anchor not
        # found"` assertions still pass.
        raise PatchError(failed[0]["reason"])
    return text


def apply_unified_diff_tolerant(
    original: str,
    diff: str,
) -> tuple[str, list[dict]]:
    """Apply ``diff`` to ``original`` tolerantly.

    Returns ``(text, failed_hunks)`` where ``failed_hunks`` is a list of
    ``{"hunk_idx", "orig_start", "reason"}`` dicts for hunks whose
    anchors couldn't be located. Hunks that DO match are applied; bad
    ones are skipped. The caller can inspect ``failed_hunks`` and
    decide whether to write the partial result or discard.

    Raises ``PatchError`` only on STRUCTURAL failures (empty diff,
    unparseable ops) — never on anchor-not-found.

    Matching uses a four-stage fallback to tolerate LLM drift:

    1. **Full anchor** — context + delete lines matched exactly. Most
       hunks land here.
    2. **Shrunken anchor** — peel outer context lines one at a time
       (leading edge, trailing edge, and both) until a strict
       sub-anchor matches. Handles drift on one or two outer context
       lines; inner context stays strict.
    3. **Per-block split** — a hunk with multiple delete/add blocks
       separated by context (e.g., three `<Grid>` edits in one
       conditional) is broken into N mini-patches, each applied with
       its own hint. Handles inner-context drift. Atomic: either all
       sub-blocks land or we fall through.
    4. **Delete-only anchor** — last resort: retry with just the `-`
       lines. Handles the case where even the split anchors miss but
       the raw delete lines exist somewhere.

    Each stage tries exact matching first, then lstripped matching.
    The file's indentation is preserved on the replacement via
    ``_reindent``.
    """
    hunks = _parse_hunks(diff)
    if not hunks:
        raise PatchError("no hunks found in diff")

    has_trailing_newline = original.endswith("\n")
    lines = original.splitlines()

    working = list(lines)
    offset = 0
    failed_hunks: list[dict] = []

    for hunk_idx, hunk in enumerate(hunks):
        delete_only = [op.text for op in hunk.ops if op.op == "-"]
        add_only = [op.text for op in hunk.ops if op.op == "+"]
        hint = max(0, hunk.orig_start - 1 + offset)

        applied = False

        # -- Stages 1 + 2: full anchor, then shrunken variants ---------
        for anchor, replacement in _anchor_variants(hunk.ops):
            pos, indent_shift = _locate(working, anchor, hint)
            if pos is not None:
                new_segment = _reindent(replacement, indent_shift)
                working[pos : pos + len(anchor)] = new_segment
                offset += len(new_segment) - len(anchor)
                applied = True
                break

        if applied:
            continue

        # -- Stage 3: per-block split (atomic) -------------------------
        split_result = _try_split_apply(working, hunk, offset)
        if split_result is not None:
            working, offset = split_result
            continue

        # -- Stage 4: delete-only anchor -------------------------------
        if delete_only:
            pos, indent_shift = _locate(working, delete_only, hint)
            if pos is not None:
                new_segment = _reindent(add_only, indent_shift)
                working[pos : pos + len(delete_only)] = new_segment
                offset += len(new_segment) - len(delete_only)
                continue

        # -- Skip this hunk — other hunks may still apply --------------
        failed_hunks.append(
            {
                "hunk_idx": hunk_idx,
                "orig_start": hunk.orig_start,
                "reason": _anchor_not_found_message(hunk_idx, hunk, delete_only),
            }
        )

    result = "\n".join(working)
    if has_trailing_newline:
        result += "\n"
    return result, failed_hunks


def _split_into_blocks(
    hunk: _Hunk,
) -> list[tuple[list[str], list[str], int]]:
    """Split a hunk into contiguous (deletes, adds, rel_orig_offset) blocks.

    A "block" is a maximal contiguous run of ``-``/``+`` ops. Context
    lines delimit blocks. ``rel_orig_offset`` is the number of original-
    file lines consumed before the block starts (so the caller can bias
    each block's hint position).
    """
    blocks: list[tuple[list[str], list[str], int]] = []
    current_d: list[str] = []
    current_a: list[str] = []
    orig_consumed = 0
    in_block = False
    block_start = 0

    for op in hunk.ops:
        if op.op in ("-", "+"):
            if not in_block:
                block_start = orig_consumed
                in_block = True
            if op.op == "-":
                current_d.append(op.text)
                orig_consumed += 1
            else:
                current_a.append(op.text)
        else:  # " " (context)
            if in_block:
                blocks.append((current_d, current_a, block_start))
                current_d, current_a = [], []
                in_block = False
            orig_consumed += 1

    if in_block and (current_d or current_a):
        blocks.append((current_d, current_a, block_start))
    return blocks


def _try_split_apply(
    working: list[str],
    hunk: _Hunk,
    offset: int,
) -> tuple[list[str], int] | None:
    """Apply a hunk as a sequence of independent delete/add blocks.

    Returns ``(new_working, new_offset)`` when every block can be
    located and placed. Returns ``None`` if ANY block can't be
    located — the caller must fall through to later stages, and the
    working state is never partially mutated.

    Pure-insertion blocks (all ``+``, no ``-``) are placed at their
    relative hint. Pure-deletion blocks (all ``-``, no ``+``) remove
    the located span. Mixed blocks swap delete for add with the usual
    ``_reindent`` indentation preservation.

    Only useful when the hunk has more than one block (otherwise it
    degenerates to the delete-only stage we already run). Single-block
    hunks still fall through to stage 4, so nothing is lost.
    """
    blocks = _split_into_blocks(hunk)
    if len(blocks) <= 1:
        return None

    trial = list(working)
    trial_offset = offset
    for deletes, adds, rel in blocks:
        sub_hint = max(0, hunk.orig_start - 1 + trial_offset + rel)
        if deletes:
            pos, indent_shift = _locate(trial, deletes, sub_hint)
            if pos is None:
                return None
            new_segment = _reindent(adds, indent_shift)
            trial[pos : pos + len(deletes)] = new_segment
            trial_offset += len(new_segment) - len(deletes)
        else:
            # Pure insertion — place at hint, no shift.
            pos = max(0, min(sub_hint, len(trial)))
            trial[pos:pos] = list(adds)
            trial_offset += len(adds)
    return trial, trial_offset


def _anchor_variants(
    ops: list[_HunkOp],
) -> list[tuple[list[str], list[str]]]:
    """Yield ``(anchor, replacement)`` pairs, strictest first.

    The first pair is the full anchor; subsequent pairs progressively
    peel OUTER context lines (those before the first ``-``/``+`` op, or
    after the last). Inner context and delete lines stay intact, so each
    variant is still a strict contiguous match — not a fuzzy guess.

    Ordered by total peel ascending; within a given total, leading-peel
    variants come before trailing-peel variants. Callers break on first
    hit.
    """
    variants: list[tuple[list[str], list[str]]] = []

    # Count outer context ops (context lines before the first '-'/'+' op
    # and after the last). These are the only ones we peel; inner context
    # between deletes is load-bearing and stays.
    leading_max = 0
    for op in ops:
        if op.op == " ":
            leading_max += 1
        else:
            break
    trailing_max = 0
    for op in reversed(ops):
        if op.op == " ":
            trailing_max += 1
        else:
            break

    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    for total in range(leading_max + trailing_max + 1):
        for leading in range(max(0, total - trailing_max), min(leading_max, total) + 1):
            trailing = total - leading
            end = len(ops) - trailing if trailing else len(ops)
            sub = ops[leading:end]
            anchor = [o.text for o in sub if o.op in (" ", "-")]
            replacement = [o.text for o in sub if o.op in (" ", "+")]
            if not anchor:
                continue
            key = (tuple(anchor), tuple(replacement))
            if key in seen:
                continue
            seen.add(key)
            variants.append((anchor, replacement))
    return variants


def _anchor_not_found_message(
    hunk_idx: int,
    hunk: _Hunk,
    delete_only: list[str],
) -> str:
    """Build a diagnostic message that shows the actual edit target.

    The prior message showed ``context_and_delete[:3]``, which for hunks
    with many leading context lines never included the delete target —
    so the reader couldn't tell what line the model was trying to edit.
    This version shows the delete lines explicitly, plus a compact
    context preview.
    """
    deletes_preview = "\n".join(delete_only[:3]) if delete_only else "(pure insertion, no delete lines)"
    if len(delete_only) > 3:
        deletes_preview += "\n…"
    context_preview = [op.text for op in hunk.ops if op.op == " "][:2]
    ctx_block = ""
    if context_preview:
        ctx_block = "\nNearby context: " + " | ".join(context_preview)
    return (
        f"hunk #{hunk_idx + 1} (orig_start={hunk.orig_start}) anchor "
        f"not found in file. Delete target(s):\n{deletes_preview}{ctx_block}"
    )


def _locate(
    lines: list[str],
    anchor: list[str],
    hint: int,
) -> tuple[int | None, int]:
    """Find ``anchor`` in ``lines``, returning ``(position, indent_shift)``.

    Tries exact match first, then an lstripped match. ``indent_shift`` is
    the difference between the file line's leading whitespace and the
    anchor line's leading whitespace — used by ``_reindent`` to preserve
    the file's indentation on the replacement when the lstripped path
    wins. Exact matches return ``indent_shift=0``.

    Returns ``(None, 0)`` when the anchor can't be located.
    """
    if not anchor:
        return max(0, min(hint, len(lines))), 0

    pos = _find_anchor(lines, anchor, hint)
    if pos is not None:
        return pos, 0

    stripped = [line.lstrip() for line in anchor]
    pos = _find_anchor_lstripped(lines, stripped, hint)
    if pos is not None:
        file_indent = _leading_ws(lines[pos])
        anchor_indent = _leading_ws(anchor[0])
        indent_shift = len(file_indent) - len(anchor_indent)
        return pos, indent_shift
    return None, 0


def _find_anchor_lstripped(
    lines: list[str],
    stripped_anchor: list[str],
    hint: int,
) -> int | None:
    """Same shape as ``_find_anchor`` but compares lstripped content."""
    n = len(lines)
    alen = len(stripped_anchor)
    positions = [p for p in range(n - alen + 1) if [lines[p + i].lstrip() for i in range(alen)] == stripped_anchor]
    if not positions:
        return None
    positions.sort(key=lambda p: (abs(p - hint), p))
    return positions[0]


def _leading_ws(s: str) -> str:
    """Return the leading whitespace of ``s`` (spaces and tabs)."""
    i = 0
    while i < len(s) and s[i] in (" ", "\t"):
        i += 1
    return s[:i]


def _reindent(segment: list[str], indent_shift: int) -> list[str]:
    """Apply ``indent_shift`` spaces to each line in ``segment``.

    When the anchor matched via lstripped comparison, the file's
    indentation differs from the diff's by ``indent_shift`` columns.
    We shift the replacement lines by the same amount so they slot in
    cleanly. Negative shifts drop up to that many leading whitespace
    chars (and no more — we never cut into non-whitespace content).

    ``indent_shift == 0`` short-circuits to a shallow copy.
    """
    if indent_shift == 0:
        return list(segment)
    out: list[str] = []
    if indent_shift > 0:
        prefix = " " * indent_shift
        for line in segment:
            out.append(prefix + line)
        return out
    # Negative shift — strip up to |shift| leading whitespace chars.
    drop = -indent_shift
    for line in segment:
        i = 0
        while i < drop and i < len(line) and line[i] in (" ", "\t"):
            i += 1
        out.append(line[i:])
    return out


__all__ = ["PatchError", "apply_unified_diff", "apply_unified_diff_tolerant"]
