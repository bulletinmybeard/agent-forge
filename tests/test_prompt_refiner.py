"""Tests for opening-prompt refinement (web/server/prompt_refiner.py + prompt-lab wiring)."""

from __future__ import annotations

import asyncio
import types

from web.server import prompt_refiner


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.prompt_tokens = 1
        self.completion_tokens = 1


class _FakeClient:
    """Stand-in for AIClient: echoes a fixed content or raises."""

    def __init__(self, *, content: str = "REFINED", raise_exc: Exception | None = None, **_: object) -> None:
        self._content = content
        self._raise = raise_exc
        self.profile = types.SimpleNamespace(provider="ollama", model="m")

    async def achat(self, messages, stream: bool = False):  # noqa: ANN001
        if self._raise:
            raise self._raise
        return _Resp(self._content)


def _enable(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(prompt_refiner, "is_prompt_refinement_enabled", lambda: on)


def _use_client(monkeypatch, **kwargs) -> None:
    monkeypatch.setattr(prompt_refiner, "AIClient", lambda **_: _FakeClient(**kwargs))


def test_disabled_returns_original(monkeypatch):
    _enable(monkeypatch, False)
    constructed = []
    monkeypatch.setattr(prompt_refiner, "AIClient", lambda **_: constructed.append(1))
    res = asyncio.run(prompt_refiner.refine_prompt("hello"))
    assert res.refined == "hello"
    assert res.changed is False
    assert not constructed  # never touches the LLM when disabled


def test_blank_input_returns_original(monkeypatch):
    _enable(monkeypatch, True)
    _use_client(monkeypatch, content="REFINED")
    res = asyncio.run(prompt_refiner.refine_prompt("   "))
    assert res.changed is False


def test_success_refines(monkeypatch):
    _enable(monkeypatch, True)
    _use_client(monkeypatch, content="REFINED")
    res = asyncio.run(prompt_refiner.refine_prompt("pls fix this"))
    assert res.original == "pls fix this"
    assert res.refined == "REFINED"
    assert res.changed is True


def test_unchanged_when_identical(monkeypatch):
    _enable(monkeypatch, True)
    _use_client(monkeypatch, content="hello")
    res = asyncio.run(prompt_refiner.refine_prompt("hello"))
    assert res.changed is False


def test_empty_output_falls_back(monkeypatch):
    _enable(monkeypatch, True)
    _use_client(monkeypatch, content="   ")
    res = asyncio.run(prompt_refiner.refine_prompt("hello"))
    assert res.refined == "hello"
    assert res.changed is False


def test_backend_error_falls_back(monkeypatch):
    _enable(monkeypatch, True)
    _use_client(monkeypatch, raise_exc=RuntimeError("boom"))
    res = asyncio.run(prompt_refiner.refine_prompt("hello"))
    assert res.refined == "hello"
    assert res.changed is False


def test_prompt_lab_feeds_refined_prompt(monkeypatch):
    """run_prompt_lab sends the refined prompt to the model and surfaces it."""
    from web.server import api

    async def fake_refine(text: str):
        return prompt_refiner.RefineResult(original=text, refined="REFINED:" + text, changed=True)

    captured: dict = {}

    class _LabClient:
        def __init__(self, profile=None):  # noqa: ANN001
            self.profile = types.SimpleNamespace(provider="ollama", model="m")

        async def achat(self, messages, stream: bool = False):  # noqa: ANN001
            captured["messages"] = messages
            return _Resp("ok")

    # run_prompt_lab does `from .prompt_refiner import refine_prompt` and
    # `from agentforge.client import AIClient` at call time, so patch the sources.
    monkeypatch.setattr(prompt_refiner, "refine_prompt", fake_refine)
    monkeypatch.setattr("agentforge.client.AIClient", _LabClient)

    req = api.PromptLabRequest(prompt="hi", profiles=["light"])
    resp = asyncio.run(api.run_prompt_lab(req))

    assert resp.refined_prompt == "REFINED:hi"
    assert resp.original_prompt == "hi"
    assert captured["messages"][-1]["content"] == "REFINED:hi"


def test_is_initial_prompt_gate():
    """The agent gate refines only the opening prompt (no prior assistant turn)."""
    from web.server.ws_endpoint import _is_initial_prompt

    class _Msg:
        def __init__(self, role: str) -> None:
            self.role = role

    class _DB:
        def __init__(self, msgs: list) -> None:
            self._msgs = msgs

        def get_messages(self, _sid: str) -> list:
            return self._msgs

    class _BadDB:
        def get_messages(self, _sid: str) -> list:
            raise RuntimeError("history unavailable")

    assert _is_initial_prompt(_DB([]), "s") is True  # empty session
    assert _is_initial_prompt(_DB([_Msg("user")]), "s") is True  # only current user msg
    assert _is_initial_prompt(_DB([_Msg("user"), _Msg("assistant")]), "s") is False  # had a reply
    assert _is_initial_prompt(None, None) is True  # no session id
    assert _is_initial_prompt(_BadDB(), "s") is True  # read error -> treat as initial
