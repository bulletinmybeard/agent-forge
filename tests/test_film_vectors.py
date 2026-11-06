"""Film Qdrant points belong to the signed-in user."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agentforge.film import vectors
from app.services.qdrant_owner import personal_collection_names, restrict_filter
from web.server.identity import ADMIN_USER_ID, reset_current_user, set_current_user

_OTHER = "00000000-0000-0000-0000-0000000000aa"
_ROW = {
    "tmdb_id": 6,
    "title": "Judgment Night",
    "year": 1993,
    "type": "movie",
    "genres": "Action",
    "overview": "A shortcut goes wrong.",
}


def _client(monkeypatch) -> MagicMock:
    vectors._backfilled.clear()
    client = MagicMock()
    client.get_collections.return_value = SimpleNamespace(collections=[SimpleNamespace(name="film_db")])
    client.retrieve.return_value = []
    client.scroll.return_value = ([], None)
    client.search.return_value = []
    client.count.return_value = SimpleNamespace(count=3)
    monkeypatch.setattr(vectors, "_client", lambda: client)
    monkeypatch.setattr(vectors, "_embed", lambda text: [0.25])
    monkeypatch.setattr(vectors, "_collection_size", lambda *args, **kwargs: 1)
    monkeypatch.setattr(vectors, "collection_name", lambda: "film_db")
    return client


def test_admin_point_id_stays_the_tmdb_id():
    assert vectors.point_id(6) == 6
    token = set_current_user(ADMIN_USER_ID)
    try:
        assert vectors.point_id(6) == 6
    finally:
        reset_current_user(token)
    token = set_current_user(_OTHER)
    try:
        other = vectors.point_id(6)
    finally:
        reset_current_user(token)
    assert other != 6
    assert isinstance(other, str)


def test_upsert_stamps_the_caller_and_does_not_reuse_the_admin_id(monkeypatch):
    client = _client(monkeypatch)
    assert vectors.upsert_media(_ROW) is True
    point = client.upsert.call_args.kwargs["points"][0]
    assert point.id == 6
    assert point.payload["user_id"] == ADMIN_USER_ID
    assert point.payload["tmdb_id"] == 6
    client.upsert.reset_mock()
    token = set_current_user(_OTHER)
    try:
        assert vectors.upsert_media(_ROW) is True
        assert vectors.point_exists(6) is False
    finally:
        reset_current_user(token)
    point = client.upsert.call_args.kwargs["points"][0]
    assert point.id != 6
    assert point.payload["user_id"] == _OTHER
    assert client.retrieve.call_args.kwargs["ids"] == [point.id]


def test_search_filters_to_the_caller(monkeypatch):
    client = _client(monkeypatch)
    assert vectors.search_neighbors("night") == []
    assert "query_filter" not in client.search.call_args.kwargs
    token = set_current_user(_OTHER)
    try:
        assert vectors.search_neighbors("night") == []
    finally:
        reset_current_user(token)
    filt = client.search.call_args.kwargs["query_filter"]
    assert filt.must[0].key == "user_id"
    assert filt.must[0].match.value == _OTHER


def test_first_open_stamps_points_with_no_owner(monkeypatch):
    client = _client(monkeypatch)
    client.scroll.return_value = ([SimpleNamespace(id=6, payload={})], None)
    assert vectors.point_exists(6) is False
    assert client.set_payload.call_args.kwargs["payload"] == {"user_id": ADMIN_USER_ID}
    assert client.set_payload.call_args.kwargs["points"] == [6]
    client.create_payload_index.assert_called()


def test_film_collection_is_personal(monkeypatch):
    monkeypatch.setattr(vectors, "collection_name", lambda: "film_db")
    assert "film_db" in personal_collection_names()
    token = set_current_user(_OTHER)
    try:
        filt = restrict_filter("film_db", None)
    finally:
        reset_current_user(token)
    assert filt is not None
    assert filt.must[0].match.value == _OTHER
