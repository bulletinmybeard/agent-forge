"""Session source namespacing — keep external clients out of the Agent Chat sidebar."""

from web.server.database import ChatDatabase
from web.server.session_source import normalize_source, resolve_session_source


def test_normalize_source_defaults_to_web():
    assert normalize_source(None) == "web"
    assert normalize_source("") == "web"
    assert normalize_source("KB") == "kb"


def test_list_sessions_filters_by_source(tmp_path):
    db_path = tmp_path / "sessions.db"
    db = ChatDatabase(db_path)
    db.create_tables()

    db.create_session("web-1", source="web")
    db.create_session("kb-1", source="kb")

    web_only = db.list_sessions(limit=10, sources=("web",))
    kb_only = db.list_sessions(limit=10, sources=("kb",))
    all_sessions = db.list_sessions(limit=10, sources=None)

    assert [s.id for s in web_only] == ["web-1"]
    assert [s.id for s in kb_only] == ["kb-1"]
    assert {s.id for s in all_sessions} == {"web-1", "kb-1"}


def test_resolve_session_source_prefers_overrides():
    assert (
        resolve_session_source(
            "s1",
            connect_source="web",
            overrides={"source": "kb"},
        )
        == "kb"
    )


def test_resolve_session_source_uses_connect_param():
    assert resolve_session_source("s1", connect_source="kb", overrides=None) == "kb"


def test_resolve_session_source_defaults_to_web():
    assert resolve_session_source("unknown-session") == "web"
