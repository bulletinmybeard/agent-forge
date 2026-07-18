from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentforge.config import reset_config
from agentforge.tools.command_policy_store import clear_runtime_override, reset_db, set_db
from web.server.app import app
from web.server.database import ChatDatabase


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_permissions_api.db"
    db = ChatDatabase(db_path)
    db.create_tables()
    reset_db()
    set_db(db)
    monkeypatch.setenv("AGENTFORGE_CHAT_DB", str(db_path))
    with TestClient(app) as test_client:
        yield test_client
    clear_runtime_override(None)
    reset_db()
    reset_config()


def test_get_effective_policy(client):
    r = client.get("/api/permissions/commands")
    assert r.status_code == 200
    data = r.json()
    assert "shell" in data
    assert "ssh" in data
    assert "yaml" in data["shell"]
    assert "override" in data["shell"]
    assert "effective" in data["shell"]
    assert data["shell"]["override"] is None


def test_validate_command(client):
    r = client.post(
        "/api/permissions/commands/validate",
        json={"tool": "shell", "command": "git status"},
    )
    assert r.status_code == 200
    assert r.json()["action"] in ("allow", "deny", "confirm")


def test_get_overrides_empty(client):
    r = client.get("/api/permissions/commands/overrides")
    assert r.status_code == 200
    data = r.json()
    assert data["shell"] is None
    assert data["ssh"] is None


def test_put_and_get_overrides(client):
    payload = {
        "shell": {
            "mode": "allowlist",
            "allowed_commands": ["git", "ls"],
            "allowed_patterns": [],
            "blocked_patterns": [],
        }
    }
    r = client.put("/api/permissions/commands/overrides", json=payload)
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r = client.get("/api/permissions/commands/overrides")
    assert r.status_code == 200
    assert r.json()["shell"]["mode"] == "allowlist"
    assert r.json()["shell"]["allowed_commands"] == ["git", "ls"]
    assert r.json()["ssh"] is None


def test_delete_single_override(client):
    client.put(
        "/api/permissions/commands/overrides",
        json={"shell": {"mode": "denylist", "blocked_patterns": [r"rm\s+-rf"]}},
    )
    client.put(
        "/api/permissions/commands/overrides",
        json={"ssh": {"mode": "confirm"}},
    )

    r = client.delete("/api/permissions/commands/overrides?tool=shell")
    assert r.status_code == 200
    assert r.json()["deleted"] == 1

    overrides = client.get("/api/permissions/commands/overrides").json()
    assert overrides["shell"] is None
    assert overrides["ssh"] is not None


def test_delete_all_overrides(client):
    client.put(
        "/api/permissions/commands/overrides",
        json={
            "shell": {"mode": "allowlist", "allowed_commands": ["ls"]},
            "ssh": {"mode": "confirm"},
        },
    )

    r = client.delete("/api/permissions/commands/overrides")
    assert r.status_code == 200
    assert r.json()["deleted"] == 2

    overrides = client.get("/api/permissions/commands/overrides").json()
    assert overrides["shell"] is None
    assert overrides["ssh"] is None


def test_validate_draft_policy_without_persisting(client):
    """Draft policy in validate body previews unsaved Web UI edits."""
    r = client.post(
        "/api/permissions/commands/validate",
        json={
            "tool": "shell",
            "command": "npm install",
            "policy": {
                "mode": "allowlist",
                "allowed_commands": ["ls"],
                "allowed_patterns": [],
                "blocked_patterns": [],
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "deny"
    assert "not allowed" in body["reason"].lower()

    # No override persisted — effective policy unchanged.
    assert client.get("/api/permissions/commands/overrides").json()["shell"] is None


def test_validate_denied_under_allowlist_override(client):
    client.put(
        "/api/permissions/commands/overrides",
        json={"shell": {"mode": "allowlist", "allowed_commands": ["ls"]}},
    )
    r = client.post(
        "/api/permissions/commands/validate",
        json={"tool": "shell", "command": "npm install"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "deny"
    assert "not allowed" in body["reason"].lower()
