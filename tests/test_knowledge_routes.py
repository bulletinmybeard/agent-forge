"""Tests for /knowledge API routes.

Uses FastAPI TestClient with mocked services.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.knowledge import router as knowledge_router


@pytest.fixture()
def mock_knowledge_service():
    with patch("app.routes.knowledge.knowledge_service") as mock:
        yield mock


@pytest.fixture()
def client(mock_knowledge_service):
    # Mount only the knowledge router on a bare app. Booting app.main instead
    # would load the framework config (agentforge.config), which needs a
    # config.yaml that CI doesn't have. Routes run in isolation against the
    # mocked service above.
    app = FastAPI()
    app.include_router(knowledge_router)
    with TestClient(app) as c:
        yield c


class TestCreateEntry:
    def test_creates_entry(self, client, mock_knowledge_service):
        mock_knowledge_service.create_entry.return_value = {
            "id": "abc-123",
            "title": "Test",
            "content": "echo hello",
            "content_type": "cheatsheet",
            "language": None,
            "tags": [],
            "source_url": None,
            "notes": None,
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
        response = client.post(
            "/knowledge/entries",
            json={"title": "Test", "content": "echo hello", "content_type": "cheatsheet"},
        )
        assert response.status_code == 201
        assert response.json()["id"] == "abc-123"

    def test_conflict_returns_409(self, client, mock_knowledge_service):
        mock_knowledge_service.create_entry.return_value = {
            "id": "abc-123",
            "title": "Test",
            "content": "echo hello",
            "content_type": "cheatsheet",
            "tags": [],
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
            "_conflict": True,
        }
        response = client.post(
            "/knowledge/entries",
            json={"title": "Test", "content": "echo hello", "content_type": "cheatsheet"},
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
            "content_type": "snippet",
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
            "content_type": "snippet",
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
            "by_content_type": {"snippet": 50},
            "recent_entries": 5,
            "tag_count": 10,
        }
        response = client.get("/knowledge/stats")
        assert response.status_code == 200
        assert response.json()["total_entries"] == 100


class TestListEntries:
    def test_returns_slim_overview(self, client, mock_knowledge_service):
        mock_knowledge_service.list_overview.return_value = {
            "results": [
                {
                    "id": "abc-123",
                    "title": "Docker prune",
                    "content_type": "cheatsheet",
                    "language": "bash",
                    "tags": ["docker"],
                    "parent_id": None,
                    "created_at": "2026-06-20T00:00:00Z",
                    "metadata": {"filename": "cleanup.md"},
                }
            ],
            "count": 1,
        }
        response = client.get("/knowledge/list?limit=100")
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["results"][0]["title"] == "Docker prune"
        assert "content" not in body["results"][0]
        mock_knowledge_service.list_overview.assert_called_once_with(limit=100)


class TestFilterEntries:
    def test_filters_by_parent_id(self, client, mock_knowledge_service):
        mock_knowledge_service.filter_entries.return_value = {
            "results": [
                {
                    "id": "child-1",
                    "title": "Page 1",
                    "content": "text",
                    "content_type": "document",
                    "tags": [],
                    "created_at": "2026-06-20T00:00:00Z",
                    "updated_at": "2026-06-20T00:00:00Z",
                    "parent_id": "parent-1",
                }
            ],
            "count": 1,
        }
        response = client.post(
            "/knowledge/filter",
            json={"parent_id": "parent-1", "limit": 10},
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1
        mock_knowledge_service.filter_entries.assert_called_once_with(
            limit=10,
            content_type=None,
            tags=None,
            project=None,
            parent_id="parent-1",
        )


class TestCreateWithMetadata:
    def test_passes_metadata_and_parent_id(self, client, mock_knowledge_service):
        mock_knowledge_service.create_entry.return_value = {
            "id": "abc-123",
            "title": "Child page",
            "content": "body",
            "content_type": "document",
            "language": None,
            "tags": [],
            "source_url": None,
            "notes": None,
            "metadata": {"filename": "doc.pdf"},
            "parent_id": "parent-uuid",
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
        response = client.post(
            "/knowledge/entries",
            json={
                "title": "Child page",
                "content": "body",
                "content_type": "document",
                "metadata": {"filename": "doc.pdf"},
                "parent_id": "parent-uuid",
            },
        )
        assert response.status_code == 201
        call_args = mock_knowledge_service.create_entry.call_args[0][0]
        assert call_args.metadata == {"filename": "doc.pdf"}
        assert call_args.parent_id == "parent-uuid"


class TestPdfExtractHelpers:
    def test_format_pdftotext_pages_splits_form_feeds(self):
        from app.routes.knowledge import _format_pdftotext_pages

        raw = "Page one\fPage two\fPage three"
        text, pages = _format_pdftotext_pages(raw)
        assert pages == 3
        assert "--- Page 1 ---" in text
        assert "--- Page 3 ---" in text
        assert "Page two" in text

    def test_pdftotext_timeout_scales_with_size(self):
        from app.routes.knowledge import _pdftotext_timeout

        assert _pdftotext_timeout(1 * 1024 * 1024) == 120
        assert _pdftotext_timeout(62 * 1024 * 1024) == 124
        assert _pdftotext_timeout(500 * 1024 * 1024) == 600
