"""Tests for knowledge attachment file storage."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient

from app.routes.knowledge import router as knowledge_router
from app.services.knowledge_file_service import KnowledgeFileService


@pytest.fixture()
def files_tmp(tmp_path):
    svc = KnowledgeFileService(tmp_path)
    with patch("app.routes.knowledge.knowledge_file_service", svc):
        yield svc


@pytest.fixture()
def client(files_tmp, mock_knowledge_service):
    app = FastAPI()
    app.include_router(knowledge_router)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def mock_knowledge_service():
    with patch("app.routes.knowledge.knowledge_service") as mock:
        yield mock


class TestKnowledgeFileRoutes:
    ENTRY_ID = "9aa7994b-fb05-df98-5ae3-2eec32fb6bad"

    def test_head_missing_returns_404(self, client):
        res = client.head(f"/knowledge/entries/{self.ENTRY_ID}/file")
        assert res.status_code == 404

    def test_upload_and_download(self, client, mock_knowledge_service, files_tmp):
        mock_knowledge_service.get_entry.return_value = {
            "id": self.ENTRY_ID,
            "title": "Paper",
            "content": "text",
            "content_type": "document",
            "metadata": {"filename": "paper.pdf"},
        }
        mock_knowledge_service.update_entry.return_value = {"id": self.ENTRY_ID}

        upload = client.post(
            f"/knowledge/entries/{self.ENTRY_ID}/file",
            files={"file": ("paper.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert upload.status_code == 201
        assert files_tmp.exists(self.ENTRY_ID)

        head = client.head(f"/knowledge/entries/{self.ENTRY_ID}/file")
        assert head.status_code == 200

        download = client.get(f"/knowledge/entries/{self.ENTRY_ID}/file")
        assert download.status_code == 200
        assert download.content == b"%PDF-1.4 fake"
        assert "paper.pdf" in download.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_save_rejects_empty(self, files_tmp):
        from fastapi import HTTPException

        upload = UploadFile(filename="empty.pdf", file=BytesIO(b""))
        with pytest.raises(HTTPException) as exc:
            await files_tmp.save(self.ENTRY_ID, upload)
        assert exc.value.status_code == 400
