"""Naive chat timestamps gain the server offset on the way out of the API."""

from datetime import datetime, timezone

from web.server.database.models import (
    ChatSession,
    CommandNote,
    Connection,
    MonitorCheck,
    ScheduledJob,
    _iso,
)
from web.server.prompt_lab.database.models import PromptLabRun

NAIVE = datetime(2026, 11, 5, 21, 55, 14)


def _wall(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.replace(tzinfo=None) == NAIVE


def test_iso_keeps_the_wall_time_and_adds_the_local_offset():
    naive = _iso(NAIVE)
    text = _iso("2026-11-05T21:55:14")
    assert naive is not None
    assert text is not None
    _wall(naive)
    _wall(text)
    assert _iso(None) is None
    assert _iso("") is None

    aware = NAIVE.replace(tzinfo=timezone.utc)
    assert _iso(aware) == aware.isoformat()
    assert _iso(aware.isoformat()) == aware.isoformat()

    session = ChatSession(id="s", title="t", created_at=NAIVE, updated_at=NAIVE)
    body = session.to_dict()
    _wall(body["created_at"])
    _wall(body["updated_at"])

    note = CommandNote(title="n", commands_json="[]", created_at=NAIVE)
    _wall(note.to_dict()["created_at"])

    conn = Connection(
        id="c",
        connector_type="gitlab",
        label="GitLab",
        encrypted_tokens="x",
        created_at=NAIVE,
        updated_at=NAIVE,
    )
    dumped = conn.to_dict()
    assert dumped["last_used_at"] is None
    _wall(dumped["created_at"])

    job = ScheduledJob(
        id="j",
        label="nightly",
        command="echo ok",
        cron="0 0 * * *",
        created_at=NAIVE,
        updated_at=NAIVE,
    )
    assert job.to_dict()["last_run_at"] is None
    _wall(job.to_dict()["created_at"])

    check = MonitorCheck(job_id="m", started_at=NAIVE)
    assert check.to_dict()["finished_at"] is None
    _wall(check.to_dict()["started_at"])

    run = PromptLabRun(id="r", prompt="p", created_at=NAIVE)
    run.results = []
    _wall(run.to_dict()["created_at"])
