"""Query Loki for log lines belonging to one chat session."""

from __future__ import annotations

from typing import Any

import httpx

from agentforge.session_logging import SESSION_ID_RE

LOG_LIMIT = 2000
WINDOW_PAD_SECONDS = 300

_PRIMARY = '{{host=~".+"}} | session_id="{sid}"'
_FALLBACK = '{{host=~".+"}} |= "{sid}"'


async def query_session_logs(
    session_id: str,
    *,
    start_ns: int,
    end_ns: int,
    loki_url: str,
    limit: int = LOG_LIMIT,
    timeout_s: float = 10.0,
    client: Any | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    if not SESSION_ID_RE.match(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    base = loki_url.rstrip("/")
    url = f"{base}/loki/api/v1/query_range"
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_s)
    try:
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for query in (_PRIMARY.format(sid=session_id), _FALLBACK.format(sid=session_id)):
            resp = await http.get(
                url,
                params={
                    "query": query,
                    "start": str(start_ns),
                    "end": str(end_ns),
                    "limit": str(limit),
                    "direction": "forward",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            for stream in (payload.get("data") or {}).get("result") or []:
                labels = {str(k): str(v) for k, v in (stream.get("stream") or {}).items()}
                if "session_id" in labels:
                    # Cardinality guard: never treat session_id as an indexed label
                    # even if a future Alloy misconfig promotes it.
                    labels.pop("session_id", None)
                for ts_ns, line in stream.get("values") or []:
                    key = (str(ts_ns), str(line))
                    if key not in rows:
                        rows[key] = {"ts_ns": str(ts_ns), "line": str(line), "labels": labels}
    finally:
        if own_client:
            await http.aclose()

    ordered = sorted(rows.values(), key=lambda r: int(r["ts_ns"]))
    truncated = len(ordered) > limit
    return ordered[:limit], truncated
