"""Tests for KnowledgeService pipeline logic.

Mocks the vector service and embedding service to test orchestration
without Qdrant or Ollama.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.models.knowledge import CreateEntryRequest, KnowledgeSearchRequest, UpdateEntryRequest
from app.services.knowledge_service import KnowledgeService


@pytest.fixture()
def mock_vector_svc():
    svc = MagicMock()
    svc.get_content_hashes.return_value = {}
    svc.get_by_id.return_value = None
    svc.generate_point_id.side_effect = lambda h: f"pt-{h[:8]}"
    return svc


@pytest.fixture()
def mock_embedding_svc():
    svc = MagicMock()
    svc.embed.return_value = [0.1, 0.2, 0.3]
    svc.embed_batch.return_value = [[0.1, 0.2], [0.3, 0.4]]
    return svc


@pytest.fixture()
def mock_dedup_svc():
    from app.services.dedup_service import DedupResult

    svc = MagicMock()
    svc.enabled = True
    svc.filter_duplicates.return_value = DedupResult(kept=[0], duplicates=[])
    return svc


@pytest.fixture()
def service(mock_vector_svc, mock_embedding_svc, mock_dedup_svc):
    return KnowledgeService(
        vector_service=mock_vector_svc,
        embedding_service=mock_embedding_svc,
        dedup_service=mock_dedup_svc,
    )


class TestCreateEntry:
    def test_creates_and_returns_entry(self, service, mock_vector_svc, mock_embedding_svc):
        req = CreateEntryRequest(title="Test", content="echo hello", content_type="command")
        result = service.create_entry(req)
        assert result["title"] == "Test"
        assert result["content"] == "echo hello"
        assert result["content_type"] == "command"
        assert "id" in result
        assert "created_at" in result
        mock_embedding_svc.embed.assert_called_once()
        mock_vector_svc.upsert_batch.assert_called_once()

    def test_builds_composite_text(self, service, mock_embedding_svc):
        req = CreateEntryRequest(
            title="My Title",
            content="my content",
            content_type="code",
            notes="my notes",
        )
        service.create_entry(req)
        embed_call_text = mock_embedding_svc.embed.call_args[0][0]
        assert "My Title" in embed_call_text
        assert "my notes" in embed_call_text
        assert "my content" in embed_call_text

    def test_duplicate_returns_conflict(self, service, mock_vector_svc):
        import hashlib

        content = "echo hello"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        point_id = f"pt-{content_hash[:8]}"  # matches mock generate_point_id

        mock_vector_svc.get_content_hashes.return_value = {point_id: content_hash}
        mock_vector_svc.get_by_id.return_value = {
            "id": point_id,
            "payload": {
                "title": "Existing",
                "content": content,
                "content_type": "command",
                "tags": [],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        }
        req = CreateEntryRequest(title="Test", content=content, content_type="command")
        result = service.create_entry(req)
        assert result.get("_conflict") is True

    def test_tags_preserved(self, service):
        req = CreateEntryRequest(title="T", content="c", content_type="code", tags=["python", "utils"])
        result = service.create_entry(req)
        assert result["tags"] == ["python", "utils"]


class TestUpdateEntry:
    def test_metadata_only_update(self, service, mock_vector_svc, mock_embedding_svc):
        mock_vector_svc.get_by_id.return_value = {
            "id": "pt-123",
            "payload": {
                "title": "Old Title",
                "content": "echo hello",
                "content_type": "command",
                "tags": ["old"],
                "notes": None,
                "language": None,
                "source_url": None,
                "content_hash": "abc",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "indexed_at": "2026-01-01T00:00:00Z",
                "text": "Old Title\n\necho hello",
            },
        }
        req = UpdateEntryRequest(tags=["new-tag"])
        result = service.update_entry("pt-123", req)
        assert result is not None
        assert result["tags"] == ["new-tag"]
        mock_embedding_svc.embed.assert_not_called()
        mock_vector_svc.set_payload.assert_called_once()

    def test_content_change_triggers_reembed(self, service, mock_vector_svc, mock_embedding_svc):
        mock_vector_svc.get_by_id.return_value = {
            "id": "pt-123",
            "payload": {
                "title": "Title",
                "content": "old content",
                "content_type": "code",
                "tags": [],
                "notes": None,
                "language": None,
                "source_url": None,
                "content_hash": "old-hash",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "indexed_at": "2026-01-01T00:00:00Z",
                "text": "Title\n\nold content",
            },
        }
        req = UpdateEntryRequest(content="new content")
        service.update_entry("pt-123", req)
        mock_embedding_svc.embed.assert_called_once()
        mock_vector_svc.upsert_batch.assert_called_once()

    def test_not_found_returns_none(self, service, mock_vector_svc):
        mock_vector_svc.get_by_id.return_value = None
        result = service.update_entry("nonexistent", UpdateEntryRequest(tags=["x"]))
        assert result is None


class TestSearch:
    def test_embeds_query_and_searches(self, service, mock_vector_svc, mock_embedding_svc):
        mock_vector_svc.search.return_value = [
            {
                "id": "abc",
                "score": 0.92,
                "payload": {
                    "title": "Docker prune",
                    "content": "docker volume prune",
                    "content_type": "command",
                    "tags": ["docker"],
                    "created_at": "2026-01-01T00:00:00Z",
                },
            }
        ]
        req = KnowledgeSearchRequest(query="docker cleanup")
        result = service.search(req)
        assert result["count"] == 1
        assert result["results"][0]["title"] == "Docker prune"
        mock_embedding_svc.embed.assert_called_once_with("docker cleanup")


class TestProcessBatch:
    def test_indexes_multiple_entries(self, service, mock_vector_svc, mock_embedding_svc, mock_dedup_svc):
        from app.services.dedup_service import DedupResult

        mock_dedup_svc.filter_duplicates.return_value = DedupResult(kept=[0, 1], duplicates=[])
        mock_embedding_svc.embed_batch.return_value = [[0.1], [0.2]]

        entries = [
            CreateEntryRequest(title="A", content="a", content_type="code"),
            CreateEntryRequest(title="B", content="b", content_type="command"),
        ]
        result = service.process_batch(entries)
        assert result["indexed"] == 2
        assert result["errors"] == 0
        mock_vector_svc.upsert_batch.assert_called_once()


class TestGetStats:
    def test_returns_stats(self, service, mock_vector_svc):
        mock_vector_svc.get_collection_info.return_value = {"points_count": 100}
        mock_vector_svc.count_by_content_type.return_value = {"code": 50, "command": 30}
        mock_vector_svc.count_recent.return_value = 5
        mock_vector_svc.facet_tags.return_value = [
            {"tag": "python", "count": 10},
            {"tag": "docker", "count": 5},
        ]
        result = service.get_stats()
        assert result["total_entries"] == 100
        assert result["by_content_type"]["code"] == 50
        assert result["recent_entries"] == 5
        assert result["tag_count"] == 2
