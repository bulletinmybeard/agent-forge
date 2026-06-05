"""Code editing tool — LLM-powered file modification.

Instead of fragile ``sed`` / ``awk`` commands, this tool reads an entire file,
sends it to a coding model with structured instructions, and writes back the
result.  Think of it as the model equivalent of an IDE refactor operation.

The approach mirrors how Claude Code works: read → reason → write, treating the
file as a structured document rather than a stream of text lines.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.code_edit import register_code_edit_tools

    registry = ToolRegistry()
    register_code_edit_tools(registry)
"""

from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from ._file_snapshots import (
    latest_snapshot_for_path,
    list_snapshots_for_path,
    load_proposal,
    load_snapshot,
    save_proposal,
    save_snapshot,
)
from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_edit_profile() -> str:
    """Return the AI profile to use for code editing.

    Defaults to 'default' (the heavy model) since editing requires strong
    reasoning about file structure.  Can be overridden in config.yaml:

        tools:
          code_edit:
            profile: "thinker"
    """
    try:
        from agentforge.config import get_config

        cfg = get_config()
        return cfg._raw.get("tools", {}).get("code_edit", {}).get("profile", "default")
    except Exception:
        return "default"


def _get_max_file_size() -> int:
    """Maximum file size in bytes that we'll attempt to edit (default: 256KB)."""
    try:
        from agentforge.config import get_config

        cfg = get_config()
        kb = cfg._raw.get("tools", {}).get("code_edit", {}).get("max_file_size_kb", 256)
        return int(kb) * 1024
    except Exception:
        return 256 * 1024


# ---------------------------------------------------------------------------
# System prompt for the editing model
# ---------------------------------------------------------------------------

_EDIT_SYSTEM_PROMPT = """\
You are a precise code/config file editor. You receive the FULL contents of a \
file and an editing instruction. Your job is to apply the requested change and \
return the COMPLETE modified file.

RULES:
1. Return ONLY the file content — no explanations, no markdown fences, no \
commentary before or after.
2. Preserve the original formatting, indentation style, and line endings \
exactly — only change what the instruction asks for.
3. When asked to "remove" a block, section, or field, remove it entirely \
including all its children/nested content and any related comments.
4. For YAML files: respect indentation hierarchy. A "block" means the key \
and everything indented beneath it. Remove trailing blank lines left by \
deletions to keep the file clean.
5. For code files: preserve imports, spacing conventions, and comment style.
6. If the instruction is ambiguous, prefer the minimal change that satisfies \
the intent.
7. NEVER add new content unless explicitly asked.
8. NEVER wrap output in ```code fences``` or any other markup.
"""


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------


def _unified_diff(original: str, modified: str, filepath: str) -> str:
    """Generate a unified diff between original and modified content."""
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)

    diff = difflib.unified_diff(
        orig_lines,
        mod_lines,
        fromfile=f"a/{os.path.basename(filepath)}",
        tofile=f"b/{os.path.basename(filepath)}",
        n=3,
    )
    return "".join(diff)


def _sha256(text: str) -> str:
    """Stable content hash for pre/post-write verification."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Strip code fences (safety net — the prompt says not to, but models do)
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences if present."""
    stripped = text.strip()

    # ```yaml ... ``` or ```python ... ``` etc.
    if stripped.startswith("```"):
        # Remove first line (```lang) and last line (```)
        lines = stripped.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        elif lines[0].strip().startswith("```"):
            lines = lines[1:]
        return "\n".join(lines)

    return text


# ---------------------------------------------------------------------------
# code_edit — the main tool
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use this tool to make STRUCTURAL edits to files — removing blocks, "
        "renaming sections, refactoring code, updating config fields, etc. "
        "This is MUCH better than sed/awk for multi-line or structural changes. "
        "NEVER use sed for multi-line edits — use code_edit instead. "
        "For simple single-line replacements, sed may still be fine. "
        "For reading files without editing, use read_file instead. "
        "Parameters: file_path (string), instruction (string), "
        "reference_paths (optional list of file paths shown to the editor as "
        "READ-ONLY context — use this for 'restore from backup' style operations "
        "so the editor can see both source and target). "
        "Example: code_edit(file_path='/path/to/file.yml', "
        "instruction='remove all devbox-related jobs and stages'). "
        "Example: code_edit(file_path='~/.zshrc', "
        "instruction='restore the SpotiSync block from the reference backup', "
        "reference_paths=['~/.zshrc.backup'])"
    ),
    confirm="Edit file {file_path}?",
)
def code_edit(
    file_path: str,
    instruction: str,
    dry_run: bool = False,
    reference_paths: list[str] | None = None,
    _propose: bool = False,
    _apply_token: str = "",
) -> str:
    """Edit a file using AI with ground-truth post-write verification.

    After writing, the tool re-reads the file from disk and computes the diff
    from the re-read content, not from the LLM's claimed output. If the
    post-write hash equals the pre-write hash (i.e. the disk is unchanged),
    the tool explicitly returns an error instead of reporting success. This
    eliminates the "agent says done but file didn't change" failure mode.

    When to use: Structural edits to files (removing YAML blocks, refactoring code
        sections, rewriting config sections, restoring from a backup file).
    When NOT to use: Simple single-line replacements (use sed), reading files without
        editing (use read_file), binary files or non-UTF-8 text.

    file_path: absolute path to the file to edit
    instruction: plain English description of the change
    dry_run: if true, show the diff without writing (default: false)
    reference_paths: optional list of additional files shown to the editor as
        READ-ONLY context — the editor can reference their content when
        producing the edit but will NOT modify them. Use this for
        backup-restore, cross-file refactors, or when the edit depends on
        content from another file.
    """
    # Coerce dry_run from various LLM outputs
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "yes", "1")

    # Resolve path
    expanded = os.path.expanduser(file_path)
    path = Path(expanded).resolve()

    if not path.is_file():
        return f"Error: File not found: {file_path}"

    # Check size limit
    max_size = _get_max_file_size()
    file_size = path.stat().st_size
    if file_size > max_size:
        return (
            f"Error: File is too large ({file_size:,} bytes, limit {max_size:,}). "
            f"Increase tools.code_edit.max_file_size_kb in config.yaml or use "
            f"shell commands for this file."
        )

    # Read original content
    try:
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: File is not UTF-8 text: {file_path}"
    except Exception as exc:
        return f"Error reading file: {exc}"

    if not original.strip():
        return f"Error: File is empty: {file_path}"

    # Pre-write snapshot for ground-truth verification + rollback support
    pre_hash = _sha256(original)

    # Save a full copy of the original content to the snapshot store so the
    # revert_file tool can restore it later. No-op if a snapshot with this
    # same content hash already exists.
    snapshot_saved = False
    try:
        save_snapshot(
            pre_hash=pre_hash,
            path=str(path),
            content=original,
            tool="code_edit",
        )
        snapshot_saved = True
    except Exception as exc:
        # Snapshot failure must NOT block the edit — log and continue.
        logger.warning(
            "[code_edit] Failed to save pre-edit snapshot for %s: %s",
            path.name,
            exc,
        )

    # Apply phase of the diff-preview confirm flow: write the previously
    # proposed content verbatim, skipping the LLM entirely. What the user
    # reviewed in the diff card is exactly what lands on disk.
    if _apply_token:
        proposal = load_proposal(_apply_token)
        if proposal is None:
            return (
                f"Error: edit proposal expired or not found (token {_apply_token[:12]}). "
                f"Re-run the edit to regenerate it."
            )
        modified = proposal.get("content", "")
        if not modified or _sha256(modified) == pre_hash:
            return "No changes — the proposal matches the current file content."
        return _write_verify_return(path, original, modified, pre_hash, snapshot_saved)

    # Load reference files (read-only context for the editing LLM)
    reference_blocks: list[str] = []
    if reference_paths:
        if isinstance(reference_paths, str):
            # Defensive: coerce a single string into a list
            reference_paths = [reference_paths]
        for ref in reference_paths:
            try:
                ref_path = Path(os.path.expanduser(ref)).resolve()
            except Exception as exc:
                return f"Error resolving reference path {ref}: {exc}"
            if not ref_path.is_file():
                return f"Error: Reference file not found: {ref}"
            if ref_path.stat().st_size > max_size:
                return f"Error: Reference file too large ({ref_path.stat().st_size:,} bytes, limit {max_size:,}): {ref}"
            try:
                ref_text = ref_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return f"Error: Reference file is not UTF-8 text: {ref}"
            except Exception as exc:
                return f"Error reading reference {ref}: {exc}"
            reference_blocks.append(
                f"--- REFERENCE: {ref_path.name} "
                f"({len(ref_text.splitlines())} lines) ---\n"
                f"{ref_text}\n"
                f"--- END REFERENCE: {ref_path.name} ---\n"
            )

    # Detect file type for context
    suffix = path.suffix.lstrip(".")
    file_type_hint = {
        "yml": "YAML",
        "yaml": "YAML",
        "json": "JSON",
        "py": "Python",
        "pyi": "Python",
        "js": "JavaScript",
        "jsx": "JavaScript/React",
        "ts": "TypeScript",
        "tsx": "TypeScript/React",
        "sh": "Shell script",
        "bash": "Shell script",
        "zsh": "Shell script",
        "toml": "TOML",
        "ini": "INI",
        "cfg": "INI",
        "xml": "XML",
        "html": "HTML",
        "htm": "HTML",
        "css": "CSS",
        "scss": "SCSS",
        "md": "Markdown",
        "rs": "Rust",
        "go": "Go",
        "rb": "Ruby",
        "tf": "Terraform/HCL",
        "Dockerfile": "Dockerfile",
    }.get(suffix, suffix.upper() if suffix else "text")

    # Build the editing prompt
    line_count = len(original.splitlines())
    prompt_parts: list[str] = [
        f"FILE TO EDIT: {path.name} ({file_type_hint}, {line_count} lines)",
        f"INSTRUCTION: {instruction}",
        "",
    ]
    if reference_blocks:
        prompt_parts.append(
            "The following reference files are provided for CONTEXT ONLY. "
            "Do NOT return their content. Use them to inform the edit applied "
            "to the TARGET file below.\n"
        )
        prompt_parts.extend(reference_blocks)
    prompt_parts += [
        "--- TARGET FILE CONTENT ---",
        original,
        "--- END TARGET FILE CONTENT ---",
        "",
        "Apply the instruction and return the complete modified TARGET file.",
    ]
    user_prompt = "\n".join(prompt_parts)

    # Call the editing model
    try:
        from agentforge.client import AIClient

        profile = _get_edit_profile()
        client = AIClient(profile=profile)

        logger.info(
            "[code_edit] Editing %s (%d lines, %s) refs=%d profile='%s': %s",
            path.name,
            line_count,
            file_type_hint,
            len(reference_blocks),
            profile,
            instruction[:100],
        )

        response = client.chat(
            [
                {"role": "system", "content": _EDIT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
        )

        modified = response.content
    except Exception as exc:
        return f"Error: LLM call failed: {exc}"

    # Strip code fences if the model wrapped them
    modified = _strip_fences(modified)

    # Ensure trailing newline matches original
    if original.endswith("\n") and not modified.endswith("\n"):
        modified += "\n"

    # Hash what the LLM claims to have produced
    llm_hash = _sha256(modified)

    # No changes?  Editor returned an unmodified file.
    if llm_hash == pre_hash:
        return (
            f"No changes — editor returned the file unchanged. "
            f"The instruction may already be satisfied, or the editor could "
            f"not determine the intended change.\n"
            f"pre_hash={pre_hash}"
        )

    if dry_run:
        diff = _unified_diff(original, modified, str(path))
        diff_lines = diff.strip().splitlines()
        additions = sum(1 for _l in diff_lines if _l.startswith("+") and not _l.startswith("+++"))
        deletions = sum(1 for _l in diff_lines if _l.startswith("-") and not _l.startswith("---"))
        _snap_line = f"snapshot_id={pre_hash}\n" if snapshot_saved else ""
        return (
            f"DRY RUN — NOT written to {path.name}\n"
            f"(+{additions} -{deletions} lines)\n"
            f"pre_hash={pre_hash}\n"
            f"expected_post_hash={llm_hash}\n"
            f"{_snap_line}\n"
            f"{diff}"
        )

    # Propose phase of the diff-preview confirm flow: cache the computed edit
    # keyed by its content hash and return the diff WITHOUT writing. The
    # orchestrator shows the diff card, confirms, then calls back with
    # _apply_token to write this exact content.
    if _propose:
        try:
            save_proposal(token=llm_hash, path=str(path), content=modified)
        except Exception as exc:
            return f"Error caching proposed edit: {exc}"
        diff = _unified_diff(original, modified, str(path))
        diff_lines = diff.strip().splitlines()
        additions = sum(1 for _l in diff_lines if _l.startswith("+") and not _l.startswith("+++"))
        deletions = sum(1 for _l in diff_lines if _l.startswith("-") and not _l.startswith("---"))
        return (
            f"PROPOSED {path.name} (+{additions} -{deletions} lines)\n"
            f"apply_token={llm_hash}\n"
            f"pre_hash={pre_hash}\n"
            f"path={path}\n"
            f"\n{diff}"
        )

    return _write_verify_return(path, original, modified, pre_hash, snapshot_saved)


def _write_verify_return(
    path: Path,
    original: str,
    modified: str,
    pre_hash: str,
    snapshot_saved: bool,
) -> str:
    """Write *modified* to *path*, re-read to verify, and return the result text.

    Shared by the normal edit path and the apply phase of the diff-preview
    flow. The diff is recomputed from the disk content (ground truth), never
    from the in-memory string, so a silent write failure can't report success.
    """
    # ---- CRITICAL: write, re-read, verify from disk ----
    try:
        path.write_text(modified, encoding="utf-8")
    except Exception as exc:
        return f"Error writing file: {exc}"

    try:
        verified = path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error: wrote file but could not re-read it to verify: {exc}\npre_hash={pre_hash}"

    post_hash = _sha256(verified)

    if post_hash == pre_hash:
        # Write "succeeded" but disk is unchanged — catastrophic silent failure.
        logger.error(
            "[code_edit] SILENT WRITE FAILURE on %s: disk unchanged after write",
            path.name,
        )
        return (
            f"ERROR: Write reported success but disk content is unchanged.\n"
            f"pre_hash={pre_hash}\n"
            f"post_hash={post_hash}\n"
            f"path={path}\n"
            f"Refusing to report success — the file on disk was NOT modified."
        )

    # Compute the REAL diff from disk content, not from in-memory output
    real_diff = _unified_diff(original, verified, str(path))
    diff_lines = real_diff.strip().splitlines()
    additions = sum(1 for _l in diff_lines if _l.startswith("+") and not _l.startswith("+++"))
    deletions = sum(1 for _l in diff_lines if _l.startswith("-") and not _l.startswith("---"))

    logger.info(
        "[code_edit] Written+verified %s (+%d -%d) pre=%s post=%s",
        path.name,
        additions,
        deletions,
        pre_hash[:12],
        post_hash[:12],
    )

    # Structured header lets _hooks.py parse ground truth from the result text.
    # snapshot_id (== pre_hash) tells the agent how to roll back via revert_file.
    _snap_line = f"snapshot_id={pre_hash}\n" if snapshot_saved else ""
    _revert_hint = f"\n(To undo: revert_file(file_path='{path}', pre_hash='{pre_hash}'))" if snapshot_saved else ""
    return (
        f"✓ VERIFIED {path.name} updated (+{additions} -{deletions} lines)\n"
        f"pre_hash={pre_hash}\n"
        f"post_hash={post_hash}\n"
        f"path={path}\n"
        f"{_snap_line}"
        f"{_revert_hint}\n"
        f"\n{real_diff}"
    )


# ---------------------------------------------------------------------------
# revert_file — first-class rollback for code_edit
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Restore a file to the content that was saved BEFORE the most recent "
        "code_edit. Use this when the user says 'undo that', 'put it back', "
        "'revert the change', or similar. Automatically verifies the write by "
        "re-reading from disk.\n"
        "\n"
        "PREFERRED USAGE: call with ONLY file_path — the tool automatically "
        "picks the most recent snapshot for that file. This is almost always "
        "what 'undo' / 'put it back' means.\n"
        "\n"
        "  revert_file(file_path='~/.zshrc')   ← always try this form first\n"
        "\n"
        "ADVANCED: pass pre_hash to target a specific older version. "
        "pre_hash must be a real sha256 you previously saw in a snapshot_id= "
        "or pre_hash= line of a code_edit / revert_file result — DO NOT "
        "fabricate or guess hashes, and DO NOT copy truncated 12-char prefixes "
        "from error messages verbatim. If you don't have the exact full hash, "
        "omit pre_hash entirely."
    ),
    confirm="Revert file {file_path}?",
)
def revert_file(file_path: str, pre_hash: str | None = None) -> str:
    """Restore a file from the snapshot store with post-write verification.

    Snapshots are created automatically by code_edit before every write.
    This tool looks up a snapshot and restores the original content,
    then re-reads the file to verify the restore actually landed.

    file_path: absolute path to the file to restore
    pre_hash: (optional) the sha256 of the version to restore. If omitted,
        the most recent snapshot whose stored path matches file_path is used.
        The sha256 is printed as ``snapshot_id=...`` in every code_edit result.
    """
    expanded = os.path.expanduser(file_path)
    path = Path(expanded).resolve()

    # --- Look up the snapshot envelope --------------------------------
    if pre_hash:
        env = load_snapshot(pre_hash)
        if env is None:
            # Try to give the user a useful hint: list available snapshots.
            # We show the FULL 64-char sha256 so an LLM that copies the
            # hash back verbatim on the next call gets a valid value —
            # previously we printed `pre_hash[:12]` and the model then
            # passed that truncation back in, which load_snapshot rejected.
            available = list_snapshots_for_path(str(path))
            if available:
                hint = "\n".join(f"  - {s['pre_hash']}  ({s['saved_at']})" for s in available[:5])
                return (
                    f"Error: snapshot {pre_hash!r} not found for {path.name}.\n"
                    f"Do NOT invent or truncate hashes. Either call revert_file "
                    f"with file_path only (picks the newest snapshot automatically), "
                    f"or pass one of these FULL sha256 hashes verbatim.\n"
                    f"Available snapshots for this file:\n{hint}"
                )
            return (
                f"Error: snapshot {pre_hash!r} not found, and no "
                f"other snapshots exist for {path}. Call revert_file with "
                f"file_path only after the next code_edit has created a snapshot."
            )
        # Cross-check the snapshot's stored path matches the requested file
        if env.get("path") and env["path"] != str(path):
            logger.warning(
                "[revert_file] snapshot path mismatch: requested=%s snapshot=%s",
                path,
                env["path"],
            )
    else:
        env = latest_snapshot_for_path(str(path))
        if env is None:
            return (
                f"Error: no snapshots found for {path}. "
                f"revert_file only works after a prior code_edit has created "
                f"a snapshot."
            )

    snapshot_content = env.get("content", "")
    snapshot_hash = env.get("pre_hash", "")
    if not snapshot_content:
        return f"Error: snapshot envelope for {snapshot_hash[:12]}... is empty."

    # --- Read current content (may not exist — we'll create it) -------
    current_exists = path.is_file()
    if current_exists:
        try:
            current = path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Error reading current file before revert: {exc}"
    else:
        current = ""

    current_hash = _sha256(current) if current_exists else ""

    # Already at the target state?
    if current_hash == snapshot_hash:
        return f"No changes — {path.name} already matches snapshot {snapshot_hash[:12]}...\npre_hash={current_hash}"

    # Save a pre-revert snapshot of the CURRENT content too so the user
    # can roll the revert itself forward again.
    if current_exists:
        try:
            save_snapshot(
                pre_hash=current_hash,
                path=str(path),
                content=current,
                tool="revert_file",
            )
        except Exception as exc:
            logger.warning(
                "[revert_file] failed to snapshot current %s: %s",
                path.name,
                exc,
            )

    # --- Write, re-read, verify ---------------------------------------
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot_content, encoding="utf-8")
    except Exception as exc:
        return f"Error writing snapshot content: {exc}"

    try:
        verified = path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error: wrote snapshot but could not re-read to verify: {exc}\nsnapshot_hash={snapshot_hash}"

    post_hash = _sha256(verified)
    if post_hash != snapshot_hash:
        logger.error(
            "[revert_file] Restore drift on %s: expected=%s got=%s",
            path.name,
            snapshot_hash[:12],
            post_hash[:12],
        )
        return (
            f"ERROR: revert wrote but disk content does not match snapshot.\n"
            f"expected_hash={snapshot_hash}\n"
            f"actual_hash={post_hash}\n"
            f"path={path}"
        )

    # --- Build diff from current → restored ---------------------------
    real_diff = _unified_diff(current, verified, str(path))
    diff_lines = real_diff.strip().splitlines()
    additions = sum(1 for _l in diff_lines if _l.startswith("+") and not _l.startswith("+++"))
    deletions = sum(1 for _l in diff_lines if _l.startswith("-") and not _l.startswith("---"))

    logger.info(
        "[revert_file] Restored %s from snapshot %s (+%d -%d)",
        path.name,
        snapshot_hash[:12],
        additions,
        deletions,
    )

    saved_at = env.get("saved_at", "unknown")
    # snapshot_id points to the PRE-REVERT snapshot we just saved above, so
    # the revert itself is trivially reversible via:
    #     revert_file(file_path='...', pre_hash='{current_hash}')
    snap_line = f"snapshot_id={current_hash}\n" if current_hash else ""
    undo_hint = (
        (f"(To undo this revert: revert_file(file_path='{path}', pre_hash='{current_hash}'))\n") if current_hash else ""
    )
    return (
        f"✓ VERIFIED {path.name} reverted from snapshot {snapshot_hash[:12]}... "
        f"(+{additions} -{deletions} lines)\n"
        f"pre_hash={current_hash or 'none'}\n"
        f"post_hash={post_hash}\n"
        f"path={path}\n"
        f"{snap_line}"
        f"snapshot_saved_at={saved_at}\n"
        f"{undo_hint}"
        f"\n{real_diff}"
    )


# ---------------------------------------------------------------------------
# revert_lines — hunk-granular partial rollback
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Revert a SPECIFIC LINE RANGE in a file to the content from a prior "
        "snapshot, leaving the rest of the file untouched. Use this when the "
        "user wants to undo PART of a previous code_edit, not the whole file. "
        "Line numbers refer to the CURRENT file (what's on disk right now, "
        "1-indexed, inclusive on both ends). Any diff hunk between current "
        "and the snapshot that overlaps the requested range is reverted "
        "ATOMICALLY — hunks are never split in half, so syntax stays valid.\n"
        "\n"
        "  revert_lines(file_path='~/.zshrc', start_line=42, end_line=48)\n"
        "\n"
        "Like revert_file, the snapshot is auto-picked (newest) when pre_hash "
        "is omitted. A pre-revert snapshot of the CURRENT content is saved "
        "first, so the partial revert is itself undoable via revert_file. "
        "To revert a single line, pass the same value for start_line and "
        "end_line.\n"
        "\n"
        "When to use: user says 'put back just that one line', 'undo that "
        "hunk only', 'revert lines 42-45 but keep the rest of my changes'. "
        "When NOT to use: user wants a full rollback (use revert_file), or "
        "wants a non-snapshot edit (use code_edit)."
    ),
    confirm="Revert lines {start_line}-{end_line} in {file_path}?",
)
def revert_lines(
    file_path: str,
    start_line: int,
    end_line: int,
    pre_hash: str | None = None,
) -> str:
    """Revert a line range in a file to the content from a prior snapshot.

    Uses ``difflib.SequenceMatcher`` to compute the reverse diff (current →
    snapshot). Any diff hunk whose current-side range intersects the user's
    ``[start_line, end_line]`` range is reverted atomically; hunks outside
    the range are preserved, so unrelated edits in the rest of the file
    survive. This matches the semantics of ``git checkout -p`` applied to
    the requested range only.

    file_path: absolute path to the file
    start_line: first line of the range to revert (1-indexed, inclusive)
    end_line: last line of the range to revert (1-indexed, inclusive).
        Pass the same value as start_line to revert a single line.
        Values past the end of the file are clamped to the file length.
    pre_hash: (optional) full sha256 of the snapshot to revert to. Omit to
        use the newest snapshot for this file. DO NOT guess or truncate —
        if unsure, omit.
    """
    # Coerce numeric args from LLM string outputs
    try:
        start_line = int(start_line)
        end_line = int(end_line)
    except (TypeError, ValueError):
        return f"Error: start_line and end_line must be integers, got start_line={start_line!r} end_line={end_line!r}"

    if start_line < 1 or end_line < 1:
        return f"Error: line numbers must be >= 1 (got start_line={start_line}, end_line={end_line})"
    if end_line < start_line:
        return (
            f"Error: end_line ({end_line}) must be >= start_line ({start_line}). "
            f"To revert a single line, pass the same value for both."
        )

    expanded = os.path.expanduser(file_path)
    path = Path(expanded).resolve()

    if not path.is_file():
        return f"Error: File not found: {file_path}"

    # --- Look up the snapshot envelope (same logic as revert_file) ----
    if pre_hash:
        env = load_snapshot(pre_hash)
        if env is None:
            available = list_snapshots_for_path(str(path))
            if available:
                hint = "\n".join(f"  - {s['pre_hash']}  ({s['saved_at']})" for s in available[:5])
                return (
                    f"Error: snapshot {pre_hash!r} not found for {path.name}.\n"
                    f"Do NOT invent or truncate hashes. Either call revert_lines "
                    f"without pre_hash (picks the newest snapshot automatically), "
                    f"or pass one of these FULL sha256 hashes verbatim.\n"
                    f"Available snapshots for this file:\n{hint}"
                )
            return (
                f"Error: snapshot {pre_hash!r} not found, and no "
                f"other snapshots exist for {path}. revert_lines only works "
                f"after a prior code_edit has created a snapshot."
            )
        if env.get("path") and env["path"] != str(path):
            logger.warning(
                "[revert_lines] snapshot path mismatch: requested=%s snapshot=%s",
                path,
                env["path"],
            )
    else:
        env = latest_snapshot_for_path(str(path))
        if env is None:
            return (
                f"Error: no snapshots found for {path}. "
                f"revert_lines only works after a prior code_edit has created "
                f"a snapshot."
            )

    snapshot_content = env.get("content", "")
    snapshot_hash = env.get("pre_hash", "")

    # --- Read current content ----------------------------------------
    try:
        current = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: File is not UTF-8 text: {file_path}"
    except Exception as exc:
        return f"Error reading file: {exc}"

    current_hash = _sha256(current)

    if current_hash == snapshot_hash:
        return f"No changes — {path.name} already matches snapshot {snapshot_hash[:12]}...\npre_hash={current_hash}"

    current_lines = current.splitlines(keepends=True)
    snapshot_lines = snapshot_content.splitlines(keepends=True)

    total_current = len(current_lines)
    if start_line > total_current:
        return f"Error: start_line {start_line} exceeds the current file length ({total_current} lines)."
    # Clamp end_line — asking for "lines 10-999" on a 20-line file is a
    # reasonable shorthand for "lines 10 to end".
    end_line = min(end_line, total_current)

    # 0-indexed half-open [s, e) for opcode intersection tests
    s = start_line - 1
    e = end_line

    # --- Compute reverse diff current → snapshot and filter by range --
    matcher = difflib.SequenceMatcher(
        a=current_lines,
        b=snapshot_lines,
        autojunk=False,
    )
    opcodes = matcher.get_opcodes()

    new_lines: list[str] = []
    applied_hunks = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            new_lines.extend(current_lines[i1:i2])
            continue

        if tag in ("replace", "delete"):
            # current has [i1:i2] that should become snapshot's [j1:j2]
            overlaps = i1 < e and i2 > s
        else:  # insert — snapshot has lines missing from current
            # insertion point is at current index i1; apply iff it falls
            # within or on the border of the requested range
            overlaps = s <= i1 <= e

        if overlaps:
            new_lines.extend(snapshot_lines[j1:j2])
            applied_hunks += 1
        else:
            new_lines.extend(current_lines[i1:i2])

    if applied_hunks == 0:
        return (
            f"No changes — no diff hunks from snapshot "
            f"{snapshot_hash[:12]}... intersect lines {start_line}-{end_line} "
            f"in {path.name}. Either that range is already at the snapshot "
            f"state, or the edit you want to undo is outside the specified "
            f"range.\npre_hash={current_hash}"
        )

    new_content = "".join(new_lines)
    new_hash = _sha256(new_content)

    if new_hash == current_hash:
        return f"No changes — computed revert produced identical content to the current file.\npre_hash={current_hash}"

    # Snapshot CURRENT so the partial revert is itself undoable via
    # revert_file(file_path=..., pre_hash=<current_hash>)
    snapshot_saved = False
    try:
        save_snapshot(
            pre_hash=current_hash,
            path=str(path),
            content=current,
            tool="revert_lines",
        )
        snapshot_saved = True
    except Exception as exc:
        logger.warning(
            "[revert_lines] failed to snapshot current %s: %s",
            path.name,
            exc,
        )

    # --- Write, re-read, verify --------------------------------------
    try:
        path.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        return f"Error writing file: {exc}"

    try:
        verified = path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error: wrote file but could not re-read to verify: {exc}\npre_hash={current_hash}"

    post_hash = _sha256(verified)

    if post_hash == current_hash:
        logger.error(
            "[revert_lines] SILENT WRITE FAILURE on %s: disk unchanged after write",
            path.name,
        )
        return (
            f"ERROR: Write reported success but disk content is unchanged.\n"
            f"pre_hash={current_hash}\n"
            f"post_hash={post_hash}\n"
            f"path={path}\n"
            f"Refusing to report success — the file on disk was NOT modified."
        )

    if post_hash != new_hash:
        logger.warning(
            "[revert_lines] Round-trip drift on %s: computed=%s disk=%s",
            path.name,
            new_hash[:12],
            post_hash[:12],
        )

    # Compute REAL diff from disk content, not from our computed buffer
    real_diff = _unified_diff(current, verified, str(path))
    diff_lines = real_diff.strip().splitlines()
    additions = sum(1 for _l in diff_lines if _l.startswith("+") and not _l.startswith("+++"))
    deletions = sum(1 for _l in diff_lines if _l.startswith("-") and not _l.startswith("---"))

    logger.info(
        "[revert_lines] Reverted %s lines %d-%d (%d hunk%s, +%d -%d) pre=%s post=%s",
        path.name,
        start_line,
        end_line,
        applied_hunks,
        "" if applied_hunks == 1 else "s",
        additions,
        deletions,
        current_hash[:12],
        post_hash[:12],
    )

    saved_at = env.get("saved_at", "unknown")
    _snap_line = f"snapshot_id={current_hash}\n" if snapshot_saved else ""
    _undo_hint = (
        (f"\n(To undo this partial revert: revert_file(file_path='{path}', pre_hash='{current_hash}'))")
        if snapshot_saved
        else ""
    )
    _hunk_word = "hunk" if applied_hunks == 1 else "hunks"
    return (
        f"✓ VERIFIED {path.name} partially reverted "
        f"lines {start_line}-{end_line} from snapshot "
        f"{snapshot_hash[:12]}... "
        f"({applied_hunks} {_hunk_word}, +{additions} -{deletions} lines)\n"
        f"pre_hash={current_hash}\n"
        f"post_hash={post_hash}\n"
        f"path={path}\n"
        f"{_snap_line}"
        f"snapshot_saved_at={saved_at}\n"
        f"hunks_applied={applied_hunks}\n"
        f"lines_requested={start_line}-{end_line}"
        f"{_undo_hint}\n"
        f"\n{real_diff}"
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_code_edit_tools(registry: ToolRegistry) -> int:
    """Register code editing tools with the given *registry*."""
    registry.register_category_hint(
        "Code Editing",
        "LLM-powered file editing. Use code_edit for structural changes to "
        "files (removing blocks, refactoring sections, rewriting config). "
        "Prefer this over sed/awk for any multi-line or structural edit.",
    )

    count = 0
    for _name, func in list(globals().items()):
        if callable(func) and hasattr(func, "_is_tool") and not _name.startswith("_"):
            registry.register(func, category="Code Editing")
            count += 1
            logger.debug("Registered code edit tool: %s", func.__name__)
    return count
