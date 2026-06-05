import asyncio

from web.server.secret import SecretBroker


def test_resolve_returns_value():
    async def run():
        broker = SecretBroker()
        sent = []
        broker.set_sender(lambda m: sent.append(m))
        task = asyncio.ensure_future(broker.request("localhost", "pw?"))
        await asyncio.sleep(0)
        req_id = sent[0]["request_id"]
        assert sent[0]["type"] == "secret.request"
        broker.resolve(req_id, value="hunter2")
        assert await task == "hunter2"

    asyncio.run(run())


def test_cancel_returns_none():
    async def run():
        broker = SecretBroker()
        sent = []
        broker.set_sender(lambda m: sent.append(m))
        task = asyncio.ensure_future(broker.request("localhost", "pw?"))
        await asyncio.sleep(0)
        broker.resolve(sent[0]["request_id"], cancelled=True)
        assert await task is None

    asyncio.run(run())


def test_no_auto_accept_attribute():
    assert not hasattr(SecretBroker(), "auto_accept")


def test_resolve_reports_whether_pending_existed():
    # A worker-originated response has no in-process waiter → resolve returns
    # False, which the WS handler uses to decide whether to stash for polling.
    async def run():
        broker = SecretBroker()
        sent = []
        broker.set_sender(lambda m: sent.append(m))
        assert broker.resolve("unknown-id", value="x") is False  # no waiter
        task = asyncio.ensure_future(broker.request("localhost", "pw?"))
        await asyncio.sleep(0)
        req_id = sent[0]["request_id"]
        assert broker.resolve(req_id, value="ok") is True  # in-process waiter
        assert await task == "ok"

    asyncio.run(run())
