"""ast-grep subprocess wrapper used by named codemods.

The named ops in this package don't drive ast-grep directly — they go through this tiny module so we have ONE place that:

- caches the ``which sg`` lookup at import time (no re-lookup per call)
- enforces a byte-cap on rg/sg output so a runaway match set can't OOM us
- validates the search root stays at-or-below cwd (no `..` escapes)
- raises ``CodemodError`` instead of leaking subprocess details

Mirrors the safety posture of ``_run_rg`` in ``coding_tools.py`` — same ``capture_output=True`` + ``check=False`` + explicit timeout pattern.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)


_SG_BIN: str | None = None
_SG_LOOKED_UP = False

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_OUTPUT_BYTES = 5 * 1024 * 1024


class CodemodError(RuntimeError):
    """Raised when ast-grep fails or its output isn't parseable."""


def sg_available() -> bool:
    """Return True if the ``sg`` binary is on PATH.

    Cached after the first call — `shutil.which` re-stats PATH each time
    otherwise. Cache survives until the process exits.
    """
    global _SG_BIN, _SG_LOOKED_UP
    if _SG_LOOKED_UP:
        return _SG_BIN is not None
    _SG_BIN = shutil.which("sg") or shutil.which("ast-grep")
    _SG_LOOKED_UP = True
    if _SG_BIN is None:
        logger.info(
            "[codemod] ast-grep ('sg') not on PATH — codemod ops will surface "
            "an install hint and fall back to the transform strategy.",
        )
    return _SG_BIN is not None


def _validate_root(root: str) -> Path:
    p = Path(root).expanduser().resolve()
    if not p.exists():
        raise CodemodError(f"codemod root does not exist: {root!r}")
    cwd = Path.cwd().resolve()
    # Allow root == cwd or any descendant. Prevents the planner from
    # pointing the codemod at ``/`` or some unrelated tree.
    try:
        p.relative_to(cwd)
    except ValueError as exc:
        raise CodemodError(f"codemod root {root!r} is outside cwd {cwd} — refusing for safety") from exc
    return p


def sg_scan_rewrite(
    rule_yaml: str,
    root: str,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> list[dict[str, Any]]:
    """Run ``sg scan --rule <file> --json=stream`` and return parsed matches.

    Each match dict carries (at minimum) ``file`` and a ``replacement``
    /``fix`` block when the rule has a fix. Callers do the file-level
    splice themselves so snapshot + rollback bookkeeping stays in one
    place rather than smeared across each op.

    ``rule_yaml`` is the rule file content; this function writes it to a
    NamedTemporaryFile so the binary reads from disk (sg's --inline-rules
    flag has surprising YAML quoting rules across shells).
    """
    if not sg_available():
        raise CodemodError(
            "ast-grep (sg) not installed — install via 'brew install ast-grep' "
            "or 'cargo install ast-grep' and retry, or rephrase the prompt to "
            "route through code_transform."
        )

    root_path = _validate_root(root)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yml",
        delete=False,
        encoding="utf-8",
    ) as fh:
        fh.write(rule_yaml)
        rule_path = fh.name

    try:
        cmd = [_SG_BIN or "sg", "scan", "--rule", rule_path, "--json=stream", str(root_path)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodemodError(f"ast-grep timed out after {timeout_seconds}s on {root_path}") from exc

        if proc.returncode not in (0, 1):  # 1 = no matches in some sg builds
            err = (proc.stderr or "").strip()[:400]
            raise CodemodError(f"ast-grep exited {proc.returncode}: {err or '(no stderr)'}")

        out = proc.stdout or ""
        if len(out.encode("utf-8")) > max_output_bytes:
            raise CodemodError(
                f"ast-grep output exceeded {max_output_bytes} bytes — narrow the rule or the search root"
            )

        matches: list[dict[str, Any]] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("[codemod] skipping non-JSON sg line: %r", line[:120])
                continue
            if isinstance(rec, list):
                matches.extend(r for r in rec if isinstance(r, dict))
            elif isinstance(rec, dict):
                matches.append(rec)
        return matches
    finally:
        try:
            os.unlink(rule_path)
        except OSError:
            pass


__all__ = ["CodemodError", "sg_available", "sg_scan_rewrite"]
