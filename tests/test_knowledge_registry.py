"""Tests for knowledge collection routing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services import knowledge_registry as reg


class TestResolveCollection:
    def test_default_without_header_or_context(self):
        with patch.object(reg.settings.knowledge, "collection_name", "knowledge_entries"):
            assert reg.resolve_collection() == "knowledge_entries"

    def test_notes_header(self):
        with (
            patch.object(reg.settings.knowledge, "collection_name", "knowledge_entries"),
            patch.object(reg.settings.knowledge, "notes_collection_name", "kb_note_entries"),
        ):
            assert reg.resolve_collection("kb_note_entries") == "kb_note_entries"

    def test_rejects_unknown_header(self):
        with pytest.raises(HTTPException) as exc:
            reg.resolve_collection("mystery_collection")
        assert exc.value.status_code == 400

    def test_contextvar_used_when_set(self):
        with (
            patch.object(reg.settings.knowledge, "collection_name", "knowledge_entries"),
            patch.object(reg.settings.knowledge, "notes_collection_name", "kb_note_entries"),
        ):
            reg.set_request_knowledge_collection("kb_note_entries")
            assert reg.resolve_collection() == "kb_note_entries"

    def test_session_source_notes(self):
        with patch.object(reg.settings.knowledge, "notes_collection_name", "kb_note_entries"):
            assert reg.collection_for_session_source("notes") == "kb_note_entries"
            assert reg.collection_for_session_source("web") is None


class TestGetKnowledgeService:
    def test_caches_per_collection(self):
        reg._services.clear()
        reg._vector_services.clear()
        with (
            patch.object(reg.settings.knowledge, "collection_name", "knowledge_entries"),
            patch.object(reg.settings.knowledge, "notes_collection_name", "kb_note_entries"),
            patch("app.services.knowledge_registry.KnowledgeVectorService") as vector_cls,
            patch("app.services.knowledge_registry.KnowledgeService") as service_cls,
        ):
            vector_cls.return_value = MagicMock()
            service_cls.return_value = MagicMock()
            a = reg.get_knowledge_service("knowledge_entries")
            b = reg.get_knowledge_service("knowledge_entries")
            c = reg.get_knowledge_service("kb_note_entries")
            assert a is b
            assert a is not c
            assert service_cls.call_count == 2