"""Session-scoped confirm auto-accept (This session)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from web.server import state
from web.server.app import app
from web.server.confirm import ConfirmationBroker, ConfirmDecision
from web.server.database import ChatDatabase

SID = "019d14f2-aaaa-bbbb-cccc-ddddeeeeffff"


@pytest.fixture
def client(tmp_path):
    from web.server import api as api_module

    db = ChatDatabase(tmp_path / "confirm.db")
    db.create_tables()
    db.create_session(SID, title="confirm")
    state.session_auto_accept.clear()
    with TestClient(app) as tc:
        api_module.set_database(db)
        yield tc
    state.session_auto_accept.clear()


def test_auto_accept_default_false(client):
    body = client.get(f"/internal/sessions/{SID}/auto-accept").json()
    assert body["auto_accept"] is False


def test_auto_accept_roundtrip(client):
    r = client.post(f"/internal/sessions/{SID}/auto-accept", json={"auto_accept": True})
    assert r.status_code == 200
    assert client.get(f"/internal/sessions/{SID}/auto-accept").json()["auto_accept"] is True
    client.post(f"/internal/sessions/{SID}/auto-accept", json={"auto_accept": False})
    assert client.get(f"/internal/sessions/{SID}/auto-accept").json()["auto_accept"] is False


def test_broker_honours_session_auto_accept():
    async def run():
        state.session_auto_accept.add(SID)
        try:
            broker = ConfirmationBroker()
            broker.session_id = SID
            sent: list[dict] = []
            broker.set_sender(lambda m: sent.append(m))
            decision = await broker.request("Write new file foo.md?")
            assert isinstance(decision, ConfirmDecision)
            assert decision.confirmed is True
            assert decision.auto_accepted is True
            assert sent[0]["auto_accepted"] is True
        finally:
            state.session_auto_accept.discard(SID)

    asyncio.run(run())


def test_broker_ignore_auto_accept_still_waits():
    async def run():
        state.session_auto_accept.add(SID)
        try:
            broker = ConfirmationBroker()
            broker.session_id = SID
            sent: list[dict] = []
            broker.set_sender(lambda m: sent.append(m))
            task = asyncio.ensure_future(
                broker.request(
                    "Approve this plan and start @build?",
                    ignore_auto_accept=True,
                    kind="plan",
                )
            )
            await asyncio.sleep(0)
            assert sent[0]["type"] == "confirm.request"
            assert sent[0].get("auto_accepted") is not True
            assert sent[0]["kind"] == "plan"
            broker.resolve(sent[0]["request_id"], False)
            decision = await task
            assert decision.confirmed is False
            assert decision.auto_accepted is False
        finally:
            state.session_auto_accept.discard(SID)

    asyncio.run(run())
