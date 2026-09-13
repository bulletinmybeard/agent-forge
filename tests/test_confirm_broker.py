"""Confirm broker: timeout is distinct from an explicit deny."""

from __future__ import annotations

import asyncio

from web.server.confirm import (
    ConfirmationBroker,
    ConfirmDecision,
    make_sync_confirm_handler,
    unanswered_confirm_request,
)


def test_unanswered_confirm_request_skips_answered():
    messages = [
        type("M", (), {"type": "confirm_prompt", "metadata": {"type": "confirm.request", "request_id": "cr_old", "prompt": "old"}, "content": "old"})(),
        type("M", (), {"type": "confirm_answer", "metadata": {"type": "confirm_answer", "request_id": "cr_old", "confirmed": True}, "content": ""})(),
        type("M", (), {"type": "confirm_prompt", "metadata": {"type": "confirm.request", "request_id": "cr_new", "prompt": "Apply edit to models.py?"}, "content": "Apply edit to models.py?"})(),
    ]
    got = unanswered_confirm_request(messages)
    assert got == {
        "type": "confirm.request",
        "request_id": "cr_new",
        "prompt": "Apply edit to models.py?",
    }


def test_unanswered_confirm_request_none_when_all_answered():
    messages = [
        type("M", (), {"type": "confirm_prompt", "metadata": {"request_id": "cr_1", "prompt": "x", "type": "confirm.request"}, "content": "x"})(),
        type("M", (), {"type": "confirm_answer", "metadata": {"request_id": "cr_1", "type": "confirm_answer"}, "content": ""})(),
    ]
    assert unanswered_confirm_request(messages) is None


def test_confirm_yes():
    async def run():
        broker = ConfirmationBroker()
        sent: list[dict] = []
        broker.set_sender(lambda m: sent.append(m))
        task = asyncio.ensure_future(broker.request("Write new file foo.md?"))
        await asyncio.sleep(0)
        assert sent[0]["type"] == "confirm.request"
        broker.resolve(sent[0]["request_id"], True)
        decision = await task
        assert decision.confirmed is True
        assert decision.timed_out is False

    asyncio.run(run())


def test_confirm_no():
    async def run():
        broker = ConfirmationBroker()
        sent: list[dict] = []
        broker.set_sender(lambda m: sent.append(m))
        task = asyncio.ensure_future(broker.request("Write new file foo.md?"))
        await asyncio.sleep(0)
        broker.resolve(sent[0]["request_id"], False)
        decision = await task
        assert bool(decision) is False
        assert decision.timed_out is False

    asyncio.run(run())


def test_confirm_timeout_emits_event_and_marks_timed_out(monkeypatch):
    async def run():
        broker = ConfirmationBroker()
        sent: list[dict] = []
        broker.set_sender(lambda m: sent.append(m))
        monkeypatch.setattr("web.server.confirm.CONFIRM_TIMEOUT_SECONDS", 0.01)
        decision = await broker.request("Write new file foo.md?")
        assert decision.confirmed is False
        assert decision.timed_out is True
        assert any(m.get("type") == "confirm.timeout" for m in sent)

    asyncio.run(run())


def test_sync_handler_preserves_timed_out():
    async def body(loop):
        broker = ConfirmationBroker()
        sent: list[dict] = []
        broker.set_sender(lambda m: sent.append(m))
        handler = make_sync_confirm_handler(broker, loop)
        fut = loop.run_in_executor(None, handler, "Write new file foo.md?")
        await asyncio.sleep(0.05)
        broker.resolve(sent[0]["request_id"], False)
        decision = await fut
        assert isinstance(decision, ConfirmDecision)
        assert decision.confirmed is False

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(body(loop))
    finally:
        loop.close()
