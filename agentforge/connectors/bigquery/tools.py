"""BigQuery tool factory — creates tool callables bound to a specific connection."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

from agentforge.secret_redactor import get_redactor
from agentforge.tools.registry import tool

logger = get_logger(__name__)

_BQ_BASE = "https://bigquery.googleapis.com/bigquery/v2"
_REQUEST_TIMEOUT = 300
_JOB_POLL_INTERVAL = 1.0
_JOB_POLL_MAX_WAIT = 300


def create_bigquery_tools(
    connection_id: str,
    token_accessor: Callable[[], str],
    default_project_id: str = "",
) -> list[Callable]:
    """Create BigQuery tool callables bound to a specific connection's credentials."""

    def _safe_read(resp) -> bytes:
        """Read response body, retrying on IncompleteRead."""
        chunks = []
        while True:
            try:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            except IncompleteRead as exc:
                chunks.append(exc.partial)
                break
        return b"".join(chunks)

    def _err_body(exc: HTTPError) -> str:
        """Read up to 500 chars of an HTTP error body, redacting any secrets."""
        raw = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        return get_redactor().redact(raw[:500]).text

    def _bq_request(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        token = token_accessor()
        qs = f"?{urlencode(params)}" if params else ""
        url = f"{_BQ_BASE}{path}{qs}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        data = json.dumps(body).encode() if body else None

        def _do_request(hdrs: dict) -> dict:
            rq = Request(url, data=data, headers=dict(hdrs), method=method)
            for attempt in range(2):
                with urlopen(rq, timeout=_REQUEST_TIMEOUT) as resp:
                    raw = _safe_read(resp)
                try:
                    return json.loads(raw.decode())
                except json.JSONDecodeError:
                    if attempt == 0:
                        logger.warning("bigquery: truncated response (%d bytes), retrying", len(raw))
                        time.sleep(0.5)
                        rq = Request(url, data=data, headers=dict(hdrs), method=method)
                        continue
                    raise

        try:
            return _do_request(headers)
        except HTTPError as exc:
            if exc.code != 401:
                raise
            headers["Authorization"] = f"Bearer {token_accessor()}"
            return _do_request(headers)

    def _run_query(sql: str, project_id: str, max_results: int = 500) -> dict:
        """Submit a query job, poll until done, return results."""
        job_body = {
            "configuration": {
                "query": {
                    "query": sql,
                    "useLegacySql": False,
                    "maximumBytesBilled": str(10 * 1024**3),  # 10 GB safety cap
                },
            },
        }
        # Submit job
        job = _bq_request("POST", f"/projects/{quote(project_id)}/jobs", body=job_body)
        job_id = job.get("jobReference", {}).get("jobId", "")
        if not job_id:
            return {"error": f"Failed to create job: {job}"}

        # Poll until done
        elapsed = 0.0
        while elapsed < _JOB_POLL_MAX_WAIT:
            status = _bq_request("GET", f"/projects/{quote(project_id)}/jobs/{quote(job_id)}")
            state = status.get("status", {}).get("state", "")
            if state == "DONE":
                errors = status.get("status", {}).get("errors")
                if errors:
                    return {"error": "; ".join(e.get("message", "") for e in errors)}
                break
            time.sleep(_JOB_POLL_INTERVAL)
            elapsed += _JOB_POLL_INTERVAL
        else:
            return {"error": f"Query timed out after {_JOB_POLL_MAX_WAIT}s (job: {job_id})"}

        # Fetch results
        results = _bq_request(
            "GET",
            f"/projects/{quote(project_id)}/queries/{quote(job_id)}",
            params={"maxResults": max_results},
        )

        # Parse into rows
        schema_fields = results.get("schema", {}).get("fields") or []
        columns = [f.get("name", f"col_{i}") for i, f in enumerate(schema_fields)]
        rows_raw = results.get("rows") or []
        rows = []
        for row in rows_raw:
            cells = row.get("f") or []
            rows.append({columns[i]: (cells[i].get("v") if i < len(cells) else None) for i in range(len(columns))})

        stats = status.get("statistics", {}).get("query", {})
        bytes_processed = int(stats.get("totalBytesProcessed", 0))

        return {
            "columns": columns,
            "rows": rows,
            "total_rows": int(results.get("totalRows", len(rows))),
            "bytes_processed": bytes_processed,
            "bytes_processed_human": _fmt_bytes(bytes_processed),
            "job_id": job_id,
            "cache_hit": stats.get("cacheHit", False),
        }

    def _fmt_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    _project_cache: dict[str, str] = {"id": default_project_id}

    def _resolve_project() -> str:
        if _project_cache["id"]:
            return _project_cache["id"]
        try:
            data = _bq_request("GET", "/projects", params={"maxResults": 50})
            for p in data.get("projects") or []:
                pid = p.get("id") or p.get("projectReference", {}).get("projectId", "")
                if pid:
                    _project_cache["id"] = pid
                    logger.info("bigquery: auto-detected billing project %s (conn %s)", pid, connection_id[:8])
                    return pid
        except Exception as exc:
            logger.warning("bigquery: project auto-detect failed: %s", exc)
        return ""

    # -- Tool functions -----------------------------------------------------

    @tool
    def bigquery_query(sql: str, project_id: str = "", max_results: int = 100) -> str:
        """Run a SQL query against Google BigQuery and return the results.

        The query runs with standard SQL (not legacy SQL). Results are capped
        at max_results rows. A 10 GB bytes-billed safety cap is enforced.

        Querying a public dataset (e.g. bigquery-public-data.pypi.*) still bills YOUR
        GCP project. That billing project is auto-detected from the connected Google
        account — leave project_id EMPTY. Do NOT invent or guess a project id.

        Common PyPI tables:
          - bigquery-public-data.pypi.file_downloads (download events)
          - bigquery-public-data.pypi.distribution_metadata (package metadata)
        """
        # Prefer the connection's own billing project (stored/auto-detected) over any
        # caller-supplied id — the model can't know the real project and tends to guess.
        billing_project = _resolve_project() or (project_id or "").strip()
        if not billing_project:
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        "No GCP project with BigQuery available for this account. Enable the BigQuery "
                        "API on a project at console.cloud.google.com, then retry (no reconnect needed)."
                    ),
                }
            )
        max_results = max(1, min(int(max_results), 1000))
        try:
            result = _run_query(sql, billing_project, max_results)
        except HTTPError as exc:
            return json.dumps({"status": "error", "error": f"HTTP {exc.code}: {_err_body(exc)}"})
        except (URLError, Exception) as exc:
            logger.error("bigquery_query error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        if "error" in result:
            return json.dumps({"status": "error", "error": result["error"]})

        return json.dumps(
            {
                "status": "ok",
                "columns": result["columns"],
                "rows": result["rows"],
                "total_rows": result["total_rows"],
                "bytes_processed": result["bytes_processed_human"],
                "cache_hit": result["cache_hit"],
            },
            indent=2,
        )

    @tool
    def bigquery_tables(dataset: str, project_id: str = "bigquery-public-data", limit: int = 50) -> str:
        """List tables in a BigQuery dataset.

        For public datasets, use project_id "bigquery-public-data".
        For your own datasets, leave empty to auto-detect.
        """
        try:
            data = _bq_request(
                "GET",
                f"/projects/{quote(project_id)}/datasets/{quote(dataset)}/tables",
                params={"maxResults": min(int(limit), 200)},
            )
        except HTTPError as exc:
            if exc.code == 404:
                return json.dumps(
                    {"status": "error", "error": f"Dataset '{dataset}' not found in project '{project_id}'"}
                )
            return json.dumps({"status": "error", "error": f"HTTP {exc.code}: {_err_body(exc)}"})
        except (URLError, Exception) as exc:
            return json.dumps({"status": "error", "error": str(exc)})

        tables = data.get("tables") or []
        results = []
        for t in tables:
            ref = t.get("tableReference", {})
            results.append(
                {
                    "table_id": ref.get("tableId", ""),
                    "type": t.get("type", ""),
                    "creation_time": t.get("creationTime", ""),
                    "row_count": t.get("numRows"),
                    "size_bytes": int(t.get("numBytes", 0) or 0),
                }
            )

        return json.dumps(
            {
                "status": "ok",
                "dataset": dataset,
                "project_id": project_id,
                "count": len(results),
                "tables": results,
            },
            indent=2,
        )

    @tool
    def bigquery_schema(table: str, dataset: str, project_id: str = "bigquery-public-data") -> str:
        """Get the schema (columns and types) of a BigQuery table."""
        try:
            data = _bq_request(
                "GET",
                f"/projects/{quote(project_id)}/datasets/{quote(dataset)}/tables/{quote(table)}",
            )
        except HTTPError as exc:
            if exc.code == 404:
                return json.dumps({"status": "error", "error": f"Table '{project_id}.{dataset}.{table}' not found"})
            return json.dumps({"status": "error", "error": f"HTTP {exc.code}: {_err_body(exc)}"})
        except (URLError, Exception) as exc:
            return json.dumps({"status": "error", "error": str(exc)})

        schema = data.get("schema", {})
        fields = schema.get("fields") or []

        def _flatten(flds: list, prefix: str = "") -> list:
            out = []
            for f in flds:
                name = f"{prefix}{f.get('name', '?')}"
                out.append(
                    {
                        "name": name,
                        "type": f.get("type", "?"),
                        "mode": f.get("mode", "NULLABLE"),
                        "description": f.get("description", ""),
                    }
                )
                sub = f.get("fields")
                if sub:
                    out.extend(_flatten(sub, prefix=f"{name}."))
            return out

        flat_fields = _flatten(fields)
        row_count = data.get("numRows")
        size_bytes = int(data.get("numBytes", 0) or 0)

        return json.dumps(
            {
                "status": "ok",
                "table": f"{project_id}.{dataset}.{table}",
                "row_count": row_count,
                "size_bytes": size_bytes,
                "field_count": len(flat_fields),
                "fields": flat_fields,
            },
            indent=2,
        )

    return [
        bigquery_query,
        bigquery_tables,
        bigquery_schema,
    ]
