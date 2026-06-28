"""Data analysis tools — file comparison with format-aware diffing.

Supports plain text (unified diff), JSON (semantic), CSV/TSV (row-level),
YAML (semantic), TOML (semantic), and binary (hash comparison).

Uses ``deepdiff`` for structured comparisons of JSON, YAML, and TOML.
Falls back to ``difflib`` for plain text.  Binary files get SHA-256
hash + size comparison.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.data_tools import register_data_tools

    registry = ToolRegistry()
    register_data_tools(registry)
"""

from __future__ import annotations

import csv
import datetime
import difflib
import hashlib
import io
import json
import logging
import mimetypes
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_OUTPUT_CHARS = 15_000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FORMAT_EXTENSIONS = {
    ".json": "json",
    ".jsonl": "json",
    ".geojson": "json",
    ".csv": "csv",
    ".tsv": "csv",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
}

_VALID_FORMATS = ("auto", "text", "json", "csv", "yaml", "toml", "binary")


def _human_size(size_bytes: int | float) -> str:
    """Convert bytes to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_binary(path: Path) -> bool:
    """Check if a file is binary by looking for null bytes in the first 8KB."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
            return b"\x00" in chunk
    except Exception:
        return False


def _detect_format(path_a: Path, path_b: Path) -> str:
    """Auto-detect diff format from file extensions."""
    ext_a = path_a.suffix.lower()
    ext_b = path_b.suffix.lower()

    fmt_a = _FORMAT_EXTENSIONS.get(ext_a)
    fmt_b = _FORMAT_EXTENSIONS.get(ext_b)

    if fmt_a and fmt_b and fmt_a == fmt_b:
        return fmt_a
    if fmt_a and fmt_b and fmt_a != fmt_b:
        return "text"  # Mismatched formats fall back to text

    # If only one is recognized, use it (other is probably the same content)
    if fmt_a:
        return fmt_a
    if fmt_b:
        return fmt_b

    # Check for binary
    if _is_binary(path_a) or _is_binary(path_b):
        return "binary"

    return "text"


def _truncate_output(output: str, total_changes: int = 0) -> str:
    """Truncate output if it exceeds the max, preserving the header."""
    if len(output) <= MAX_OUTPUT_CHARS:
        return output

    # Find end of header (first blank line)
    header_end = output.find("\n\n")
    if header_end < 0:
        header_end = 0

    header = output[: header_end + 2]
    body = output[header_end + 2 :]
    max_body = MAX_OUTPUT_CHARS - len(header) - 100

    truncated = body[:max_body]
    note = f"\n\n[... output truncated at {MAX_OUTPUT_CHARS:,} chars"
    if total_changes:
        note += f" — showing partial results of {total_changes:,} total changes"
    note += "]"

    return header + truncated + note


# ---------------------------------------------------------------------------
# Diff strategies
# ---------------------------------------------------------------------------


def _diff_text(path_a: Path, path_b: Path, context_lines: int) -> str:
    """Unified text diff via difflib."""
    text_a = path_a.read_text(errors="replace").splitlines(keepends=True)
    text_b = path_b.read_text(errors="replace").splitlines(keepends=True)

    diff = list(
        difflib.unified_diff(
            text_a,
            text_b,
            fromfile=str(path_a),
            tofile=str(path_b),
            n=context_lines,
        )
    )

    if not diff:
        hash_a = _sha256(path_a)
        return f"Files are identical (SHA-256: {hash_a[:16]}..., {_human_size(path_a.stat().st_size)})"

    # Count stats
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    unchanged = len(text_a) - removed

    header = (
        f"Diff: {path_a.name} vs {path_b.name}\n"
        f"Strategy: text (unified, {context_lines} lines context)\n"
        f"Stats: {added} additions, {removed} deletions, {unchanged} unchanged\n\n"
    )

    body = "".join(diff)
    output = header + body
    return _truncate_output(output, added + removed)


def _diff_json(path_a: Path, path_b: Path) -> str:
    """Semantic JSON diff using deepdiff."""
    try:
        with open(path_a) as f:
            obj_a = json.load(f)
        with open(path_b) as f:
            obj_b = json.load(f)
    except json.JSONDecodeError as exc:
        return f"(failed to parse as JSON — falling back to text diff)\n\n{exc}"

    try:
        from deepdiff import DeepDiff
    except ImportError:
        return "(deepdiff package not installed — falling back to text diff)\nInstall with: pip install deepdiff"

    dd = DeepDiff(obj_a, obj_b, verbose_level=2)

    if not dd:
        hash_a = _sha256(path_a)
        return f"Files are identical (SHA-256: {hash_a[:16]}..., {_human_size(path_a.stat().st_size)})"

    # Convert deepdiff paths from root['key1']['key2'] to root.key1.key2
    def _clean_path(p: str) -> str:
        return p.replace("['", ".").replace("']", "").replace('["', ".").replace('"]', "")

    sections = []
    total_changes = 0

    # Added
    added = dd.get("dictionary_item_added", {})
    if not isinstance(added, dict):
        added = {str(k): k for k in added} if added else {}
    items_added = dd.get("iterable_item_added", {})
    all_added = {**added, **items_added}
    if all_added:
        total_changes += len(all_added)
        lines = ["Added:"]
        for path, val in sorted(all_added.items()):
            val_str = json.dumps(val, default=str) if not isinstance(val, str) else f'"{val}"'
            if len(val_str) > 100:
                val_str = val_str[:97] + "..."
            lines.append(f"  {_clean_path(path)} = {val_str}")
        sections.append("\n".join(lines))

    # Removed
    removed = dd.get("dictionary_item_removed", {})
    if not isinstance(removed, dict):
        removed = {str(k): k for k in removed} if removed else {}
    items_removed = dd.get("iterable_item_removed", {})
    all_removed = {**removed, **items_removed}
    if all_removed:
        total_changes += len(all_removed)
        lines = ["Removed:"]
        for path in sorted(all_removed.keys()):
            lines.append(f"  {_clean_path(path)}")
        sections.append("\n".join(lines))

    # Changed values
    changed = dd.get("values_changed", {})
    if changed:
        total_changes += len(changed)
        lines = ["Changed values:"]
        for path, detail in sorted(changed.items()):
            old = detail.get("old_value", "?")
            new = detail.get("new_value", "?")
            old_str = json.dumps(old, default=str) if not isinstance(old, str) else f'"{old}"'
            new_str = json.dumps(new, default=str) if not isinstance(new, str) else f'"{new}"'
            if len(old_str) > 50:
                old_str = old_str[:47] + "..."
            if len(new_str) > 50:
                new_str = new_str[:47] + "..."
            lines.append(f"  {_clean_path(path)}: {old_str} → {new_str}")
        sections.append("\n".join(lines))

    # Type changes
    type_changes = dd.get("type_changes", {})
    if type_changes:
        total_changes += len(type_changes)
        lines = ["Type changes:"]
        for path, detail in sorted(type_changes.items()):
            old = detail.get("old_value", "?")
            new = detail.get("new_value", "?")
            old_type = type(old).__name__
            new_type = type(new).__name__
            lines.append(f"  {_clean_path(path)}: {old!r} ({old_type}) → {new!r} ({new_type})")
        sections.append("\n".join(lines))

    header = (
        f"Diff: {path_a.name} vs {path_b.name}\n"
        f"Strategy: json (semantic)\n"
        f"Stats: {len(all_added)} added, {len(all_removed)} removed, "
        f"{len(changed)} changed values, {len(type_changes)} type changes\n"
    )

    output = header + "\n" + "\n\n".join(sections)
    return _truncate_output(output, total_changes)


def _diff_csv(path_a: Path, path_b: Path) -> str:
    """Row-level CSV diff with column alignment."""

    def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
        """Read CSV, auto-detect delimiter, return (headers, rows-as-dicts)."""
        text = path.read_text(errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096])
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = "\t" if path.suffix.lower() == ".tsv" else ","

        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        headers = list(reader.fieldnames or [])
        rows = list(reader)
        return headers, rows

    try:
        headers_a, rows_a = _read_csv(path_a)
        headers_b, rows_b = _read_csv(path_b)
    except Exception as exc:
        return f"(failed to parse as CSV — falling back to text diff)\n\n{exc}"

    # Detect key column
    key_col = None
    id_candidates = ("id", "ID", "_id", "Id", "key", "Key", "KEY")
    all_headers = headers_a or headers_b

    for candidate in id_candidates:
        if candidate in all_headers:
            # Check uniqueness in both files
            vals_a = [r.get(candidate, "") for r in rows_a]
            vals_b = [r.get(candidate, "") for r in rows_b]
            if len(vals_a) == len(set(vals_a)) and len(vals_b) == len(set(vals_b)):
                key_col = candidate
                break

    # Fall back to first column if its values are unique
    if not key_col and all_headers:
        first = all_headers[0]
        vals_a = [r.get(first, "") for r in rows_a]
        vals_b = [r.get(first, "") for r in rows_b]
        if len(vals_a) == len(set(vals_a)) and len(vals_b) == len(set(vals_b)):
            key_col = first

    if key_col:
        # Key-based diff
        dict_a = OrderedDict((r.get(key_col, f"row_{i}"), r) for i, r in enumerate(rows_a))
        dict_b = OrderedDict((r.get(key_col, f"row_{i}"), r) for i, r in enumerate(rows_b))

        keys_a = set(dict_a.keys())
        keys_b = set(dict_b.keys())

        added_keys = keys_b - keys_a
        removed_keys = keys_a - keys_b
        common_keys = keys_a & keys_b

        # Find changed cells
        changed_rows = []
        changed_cell_count = 0
        compare_cols = [h for h in (headers_a or headers_b) if h != key_col]

        for k in sorted(common_keys):
            row_a = dict_a[k]
            row_b = dict_b[k]
            cell_changes = []
            for col in compare_cols:
                va = row_a.get(col, "")
                vb = row_b.get(col, "")
                if va != vb:
                    cell_changes.append((col, va, vb))
                    changed_cell_count += 1
            if cell_changes:
                changed_rows.append((k, cell_changes))

        sections = []

        if added_keys:
            lines = [f"Added rows ({len(added_keys)}):"]
            for k in sorted(added_keys):
                row = dict_b[k]
                parts = [f"{key_col}={k}"]
                for col in compare_cols[:6]:
                    val = row.get(col, "")
                    if val:
                        parts.append(f'{col}="{val}"')
                lines.append("  " + "  ".join(parts))
            sections.append("\n".join(lines))

        if removed_keys:
            lines = [f"Removed rows ({len(removed_keys)}):"]
            for k in sorted(removed_keys):
                row = dict_a[k]
                parts = [f"{key_col}={k}"]
                for col in compare_cols[:6]:
                    val = row.get(col, "")
                    if val:
                        parts.append(f'{col}="{val}"')
                lines.append("  " + "  ".join(parts))
            sections.append("\n".join(lines))

        if changed_rows:
            lines = [f"Changed rows ({len(changed_rows)} rows, {changed_cell_count} cells):"]
            for k, changes in changed_rows[:50]:
                for col, old, new in changes:
                    lines.append(f'  {key_col}={k}  {col}: "{old}" → "{new}"')
            if len(changed_rows) > 50:
                lines.append(f"  ... and {len(changed_rows) - 50} more changed rows")
            sections.append("\n".join(lines))

        total_changes = len(added_keys) + len(removed_keys) + changed_cell_count

        if not sections:
            hash_a = _sha256(path_a)
            return f"Files are identical (SHA-256: {hash_a[:16]}..., {_human_size(path_a.stat().st_size)})"

        col_list = ", ".join(all_headers[:10])
        if len(all_headers) > 10:
            col_list += f", ... ({len(all_headers)} total)"

        header = (
            f"Diff: {path_a.name} vs {path_b.name}\n"
            f"Strategy: csv (key column: {key_col})\n"
            f"Stats: {len(added_keys)} added rows, {len(removed_keys)} removed rows, "
            f"{changed_cell_count} changed cells across {len(changed_rows)} rows\n"
            f"Columns: {col_list}\n"
        )

        output = header + "\n" + "\n\n".join(sections)
        return _truncate_output(output, total_changes)

    else:
        # Index-based diff (no unique key found)
        max_rows = max(len(rows_a), len(rows_b))
        diffs = []
        for i in range(max_rows):
            ra = rows_a[i] if i < len(rows_a) else None
            rb = rows_b[i] if i < len(rows_b) else None
            if ra != rb:
                diffs.append((i, ra, rb))

        if not diffs:
            hash_a = _sha256(path_a)
            return f"Files are identical (SHA-256: {hash_a[:16]}..., {_human_size(path_a.stat().st_size)})"

        header = (
            f"Diff: {path_a.name} vs {path_b.name}\n"
            f"Strategy: csv (row-index based, no unique key column found)\n"
            f"Stats: {len(diffs)} rows differ (of {max_rows} total)\n"
        )

        lines = []
        for idx, ra, rb in diffs[:50]:
            if ra is None:
                lines.append(f"  Row {idx + 1}: (added) {rb}")
            elif rb is None:
                lines.append(f"  Row {idx + 1}: (removed) {ra}")
            else:
                # Show changed cells
                for col in all_headers:
                    va = ra.get(col, "")
                    vb = rb.get(col, "")
                    if va != vb:
                        lines.append(f'  Row {idx + 1}, {col}: "{va}" → "{vb}"')

        if len(diffs) > 50:
            lines.append(f"  ... and {len(diffs) - 50} more differing rows")

        output = header + "\n" + "\n".join(lines)
        return _truncate_output(output, len(diffs))


def _diff_yaml(path_a: Path, path_b: Path) -> str:
    """Semantic YAML diff via deepdiff."""
    try:
        import yaml
    except ImportError:
        return "(PyYAML not installed — falling back to text diff)"

    try:
        with open(path_a) as f:
            obj_a = yaml.safe_load(f)
        with open(path_b) as f:
            obj_b = yaml.safe_load(f)
    except Exception as exc:
        return f"(failed to parse as YAML — falling back to text diff)\n\n{exc}"

    if obj_a is None and obj_b is None:
        return "Both files are empty YAML documents."

    try:
        from deepdiff import DeepDiff
    except ImportError:
        return "(deepdiff package not installed — falling back to text diff)\nInstall with: pip install deepdiff"

    dd = DeepDiff(obj_a, obj_b, verbose_level=2)

    if not dd:
        hash_a = _sha256(path_a)
        return f"Files are identical (SHA-256: {hash_a[:16]}..., {_human_size(path_a.stat().st_size)})"

    # Reuse JSON formatting logic (deepdiff output is the same structure)
    def _clean_path(p: str) -> str:
        return p.replace("['", ".").replace("']", "").replace('["', ".").replace('"]', "")

    sections = []
    counts = {"added": 0, "removed": 0, "changed": 0}

    added = dd.get("dictionary_item_added", {})
    if not isinstance(added, dict):
        added = {str(k): k for k in added} if added else {}
    items_added = dd.get("iterable_item_added", {})
    all_added = {**added, **items_added}
    if all_added:
        counts["added"] = len(all_added)
        lines = ["Added:"]
        for path, val in sorted(all_added.items()):
            val_str = repr(val) if not isinstance(val, str) else f'"{val}"'
            if len(val_str) > 100:
                val_str = val_str[:97] + "..."
            lines.append(f"  {_clean_path(path)} = {val_str}")
        sections.append("\n".join(lines))

    removed = dd.get("dictionary_item_removed", {})
    if not isinstance(removed, dict):
        removed = {str(k): k for k in removed} if removed else {}
    items_removed = dd.get("iterable_item_removed", {})
    all_removed = {**removed, **items_removed}
    if all_removed:
        counts["removed"] = len(all_removed)
        lines = ["Removed:"]
        for path in sorted(all_removed.keys()):
            lines.append(f"  {_clean_path(path)}")
        sections.append("\n".join(lines))

    changed = dd.get("values_changed", {})
    if changed:
        counts["changed"] = len(changed)
        lines = ["Changed values:"]
        for path, detail in sorted(changed.items()):
            old = detail.get("old_value", "?")
            new = detail.get("new_value", "?")
            old_str = repr(old) if not isinstance(old, str) else f'"{old}"'
            new_str = repr(new) if not isinstance(new, str) else f'"{new}"'
            if len(old_str) > 50:
                old_str = old_str[:47] + "..."
            if len(new_str) > 50:
                new_str = new_str[:47] + "..."
            lines.append(f"  {_clean_path(path)}: {old_str} → {new_str}")
        sections.append("\n".join(lines))

    total = sum(counts.values())
    header = (
        f"Diff: {path_a.name} vs {path_b.name}\n"
        f"Strategy: yaml (semantic)\n"
        f"Stats: {counts['added']} added, {counts['removed']} removed, {counts['changed']} changed values\n"
    )

    output = header + "\n" + "\n\n".join(sections)
    return _truncate_output(output, total)


def _diff_toml(path_a: Path, path_b: Path) -> str:
    """Semantic TOML diff via deepdiff."""
    import tomllib

    try:
        with open(path_a, "rb") as f:
            obj_a = tomllib.load(f)
        with open(path_b, "rb") as f:
            obj_b = tomllib.load(f)
    except Exception as exc:
        return f"(failed to parse as TOML — falling back to text diff)\n\n{exc}"

    try:
        from deepdiff import DeepDiff
    except ImportError:
        return "(deepdiff package not installed — falling back to text diff)\nInstall with: pip install deepdiff"

    dd = DeepDiff(obj_a, obj_b, verbose_level=2)

    if not dd:
        hash_a = _sha256(path_a)
        return f"Files are identical (SHA-256: {hash_a[:16]}..., {_human_size(path_a.stat().st_size)})"

    def _clean_path(p: str) -> str:
        return p.replace("['", ".").replace("']", "").replace('["', ".").replace('"]', "")

    sections = []
    counts = {"added": 0, "removed": 0, "changed": 0}

    added = dd.get("dictionary_item_added", {})
    if not isinstance(added, dict):
        added = {str(k): k for k in added} if added else {}
    if added:
        counts["added"] = len(added)
        lines = ["Added:"]
        for path, val in sorted(added.items()):
            val_str = repr(val)
            if len(val_str) > 100:
                val_str = val_str[:97] + "..."
            lines.append(f"  {_clean_path(path)} = {val_str}")
        sections.append("\n".join(lines))

    removed = dd.get("dictionary_item_removed", {})
    if not isinstance(removed, dict):
        removed = {str(k): k for k in removed} if removed else {}
    if removed:
        counts["removed"] = len(removed)
        lines = ["Removed:"]
        for path in sorted(removed.keys()):
            lines.append(f"  {_clean_path(path)}")
        sections.append("\n".join(lines))

    changed = dd.get("values_changed", {})
    if changed:
        counts["changed"] = len(changed)
        lines = ["Changed values:"]
        for path, detail in sorted(changed.items()):
            old = detail.get("old_value", "?")
            new = detail.get("new_value", "?")
            lines.append(f"  {_clean_path(path)}: {old!r} → {new!r}")
        sections.append("\n".join(lines))

    total = sum(counts.values())
    header = (
        f"Diff: {path_a.name} vs {path_b.name}\n"
        f"Strategy: toml (semantic)\n"
        f"Stats: {counts['added']} added, {counts['removed']} removed, {counts['changed']} changed values\n"
    )

    output = header + "\n" + "\n\n".join(sections)
    return _truncate_output(output, total)


def _diff_binary(path_a: Path, path_b: Path) -> str:
    """Binary diff — hash and size comparison."""
    size_a = path_a.stat().st_size
    size_b = path_b.stat().st_size
    hash_a = _sha256(path_a)
    hash_b = _sha256(path_b)

    mime_a = mimetypes.guess_type(str(path_a))[0] or "application/octet-stream"
    mime_b = mimetypes.guess_type(str(path_b))[0] or "application/octet-stream"

    mtime_a = datetime.datetime.fromtimestamp(path_a.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    mtime_b = datetime.datetime.fromtimestamp(path_b.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    identical = hash_a == hash_b
    size_diff = ""
    if not identical and size_a > 0:
        pct = ((size_b - size_a) / size_a) * 100
        sign = "+" if pct >= 0 else ""
        size_diff = f" ({sign}{pct:.1f}%)"

    lines = [
        f"Diff: {path_a.name} vs {path_b.name}",
        "Strategy: binary (hash comparison)\n",
        f"{'':17} {'file_a':<25} {'file_b':<25}",
        f"Size:            {_human_size(size_a):<25} {_human_size(size_b):<25}{size_diff}",
        f"SHA-256:         {hash_a[:24]}...  {hash_b[:24]}...",
        f"MIME:            {mime_a:<25} {mime_b:<25}",
        f"Modified:        {mtime_a:<25} {mtime_b:<25}",
        "",
    ]

    if identical:
        lines.append(f"Verdict: files are identical (SHA-256: {hash_a})")
    else:
        lines.append("Verdict: files differ (different hashes)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------


@tool
def diff_files(file_a: str, file_b: str, context_lines: int = 3, format: str = "auto") -> str:
    """Compare two files and return the differences.

    file_a: path to the first file (the "before" / reference)
    file_b: path to the second file (the "after" / changed)
    context_lines: number of surrounding lines in unified diff (default 3)
    format: diff strategy — auto, text, json, csv, yaml, toml, binary
    """
    fmt = format.strip().lower()

    if fmt not in _VALID_FORMATS:
        return f'Error: unknown format "{fmt}". Valid: {", ".join(_VALID_FORMATS)}'

    path_a = Path(file_a).expanduser().resolve()
    path_b = Path(file_b).expanduser().resolve()

    if not path_a.exists():
        return f"Error: file not found — {path_a}"
    if not path_b.exists():
        return f"Error: file not found — {path_b}"
    if not path_a.is_file():
        return f"Error: not a file — {path_a}"
    if not path_b.is_file():
        return f"Error: not a file — {path_b}"

    # Size check (except binary which only reads hashes)
    if fmt != "binary":
        size_a = path_a.stat().st_size
        size_b = path_b.stat().st_size
        if size_a > MAX_FILE_SIZE:
            return (
                f"Error: file_a is {_human_size(size_a)} — max supported size is "
                f"{_human_size(MAX_FILE_SIZE)}. Use binary format for large files."
            )
        if size_b > MAX_FILE_SIZE:
            return (
                f"Error: file_b is {_human_size(size_b)} — max supported size is "
                f"{_human_size(MAX_FILE_SIZE)}. Use binary format for large files."
            )

    # Auto-detect format
    if fmt == "auto":
        fmt = _detect_format(path_a, path_b)

    try:
        if fmt == "text":
            return _diff_text(path_a, path_b, context_lines)
        elif fmt == "json":
            result = _diff_json(path_a, path_b)
            if result.startswith("(failed") or result.startswith("(deepdiff"):
                # Fallback to text diff
                fallback_note = result.split("\n")[0]
                return fallback_note + "\n\n" + _diff_text(path_a, path_b, context_lines)
            return result
        elif fmt == "csv":
            result = _diff_csv(path_a, path_b)
            if result.startswith("(failed"):
                fallback_note = result.split("\n")[0]
                return fallback_note + "\n\n" + _diff_text(path_a, path_b, context_lines)
            return result
        elif fmt == "yaml":
            result = _diff_yaml(path_a, path_b)
            if result.startswith("(") and "falling back" in result:
                fallback_note = result.split("\n")[0]
                return fallback_note + "\n\n" + _diff_text(path_a, path_b, context_lines)
            return result
        elif fmt == "toml":
            result = _diff_toml(path_a, path_b)
            if result.startswith("(") and "falling back" in result:
                fallback_note = result.split("\n")[0]
                return fallback_note + "\n\n" + _diff_text(path_a, path_b, context_lines)
            return result
        elif fmt == "binary":
            return _diff_binary(path_a, path_b)
        else:
            return _diff_text(path_a, path_b, context_lines)
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_data_tools(registry: ToolRegistry) -> int:
    """Register data analysis tools. Returns count."""
    registry.register_category_hint(
        "Data",
        "Data tools for comparing and analyzing files. "
        "diff_files supports text, JSON (semantic), CSV/TSV (row-level), "
        "YAML (semantic), TOML (semantic), and binary (hash comparison). "
        "Format is auto-detected from file extension by default.",
    )
    tools = [diff_files]
    for func in tools:
        registry.register(func, category="Data")
    return len(tools)
