# tests/test_session_logging.py
from __future__ import annotations

import io
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentforge.session_logging as sl
from agentforge.config import get_request_session_id, set_request_session_id
from agentforge.session_logging import SessionIdFilter, bind_session_from_job, clear_session_binding

SID = "019d14f2-aaaa-bbbb-cccc-ddddeeeeffff"


def _emit(msg: str, *args: object) -> str:
    logger = logging.getLogger("test.session_logging")
    logger.setLevel(logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    handler.addFilter(SessionIdFilter())
    logger.handlers = [handler]
    logger.propagate = False
    logger.info(msg, *args)
    return stream.getvalue().rstrip("\n")


@pytest.fixture(autouse=True)
def _clear_session():
    set_request_session_id(None)
    yield
    set_request_session_id(None)


def test_appends_full_uuid_when_context_set():
    set_request_session_id(SID)
    line = _emit("hello")
    assert line.endswith(f" session_id={SID}")
    assert line.startswith("INFO:test.session_logging:hello")


def test_leaves_line_unchanged_when_unset():
    line = _emit("hello")
    assert line == "INFO:test.session_logging:hello"
    assert "session_id=" not in line


def test_does_not_double_append():
    set_request_session_id(SID)
    line = _emit("hello session_id=%s", SID)
    assert line.count(f"session_id={SID}") == 1


def test_ignores_non_uuid():
    set_request_session_id("sess-123")
    line = _emit("hello")
    assert line == "INFO:test.session_logging:hello"


def test_filter_swallows_contextvar_errors(monkeypatch):
    def boom() -> str | None:
        raise RuntimeError("nope")

    monkeypatch.setattr(sl, "get_request_session_id", boom)
    line = _emit("hello")
    assert line == "INFO:test.session_logging:hello"


def test_bind_from_job_kwargs_and_clear():
    bind_session_from_job(SimpleNamespace(kwargs={"session_id": SID}))
    assert get_request_session_id() == SID
    clear_session_binding()
    assert get_request_session_id() is None


def test_bind_ignores_missing_kwargs():
    set_request_session_id(SID)
    bind_session_from_job(SimpleNamespace(kwargs={}))
    assert get_request_session_id() is None


def test_no_session_id_slice_in_log_calls():
    roots = [Path("web/server"), Path("agentforge")]
    hits: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "session_id[:12]" in text:
                hits.append(str(path))
    assert hits == []
