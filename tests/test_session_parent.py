"""Parent sessions group child calls without putting them in the sidebar list."""

import pytest

from web.server.database import ChatDatabase
from web.server.session_source import resolve_parent_id


def test_children_are_hidden_until_listed_by_parent(tmp_path):
    db = ChatDatabase(tmp_path / "sessions.db")
    db.create_tables()
    db.create_session("task-1", title="Task", source="research-app")
    db.create_session("wave-1", title="Wave", source="research-app", parent_session_id="task-1")

    roots = db.list_sessions(limit=10, sources=("research-app",))
    children = db.list_sessions(limit=10, sources=("research-app",), parent_id="task-1")

    assert [row.id for row in roots] == ["task-1"]
    assert [row.id for row in children] == ["wave-1"]
    assert children[0].parent_session_id == "task-1"
    assert children[0].to_dict()["parent_session_id"] == "task-1"


def test_parent_must_exist_and_be_a_root(tmp_path):
    db = ChatDatabase(tmp_path / "sessions.db")
    db.create_tables()
    db.create_session("task-1", source="research-app")
    db.create_session("wave-1", source="research-app", parent_session_id="task-1")

    with pytest.raises(ValueError, match="does not exist"):
        db.create_session("wave-2", parent_session_id="missing")
    with pytest.raises(ValueError, match="already a child"):
        db.create_session("wave-3", parent_session_id="wave-1")
    with pytest.raises(ValueError, match="own parent"):
        db.create_session("task-1", parent_session_id="task-1")


def test_parent_id_is_write_once(tmp_path):
    db = ChatDatabase(tmp_path / "sessions.db")
    db.create_tables()
    db.create_session("task-1", source="web")
    db.create_session("wave-1", source="web", parent_session_id="task-1")

    updated = db.update_session("wave-1", title="Renamed", parent_session_id="other")

    assert updated is not None
    assert updated.title == "Renamed"
    assert updated.parent_session_id == "task-1"


def test_delete_parent_removes_children(tmp_path):
    db = ChatDatabase(tmp_path / "sessions.db")
    db.create_tables()
    db.create_session("task-1", source="research-app")
    db.create_session("wave-1", source="research-app", parent_session_id="task-1")
    db.add_message("wave-1", "user", "query", content="look this up")

    assert db.delete_session("task-1") is True
    assert db.get_session("task-1") is None
    assert db.get_session("wave-1") is None
    assert db.get_messages("wave-1") == []


def test_resolve_parent_id_prefers_overrides():
    assert resolve_parent_id(connect_parent="from-url", overrides={"parent_id": "from-body"}) == "from-body"
    assert resolve_parent_id(connect_parent="from-url", overrides=None) == "from-url"
    assert resolve_parent_id(connect_parent="from-url", overrides={"parent_id": "  "}) is None
    assert resolve_parent_id(connect_parent=None, overrides={"parent_session_id": "legacy"}) == "legacy"
