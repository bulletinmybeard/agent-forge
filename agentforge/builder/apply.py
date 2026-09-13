"""Deterministic undo/redo for an approved @build.

Each build writes a gzip JSON bundle next to the plan file. Undo restores
the pre-build bytes; redo writes the post-build bytes. No LLM.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentforge.tools._file_snapshots import load_snapshot

logger = logging.getLogger(__name__)

SCHEMA = 1
MAX_FILE_BYTES = 2_000_000
_EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def bundle_path_for(plan_path: Path | str) -> Path:
    return Path(plan_path).expanduser().with_suffix(".apply.json.gz")


def save_apply_bundle(plan_path: Path | str, receipts: dict[str, dict[str, str]]) -> Path | None:
    """Persist before/after bytes for every written/edited path in *receipts*."""
    dest = bundle_path_for(plan_path)
    files: list[dict[str, Any]] = []
    for rec in receipts.values():
        entry = _entry_from_receipt(rec)
        if entry is not None:
            files.append(entry)
    if not files:
        return None
    env = {
        "schema": SCHEMA,
        "plan_path": str(Path(plan_path).expanduser()),
        "created": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(gzip.compress(json.dumps(env).encode("utf-8")))
    logger.info("saved build apply bundle %s (%d file(s))", dest, len(files))
    return dest


def load_apply_bundle(plan_path: Path | str) -> dict[str, Any] | None:
    dest = bundle_path_for(plan_path)
    if not dest.is_file():
        return None
    try:
        return json.loads(gzip.decompress(dest.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("apply bundle unreadable %s: %s", dest, exc)
        return None


def undo_bundle(plan_path: Path | str) -> str:
    """Restore pre-build content for every file in the bundle."""
    return _apply_side(plan_path, side="before")


def redo_bundle(plan_path: Path | str) -> str:
    """Write post-build content for every file in the bundle."""
    return _apply_side(plan_path, side="after")


def _entry_from_receipt(rec: dict[str, str]) -> dict[str, Any] | None:
    raw_path = (rec.get("path") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    pre_hash = (rec.get("pre_hash") or "").strip()
    post_hash = (rec.get("post_hash") or "").strip()
    snap = load_snapshot(pre_hash) if pre_hash else None
    if snap and isinstance(snap.get("content"), str):
        before = snap["content"]
    elif pre_hash == _EMPTY_HASH or not pre_hash:
        before = ""
    else:
        logger.warning("no snapshot for %s (pre_hash=%s), skip bundle entry", path, pre_hash[:12])
        return None
    after = ""
    try:
        if path.is_file():
            after = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if len(before.encode("utf-8")) > MAX_FILE_BYTES or len(after.encode("utf-8")) > MAX_FILE_BYTES:
        logger.warning("skip apply-bundle entry %s (over %d bytes)", path, MAX_FILE_BYTES)
        return None
    if after == "" and before == "":
        return None
    created = (not before) or pre_hash == _EMPTY_HASH
    return {
        "path": str(path),
        "pre_hash": pre_hash,
        "post_hash": post_hash or hashlib.sha256(after.encode("utf-8")).hexdigest(),
        "before": before,
        "after": after,
        "diff_text": rec.get("diff_text") or "",
        "created": created,
    }


def _apply_side(plan_path: Path | str, *, side: str) -> str:
    bundle = load_apply_bundle(plan_path)
    if not bundle or not bundle.get("files"):
        return (
            f"No apply bundle next to `{plan_path}`. Run `@build` once so writes "
            "are recorded, then undo/redo from this session."
        )
    want = "before" if side == "before" else "after"
    other = "after" if want == "before" else "before"
    verb = "Reverted" if want == "before" else "Re-applied"
    lines = [f"# Build {verb.lower()}", "", f"Plan: `{plan_path}`", ""]
    ok = 0
    skipped = 0
    for entry in bundle["files"]:
        path = Path(str(entry.get("path") or "")).expanduser()
        target = entry.get(want) or ""
        current_ok, current = _read_text(path)
        expected_other = entry.get(other) or ""
        if current_ok and current == target:
            lines.append(f"- skip `{path}` (already {want})")
            skipped += 1
            continue
        if current_ok and current != expected_other and current != target:
            lines.append(f"- skip `{path}` (file drifted since the build)")
            skipped += 1
            continue
        if want == "before" and entry.get("created") and not target:
            try:
                if path.is_file():
                    path.unlink()
                    lines.append(f"- removed `{path}` (created by the build)")
                    ok += 1
                else:
                    lines.append(f"- skip `{path}` (already absent)")
                    skipped += 1
            except OSError as exc:
                lines.append(f"- fail `{path}`: {exc}")
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(target, encoding="utf-8")
            lines.append(f"- wrote `{path}`")
            ok += 1
        except OSError as exc:
            lines.append(f"- fail `{path}`: {exc}")
    lines.append("")
    lines.append(f"{verb} {ok} file(s), skipped {skipped}.")
    if want == "before":
        lines.append("Say **apply the changes again** (or `@build redo`) to put them back.")
    else:
        lines.append("Say **undo the build** (or `@build undo`) to restore the pre-build files.")
    return "\n".join(lines)


def _read_text(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, ""
    try:
        return True, path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False, ""
