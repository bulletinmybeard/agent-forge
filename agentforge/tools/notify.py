"""macOS notification tool — send system notifications via ``terminal-notifier``.

Wraps the ``terminal-notifier`` CLI (installed via Homebrew) to deliver
native macOS notifications from agent runs, scheduler jobs, or any tool
pipeline.  Supports clickable notifications that can open URLs, execute
shell commands, or activate specific apps.

Notifications are grouped by chat session ID so concurrent sessions don't
clobber each other — new notifications in the same group replace the old one.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.notify import register_notify_tools

    registry = ToolRegistry()
    register_notify_tools(registry)
"""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)

# Default group prefix — combined with session ID for per-session grouping
_GROUP_PREFIX = "agentforge"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: str, timeout: int = 15) -> str:
    """Run a shell command and return its stdout (or stderr on failure)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            stderr = result.stderr.strip()
            if output:
                output += f"\nSTDERR: {stderr}"
            else:
                output = f"Error: {stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except FileNotFoundError:
        return "Error: 'terminal-notifier' not found — install via: brew install terminal-notifier"
    except Exception as exc:
        return f"Error: {exc}"


def _build_group_id(group: str, session_id: str = "") -> str:
    """Build a notification group ID, optionally scoped to a chat session."""
    if session_id:
        return f"{group}/{session_id}"
    return group


def _execute_allowed() -> bool:
    """Whether ``notify(execute=...)`` may attach a click-to-run shell command.

    Off by default: ``-execute`` runs an arbitrary shell command when the user
    clicks the notification, so a prompt-injected agent could turn one click
    into RCE. Opt in via ``tools.notify.allow_execute: true``.
    """
    try:
        from agentforge.config import get_config

        cfg = get_config()
        return bool(cfg._raw.get("tools", {}).get("notify", {}).get("allow_execute", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# notify — send macOS system notification
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use notify to send a macOS system notification.  Great for alerting "
        "the user when a long-running task completes.  Supports clickable "
        "notifications that open URLs or run shell commands on click.  "
        "ALWAYS pass session_id (from the Session ID shown in the chat config bar) "
        "so the notification links directly to the active chat session.  "
        "Requires terminal-notifier (brew install terminal-notifier)."
    )
)
def notify(
    title: str,
    message: str,
    open_url: str = "",
    execute: str = "",
    subtitle: str = "",
    sound: str = "default",
    group: str = _GROUP_PREFIX,
    session_id: str = "",
    app_icon: str = "",
    content_image: str = "",
    ignore_dnd: bool = False,
) -> str:
    """Send a macOS system notification via terminal-notifier.

    title: notification title (bold text).
    message: notification body text.
    open_url: optional URL to open when the notification is clicked.
    execute: optional shell command to run when the notification is clicked.
             Mutually exclusive with open_url (open_url takes precedence).
    subtitle: optional subtitle line (displayed between title and message).
    sound: notification sound name (default 'default'). Use '' for silent.
           Common sounds: default, Basso, Blow, Bottle, Frog, Funk, Glass,
           Hero, Morse, Ping, Pop, Purr, Sosumi, Submarine, Tink.
    group: group ID for notification coalescing (default 'agentforge').
           Notifications with the same group replace each other.
    session_id: optional chat session ID — appended to group for per-session
                scoping so concurrent sessions don't replace each other's
                notifications.
    app_icon: optional URL/path to an image to display as the app icon.
    content_image: optional URL/path to an image attached to the notification.
    ignore_dnd: if True, deliver even when Do Not Disturb is enabled.

    Examples:
      notify('Build Complete', 'Frontend build finished successfully')
      notify('Deployment', 'Site deployed', open_url='https://example.com')
      notify('Health Check', 'All OK', subtitle='example.com', session_id='abc-123')
      notify('Backup', 'Done!', execute='open /tmp/backup.log')
      notify('Alert', 'Disk 90%', sound='Basso', ignore_dnd=True)
    """
    parts = [
        "terminal-notifier",
        f"-title {shlex.quote(title)}",
        f"-message {shlex.quote(message)}",
    ]

    if subtitle:
        parts.append(f"-subtitle {shlex.quote(subtitle)}")
    execute_blocked = False
    if open_url:
        parts.append(f"-open {shlex.quote(open_url)}")
    elif execute:
        if _execute_allowed():
            parts.append(f"-execute {shlex.quote(execute)}")
        else:
            # Click-to-run shell command is a prompt-injection -> RCE vector.
            # Dropped unless tools.notify.allow_execute is set.
            execute_blocked = True
            logger.warning("notify: ignoring execute=%r (tools.notify.allow_execute is off)", execute[:80])
    if sound:
        parts.append(f"-sound {shlex.quote(sound)}")

    # Group — scoped to session if provided
    effective_group = _build_group_id(group, session_id)
    parts.append(f"-group {shlex.quote(effective_group)}")

    if app_icon:
        parts.append(f"-appIcon {shlex.quote(app_icon)}")
    if content_image:
        parts.append(f"-contentImage {shlex.quote(content_image)}")
    if ignore_dnd:
        parts.append("-ignoreDnD")

    # When no open_url or (allowed) execute is set, clicking the notification
    # opens the AgentForge web UI — linking to the chat session if available
    if not open_url and (not execute or execute_blocked):
        if session_id:
            parts.append(f"-open {shlex.quote(f'http://localhost:8200/chat/{session_id}')}")
        else:
            parts.append("-open 'http://localhost:8200'")

    cmd = " ".join(parts)
    logger.info("Sending notification: %s — %s (group=%s)", title, message, effective_group)
    result = _run(cmd)

    if result.startswith("Error:"):
        return result
    if execute_blocked:
        return f"Notification sent: {title} — {message} (note: execute= ignored; enable tools.notify.allow_execute to use it)"
    return f"Notification sent: {title} — {message}"


# ---------------------------------------------------------------------------
# notify_list — list delivered notifications
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use notify_list to check which notifications are currently delivered "
        "for a given group.  Use group='ALL' to see all notifications.  "
        "Useful for checking if a previous notification was delivered."
    )
)
def notify_list(group: str = _GROUP_PREFIX, session_id: str = "") -> str:
    """List delivered notifications for a group.

    group: group ID to query (default 'agentforge'). Use 'ALL' to list everything.
    session_id: optional session ID — appended to group for per-session lookup.

    Examples:
      notify_list()                          # all agentforge notifications
      notify_list('ALL')                     # every notification
      notify_list(session_id='abc-123')      # notifications for a specific session
    """
    if group == "ALL":
        effective_group = "ALL"
    else:
        effective_group = _build_group_id(group, session_id)

    result = _run(f"terminal-notifier -list {shlex.quote(effective_group)}")
    if result == "(no output)":
        return f"No notifications found for group '{effective_group}'."
    return result


# ---------------------------------------------------------------------------
# notify_remove — remove/dismiss notifications
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use notify_remove to dismiss/clear notifications for a group.  Useful for cleaning up after a task completes."
    )
)
def notify_remove(group: str = _GROUP_PREFIX, session_id: str = "") -> str:
    """Remove/dismiss delivered notifications for a group.

    group: group ID to clear (default 'agentforge').
    session_id: optional session ID — appended to group for per-session clearing.

    Examples:
      notify_remove()                        # clear all agentforge notifications
      notify_remove(session_id='abc-123')    # clear notifications for a session
    """
    effective_group = _build_group_id(group, session_id)
    result = _run(f"terminal-notifier -remove {shlex.quote(effective_group)}")
    if result.startswith("Error:"):
        return result
    return f"Notifications removed for group '{effective_group}'."


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_notify_tools(registry: ToolRegistry) -> int:
    """Register notification tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "Notification",
        "macOS system notifications via terminal-notifier.  "
        "Use to alert the user when long tasks complete or when "
        "scheduler jobs finish.  Notifications can open URLs or "
        "run shell commands when clicked.  Group by session_id "
        "so concurrent chat sessions don't clobber each other.",
    )

    tools = [notify, notify_list, notify_remove]
    for func in tools:
        registry.register(func, category="Notification")
    return len(tools)
