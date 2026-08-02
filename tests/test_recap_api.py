"""POST /api/sessions/{id}/recap — watermark no-ops, persistence, and failure handling."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings as af_settings
from web.server import api as api_module
from web.server.app import app
from web.server.database import ChatDatabase

RECAP_TEXT = "We wired up the recap endpoint and covered it with tests."


class _StubResponse:
    def __init__(self, content: str):
        self.content = content


class _StubClient:
    """Stands in for AIClient so the endpoint is testable without a live model."""

    calls = 0
    reply = RECAP_TEXT
    raises = False
    last_prompt: str | None = None

    def __init__(self, profile=None):
        self.profile = profile

    async def achat(self, messages, stream=False):
        type(self).calls += 1
        type(self).last_prompt = messages[-1]["content"]
        if type(self).raises:
            raise RuntimeError("backend exploded")
        return _StubResponse(type(self).reply)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = ChatDatabase(tmp_path / "recap_api.db")
    db.create_tables()
    db.create_session("s1", source="web")

    _StubClient.calls = 0
    _StubClient.reply = RECAP_TEXT
    _StubClient.raises = False
    _StubClient.last_prompt = None
    monkeypatch.setattr(api_module, "AIClient", _StubClient)

    with TestClient(app) as tc:
        # After startup, point the router at the throwaway DB.
        api_module.set_database(db)
        yield tc, db


def _exchanges(db, n: int, prefix: str = "q"):
    for i in range(n):
        db.add_message("s1", role="user", msg_type="query", content=f"{prefix}{i}")
        db.add_message("s1", role="assistant", msg_type="result", content=f"{prefix}-a{i}")


def test_unknown_session_404(client):
    tc, _db = client
    assert tc.post("/api/sessions/nope/recap").status_code == 404


def test_dangling_query_alone_makes_no_llm_call(client):
    tc, db = client
    db.add_message("s1", role="user", msg_type="query", content="hi")

    body = tc.post("/api/sessions/s1/recap").json()

    assert body["created"] is False
    assert body["reason"] == "too_few"
    assert _StubClient.calls == 0


def test_single_exchange_is_enough_to_trigger(client):
    """Matches observed Grok behaviour: one short exchange gets its own recap."""
    tc, db = client
    _exchanges(db, 1)

    body = tc.post("/api/sessions/s1/recap").json()

    assert body["created"] is True
    assert body["covered"]["messages"] == 2


def test_creates_and_persists_recap(client):
    tc, db = client
    _exchanges(db, 3)

    body = tc.post("/api/sessions/s1/recap").json()

    assert body["created"] is True
    assert body["recap"] == RECAP_TEXT
    assert body["covered"] == {"from_sequence": 1, "to_sequence": 6, "messages": 6}
    assert _StubClient.calls == 1

    stored = [m for m in db.get_messages("s1") if m.type == "recap"]
    assert len(stored) == 1
    # Volatile keeps it out of future conversation history and future recaps.
    assert stored[0].is_volatile is True
    assert stored[0].content == RECAP_TEXT


def test_second_call_without_new_messages_is_a_free_no_op(client):
    tc, db = client
    _exchanges(db, 3)
    tc.post("/api/sessions/s1/recap")

    body = tc.post("/api/sessions/s1/recap").json()

    assert body["created"] is False
    assert body["reason"] == "no_new_messages"
    assert body["recap"] == RECAP_TEXT
    assert _StubClient.calls == 1  # no second generation


def test_next_recap_reads_only_new_messages(client):
    tc, db = client
    _exchanges(db, 20)
    tc.post("/api/sessions/s1/recap")
    _exchanges(db, 3, prefix="later")

    body = tc.post("/api/sessions/s1/recap").json()

    assert body["created"] is True
    assert body["covered"]["messages"] == 6
    assert body["covered"]["from_sequence"] == 42  # 40 messages + recap at 41
    assert _StubClient.calls == 2


def test_later_recaps_are_seeded_with_the_previous_one(client):
    """Cumulative output, incremental input: previous recap in, old messages out."""
    tc, db = client
    _exchanges(db, 3)
    tc.post("/api/sessions/s1/recap")
    first_prompt = _StubClient.last_prompt
    assert first_prompt is not None
    assert "RECAP SO FAR" not in first_prompt
    assert "q0" in first_prompt

    _exchanges(db, 1, prefix="later")
    tc.post("/api/sessions/s1/recap")
    second_prompt = _StubClient.last_prompt
    assert second_prompt is not None

    # Previous recap seeds the update...
    assert "RECAP SO FAR" in second_prompt
    assert RECAP_TEXT in second_prompt
    # ...the new exchange is included...
    assert "later0" in second_prompt
    # ...and the already-summarised messages are NOT re-read.
    assert "q0" not in second_prompt


def test_generation_failure_is_swallowed(client):
    tc, db = client
    _exchanges(db, 3)
    _StubClient.raises = True

    resp = tc.post("/api/sessions/s1/recap")

    assert resp.status_code == 200
    assert resp.json() == {
        "recap": None,
        "created": False,
        "sequence": None,
        "covered": None,
        "reason": "generation_failed",
    }
    assert [m for m in db.get_messages("s1") if m.type == "recap"] == []


def test_markdown_is_stripped_before_persisting(client):
    tc, db = client
    _exchanges(db, 3)
    _StubClient.reply = "We renamed `foo_bar` and shipped **the fix**."

    body = tc.post("/api/sessions/s1/recap").json()

    expected = "We renamed foo_bar and shipped the fix."
    assert body["recap"] == expected
    stored = [m for m in db.get_messages("s1") if m.type == "recap"]
    assert stored[0].content == expected


def test_reply_that_is_only_markdown_counts_as_empty(client):
    tc, db = client
    _exchanges(db, 3)
    _StubClient.reply = "``` \n ```"

    body = tc.post("/api/sessions/s1/recap").json()

    assert body["created"] is False
    assert body["reason"] == "empty_summary"


def test_empty_summary_is_not_persisted(client):
    tc, db = client
    _exchanges(db, 3)
    _StubClient.reply = "   "

    body = tc.post("/api/sessions/s1/recap").json()

    assert body["created"] is False
    assert body["reason"] == "empty_summary"
    assert [m for m in db.get_messages("s1") if m.type == "recap"] == []


def test_disabled_returns_503(client, monkeypatch):
    tc, db = client
    _exchanges(db, 3)
    monkeypatch.setattr(af_settings.recap, "enabled", False)

    assert tc.post("/api/sessions/s1/recap").status_code == 503
