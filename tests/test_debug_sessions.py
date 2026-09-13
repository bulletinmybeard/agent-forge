# tests/test_debug_sessions.py
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from web.server import api as api_module
from web.server.app import app
from web.server.database import ChatDatabase

SID = "019d14f2-aaaa-bbbb-cccc-ddddeeeeffff"


@pytest.fixture
def client(tmp_path):
    db = ChatDatabase(tmp_path / "debug.db")
    db.create_tables()
    db.create_session(SID, title="debug me")
    db.add_message(SID, role="user", msg_type="query", content="hi", tool_calls=[{"name": "shell"}])
    with TestClient(app) as tc:
        api_module.set_database(db)
        yield tc, db


def test_unknown_session_404(client):
    tc, _db = client
    assert tc.get("/api/debug/sessions/nope").status_code == 404


def test_bundle_without_loki(client, monkeypatch):
    tc, _db = client
    monkeypatch.delenv("LOKI_URL", raising=False)
    with patch("web.server.debug_sessions.get_audit_log", return_value=None):
        body = tc.get(f"/api/debug/sessions/{SID}").json()
    assert body["session"]["id"] == SID
    assert body["messages"][0]["tool_calls"][0]["name"] == "shell"
    assert body["audit_tools"][0]["tool_name"] == "shell"
    assert body["audit_tools"][0]["source"] == "messages"
    assert body["logs"] == []
    assert body["logs_error"]
    assert body["truncated"] is False
    assert tc.get(f"/api/debug/sessions/{SID}").status_code == 200


def test_audit_fallback_skips_when_redis_has_rows(client, monkeypatch):
    tc, _db = client
    monkeypatch.delenv("LOKI_URL", raising=False)

    class _Audit:
        async def query_tool_executions(self, **_k):
            return [{"session_id": SID, "tool_name": "from_redis", "source": "redis"}]

        async def query_agent_runs(self, **_k):
            return [{"session_id": SID, "event": "complete"}]

    with patch("web.server.debug_sessions.get_audit_log", return_value=_Audit()):
        body = tc.get(f"/api/debug/sessions/{SID}").json()
    assert [r["tool_name"] for r in body["audit_tools"]] == ["from_redis"]
    assert body["audit_runs"][0]["event"] == "complete"


def test_loki_failure_still_200(client, monkeypatch):
    tc, _db = client
    monkeypatch.setenv("LOKI_URL", "http://loki:3100")

    async def boom(*_a, **_k):
        raise RuntimeError("loki down")

    with (
        patch("web.server.debug_sessions.get_audit_log", return_value=None),
        patch("web.server.debug_sessions.query_session_logs", side_effect=boom),
    ):
        resp = tc.get(f"/api/debug/sessions/{SID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["id"] == SID
    assert body["logs"] == []
    assert "loki down" in body["logs_error"]
