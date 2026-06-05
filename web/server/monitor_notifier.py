"""monitor_notifier — alert delivery for @monitor change detection.

Supports two notification channels:
  - **terminal-notifier** — macOS desktop notifications (runs on the host)
  - **webhook** — POST JSON to a URL (Slack, Discord, custom integrations)

Both can fire simultaneously when ``notification_method="both"``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def notify(
    label: str,
    url: str,
    status: str,
    diff_summary: str,
    notification_method: str = "terminal-notifier",
    webhook_url: str | None = None,
) -> bool:
    """Send a monitor change notification.

    Returns True if at least one notification channel succeeded.
    """
    success = False

    if notification_method in ("terminal-notifier", "both"):
        if _notify_terminal(label, url, status, diff_summary):
            success = True

    if notification_method in ("webhook", "both") and webhook_url:
        if _notify_webhook(webhook_url, label, url, status, diff_summary):
            success = True

    return success


def _notify_terminal(label: str, url: str, status: str, diff_summary: str) -> bool:
    """Send a macOS desktop notification via terminal-notifier."""
    try:
        title = f"🔍 Monitor: {label}"
        if status == "changed":
            subtitle = "Content changed"
            sound = "Basso"
        elif status == "error":
            subtitle = "Check failed"
            sound = "Sosumi"
        else:
            subtitle = status
            sound = "default"

        message = diff_summary[:200] if diff_summary else f"Change detected on {url}"

        cmd = [
            "terminal-notifier",
            "-title",
            title,
            "-subtitle",
            subtitle,
            "-message",
            message,
            "-open",
            url,
            "-sound",
            sound,
            "-group",
            f"monitor-{label}",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.info("Terminal notification sent for '%s'", label)
            return True
        else:
            logger.warning("terminal-notifier failed (rc=%d): %s", result.returncode, result.stderr)
            return False

    except FileNotFoundError:
        logger.warning("terminal-notifier not installed — skipping desktop notification")
        return False
    except Exception as exc:
        logger.warning("Terminal notification error: %s", exc)
        return False


def _notify_webhook(
    webhook_url: str,
    label: str,
    url: str,
    status: str,
    diff_summary: str,
) -> bool:
    """POST a JSON payload to a webhook URL."""
    payload = {
        "event": "monitor.changed",
        "label": label,
        "url": url,
        "status": status,
        "diff_summary": diff_summary,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "AgentForge-Monitor/1.0"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            if resp.status < 300:
                logger.info("Webhook notification sent for '%s' → %s", label, webhook_url)
                return True
            else:
                logger.warning("Webhook returned %d for '%s'", resp.status, label)
                return False

    except (URLError, OSError) as exc:
        logger.warning("Webhook notification failed for '%s': %s", label, exc)
        return False
    except Exception as exc:
        logger.warning("Webhook error: %s", exc)
        return False
