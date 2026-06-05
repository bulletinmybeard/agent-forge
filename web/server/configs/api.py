"""Configs dashboard REST API — read-only YAML viewer.

Mounted under ``/api/configs``. Resolves files relative to the process
working directory so the same endpoint works on the Mac dev host and
inside the remote containers (where the bind-mount surfaces the deployed
copy at /app/<name>).

Whitelist enforced — no path traversal, no arbitrary file reads.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/configs", tags=["configs"])

# Secret redaction for the viewer. These configs hold API keys, DB URLs with
# credentials, Slack/provider tokens — never return them verbatim.
_SECRET_SUBSTRINGS = ("password", "passwd", "secret", "token", "apikey", "api_key", "credential")
_SECRET_SUFFIXES = ("_key", "_token", "_secret", "_password")
# user:pass@host inside a connection URL, regardless of the key name.
_URL_CREDS_RE = re.compile(r"://([^:@/\s]+):([^@/\s]+)@")
# A `key: value` YAML line (captures indent, key, separator, value).
_KV_RE = re.compile(r"^(\s*)([^:#\s][^:]*?)(\s*:\s*)(.+?)(\s*)$")
# A `key:` line with no inline value — opens a block (list/map of children).
_BARE_KEY_RE = re.compile(r"^(\s*)([^:#\s][^:]*?)\s*:\s*(#.*)?$")
_REDACTED = "'***redacted***'"


def _is_secret_key(key: str) -> bool:
    k = key.strip().strip("\"'").lower()
    if k == "key":  # bare 'key' commonly holds a provider API key here
        return True
    if any(s in k for s in _SECRET_SUBSTRINGS):
        return True
    return any(k.endswith(suf) for suf in _SECRET_SUFFIXES)


_EMPTY_VALUES = ("", "''", '""', "[]", "{}", "null", "~")


def _redact(content: str) -> str:
    """Mask secret values + URL credentials, preserving structure and comments.

    Handles inline scalars (``token: abc``), connection-URL credentials, and
    secret *blocks* — e.g., a list under ``api_keys:`` whose items must also be
    masked even though they aren't ``key: value`` lines.
    """
    out: list[str] = []
    block_indent: int | None = None  # indent of an active secret block; its children are secret
    for raw in content.splitlines():
        line = _URL_CREDS_RE.sub(r"://\1:***@", raw)
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Leave the active secret block once indentation returns to its level.
        if block_indent is not None and stripped and indent <= block_indent:
            block_indent = None

        # Inside a secret block: redact list items and nested scalar values.
        if block_indent is not None and indent > block_indent and stripped and not stripped.startswith("#"):
            if stripped.startswith("- "):
                out.append(f"{line[:indent]}- {_REDACTED}")
                continue
            m = _KV_RE.match(line)
            if m and m.group(4).strip() not in _EMPTY_VALUES:
                out.append(f"{m.group(1)}{m.group(2)}{m.group(3)}{_REDACTED}")
                continue
            out.append(line)
            continue

        m = _KV_RE.match(line)
        if m and _is_secret_key(m.group(2)):
            if m.group(4).strip() not in _EMPTY_VALUES:
                line = f"{m.group(1)}{m.group(2)}{m.group(3)}{_REDACTED}"
            out.append(line)
            continue

        bm = _BARE_KEY_RE.match(line)
        if bm and _is_secret_key(bm.group(2)):
            block_indent = indent  # a list/map of secret values follows
        out.append(line)
    return "\n".join(out)


# Whitelist of file names. The user-facing identifier matches the file name
# verbatim; lookups join against cwd.
_ALLOWED: tuple[str, ...] = (
    "config.yaml",
    "framework-config.yaml",
    "tool_routing.yaml",
    "work_log.yaml",
)


def _resolve(name: str) -> Path:
    """Return the resolved path for *name* if whitelisted, else 404."""
    if name not in _ALLOWED:
        raise HTTPException(status_code=404, detail=f"Unknown config: {name!r}")
    return Path.cwd() / name


def _stat(path: Path) -> dict:
    st = path.stat()
    return {
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


@router.get("")
def list_configs() -> dict:
    """List the whitelisted configs with size and mtime where they exist on disk."""
    items: list[dict] = []
    for name in _ALLOWED:
        path = Path.cwd() / name
        entry: dict = {"name": name, "exists": path.exists()}
        if path.exists():
            entry.update(_stat(path))
        items.append(entry)
    return {"configs": items, "cwd": str(Path.cwd())}


@router.get("/{name}")
def get_config(name: str) -> dict:
    """Return the full content of *name* as UTF-8 text."""
    path = _resolve(name)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"{name} not found at {path}",
        )
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        raise HTTPException(status_code=500, detail=f"Read failed: {exc}") from exc
    return {"name": name, "content": _redact(content), "redacted": True, **_stat(path)}
