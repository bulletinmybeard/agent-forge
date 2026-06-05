import pytest

pytest.importorskip("httpx")  # web service dep; skip locally if absent

import web.server.queue.jobs_common as jc  # noqa: E402


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeHttp:
    """Records POST broadcasts; returns queued GET poll payloads in order."""

    def __init__(self, poll_payloads):
        self.posted = []
        self._poll = list(poll_payloads)

    def post(self, url, json=None):
        self.posted.append((url, json))
        return _FakeResp(200, {"ok": True})

    def get(self, url):
        return _FakeResp(200, self._poll.pop(0))


def _wire(monkeypatch, http):
    monkeypatch.setattr(jc, "_get_sync_http", lambda: http)
    monkeypatch.setattr(jc.time, "sleep", lambda _s: None)  # no real delay


def test_returns_value_and_broadcasts_secret_request(monkeypatch):
    http = _FakeHttp([{"ready": False}, {"ready": True, "value": "hunter2"}])
    _wire(monkeypatch, http)
    p = jc.WorkerSecretProvider("sess-1")
    assert p.get("localhost") == "hunter2"
    url, body = http.posted[0]
    assert url == "/internal/sessions/sess-1/broadcast"
    assert body["type"] == "secret.request"
    assert body["request_id"].startswith("sr_")
    assert body["label"] == "localhost"


def test_caches_within_session(monkeypatch):
    http = _FakeHttp([{"ready": True, "value": "pw"}])
    _wire(monkeypatch, http)
    p = jc.WorkerSecretProvider("sess-2")
    assert p.get("localhost") == "pw"
    assert p.get("localhost") == "pw"  # served from cache, no second broadcast
    assert len(http.posted) == 1


def test_cancelled_returns_none(monkeypatch):
    http = _FakeHttp([{"ready": True, "cancelled": True}])
    _wire(monkeypatch, http)
    assert jc.WorkerSecretProvider("sess-3").get("localhost") is None


def test_invalidate_forces_reprompt(monkeypatch):
    http = _FakeHttp([{"ready": True, "value": "pw"}, {"ready": True, "value": "pw2"}])
    _wire(monkeypatch, http)
    p = jc.WorkerSecretProvider("sess-4")
    assert p.get("localhost") == "pw"
    p.invalidate("localhost")
    assert p.get("localhost") == "pw2"
    assert len(http.posted) == 2  # re-broadcast after invalidate
