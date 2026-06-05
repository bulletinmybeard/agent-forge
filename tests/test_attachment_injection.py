"""Regression test: worker-mode runners must inject uploaded attachments.

Bug: @search (web_search) and the other worker runners dropped attachments —
_run_web_search & co. never called the attachment-injection logic that
_run_agent has, so an attached document never reached the model.
"""

import types

from agentforge.client import AIClient
from web.server.ws_endpoint import _attachment_from_payload, _attachment_text_block, _inject_attachments


class _FakeClient:
    """Minimal stand-in exposing _apply_attachments with a chosen provider."""

    def __init__(self, provider: str = "ollama") -> None:
        self._profile = types.SimpleNamespace(provider=provider)

    _apply_attachments = AIClient._apply_attachments


def test_inject_attachments_appends_md_text(tmp_path):
    doc = tmp_path / "filme.md"
    doc.write_text("Terminator (1984)\nAliens (1986)\n", encoding="utf-8")
    overrides = {"_attachments": [{"path": str(doc), "name": "filme.md", "is_image": False}]}

    msgs = _inject_attachments(_FakeClient(), [{"role": "user", "content": "describe these"}], overrides)

    assert "Terminator (1984)" in msgs[-1]["content"]
    assert "filme.md" in msgs[-1]["content"]
    # popped so it can't leak downstream as an unknown override key
    assert "_attachments" not in overrides


def test_inject_attachments_noop_without_attachments():
    msgs_in = [{"role": "user", "content": "hi"}]
    assert _inject_attachments(_FakeClient(), msgs_in, {}) is msgs_in
    assert _inject_attachments(_FakeClient(), msgs_in, None) is msgs_in


def test_inject_attachments_skips_entries_without_path():
    overrides = {"_attachments": [{"name": "x.md", "is_image": False}]}  # no usable path
    msgs_in = [{"role": "user", "content": "hi"}]
    assert _inject_attachments(_FakeClient(), msgs_in, overrides) is msgs_in


def test_attachment_text_block_concatenates_docs_and_skips_images(tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("Alpha doc body", encoding="utf-8")
    overrides = {
        "_attachments": [
            {"path": str(doc), "name": "a.md", "is_image": False},
            {"path": "/nope/img.png", "name": "img.png", "is_image": True},  # skipped
        ]
    }
    block = _attachment_text_block(overrides)
    assert "Alpha doc body" in block
    assert "a.md" in block
    assert "img.png" not in block
    assert "_attachments" not in overrides  # popped


def test_attachment_text_block_empty():
    assert _attachment_text_block({}) == ""
    assert _attachment_text_block(None) == ""


def test_extracted_sidecar_used_for_binary_docs(tmp_path):
    # A PDF (non-UTF-8) would be dropped on the worker path; the pre-extracted
    # sidecar must be read instead so the document text still reaches the model.
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\x00\x01 binary junk \xff\xfe")
    sidecar = tmp_path / "doc.extracted.md"
    sidecar.write_text("Extracted PDF body text", encoding="utf-8")
    payload = {"path": str(pdf), "name": "doc.pdf", "is_image": False, "extracted_path": str(sidecar)}

    att = _attachment_from_payload(payload)
    assert att.as_context_text() == "Extracted PDF body text"

    block = _attachment_text_block({"_attachments": [payload]})
    assert "Extracted PDF body text" in block
