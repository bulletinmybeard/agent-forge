"""Cloud storage tools - Put.io and Premiumize.me management.

Provides tools for browsing, searching, downloading, and transferring files
on Put.io and Premiumize.me cloud storage services.

Configuration (config.yaml or environment variables)::

    cloud:
      putio:
        oauth_token: "YOUR_PUTIO_TOKEN"   # or PUTIO_TOKEN env var
      premiumize:
        api_key: "YOUR_PM_KEY"            # or PREMIUMIZE_API_KEY env var

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.cloud_tools import register_cloud_tools

    registry = ToolRegistry()
    register_cloud_tools(registry)
"""

from __future__ import annotations

import json
import os
import re
import ssl
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode, urlparse
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PUTIO_BASE = "https://api.put.io/v2"
_PM_BASE = "https://www.premiumize.me/api"
_REQUEST_TIMEOUT = 20  # seconds


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_putio_token() -> str:
    """Return the Put.io OAuth token from the ``PUTIO_TOKEN`` environment variable."""
    return os.environ.get("PUTIO_TOKEN", "")


def _get_premiumize_key() -> str:
    """Return the Premiumize.me API key from the ``PREMIUMIZE_API_KEY`` environment variable."""
    return os.environ.get("PREMIUMIZE_API_KEY", "")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_get(url: str, headers: dict | None = None) -> dict:
    """Perform a GET request and return the parsed JSON response."""
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _http_post(url: str, data: dict, headers: dict | None = None) -> dict:
    """Perform a POST request with form-encoded data and return parsed JSON."""
    encoded = urlencode(data).encode()
    req = Request(url, data=encoded, headers=headers or {})
    with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Put.io helpers
# ---------------------------------------------------------------------------


def _putio_get(path: str, params: dict | None = None) -> dict:
    token = _get_putio_token()
    if not token:
        raise ValueError(
            "Put.io token not configured (set cloud.putio.oauth_token in config.yaml or PUTIO_TOKEN env var)"
        )
    qs = f"?{urlencode(params)}" if params else ""
    url = f"{_PUTIO_BASE}{path}{qs}"
    return _http_get(url, headers={"Authorization": f"Bearer {token}"})


def _putio_post(path: str, data: dict | None = None) -> dict:
    token = _get_putio_token()
    if not token:
        raise ValueError(
            "Put.io token not configured (set cloud.putio.oauth_token in config.yaml or PUTIO_TOKEN env var)"
        )
    url = f"{_PUTIO_BASE}{path}"
    return _http_post(url, data=data or {}, headers={"Authorization": f"Bearer {token}"})


def _fmt_size(size_bytes: int) -> str:
    """Format bytes to human-readable size string."""
    if size_bytes == 0:
        return "0 B"
    size: float = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} PB"


def _fmt_putio_file(f: dict) -> dict:
    """Format a Put.io file dict into a clean summary.

    Keeps the JSON compact so large folders (200+ items) fit inside the
    agent's per-tool output cap. ``size_bytes`` is dropped (redundant with
    the human-readable ``size``); ``web_url`` is dropped (computable from
    ``id`` as ``https://put.io/files/{id}`` on demand, no need to ship it
    on every item).
    """
    size = f.get("size", 0)
    file_id = f.get("id")
    return {
        "id": file_id,
        "name": f.get("name", ""),
        "type": f.get("file_type", ""),
        "size": _fmt_size(size),
        "parent_id": f.get("parent_id"),
        "created_at": f.get("created_at", ""),
    }


# ---------------------------------------------------------------------------
# Premiumize helpers
# ---------------------------------------------------------------------------

# Premiumize takes the key as ``apikey`` in the query string (GET) - it ends up
# in the request URL, so scrub it before anything is logged or returned.
_PM_KEY_RE = re.compile(r"(apikey=)[^&\s]*")


def _redact_key_in_url(url: str) -> str:
    """Replace the Premiumize ``apikey`` value in a URL/string with ``***``."""
    return _PM_KEY_RE.sub(r"\1***", url)


def _pm_get(path: str, params: dict | None = None) -> dict:
    key = _get_premiumize_key()
    if not key:
        raise ValueError(
            "Premiumize API key not configured (set cloud.premiumize.api_key in config.yaml or PREMIUMIZE_API_KEY env var)"
        )
    p = dict(params or {})
    p["apikey"] = key
    url = f"{_PM_BASE}{path}?{urlencode(p)}"
    try:
        return _http_get(url)
    except (HTTPError, URLError) as exc:
        # HTTPError.url / URLError.filename carry the key-bearing request URL.
        # Scrub it so it can't leak through callers that stringify the error.
        try:
            url = getattr(exc, "url", None)
            if isinstance(url, str):
                setattr(exc, "url", _redact_key_in_url(url))
            filename = getattr(exc, "filename", None)
            if isinstance(filename, str):
                setattr(exc, "filename", _redact_key_in_url(filename))
        except Exception:  # noqa: BLE001 - never let scrubbing mask the real error
            pass
        raise


def _pm_post(path: str, data: dict | None = None) -> dict:
    key = _get_premiumize_key()
    if not key:
        raise ValueError(
            "Premiumize API key not configured (set cloud.premiumize.api_key in config.yaml or PREMIUMIZE_API_KEY env var)"
        )
    d = dict(data or {})
    d["apikey"] = key
    url = f"{_PM_BASE}{path}"
    return _http_post(url, data=d)


def _fmt_pm_item(item: dict) -> dict:
    """Format a Premiumize folder item into a clean summary."""
    size = item.get("size", 0) or 0
    created = item.get("created_at")
    if created:
        try:
            created = datetime.fromtimestamp(int(created)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "type": item.get("type", ""),
        "size": _fmt_size(size),
        "size_bytes": size,
        "link": item.get("link", ""),
        "stream_link": item.get("stream_link", ""),
        "created_at": created,
    }


# ===========================================================================
# PUT.IO TOOLS
# ===========================================================================


@tool(locality="remote")
def putio_list_files(
    parent_id: int = 0,
    limit: int = 0,
) -> str:
    """List ALL files and folders in a Put.io folder (one level, no recursion).

    Walks Put.io's cursor-based pagination so the full folder is returned,
    not just the first page. The previous behaviour silently capped at the
    API's default page size and reported that page count as the total -
    don't trust an old "total" value from a tool result that pre-dates this
    change.

    For RECURSIVE listings (everything inside a folder including sub-trees),
    use ``putio_list_recursive`` instead - it walks the tree server-side in a
    single tool call and returns a flat compact text listing. Calling
    ``putio_list_files`` per subfolder by hand is brittle and frequently
    fails to honour "list X recursively" prompts.

    Returns: JSON with the full ``files`` list plus ``total`` (actual count
    returned), ``pages_fetched`` and ``more_pages_available`` (true only if
    the safety cap kicked in before exhausting the cursor - extremely rare
    folders only).
    """
    _MAX_PAGES = 50  # safety stop - at per_page=1000, this is 50,000 items

    try:
        try:
            limit = int(limit) if limit else 0
        except (TypeError, ValueError):
            limit = 0
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            parent_id = 0

        all_files: list[dict] = []
        # First page - explicit per_page so we don't depend on a moving API default.
        data = _putio_get(
            "/files/list",
            params={"parent_id": parent_id, "per_page": 1000},
        )
        all_files.extend(data.get("files", []))
        cursor = data.get("cursor")
        pages = 1

        # Subsequent pages via cursor. Put.io v2 uses GET /files/list/continue.
        while cursor and pages < _MAX_PAGES:
            if limit and len(all_files) >= limit:
                break
            data = _putio_get("/files/list/continue", params={"cursor": cursor})
            page_files = data.get("files", [])
            if not page_files:
                break
            all_files.extend(page_files)
            cursor = data.get("cursor")
            pages += 1

        if not all_files:
            return json.dumps(
                {
                    "status": "empty",
                    "message": "No files found in this folder",
                    "parent_id": parent_id,
                }
            )

        if limit and len(all_files) > limit:
            all_files = all_files[:limit]

        files_out = [_fmt_putio_file(f) for f in all_files]
        return json.dumps(
            {
                "status": "ok",
                "parent_id": parent_id,
                "total": len(files_out),
                "pages_fetched": pages,
                "more_pages_available": bool(cursor) and pages >= _MAX_PAGES,
                "files": files_out,
            },
            indent=2,
        )
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_list_files error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def putio_search_files(
    query: str,
    limit: int = 0,
) -> str:
    """Search ALL files on Put.io by name or keyword (cursor-paginated).

    Walks Put.io's cursor-based pagination so the full match set is
    returned, not just the first page. The previous behaviour silently
    capped at 30 hits and the agent treated that as the true total.

    Returns: JSON with the full ``files`` list plus ``total`` (actual count
    returned), ``pages_fetched`` and ``more_pages_available`` (true only if
    the safety cap kicked in before exhausting the cursor).
    """
    _MAX_PAGES = 50  # safety stop - at per_page=1000, this is 50,000 hits

    try:
        try:
            limit = int(limit) if limit else 0
        except (TypeError, ValueError):
            limit = 0

        all_files: list[dict] = []
        data = _putio_get(
            "/files/search",
            params={"query": query, "per_page": 1000},
        )
        all_files.extend(data.get("files", []))
        cursor = data.get("cursor")
        pages = 1

        while cursor and pages < _MAX_PAGES:
            if limit and len(all_files) >= limit:
                break
            data = _putio_get("/files/search/continue", params={"cursor": cursor})
            page_files = data.get("files", [])
            if not page_files:
                break
            all_files.extend(page_files)
            cursor = data.get("cursor")
            pages += 1

        if not all_files:
            return json.dumps(
                {
                    "status": "no_results",
                    "query": query,
                    "message": "No files matched your search",
                }
            )

        if limit and len(all_files) > limit:
            all_files = all_files[:limit]

        files_out = [_fmt_putio_file(f) for f in all_files]
        return json.dumps(
            {
                "status": "ok",
                "query": query,
                "total": len(files_out),
                "pages_fetched": pages,
                "more_pages_available": bool(cursor) and pages >= _MAX_PAGES,
                "files": files_out,
            },
            indent=2,
        )
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_search_files error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


def _putio_walk_pages(parent_id: int) -> list[dict]:
    """Walk Put.io's cursor-based pagination for one folder, returning every file."""
    files: list[dict] = []
    data = _putio_get("/files/list", params={"parent_id": parent_id, "per_page": 1000})
    files.extend(data.get("files", []))
    cursor = data.get("cursor")
    pages = 1
    while cursor and pages < 50:
        data = _putio_get("/files/list/continue", params={"cursor": cursor})
        page = data.get("files", [])
        if not page:
            break
        files.extend(page)
        cursor = data.get("cursor")
        pages += 1
    return files


@tool(locality="remote")
def putio_list_recursive(
    parent_id: int = 0,
    max_depth: int = 0,
    max_items: int = 0,
    save_to: str = "",
    only_empty_folders: bool = False,
) -> str:
    """List the FULL recursive contents of a Put.io folder as a flat text tree.

    Walks the entire folder tree starting from ``parent_id``, paginating each
    level via Put.io's cursor API, and returns a compact one-line-per-item
    listing with full paths and sizes. Use this instead of calling
    ``putio_list_files`` repeatedly for each subfolder - the LLM doesn't have
    to make recursion decisions, the tool does it server-side in one call.

    REQUIRED WORKFLOW when the user names a specific folder (e.g., "Movies"):
      1. Call ``putio_list_files(parent_id=0)`` to list the root and locate
         the folder by name in the output. Note its ``id`` field.
      2. Call ``putio_list_recursive(parent_id=<that_id>, save_to=...)``.
      Do NOT call ``putio_list_recursive(parent_id=0)`` and walk the entire
      account when the user only asked about one subfolder - that wastes the
      output budget on irrelevant data.

    To save the result to a local file, prefer the ``save_to`` argument over
    the two-step ``putio_list_recursive(...)`` -> ``write_file(...)`` flow.
    The two-step flow requires the model to re-emit the entire listing as a
    string argument to write_file, which often blows past the model's output
    token budget for trees >10K characters. With ``save_to``, the tool
    cross-dispatches the file write server-side and only returns a short
    confirmation.

    Do NOT shell-redirect; ``shell("putio_list_recursive ... > file")`` is
    not a valid invocation (these are tool functions, not CLI binaries).
    """
    _DEPTH_HARD_STOP = 50
    _ITEM_HARD_STOP = 20000

    try:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            parent_id = 0
        try:
            max_depth = int(max_depth)
        except (TypeError, ValueError):
            max_depth = 0
        try:
            max_items = int(max_items)
        except (TypeError, ValueError):
            max_items = 0

        # Resolve the starting folder name so the tree is rooted somewhere
        # human-readable instead of the bare numeric id.
        if parent_id == 0:
            root_name = ""
        else:
            try:
                info = _putio_get(f"/files/{parent_id}")
                root_name = (info.get("file") or {}).get("name", f"folder_{parent_id}")
            except Exception:
                root_name = f"folder_{parent_id}"

        # Collect raw entries during the walk so we can optionally
        # post-filter (e.g., only_empty_folders=True drops files + folders
        # whose subtree contains any file).
        entries: list[tuple[str, str, int]] = []  # (kind, path, size_bytes)
        folders_count = 0
        files_count = 0
        truncated = False

        def _format_line(prefix: str, path: str, size_bytes: int) -> str:
            # Path-padded to 70 chars then size right-aligned for readability.
            size_str = _fmt_size(size_bytes) if size_bytes else ""
            return f"{prefix:<10}{path:<70} {size_str:>12}".rstrip()

        # Per-folder errors during the walk (404 on a stale subfolder, network
        # blip on one of N pages, etc.) are recorded here and surfaced in the
        # footer rather than killing the whole tool. A single bad folder out
        # of 207 should not turn a successful root walk into "404 not found".
        walk_errors: list[str] = []

        def _walk(folder_id: int, path_prefix: str, depth: int) -> None:
            nonlocal folders_count, files_count, truncated
            if depth >= _DEPTH_HARD_STOP:
                return
            if max_depth and depth >= max_depth:
                return
            if max_items and (folders_count + files_count) >= max_items:
                truncated = True
                return
            if (folders_count + files_count) >= _ITEM_HARD_STOP:
                truncated = True
                return

            try:
                page_entries = _putio_walk_pages(folder_id)
            except Exception as page_exc:  # noqa: BLE001 - walk-tolerant
                msg = f"{path_prefix or '/'} (id={folder_id}): {type(page_exc).__name__}: {str(page_exc)[:120]}"
                walk_errors.append(msg)
                logger.warning(
                    "putio_list_recursive: skipping folder due to error - %s",
                    msg,
                )
                return

            for entry in page_entries:
                if truncated:
                    return
                name = entry.get("name", "?")
                size = entry.get("size", 0)
                is_dir = (entry.get("file_type") or "").upper() == "FOLDER"
                full_path = f"{path_prefix}/{name}" if path_prefix else f"/{name}"
                if is_dir:
                    folders_count += 1
                    entries.append(("[FOLDER]", full_path + "/", size))
                    _walk(int(entry.get("id", 0)), full_path, depth + 1)
                else:
                    files_count += 1
                    entries.append(("[FILE]", full_path, size))
                    if max_items and (folders_count + files_count) >= max_items:
                        truncated = True
                        return
                    if (folders_count + files_count) >= _ITEM_HARD_STOP:
                        truncated = True
                        return

        logger.info(
            "putio_list_recursive: starting walk parent_id=%s "
            "(only_empty_folders=%s, save_to=%r, max_depth=%s, max_items=%s)",
            parent_id,
            bool(only_empty_folders),
            save_to,
            max_depth,
            max_items,
        )
        _walk(parent_id, "" if parent_id == 0 else f"/{root_name}", 0)
        logger.info(
            "putio_list_recursive: walk done - %d folders, %d files, %d errors",
            folders_count,
            files_count,
            len(walk_errors),
        )

        # Post-filter: only_empty_folders keeps folders whose subtree contains
        # zero files. Drops every [FILE] entry and every [FOLDER] that has at
        # least one descendant file at any depth.
        empty_folders_count = 0
        if only_empty_folders:
            file_paths = {p for kind, p, _ in entries if kind == "[FILE]"}
            filtered: list[tuple[str, str, int]] = []
            for kind, path, size in entries:
                if kind == "[FILE]":
                    continue
                # path has trailing "/"; trim before prefix-matching files
                folder_root = path.rstrip("/") + "/"
                has_descendant = any(fp.startswith(folder_root) for fp in file_paths)
                if not has_descendant:
                    filtered.append((kind, path, size))
                    empty_folders_count += 1
            entries = filtered

        # Build the rendered output.
        root_label = root_name or "(root)"
        title = f"Put.io Recursive Listing - {root_label}" + (" (empty folders only)" if only_empty_folders else "")
        header = [title, "=" * len(title), ""]
        rendered_lines = [_format_line(k, p, s) for k, p, s in entries]

        if only_empty_folders:
            footer = [
                "",
                f"Empty folders: {empty_folders_count} (walked {folders_count} folders, {files_count} files in total)",
            ]
        else:
            footer = [
                "",
                f"Total: {folders_count} folders, {files_count} files",
            ]
        if truncated:
            cap = max_items or _ITEM_HARD_STOP
            footer.append(
                f"NOTE: walk stopped at {cap}-item safety cap - re-run with "
                f"max_items=<larger> or a deeper parent_id to continue."
            )
        if walk_errors:
            footer.append("")
            footer.append(f"WARNINGS: {len(walk_errors)} subfolder(s) skipped due to errors:")
            for msg in walk_errors[:10]:
                footer.append(f"  - {msg}")
            if len(walk_errors) > 10:
                footer.append(f"  ... and {len(walk_errors) - 10} more")

        listing = "\n".join(header + rendered_lines + footer)

        # save_to mode - cross-dispatch the write to the mac role so the
        # listing lands on the user's filesystem without round-tripping
        # through the model's output token budget. Returns a short
        # confirmation instead of the full listing.
        if save_to:
            try:
                from web.server.queue.dispatch_compat import saq_dispatch_tool

                expanded = save_to.strip()
                logger.info(
                    "putio_list_recursive: cross-dispatching write_file to mac (path=%s, %d chars)",
                    expanded,
                    len(listing),
                )
                result = saq_dispatch_tool(
                    "write_file",
                    {"path": expanded, "content": listing},
                    target_role="mac",
                )
                if only_empty_folders:
                    summary = (
                        f"Found {empty_folders_count} empty folder(s) "
                        f"(walked {folders_count} folders, {files_count} files). "
                    )
                else:
                    summary = (
                        f"Recursive listing complete: {folders_count} folders, "
                        f"{files_count} files ({len(listing):,} chars total). "
                    )
                return summary + f"Saved via cross-dispatch - {result}"
            except Exception as exc:  # noqa: BLE001
                logger.error("putio_list_recursive: save_to cross-dispatch failed: %s", exc)
                return (
                    f"ERROR: walked the tree successfully ({folders_count} folders, "
                    f"{files_count} files) but failed to save to {save_to}: {exc}. "
                    f"Retry without save_to to receive the listing inline, then "
                    f"call write_file separately."
                )

        return listing
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_list_recursive error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def putio_add_transfer(
    url: str,
    parent_id: int = 0,
) -> str:
    """Add a magnet link or URL to Put.io for cloud downloading.

    Accepts magnet URIs (magnet:?...) and direct download URLs.
    Returns the transfer ID and name once submitted.
    """
    try:
        data = _putio_post("/transfers/add", data={"url": url, "parent_id": parent_id})
        transfer = data.get("transfer", {})
        if not transfer:
            return json.dumps({"status": "error", "error": data.get("error_message", "Unknown error"), "raw": data})
        return json.dumps(
            {
                "status": "ok",
                "message": "Transfer added to Put.io",
                "transfer_id": transfer.get("id"),
                "name": transfer.get("name", ""),
                "transfer_status": transfer.get("status", ""),
            },
            indent=2,
        )
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_add_transfer error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def putio_list_transfers() -> str:
    """List active and recent transfers (downloads) on Put.io.

    Shows name, status, and progress percentage for each transfer.
    """
    try:
        data = _putio_get("/transfers/list")
        transfers = data.get("transfers", [])
        if not transfers:
            return json.dumps({"status": "empty", "message": "No active or recent transfers"})
        out = []
        for t in transfers:
            out.append(
                {
                    "id": t.get("id"),
                    "name": t.get("name", ""),
                    "status": t.get("status", ""),
                    "percent_done": t.get("percent_done", 0),
                    "size": _fmt_size(t.get("size", 0) or 0),
                    "error_message": t.get("error_message") or None,
                }
            )
        return json.dumps({"status": "ok", "count": len(out), "transfers": out}, indent=2)
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_list_transfers error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote", confirm="Delete Put.io files {file_ids}? This is permanent.")
def putio_delete_files(file_ids: list[int]) -> str:
    """Delete one or more files or folders from Put.io by their IDs.

    This is permanent and cannot be undone.
    Use putio_list_files or putio_search_files to find file IDs first.
    """
    try:
        if not file_ids:
            return json.dumps({"status": "error", "error": "No file IDs provided"})
        ids_csv = ",".join(str(i) for i in file_ids)
        data = _putio_post("/files/delete", data={"file_ids": ids_csv})
        if data.get("status") == "OK":
            return json.dumps({"status": "ok", "message": f"Deleted {len(file_ids)} item(s)", "file_ids": file_ids})
        return json.dumps({"status": "error", "error": data.get("error_message", "Unknown error"), "raw": data})
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_delete_files error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


def _confirm_empty_folder_delete(parent_id: int = 0, max_items: int = 0, dry_run: bool = True, **_):
    """Confirm prompt only fires when the call will actually delete folders.

    Returns the prompt string when ``dry_run`` is False, or ``None`` for the
    safe preview path. Booleans arrive as strings ('true'/'false') from
    JSON-shaped tool calls - handle both.
    """
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in ("false", "0", "no", "")
    if dry_run:
        return None
    return (
        f"Permanently delete ALL truly-empty folders under parent_id={parent_id} on Put.io? "
        f"This is recursive and irreversible. Set dry_run=True to preview first."
    )


@tool(locality="remote", confirm_condition=_confirm_empty_folder_delete)
def putio_delete_empty_folders(
    parent_id: int = 0,
    max_items: int = 0,
    dry_run: bool = True,
) -> str:
    """Find and (optionally) delete all truly-empty folders under a Put.io parent.

    Action companion to ``putio_list_recursive(only_empty_folders=True)``. The
    listing tool only previews; this tool acts. By default ``dry_run=True``
    so the first call returns the candidate list without deleting - the
    model (or user) must explicitly set ``dry_run=False`` to actually delete.

    "Empty" means the folder's entire subtree contains zero files (same
    definition as ``only_empty_folders``). Folders are SKIPPED if any
    subfolder failed to enumerate during the walk - better to leave them
    than risk deleting a folder whose contents we couldn't fully verify.
    If walk errors are present, the tool refuses to delete entirely
    (returns the error list so you can investigate); use
    ``putio_list_recursive`` to inspect, then re-run.

    REQUIRED WORKFLOW:
      1. Call ``putio_list_files(parent_id=0)`` to find the target folder
         ID (e.g., Movies). Do NOT use parent_id=0 unless you really mean
         the entire account.
      2. Call ``putio_delete_empty_folders(parent_id=<that_id>)`` to preview.
      3. Inspect the returned list. If it looks right, call again with
         ``dry_run=False`` to delete.

    Returns: JSON with ``dry_run``, ``empty_folder_count``, ``folders``
        (list of {id, path}), and either ``deleted_count`` (when acting)
        or a note that this was a preview.
    """
    _DEPTH_HARD_STOP = 50
    _ITEM_HARD_STOP = 20000

    try:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            parent_id = 0
        try:
            max_items = int(max_items)
        except (TypeError, ValueError):
            max_items = 0
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() not in ("false", "0", "no", "")

        # Walk: collect (id, full_path_with_slash) for every folder + a set
        # of file paths. Errors recorded per-folder so we can refuse to
        # delete if anything was unverifiable.
        folder_records: list[tuple[int, str]] = []
        file_paths: set[str] = set()
        walk_errors: list[str] = []
        folders_count = 0
        files_count = 0
        truncated = False

        def _walk(folder_id: int, path_prefix: str, depth: int) -> None:
            nonlocal folders_count, files_count, truncated
            if depth >= _DEPTH_HARD_STOP:
                return
            if max_items and (folders_count + files_count) >= max_items:
                truncated = True
                return
            if (folders_count + files_count) >= _ITEM_HARD_STOP:
                truncated = True
                return
            try:
                page_entries = _putio_walk_pages(folder_id)
            except Exception as page_exc:  # noqa: BLE001
                msg = f"{path_prefix or '/'} (id={folder_id}): {type(page_exc).__name__}: {str(page_exc)[:120]}"
                walk_errors.append(msg)
                logger.warning("putio_delete_empty_folders: skipping folder - %s", msg)
                return
            for entry in page_entries:
                if truncated:
                    return
                name = entry.get("name", "?")
                is_dir = (entry.get("file_type") or "").upper() == "FOLDER"
                full_path = f"{path_prefix}/{name}" if path_prefix else f"/{name}"
                if is_dir:
                    folders_count += 1
                    eid = int(entry.get("id", 0))
                    folder_records.append((eid, full_path + "/"))
                    _walk(eid, full_path, depth + 1)
                else:
                    files_count += 1
                    file_paths.add(full_path)

        # Resolve root name for nicer paths in the response.
        if parent_id == 0:
            root_name = ""
        else:
            try:
                info = _putio_get(f"/files/{parent_id}")
                root_name = (info.get("file") or {}).get("name", f"folder_{parent_id}")
            except Exception:
                root_name = f"folder_{parent_id}"

        logger.info(
            "putio_delete_empty_folders: scanning parent_id=%s (dry_run=%s, max_items=%s)",
            parent_id,
            dry_run,
            max_items,
        )
        _walk(parent_id, "" if parent_id == 0 else f"/{root_name}", 0)
        logger.info(
            "putio_delete_empty_folders: scan done - %d folders, %d files, %d errors",
            folders_count,
            files_count,
            len(walk_errors),
        )

        # Compute the empty set (same logic as putio_list_recursive's filter).
        empty: list[dict] = []
        for fid, fpath in folder_records:
            if not any(fp.startswith(fpath) for fp in file_paths):
                empty.append({"id": fid, "path": fpath})

        # Refuse to delete if walk had errors - emptiness can't be trusted
        # for any folder whose subtree wasn't fully enumerated.
        if walk_errors and not dry_run:
            return json.dumps(
                {
                    "status": "refused",
                    "reason": "walk had errors - cannot safely verify which folders are empty",
                    "walk_errors": walk_errors[:20],
                    "walk_error_count": len(walk_errors),
                    "empty_folder_candidates": len(empty),
                    "hint": "Re-run with dry_run=True to see the candidate list and walk errors, "
                    "then resolve the errors (or delete specific IDs via putio_delete_files) "
                    "before retrying with dry_run=False.",
                },
                indent=2,
            )

        # Dry-run preview - no API mutations.
        if dry_run:
            return json.dumps(
                {
                    "status": "ok",
                    "dry_run": True,
                    "parent_id": parent_id,
                    "empty_folder_count": len(empty),
                    "scan_summary": {
                        "folders_walked": folders_count,
                        "files_walked": files_count,
                        "walk_errors": len(walk_errors),
                        "truncated": truncated,
                    },
                    "folders": empty[:500],  # cap at 500 in preview
                    "note": (
                        "Preview only - nothing was deleted. To delete, re-run "
                        "with dry_run=False. The first 500 candidates are listed; "
                        "actual delete will process all of them."
                    )
                    if len(empty) > 500
                    else ("Preview only - nothing was deleted. To delete, re-run with dry_run=False."),
                },
                indent=2,
            )

        # Actual delete - chunk into batches of 200 IDs to keep each API
        # call modest and to surface partial-failure granularity.
        if not empty:
            return json.dumps(
                {
                    "status": "ok",
                    "dry_run": False,
                    "deleted_count": 0,
                    "message": "No empty folders found - nothing to delete.",
                },
                indent=2,
            )

        ids = [f["id"] for f in empty]
        deleted = 0
        delete_errors: list[str] = []
        BATCH = 200
        for i in range(0, len(ids), BATCH):
            chunk = ids[i : i + BATCH]
            try:
                ids_csv = ",".join(str(i) for i in chunk)
                data = _putio_post("/files/delete", data={"file_ids": ids_csv})
                if data.get("status") == "OK":
                    deleted += len(chunk)
                else:
                    delete_errors.append(f"batch {i // BATCH + 1}: {data.get('error_message', 'unknown')}")
            except Exception as exc:  # noqa: BLE001
                delete_errors.append(f"batch {i // BATCH + 1}: {type(exc).__name__}: {str(exc)[:120]}")

        return json.dumps(
            {
                "status": "ok" if deleted == len(ids) else "partial",
                "dry_run": False,
                "deleted_count": deleted,
                "requested_count": len(ids),
                "delete_errors": delete_errors,
                "scan_summary": {
                    "folders_walked": folders_count,
                    "files_walked": files_count,
                    "walk_errors": len(walk_errors),
                },
            },
            indent=2,
        )
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_delete_empty_folders error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def putio_get_download_url(file_id: int) -> str:
    """Get a direct download URL for a Put.io file by its file ID.

    Returns a time-limited URL you can use to download the file directly.
    """
    try:
        token = _get_putio_token()
        if not token:
            return json.dumps({"status": "error", "error": "Put.io token not configured"})
        url = f"{_PUTIO_BASE}/files/{file_id}/url"
        data = _http_get(url, headers={"Authorization": f"Bearer {token}"})
        dl_url = data.get("url") or data.get("download_url", "")
        if not dl_url:
            return json.dumps({"status": "error", "error": "No download URL returned", "raw": data})
        return json.dumps({"status": "ok", "file_id": file_id, "download_url": dl_url}, indent=2)
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_get_download_url error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def putio_clean_transfers() -> str:
    """Remove all completed (and errored) transfers from the Put.io transfer list.

    This only clears the transfer entries - downloaded files remain safely in
    your Put.io storage.  Use after downloads finish to declutter the list.
    Equivalent to the "CLEAR COMPLETED" button in the Put.io web UI.
    """
    try:
        data = _putio_post("/transfers/clean")
        if data.get("status") == "OK":
            return json.dumps({"status": "ok", "message": "Completed transfers cleared from the list"})
        return json.dumps({"status": "error", "error": data.get("error_message", "Unknown error"), "raw": data})
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_clean_transfers error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote", confirm="Cancel/stop {transfer_ids_csv} transfer(s) on Put.io?")
def putio_cancel_transfers(transfer_ids_csv: str) -> str:
    """Cancel or stop Put.io transfers by their IDs.

    For SEEDING transfers: stops seeding (files already downloaded stay safe).
    For DOWNLOADING/QUEUED transfers: cancels the transfer entirely.

    Use ``putio_list_transfers`` first to get transfer IDs, then pass the IDs
    of transfers you want to cancel.  Typically used to stop seeding after a
    download completes, or to abort a stuck/unwanted transfer.
    """
    try:
        if not transfer_ids_csv or not transfer_ids_csv.strip():
            return json.dumps({"status": "error", "error": "No transfer IDs provided"})
        ids = [int(x.strip()) for x in transfer_ids_csv.split(",") if x.strip().isdigit()]
        if not ids:
            return json.dumps(
                {
                    "status": "error",
                    "error": "No valid numeric transfer IDs provided - use putio_list_transfers to get IDs first",
                }
            )
        data = _putio_post("/transfers/cancel", data={"transfer_ids": ",".join(str(i) for i in ids)})
        if data.get("status") == "OK":
            return json.dumps({"status": "ok", "message": f"Cancelled {len(ids)} transfer(s)", "transfer_ids": ids})
        return json.dumps({"status": "error", "error": data.get("error_message", "Unknown error"), "raw": data})
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("putio_cancel_transfers error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


# ===========================================================================
# PREMIUMIZE TOOLS
# ===========================================================================


@tool(locality="remote")
def premiumize_list_files(
    folder_id: str = "",
    limit: int = 0,
) -> str:
    """List ALL files and folders in a Premiumize.me folder.

    Leave folder_id empty to list the root folder. Premiumize's
    ``/folder/list`` returns the entire folder in a single response - no
    cursor pagination is involved - so the only previous limitation was an
    in-memory slice that hid items past the default cap of 50.

    Returns: JSON with the full ``items`` list plus ``total`` (actual count
    returned) and the resolved ``path``.
    """
    try:
        try:
            limit = int(limit) if limit else 0
        except (TypeError, ValueError):
            limit = 0

        params: dict = {}
        if folder_id:
            params["id"] = folder_id
        data = _pm_get("/folder/list", params=params)
        if data.get("status") != "success":
            return json.dumps({"status": "error", "error": data.get("message", "Unknown error")})
        content = data.get("content", [])
        breadcrumbs = data.get("breadcrumbs", [])
        if not content:
            return json.dumps({"status": "empty", "message": "Folder is empty", "folder_id": folder_id or "root"})

        if limit and len(content) > limit:
            content = content[:limit]

        items_out = [_fmt_pm_item(item) for item in content]
        path_str = " / ".join(b.get("name", "") for b in breadcrumbs) if breadcrumbs else "root"
        return json.dumps(
            {
                "status": "ok",
                "path": path_str,
                "folder_id": folder_id or "root",
                "total": len(items_out),
                "items": items_out,
            },
            indent=2,
        )
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("premiumize_list_files error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def premiumize_search_files(
    query: str,
    limit: int = 0,
) -> str:
    """Search ALL files in Premiumize.me by name keyword (root folder).

    Performs a client-side substring match against files in your root folder.
    Premiumize has no native search and no list pagination, so this loads
    the full root folder and filters in-memory. Returns every match by
    default; previously capped at 30 silently.
    """
    try:
        try:
            limit = int(limit) if limit else 0
        except (TypeError, ValueError):
            limit = 0

        data = _pm_get("/folder/list")
        if data.get("status") != "success":
            return json.dumps({"status": "error", "error": data.get("message", "Unknown error")})
        content = data.get("content", [])
        q_lower = query.lower()
        matches = [item for item in content if q_lower in item.get("name", "").lower()]
        if not matches:
            return json.dumps(
                {
                    "status": "no_results",
                    "query": query,
                    "message": "No files matched your search in the root folder. "
                    "Use premiumize_list_files with a folder_id to search subfolders.",
                }
            )

        if limit and len(matches) > limit:
            matches = matches[:limit]

        items_out = [_fmt_pm_item(item) for item in matches]
        return json.dumps(
            {
                "status": "ok",
                "query": query,
                "total": len(items_out),
                "note": "Search covers root folder only.",
                "items": items_out,
            },
            indent=2,
        )
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("premiumize_search_files error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def premiumize_add_transfer(
    url: str,
    folder_id: str = "",
) -> str:
    """Add a magnet link or hoster URL to Premiumize.me for cloud downloading.

    Accepts magnet URIs (magnet:?...) and hoster URLs such as Rapidgator and DDownload.
    Premiumize will download the file to your cloud storage.
    """
    try:
        data: dict = {"src": url}
        if folder_id:
            data["folder_id"] = folder_id
        result = _pm_post("/transfer/create", data=data)
        if result.get("status") != "success":
            return json.dumps({"status": "error", "error": result.get("message", "Unknown error"), "raw": result})
        return json.dumps(
            {
                "status": "ok",
                "message": "Transfer added to Premiumize",
                "name": result.get("name", ""),
                "type": result.get("type", ""),
            },
            indent=2,
        )
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("premiumize_add_transfer error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def premiumize_list_transfers() -> str:
    """List active and recent transfers (downloads) on Premiumize.me.

    Shows name, status, and progress for each ongoing cloud download.
    """
    try:
        data = _pm_get("/transfer/list")
        if data.get("status") != "success":
            return json.dumps({"status": "error", "error": data.get("message", "Unknown error")})
        transfers = data.get("transfers", [])
        if not transfers:
            return json.dumps({"status": "empty", "message": "No active or recent transfers"})
        out = []
        for t in transfers:
            progress = t.get("progress", 0)
            if isinstance(progress, float) and progress <= 1.0:
                progress = round(progress * 100, 1)
            out.append(
                {
                    "id": t.get("id", ""),
                    "name": t.get("name", ""),
                    "status": t.get("status", ""),
                    "progress": f"{progress}%",
                    "size": _fmt_size(t.get("size", 0) or 0),
                    "message": t.get("message") or None,
                }
            )
        return json.dumps({"status": "ok", "count": len(out), "transfers": out}, indent=2)
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("premiumize_list_transfers error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def premiumize_get_direct_link(url: str) -> str:
    """Resolve a hoster URL to a direct CDN download link via Premiumize.me.

    Premiumize premium-leeches the hoster URL (Rapidgator, DDownload, etc.)
    and returns a direct download link. Provide the original hoster URL.
    """
    try:
        result = _pm_post("/transfer/directdl", data={"src": url})
        if result.get("status") != "success":
            return json.dumps({"status": "error", "error": result.get("message", "Unknown error"), "raw": result})
        content = result.get("content", [])
        if not content:
            return json.dumps({"status": "error", "error": "No download links returned", "raw": result})
        links_out = []
        for item in content:
            links_out.append(
                {
                    "name": item.get("name", ""),
                    "size": _fmt_size(item.get("size", 0) or 0),
                    "link": item.get("link", ""),
                    "stream_link": item.get("stream_link", ""),
                }
            )
        return json.dumps(
            {
                "status": "ok",
                "source_url": url,
                "files": links_out,
            },
            indent=2,
        )
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("premiumize_get_direct_link error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


@tool(locality="remote")
def premiumize_check_links(urls: list[str]) -> str:
    """Check whether hoster URLs or magnets are available via Premiumize.me.

    Uses Premiumize /cache/check to determine availability before adding a
    transfer. Returns green (cached and ready), orange (not cached but
    downloadable), or red (unavailable / unsupported) for each URL.
    Always call this before premiumize_add_transfer for hoster URLs.
    """
    try:
        key = _get_premiumize_key()
        if not key:
            raise ValueError(
                "Premiumize API key not configured (set cloud.premiumize.api_key in config.yaml or PREMIUMIZE_API_KEY env var)"
            )

        # Build multivalue POST body - urlencode doesn't natively handle
        # repeated keys for lists, so we build the pairs manually.
        pairs = [f"apikey={quote_plus(key)}"]
        for u in urls[:10]:
            pairs.append(f"items[]={quote_plus(u)}")
        body = "&".join(pairs).encode()

        req = Request(
            f"{_PM_BASE}/cache/check",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())

        if result.get("status") != "success":
            return json.dumps({"status": "error", "error": result.get("message", "Unknown error")})

        # Premiumize /cache/check returns parallel arrays:
        #   response[]   - bool, true = file is already in Premiumize cache
        #   filesize[]   - int or null, size in bytes (populated only when cached)
        #   filename[]   - str or null
        responses = result.get("response", [])
        filesizes = result.get("filesize", [None] * len(responses))
        filenames = result.get("filename", [None] * len(responses))

        out = []
        for i, cached in enumerate(responses):
            url_label = urls[i] if i < len(urls) else f"item_{i}"
            fsize = filesizes[i] if i < len(filesizes) else None
            fname = filenames[i] if i < len(filenames) else None

            if cached:
                # In Premiumize cache - instant download, definitely available
                status_str = "green"
                label = "[ok] cached and ready on Premiumize"
            else:
                # Not in cache - file may still be downloadable from the hoster.
                # Use check_hoster_availability for a definitive answer.
                status_str = "orange"
                label = "(!)  not in Premiumize cache - check hoster availability before adding"

            out.append(
                {
                    "url": url_label,
                    "status": status_str,
                    "label": label,
                    "filename": fname,
                    "filesize": fsize,
                }
            )

        return json.dumps({"status": "ok", "results": out}, indent=2)

    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("premiumize_check_links error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


# ---------------------------------------------------------------------------
# check_hoster_availability - supporting data
# ---------------------------------------------------------------------------

# CDN / bot-protection challenge page fingerprints.
# If any of these appear in a 200 response, the request was intercepted by a
# CDN and we cannot determine actual file status from the body.
_CDN_CHALLENGE_PATTERNS = [
    "just a moment",  # Cloudflare JS challenge title
    "checking your browser",  # Cloudflare browser check text
    "enable javascript and cookies",  # Cloudflare / generic bot wall
    "cf-browser-verification",  # Cloudflare hidden form field
    "ray id:",  # Cloudflare Ray ID footer
    "ddos-guard",  # DDoS-Guard CDN
    "datadome",  # DataDome bot management
    "_ddgid",  # DDoS-Guard cookie
    "shield_check",  # Generic shield check
    "please wait while we verify",  # Generic bot wall phrase
]

# Hoster-specific "file not found" fingerprints - checked against lowercased
# response body HTML.  Ordered most-specific first.
_HOSTER_NOT_FOUND_PATTERNS: dict[str, list[str]] = {
    "rapidgator.net": [
        "error 404",
        "file not found",
        "404 file not found",
        "the file you requested does not exist",
        "file has been deleted",
        "no such file",
    ],
    "ddownload.com": [
        "file not found",
        "file has been deleted",
        "no such file",
        "this file is no longer available",
    ],
    "uploaded.net": ["file not found", "file has been deleted", "no longer available"],
    "katfile.com": ["file not found", "file has been removed", "file was deleted"],
    "filefox.cc": ["file not found", "file was deleted", "no longer available"],
    "nitroflare.com": ["file not found", "file was removed", "link has expired"],
    "1fichier.com": ["file doesn't exist", "file not found", "lien invalide"],
    "mexashare.com": ["file not found", "file was deleted"],
    "filejoker.net": ["file not found", "file was deleted"],
    "turbobit.net": ["file not found", "file was deleted", "link expired"],
}

# Redirect patterns - when a hoster redirects away from the file path,
# the file is almost certainly deleted.  The key is the hoster hostname and
# the value is a list of path prefixes that indicate a "file page".  If the
# request starts on one of these paths and the response ends up on a
# *different* path, we treat the file as unavailable.
_HOSTER_FILE_PATH_PREFIXES: dict[str, list[str]] = {
    "rapidgator.net": ["/file/"],
    "ddownload.com": ["/d/", "/dl/"],
    "uploaded.net": ["/file/"],
    "katfile.com": ["/", "/file/"],  # katfile uses root-level file IDs
    "filefox.cc": ["/", "/file/"],
    "nitroflare.com": ["/view/"],
    "1fichier.com": ["/"],
    "mexashare.com": ["/"],
    "filejoker.net": ["/"],
    "turbobit.net": ["/", "/file/"],
}


_LANG_PREFIX_RE = re.compile(r"^/[a-z]{2}(?:-[a-zA-Z]{2,4})?/")


def _strip_lang_prefix(path: str) -> str:
    """Strip a two-letter language code prefix from a URL path.

    e.g., ``/en/file/abc/...`` -> ``/file/abc/...``
         ``/ru/file/abc/...`` -> ``/file/abc/...``
         ``/file/abc/...``    -> ``/file/abc/...``  (no change)

    Handles paths like ``/en/``, ``/de/``, ``/ru/``, ``/pt-br/`` etc.
    This prevents false positives when a hoster adds a language prefix via
    redirect but keeps the file at the same logical path (e.g., Rapidgator's
    ``/file/{hash}/...`` -> ``/en/file/{hash}/...`` redirect).
    """
    return _LANG_PREFIX_RE.sub("/", path, count=1)


def _is_away_redirect(original_url: str, final_url: str) -> bool:
    """Return True if the hoster redirected *away* from the file page.

    Compares the path of the original URL against the final URL.  If the
    original was on a known file-serving path and the final URL is on a
    different path (login, premium, error, etc.), the file is gone.

    Language-code prefixes (``/en/``, ``/de/``, ``/ru/``, ...) are stripped
    before comparison so that a redirect from ``/file/x/f.rar.html`` to
    ``/en/file/x/f.rar.html`` is NOT treated as redirect-away.
    """
    orig = urlparse(original_url)
    final = urlparse(final_url)

    # Different host = definitely redirected away
    if orig.netloc.lower().replace("www.", "") != final.netloc.lower().replace("www.", ""):
        return True

    # Normalise language-prefix redirects before any path comparison
    orig_path = _strip_lang_prefix(orig.path)
    final_path = _strip_lang_prefix(final.path)

    # Same normalised path (or trivial trailing-slash difference) = not a redirect
    if orig_path.rstrip("/") == final_path.rstrip("/"):
        return False

    hostname = orig.netloc.lower().replace("www.", "")
    prefixes = _HOSTER_FILE_PATH_PREFIXES.get(hostname)

    if prefixes:
        # The original URL was on a file-serving path ...
        on_file_path = any(orig_path.startswith(p) for p in prefixes)
        still_on_file_path = any(final_path.startswith(p) for p in prefixes)
        if on_file_path and not still_on_file_path:
            return True

    # Fallback: if the paths are different and the final path looks like a
    # generic non-file page, treat as redirect-away.
    non_file_paths = ("/article/", "/login", "/premium", "/register", "/error", "/404", "/upgrade")
    if any(final_path.lower().startswith(p) for p in non_file_paths):
        return True

    return False


# Applied when no hoster-specific entry matches.
_GENERIC_NOT_FOUND_PATTERNS = [
    "file not found",
    "file has been deleted",
    "file has been removed",
    "no such file",
    "this file is not available",
    "this file is no longer available",
    "the requested file could not be found",
    "link has expired",
    "link is invalid",
    "error 404",
]

# Browser-like headers used for all hoster requests.
_HOSTER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
}


def _check_with_playwright(url: str) -> dict | None:
    """Fallback availability check using a real Firefox browser (headless).

    Used when the lightweight urllib check is blocked by CDN/bot-protection.
    Returns a status dict (same schema as check_hoster_availability) or None if
    Playwright is not installed / browsers are not available.

    Firefox is used intentionally - it has a better TLS fingerprint than
    Chromium-based headless browsers and is less likely to be flagged by
    Cloudflare, matching the strategy used by the price-scout scraper.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.debug("check_hoster_availability: playwright not installed - skipping browser fallback")
        return None

    has_stealth = False
    try:
        from playwright_stealth import stealth_sync  # type: ignore

        has_stealth = True
    except ImportError:
        pass

    try:
        with sync_playwright() as pw:
            browser = pw.firefox.launch(
                headless=True,
                firefox_user_prefs={
                    "general.useragent.override": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0"
                    ),
                },
            )
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0"),
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            if has_stealth:
                stealth_sync(page)

            try:
                response = page.goto(url, timeout=25_000, wait_until="domcontentloaded")
                http_status = response.status if response else 200

                if http_status in (404, 410):
                    return {
                        "status": "unavailable",
                        "method": "playwright/firefox",
                        "http_status": http_status,
                        "url": url,
                        "reason": f"HTTP {http_status}",
                    }

                # Redirect detection - browser followed redirects automatically
                final_page_url = page.url
                if _is_away_redirect(url, final_page_url):
                    return {
                        "status": "unavailable",
                        "method": "playwright/firefox+redirect",
                        "http_status": http_status,
                        "url": url,
                        "final_url": final_page_url,
                        "reason": f"Redirected away from file page -> {final_page_url}",
                    }

                body = page.content().lower()

                # CDN challenge still present even in the browser?
                cdn_hit = next((p for p in _CDN_CHALLENGE_PATTERNS if p in body), None)
                if cdn_hit:
                    return {
                        "status": "blocked",
                        "method": "playwright/firefox",
                        "http_status": http_status,
                        "url": url,
                        "reason": "CDN challenge still shown in browser",
                    }

                hostname = urlparse(url).netloc.lower()
                if hostname.startswith("www."):
                    hostname = hostname[4:]
                patterns = _HOSTER_NOT_FOUND_PATTERNS.get(hostname, _GENERIC_NOT_FOUND_PATTERNS)
                matched = next((p for p in patterns if p in body), None)

                if matched or http_status >= 400:
                    return {
                        "status": "unavailable",
                        "method": "playwright/firefox",
                        "http_status": http_status,
                        "url": url,
                        "reason": f"page contains '{matched}'" if matched else f"HTTP {http_status}",
                    }

                return {"status": "available", "method": "playwright/firefox", "http_status": http_status, "url": url}

            finally:
                context.close()
                browser.close()

    except Exception as exc:
        logger.debug("check_hoster_availability: playwright/firefox check failed: %s", exc)
        return None


@tool(locality="remote")
def check_hoster_availability(url: str) -> str:
    """Check whether a file hoster URL is live before adding a transfer.

    Makes independent HTTP requests - no Premiumize API call, no usage counted.
    Handles Cloudflare/CDN bot-protection interception gracefully.

    Returns one of four statuses:
      available   - file page loaded and no "not found" indicators
      unavailable - HTTP 404/410 or "file not found" text detected in body
      blocked     - CDN/bot-protection intercepted the request (treat as: do not add)
      error       - network timeout or unexpected error
    """
    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    ssl_ctx = ssl.create_default_context()

    # ------------------------------------------------------------------
    # Step 1: HEAD request - fast-path for direct 404 / redirect.
    # Many hosters return 404 on HEAD even when Cloudflare would normally
    # serve a JS challenge for a full page GET.  Also catches redirects
    # (e.g., Rapidgator redirects /file/... -> /article/premium for deleted
    # files while returning HTTP 200).
    # ------------------------------------------------------------------
    head_status: int | None = None
    head_final_url: str | None = None
    try:
        head_req = Request(url, method="HEAD", headers=_HOSTER_HEADERS)
        with urlopen(head_req, timeout=10, context=ssl_ctx) as resp:
            head_status = resp.status
            head_final_url = resp.url
    except HTTPError as exc:
        head_status = exc.code
    except (URLError, Exception):
        pass  # HEAD not supported or network error - fall through to GET

    if head_status in (404, 410):
        return json.dumps(
            {
                "status": "unavailable",
                "method": "HEAD",
                "http_status": head_status,
                "url": url,
                "reason": f"HTTP {head_status}",
            }
        )

    # Redirect detection on HEAD - if the hoster sent us away from the
    # file path it almost certainly means the file is gone.
    if head_final_url and _is_away_redirect(url, head_final_url):
        return json.dumps(
            {
                "status": "unavailable",
                "method": "HEAD+redirect",
                "http_status": head_status or 200,
                "url": url,
                "final_url": head_final_url,
                "reason": f"Redirected away from file page -> {head_final_url}",
            }
        )

    if head_status == 403:
        # 403 on HEAD often means CDN blocking - note it but still try GET
        logger.debug("check_hoster_availability: HEAD 403 for %s - trying GET", url)

    # ------------------------------------------------------------------
    # Step 2: Full GET request to read the page body.
    # ------------------------------------------------------------------
    try:
        get_req = Request(url, headers=_HOSTER_HEADERS)
        with urlopen(get_req, timeout=15, context=ssl_ctx) as resp:
            http_status = resp.status
            get_final_url = resp.url

            # Redirect detection on GET (mirrors Step 1 HEAD check)
            if _is_away_redirect(url, get_final_url):
                return json.dumps(
                    {
                        "status": "unavailable",
                        "method": "GET+redirect",
                        "http_status": http_status,
                        "url": url,
                        "final_url": get_final_url,
                        "reason": f"Redirected away from file page -> {get_final_url}",
                    }
                )

            raw = resp.read(32_768)
            # Try to decompress gzip if the response is compressed
            try:
                import gzip as _gzip

                raw = _gzip.decompress(raw)
            except Exception:
                pass
            body = raw.decode("utf-8", errors="replace").lower()
    except HTTPError as exc:
        if exc.code in (404, 410):
            return json.dumps(
                {
                    "status": "unavailable",
                    "method": "GET",
                    "http_status": exc.code,
                    "url": url,
                    "reason": f"HTTP {exc.code}",
                }
            )
        if exc.code in (403, 503):
            # 403/503 from Cloudflare = blocked - try Firefox fallback
            logger.debug("check_hoster_availability: HTTP %d for %s - trying Firefox fallback", exc.code, url)
            pw_result = _check_with_playwright(url)
            if pw_result:
                return json.dumps(pw_result)
            return json.dumps(
                {
                    "status": "blocked",
                    "http_status": exc.code,
                    "url": url,
                    "reason": f"HTTP {exc.code} - CDN/bot-protection blocking the check",
                }
            )
        return json.dumps({"status": "error", "http_status": exc.code, "error": f"HTTP {exc.code}"})
    except URLError as exc:
        return json.dumps({"status": "error", "error": str(exc.reason)})
    except Exception as exc:
        logger.error("check_hoster_availability GET error: %s", exc)
        return json.dumps({"status": "error", "error": str(exc)})

    # ------------------------------------------------------------------
    # Step 3: Detect CDN challenge pages.
    # A challenge page means the urllib request was intercepted - try to
    # fall back to a real Firefox browser (playwright-stealth) which can
    # execute the JS challenge and reach the actual file page.
    # ------------------------------------------------------------------
    cdn_hit = next((p for p in _CDN_CHALLENGE_PATTERNS if p in body), None)
    if cdn_hit:
        logger.debug("check_hoster_availability: CDN challenge detected for %s - trying Firefox fallback", url)
        pw_result = _check_with_playwright(url)
        if pw_result:
            return json.dumps(pw_result)
        # Playwright not available or also failed
        return json.dumps(
            {
                "status": "blocked",
                "http_status": http_status,
                "url": url,
                "reason": (
                    f"CDN challenge page detected ('{cdn_hit}') - "
                    "Firefox fallback unavailable, cannot verify file status"
                ),
            }
        )

    # ------------------------------------------------------------------
    # Step 4: Check for file-not-found patterns.
    # ------------------------------------------------------------------
    patterns = _HOSTER_NOT_FOUND_PATTERNS.get(hostname, _GENERIC_NOT_FOUND_PATTERNS)
    matched = next((p for p in patterns if p in body), None)

    if matched or http_status >= 400:
        return json.dumps(
            {
                "status": "unavailable",
                "http_status": http_status,
                "url": url,
                "reason": f"page contains '{matched}'" if matched else f"HTTP {http_status}",
            }
        )

    return json.dumps(
        {
            "status": "available",
            "http_status": http_status,
            "url": url,
        }
    )


@tool(locality="remote", confirm="Delete Premiumize item '{item_id}'? This is permanent.")
def premiumize_delete_item(item_id: str) -> str:
    """Delete a file or folder from Premiumize.me by its item ID.

    This is permanent. Use premiumize_list_files to find item IDs first.
    """
    try:
        result = _pm_post("/item/delete", data={"id[]": item_id})
        if result.get("status") != "success":
            return json.dumps({"status": "error", "error": result.get("message", "Unknown error")})
        return json.dumps({"status": "ok", "message": f"Item {item_id} deleted successfully"})
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except (HTTPError, URLError) as e:
        return json.dumps({"status": "error", "error": f"HTTP error: {e}"})
    except Exception as e:
        logger.error("premiumize_delete_item error: %s", e)
        return json.dumps({"status": "error", "error": str(e)})


# ===========================================================================
# Registration
# ===========================================================================


def register_cloud_tools(registry: "ToolRegistry") -> int:
    """Register cloud storage tools, gated on credentials.

    Put.io tools register only when ``PUTIO_TOKEN`` is set; Premiumize tools
    only when ``PREMIUMIZE_API_KEY`` is set. With neither, nothing registers.
    """
    putio_tools = [
        putio_list_files,
        putio_list_recursive,
        putio_search_files,
        putio_add_transfer,
        putio_delete_empty_folders,
        putio_list_transfers,
        putio_delete_files,
        putio_get_download_url,
        putio_clean_transfers,
        putio_cancel_transfers,
    ]
    premiumize_tools = [
        # Availability checks (call before adding any hoster-URL transfer)
        check_hoster_availability,
        premiumize_check_links,
        premiumize_list_files,
        premiumize_search_files,
        premiumize_add_transfer,
        premiumize_list_transfers,
        premiumize_get_direct_link,
        premiumize_delete_item,
    ]

    count = 0
    if _get_putio_token():
        for fn in putio_tools:
            registry.register(fn)
        count += len(putio_tools)
    if _get_premiumize_key():
        for fn in premiumize_tools:
            registry.register(fn)
        count += len(premiumize_tools)

    logger.debug("Registered %d cloud tools (Put.io + Premiumize)", count)
    return count
