"""Gmail tool factory — creates tool callables bound to a specific connection."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
_REQUEST_TIMEOUT = 20
_DEFAULT_BODY_MAX_CHARS = 8_000


# ---------------------------------------------------------------------------
# Body decoding helpers (migrated from gmail_tools.py)
# ---------------------------------------------------------------------------


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag in ("br", "p", "div", "li", "tr"):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _b64url_decode(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.debug("gmail connector: body decode failed: %s", exc)
        return ""


def _walk_parts(payload: dict, acc: list[dict]) -> None:
    if not payload:
        return
    parts = payload.get("parts") or []
    if not parts:
        acc.append(payload)
        return
    for part in parts:
        _walk_parts(part, acc)


def _extract_body_and_attachments(payload: dict) -> tuple[str, list[dict]]:
    parts: list[dict] = []
    _walk_parts(payload, parts)

    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict] = []

    for part in parts:
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        data = body.get("data")

        if filename and attachment_id:
            attachments.append(
                {
                    "filename": filename,
                    "mime_type": mime,
                    "size_bytes": int(body.get("size", 0) or 0),
                    "attachment_id": attachment_id,
                }
            )
            continue

        if not data:
            continue
        decoded = _b64url_decode(data)
        if mime == "text/plain":
            plain.append(decoded)
        elif mime == "text/html":
            html.append(decoded)

    if plain:
        text = "\n\n".join(plain).strip()
    elif html:
        stripper = _HTMLStripper()
        try:
            stripper.feed("\n\n".join(html))
            text = stripper.text()
        except Exception:
            text = "\n\n".join(html)
    else:
        text = ""

    return text, attachments


def _header(headers: list[dict], name: str) -> str:
    lower = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == lower:
            return str(h.get("value") or "")
    return ""


def _fmt_thread_summary(thread: dict) -> dict:
    messages = thread.get("messages") or []
    last = messages[-1] if messages else {}
    headers = (last.get("payload") or {}).get("headers") or []
    return {
        "thread_id": thread.get("id", ""),
        "history_id": thread.get("historyId", ""),
        "message_count": len(messages),
        "snippet": thread.get("snippet") or last.get("snippet") or "",
        "subject": _header(headers, "Subject"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "date": _header(headers, "Date"),
        "label_ids": last.get("labelIds") or [],
    }


def _fmt_message(msg: dict, max_chars: int) -> dict:
    headers = (msg.get("payload") or {}).get("headers") or []
    body, attachments = _extract_body_and_attachments(msg.get("payload") or {})
    truncated = False
    if max_chars and len(body) > max_chars:
        body = body[:max_chars]
        truncated = True
    return {
        "message_id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "subject": _header(headers, "Subject"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "cc": _header(headers, "Cc"),
        "date": _header(headers, "Date"),
        "label_ids": msg.get("labelIds") or [],
        "snippet": msg.get("snippet", ""),
        "body": body,
        "body_truncated": truncated,
        "attachments": attachments,
    }


# ---------------------------------------------------------------------------
# Unsubscribe helpers (migrated from gmail_tools.py)
# ---------------------------------------------------------------------------

_UNSUB_PAT = re.compile(
    r"unsubscribe|opt[-_ ]?out|abmelden|uitschrijven|se désinscrire|cancelar suscripción",
    re.I,
)
_unsubscribe_resolve_cache: dict[str, tuple[dict, float]] = {}
_UNSUB_CACHE_TTL = 60.0


def _parse_list_unsubscribe(header_value: str) -> tuple[str | None, str | None]:
    http_url: str | None = None
    mailto: str | None = None
    for part in re.findall(r"<([^>]+)>", header_value or ""):
        p = part.strip()
        if (p.startswith("http://") or p.startswith("https://")) and http_url is None:
            http_url = p
        elif p.startswith("mailto:") and mailto is None:
            mailto = p
    return http_url, mailto


def _scrape_body_unsub_link(payload: dict) -> str | None:
    parts: list[dict] = []
    _walk_parts(payload, parts)
    html_bits: list[str] = []
    plain_bits: list[str] = []
    for part in parts:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        decoded = _b64url_decode(data)
        if mime == "text/html":
            html_bits.append(decoded)
        elif mime == "text/plain":
            plain_bits.append(decoded)

    for html in html_bits:
        for m in re.finditer(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            html,
            re.I | re.DOTALL,
        ):
            url = m.group(1).strip()
            text = m.group(2) or ""
            if not url.startswith(("http://", "https://")):
                continue
            start = max(0, m.start() - 80)
            end = min(len(html), m.end() + 80)
            if _UNSUB_PAT.search(text) or _UNSUB_PAT.search(html[start:end]):
                return url

    for plain in plain_bits:
        for line in plain.split("\n"):
            if _UNSUB_PAT.search(line):
                m = re.search(r"https?://\S+", line)
                if m:
                    return m.group(0).rstrip(".,;:)]}")
    return None


def _sidecar_url() -> str | None:
    env_url = os.environ.get("SIDECAR_URL")
    if env_url:
        return env_url.rstrip("/")
    return None


_UNSUB_AVOID_BUTTON_TEXTS = [
    "manage permissions",
    "manage preferences",
    "manage communication",
    "click here",
    "change preferences",
    "update preferences",
]


def _sidecar_unsubscribe_click(url: str) -> dict | None:
    base = _sidecar_url()
    if not base:
        logger.info("gmail connector: sidecar not configured; skipping auto_click")
        return None

    payload = {
        "url": url,
        "avoid_button_texts": _UNSUB_AVOID_BUTTON_TEXTS,
        "timeout": 30,
    }
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    sidecar_token = os.environ.get("SIDECAR_AUTH_TOKEN", "").strip()
    if sidecar_token:
        headers["X-Sidecar-Token"] = sidecar_token
    req = Request(
        f"{base}/unsubscribe/click",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except (HTTPError, URLError) as exc:
        logger.warning("gmail connector: sidecar /unsubscribe/click failed: %s", exc)
        return None
    except Exception as exc:
        logger.warning("gmail connector: sidecar call error: %s", exc)
        return None


def _http_call(
    url: str,
    *,
    method: str,
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int | None, str]:
    headers = {"User-Agent": "AgentForge-Unsubscribe/1.0"}
    if content_type:
        headers["Content-Type"] = content_type

    last_err = ""
    for attempt in range(2):
        try:
            req = Request(url, data=body, method=method, headers=headers)
            with urlopen(req, timeout=10) as resp:
                return getattr(resp, "status", 200), ""
        except HTTPError as exc:
            if exc.code in (429, 503) and attempt == 0:
                time.sleep(2)
                last_err = f"HTTP {exc.code}: {exc.reason}"
                continue
            return exc.code, f"HTTP {exc.code}: {exc.reason}"
        except URLError as exc:
            last_err = f"URL error: {exc.reason}"
        except Exception as exc:
            last_err = f"Error: {exc}"
    return None, last_err or "Retries exhausted"


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def create_gmail_tools(
    connection_id: str,
    token_accessor: Callable[[], str],
) -> list[Callable]:
    """Create Gmail tool callables bound to a specific connection's credentials."""

    def _gmail_get(path: str, params: dict | None = None) -> dict:
        token = token_accessor()
        qs = f"?{urlencode(params)}" if params else ""
        url = f"{_GMAIL_BASE}{path}{qs}"

        def _do(t: str) -> dict:
            req = Request(url, headers={"Authorization": f"Bearer {t}"})
            with urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode())

        try:
            return _do(token)
        except HTTPError as exc:
            if exc.code != 401:
                raise
            new_token = token_accessor()
            return _do(new_token)

    def _resolve_unsubscribe(sender: str, thread_id: str) -> dict:
        key = f"{sender}|{thread_id}"
        cached = _unsubscribe_resolve_cache.get(key)
        if cached is not None:
            info, stored_at = cached
            if time.monotonic() - stored_at < _UNSUB_CACHE_TTL:
                return info

        resolved_thread_id = thread_id
        if not resolved_thread_id:
            if not sender:
                raise ValueError("sender or thread_id is required")
            listing = _gmail_get(
                "/users/me/threads",
                params={"q": f"from:{sender} newer_than:60d", "maxResults": 1},
            )
            threads = listing.get("threads") or []
            if not threads:
                info = {
                    "thread_id": "",
                    "sender": sender,
                    "method": "none",
                    "target": None,
                    "note": f"No threads from {sender} in the last 60 days.",
                }
                _unsubscribe_resolve_cache[key] = (info, time.monotonic())
                return info
            resolved_thread_id = threads[0].get("id") or ""

        thread = _gmail_get(f"/users/me/threads/{resolved_thread_id}", params={"format": "full"})
        messages = thread.get("messages") or []
        first = messages[0] if messages else {}
        headers = (first.get("payload") or {}).get("headers") or []
        from_header = _header(headers, "From")
        list_unsub = _header(headers, "List-Unsubscribe")
        list_unsub_post = _header(headers, "List-Unsubscribe-Post")

        http_url, mailto = _parse_list_unsubscribe(list_unsub) if list_unsub else (None, None)
        is_one_click = list_unsub_post and "one-click" in list_unsub_post.lower() and http_url is not None

        if is_one_click:
            method = "one_click_post"
            target: str | None = http_url
        elif http_url:
            method = "http_get"
            target = http_url
        elif mailto:
            method = "mailto"
            target = mailto
        else:
            body_link = _scrape_body_unsub_link(first.get("payload") or {})
            if body_link:
                method = "body_link"
                target = body_link
            else:
                method = "none"
                target = None

        info = {
            "thread_id": resolved_thread_id,
            "sender": from_header or sender,
            "method": method,
            "target": target,
        }
        _unsubscribe_resolve_cache[key] = (info, time.monotonic())
        return info

    def _unsubscribe_condition(sender: str = "", thread_id: str = "", auto_click: bool = False) -> str | None:
        try:
            info = _resolve_unsubscribe(sender, thread_id)
        except Exception:
            target_label = sender or f"thread {thread_id}" or "sender"
            return (
                f"Unsubscribe from {target_label}? (pre-check failed -- the method "
                f"will be determined when the tool runs)"
            )
        method = info.get("method")
        sender_label = info.get("sender") or sender
        target = info.get("target")
        if method == "one_click_post":
            return f"Unsubscribe from {sender_label} via one-click POST (RFC 8058)?\n  target: {target}"
        if method == "http_get":
            return (
                f"Unsubscribe from {sender_label} via https GET? "
                f"(success is tentative -- page may still require a click)\n"
                f"  target: {target}"
            )
        if method == "body_link" and auto_click:
            return f"Click-through unsubscribe from {sender_label} via the browser?\n  target: {target}"
        return None

    # -- Tool functions (closures over _gmail_get) --------------------------

    from agentforge.tools.registry import tool

    @tool
    def gmail_search_threads(query: str, limit: int = 20) -> str:
        """Search Gmail threads with the standard Gmail query syntax.

        Examples of queries:
            from:alex@example.com newer_than:7d
            subject:"invoice" has:attachment
            label:starred after:2026/03/01

        Returns a list of thread summaries with subject, sender, date, snippet,
        and label IDs.
        """
        try:
            params: dict = {"maxResults": max(1, min(int(limit), 100))}
            if query:
                params["q"] = query
            listing = _gmail_get("/users/me/threads", params=params)
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {exc}"})
        except Exception as exc:
            logger.error("gmail_search_threads error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        entries = listing.get("threads") or []
        if not entries:
            return json.dumps(
                {
                    "status": "no_results",
                    "query": query,
                    "message": "No threads matched your query.",
                }
            )

        summaries: list[dict] = []
        for entry in entries:
            thread_id = entry.get("id")
            if not thread_id:
                continue
            try:
                thread = _gmail_get(f"/users/me/threads/{thread_id}", params={"format": "metadata"})
            except Exception as exc:
                logger.debug("gmail_search_threads: skip thread %s (%s)", thread_id, exc)
                continue
            summaries.append(_fmt_thread_summary(thread))

        return json.dumps(
            {
                "status": "ok",
                "query": query,
                "count": len(summaries),
                "threads": summaries,
            },
            indent=2,
        )

    @tool
    def gmail_get_thread(thread_id: str, max_chars: int = _DEFAULT_BODY_MAX_CHARS) -> str:
        """Fetch a full Gmail thread -- every message with headers, decoded body, and attachment metadata."""
        if not thread_id:
            return json.dumps({"status": "error", "error": "thread_id is required"})

        try:
            thread = _gmail_get(f"/users/me/threads/{thread_id}", params={"format": "full"})
        except HTTPError as exc:
            if exc.code == 404:
                return json.dumps({"status": "not_found", "thread_id": thread_id})
            return json.dumps({"status": "error", "error": f"HTTP error: {exc}"})
        except (URLError, Exception) as exc:
            logger.error("gmail_get_thread error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        messages = [_fmt_message(m, max_chars=max_chars) for m in (thread.get("messages") or [])]
        return json.dumps(
            {
                "status": "ok",
                "thread_id": thread.get("id", thread_id),
                "history_id": thread.get("historyId", ""),
                "message_count": len(messages),
                "messages": messages,
            },
            indent=2,
        )

    @tool
    def gmail_get_message(message_id: str, max_chars: int = _DEFAULT_BODY_MAX_CHARS) -> str:
        """Fetch a single Gmail message by ID."""
        if not message_id:
            return json.dumps({"status": "error", "error": "message_id is required"})

        try:
            msg = _gmail_get(f"/users/me/messages/{message_id}", params={"format": "full"})
        except HTTPError as exc:
            if exc.code == 404:
                return json.dumps({"status": "not_found", "message_id": message_id})
            return json.dumps({"status": "error", "error": f"HTTP error: {exc}"})
        except (URLError, Exception) as exc:
            logger.error("gmail_get_message error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        return json.dumps(
            {
                "status": "ok",
                "message": _fmt_message(msg, max_chars=max_chars),
            },
            indent=2,
        )

    @tool
    def gmail_list_labels() -> str:
        """List all Gmail labels (system + user-defined).

        Returns label IDs and names -- use them with the `label:` operator in
        gmail_search_threads queries.
        """
        try:
            data = _gmail_get("/users/me/labels")
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {exc}"})
        except Exception as exc:
            logger.error("gmail_list_labels error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        labels = [
            {
                "id": lbl.get("id", ""),
                "name": lbl.get("name", ""),
                "type": lbl.get("type", ""),
            }
            for lbl in data.get("labels") or []
        ]
        return json.dumps({"status": "ok", "count": len(labels), "labels": labels}, indent=2)

    @tool
    def gmail_get_profile() -> str:
        """Return the connected Gmail account -- email address and total message count."""
        try:
            data = _gmail_get("/users/me/profile")
        except (HTTPError, URLError) as exc:
            return json.dumps({"status": "error", "error": f"HTTP error: {exc}"})
        except Exception as exc:
            logger.error("gmail_get_profile error: %s", exc)
            return json.dumps({"status": "error", "error": str(exc)})

        return json.dumps(
            {
                "status": "ok",
                "email": data.get("emailAddress", ""),
                "messages_total": int(data.get("messagesTotal", 0) or 0),
                "threads_total": int(data.get("threadsTotal", 0) or 0),
                "history_id": data.get("historyId", ""),
            },
            indent=2,
        )

    @tool(
        confirm_condition=_unsubscribe_condition,
        hint=(
            "Unsubscribe from a sender by reading the RFC 2369 / 8058 List-Unsubscribe "
            "headers of the most recent matching thread. Set auto_click=True only when "
            "the user explicitly asks for automated click-through."
        ),
    )
    def gmail_unsubscribe(sender: str = "", thread_id: str = "", auto_click: bool = False) -> str:
        """Unsubscribe from a sender using the List-Unsubscribe headers.

        sender: free-form string used as Gmail 'from:' filter (e.g., '@ziggo.nl')
        thread_id: explicit thread id -- wins over sender when both are given
        auto_click: opt in to sidecar-driven click-through for body_link senders
        """
        sender = (sender or "").strip()
        thread_id = (thread_id or "").strip()
        if not sender and not thread_id:
            return json.dumps(
                {
                    "status": "failed",
                    "error": "Pass either sender or thread_id.",
                }
            )

        try:
            info = _resolve_unsubscribe(sender, thread_id)
        except (HTTPError, URLError) as exc:
            return json.dumps(
                {
                    "status": "failed",
                    "sender": sender,
                    "thread_id": thread_id,
                    "error": f"HTTP error while looking up thread: {exc}",
                }
            )
        except Exception as exc:
            logger.error("gmail_unsubscribe resolve error: %s", exc)
            return json.dumps(
                {
                    "status": "failed",
                    "sender": sender,
                    "thread_id": thread_id,
                    "error": str(exc),
                }
            )

        method = info["method"]
        target = info.get("target")
        thread_id_resolved = info.get("thread_id", "")
        sender_resolved = info.get("sender") or sender

        if method == "none":
            return json.dumps(
                {
                    "status": "failed",
                    "sender": sender_resolved,
                    "thread_id": thread_id_resolved,
                    "method": "none",
                    "target": None,
                    "http_status": None,
                    "note": info.get(
                        "note",
                        f"No List-Unsubscribe header and no body link found for "
                        f"{sender_resolved or thread_id_resolved or 'the selected thread'}.",
                    ),
                }
            )
        if method == "mailto":
            return json.dumps(
                {
                    "status": "manual_required",
                    "sender": sender_resolved,
                    "thread_id": thread_id_resolved,
                    "method": "mailto",
                    "target": target,
                    "http_status": None,
                    "note": (
                        "Sender only accepts unsubscribe via email and we do not have "
                        "gmail.send scope. Send a blank message to the address yourself."
                    ),
                }
            )
        if method == "body_link":
            if auto_click and target:
                sidecar_result = _sidecar_unsubscribe_click(target)
                if sidecar_result is None:
                    return json.dumps(
                        {
                            "status": "manual_required",
                            "sender": sender_resolved,
                            "thread_id": thread_id_resolved,
                            "method": "body_link",
                            "target": target,
                            "http_status": None,
                            "note": (
                                "auto_click was requested but the sidecar is unreachable "
                                "-- falling back to the manual body link."
                            ),
                        }
                    )

                sc_status = sidecar_result.get("status")
                if sc_status == "clicked":
                    status_tier = (
                        "unsubscribed_via_browser"
                        if sidecar_result.get("success_detected")
                        else "maybe_unsubscribed_via_browser"
                    )
                    return json.dumps(
                        {
                            "status": status_tier,
                            "sender": sender_resolved,
                            "thread_id": thread_id_resolved,
                            "method": "body_link",
                            "target": target,
                            "http_status": None,
                            "clicked_selector": sidecar_result.get("clicked_selector"),
                            "final_url": sidecar_result.get("final_url"),
                            "final_heading": sidecar_result.get("final_heading"),
                            "success_signal": sidecar_result.get("success_signal"),
                            "note": (
                                f"Browser clicked {sidecar_result.get('clicked_selector')}. "
                                f"Signal: {sidecar_result.get('success_signal') or 'none -- verify manually'}."
                            ),
                        }
                    )

                return json.dumps(
                    {
                        "status": "failed",
                        "sender": sender_resolved,
                        "thread_id": thread_id_resolved,
                        "method": "body_link",
                        "target": target,
                        "http_status": None,
                        "note": (
                            f"Sidecar click-through {sc_status}: "
                            f"{sidecar_result.get('error') or 'no details'}. "
                            f"Open the target URL manually to unsubscribe."
                        ),
                    }
                )

            return json.dumps(
                {
                    "status": "manual_required",
                    "sender": sender_resolved,
                    "thread_id": thread_id_resolved,
                    "method": "body_link",
                    "target": target,
                    "http_status": None,
                    "note": (
                        "No List-Unsubscribe header -- best-effort link from the message "
                        "body. Open the URL to unsubscribe."
                    ),
                }
            )

        if method == "one_click_post":
            http_status, err = _http_call(
                target or "",
                method="POST",
                body=urlencode({"List-Unsubscribe": "One-Click"}).encode(),
                content_type="application/x-www-form-urlencoded",
            )
        else:
            http_status, err = _http_call(target or "", method="GET")

        if err or http_status is None or http_status >= 400:
            return json.dumps(
                {
                    "status": "failed",
                    "sender": sender_resolved,
                    "thread_id": thread_id_resolved,
                    "method": method,
                    "target": target,
                    "http_status": http_status,
                    "note": err or f"HTTP {http_status}",
                }
            )

        if method == "one_click_post":
            return json.dumps(
                {
                    "status": "unsubscribed",
                    "sender": sender_resolved,
                    "thread_id": thread_id_resolved,
                    "method": method,
                    "target": target,
                    "http_status": http_status,
                    "note": "RFC 8058 one-click POST returned 2xx.",
                }
            )
        return json.dumps(
            {
                "status": "maybe_unsubscribed",
                "sender": sender_resolved,
                "thread_id": thread_id_resolved,
                "method": method,
                "target": target,
                "http_status": http_status,
                "note": ("GET succeeded but some senders land on a confirm page -- open the target URL to verify."),
            }
        )

    return [
        gmail_search_threads,
        gmail_get_thread,
        gmail_get_message,
        gmail_list_labels,
        gmail_get_profile,
        gmail_unsubscribe,
    ]
