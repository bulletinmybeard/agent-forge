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
            patch.object(reg.settings.knowledge, "mail_collection_name", "kb_mail_entries"),
        ):
            assert reg.resolve_collection("kb_note_entries") == "kb_note_entries"

    def test_mail_header(self):
        with (
            patch.object(reg.settings.knowledge, "collection_name", "knowledge_entries"),
            patch.object(reg.settings.knowledge, "notes_collection_name", "kb_note_entries"),
            patch.object(reg.settings.knowledge, "mail_collection_name", "kb_mail_entries"),
        ):
            assert reg.resolve_collection("kb_mail_entries") == "kb_mail_entries"

    def test_rejects_unknown_header(self):
        with pytest.raises(HTTPException) as exc:
            reg.resolve_collection("mystery_collection")
        assert exc.value.status_code == 400

    def test_get_request_knowledge_collection(self):
        reg.set_request_knowledge_collection(None)
        assert reg.get_request_knowledge_collection() is None
        reg.set_request_knowledge_collection("snippet_entries")
        assert reg.get_request_knowledge_collection() == "snippet_entries"
        reg.set_request_knowledge_collection(None)

    def test_contextvar_used_when_set(self):
        with (
            patch.object(reg.settings.knowledge, "collection_name", "knowledge_entries"),
            patch.object(reg.settings.knowledge, "notes_collection_name", "kb_note_entries"),
            patch.object(reg.settings.knowledge, "mail_collection_name", "kb_mail_entries"),
        ):
            reg.set_request_knowledge_collection("kb_note_entries")
            assert reg.resolve_collection() == "kb_note_entries"

    def test_session_source_notes(self):
        with patch.object(reg.settings.knowledge, "notes_collection_name", "kb_note_entries"):
            assert reg.collection_for_session_source("notes") == "kb_note_entries"
            assert reg.collection_for_session_source("web") is None

    def test_session_source_mail(self):
        with patch.object(reg.settings.knowledge, "mail_collection_name", "kb_mail_entries"):
            assert reg.collection_for_session_source("mail") == "kb_mail_entries"
            assert reg.collection_for_session_source("other") is None

    def test_allowed_includes_mail(self):
        with (
            patch.object(reg.settings.knowledge, "collection_name", "knowledge_entries"),
            patch.object(reg.settings.knowledge, "notes_collection_name", "kb_note_entries"),
            patch.object(reg.settings.knowledge, "mail_collection_name", "kb_mail_entries"),
            patch.object(reg.settings.knowledge, "snippet_collection_name", "snippet_entries"),
        ):
            allowed = reg.allowed_collections()
            assert "knowledge_entries" in allowed
            assert "kb_note_entries" in allowed
            assert "kb_mail_entries" in allowed
            assert "snippet_entries" in allowed

    def test_session_source_snippets(self):
        with patch.object(reg.settings.knowledge, "snippet_collection_name", "snippet_entries"):
            assert reg.collection_for_session_source("snippets") == "snippet_entries"
            assert reg.collection_for_session_source("other") is None


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
            vector_cls.side_effect = lambda **kw: MagicMock()
            service_cls.side_effect = lambda **kw: MagicMock()
            a = reg.get_knowledge_service("knowledge_entries")
            b = reg.get_knowledge_service("knowledge_entries")
            c = reg.get_knowledge_service("kb_note_entries")
            assert a is b
            assert a is not c
            assert service_cls.call_count == 2
