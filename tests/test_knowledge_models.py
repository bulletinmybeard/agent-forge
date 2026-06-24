"""Tests for knowledge database Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.knowledge import (
    VALID_CONTENT_TYPES,
    BatchCreateRequest,
    BulkDeleteRequest,
    CreateEntryRequest,
    KnowledgeSearchRequest,
    UpdateEntryRequest,
)


class TestCreateEntryRequest:
    def test_valid_minimal(self):
        req = CreateEntryRequest(title="Test", content="echo hello", content_type="cheatsheet")
        assert req.title == "Test"
        assert req.content == "echo hello"
        assert req.content_type == "cheatsheet"
        assert req.tags == []
        assert req.language is None
        assert req.source_url is None
        assert req.notes is None

    def test_valid_full(self):
        req = CreateEntryRequest(
            title="Docker prune",
            content="docker volume prune -f",
            content_type="cheatsheet",
            language="bash",
            tags=["Docker", "  PROJ-123  "],
            source_url="https://docs.docker.com",
            notes="Safe to run",
        )
        assert req.tags == ["docker", "proj-123"]
        assert req.language == "bash"

    def test_tags_normalized(self):
        req = CreateEntryRequest(
            title="Test",
            content="x",
            content_type="snippet",
            tags=["  FOO  ", "Bar", "baz"],
        )
        assert req.tags == ["foo", "bar", "baz"]

    def test_title_too_long(self):
        with pytest.raises(ValidationError):
            CreateEntryRequest(title="x" * 201, content="y", content_type="snippet")

    def test_invalid_content_type(self):
        with pytest.raises(ValidationError):
            CreateEntryRequest(title="Test", content="y", content_type="invalid_type")

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            CreateEntryRequest(title="Test")


class TestUpdateEntryRequest:
    def test_all_optional(self):
        req = UpdateEntryRequest()
        assert req.title is None
        assert req.content is None
        assert req.tags is None

    def test_partial_update(self):
        req = UpdateEntryRequest(tags=["NEW-TAG"])
        assert req.tags == ["new-tag"]
        assert req.title is None

    def test_tags_normalized_on_update(self):
        req = UpdateEntryRequest(tags=["  MiXeD  "])
        assert req.tags == ["mixed"]


class TestKnowledgeSearchRequest:
    def test_defaults(self):
        req = KnowledgeSearchRequest(query="docker cleanup")
        assert req.limit == 10
        assert req.tags is None
        assert req.content_type is None

    def test_limit_capped(self):
        req = KnowledgeSearchRequest(query="test", limit=100)
        assert req.limit == 50


class TestBatchCreateRequest:
    def test_valid(self):
        entries = [
            CreateEntryRequest(title="A", content="a", content_type="snippet"),
            CreateEntryRequest(title="B", content="b", content_type="cheatsheet"),
        ]
        req = BatchCreateRequest(entries=entries)
        assert len(req.entries) == 2

    def test_too_many_entries(self):
        entries = [CreateEntryRequest(title=f"E{i}", content=f"c{i}", content_type="snippet") for i in range(101)]
        with pytest.raises(ValidationError):
            BatchCreateRequest(entries=entries)

    def test_empty_entries(self):
        with pytest.raises(ValidationError):
            BatchCreateRequest(entries=[])


class TestBulkDeleteRequest:
    def test_at_least_one_filter_required(self):
        req = BulkDeleteRequest(tags=["old"])
        assert req.tags == ["old"]

    def test_tags_normalized(self):
        req = BulkDeleteRequest(tags=["  OLD  "])
        assert req.tags == ["old"]

    def test_no_filters_raises(self):
        with pytest.raises(ValidationError):
            BulkDeleteRequest()


class TestValidContentTypes:
    def test_expected_types(self):
        expected = {"note", "reference", "documentation", "document", "cheatsheet", "snippet"}
        assert VALID_CONTENT_TYPES == expected

    @pytest.mark.parametrize("content_type", sorted(VALID_CONTENT_TYPES))
    def test_each_type_accepted(self, content_type: str):
        req = CreateEntryRequest(title="T", content="c", content_type=content_type)
        assert req.content_type == content_type

    @pytest.mark.parametrize(
        "content_type",
        ["code", "command", "url", "config", "error_solution", "api_example"],
    )
    def test_legacy_types_rejected(self, content_type: str):
        with pytest.raises(ValidationError):
            CreateEntryRequest(title="T", content="c", content_type=content_type)

    def test_metadata_and_parent_id_on_create(self):
        req = CreateEntryRequest(
            title="Attachment",
            content="page 1",
            content_type="document",
            metadata={"filename": "doc.pdf", "pages": 3},
            parent_id="parent-uuid-123",
        )
        assert req.metadata == {"filename": "doc.pdf", "pages": 3}
        assert req.parent_id == "parent-uuid-123"

    def test_parent_id_on_update(self):
        req = UpdateEntryRequest(parent_id="new-parent-uuid")
        assert req.parent_id == "new-parent-uuid"
