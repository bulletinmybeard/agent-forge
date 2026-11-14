"""Worker live-UI broadcasts have to survive AsyncClient's awaited hooks."""

import asyncio
import json

import httpx

from web.server.identity import reset_current_user, set_current_user
from web.server.queue import jobs_common


def test_async_broadcast_posts_tool_call_with_user_header(monkeypatch):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(jobs_common.httpx, "AsyncClient", factory)
    saved = dict(jobs_common._async_clients)
    jobs_common._async_clients.clear()

    async def run() -> None:
        token = set_current_user("user-1")
        client = None
        try:
            client = jobs_common._get_async_http()
            sock = jobs_common.HttpCallbackSocket("sess-1", "job-1")
            await sock.send_json({"type": "tool.call", "name": "mail_search", "args": {"q": "milk"}})
            await sock.send_json({"type": "agent.result", "text": "done"})
        finally:
            reset_current_user(token)
            if client is not None:
                await client.aclose()

    try:
        asyncio.run(run())
    finally:
        jobs_common._async_clients.clear()
        jobs_common._async_clients.update(saved)

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/internal/sessions/sess-1/broadcast"
    assert request.headers["x-agentforge-user"] == "user-1"
    body = json.loads(request.content)
    assert body["type"] == "tool.call"
    assert body["name"] == "mail_search"
