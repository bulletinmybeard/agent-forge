"""Personal Qdrant collections are visible only to the caller when auth is on."""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest
from qdrant_client.models import Filter

from app.security import ADMIN_USER_ID, reset_current_user, set_current_user
from app.services.knowledge_vector_service import KnowledgeVectorService
from app.services.qdrant_owner import (
    backfill_missing_owner,
    delete_personal_points,
    hash_material,
    payload_visible,
    restrict_filter,
)

_OTHER = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def wall(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ADMIN_PASSWORD", "test-password")
    token = set_current_user(_OTHER)
    yield
    reset_current_user(token)


def test_backfill_stamps_only_the_scroll_of_empty_owners():
    missing = types.SimpleNamespace(id="m1", payload={})
    client = MagicMock()
    client.scroll.return_value = ([missing], None)
    stamped = backfill_missing_owner(client, "kb_mail_entries")
    assert stamped == 1
    kwargs = client.set_payload.call_args.kwargs
    assert kwargs["collection_name"] == "kb_mail_entries"
    assert kwargs["points"] == ["m1"]
    assert kwargs["payload"] == {"user_id": ADMIN_USER_ID}


def test_backfill_fallback_leaves_an_existing_owner():
    missing = types.SimpleNamespace(id="m1", payload={})
    owned = types.SimpleNamespace(id="m2", payload={"user_id": _OTHER})
    client = MagicMock()
    client.scroll.side_effect = [RuntimeError("no isempty"), ([missing, owned], None)]
    stamped = backfill_missing_owner(client, "conversation_memory")
    assert stamped == 1
    assert client.set_payload.call_args.kwargs["points"] == ["m1"]


def test_auth_off_shows_a_point_with_no_owner():
    assert payload_visible({"title": "note"}) is True
    assert hash_material("abc") == "abc"


def test_other_user_cannot_read_or_delete_admin_point(wall):
    svc = KnowledgeVectorService()
    client = MagicMock()
    svc._client = client
    point = types.SimpleNamespace(
        id="abc",
        payload={"title": "mail", "user_id": ADMIN_USER_ID, "content_hash": "h"},
    )
    client.retrieve.return_value = [point]
    assert svc.get_by_id("abc") is None
    svc.delete_point("abc")
    client.delete.assert_not_called()


def test_search_requires_the_caller(wall):
    svc = KnowledgeVectorService()
    client = MagicMock()
    svc._client = client
    client.query_points.return_value = types.SimpleNamespace(points=[])
    svc.search(query_vector=[0.1], limit=5)
    must = client.query_points.call_args.kwargs["query_filter"].must
    owners = [item for item in must if getattr(item, "key", None) == "user_id"]
    assert len(owners) == 1
    assert owners[0].match.value == _OTHER


def test_admin_point_id_stays_the_historical_hash(monkeypatch):
    plain = KnowledgeVectorService.generate_point_id("same-body")
    monkeypatch.setenv("AGENTFORGE_ADMIN_PASSWORD", "test-password")
    token = set_current_user(ADMIN_USER_ID)
    try:
        assert KnowledgeVectorService.generate_point_id("same-body") == plain
    finally:
        reset_current_user(token)
    token = set_current_user(_OTHER)
    try:
        assert KnowledgeVectorService.generate_point_id("same-body") != plain
    finally:
        reset_current_user(token)


def test_qdrant_tool_filter_skips_the_indexed_corpus(wall):
    from app.config import settings

    shared = restrict_filter(settings.qdrant.collection_name, None)
    assert shared is None
    personal = restrict_filter(settings.knowledge.mail_collection_name, None)
    assert isinstance(personal, Filter)
    assert personal.must[0].key == "user_id"
    assert personal.must[0].match.value == _OTHER


def test_delete_personal_points_skips_conversation_memory():
    client = MagicMock()
    client.collection_exists.return_value = True
    delete_personal_points(_OTHER, client=client)
    names = [call.kwargs["collection_name"] for call in client.delete.call_args_list]
    assert "conversation_memory" not in names
    assert settings_mail() in names
    for call in client.delete.call_args_list:
        must = call.kwargs["points_selector"].must
        assert must[0].match.value == _OTHER


def settings_mail() -> str:
    from app.config import settings

    return settings.knowledge.mail_collection_name


def test_user_delete_during_pytest_does_not_open_qdrant():
    delete_personal_points(_OTHER)
