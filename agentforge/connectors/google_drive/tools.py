"""Google Drive tool factory — creates tool callables bound to a specific connection."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from agentforge.secret_redactor import get_redactor

logger = get_logger(__name__)

_DRIVE_BASE = "https://www.googleapis.com/drive/v3"
_REQUEST_TIMEOUT = 20
_DEFAULT_MAX_CHARS = 50_000


def create_drive_tools(
    connection_id: str,
    token_accessor: Callable[[], str],
) -> list[Callable]:
    """Create Google Drive tool callables bound to a specific connection's credentials."""

    def _err_body(exc: Exception) -> str:
        """Render an HTTP error for a tool result, redacting secrets from the body.

        For HTTPError we read up to 500 chars of the upstream body (which can
        echo request data); other errors fall back to ``str(exc)``. Either way
        the result is run through the redactor before it reaches the LLM.
        """
        if isinstance(exc, HTTPError) and hasattr(exc, "read"):
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw = str(exc)
            text = f"{exc.code}: {raw[:500]}" if raw else str(exc)
        else:
            text = str(exc)
        return get_redactor().redact(text).text

    def _drive_get(path: str, params: dict | None = None) -> dict:
        token = token_accessor()
        qs = f"?{urlencode(params)}" if params else ""
        url = f"{_DRIVE_BASE}{path}{qs}"

        def _do(t: str) -> dict:
            req = Request(url, headers={"Authorization": f"Bearer {t}"})
            with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode())

        try:
            return _do(token)
        except HTTPError as exc:
            if exc.code != 401:
                raise
            new_token = token_accessor()
            return _do(new_token)

    def _drive_download(file_id: str, mime_override: str | None = None) -> str:
        """Download a file's text content. For Google Docs/Sheets/Slides, exports as plain text."""
        token = token_accessor()

        if mime_override:
            url = f"{_DRIVE_BASE}/files/{quote(file_id)}/export?mimeType={quote(mime_override)}"
        else:
            url = f"{_DRIVE_BASE}/files/{quote(file_id)}?alt=media"

        req = Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code != 401:
                raise
            new_token = token_accessor()
            req = Request(url, headers={"Authorization": f"Bearer {new_token}"})
            with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")

    from agentforge.tools.registry import tool

    @tool
    def drive_list_files(query: str = "", limit: int = 25) -> str:
        """List or search files in Google Drive.

        Uses Google Drive query syntax. Examples:
            name contains 'report'
            mimeType = 'application/pdf'
            modifiedTime > '2026-01-01'
            'root' in parents (files in root folder)
            name contains 'budget' and mimeType = 'application/vnd.google-apps.spreadsheet'

        Leave query empty to list recent files.
        """
        try:
            params = {
                "pageSize": max(1, min(int(limit), 100)),
                "fields": "files(id,name,mimeType,size,modifiedTime,owners,shared,webViewLink,parents)",
                "orderBy": "modifiedTime desc",
            }
            if query:
                params["q"] = query

            data = _drive_get("/files", params=params)
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {_err_body(exc)}"})
        except Exception as exc:
            logger.error("drive_list_files error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        files = data.get("files") or []
        if not files:
            return json.dumps(
                {
                    "status": "no_results",
                    "query": query,
                    "message": "No files matched your query.",
                }
            )

        results = []
        for f in files:
            owners = f.get("owners") or []
            owner_email = owners[0].get("emailAddress", "") if owners else ""
            results.append(
                {
                    "file_id": f.get("id", ""),
                    "name": f.get("name", ""),
                    "mime_type": f.get("mimeType", ""),
                    "size_bytes": int(f.get("size", 0) or 0),
                    "modified_time": f.get("modifiedTime", ""),
                    "owner": owner_email,
                    "shared": f.get("shared", False),
                    "web_link": f.get("webViewLink", ""),
                }
            )

        return json.dumps(
            {
                "status": "ok",
                "query": query,
                "count": len(results),
                "files": results,
            },
            indent=2,
        )

    @tool
    def drive_get_file(file_id: str) -> str:
        """Get metadata for a single file by ID.

        Returns file name, type, size, owner, permissions, and sharing info.
        """
        if not file_id:
            return json.dumps({"status": "error", "error": "file_id is required"})

        try:
            data = _drive_get(
                f"/files/{quote(file_id)}",
                params={
                    "fields": (
                        "id,name,mimeType,size,modifiedTime,createdTime,"
                        "owners,shared,webViewLink,description,"
                        "permissions(id,emailAddress,role,type)"
                    ),
                },
            )
        except HTTPError as exc:
            if exc.code == 404:
                return json.dumps({"status": "not_found", "file_id": file_id})
            return json.dumps({"status": "error", "error": f"HTTP error: {_err_body(exc)}"})
        except (URLError, Exception) as exc:
            logger.error("drive_get_file error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        owners = data.get("owners") or []
        perms = data.get("permissions") or []

        return json.dumps(
            {
                "status": "ok",
                "file": {
                    "file_id": data.get("id", ""),
                    "name": data.get("name", ""),
                    "mime_type": data.get("mimeType", ""),
                    "size_bytes": int(data.get("size", 0) or 0),
                    "created_time": data.get("createdTime", ""),
                    "modified_time": data.get("modifiedTime", ""),
                    "description": data.get("description", ""),
                    "owner": owners[0].get("emailAddress", "") if owners else "",
                    "shared": data.get("shared", False),
                    "web_link": data.get("webViewLink", ""),
                    "permissions": [
                        {
                            "email": p.get("emailAddress", ""),
                            "role": p.get("role", ""),
                            "type": p.get("type", ""),
                        }
                        for p in perms
                    ],
                },
            },
            indent=2,
        )

    @tool
    def drive_read_file(file_id: str, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
        """Read the text content of a file from Google Drive.

        For Google Docs, Sheets, and Slides, the content is exported as plain text.
        For regular files (PDF, txt, csv, etc.), the raw content is downloaded.
        Binary files (images, videos) cannot be read this way.
        """
        if not file_id:
            return json.dumps({"status": "error", "error": "file_id is required"})

        # First get metadata to determine mime type
        try:
            meta = _drive_get(
                f"/files/{quote(file_id)}",
                params={"fields": "id,name,mimeType,size"},
            )
        except HTTPError as exc:
            if exc.code == 404:
                return json.dumps({"status": "not_found", "file_id": file_id})
            return json.dumps({"status": "error", "error": f"HTTP error: {_err_body(exc)}"})
        except (URLError, Exception) as exc:
            return json.dumps({"status": "error", "error": str(exc)})

        mime = meta.get("mimeType", "")
        name = meta.get("name", "")

        # Google Workspace types need export
        export_map = {
            "application/vnd.google-apps.document": "text/plain",
            "application/vnd.google-apps.spreadsheet": "text/csv",
            "application/vnd.google-apps.presentation": "text/plain",
            "application/vnd.google-apps.drawing": "image/svg+xml",
        }

        binary_types = {"image/", "video/", "audio/", "application/zip", "application/octet-stream"}
        if any(mime.startswith(bt) for bt in binary_types):
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Cannot read binary file: {name} ({mime})",
                }
            )

        try:
            export_mime = export_map.get(mime)
            content = _drive_download(file_id, mime_override=export_mime)
        except HTTPError as exc:
            return json.dumps({"status": "error", "error": f"Download failed: {_err_body(exc)}"})
        except (URLError, Exception) as exc:
            return json.dumps({"status": "error", "error": str(exc)})

        truncated = False
        if max_chars and len(content) > max_chars:
            content = content[:max_chars]
            truncated = True

        return json.dumps(
            {
                "status": "ok",
                "file_id": file_id,
                "name": name,
                "mime_type": mime,
                "content": content,
                "content_truncated": truncated,
                "char_count": len(content),
            },
            indent=2,
        )

    @tool
    def drive_list_shared_drives() -> str:
        """List shared drives (Team Drives) the user has access to.

        Returns drive names and IDs. Use the drive ID with drive_list_files
        to search within a specific shared drive.
        """
        try:
            data = _drive_get(
                "/drives",
                params={"pageSize": 100, "fields": "drives(id,name,createdTime)"},
            )
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {_err_body(exc)}"})
        except Exception as exc:
            logger.error("drive_list_shared_drives error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        drives = data.get("drives") or []
        return json.dumps(
            {
                "status": "ok",
                "count": len(drives),
                "drives": [
                    {
                        "drive_id": d.get("id", ""),
                        "name": d.get("name", ""),
                        "created_time": d.get("createdTime", ""),
                    }
                    for d in drives
                ],
            },
            indent=2,
        )

    return [
        drive_list_files,
        drive_get_file,
        drive_read_file,
        drive_list_shared_drives,
    ]
