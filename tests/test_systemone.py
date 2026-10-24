"""Clef /v1/systemone body checks and the decide tool. No live Ollama call."""

from __future__ import annotations

import asyncio
import base64
import json
import types

import pytest
from ollama import ResponseError

from agentforge.backends.ollama import OllamaBackend
from agentforge.backends.systemone import SystemOneError, build_systemone_body
from agentforge.client import AIClient, get_models_used, reset_models_used
from agentforge.tools.decision_tools import decide

_NOUL = {"greet": {"type": "noul", "instructions": "Is this a greeting?"}}


def test_body_from_text_state_and_json_questions():
    body = build_systemone_body("clef:cloud", "hello", json.dumps(_NOUL))
    assert body["model"] == "clef:cloud"
    assert body["state"] == "hello"
    assert body["questions"]["greet"]["type"] == "noul"
    assert "criteria" not in body["questions"]["greet"]
    assert "images" not in body


def test_json_state_object_is_parsed():
    body = build_systemone_body("clef:cloud", '{"text": "hi"}', _NOUL)
    assert body["state"] == {"text": "hi"}


def test_choice_allows_null_option_text():
    questions = {
        "pick": {
            "type": "choice",
            "instructions": "Pick one.",
            "criteria": {"a": "first", "b": None},
        }
    }
    body = build_systemone_body("clef:cloud", "state", questions)
    assert body["questions"]["pick"]["criteria"]["b"] is None


@pytest.mark.parametrize(
    "questions",
    [
        {},
        {"bad": {"type": "rank", "instructions": "nope"}},
        {"bad": {"type": "choice", "instructions": "one", "criteria": {"only": "x"}}},
        {"bad": {"type": "score", "instructions": "one", "criteria": ["low"]}},
        {"has space": {"type": "noul", "instructions": "nope"}},
    ],
)
def test_rejects_bad_questions(questions):
    with pytest.raises(SystemOneError):
        build_systemone_body("clef:cloud", "state", questions)


def test_rejects_jsonish_state_that_does_not_parse():
    with pytest.raises(SystemOneError):
        build_systemone_body("clef:cloud", "{not json", _NOUL)


def _backend() -> OllamaBackend:
    backend = object.__new__(OllamaBackend)
    backend._profile = types.SimpleNamespace(model="clef:cloud")
    return backend


def test_backend_posts_systemone():
    backend = _backend()
    seen: dict = {}

    class _Fake:
        def _request_raw(self, method, path, json=None):
            seen["method"] = method
            seen["path"] = path
            seen["json"] = json
            return types.SimpleNamespace(json=lambda: {"model": "clef:cloud", "answers": {"greet": {"yes": 0.8}}})

    setattr(backend, "_sync", _Fake())
    out = backend.systemone("hello", _NOUL)
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/systemone"
    assert seen["json"]["model"] == "clef:cloud"
    assert seen["json"]["state"] == "hello"
    assert "images" not in seen["json"]
    assert out["answers"]["greet"]["yes"] == 0.8


def test_backend_uses_client_systemone_when_present():
    backend = _backend()
    seen: dict = {}

    class _Fake:
        def systemone(self, **kwargs):
            seen.update(kwargs)
            return {"model": "clef", "answers": {}}

        def _request_raw(self, *args, **kwargs):
            raise AssertionError("raw path should not run")

    setattr(backend, "_sync", _Fake())
    out = backend.systemone({"text": "hi"}, _NOUL)
    assert seen["model"] == "clef:cloud"
    assert seen["state"] == {"text": "hi"}
    assert "images" not in seen
    assert out["model"] == "clef"


def test_asystemone_posts():
    backend = _backend()
    seen: dict = {}

    class _Fake:
        async def _request_raw(self, method, path, json=None):
            seen["method"] = method
            seen["path"] = path
            return types.SimpleNamespace(json=lambda: {"model": "clef:cloud", "answers": {}})

    setattr(backend, "_async", _Fake())
    out = asyncio.run(backend.asystemone("hello", _NOUL))
    assert seen == {"method": "POST", "path": "/v1/systemone"}
    assert out["model"] == "clef:cloud"


def test_client_systemone_unsupported():
    client = object.__new__(AIClient)
    client._backend = object()
    client._profile = types.SimpleNamespace(name="openai-default", provider="openai", model="gpt")
    with pytest.raises(NotImplementedError):
        client.systemone("state", _NOUL)


def test_client_records_model():
    client = object.__new__(AIClient)
    client._profile = types.SimpleNamespace(name="ollama-clef", provider="ollama", model="clef:cloud")

    class _Backend:
        def systemone(self, state, questions, images=None):
            return {"answers": {}}

    client._backend = _Backend()
    reset_models_used()
    client.systemone("hi", _NOUL)
    assert get_models_used() == ["clef:cloud"]


def test_decide_returns_response(monkeypatch):
    captured: dict = {}

    class _FakeClient:
        def __init__(self, profile):
            captured["profile"] = profile

        def systemone(self, state, questions, images=None):
            captured["state"] = state
            captured["questions"] = questions
            captured["images"] = images
            return {"model": "clef:cloud", "answers": {"greet": {"yes": 0.8}}}

    monkeypatch.setattr("agentforge.client.AIClient", _FakeClient)
    raw = decide("hi", json.dumps(_NOUL))
    data = json.loads(raw)
    assert data["answers"]["greet"]["yes"] == 0.8
    assert captured["profile"] == "ollama-clef"
    assert captured["images"] is None


def test_decide_loads_image(monkeypatch, tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG" + b"x" * 8)
    captured: dict = {}

    class _FakeClient:
        def __init__(self, profile):
            pass

        def systemone(self, state, questions, images=None):
            captured["images"] = images
            return {"model": "clef:cloud", "answers": {}}

    monkeypatch.setattr("agentforge.client.AIClient", _FakeClient)
    raw = decide("hi", json.dumps(_NOUL), json.dumps([str(image)]))
    assert "error" not in json.loads(raw)
    assert captured["images"] == [base64.b64encode(image.read_bytes()).decode("ascii")]


def test_decide_missing_image():
    data = json.loads(decide("hi", json.dumps(_NOUL), '["/no/such/clef-image.png"]'))
    assert "not found" in data["error"]


def test_decide_response_error(monkeypatch):
    class _FakeClient:
        def __init__(self, profile):
            pass

        def systemone(self, *args, **kwargs):
            raise ResponseError("nope", 400)

    monkeypatch.setattr("agentforge.client.AIClient", _FakeClient)
    data = json.loads(decide("hi", json.dumps(_NOUL)))
    assert data["status"] == 400
    assert "nope" in data["error"]
