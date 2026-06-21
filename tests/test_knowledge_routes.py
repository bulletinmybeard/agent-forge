"""Tests for /knowledge API routes.

Uses FastAPI TestClient with mocked services.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def mock_knowledge_service():
    with patch("app.routes.knowledge.knowledge_service") as mock:
        yield mock


@pytest.fixture()
def client(mock_knowledge_service):
    with (
        patch("app.services.knowledge_vector_service.knowledge_vector_service") as mock_kv,
        patch("app.services.vector_service.vector_service") as mock_vs,
    ):
        mock_kv.ensure_collection.return_value = None
        mock_vs.ensure_collection.return_value = None
        from app.main import app

        with TestClient(app) as c:
            yield c


class TestCreateEntry:
    def test_creates_entry(self, client, mock_knowledge_service):
        mock_knowledge_service.create_entry.return_value = {
            "id": "abc-123",
            "title": "Test",
            "content": "echo hello",
            "content_type": "command",
            "language": None,
            "tags": [],
            "source_url": None,
            "notes": None,
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
        response = client.post(
            "/knowledge/entries",
            json={"title": "Test", "content": "echo hello", "content_type": "command"},
        )
        assert response.status_code == 201
        assert response.json()["id"] == "abc-123"

    def test_conflict_returns_409(self, client, mock_knowledge_service):
        mock_knowledge_service.create_entry.return_value = {
            "id": "abc-123",
            "title": "Test",
            "content": "echo hello",
            "content_type": "command",
            "tags": [],
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
            "_conflict": True,
        }
        response = client.post(
            "/knowledge/entries",
            json={"title": "Test", "content": "echo hello", "content_type": "command"},
        )
        assert response.status_code == 409

    def test_invalid_content_type_returns_422(self, client):
        response = client.post(
            "/knowledge/entries",
            json={"title": "Test", "content": "x", "content_type": "invalid"},
        )
        assert response.status_code == 422


class TestGetEntry:
    def test_found(self, client, mock_knowledge_service):
        mock_knowledge_service.get_entry.return_value = {
            "id": "abc-123",
            "title": "Test",
            "content": "x",
            "content_type": "code",
            "tags": [],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        response = client.get("/knowledge/entries/abc-123")
        assert response.status_code == 200

    def test_not_found(self, client, mock_knowledge_service):
        mock_knowledge_service.get_entry.return_value = None
        response = client.get("/knowledge/entries/nonexistent")
        assert response.status_code == 404


class TestUpdateEntry:
    def test_updates_entry(self, client, mock_knowledge_service):
        mock_knowledge_service.update_entry.return_value = {
            "id": "abc-123",
            "title": "Updated",
            "content": "x",
            "content_type": "code",
            "tags": ["new"],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
        response = client.put("/knowledge/entries/abc-123", json={"tags": ["new"]})
        assert response.status_code == 200
        assert response.json()["tags"] == ["new"]

    def test_not_found(self, client, mock_knowledge_service):
        mock_knowledge_service.update_entry.return_value = None
        response = client.put("/knowledge/entries/nonexistent", json={"tags": ["x"]})
        assert response.status_code == 404


class TestDeleteEntry:
    def test_deletes_entry(self, client, mock_knowledge_service):
        response = client.delete("/knowledge/entries/abc-123")
        assert response.status_code == 204
        mock_knowledge_service.delete_entry.assert_called_once_with("abc-123")


class TestSearch:
    def test_search(self, client, mock_knowledge_service):
        mock_knowledge_service.search.return_value = {
            "query": "docker",
            "results": [],
            "count": 0,
        }
        response = client.post("/knowledge/search", json={"query": "docker"})
        assert response.status_code == 200
        assert response.json()["count"] == 0


class TestTags:
    def test_returns_tags(self, client, mock_knowledge_service):
        mock_knowledge_service.get_tags.return_value = [{"tag": "python", "count": 10}]
        response = client.get("/knowledge/tags")
        assert response.status_code == 200
        assert len(response.json()["tags"]) == 1


class TestStats:
    def test_returns_stats(self, client, mock_knowledge_service):
        mock_knowledge_service.get_stats.return_value = {
            "total_entries": 100,
            "by_content_type": {"code": 50},
            "recent_entries": 5,
            "tag_count": 10,
        }
        response = client.get("/knowledge/stats")
        assert response.status_code == 200
        assert response.json()["total_entries"] == 100
