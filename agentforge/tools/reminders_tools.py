"""Apple Reminders tools — manage reminders on macOS.

Uses ``remindctl`` (preferred, via ``brew install steipete/tap/remindctl``) when
available on PATH.  Falls back to ``osascript`` / AppleScript otherwise.

All tools are macOS-only and register only on Darwin.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)

_SHOW_FILTERS = frozenset(
    {
        "today",
        "tomorrow",
        "yesterday",
        "week",
        "overdue",
        "upcoming",
        "open",
        "completed",
        "all",
    }
)
_PRIORITY_VALUES = frozenset({"none", "low", "medium", "high"})
_REPEAT_VALUES = frozenset(
    {
        "daily",
        "weekly",
        "biweekly",
        "monthly",
        "yearly",
    }
)
_ID_PREFIX_RE = re.compile(r"^x-apple-reminder://", re.IGNORECASE)
_RELATIVE_DUE_KEYWORDS = frozenset({"today", "tomorrow", "yesterday"})
_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_RELATIVE_DUE_WITH_TIME_RE = re.compile(
    r"^(today|tomorrow|yesterday)(?:\s+(?:at\s+)?(\d{1,2}:\d{2}))?$",
    re.IGNORECASE,
)
_IN_DURATION_RE = re.compile(
    r"^in\s+(?:"
    r"(?P<minutes>\d+)\s*(?:minutes?|mins?)|"
    r"(?P<hours>\d+(?:\.\d+)?)\s*(?:hours?|hrs?)|"
    r"(?P<half>half\s+an?\s+hour)|"
    r"(?P<hour>an?\s+hour)"
    r")\s*$",
    re.IGNORECASE,
)
_CLOCK_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}$")
_ID_LIKE_RE = re.compile(r"^[0-9A-Fa-f-]{8,36}$")


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _has_remindctl() -> bool:
    return shutil.which("remindctl") is not None


def _backend() -> str:
    return "remindctl" if _has_remindctl() else "osascript"


def _normalize_id(reminder_id: str) -> str:
    return _ID_PREFIX_RE.sub("", reminder_id.strip())


def _split_ids(reminder_ids: str) -> list[str]:
    return [part.strip() for part in reminder_ids.split(",") if part.strip()]


def _is_reminder_id(value: str) -> bool:
    """True when *value* looks like a remindctl ID or ID prefix (not a title)."""
    candidate = _normalize_id(value.strip())
    return bool(candidate) and bool(_ID_LIKE_RE.match(candidate))


def _parse_reminders_json(raw: str) -> list[dict[str, Any]] | str:
    if raw.startswith("Error:"):
        return raw
    try:
        data = json.loads(raw.split("\n(showing")[0])
    except json.JSONDecodeError:
        return f"Error: could not parse reminders JSON: {raw[:200]}"
    return data if isinstance(data, list) else []


def _load_open_reminders(*, list_name: str = "") -> list[dict[str, Any]] | str:
    if _has_remindctl():
        if list_name:
            raw = _remindctl_list_reminders(list_name)
        else:
            raw = _remindctl_show("open")
        return _parse_reminders_json(raw)

    if list_name:
        raw = _osascript_list_reminders(list_name)
    else:
        raw = _osascript_show("open")
    if raw.startswith("Error:"):
        return raw
    return _parse_reminders_json(raw)


def _load_reminders_for_search(
    *,
    list_name: str = "",
    include_completed: bool = False,
) -> list[dict[str, Any]] | str:
    """Collect reminders for title search."""
    if not _has_remindctl():
        filters = ["open"]
        if include_completed:
            filters.append("completed")
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for filter_name in filters:
            raw = _osascript_show(filter_name, list_name=list_name)
            parsed = _parse_reminders_json(raw)
            if isinstance(parsed, str):
                return parsed
            for item in parsed:
                rid = str(item.get("id", ""))
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                merged.append(item)
        return merged

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _extend(raw: str) -> str | None:
        parsed = _parse_reminders_json(raw)
        if isinstance(parsed, str):
            return parsed
        for item in parsed:
            rid = str(item.get("id", ""))
            if rid and rid in seen:
                continue
            if rid:
                seen.add(rid)
            merged.append(item)
        return None

    if list_name:
        err = _extend(_remindctl_list_reminders(list_name))
        if err:
            return err
        if include_completed:
            err = _extend(_remindctl_show("completed", list_name=list_name))
            if err:
                return err
        return merged

    err = _extend(_remindctl_show("open"))
    if err:
        return err
    if include_completed:
        err = _extend(_remindctl_show("completed"))
        if err:
            return err
    return merged


def _match_title_query(title: str, query: str) -> bool:
    return query.casefold() in title.casefold()


def _resolve_reminder_ids(
    identifiers: list[str],
    *,
    list_name: str = "",
) -> tuple[list[str], str | None]:
    """Map titles to IDs when the model passes a name instead of a UUID."""
    resolved: list[str] = []
    title_lookups: list[str] = []

    for item in identifiers:
        if _is_reminder_id(item):
            resolved.append(_normalize_id(item))
        else:
            title_lookups.append(item)

    if not title_lookups:
        return resolved, None

    reminders = _load_reminders_for_search(list_name=list_name, include_completed=False)
    if isinstance(reminders, str):
        return [], reminders

    for title in title_lookups:
        needle = title.casefold()
        matches = [r for r in reminders if str(r.get("title", "")).casefold() == needle]
        if not matches:
            return [], f'Error: Reminder not found by title: "{title}". Call reminders_show first and use the id field.'
        if len(matches) > 1:
            options = ", ".join(f"{m.get('title')} ({m.get('id', '')[:8]})" for m in matches[:5])
            return [], (f'Error: Multiple reminders titled "{title}". Pass a specific id instead: {options}')
        resolved.append(str(matches[0]["id"]))

    return resolved, None


def _format_json(data: Any, *, limit: int | None = None) -> str:
    suffix = ""
    if isinstance(data, list) and limit is not None and limit > 0:
        total = len(data)
        if total > limit:
            data = data[:limit]
            suffix = f"\n(showing {limit} of {total} items — use reminders_find with title_query to search by name)"
    return json.dumps(data, indent=2, ensure_ascii=False) + suffix


def _reject_browse_limit(
    limit: int,
    *,
    title_query: str,
    tool_name: str,
    list_name: str = "",
) -> str | None:
    """Block limit=1-style browsing on a named list (e.g. Private with 200+ items)."""
    if not list_name.strip() or title_query.strip() or limit >= 5:
        return None
    return (
        f"Error: limit={limit} on {tool_name} for list {list_name!r} hides most items. "
        "Use reminders_find(title_query='Buy milk') to search by name, or omit limit."
    )


def _run(argv: list[str], *, timeout: int = 60) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"command timed out ({timeout}s limit)"
    except FileNotFoundError:
        return 1, "", f"command not found: {argv[0]}"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _run_remindctl(args: list[str], *, timeout: int = 60) -> str:
    code, stdout, stderr = _run(["remindctl", *args], timeout=timeout)
    if code != 0:
        detail = stderr or stdout or "remindctl failed"
        return f"Error: {detail}"
    return stdout or "(no output)"


def _run_osascript(script: str, *, timeout: int = 120) -> str:
    code, stdout, stderr = _run(["osascript", "-e", script], timeout=timeout)
    if code != 0:
        detail = stderr or stdout or "osascript failed"
        return f"Error: {detail}"
    return stdout or "(no output)"


def _applescript_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _require_macos() -> str | None:
    if not _is_macos():
        return "Error: Apple Reminders tools are only available on macOS."
    return None


def _install_hint() -> str:
    if _has_remindctl():
        return ""
    return " Install remindctl for richer support: brew install steipete/tap/remindctl"


def _local_today() -> date:
    return date.today()


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _format_due_datetime(dt: datetime) -> str:
    return dt.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")


def _parse_duration_minutes(text: str) -> int | None:
    """Parse 'in 30 minutes', 'in half an hour', 'an hour', etc."""
    stripped = text.strip()
    if not stripped:
        return None
    match = _IN_DURATION_RE.match(stripped)
    if not match:
        return None
    if match.group("half"):
        return 30
    if match.group("hour"):
        return 60
    if match.group("minutes"):
        return int(match.group("minutes"))
    if match.group("hours"):
        return int(float(match.group("hours")) * 60)
    return None


def _due_date_from_minutes(minutes: int) -> str:
    return _format_due_datetime(_now_local() + timedelta(minutes=minutes))


def _validate_due_date(due_date: str) -> str | None:
    """Reject absolute due dates in the past; pass relative keywords through."""
    if not due_date or not due_date.strip():
        return None
    stripped = due_date.strip()
    if stripped.lower() in _RELATIVE_DUE_KEYWORDS:
        return None
    if _parse_duration_minutes(stripped):
        return None
    if _CLOCK_ONLY_RE.match(stripped):
        return (
            f"Error: bare clock time {stripped!r} is ambiguous. "
            "'Half an hour' means 30 minutes from now — use due_in_minutes=30 or "
            "due_date='in 30 minutes', NOT '15:30'. For a wall-clock time use "
            f"'today {stripped}' or '{_local_today().isoformat()} {stripped}'."
        )
    match = _ISO_DATE_PREFIX_RE.match(stripped)
    if not match:
        return None
    try:
        parsed = date.fromisoformat(match.group(1))
    except ValueError:
        return None
    today = _local_today()
    if parsed < today:
        tomorrow = (today + timedelta(days=1)).isoformat()
        return (
            f"Error: due_date {stripped!r} is in the past (today is {today.isoformat()}). "
            f"For relative dates pass tomorrow or today — do NOT invent ISO dates. "
            f"For a specific future time use e.g. '{tomorrow} 09:00'."
        )
    return None


def _resolve_due_fields(due_date: str, due_in_minutes: int) -> str:
    if due_in_minutes and due_in_minutes > 0:
        return _due_date_from_minutes(due_in_minutes)
    return _normalize_due_date(due_date)


def _normalize_due_date(due_date: str) -> str:
    """Resolve relative due-date keywords to remindctl-friendly absolute values."""
    if not due_date:
        return ""
    stripped = due_date.strip()
    duration = _parse_duration_minutes(stripped)
    if duration is not None:
        return _due_date_from_minutes(duration)
    match = _RELATIVE_DUE_WITH_TIME_RE.match(stripped)
    if not match:
        return stripped

    keyword = match.group(1).lower()
    time_part = match.group(2)
    base = _local_today()
    if keyword == "tomorrow":
        base += timedelta(days=1)
    elif keyword == "yesterday":
        base -= timedelta(days=1)

    if time_part:
        return f"{base.isoformat()} {time_part}"
    # remindctl accepts bare keywords for all-day; keep them when no time given.
    if keyword in _RELATIVE_DUE_KEYWORDS and not time_part:
        return keyword
    return base.isoformat()


def _remindctl_status() -> str:
    return _run_remindctl(["status", "--json"])


def _remindctl_lists() -> str:
    return _run_remindctl(["list", "--json"])


def _remindctl_show(
    filter_name: str,
    *,
    list_name: str = "",
    limit: int | None = None,
) -> str:
    args = ["show", filter_name, "--json"]
    if list_name:
        args.extend(["--list", list_name])
    raw = _run_remindctl(args)
    if raw.startswith("Error:") or limit is None:
        return raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return _format_json(data, limit=limit)


def _remindctl_list_reminders(list_name: str, *, limit: int | None = None) -> str:
    raw = _run_remindctl(["list", list_name, "--json"])
    if raw.startswith("Error:") or limit is None:
        return raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return _format_json(data, limit=limit)


def _remindctl_add(
    title: str,
    *,
    list_name: str = "",
    due_date: str = "",
    notes: str = "",
    priority: str = "",
    url: str = "",
    repeat_rule: str = "",
) -> str:
    args = ["add", title, "--json"]
    if list_name:
        args.extend(["--list", list_name])
    if due_date:
        args.extend(["--due", due_date])
    if notes:
        args.extend(["--notes", notes])
    if priority:
        args.extend(["--priority", priority])
    if url:
        args.extend(["--url", url])
    if repeat_rule:
        args.extend(["--repeat", repeat_rule])
    return _run_remindctl(args)


def _remindctl_edit(
    reminder_id: str,
    *,
    title: str = "",
    list_name: str = "",
    due_date: str = "",
    clear_due: bool = False,
    notes: str = "",
    priority: str = "",
    url: str = "",
    clear_url: bool = False,
    repeat_rule: str = "",
    clear_repeat: bool = False,
    complete: bool = False,
    incomplete: bool = False,
) -> str:
    args = ["edit", _normalize_id(reminder_id), "--json"]
    if title:
        args.extend(["--title", title])
    if list_name:
        args.extend(["--list", list_name])
    if due_date:
        args.extend(["--due", due_date])
    if clear_due:
        args.append("--clear-due")
    if notes:
        args.extend(["--notes", notes])
    if priority:
        args.extend(["--priority", priority])
    if url:
        args.extend(["--url", url])
    if clear_url:
        args.append("--clear-url")
    if repeat_rule:
        args.extend(["--repeat", repeat_rule])
    if clear_repeat:
        args.append("--no-repeat")
    if complete:
        args.append("--complete")
    if incomplete:
        args.append("--incomplete")
    return _run_remindctl(args)


def _remindctl_complete(reminder_ids: list[str]) -> str:
    return _run_remindctl(["complete", *reminder_ids, "--json"])


def _remindctl_delete(reminder_ids: list[str], *, force: bool = False) -> str:
    args = ["delete", *reminder_ids, "--json"]
    if force:
        args.append("--force")
    return _run_remindctl(args)


def _osascript_lists() -> str:
    script = 'tell application "Reminders" to get name of every list'
    raw = _run_osascript(script)
    if raw.startswith("Error:"):
        return raw
    names = [part.strip() for part in raw.split(",") if part.strip()]
    payload = [{"title": name} for name in names]
    return _format_json(payload)


def _osascript_show(
    filter_name: str,
    *,
    list_name: str = "",
    limit: int | None = None,
) -> str:
    filter_name = filter_name.lower()
    if filter_name not in _SHOW_FILTERS and not re.match(r"^\d{4}-\d{2}-\d{2}", filter_name):
        return f"Error: unsupported filter '{filter_name}' without remindctl.{_install_hint()}"

    list_clause = ""
    if list_name:
        list_clause = f' in list "{_applescript_quote(list_name)}"'

    if filter_name == "completed":
        status_clause = "whose completed is true"
    elif filter_name in {"open", "all"}:
        status_clause = "whose completed is false" if filter_name == "open" else ""
    else:
        return f"Error: filter '{filter_name}' requires remindctl for date-based queries.{_install_hint()}"

    if status_clause:
        selector = f"(reminders{list_clause} {status_clause})"
    else:
        selector = f"(reminders{list_clause})"

    script = f"""
tell application "Reminders"
    set out to ""
    repeat with R in {selector}
        set out to out & (name of R) & tab & (id of R as text) & linefeed
    end repeat
    return out
end tell
"""
    raw = _run_osascript(script)
    if raw.startswith("Error:"):
        return raw
    if raw == "(no output)":
        return _format_json([])

    reminders: list[dict[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        if "\t" in line:
            title, rid = line.split("\t", 1)
        else:
            title, rid = line, ""
        reminders.append({"title": title.strip(), "id": rid.strip()})

    return _format_json(reminders, limit=limit)


def _osascript_list_reminders(list_name: str, *, limit: int | None = None) -> str:
    return _osascript_show("open", list_name=list_name, limit=limit)


def _osascript_add(
    title: str,
    *,
    list_name: str = "",
    due_date: str = "",
    notes: str = "",
) -> str:
    target = f'list "{_applescript_quote(list_name)}"' if list_name else "default list"
    props = [f'name:"{_applescript_quote(title)}"']
    if notes:
        props.append(f'body:"{_applescript_quote(notes)}"')
    if due_date:
        return f"Error: due dates require remindctl.{_install_hint()}"
    props_text = ", ".join(props)
    script = f'tell application "Reminders" to make new reminder in {target} with properties {{{props_text}}}'
    raw = _run_osascript(script)
    if raw.startswith("Error:"):
        return raw
    return _format_json({"title": title, "listName": list_name or "(default)", "id": raw.strip()})


def _osascript_edit(
    reminder_id: str,
    *,
    title: str = "",
    list_name: str = "",
    notes: str = "",
    complete: bool = False,
    incomplete: bool = False,
) -> str:
    rid = (
        reminder_id
        if reminder_id.startswith("x-apple-reminder://")
        else f"x-apple-reminder://{_normalize_id(reminder_id)}"
    )

    if list_name:
        return f"Error: moving reminders between lists requires remindctl.{_install_hint()}"

    actions: list[str] = []
    if title:
        actions.append(f'set name of reminder id "{_applescript_quote(rid)}" to "{_applescript_quote(title)}"')
    if notes:
        actions.append(f'set body of reminder id "{_applescript_quote(rid)}" to "{_applescript_quote(notes)}"')
    if complete:
        actions.append(f'set completed of reminder id "{_applescript_quote(rid)}" to true')
    if incomplete:
        actions.append(f'set completed of reminder id "{_applescript_quote(rid)}" to false')

    if not actions:
        return "Error: no edit fields provided."

    script = 'tell application "Reminders"\n' + "\n".join(actions) + "\nend tell"
    raw = _run_osascript(script)
    if raw.startswith("Error:"):
        return raw
    return _format_json({"id": rid, "updated": True})


def _osascript_complete(reminder_ids: list[str]) -> str:
    actions = []
    for rid in reminder_ids:
        full = rid if rid.startswith("x-apple-reminder://") else f"x-apple-reminder://{rid}"
        actions.append(f'set completed of reminder id "{_applescript_quote(full)}" to true')
    script = 'tell application "Reminders"\n' + "\n".join(actions) + "\nend tell"
    raw = _run_osascript(script)
    if raw.startswith("Error:"):
        return raw
    return _format_json({"completed": len(reminder_ids)})


def _osascript_delete(reminder_ids: list[str]) -> str:
    actions = []
    for rid in reminder_ids:
        full = rid if rid.startswith("x-apple-reminder://") else f"x-apple-reminder://{rid}"
        actions.append(f'delete reminder id "{_applescript_quote(full)}"')
    script = 'tell application "Reminders"\n' + "\n".join(actions) + "\nend tell"
    raw = _run_osascript(script)
    if raw.startswith("Error:"):
        return raw
    return _format_json({"deleted": len(reminder_ids)})


def _osascript_status() -> str:
    raw = _run_osascript('tell application "Reminders" to get name of every list')
    if raw.startswith("Error:"):
        if "not allowed" in raw.lower() or "access" in raw.lower():
            return _format_json({"authorized": False, "status": "denied"})
        return raw
    return _format_json({"authorized": True, "status": "full-access", "backend": "osascript"})


@tool(
    hint=(
        "Use reminders_status to check Reminders permission before creating or "
        "deleting reminders. If access is denied, direct the user to "
        "System Settings > Privacy & Security > Reminders."
    )
)
def reminders_status() -> str:
    """Check Apple Reminders authorization status."""
    if err := _require_macos():
        return err
    if _has_remindctl():
        return _remindctl_status()
    return _osascript_status()


@tool(
    hint=(
        "Use reminders_lists to list reminder list names, or pass list_name to browse "
        "open items in one list. To find a NAMED reminder use reminders_find — NEVER "
        "pass limit=1 on reminders_lists (it hides the item you need)."
    )
)
def reminders_lists(
    list_name: str = "",
    limit: int = 50,
    title_query: str = "",
) -> str:
    """List reminder lists or open reminders in a specific list."""
    if err := _require_macos():
        return err
    if title_query.strip():
        return reminders_find(
            title_query.strip(),
            list_name=list_name,
            include_completed=False,
            limit=limit or 30,
        )
    if list_name:
        if reject := _reject_browse_limit(
            limit, title_query=title_query, tool_name="reminders_lists", list_name=list_name
        ):
            return reject
        if _has_remindctl():
            raw = _remindctl_list_reminders(list_name)
        else:
            raw = _osascript_list_reminders(list_name, limit=None)
        parsed = _parse_reminders_json(raw)
        if isinstance(parsed, str):
            return parsed
        return _format_json(parsed, limit=limit or None)
    if _has_remindctl():
        return _remindctl_lists()
    return _osascript_lists()


@tool(
    hint=(
        "Use reminders_find when searching for a reminder BY NAME (e.g. 'Buy milk'). "
        "Returns small JSON with ids for reminders_edit. NEVER call reminders_add after "
        "a search unless the user explicitly asked to create a new reminder."
    )
)
def reminders_find(
    title_query: str,
    list_name: str = "",
    include_completed: bool = True,
    limit: int = 30,
) -> str:
    """Search reminders by title (case-insensitive substring)."""
    if err := _require_macos():
        return err
    query = title_query.strip()
    if not query:
        return "Error: title_query is required."

    reminders = _load_reminders_for_search(
        list_name=list_name,
        include_completed=include_completed,
    )
    if isinstance(reminders, str):
        return reminders

    matches = [r for r in reminders if _match_title_query(str(r.get("title", "")), query)]
    if not matches:
        scope = f' in list "{list_name}"' if list_name else " across all lists"
        completed = " (including completed)" if include_completed else ""
        return f'No reminders matching "{query}"{scope}{completed}.'
    return _format_json(matches, limit=limit or None)


@tool(
    hint=(
        "Use reminders_show for DATE filters (today, tomorrow, overdue, open, …). "
        "For a named reminder use reminders_find — not filter_name=all with limit=1. "
        "When the user said 'tomorrow', use filter_name=tomorrow not all."
    )
)
def reminders_show(
    filter_name: str = "today",
    list_name: str = "",
    limit: int = 50,
    title_query: str = "",
) -> str:
    """Show reminders matching a filter."""
    if err := _require_macos():
        return err
    if title_query.strip():
        return reminders_find(
            title_query.strip(),
            list_name=list_name,
            include_completed=filter_name == "completed",
            limit=limit or 30,
        )
    if reject := _reject_browse_limit(limit, title_query=title_query, tool_name="reminders_show", list_name=list_name):
        return reject
    if _has_remindctl():
        args = ["show", filter_name, "--json"]
        if list_name:
            args.extend(["--list", list_name])
        raw = _run_remindctl(args)
    else:
        raw = _osascript_show(filter_name, list_name=list_name, limit=None)
    parsed = _parse_reminders_json(raw)
    if isinstance(parsed, str):
        return parsed
    return _format_json(parsed, limit=limit or None)


@tool(
    hint=(
        "Use reminders_add ONLY to create a NEW reminder. NEVER use for updates — "
        "use reminders_edit after reminders_find/reminders_show. For 'in 30 minutes' "
        "or 'half an hour' use due_in_minutes=30 (NOT due_date='15:30' — that is a "
        "common mistake). due_date also accepts 'in 30 minutes'. For calendar dates "
        "use tomorrow or today 09:00."
    )
)
def reminders_add(
    title: str,
    list_name: str = "",
    due_date: str = "",
    due_in_minutes: int = 0,
    notes: str = "",
    priority: str = "",
    url: str = "",
    repeat_rule: str = "",
) -> str:
    """Create a new Apple Reminder."""
    if err := _require_macos():
        return err
    if priority and priority not in _PRIORITY_VALUES:
        return f"Error: priority must be one of: {', '.join(sorted(_PRIORITY_VALUES))}"
    base_repeat = repeat_rule.split()[0] if repeat_rule else ""
    if repeat_rule and base_repeat not in _REPEAT_VALUES and not repeat_rule.startswith("every "):
        return "Error: repeat_rule must be daily/weekly/biweekly/monthly/yearly or 'every N days/weeks/months/years'."
    if due_in_minutes and due_date:
        return "Error: pass due_date or due_in_minutes, not both."
    if due_err := _validate_due_date(due_date):
        return due_err
    due_date = _resolve_due_fields(due_date, due_in_minutes)
    if _has_remindctl():
        return _remindctl_add(
            title,
            list_name=list_name,
            due_date=due_date,
            notes=notes,
            priority=priority,
            url=url,
            repeat_rule=repeat_rule,
        )
    if priority or url or repeat_rule:
        return f"Error: priority/url/repeat require remindctl.{_install_hint()}"
    return _osascript_add(title, list_name=list_name, due_date=due_date, notes=notes)


@tool(
    hint=(
        "Use reminders_edit to update an EXISTING reminder. Pass id from "
        "reminders_find/reminders_show, an ID prefix, or exact title (resolved "
        "automatically). To move between lists, pass list_name. Never recreate with "
        "reminders_add when editing."
    )
)
def reminders_edit(
    reminder_id: str,
    title: str = "",
    list_name: str = "",
    due_date: str = "",
    due_in_minutes: int = 0,
    clear_due: bool = False,
    notes: str = "",
    priority: str = "",
    url: str = "",
    clear_url: bool = False,
    repeat_rule: str = "",
    clear_repeat: bool = False,
    complete: bool = False,
    incomplete: bool = False,
) -> str:
    """Edit an existing reminder."""
    if err := _require_macos():
        return err
    if clear_due and (due_date or due_in_minutes):
        return "Error: pass clear_due alone, not with due_date/due_in_minutes."
    if due_in_minutes and due_date:
        return "Error: pass due_date or due_in_minutes, not both."
    if clear_url and url:
        return "Error: pass url or clear_url, not both."
    if clear_repeat and repeat_rule:
        return "Error: pass repeat_rule or clear_repeat, not both."
    if complete and incomplete:
        return "Error: pass complete or incomplete, not both."
    if priority and priority not in _PRIORITY_VALUES:
        return f"Error: priority must be one of: {', '.join(sorted(_PRIORITY_VALUES))}"
    if due_date or due_in_minutes:
        if due_err := _validate_due_date(due_date):
            return due_err
        due_date = _resolve_due_fields(due_date, due_in_minutes)

    lookup_scope = list_name if not _is_reminder_id(reminder_id) else ""
    resolved, resolve_err = _resolve_reminder_ids([reminder_id], list_name=lookup_scope)
    if resolve_err:
        return resolve_err
    reminder_id = resolved[0]

    if _has_remindctl():
        return _remindctl_edit(
            reminder_id,
            title=title,
            list_name=list_name,
            due_date=due_date,
            clear_due=clear_due,
            notes=notes,
            priority=priority,
            url=url,
            clear_url=clear_url,
            repeat_rule=repeat_rule,
            clear_repeat=clear_repeat,
            complete=complete,
            incomplete=incomplete,
        )

    advanced = any([list_name, due_date, clear_due, priority, url, clear_url, repeat_rule, clear_repeat])
    if advanced:
        return f"Error: those edit fields require remindctl.{_install_hint()}"
    return _osascript_edit(
        reminder_id,
        title=title,
        notes=notes,
        complete=complete,
        incomplete=incomplete,
    )


@tool(
    hint=(
        "Use reminders_complete to mark reminders done. Pass comma-separated IDs "
        "from reminders_show output, or exact titles (resolved automatically)."
    )
)
def reminders_complete(reminder_ids: str, list_name: str = "") -> str:
    """Mark reminders as completed."""
    if err := _require_macos():
        return err
    ids = _split_ids(reminder_ids)
    if not ids:
        return "Error: reminder_ids is required."
    if _has_remindctl():
        resolved, resolve_err = _resolve_reminder_ids(ids, list_name=list_name)
        if resolve_err:
            return resolve_err
        return _remindctl_complete(resolved)
    if any(not _is_reminder_id(i) for i in ids):
        return f"Error: title lookup requires remindctl.{_install_hint()}"
    return _osascript_complete([_normalize_id(i) for i in ids])


@tool(
    confirm="Delete reminder(s) {reminder_ids}? This cannot be undone.",
    hint=(
        "Use reminders_delete to remove reminders. Pass IDs from reminders_show, "
        "or exact titles (resolved automatically). Requires confirmation."
    ),
)
def reminders_delete(reminder_ids: str, list_name: str = "") -> str:
    """Delete one or more reminders."""
    if err := _require_macos():
        return err
    ids = _split_ids(reminder_ids)
    if not ids:
        return "Error: reminder_ids is required."
    if _has_remindctl():
        resolved, resolve_err = _resolve_reminder_ids(ids, list_name=list_name)
        if resolve_err:
            return resolve_err
        return _remindctl_delete(resolved, force=True)
    if any(not _is_reminder_id(i) for i in ids):
        return f"Error: title lookup requires remindctl.{_install_hint()}"
    return _osascript_delete([_normalize_id(i) for i in ids])


def register_reminders_tools(registry: ToolRegistry) -> int:
    """Register Apple Reminders tools."""
    backend = _backend() if _is_macos() else "unavailable"
    hint_extra = (
        f" Backend: {backend}."
        + (
            " Install remindctl for full feature support: brew install steipete/tap/remindctl."
            if backend == "osascript"
            else ""
        )
        + (" Runs on the Mac local worker in split deploy." if not _is_macos() else "")
    )

    now = _now_local()
    today = now.date().isoformat()
    registry.register_category_hint(
        "Reminders",
        f"Apple Reminders on macOS — Mac local time now is {now.strftime('%Y-%m-%d %H:%M %Z')} "
        f"(today is {today}). You HAVE access via these tools. "
        "For any user question about reminders/todos/due tasks, call reminders_show, "
        "reminders_find, or reminders_lists before answering. "
        "For 'in 30 minutes' / 'half an hour' use due_in_minutes=30 — NEVER due_date='15:30'. "
        "When creating calendar due dates pass tomorrow/today — never invent past ISO dates. "
        "Never say you lack reminder or calendar access. "
        "Changes sync via iCloud. Check reminders_status before mutating data." + hint_extra,
    )

    tools = [
        reminders_status,
        reminders_lists,
        reminders_show,
        reminders_find,
        reminders_add,
        reminders_edit,
        reminders_complete,
        reminders_delete,
    ]
    for func in tools:
        registry.register(func, category="Reminders")
    return len(tools)
