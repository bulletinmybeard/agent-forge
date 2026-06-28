"""Tests for Apple Reminders tools."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

import agentforge.tools.reminders_tools as rt
from agentforge.tools.registry import ToolRegistry


def test_normalize_id_strips_apple_prefix():
    assert rt._normalize_id("x-apple-reminder://ABC-123") == "ABC-123"
    assert rt._normalize_id("ABC-123") == "ABC-123"


def test_split_ids_handles_commas_and_prefixes():
    assert rt._split_ids("ABC, x-apple-reminder://DEF") == ["ABC", "x-apple-reminder://DEF"]


def test_register_on_non_macos_for_split_dispatch():
    registry = ToolRegistry()
    with patch.object(rt, "_is_macos", return_value=False):
        count = rt.register_reminders_tools(registry)
    assert count == 8
    assert "reminders_show" in registry.list_tools()
    assert "reminders_find" in registry.list_tools()


def test_register_on_macos():
    registry = ToolRegistry()
    with patch.object(rt, "_is_macos", return_value=True):
        count = rt.register_reminders_tools(registry)
    assert count == 8
    assert "reminders_add" in registry.list_tools()
    assert "reminders_delete" in registry.list_tools()


def test_reminders_status_uses_remindctl(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    monkeypatch.setattr(rt, "_has_remindctl", lambda: True)

    with patch.object(rt, "_run_remindctl", return_value='{"authorized": true}') as run:
        result = rt.reminders_status()

    run.assert_called_once_with(["status", "--json"])
    assert result == '{"authorized": true}'


def test_reminders_add_builds_remindctl_args(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    monkeypatch.setattr(rt, "_has_remindctl", lambda: True)

    with patch.object(
        rt,
        "_run_remindctl",
        return_value='{"title": "Buy milk"}',
    ) as run:
        result = rt.reminders_add(
            "Buy milk",
            list_name="Groceries",
            due_date="2026-06-30",
            notes="2%",
            priority="high",
        )

    run.assert_called_once_with(
        [
            "add",
            "Buy milk",
            "--json",
            "--list",
            "Groceries",
            "--due",
            "2026-06-30",
            "--notes",
            "2%",
            "--priority",
            "high",
        ]
    )
    assert "Buy milk" in result


def test_reminders_edit_rejects_conflicting_flags(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    result = rt.reminders_edit("ABC", due_date="today", clear_due=True)
    assert result.startswith("Error:")


def test_reminders_complete_splits_ids(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    monkeypatch.setattr(rt, "_has_remindctl", lambda: True)

    with patch.object(rt, "_run_remindctl", return_value='[{"id": "A"}]') as run:
        rt.reminders_complete("AAAAAAAA, x-apple-reminder://BBBBBBBB")

    run.assert_called_once_with(["complete", "AAAAAAAA", "BBBBBBBB", "--json"])


def test_is_reminder_id():
    assert rt._is_reminder_id("A84C4205-C899-4EBC-A54A-84D6E5A15A58")
    assert rt._is_reminder_id("A84C4205")
    assert not rt._is_reminder_id("Buy milk")


def test_resolve_reminder_ids_by_title(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    payload = [{"id": "UUID-1234-5678", "title": "Buy milk"}]
    with patch.object(rt, "_load_reminders_for_search", return_value=payload):
        resolved, err = rt._resolve_reminder_ids(["Buy milk"])
    assert err is None
    assert resolved == ["UUID-1234-5678"]


def test_reminders_delete_resolves_title(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    monkeypatch.setattr(rt, "_has_remindctl", lambda: True)

    with patch.object(rt, "_resolve_reminder_ids", return_value=(["ABC12345"], None)):
        with patch.object(rt, "_remindctl_delete", return_value='{"deleted": 1}') as delete:
            result = rt.reminders_delete("Buy milk")

    delete.assert_called_once_with(["ABC12345"], force=True)
    assert "deleted" in result


def test_reminders_show_applies_limit(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    monkeypatch.setattr(rt, "_has_remindctl", lambda: True)

    payload = [{"title": f"item-{i}"} for i in range(5)]
    with patch.object(rt, "_run_remindctl", return_value=json.dumps(payload)):
        result = rt.reminders_show("open", limit=2)

    data = json.loads(result.split("\n(showing")[0])
    assert len(data) == 2
    assert "showing 2 of 5" in result


def test_reminders_add_osascript_fallback_without_remindctl(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    monkeypatch.setattr(rt, "_has_remindctl", lambda: False)

    with patch.object(rt, "_osascript_add", return_value='{"title": "Test"}') as add:
        result = rt.reminders_add("Test", list_name="Private")

    add.assert_called_once()
    assert "Test" in result


def test_reminders_add_rejects_priority_without_remindctl(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    monkeypatch.setattr(rt, "_has_remindctl", lambda: False)

    result = rt.reminders_add("Test", priority="high")
    assert "remindctl" in result


def test_validate_due_date_rejects_past_iso():
    err = rt._validate_due_date("2026-05-16 09:00")
    assert err is not None
    assert "in the past" in err
    assert "tomorrow" in err


def test_normalize_due_date_passes_keywords():
    assert rt._normalize_due_date("Tomorrow") == "tomorrow"
    assert rt._validate_due_date("tomorrow") is None


def test_normalize_due_date_in_half_hour(monkeypatch):
    fixed = datetime(2026, 6, 28, 10, 45, tzinfo=datetime.now().astimezone().tzinfo)
    monkeypatch.setattr(rt, "_now_local", lambda: fixed)
    assert rt._normalize_due_date("in half an hour") == "2026-06-28 11:15"


def test_validate_due_date_rejects_bare_1530():
    err = rt._validate_due_date("15:30")
    assert err is not None
    assert "Half an hour" in err


def test_reminders_add_due_in_minutes(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    monkeypatch.setattr(rt, "_has_remindctl", lambda: True)
    fixed = datetime(2026, 6, 28, 10, 45, tzinfo=datetime.now().astimezone().tzinfo)
    monkeypatch.setattr(rt, "_now_local", lambda: fixed)

    with patch.object(rt, "_run_remindctl", return_value='{"title": "Walk Lali"}') as run:
        rt.reminders_add("Walk Lali", list_name="Private", due_in_minutes=30)

    assert run.call_args[0][0][-1] == "2026-06-28 11:15"


def test_normalize_due_date_resolves_tomorrow_with_time(monkeypatch):
    monkeypatch.setattr(rt, "_local_today", lambda: __import__("datetime").date(2026, 6, 28))
    assert rt._normalize_due_date("tomorrow 09:00") == "2026-06-29 09:00"


def test_reminders_add_rejects_past_due_date(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    result = rt.reminders_add("Buy milk", list_name="Private", due_date="2026-05-16 09:00")
    assert "in the past" in result


def test_reminders_find_matches_substring(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    payload = [
        {"id": "A1", "title": "Buy milk", "list": "Private"},
        {"id": "B2", "title": "Buy eggs", "list": "Private"},
    ]
    with patch.object(rt, "_load_reminders_for_search", return_value=payload):
        result = rt.reminders_find("milk")

    assert "Buy milk" in result
    assert "Buy eggs" not in result


def test_reminders_lists_rejects_limit_one_on_named_list(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    result = rt.reminders_lists("Private", limit=1)
    assert result.startswith("Error:")
    assert "reminders_find" in result


def test_format_json_shows_total_when_truncated():
    data = [{"title": f"item-{i}"} for i in range(10)]
    result = rt._format_json(data, limit=3)
    assert "showing 3 of 10" in result
    assert "reminders_find" in result


def test_reminders_edit_resolves_title(monkeypatch):
    monkeypatch.setattr(rt, "_is_macos", lambda: True)
    monkeypatch.setattr(rt, "_has_remindctl", lambda: True)

    with patch.object(rt, "_resolve_reminder_ids", return_value=(["UUID-1"], None)):
        with patch.object(rt, "_remindctl_edit", return_value='{"updated": true}') as edit:
            result = rt.reminders_edit("Buy milk", due_date="tomorrow 11:15")

    edit.assert_called_once()
    assert edit.call_args[0][0] == "UUID-1"
    assert "updated" in result
