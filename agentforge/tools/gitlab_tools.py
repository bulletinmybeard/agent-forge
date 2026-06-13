"""GitLab tools — manage projects, merge requests, pipelines, and users.

Provides tools for browsing GitLab projects, searching merge requests,
inspecting CI/CD pipelines, reading job logs, and looking up users — all
via the GitLab v4 REST API.

Configuration (config.yaml or environment variables)::

    gitlab:
      url: "https://gitlab.example.com"   # or GITLAB_URL env var
      token: "glpat-..."                   # or GITLAB_TOKEN env var

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.gitlab_tools import register_gitlab_tools

    registry = ToolRegistry()
    register_gitlab_tools(registry)
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml
from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)

_REQUEST_TIMEOUT = 30  # seconds
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"
_gitlab_config_cache: dict | None = None
_connector_override: threading.local = threading.local()


def _get_gitlab_config() -> dict:
    """Load and cache the ``gitlab`` section from the config.yaml."""
    global _gitlab_config_cache
    if _gitlab_config_cache is None:
        cfg: dict = {}
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH) as fh:
                full = yaml.safe_load(fh) or {}
            cfg = full.get("gitlab", {})
        _gitlab_config_cache = cfg
    return _gitlab_config_cache


def _gitlab_url() -> str:
    """Return the GitLab base URL (no trailing slash)."""
    override = getattr(_connector_override, "url", None)
    if override:
        return override
    url = _get_gitlab_config().get("url", "")
    if url:
        return str(url).rstrip("/")
    return os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")


def _gitlab_token() -> str:
    """Return the GitLab personal/project access token."""
    override = getattr(_connector_override, "token", None)
    if override:
        return override
    token = _get_gitlab_config().get("token", "")
    if token:
        return str(token)
    token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        raise ValueError("GitLab token not configured — set gitlab.token in config.yaml or GITLAB_TOKEN env var")
    return token


def _gitlab_read_write() -> bool:
    """Return True if GitLab write operations are enabled."""
    override = getattr(_connector_override, "read_write", None)
    if override is not None:
        return override
    env = os.environ.get("GITLAB_READ_WRITE", "")
    if env:
        return env.lower() in ("1", "true", "yes")
    return bool(_get_gitlab_config().get("read_write", True))


def set_connector_override(url: str, token: str, read_write: bool = True) -> None:
    """Set local config override for connector-injected GitLab credentials."""
    _connector_override.url = url.rstrip("/")
    _connector_override.token = token
    _connector_override.read_write = read_write


def clear_connector_override() -> None:
    """Clear local config override."""
    _connector_override.url = None
    _connector_override.token = None
    _connector_override.read_write = None


def _gitlab_headers() -> dict[str, str]:
    """Build common request headers (auth + JSON)."""
    return {
        "PRIVATE-TOKEN": _gitlab_token(),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def _gl_get(path: str, params: dict | None = None) -> dict | list:
    """Perform a GET request against the GitLab API v4."""
    qs = f"?{urlencode(params)}" if params else ""
    url = f"{_gitlab_url()}/api/v4{path}{qs}"
    req = Request(url, headers=_gitlab_headers())
    with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _gl_post(path: str, data: dict | None = None) -> dict | list:
    """Perform a POST request against the GitLab API v4."""
    url = f"{_gitlab_url()}/api/v4{path}"
    body = json.dumps(data or {}).encode()
    req = Request(url, data=body, headers=_gitlab_headers(), method="POST")
    with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _gl_put(path: str, data: dict | None = None) -> dict | list:
    """Perform a PUT request against the GitLab API v4."""
    url = f"{_gitlab_url()}/api/v4{path}"
    body = json.dumps(data or {}).encode()
    req = Request(url, data=body, headers=_gitlab_headers(), method="PUT")
    with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _gl_get_text(path: str) -> str:
    """Perform a GET request that returns plain text (e.g. job logs)."""
    url = f"{_gitlab_url()}/api/v4{path}"
    headers = _gitlab_headers()
    headers["Accept"] = "text/plain"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _encode_project(project: str) -> str:
    """URL-encode a project path (e.g. 'group/project' > 'group%2Fproject').
    Numeric IDs are passed through as-is."""
    if project.isdigit():
        return project
    return quote(project, safe="")


def _fmt_size(size_bytes: int | None) -> str:
    """Format byte count as human-readable string."""
    if not size_bytes:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} PB"


def _error(msg: str) -> str:
    """Return a JSON error string."""
    return json.dumps({"status": "error", "error": msg})


def _http_error(e: HTTPError) -> str:
    """Format an HTTPError into a JSON error string."""
    body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
    return json.dumps({"status": "error", "http_status": e.code, "error": body or str(e)})


@tool
def gitlab_projects(search: str = "", owned: bool = False, limit: int = 20) -> str:
    """List or search GitLab projects. Use ``owned=true`` to show only your own projects."""
    try:
        params: dict = {
            "per_page": min(limit, 100),
            "order_by": "last_activity_at",
            "sort": "desc",
        }
        if search:
            params["search"] = search
        if owned:
            params["owned"] = "true"
        else:
            params["membership"] = "true"

        projects = _gl_get("/projects", params)
        if not isinstance(projects, list):
            return _error(f"Unexpected response: {projects}")

        lines: list[str] = []
        lines.append(f"**{len(projects)} project(s)**")
        lines.append("")

        for p in projects:
            name = p.get("path_with_namespace", p.get("name", "?"))
            desc = p.get("description") or ""
            branch = p.get("default_branch", "—")
            stars = p.get("star_count", 0)
            forks = p.get("forks_count", 0)
            activity = (p.get("last_activity_at") or "")[:10]

            line = f"• **{name}** (`{branch}`)"
            if desc:
                line += f" — {desc[:80]}"
            meta = []
            if stars:
                meta.append(f"⭐ {stars}")
            if forks:
                meta.append(f"🍴 {forks}")
            if activity:
                meta.append(f"active: {activity}")
            if meta:
                line += f"  [{', '.join(meta)}]"
            lines.append(line)

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_projects error: %s", e)
        return _error(str(e))


@tool
def gitlab_project(project: str) -> str:
    """Get detailed information about a specific GitLab project."""
    try:
        pid = _encode_project(project)
        data = _gl_get(f"/projects/{pid}", {"statistics": "true"})
        if not isinstance(data, dict):
            return _error(f"Unexpected response: {data}")

        name = data.get("path_with_namespace", data.get("name", "?"))
        desc = data.get("description") or "—"
        branch = data.get("default_branch", "—")
        vis = data.get("visibility", "?")
        url = data.get("web_url", "")
        created = (data.get("created_at") or "")[:10]
        activity = (data.get("last_activity_at") or "")[:10]
        stars = data.get("star_count", 0)
        forks = data.get("forks_count", 0)
        open_issues = data.get("open_issues_count", 0)

        stats = data.get("statistics", {})
        repo_size = _fmt_size(stats.get("repository_size"))
        commit_count = stats.get("commit_count", "?")

        lines: list[str] = [
            f"**{name}** ({vis})",
            f"{desc}",
            "",
            f"Branch: `{branch}` | Stars: {stars} | Forks: {forks} | Open issues: {open_issues}",
            f"Commits: {commit_count} | Repo size: {repo_size}",
            f"Created: {created} | Last activity: {activity}",
        ]
        if url:
            lines.append(f"URL: {url}")

        try:
            langs = _gl_get(f"/projects/{pid}/languages")
            if isinstance(langs, dict) and langs:
                lang_str = ", ".join(f"{k} {v:.0f}%" for k, v in list(langs.items())[:5])
                lines.append(f"Languages: {lang_str}")
        except Exception:
            pass

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_project error: %s", e)
        return _error(str(e))


@tool(confirm="Create project '{name}' (visibility: {visibility})?")
def gitlab_create_project(
    name: str,
    visibility: str = "private",
    description: str = "",
    namespace_id: int = 0,
    default_branch: str = "main",
    initialize_with_readme: bool = True,
) -> str:
    """Create a new GitLab project."""
    if not _gitlab_read_write():
        return _error("Write access is disabled for this GitLab connection.")
    try:
        payload: dict = {
            "name": name,
            "visibility": visibility if visibility in ("private", "internal", "public") else "private",
            "default_branch": default_branch,
            "initialize_with_readme": initialize_with_readme,
        }
        if description:
            payload["description"] = description
        if namespace_id:
            payload["namespace_id"] = namespace_id

        project = _gl_post("/projects", payload)
        if not isinstance(project, dict):
            return _error(f"Unexpected response: {project}")

        return json.dumps(
            {
                "status": "ok",
                "project": {
                    "id": project.get("id"),
                    "path_with_namespace": project.get("path_with_namespace", ""),
                    "web_url": project.get("web_url", ""),
                    "visibility": project.get("visibility", ""),
                    "default_branch": project.get("default_branch", ""),
                    "description": project.get("description", ""),
                    "created_at": project.get("created_at", ""),
                },
            },
            indent=2,
        )
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_create_project error: %s", e)
        return _error(str(e))


@tool(
    confirm="DELETE project '{project}'? This permanently removes the repo, MRs, issues, and all data. Cannot be undone."
)
def gitlab_delete_project(project: str) -> str:
    """Delete a GitLab project permanently."""
    if not _gitlab_read_write():
        return _error("Write access is disabled for this GitLab connection.")
    try:
        pid = _encode_project(project)
        url = f"{_gitlab_url()}/api/v4/projects/{pid}"
        req = Request(url, headers=_gitlab_headers(), method="DELETE")
        with urlopen(req, timeout=_REQUEST_TIMEOUT):
            pass  # 202 Accepted

        return json.dumps(
            {
                "status": "ok",
                "message": f"Project '{project}' has been scheduled for deletion.",
            }
        )
    except HTTPError as e:
        if e.code == 404:
            return _error(f"Project '{project}' not found")
        if e.code == 403:
            return _error(f"Insufficient permissions to delete '{project}'")
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_delete_project error: %s", e)
        return _error(str(e))


@tool(confirm="Update project '{project}'?")
def gitlab_update_project(
    project: str,
    name: str = "",
    description: str = "",
    visibility: str = "",
    default_branch: str = "",
    archived: str = "",
) -> str:
    """Update a GitLab project's settings.
    Only the fields you provide will be changed; omitted fields stay as-is.
    """
    if not _gitlab_read_write():
        return _error("Write access is disabled for this GitLab connection.")
    try:
        pid = _encode_project(project)
        payload: dict = {}
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        if visibility in ("private", "internal", "public"):
            payload["visibility"] = visibility
        if default_branch:
            payload["default_branch"] = default_branch
        if archived.lower() in ("true", "false"):
            payload["archived"] = archived.lower() == "true"

        if not payload:
            return _error("No fields to update — provide at least one field to change.")

        result = _gl_put(f"/projects/{pid}", payload)
        if not isinstance(result, dict):
            return _error(f"Unexpected response: {result}")

        return json.dumps(
            {
                "status": "ok",
                "project": {
                    "id": result.get("id"),
                    "path_with_namespace": result.get("path_with_namespace", ""),
                    "name": result.get("name", ""),
                    "description": result.get("description", ""),
                    "visibility": result.get("visibility", ""),
                    "default_branch": result.get("default_branch", ""),
                    "archived": result.get("archived", False),
                    "web_url": result.get("web_url", ""),
                },
            },
            indent=2,
        )
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_update_project error: %s", e)
        return _error(str(e))


@tool
def gitlab_file_content(project: str, file_path: str, ref: str = "") -> str:
    """Read a file's content from a GitLab repository."""
    try:
        pid = _encode_project(project)
        encoded_path = quote(file_path, safe="")
        params: dict = {}
        if ref:
            params["ref"] = ref

        data = _gl_get(f"/projects/{pid}/repository/files/{encoded_path}", params)
        if not isinstance(data, dict):
            return _error(f"Unexpected response: {data}")

        import base64

        content_b64 = data.get("content", "")
        try:
            content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
        except Exception:
            content = content_b64

        file_name = data.get("file_name", file_path.split("/")[-1])
        ext = file_name.rsplit(".", 1)[-1] if "." in file_name else ""

        return json.dumps(
            {
                "status": "ok",
                "file": {
                    "path": data.get("file_path", file_path),
                    "name": file_name,
                    "size": data.get("size", 0),
                    "ref": data.get("ref", ref or "default"),
                    "last_commit_id": data.get("last_commit_id", "")[:12],
                    "content_sha256": data.get("content_sha256", ""),
                    "extension": ext,
                },
                "content": content,
            },
            indent=2,
        )
    except HTTPError as e:
        if e.code == 404:
            return _error(f"File '{file_path}' not found in project '{project}'" + (f" (ref: {ref})" if ref else ""))
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_file_content error: %s", e)
        return _error(str(e))


@tool
def gitlab_commit_diff(project: str, sha: str) -> str:
    """View the diff of a specific commit."""
    try:
        pid = _encode_project(project)

        # Fetch commit metadata
        commit = _gl_get(f"/projects/{pid}/repository/commits/{sha}")
        if not isinstance(commit, dict):
            return _error(f"Unexpected response: {commit}")

        # Fetch the diff
        diffs = _gl_get(f"/projects/{pid}/repository/commits/{sha}/diff")
        if not isinstance(diffs, list):
            return _error(f"Unexpected diff response: {diffs}")

        lines: list[str] = []
        lines.append(f"**Commit {commit.get('short_id', sha[:12])}** by {commit.get('author_name', '?')}")
        lines.append(f"Date: {commit.get('created_at', '?')}")
        lines.append(f"Message: {commit.get('title', '?')}")
        lines.append("")

        stats = commit.get("stats") or {}
        lines.append(f"**{len(diffs)} file(s) changed** — +{stats.get('additions', 0)} −{stats.get('deletions', 0)}")
        lines.append("")

        for d in diffs:
            old_path = d.get("old_path", "")
            new_path = d.get("new_path", "")
            if d.get("new_file"):
                lines.append(f"### {new_path} (new file)")
            elif d.get("deleted_file"):
                lines.append(f"### {old_path} (deleted)")
            elif d.get("renamed_file"):
                lines.append(f"### {old_path} > {new_path} (renamed)")
            else:
                lines.append(f"### {new_path}")

            diff_text = d.get("diff", "")
            if diff_text:
                lines.append(f"```diff\n{diff_text}\n```")
            lines.append("")

        return "\n".join(lines)
    except HTTPError as e:
        if e.code == 404:
            return _error(f"Commit '{sha}' not found in project '{project}'")
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_commit_diff error: %s", e)
        return _error(str(e))


@tool
def gitlab_branches(project: str, search: str = "", limit: int = 20) -> str:
    """List branches in a GitLab project.
    Shows branch name, whether it's protected, and the last commit date.
    """
    try:
        pid = _encode_project(project)
        params: dict = {"per_page": min(limit, 100)}
        if search:
            params["search"] = search

        branches = _gl_get(f"/projects/{pid}/repository/branches", params)
        if not isinstance(branches, list):
            return _error(f"Unexpected response: {branches}")

        lines: list[str] = [f"**{len(branches)} branch(es)** in {project}", ""]

        for b in branches:
            name = b.get("name", "?")
            protected = " 🔒" if b.get("protected") else ""
            default = " (default)" if b.get("default") else ""
            commit = b.get("commit", {})
            date = (commit.get("committed_date") or "")[:10]
            author = commit.get("author_name", "")

            line = f"• `{name}`{protected}{default}"
            if date:
                line += f" — {date}"
            if author:
                line += f" by {author}"
            lines.append(line)

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_branches error: %s", e)
        return _error(str(e))


@tool(confirm="Create branch '{branch}' from '{ref}' in {project}?")
def gitlab_create_branch(project: str, branch: str, ref: str = "main") -> str:
    """Create a new branch in a GitLab project."""
    if not _gitlab_read_write():
        return _error("Write access is disabled for this GitLab connection.")
    try:
        pid = _encode_project(project)
        result = _gl_post(
            f"/projects/{pid}/repository/branches",
            {
                "branch": branch,
                "ref": ref,
            },
        )
        if not isinstance(result, dict):
            return _error(f"Unexpected response: {result}")

        commit = result.get("commit") or {}
        return json.dumps(
            {
                "status": "ok",
                "branch": {
                    "name": result.get("name", ""),
                    "protected": result.get("protected", False),
                    "commit_sha": commit.get("id", "")[:12],
                    "commit_message": commit.get("message", "").strip()[:120],
                },
            },
            indent=2,
        )
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_create_branch error: %s", e)
        return _error(str(e))


@tool(confirm="Delete branch '{branch}' from {project}? This cannot be undone.")
def gitlab_delete_branch(project: str, branch: str) -> str:
    """Delete a branch from a GitLab project.
    Typically used to clean up merged feature branches. Protected branches
    cannot be deleted.
    """
    if not _gitlab_read_write():
        return _error("Write access is disabled for this GitLab connection.")
    try:
        pid = _encode_project(project)
        encoded_branch = quote(branch, safe="")
        url = f"{_gitlab_url()}/api/v4/projects/{pid}/repository/branches/{encoded_branch}"
        req = Request(url, headers=_gitlab_headers(), method="DELETE")
        with urlopen(req, timeout=_REQUEST_TIMEOUT):
            pass  # 204 No Content on success

        return json.dumps(
            {
                "status": "ok",
                "message": f"Branch '{branch}' deleted from {project}.",
            }
        )
    except HTTPError as e:
        if e.code == 404:
            return _error(f"Branch '{branch}' not found in project '{project}'")
        if e.code == 400:
            return _error(f"Cannot delete branch '{branch}' — it may be protected")
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_delete_branch error: %s", e)
        return _error(str(e))


@tool(confirm="Create MR '{title}' ({source_branch} > {target_branch}) in {project}?")
def gitlab_create_merge_request(
    project: str,
    source_branch: str,
    target_branch: str = "main",
    title: str = "",
    description: str = "",
    draft: bool = False,
    assignee_username: str = "",
    labels: str = "",
    remove_source_branch: bool = True,
) -> str:
    """Create a new merge request in a GitLab project."""
    if not _gitlab_read_write():
        return _error("Write access is disabled for this GitLab connection.")
    try:
        pid = _encode_project(project)
        if not title:
            title = source_branch.replace("-", " ").replace("/", ": ").title()
        if draft:
            title = f"Draft: {title}"

        payload: dict = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "remove_source_branch": remove_source_branch,
        }
        if description:
            payload["description"] = description
        if labels:
            payload["labels"] = labels

        if assignee_username:
            users = _gl_get("/users", {"username": assignee_username})
            if isinstance(users, list) and users:
                payload["assignee_id"] = users[0]["id"]

        mr = _gl_post(f"/projects/{pid}/merge_requests", payload)
        if not isinstance(mr, dict):
            return _error(f"Unexpected response: {mr}")

        return json.dumps(
            {
                "status": "ok",
                "merge_request": {
                    "iid": mr.get("iid"),
                    "title": mr.get("title", ""),
                    "state": mr.get("state", ""),
                    "source_branch": mr.get("source_branch", ""),
                    "target_branch": mr.get("target_branch", ""),
                    "web_url": mr.get("web_url", ""),
                    "draft": mr.get("draft", False),
                    "author": (mr.get("author") or {}).get("username", ""),
                    "created_at": mr.get("created_at", ""),
                },
            },
            indent=2,
        )
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_create_merge_request error: %s", e)
        return _error(str(e))


@tool
def gitlab_merge_requests(
    project: str = "",
    state: str = "opened",
    scope: str = "",
    author: str = "",
    search: str = "",
    labels: str = "",
    source_branch: str = "",
    target_branch: str = "",
    limit: int = 20,
) -> str:
    """List or search merge requests across projects or within a specific project."""
    try:
        params: dict = {
            "per_page": min(limit, 100),
            "state": state,
            "order_by": "updated_at",
            "sort": "desc",
        }
        if scope:
            params["scope"] = scope
        if author:
            params["author_username"] = author
        if search:
            params["search"] = search
        if labels:
            params["labels"] = labels
        if source_branch:
            params["source_branch"] = source_branch
        if target_branch:
            params["target_branch"] = target_branch

        if project:
            pid = _encode_project(project)
            mrs = _gl_get(f"/projects/{pid}/merge_requests", params)
        else:
            # Global MR search
            mrs = _gl_get("/merge_requests", params)

        if not isinstance(mrs, list):
            return _error(f"Unexpected response: {mrs}")

        lines: list[str] = [f"**{len(mrs)} merge request(s)** ({state})", ""]

        for mr in mrs:
            iid = mr.get("iid", "?")
            title = mr.get("title", "?")
            author_name = mr.get("author", {}).get("username", "?")
            source = mr.get("source_branch", "?")
            target = mr.get("target_branch", "?")
            proj = mr.get("references", {}).get("full", "") or mr.get("web_url", "")
            updated = (mr.get("updated_at") or "")[:10]
            draft = "📝 " if mr.get("draft") or mr.get("work_in_progress") else ""

            # Pipeline status (included in MR list response)
            pipeline = mr.get("head_pipeline") or {}
            pipe_status = pipeline.get("status", "")
            pipe_icon = {
                "success": "✅",
                "failed": "❌",
                "running": "🔄",
                "pending": "⏳",
                "canceled": "🚫",
                "skipped": "⏭️",
            }.get(pipe_status, "")

            approvals = ""
            upvotes = mr.get("upvotes", 0)
            if upvotes:
                approvals = f" 👍 {upvotes}"

            line = f"• {draft}**!{iid}** {title}"
            line += f"\n  `{source}` > `{target}` | {author_name} | {updated}"
            if pipe_icon:
                line += f" | CI: {pipe_icon} {pipe_status}"
            if approvals:
                line += approvals
            if proj:
                line += f"\n  {proj}"

            lines.append(line)

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_merge_requests error: %s", e)
        return _error(str(e))


@tool
def gitlab_merge_request(project: str, mr_iid: int) -> str:
    """Get detailed information about a specific merge request."""
    try:
        pid = _encode_project(project)
        mr = _gl_get(f"/projects/{pid}/merge_requests/{mr_iid}")
        if not isinstance(mr, dict):
            return _error(f"Unexpected response: {mr}")

        title = mr.get("title", "?")
        desc = mr.get("description") or "—"
        state = mr.get("state", "?")
        author = mr.get("author", {}).get("username", "?")
        source = mr.get("source_branch", "?")
        target = mr.get("target_branch", "?")
        created = (mr.get("created_at") or "")[:10]
        updated = (mr.get("updated_at") or "")[:10]
        merged_by = mr.get("merged_by", {}).get("username") if mr.get("merged_by") else None
        url = mr.get("web_url", "")
        draft = "📝 DRAFT " if mr.get("draft") or mr.get("work_in_progress") else ""
        labels = mr.get("labels", [])
        conflicts = mr.get("has_conflicts", False)

        diff_stats = mr.get("diff_stats") or {}
        additions = diff_stats.get("additions", mr.get("changes_count", "?"))
        deletions = diff_stats.get("deletions", "?")
        files_changed = diff_stats.get("file_count", "?")

        reviewers = [r.get("username", "?") for r in mr.get("reviewers", [])]
        assignees = [a.get("username", "?") for a in mr.get("assignees", [])]

        pipeline = mr.get("head_pipeline") or {}
        pipe_status = pipeline.get("status", "none")
        pipe_id = pipeline.get("id")

        lines: list[str] = [
            f"**!{mr_iid}** {draft}{title} [{state.upper()}]",
            "",
            f"{desc[:500]}{'…' if len(desc) > 500 else ''}",
            "",
            f"Author: {author} | `{source}` > `{target}`",
            f"Created: {created} | Updated: {updated}",
        ]
        if merged_by:
            lines.append(f"Merged by: {merged_by}")
        if assignees:
            lines.append(f"Assignees: {', '.join(assignees)}")
        if reviewers:
            lines.append(f"Reviewers: {', '.join(reviewers)}")
        if labels:
            lines.append(f"Labels: {', '.join(labels)}")
        if conflicts:
            lines.append("⚠️ Has merge conflicts")

        lines.append(f"Changes: +{additions} −{deletions} in {files_changed} file(s)")
        lines.append(f"Pipeline: {pipe_status}" + (f" (#{pipe_id})" if pipe_id else ""))

        # Fetch approvals
        try:
            approvals = _gl_get(f"/projects/{pid}/merge_requests/{mr_iid}/approvals")
            if isinstance(approvals, dict):
                approved = approvals.get("approved", False)
                approved_by = [a.get("user", {}).get("username", "?") for a in approvals.get("approved_by", [])]
                if approved_by:
                    lines.append(f"Approved by: {', '.join(approved_by)}")
                elif approved:
                    lines.append("Status: Approved")
                else:
                    rules = approvals.get("approvals_required", 0)
                    left = approvals.get("approvals_left", 0)
                    if rules:
                        lines.append(f"Approvals: {rules - left}/{rules} required")
        except Exception:
            pass

        if url:
            lines.append(f"URL: {url}")

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_merge_request error: %s", e)
        return _error(str(e))


@tool
def gitlab_merge_request_changes(project: str, mr_iid: int) -> str:
    """Get the diff (changed files) of a merge request."""
    try:
        pid = _encode_project(project)
        data = _gl_get(f"/projects/{pid}/merge_requests/{mr_iid}/changes")
        if not isinstance(data, dict):
            return _error(f"Unexpected response: {data}")

        changes = data.get("changes", [])
        lines: list[str] = [
            f"**!{mr_iid}** — {len(changes)} file(s) changed",
            "",
        ]

        total_lines = 0
        max_lines = 200  # cap total output

        for c in changes:
            if total_lines > max_lines:
                lines.append(
                    f"\n_... {len(changes) - len([l for l in lines if l.startswith('###')])} more file(s) truncated_"
                )
                break

            path = c.get("new_path") or c.get("old_path") or "?"
            new_file = c.get("new_file", False)
            deleted = c.get("deleted_file", False)
            renamed = c.get("renamed_file", False)

            status = ""
            if new_file:
                status = " (new)"
            elif deleted:
                status = " (deleted)"
            elif renamed:
                old = c.get("old_path", "")
                status = f" (renamed from {old})"

            lines.append(f"### {path}{status}")

            diff = c.get("diff", "")
            if diff:
                # Truncate individual file diffs
                diff_lines = diff.split("\n")
                if len(diff_lines) > 40:
                    diff_lines = diff_lines[:40]
                    diff_lines.append(f"... ({len(c.get('diff', '').split(chr(10)))} lines total)")
                lines.append(f"```diff\n{chr(10).join(diff_lines)}\n```")
                total_lines += len(diff_lines)
            lines.append("")

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_merge_request_changes error: %s", e)
        return _error(str(e))


@tool(confirm="Update MR !{mr_iid} in {project}?")
def gitlab_merge_request_update(
    project: str,
    mr_iid: int,
    title: str = "",
    description: str = "",
    draft: str = "",
    target_branch: str = "",
    labels: str = "",
    assignee_usernames: str = "",
    reviewer_usernames: str = "",
    milestone_id: int = 0,
    remove_source_branch: str = "",
    state_event: str = "",
) -> str:
    """Update a merge request and set draft status, title, labels, assignees, reviewers, etc."""
    try:
        pid = _encode_project(project)
        payload: dict = {}

        if title:
            payload["title"] = title
        if description:
            payload["description"] = description
        if draft.lower() in ("true", "false"):
            payload["title"] = payload.get("title", "")
            if draft.lower() == "true":
                # Fetch current title if not provided, to prepend "Draft: "
                if not payload["title"]:
                    current = _gl_get(f"/projects/{pid}/merge_requests/{mr_iid}")
                    if isinstance(current, dict):
                        cur_title = current.get("title", "")
                        if not cur_title.startswith("Draft: "):
                            payload["title"] = f"Draft: {cur_title}"
                        else:
                            payload["title"] = cur_title
                elif not payload["title"].startswith("Draft: "):
                    payload["title"] = f"Draft: {payload['title']}"
            elif draft.lower() == "false":
                if not payload["title"]:
                    current = _gl_get(f"/projects/{pid}/merge_requests/{mr_iid}")
                    if isinstance(current, dict):
                        cur_title = current.get("title", "")
                        payload["title"] = cur_title.removeprefix("Draft: ").removeprefix("WIP: ")
                else:
                    payload["title"] = payload["title"].removeprefix("Draft: ").removeprefix("WIP: ")
        if target_branch:
            payload["target_branch"] = target_branch
        if labels:
            payload["labels"] = labels
        if assignee_usernames:
            # Resolve usernames to IDs
            unames = [u.strip() for u in assignee_usernames.split(",") if u.strip()]
            ids = []
            for uname in unames:
                users = _gl_get("/users", {"username": uname})
                if isinstance(users, list) and users:
                    ids.append(users[0]["id"])
            if ids:
                payload["assignee_ids"] = ids
        if reviewer_usernames:
            unames = [u.strip() for u in reviewer_usernames.split(",") if u.strip()]
            ids = []
            for uname in unames:
                users = _gl_get("/users", {"username": uname})
                if isinstance(users, list) and users:
                    ids.append(users[0]["id"])
            if ids:
                payload["reviewer_ids"] = ids
        if milestone_id:
            payload["milestone_id"] = milestone_id
        if remove_source_branch.lower() in ("true", "false"):
            payload["remove_source_branch"] = remove_source_branch.lower() == "true"
        if state_event.lower() in ("close", "reopen"):
            payload["state_event"] = state_event.lower()

        if not payload:
            return "⚠️ No fields to update — provide at least one field to change."

        result = _gl_put(f"/projects/{pid}/merge_requests/{mr_iid}", payload)
        if not isinstance(result, dict):
            return _error(f"Unexpected response: {result}")

        new_title = result.get("title", "?")
        new_state = result.get("state", "?")
        is_draft = result.get("draft") or result.get("work_in_progress") or False
        new_labels = result.get("labels", [])
        assignees = [a.get("username", "?") for a in result.get("assignees", [])]
        reviewers = [r.get("username", "?") for r in result.get("reviewers", [])]

        lines: list[str] = [
            f"✅ Updated **!{mr_iid}** in {project}",
            "",
            f"Title: {new_title}",
            f"State: {new_state} | Draft: {'yes' if is_draft else 'no'}",
        ]
        if new_labels:
            lines.append(f"Labels: {', '.join(new_labels)}")
        if assignees:
            lines.append(f"Assignees: {', '.join(assignees)}")
        if reviewers:
            lines.append(f"Reviewers: {', '.join(reviewers)}")

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_merge_request_update error: %s", e)
        return _error(str(e))


@tool(confirm="Approve MR !{mr_iid} in {project}?")
def gitlab_merge_request_approve(project: str, mr_iid: int) -> str:
    """Approve a merge request."""
    try:
        pid = _encode_project(project)
        result = _gl_post(f"/projects/{pid}/merge_requests/{mr_iid}/approve")
        if isinstance(result, dict):
            approved_by = [a.get("user", {}).get("username", "?") for a in result.get("approved_by", [])]
            approvals_left = result.get("approvals_left", 0)
            lines = [f"✅ Approved MR !{mr_iid} in {project}"]
            if approved_by:
                lines.append(f"Approved by: {', '.join(approved_by)}")
            if approvals_left:
                lines.append(f"Approvals still needed: {approvals_left}")
            else:
                lines.append("All required approvals met.")
            return "\n".join(lines)
        return f"✅ Approved MR !{mr_iid} in {project}."
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_merge_request_approve error: %s", e)
        return _error(str(e))


@tool(confirm="Merge MR !{mr_iid} in {project}? This will merge the source branch into the target.")
def gitlab_merge_request_merge(
    project: str,
    mr_iid: int,
    merge_when_pipeline_succeeds: bool = False,
    squash: bool = False,
    should_remove_source_branch: bool = False,
) -> str:
    """Merge an approved merge request."""
    try:
        pid = _encode_project(project)
        payload: dict = {}
        if merge_when_pipeline_succeeds:
            payload["merge_when_pipeline_succeeds"] = True
        if squash:
            payload["squash"] = True
        if should_remove_source_branch:
            payload["should_remove_source_branch"] = True

        result = _gl_put(f"/projects/{pid}/merge_requests/{mr_iid}/merge", payload)
        if isinstance(result, dict):
            state = result.get("state", "?")
            title = result.get("title", "?")
            merged_by = result.get("merged_by", {}).get("username", "") if result.get("merged_by") else ""
            target = result.get("target_branch", "?")

            if state == "merged":
                lines = [f"✅ Merged **!{mr_iid}** ({title}) into `{target}`"]
                if merged_by:
                    lines.append(f"Merged by: {merged_by}")
                return "\n".join(lines)
            elif merge_when_pipeline_succeeds:
                return f"⏳ MR !{mr_iid} set to auto-merge when pipeline succeeds."
            else:
                return f"MR !{mr_iid} state: {state}"
        return f"✅ Merge request !{mr_iid} merge triggered."
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        # Common errors: 405 = can't merge (conflicts, approvals), 406 = already merged
        if e.code == 405:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            return _error(
                f"MR !{mr_iid} cannot be merged — check for conflicts, pending approvals, or failed pipeline. {body}"
            )
        if e.code == 406:
            return f"MR !{mr_iid} is already merged."
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_merge_request_merge error: %s", e)
        return _error(str(e))


@tool
def gitlab_pipelines(
    project: str,
    status: str = "",
    ref: str = "",
    username: str = "",
    limit: int = 20,
) -> str:
    """List recent CI/CD pipelines for a project."""
    try:
        pid = _encode_project(project)
        params: dict = {
            "per_page": min(limit, 100),
            "order_by": "updated_at",
            "sort": "desc",
        }
        if status:
            params["status"] = status
        if ref:
            params["ref"] = ref
        if username:
            params["username"] = username

        pipelines = _gl_get(f"/projects/{pid}/pipelines", params)
        if not isinstance(pipelines, list):
            return _error(f"Unexpected response: {pipelines}")

        status_icons = {
            "success": "✅",
            "failed": "❌",
            "running": "🔄",
            "pending": "⏳",
            "canceled": "🚫",
            "skipped": "⏭️",
            "created": "🆕",
            "manual": "👆",
        }

        lines: list[str] = [f"**{len(pipelines)} pipeline(s)** in {project}", ""]

        for p in pipelines:
            pid_num = p.get("id", "?")
            p_status = p.get("status", "?")
            ref_name = p.get("ref", "?")
            icon = status_icons.get(p_status, "❓")
            created = (p.get("created_at") or "")[:16].replace("T", " ")
            source = p.get("source", "")
            duration = p.get("duration")

            dur_str = ""
            if duration:
                mins, secs = divmod(int(duration), 60)
                dur_str = f" ({mins}m {secs}s)" if mins else f" ({secs}s)"

            line = f"• {icon} **#{pid_num}** [{p_status}] `{ref_name}`"
            if source:
                line += f" ({source})"
            if dur_str:
                line += dur_str
            if created:
                line += f" — {created}"
            lines.append(line)

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_pipelines error: %s", e)
        return _error(str(e))


@tool
def gitlab_pipeline_jobs(project: str, pipeline_id: int) -> str:
    """List all jobs in a specific CI/CD pipeline."""
    try:
        pid = _encode_project(project)
        jobs = _gl_get(f"/projects/{pid}/pipelines/{pipeline_id}/jobs", {"per_page": 100})
        if not isinstance(jobs, list):
            return _error(f"Unexpected response: {jobs}")

        status_icons = {
            "success": "✅",
            "failed": "❌",
            "running": "🔄",
            "pending": "⏳",
            "canceled": "🚫",
            "skipped": "⏭️",
            "created": "🆕",
            "manual": "👆",
        }

        lines: list[str] = [f"**Pipeline #{pipeline_id}** — {len(jobs)} job(s)", ""]

        # Group by stage
        stages: dict[str, list] = {}
        for j in jobs:
            stage = j.get("stage", "unknown")
            stages.setdefault(stage, []).append(j)

        for stage, stage_jobs in stages.items():
            lines.append(f"**Stage: {stage}**")
            for j in stage_jobs:
                jid = j.get("id", "?")
                name = j.get("name", "?")
                j_status = j.get("status", "?")
                icon = status_icons.get(j_status, "❓")
                duration = j.get("duration")
                runner = j.get("runner", {})
                runner_desc = runner.get("description", "") if runner else ""
                has_artifacts = bool(j.get("artifacts", []))

                dur_str = ""
                if duration:
                    mins, secs = divmod(int(duration), 60)
                    dur_str = f" {mins}m {secs}s" if mins else f" {secs}s"

                line = f"  {icon} **{name}** [{j_status}]{dur_str}"
                if has_artifacts:
                    line += " 📦"
                if runner_desc:
                    line += f" (runner: {runner_desc[:30]})"
                line += f"  `job:{jid}`"
                lines.append(line)
            lines.append("")

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_pipeline_jobs error: %s", e)
        return _error(str(e))


@tool
def gitlab_job_log(project: str, job_id: int, tail: int = 100) -> str:
    """Read the log output of a specific CI/CD job."""
    try:
        pid = _encode_project(project)
        log_text = _gl_get_text(f"/projects/{pid}/jobs/{job_id}/trace")

        # Return last N lines
        lines = log_text.split("\n")
        tail = min(tail, 500)
        if len(lines) > tail:
            truncated = lines[-tail:]
            header = f"_... showing last {tail} of {len(lines)} lines ..._\n\n"
        else:
            truncated = lines
            header = ""

        return f"**Job #{job_id} log:**\n\n{header}```\n{chr(10).join(truncated)}\n```"
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_job_log error: %s", e)
        return _error(str(e))


@tool(confirm="Retry failed pipeline #{pipeline_id} in {project}?")
def gitlab_pipeline_retry(project: str, pipeline_id: int) -> str:
    """Retry all failed jobs in a CI/CD pipeline."""
    try:
        pid = _encode_project(project)
        result = _gl_post(f"/projects/{pid}/pipelines/{pipeline_id}/retry")
        if isinstance(result, dict):
            new_status = result.get("status", "?")
            return f"✅ Pipeline #{pipeline_id} retry triggered — status: {new_status}"
        return f"✅ Pipeline #{pipeline_id} retry triggered."
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_pipeline_retry error: %s", e)
        return _error(str(e))


@tool(confirm="Cancel running pipeline #{pipeline_id} in {project}?")
def gitlab_pipeline_cancel(project: str, pipeline_id: int) -> str:
    """Cancel a running CI/CD pipeline."""
    try:
        pid = _encode_project(project)
        result = _gl_post(f"/projects/{pid}/pipelines/{pipeline_id}/cancel")
        if isinstance(result, dict):
            new_status = result.get("status", "?")
            return f"✅ Pipeline #{pipeline_id} cancelled — status: {new_status}"
        return f"✅ Pipeline #{pipeline_id} cancelled."
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_pipeline_cancel error: %s", e)
        return _error(str(e))


@tool
def gitlab_runners(
    scope: str = "",
    status: str = "",
    runner_type: str = "",
    tag_list: str = "",
    project: str = "",
    limit: int = 20,
) -> str:
    """List CI/CD runners accessible to the authenticated user."""
    try:
        params: dict = {"per_page": min(limit, 100)}
        if scope:
            params["scope"] = scope
        if status:
            params["status"] = status
        if runner_type:
            params["type"] = runner_type
        if tag_list:
            params["tag_list"] = tag_list

        if project:
            pid = _encode_project(project)
            runners = _gl_get(f"/projects/{pid}/runners", params)
        else:
            runners = _gl_get("/runners/all", params)

        if not isinstance(runners, list):
            return _error(f"Unexpected response: {runners}")

        status_icons = {
            "online": "🟢",
            "offline": "🔴",
            "stale": "🟡",
            "never_contacted": "⚪",
            "paused": "⏸️",
        }

        lines: list[str] = [f"**{len(runners)} runner(s)**", ""]

        for r in runners:
            rid = r.get("id", "?")
            desc = r.get("description") or "—"
            r_status = r.get("status", "?")
            active = r.get("active", True)
            is_shared = r.get("is_shared", False)
            rtype = r.get("runner_type", "")
            tags = r.get("tag_list", [])
            ip = r.get("ip_address", "")

            icon = status_icons.get(r_status, "❓")
            if not active:
                icon = "⏸️"
                r_status = "paused"

            type_label = ""
            if rtype == "instance_type":
                type_label = " (shared)"
            elif rtype == "group_type":
                type_label = " (group)"
            elif rtype == "project_type":
                type_label = " (project)"
            elif is_shared:
                type_label = " (shared)"

            line = f"• {icon} **#{rid}** {desc[:50]}{type_label} [{r_status}]"
            if tags:
                line += f"  tags: `{', '.join(tags)}`"
            if ip:
                line += f"  IP: {ip}"
            lines.append(line)

        return "\n".join(lines)
    except HTTPError as e:
        # Fallback: if /runners/all fails (non-admin), try /runners
        if not project and e.code == 403:
            try:
                params_fallback: dict = {"per_page": min(limit, 100)}
                if scope:
                    params_fallback["scope"] = scope
                if status:
                    params_fallback["status"] = status
                if runner_type:
                    params_fallback["type"] = runner_type
                if tag_list:
                    params_fallback["tag_list"] = tag_list
                runners = _gl_get("/runners", params_fallback)
                if not isinstance(runners, list):
                    return _error(f"Unexpected response: {runners}")

                status_icons = {
                    "online": "🟢",
                    "offline": "🔴",
                    "stale": "🟡",
                    "never_contacted": "⚪",
                    "paused": "⏸️",
                }
                lines = [f"**{len(runners)} runner(s)** (user-accessible)", ""]
                for r in runners:
                    rid = r.get("id", "?")
                    desc = r.get("description") or "—"
                    r_status = r.get("status", "?")
                    active = r.get("active", True)
                    icon = status_icons.get(r_status, "❓")
                    if not active:
                        icon = "⏸️"
                        r_status = "paused"
                    tags = r.get("tag_list", [])
                    line = f"• {icon} **#{rid}** {desc[:50]} [{r_status}]"
                    if tags:
                        line += f"  tags: `{', '.join(tags)}`"
                    lines.append(line)
                return "\n".join(lines)
            except Exception:
                pass
        return _http_error(e)
    except ValueError as e:
        return _error(str(e))
    except (URLError, Exception) as e:
        logger.error("gitlab_runners error: %s", e)
        return _error(str(e))


@tool
def gitlab_runner(runner_id: int) -> str:
    """Get detailed information about a specific CI/CD runner."""
    try:
        data = _gl_get(f"/runners/{runner_id}")
        if not isinstance(data, dict):
            return _error(f"Unexpected response: {data}")

        rid = data.get("id", "?")
        desc = data.get("description") or "—"
        active = data.get("active", True)
        status = data.get("status", "?")
        is_shared = data.get("is_shared", False)
        rtype = data.get("runner_type", "")
        locked = data.get("locked", False)
        run_untagged = data.get("run_untagged", False)
        tags = data.get("tag_list", [])
        ip = data.get("ip_address") or "—"
        platform = data.get("platform") or "—"
        arch = data.get("architecture") or "—"
        version = data.get("version") or "—"
        revision = data.get("revision") or ""
        contacted = (data.get("contacted_at") or "")[:16].replace("T", " ")
        created = (data.get("created_at") or "")[:10]
        max_timeout = data.get("maximum_timeout")

        status_icon = {"online": "🟢", "offline": "🔴", "stale": "🟡", "never_contacted": "⚪"}.get(status, "❓")
        if not active:
            status_icon = "⏸️"
            status = "paused"

        type_label = {
            "instance_type": "Shared (instance)",
            "group_type": "Group",
            "project_type": "Project",
        }.get(rtype, "Shared" if is_shared else "Specific")

        lines: list[str] = [
            f"{status_icon} **Runner #{rid}** — {desc}",
            "",
            f"Status: {status} | Active: {'yes' if active else 'no'} | Locked: {'yes' if locked else 'no'}",
            f"Type: {type_label} | Run untagged: {'yes' if run_untagged else 'no'}",
            f"Platform: {platform} | Arch: {arch} | Version: {version}" + (f" ({revision[:8]})" if revision else ""),
            f"IP: {ip} | Last contact: {contacted} | Created: {created}",
        ]
        if tags:
            lines.append(f"Tags: `{', '.join(tags)}`")
        if max_timeout:
            lines.append(f"Max job timeout: {max_timeout}s")

        # Fetch projects associated with this runner
        try:
            projects = _gl_get(f"/runners/{runner_id}/jobs", {"per_page": 5})
            if isinstance(projects, list) and projects:
                proj_names = set()
                for j in projects:
                    pipeline = j.get("pipeline", {})
                    pref = pipeline.get("project_id")
                    if pref:
                        proj_names.add(str(pref))
                if proj_names:
                    lines.append(f"Recent project IDs: {', '.join(sorted(proj_names))}")
        except Exception:
            pass

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_runner error: %s", e)
        return _error(str(e))


@tool(confirm="Pause runner #{runner_id}? It will stop picking up new jobs.")
def gitlab_runner_pause(runner_id: int) -> str:
    """Pause a CI/CD runner so it stops picking up new jobs."""
    try:
        result = _gl_put(f"/runners/{runner_id}", {"active": False})
        if isinstance(result, dict):
            desc = result.get("description", f"#{runner_id}")
            return f"⏸️ Runner #{runner_id} ({desc}) paused — it will not pick up new jobs."
        return f"⏸️ Runner #{runner_id} paused."
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_runner_pause error: %s", e)
        return _error(str(e))


@tool(confirm="Resume runner #{runner_id}? It will start picking up new jobs again.")
def gitlab_runner_resume(runner_id: int) -> str:
    """Resume a paused CI/CD runner so it starts accepting jobs again."""
    try:
        result = _gl_put(f"/runners/{runner_id}", {"active": True})
        if isinstance(result, dict):
            desc = result.get("description", f"#{runner_id}")
            return f"▶️ Runner #{runner_id} ({desc}) resumed — it will now accept new jobs."
        return f"▶️ Runner #{runner_id} resumed."
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_runner_resume error: %s", e)
        return _error(str(e))


@tool
def gitlab_users(search: str = "", limit: int = 20) -> str:
    """Search for GitLab users by name or username."""
    try:
        params: dict = {"per_page": min(limit, 100)}
        if search:
            params["search"] = search

        users = _gl_get("/users", params)
        if not isinstance(users, list):
            return _error(f"Unexpected response: {users}")

        lines: list[str] = [f"**{len(users)} user(s)**", ""]

        for u in users:
            username = u.get("username", "?")
            name = u.get("name", "")
            state = u.get("state", "?")
            admin = " 👑" if u.get("is_admin") else ""
            bot = " 🤖" if u.get("bot") else ""
            last_sign = (u.get("last_sign_in_at") or "")[:10]

            line = f"• **@{username}**{admin}{bot}"
            if name and name != username:
                line += f" ({name})"
            line += f" [{state}]"
            if last_sign:
                line += f" — last seen: {last_sign}"
            lines.append(line)

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_users error: %s", e)
        return _error(str(e))


@tool
def gitlab_user_events(username: str, limit: int = 20) -> str:
    """Get recent activity events for a GitLab user."""
    try:
        # First resolve username > user ID
        users = _gl_get("/users", {"username": username})
        if not isinstance(users, list) or not users:
            return _error(f"User '{username}' not found")
        user_id = users[0].get("id")

        events = _gl_get(f"/users/{user_id}/events", {"per_page": min(limit, 100)})
        if not isinstance(events, list):
            return _error(f"Unexpected response: {events}")

        lines: list[str] = [f"**@{username}** — {len(events)} recent event(s)", ""]

        action_icons = {
            "pushed to": "📤",
            "pushed new": "📤",
            "opened": "🆕",
            "closed": "🔴",
            "merged": "🟣",
            "accepted": "✅",
            "approved": "👍",
            "commented on": "💬",
            "created": "🆕",
            "updated": "✏️",
            "deleted": "🗑️",
            "joined": "👋",
            "left": "👋",
        }

        for ev in events:
            action = ev.get("action_name", "?")
            target_type = ev.get("target_type") or ev.get("push_data", {}).get("ref_type", "")
            target_title = ev.get("target_title") or ""
            created = (ev.get("created_at") or "")[:16].replace("T", " ")

            icon = "📌"
            for key, ic in action_icons.items():
                if key in action.lower():
                    icon = ic
                    break

            line = f"• {icon} **{action}**"
            if target_type:
                line += f" {target_type}"
            if target_title:
                line += f": {target_title[:80]}"
            if created:
                line += f" ({created})"
            lines.append(line)

        return "\n".join(lines)
    except ValueError as e:
        return _error(str(e))
    except HTTPError as e:
        return _http_error(e)
    except (URLError, Exception) as e:
        logger.error("gitlab_user_events error: %s", e)
        return _error(str(e))


def register_gitlab_tools(registry: "ToolRegistry") -> int:
    """Register GitLab tools with the given registry."""
    rw = _gitlab_read_write()

    # Read-only tools are always registered
    tools: list = [
        # Projects & repos
        gitlab_projects,
        gitlab_project,
        gitlab_branches,
        # Merge requests (read)
        gitlab_merge_requests,
        gitlab_merge_request,
        gitlab_merge_request_changes,
        # CI/CD (read)
        gitlab_pipelines,
        gitlab_pipeline_jobs,
        gitlab_job_log,
        # Runners (read)
        gitlab_runners,
        gitlab_runner,
        # Users
        gitlab_users,
        gitlab_user_events,
    ]

    # Write tools. Only when read_write is enabled
    if rw:
        tools.extend(
            [
                gitlab_merge_request_update,  # MR: draft, labels, assignees, reviewers
                gitlab_merge_request_approve,  # MR: approve
                gitlab_merge_request_merge,  # MR: merge
                gitlab_pipeline_retry,  # CI/CD: retry failed pipeline
                gitlab_pipeline_cancel,  # CI/CD: cancel running pipeline
                gitlab_runner_pause,  # Runner: pause
                gitlab_runner_resume,  # Runner: resume
            ]
        )
        logger.debug("GitLab read-write mode: write tools enabled")
    else:
        logger.debug("GitLab read-only mode: write tools disabled")

    for fn in tools:
        registry.register(fn)
    logger.debug("Registered %d GitLab tools (%s)", len(tools), "read-write" if rw else "read-only")
    return len(tools)
