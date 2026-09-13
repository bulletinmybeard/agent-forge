"""Cancelled / timed-out writes are not unverified successes."""

from __future__ import annotations

from web.server._hooks import _is_tool_failure, tool_audit_status


def test_cancelled_write_is_tool_failure():
    assert _is_tool_failure("Operation cancelled by user.") is True
    assert _is_tool_failure("(cancelled by user)") is True
    assert _is_tool_failure("Write not confirmed (timed out). File was not saved.") is True


def test_successful_write_is_not_failure():
    assert _is_tool_failure("Wrote 1,204 chars to /tmp/out.md") is False


def test_audit_status_cancelled_vs_error_vs_success():
    assert tool_audit_status("Operation cancelled by user.") == "cancelled"
    assert tool_audit_status("Write not confirmed (timed out). File was not saved.") == "cancelled"
    assert tool_audit_status("Error writing file: permission denied") == "error"
    assert tool_audit_status("Wrote 12 chars to ~/Downloads/a.md") == "success"
    assert tool_audit_status(None) == "success"
