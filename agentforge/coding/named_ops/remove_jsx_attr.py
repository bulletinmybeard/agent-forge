"""``remove_jsx_attr`` — strip every attribute whose name matches a regex
from instances of a named JSX component.

Example: ``remove_jsx_attr(component="Card", attr_pattern="^data-")`` turns

    <Card data-test="x" id="root" data-id={1} onClick={fn}>
        <Inner />
    </Card>
    <Card data-only="1" />

into

    <Card id="root" onClick={fn}>
        <Inner />
    </Card>
    <Card />

regardless of whether the source was single-line, self-closing, or
ruff-reflowed across multiple lines — that's the whole point of going
through an AST tool instead of regex.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from chalkbox.logging.bridge import get_logger
from pydantic import Field

from agentforge.coding.named_ops import NamedOpParams, NamedOpResult, register
from agentforge.coding.named_ops._sg import (
    _SG_BIN,  # set by sg_available()
    CodemodError,
    sg_available,
    sg_scan_rewrite,
)

logger = get_logger(__name__)


class RemoveJsxAttrParams(NamedOpParams):
    component: str = Field(..., min_length=1, description="JSX tag name, e.g., 'Card'")
    attr_pattern: str = Field(..., min_length=1, description="Regex over attribute names, e.g., '^data-'")
    glob: str = Field(default="**/*.{jsx,tsx}", description="File glob")
    path: str = Field(default=".", description="Search root")


def _build_rule(component: str, attr_pattern: str) -> str:
    """Build an ast-grep rule (YAML) that matches and removes attrs.

    Two pattern alternatives cover the JSX attribute shapes:

    1. ``$NAME=$VALUE`` — covers ``data-x="1"``, ``data-x={1}``, etc.
    2. ``$NAME`` matched as a ``jsx_attribute`` node — covers boolean
       shorthand attributes (``data-active`` with no value).

    The ``inside:`` constraint scopes to opening or self-closing elements
    of the named component. ``constraints.NAME.regex`` filters the
    attribute identifier by the caller-supplied regex.

    Note: ast-grep rule syntax tends to vary slightly across versions.
    If a rule fails to match in your environment, run ``sg scan --rule
    <rulefile> --debug-query`` against a known-good fixture to inspect
    what the parser actually accepts. We deliberately keep the rule
    small so it's easy to tweak.
    """
    # YAML-escape the component name (we forbid anything but identifier
    # chars at the param-validation layer, so a simple braced string is
    # safe).
    safe_component = re.sub(r"[^A-Za-z0-9_$.]", "", component)
    # Anchor the component-name regex so partial matches don't fire.
    component_regex = f"^{re.escape(safe_component)}$"
    return f"""id: remove-jsx-attr
language: tsx
rule:
  any:
    - pattern: $NAME=$VALUE
    - kind: jsx_attribute
  inside:
    any:
      - kind: jsx_opening_element
      - kind: jsx_self_closing_element
    has:
      field: name
      regex: {component_regex!r}
constraints:
  NAME:
    regex: {attr_pattern!r}
fix: ""
"""


def _files_with_matches(matches: list[dict[str, Any]]) -> list[str]:
    """Pull the unique list of file paths out of sg's JSON match stream."""
    seen: dict[str, None] = {}
    for m in matches:
        f = m.get("file") or m.get("path")
        if isinstance(f, str) and f and f not in seen:
            seen[f] = None
    return list(seen)


def _apply_update_all(rule_yaml: str, file_path: str, *, timeout: int = 60) -> None:
    """Re-run sg with ``--update-all`` on a single file to write the fix.

    Discovery and write are split into two sg invocations: discovery to
    learn which files to snapshot, then ``--update-all`` per-file so a
    write failure on file 5 doesn't leave files 1-4 unsnapshotted.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yml",
        delete=False,
        encoding="utf-8",
    ) as fh:
        fh.write(rule_yaml)
        rule_path = fh.name

    try:
        cmd = [_SG_BIN or "sg", "scan", "--rule", rule_path, "--update-all", file_path]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if proc.returncode not in (0, 1):
            raise CodemodError(f"sg --update-all failed on {file_path}: {(proc.stderr or '').strip()[:200]}")
    finally:
        try:
            Path(rule_path).unlink()
        except OSError:
            pass


class RemoveJsxAttr:
    """Named op — register at module import."""

    name = "remove_jsx_attr"
    description = (
        "Strip every attribute whose name matches `attr_pattern` from "
        "instances of `<component>`. AST-aware, handles single-line, "
        "self-closing, and multi-line reflowed JSX equivalently."
    )
    param_schema: type[NamedOpParams] = RemoveJsxAttrParams

    def run(
        self,
        params: NamedOpParams,
        *,
        session_id: str,
        burst_id: str,
    ) -> NamedOpResult:
        if not isinstance(params, RemoveJsxAttrParams):
            return NamedOpResult(
                ok=False,
                error=f"params must be RemoveJsxAttrParams, got {type(params).__name__}",
            )

        if not sg_available():
            return NamedOpResult(
                ok=False,
                error=(
                    "ast-grep (sg) not installed — install via 'brew install "
                    "ast-grep' (macOS) or 'cargo install ast-grep' (linux), "
                    "then re-run. Fall back to code_transform if install isn't "
                    "an option."
                ),
            )

        rule_yaml = _build_rule(params.component, params.attr_pattern)

        try:
            matches = sg_scan_rewrite(rule_yaml, params.path)
        except CodemodError as exc:
            return NamedOpResult(ok=False, error=str(exc))

        affected = _files_with_matches(matches)
        if not affected:
            logger.info(
                "[remove_jsx_attr] no matches for component=%s attr_pattern=%s in %s",
                params.component,
                params.attr_pattern,
                params.path,
            )
            return NamedOpResult(ok=True, files_touched=[], sites_changed=0)

        # Lazy imports — keep the registry-population path cheap so tests
        # that don't touch the rollback store don't have to mock Redis.
        from agentforge.coding.rollback import get_rollback_store
        from agentforge.tools._file_snapshots import save_snapshot

        snapshot_ids: list[str] = []
        files_touched: list[str] = []
        sites_changed = 0

        for file_path in affected:
            p = Path(file_path)
            try:
                original = p.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("[remove_jsx_attr] skipping %s: %s", file_path, exc)
                continue

            pre_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
            try:
                save_snapshot(
                    pre_hash=pre_hash,
                    path=str(p.resolve()),
                    content=original,
                    tool="code_codemod",
                    session_id=session_id,
                )
            except Exception as exc:
                logger.warning("[remove_jsx_attr] snapshot failed for %s: %s", file_path, exc)
                continue

            try:
                _apply_update_all(rule_yaml, file_path)
            except CodemodError as exc:
                logger.warning("[remove_jsx_attr] update-all failed for %s: %s", file_path, exc)
                continue

            try:
                new_content = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if new_content == original:
                # sg said this file had matches in the discovery phase but
                # update-all was a no-op — likely a rule/parser mismatch.
                # Drop the snapshot from the rollback set so undo doesn't
                # "revert" a file we didn't actually change.
                logger.info(
                    "[remove_jsx_attr] %s unchanged after update-all (sg matched but didn't fix)",
                    file_path,
                )
                continue

            # Count sites by counting matches sg reported for this file.
            sites_changed += sum(1 for m in matches if (m.get("file") or m.get("path")) == file_path)
            files_touched.append(file_path)
            snapshot_ids.append(pre_hash)

        # Register the burst for undo via the same key shape code_apply uses.
        if snapshot_ids:
            try:
                from agentforge.config import get_config

                ttl = int(get_config().get("coding.snapshot_ttl_seconds", 86400) or 86400)
            except Exception:
                ttl = 86400
            try:
                store = get_rollback_store()
                store.store_burst(
                    session_id=session_id,
                    burst_id=burst_id,
                    snapshot_ids=snapshot_ids,
                    ttl_seconds=ttl,
                )
            except Exception as exc:
                logger.warning("[remove_jsx_attr] rollback registration failed: %s", exc)

        logger.info(
            "[remove_jsx_attr] burst=%s session=%s files=%d sites=%d",
            burst_id,
            session_id,
            len(files_touched),
            sites_changed,
        )
        return NamedOpResult(
            ok=True,
            files_touched=files_touched,
            sites_changed=sites_changed,
            snapshot_ids=snapshot_ids,
        )


# Register at import time so framework.coding.named_ops.REGISTRY is
# populated as soon as the package is imported.
register(RemoveJsxAttr())


__all__ = ["RemoveJsxAttr", "RemoveJsxAttrParams"]
