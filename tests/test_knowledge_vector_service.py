"""Tests for KnowledgeVectorService.

Uses a mock QdrantClient to verify Qdrant API calls without a running instance.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest
from qdrant_client.models import PointStruct

from app.services.knowledge_vector_service import KnowledgeVectorService


@pytest.fixture()
def mock_client():
    client = MagicMock()
    client.get_collections.return_value = types.SimpleNamespace(collections=[])
    return client


@pytest.fixture()
def svc(mock_client):
    service = KnowledgeVectorService()
    service._client = mock_client
    return service


class TestEnsureCollection:
    def test_creates_collection_when_missing(self, svc, mock_client):
        mock_client.get_collections.return_value = types.SimpleNamespace(collections=[])
        svc.ensure_collection()
        mock_client.create_collection.assert_called_once()
        call_kwargs = mock_client.create_collection.call_args
        assert call_kwargs.kwargs["collection_name"] == "knowledge_entries"

    def test_skips_when_exists(self, svc, mock_client):
        existing = types.SimpleNamespace(name="knowledge_entries")
        mock_client.get_collections.return_value = types.SimpleNamespace(collections=[existing])
        svc.ensure_collection()
        mock_client.create_collection.assert_not_called()

    def test_creates_payload_indexes(self, svc, mock_client):
        svc.ensure_collection()
        index_calls = mock_client.create_payload_index.call_args_list
        indexed_fields = {call.kwargs["field_name"] for call in index_calls}
        assert "content_type" in indexed_fields
        assert "language" in indexed_fields
        assert "tags" in indexed_fields
        assert "created_at" in indexed_fields
        assert "updated_at" in indexed_fields
        assert "source_url" in indexed_fields


class TestUpsertBatch:
    def test_upserts_points(self, svc, mock_client):
        points = [PointStruct(id="abc", vector=[0.1, 0.2], payload={"text": "hello"})]
        svc.upsert_batch(points)
        mock_client.upsert.assert_called_once()
        call_kwargs = mock_client.upsert.call_args.kwargs
        assert call_kwargs["collection_name"] == "knowledge_entries"
        assert call_kwargs["points"] == points

    def test_empty_batch_skips(self, svc, mock_client):
        svc.upsert_batch([])
        mock_client.upsert.assert_not_called()


class TestGetById:
    def test_returns_entry_when_found(self, svc, mock_client):
        point = types.SimpleNamespace(
            id="abc-123",
            payload={"title": "Test", "content": "hello", "content_type": "snippet"},
        )
        mock_client.retrieve.return_value = [point]
        result = svc.get_by_id("abc-123")
        assert result is not None
        assert result["id"] == "abc-123"
        assert result["payload"]["title"] == "Test"

    def test_returns_none_when_not_found(self, svc, mock_client):
        mock_client.retrieve.return_value = []
        result = svc.get_by_id("nonexistent")
        assert result is None


class TestSearch:
    def test_basic_search(self, svc, mock_client):
        hit = types.SimpleNamespace(id="abc", score=0.95, payload={"title": "Test", "content": "hello"})
        mock_client.query_points.return_value = types.SimpleNamespace(points=[hit])
        results = svc.search(query_vector=[0.1, 0.2], limit=10)
        assert len(results) == 1
        assert results[0]["score"] == 0.95

    def test_search_with_tag_filter(self, svc, mock_client):
        mock_client.query_points.return_value = types.SimpleNamespace(points=[])
        svc.search(query_vector=[0.1], limit=5, tags=["python", "docker"])
        call_kwargs = mock_client.query_points.call_args.kwargs
        assert call_kwargs["query_filter"] is not None


class TestSetPayload:
    def test_calls_set_payload(self, svc, mock_client):
        svc.set_payload("abc-123", {"tags": ["new"]})
        mock_client.set_payload.assert_called_once()
        call_kwargs = mock_client.set_payload.call_args.kwargs
        assert call_kwargs["collection_name"] == "knowledge_entries"
        assert call_kwargs["payload"] == {"tags": ["new"]}


class TestDeletePoint:
    def test_deletes_by_id(self, svc, mock_client):
        svc.delete_point("abc-123")
        mock_client.delete.assert_called_once()


class TestFacetTags:
    def test_returns_tag_counts(self, svc, mock_client):
        hit1 = types.SimpleNamespace(value="python", count=10)
        hit2 = types.SimpleNamespace(value="docker", count=5)
        mock_client.facet.return_value = types.SimpleNamespace(hits=[hit1, hit2])
        result = svc.facet_tags()
        assert len(result) == 2
        assert result[0] == {"tag": "python", "count": 10}

    def test_skips_empty_values(self, svc, mock_client):
        hit = types.SimpleNamespace(value="", count=3)
        mock_client.facet.return_value = types.SimpleNamespace(hits=[hit])
        result = svc.facet_tags()
        assert len(result) == 0
