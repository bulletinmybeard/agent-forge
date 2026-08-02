"""Direct tool-run API — allowlist, in-process path, async job poll."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web.server import tools_run_api as mod
from web.server.app import app


@pytest.fixture(autouse=True)
def _reset_jobs():
    mod._JOBS.clear()
    yield
    mod._JOBS.clear()


@pytest.fixture
def client():
    with TestClient(app) as tc:
        yield tc


def test_default_allowlist_includes_quality_tools():
    allowed = mod._DEFAULT_ALLOWLIST
    assert "linter_run" in allowed
    assert "test_runner" in allowed
    assert "docker_ps" in allowed
    assert "git_status" in allowed
    assert "shell" not in allowed
    assert "ssh" not in allowed


def test_allowlist_env_allow_and_deny(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_TOOLS_RUN_ALLOW", "shell, custom_tool")
    monkeypatch.setenv("AGENTFORGE_TOOLS_RUN_DENY", "shell")
    allowed = mod._allowlist()
    assert "custom_tool" in allowed
    assert "shell" not in allowed
    assert "linter_run" in allowed


def test_assert_allowed_rejects_shell():
    with pytest.raises(Exception) as ei:
        mod._assert_allowed("shell")
    # FastAPI HTTPException
    assert getattr(ei.value, "status_code", None) == 403


def test_run_allowlist_endpoint(client):
    r = client.get("/api/tools/run-allowlist")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    names = {t["name"] for t in data["allowed_tools"]}
    assert "linter_run" in names
    for entry in data["allowed_tools"]:
        assert "role" in entry
        assert entry["role"] in ("local", "remote")


def test_tools_run_rejects_disallowed(client):
    r = client.post(
        "/api/tools/run",
        json={"tool": "shell", "args": {"command": "echo hi"}},
    )
    assert r.status_code == 403
    assert "not allowed" in r.json()["detail"].lower() or "shell" in r.json()["detail"]


def test_tools_run_empty_tool(client):
    r = client.post("/api/tools/run", json={"tool": "  ", "args": {}})
    assert r.status_code == 400


def test_tools_run_in_process_ok(client, monkeypatch):
    async def _fake_run(tool, args, **_kwargs):
        assert tool == "linter_run"
        assert args.get("path") == "/tmp/proj"
        return "All checks passed", "local", "in_process"

    monkeypatch.setattr(mod, "_run_tool", _fake_run)

    r = client.post(
        "/api/tools/run",
        json={
            "tool": "linter_run",
            "args": {"path": "/tmp/proj", "group": "lint"},
            "wait": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["tool"] == "linter_run"
    assert body["role"] == "local"
    assert body["dispatch"] == "in_process"
    assert "All checks passed" in body["output"]
    assert body["duration_s"] is not None
    assert body["job_id"]


def test_tools_run_error_prefix(client, monkeypatch):
    async def _fake_run(tool, args, **_kwargs):
        return "Error: ruff not found", "local", "in_process"

    monkeypatch.setattr(mod, "_run_tool", _fake_run)

    r = client.post(
        "/api/tools/run",
        json={"tool": "linter_run", "args": {"path": "."}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["error"] and body["error"].startswith("Error:")


def test_tools_run_async_poll(client, monkeypatch):
    async def _fake_run(tool, args, **_kwargs):
        return "pytest: 3 passed", "local", "saq"

    monkeypatch.setattr(mod, "_run_tool", _fake_run)

    r = client.post(
        "/api/tools/run",
        json={"tool": "test_runner", "args": {"path": "tests"}, "wait": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]

    # Background task runs under TestClient's event loop
    for _ in range(50):
        st = client.get(f"/api/tools/run/{job_id}")
        assert st.status_code == 200
        data = st.json()
        if data["status"] in ("done", "error"):
            break
    else:
        pytest.fail("async job never finished")

    assert data["status"] == "done"
    assert "3 passed" in data["output"]
    assert data["dispatch"] == "saq"


def test_tools_run_cancel(client, monkeypatch):
    import asyncio

    started = asyncio.Event()

    async def _fake_run(tool, args, **kwargs):
        cancel_event = kwargs.get("cancel_event")
        started.set()
        # Wait until cancel or timeout
        for _ in range(100):
            if cancel_event is not None and cancel_event.is_set():
                from fastapi import HTTPException

                raise HTTPException(status_code=499, detail="cancelled")
            await asyncio.sleep(0.05)
        return "should not finish", "local", "saq"

    monkeypatch.setattr(mod, "_run_tool", _fake_run)

    r = client.post(
        "/api/tools/run",
        json={"tool": "test_runner", "args": {"path": "tests"}, "wait": False},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    # Give background task a moment to start
    for _ in range(20):
        if client.get(f"/api/tools/run/{job_id}").json()["status"] == "running":
            break

    c = client.delete(f"/api/tools/run/{job_id}")
    assert c.status_code == 200
    assert c.json()["status"] == "cancelled"

    # Eventually settles cancelled
    for _ in range(50):
        st = client.get(f"/api/tools/run/{job_id}").json()
        if st["status"] == "cancelled":
            break
    assert client.get(f"/api/tools/run/{job_id}").json()["status"] == "cancelled"


def test_tools_run_unknown_job(client):
    r = client.get("/api/tools/run/does-not-exist")
    assert r.status_code == 404
