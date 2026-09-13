# tests/test_loki_logs.py
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from web.server.loki_logs import query_session_logs

SID = "019d14f2-aaaa-bbbb-cccc-ddddeeeeffff"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _payload(values, labels=None):
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": labels or {"host": "worker", "job": "docker"}, "values": values}],
        },
    }


@pytest.mark.asyncio
async def test_rejects_non_uuid():
    with pytest.raises(ValueError):
        await query_session_logs("nope", start_ns=1, end_ns=2, loki_url="http://loki:3100")


@pytest.mark.asyncio
async def test_merges_and_dedupes():
    SID = "019d14f2-aaaa-bbbb-cccc-ddddeeeeffff"
    primary = _payload([["100", "a session_id=" + SID], ["200", "b"]])
    fallback = _payload([["100", "a session_id=" + SID], ["150", "mid"]])
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[_Resp(primary), _Resp(fallback)])
    lines, truncated = await query_session_logs(SID, start_ns=1, end_ns=9, loki_url="http://loki:3100", client=client)
    assert truncated is False
    assert [row["line"] for row in lines] == [
        "a session_id=" + SID,
        "mid",
        "b",
    ]
    assert client.get.call_count == 2


@pytest.mark.asyncio
async def test_caps_and_sets_truncated():
    values = [[str(i), f"line-{i} {SID}"] for i in range(5)]
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[_Resp(_payload(values)), _Resp(_payload([]))])
    lines, truncated = await query_session_logs(
        SID, start_ns=1, end_ns=9, loki_url="http://loki:3100", limit=3, client=client
    )
    assert truncated is True
    assert len(lines) == 3
