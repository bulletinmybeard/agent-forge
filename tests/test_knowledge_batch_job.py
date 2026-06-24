"""Tests for the knowledge batch indexing SAQ job."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mock_knowledge_service():
    svc = MagicMock()
    svc.process_batch.return_value = {
        "indexed": 2,
        "skipped": 0,
        "deduped": 0,
        "errors": 0,
    }
    return svc


class TestIndexKnowledgeBatchSaq:
    def test_processes_entries(self, mock_knowledge_service):
        entries = [
            {
                "title": "A",
                "content": "a",
                "content_type": "snippet",
                "tags": [],
                "language": None,
                "source_url": None,
                "notes": None,
            },
            {
                "title": "B",
                "content": "b",
                "content_type": "cheatsheet",
                "tags": [],
                "language": None,
                "source_url": None,
                "notes": None,
            },
        ]
        entries_json = json.dumps(entries)

        with patch("web.server.queue.jobs_common._post_status", new_callable=MagicMock):
            with patch(
                "app.services.knowledge_service.knowledge_service",
                mock_knowledge_service,
            ):
                from web.server.queue.jobs_saq import index_knowledge_batch_saq

                ctx = {"job": MagicMock()}
                result = asyncio.run(index_knowledge_batch_saq(ctx, job_id="test-job", entries_json=entries_json))
                assert result["indexed"] == 2
                mock_knowledge_service.process_batch.assert_called_once()

    def test_handles_invalid_json(self):
        with patch("web.server.queue.jobs_common._post_status", new_callable=MagicMock):
            from web.server.queue.jobs_saq import index_knowledge_batch_saq

            ctx = {"job": MagicMock()}
            result = asyncio.run(index_knowledge_batch_saq(ctx, job_id="test-job", entries_json="not-json"))
            assert result.get("errors", 0) > 0 or "error" in result
