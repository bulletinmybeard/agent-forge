"""On-disk snapshot store for first-class file rollback.

Used by the code_edit tool (pre-edit snapshot) and the revert_file tool
(restore snapshot). Lives in the framework layer so there is no
dependency on web/server/result_store or Redis — any process that runs
AgentLoop can save and load snapshots.

Layout::

    $XDG_CACHE_HOME/agentforge/snapshots/
    └─ <pre_hash>.json.gz      # gzip-compressed JSON envelope

Envelope schema::

    {
        "schema": 1,
        "pre_hash": "<sha256 of original content>",
        "path": "<absolute path at snapshot time>",
        "saved_at": "<ISO 8601 UTC>",
        "tool": "<tool name that took the snapshot>",
        "session_id": "<optional — framework doesn't know it>",
        "content": "<original UTF-8 text>"
    }

Design notes
------------
- Keyed by *pre_hash* (not by path) so the same file at different points
  in time produces distinct snapshots. revert_file looks up by hash
  when the caller passes ``pre_hash=...``, otherwise it picks the newest
  snapshot whose ``path`` field matches the requested file.
- Gzip compression because config files compress 5-10x.
- Stored on local disk rather than Redis so the framework layer has no
  network dependency and works in offline / shell-only contexts.
- Housekeeping: :func:`prune_old` drops snapshots older than ``max_age_days``.
  Caller is responsible for invoking it (e.g., periodic task); we do NOT
  prune on every save to keep the save path deterministic.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_DEFAULT_MAX_AGE_DAYS = 7


# ---------------------------------------------------------------------------
# Directory resolution
# ---------------------------------------------------------------------------


def snapshot_dir() -> Path:
    """Return the directory where snapshots are stored.

    Honours ``$AGENTFORGE_SNAPSHOT_DIR`` for tests, then ``$XDG_CACHE_HOME``,
    then falls back to ``~/.cache``. Creates the directory if missing.
    """
    override = os.environ.get("AGENTFORGE_SNAPSHOT_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
        base = base / "agentforge" / "snapshots"
    base.mkdir(parents=True, exist_ok=True)
    return base


# ---------------------------------------------------------------------------
# Proposal cache — code_edit "propose then apply" (diff-preview confirmation)
# ---------------------------------------------------------------------------

_PROPOSAL_MAX_AGE_SECONDS = 3600  # a proposal is short-lived (the review window)


def _proposal_path(token: str) -> Path:
    if not token or not all(c in "0123456789abcdef" for c in token.lower()):
        raise ValueError(f"invalid proposal token: {token!r}")
    return snapshot_dir() / f"proposal_{token}.json.gz"


def save_proposal(*, token: str, path: str, content: str) -> Path:
    """Cache a proposed (not-yet-written) file edit keyed by *token*.

    Backs code_edit's diff-preview confirmation: the edit is computed once,
    stashed here, and written verbatim on approval — so what the user reviews
    is exactly what lands on disk. Proposals are short-lived; stale ones are
    swept on each write.
    """
    _sweep_stale_proposals()
    dst = _proposal_path(token)
    env = {
        "schema": SCHEMA_VERSION,
        "token": token,
        "path": str(path),
        "content": content,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    dst.write_bytes(gzip.compress(json.dumps(env).encode("utf-8")))
    return dst


def load_proposal(token: str) -> dict | None:
    """Load a cached proposal by token, or None if missing/unreadable."""
    try:
        src = _proposal_path(token)
    except ValueError:
        return None
    if not src.is_file():
        return None
    try:
        return json.loads(gzip.decompress(src.read_bytes()).decode("utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("[file_snapshots] proposal load failed for %s: %s", token[:12], exc)
        return None


def discard_proposal(token: str) -> None:
    """Best-effort delete of a consumed / declined proposal."""
    try:
        _proposal_path(token).unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def _sweep_stale_proposals() -> None:
    """Best-effort removal of proposal files older than the review window."""
    try:
        cutoff = time.time() - _PROPOSAL_MAX_AGE_SECONDS
        for fp in snapshot_dir().glob("proposal_*.json.gz"):
            try:
                if fp.stat().st_mtime < cutoff:
                    fp.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def _snapshot_path(pre_hash: str) -> Path:
    """Resolve the on-disk path for a given *full* content hash.

    This is the strict form — callers that may be passing in a
    user-provided or LLM-generated prefix should use
    :func:`_resolve_snapshot_hash` instead, which falls back to prefix
    matching when the input is shorter than 64 chars.
    """
    if not pre_hash or not all(c in "0123456789abcdef" for c in pre_hash.lower()):
        raise ValueError(f"invalid pre_hash: {pre_hash!r}")
    return snapshot_dir() / f"{pre_hash}.json.gz"


def _resolve_snapshot_hash(hash_or_prefix: str) -> str | None:
    """Resolve a possibly-truncated hash to a full 64-char sha256.

    The revert_file tool's error listings used to show 12-char prefixes
    (via ``pre_hash[:12]``) and LLMs would faithfully echo those back as
    the ``pre_hash`` argument on the next call. That produced
    ``invalid pre_hash`` ValueErrors deep in :func:`_snapshot_path`
    because the strict validator rejects anything other than a full
    hex sha256 of length 64.

    This helper accepts either a full 64-char hash (fast path — direct
    file existence check) or any hex prefix. For prefixes it scans the
    snapshot directory and returns the unique match, or ``None`` if
    there are zero or multiple candidates. Ambiguous matches are logged
    at WARNING so operators can see when a prefix was too short.

    Returns the resolved full hash, or ``None`` if not found / ambiguous.
    """
    if not hash_or_prefix:
        return None

    h = hash_or_prefix.strip().lower()
    if not all(c in "0123456789abcdef" for c in h):
        return None

    # Fast path — full 64-char sha256
    if len(h) == 64:
        if (snapshot_dir() / f"{h}.json.gz").is_file():
            return h
        return None

    # Slow path — scan for files whose stem starts with the prefix.
    # Prefixes shorter than 8 chars are almost certainly typos / fabrications
    # and we refuse to disambiguate them to avoid matching half the store.
    if len(h) < 8:
        logger.debug(
            "[file_snapshots] refusing to resolve prefix %r (too short, min 8 chars)",
            hash_or_prefix,
        )
        return None

    matches: list[str] = []
    try:
        for fp in snapshot_dir().glob(f"{h}*.json.gz"):
            stem = fp.name[: -len(".json.gz")]
            if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
                matches.append(stem)
    except OSError as exc:
        logger.debug("[file_snapshots] prefix scan failed: %s", exc)
        return None

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "[file_snapshots] ambiguous snapshot prefix %r matches %d candidates: %s",
            hash_or_prefix,
            len(matches),
            ", ".join(m[:16] + "…" for m in matches[:5]),
        )
        return None
    return matches[0]


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


def save_snapshot(
    *,
    pre_hash: str,
    path: str,
    content: str,
    tool: str = "code_edit",
    session_id: str | None = None,
) -> Path:
    """Save a snapshot of *content* keyed by *pre_hash*.

    If a snapshot with the same hash already exists (because the same file
    content was snapshotted before — possibly days ago in a prior test
    session), we REFRESH the envelope's ``saved_at`` timestamp so that
    :func:`latest_snapshot_for_path` correctly picks this snapshot as the
    "most recent" on the next revert. The original creation time is
    preserved in ``first_saved_at`` for audit purposes.

    Why this matters — the bug this solves
    --------------------------------------
    Without this refresh, the timeline looks like:

        day 1, 10:00  save_snapshot(hash=X)  → envelope.saved_at = 10:00
        day 1, 11:00  save_snapshot(hash=Y)  → envelope.saved_at = 11:00
        day 2, 00:05  code_edit reads the same file whose content is X
                      → save_snapshot(hash=X) no-op, envelope still 10:00
        day 2, 00:06  revert_file() with no pre_hash
                      → latest_snapshot_for_path returns Y (11:00 > 10:00)
                      → file is "reverted" to the WRONG prior state

    That is exactly what happened in the Job 4 .zshrc restore where
    ``6ad96e34eed7`` (a stale snapshot from the previous test session)
    was picked over ``553bbea33166`` (the one code_edit had just tried
    to save but which already existed on disk from a much earlier run).

    By updating ``saved_at`` on every save attempt, the semantic becomes
    "the most recent code_edit / revert_file that consumed this content"
    — which matches what users mean by "undo my most recent edit".
    """
    target = _snapshot_path(pre_hash)
    now_iso = datetime.now(timezone.utc).isoformat()

    if target.exists():
        # Same content hash — refresh saved_at so ordering in
        # latest_snapshot_for_path reflects this use, not the first
        # time the hash was ever seen. Preserve first_saved_at for audit.
        try:
            with gzip.open(target, "rb") as fh:
                env = json.loads(fh.read().decode("utf-8"))
            if not isinstance(env, dict):
                env = {}
        except Exception:
            logger.exception(
                "[file_snapshots] corrupt envelope at %s, rewriting from scratch",
                target,
            )
            env = {}

        # Preserve original creation time; back-fill from old saved_at if absent
        if "first_saved_at" not in env:
            env["first_saved_at"] = env.get("saved_at", now_iso)
        env["schema"] = SCHEMA_VERSION
        env["pre_hash"] = pre_hash
        env["path"] = str(path)
        env["saved_at"] = now_iso
        env["tool"] = tool
        if session_id:
            env["session_id"] = session_id
        elif "session_id" not in env:
            env["session_id"] = ""
        env["content"] = content  # defensive — should already be identical

        payload = json.dumps(env).encode("utf-8")
        tmp = target.with_name(target.name + ".tmp")
        try:
            with gzip.open(tmp, "wb", compresslevel=6) as fh:
                fh.write(payload)
            os.replace(tmp, target)  # atomic rename
        except Exception:
            logger.exception("[file_snapshots] failed to refresh %s", target)
            # Last-resort: at least bump mtime so we don't completely lose
            # ordering information on filesystems where mtime-sort works.
            try:
                os.utime(target, None)
            except OSError:
                pass
            try:
                tmp.unlink()
            except OSError:
                pass
        else:
            logger.debug(
                "[file_snapshots] refreshed %s (saved_at bumped to %s)",
                target.name,
                now_iso,
            )
        return target

    envelope = {
        "schema": SCHEMA_VERSION,
        "pre_hash": pre_hash,
        "path": str(path),
        "saved_at": now_iso,
        "first_saved_at": now_iso,
        "tool": tool,
        "session_id": session_id or "",
        "content": content,
    }
    payload = json.dumps(envelope).encode("utf-8")

    # Atomic write: stream into a .tmp sibling, then rename into place
    tmp = target.with_name(target.name + ".tmp")
    try:
        with gzip.open(tmp, "wb", compresslevel=6) as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except Exception:
        logger.exception("[file_snapshots] failed to save %s", target)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    logger.debug(
        "[file_snapshots] saved %s (%d bytes gz) for %s",
        target.name,
        target.stat().st_size,
        path,
    )
    return target


def load_snapshot(pre_hash: str) -> dict | None:
    """Load a snapshot by content hash. Returns the envelope dict or ``None``.

    Accepts either a full 64-char sha256 or a hex prefix of 8+ chars that
    uniquely identifies one snapshot. Prefix matching exists because LLMs
    frequently copy truncated hashes out of error messages / UI and pass
    them back in verbatim.
    """
    full = _resolve_snapshot_hash(pre_hash)
    if full is None:
        return None
    target = snapshot_dir() / f"{full}.json.gz"
    if not target.is_file():
        return None
    try:
        with gzip.open(target, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except Exception:
        logger.exception("[file_snapshots] failed to load %s", target)
        return None


def latest_snapshot_for_path(path: str) -> dict | None:
    """Return the most recent snapshot envelope whose stored path matches *path*.

    Used by ``revert_file`` when the caller did not specify a ``pre_hash``.
    Ordering key is ``(saved_at, mtime)`` (both newest first). ``saved_at`` is
    an ISO 8601 UTC string with microsecond precision emitted by
    :func:`save_snapshot`, so it's a more reliable sort key than filesystem
    mtime — which can have 1-second granularity on some filesystems and is
    additionally bumped by idempotent re-saves via :func:`os.utime`.
    """
    resolved = str(Path(os.path.expanduser(path)).resolve())
    candidates: list[tuple[str, float, dict]] = []
    for fp in snapshot_dir().glob("*.json.gz"):
        try:
            with gzip.open(fp, "rb") as fh:
                env = json.loads(fh.read().decode("utf-8"))
        except Exception:
            continue
        if env.get("path") != resolved:
            continue
        try:
            mtime = fp.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((str(env.get("saved_at", "")), mtime, env))
    if not candidates:
        return None
    # Newest first: primary = saved_at ISO string, secondary = fs mtime
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return candidates[0][2]


def list_snapshots_for_path(path: str) -> list[dict]:
    """Return metadata (no content) for every snapshot matching *path*, newest first."""
    resolved = str(Path(os.path.expanduser(path)).resolve())
    out: list[dict] = []
    for fp in snapshot_dir().glob("*.json.gz"):
        try:
            with gzip.open(fp, "rb") as fh:
                env = json.loads(fh.read().decode("utf-8"))
        except Exception:
            continue
        if env.get("path") != resolved:
            continue
        out.append(
            {
                "pre_hash": env.get("pre_hash", ""),
                "path": env.get("path", ""),
                "saved_at": env.get("saved_at", ""),
                "first_saved_at": env.get("first_saved_at", env.get("saved_at", "")),
                "tool": env.get("tool", ""),
                "size": fp.stat().st_size,
                "mtime": fp.stat().st_mtime,
            }
        )
    # Primary sort: envelope saved_at (ISO 8601, microsecond precision);
    # secondary: filesystem mtime. Both newest first.
    out.sort(key=lambda d: (d["saved_at"], d["mtime"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def prune_old(max_age_days: int = _DEFAULT_MAX_AGE_DAYS) -> int:
    """Delete snapshots older than *max_age_days* (by mtime). Returns count deleted."""
    cutoff = time.time() - max_age_days * 86400
    deleted = 0
    for fp in snapshot_dir().glob("*.json.gz"):
        try:
            if fp.stat().st_mtime < cutoff:
                fp.unlink()
                deleted += 1
        except OSError:
            continue
    if deleted:
        logger.info("[file_snapshots] pruned %d snapshots older than %d days", deleted, max_age_days)
    return deleted
