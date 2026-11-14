"""Pinned argv actions.

The model picks a name from ``tools.actions.allow`` and supplies slot values.
The worker fills the argv for this OS and runs it with ``shell=False``.
A missing config list uses the built-in actions (file-size, file-bytes,
git-status, git-log). Setting ``allow`` replaces that list.
"""

from __future__ import annotations

import json
import platform
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from chalkbox.logging.bridge import get_logger

from agentforge.config import get_config

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)

_SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_DEFAULT_TIMEOUT = 30
_MAX_OUTPUT = 100_000

_DEFAULT_ALLOW: list[dict[str, Any]] = [
    {
        "name": "file-size",
        "darwin": ["stat", "-f", "%z", "{path}"],
        "linux": ["stat", "-c", "%s", "{path}"],
    },
    {"name": "file-bytes", "argv": ["wc", "-c", "{path}"]},
    {"name": "git-status", "argv": ["git", "-C", "{path}", "status", "--short"]},
    {
        "name": "git-log",
        "argv": ["git", "-C", "{path}", "log", "-n", "{n}", "--oneline"],
        "slots": {"n": {"type": "int", "min": 1, "max": 50, "default": 20}},
    },
]

_ACTION_KEYS = frozenset({"name", "argv", "darwin", "linux", "slots"})
_SLOT_KEYS = frozenset({"type", "min", "max", "default"})


@dataclass(frozen=True)
class SlotSpec:
    type: str
    default: str | int | None = None
    min: int | None = None
    max: int | None = None

    @property
    def required(self) -> bool:
        return self.default is None


@dataclass(frozen=True)
class ActionSpec:
    name: str
    argv: tuple[str, ...]
    darwin: tuple[str, ...]
    linux: tuple[str, ...]
    slots: dict[str, SlotSpec]


def _as_argv(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    argv: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, str | int):
            raise ValueError(f"{label} entries must be strings")
        text = str(item)
        if "\x00" in text:
            raise ValueError(f"{label} contains a NUL byte")
        argv.append(text)
    return tuple(argv)


def _slot_int(value: object, message: str) -> int | None:
    # bool is an int; true/false is not a slot bound.
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(message)
    return value


def _parse_slot(action_name: str, slot_name: str, raw: object) -> SlotSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"{action_name}.{slot_name} must be a mapping")
    unknown = set(raw) - _SLOT_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ValueError(f"{action_name}.{slot_name} has unknown keys: {names}")
    slot_type = str(raw.get("type") or "str")
    if slot_type not in ("str", "int"):
        raise ValueError(f"{action_name}.{slot_name} type must be str or int")
    default = raw.get("default")
    minimum = raw.get("min")
    maximum = raw.get("max")
    default_value: str | int | None
    minimum_value: int | None
    maximum_value: int | None
    if slot_type == "int":
        default_value = _slot_int(default, f"{action_name}.{slot_name} default must be an int")
        minimum_value = _slot_int(minimum, f"{action_name}.{slot_name} min must be an int")
        maximum_value = _slot_int(maximum, f"{action_name}.{slot_name} max must be an int")
    else:
        if default is not None and not isinstance(default, str):
            raise ValueError(f"{action_name}.{slot_name} default must be a string")
        if minimum is not None:
            raise ValueError(f"{action_name}.{slot_name} min applies to int slots")
        if maximum is not None:
            raise ValueError(f"{action_name}.{slot_name} max applies to int slots")
        default_value = default if isinstance(default, str) else None
        minimum_value = None
        maximum_value = None
    return SlotSpec(type=slot_type, default=default_value, min=minimum_value, max=maximum_value)


def _placeholders(templates: list[tuple[str, ...]]) -> set[str]:
    found: set[str] = set()
    for template in templates:
        for token in template:
            found.update(_SLOT_RE.findall(token))
    return found


def parse_allow(allow: object) -> dict[str, ActionSpec]:
    """Parse ``tools.actions.allow`` into action specs.

    Raises ``ValueError`` on a malformed entry. Placeholders with no ``slots``
    entry become required strings.
    """
    if not isinstance(allow, list):
        raise ValueError("tools.actions.allow must be a list")
    catalog: dict[str, ActionSpec] = {}
    for entry in allow:
        if not isinstance(entry, dict):
            raise ValueError("each action must be a mapping")
        raw = cast(dict[str, Any], entry)
        unknown = set(raw) - _ACTION_KEYS
        if unknown:
            names = ", ".join(sorted(str(key) for key in unknown))
            raise ValueError(f"action has unknown keys: {names}")
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"invalid action name: {name!r}")
        if name in catalog:
            raise ValueError(f"duplicate action name: {name}")
        argv = _as_argv(raw["argv"], f"{name}.argv") if "argv" in raw else ()
        darwin = _as_argv(raw["darwin"], f"{name}.darwin") if "darwin" in raw else ()
        linux = _as_argv(raw["linux"], f"{name}.linux") if "linux" in raw else ()
        if not argv and not darwin and not linux:
            raise ValueError(f"{name} needs argv, darwin, or linux")
        declared: dict[str, SlotSpec] = {}
        raw_slots = raw.get("slots") or {}
        if not isinstance(raw_slots, dict):
            raise ValueError(f"{name}.slots must be a mapping")
        for slot_name, raw_slot in raw_slots.items():
            if not isinstance(slot_name, str) or not slot_name.isidentifier():
                raise ValueError(f"{name} has an invalid slot name: {slot_name!r}")
            declared[slot_name] = _parse_slot(name, slot_name, raw_slot)
        for slot_name in _placeholders([argv, darwin, linux]):
            declared.setdefault(slot_name, SlotSpec(type="str"))
        catalog[name] = ActionSpec(name=name, argv=argv, darwin=darwin, linux=linux, slots=declared)
    return catalog


def load_action_catalog() -> tuple[dict[str, ActionSpec], str | None]:
    """Return the configured catalog, or the built-in list when unset."""
    try:
        tools_cfg = get_config()._raw.get("tools") or {}
    except Exception as exc:
        logger.warning("action config unavailable, using built-in actions: %s", exc)
        return parse_allow(_DEFAULT_ALLOW), None
    if not isinstance(tools_cfg, dict) or "actions" not in tools_cfg:
        return parse_allow(_DEFAULT_ALLOW), None
    actions_cfg = tools_cfg.get("actions") or {}
    if not isinstance(actions_cfg, dict):
        return {}, "Error: tools.actions must be a mapping"
    if "allow" not in actions_cfg:
        return parse_allow(_DEFAULT_ALLOW), None
    try:
        return parse_allow(actions_cfg.get("allow")), None
    except ValueError as exc:
        return {}, f"Error: {exc}"


def action_timeout() -> int:
    try:
        tools_cfg = get_config()._raw.get("tools") or {}
        raw = (tools_cfg.get("actions") or {}).get("timeout", _DEFAULT_TIMEOUT)
        timeout = int(raw)
    except Exception:
        return _DEFAULT_TIMEOUT
    return timeout if timeout > 0 else _DEFAULT_TIMEOUT


def template_for(spec: ActionSpec, system: str) -> tuple[tuple[str, ...], str | None]:
    """Pick the argv template for this OS. A platform list wins over ``argv``."""
    if system == "Darwin" and spec.darwin:
        return spec.darwin, None
    if system == "Linux" and spec.linux:
        return spec.linux, None
    if spec.argv:
        return spec.argv, None
    if system == "Darwin" and spec.linux and not spec.darwin:
        return (), f"Error: action '{spec.name}' has no darwin argv"
    if system == "Linux" and spec.darwin and not spec.linux:
        return (), f"Error: action '{spec.name}' has no linux argv"
    return (), f"Error: action '{spec.name}' has no argv for {system}"


def _coerce_slot(spec: SlotSpec, value: object) -> tuple[str | None, str | None]:
    if spec.type == "int":
        if isinstance(value, bool) or isinstance(value, float):
            return None, "must be an int"
        if isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError:
                return None, "must be an int"
        if not isinstance(value, int):
            return None, "must be an int"
        if spec.min is not None and value < spec.min:
            return None, f"must be >= {spec.min}"
        if spec.max is not None and value > spec.max:
            return None, f"must be <= {spec.max}"
        return str(value), None
    if not isinstance(value, str):
        return None, "must be a string"
    if value == "" or "\x00" in value:
        return None, "must be a non-empty string"
    return value, None


def bind_slots(spec: ActionSpec, args: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    unknown = sorted(set(args) - set(spec.slots))
    if unknown:
        return {}, f"Error: action '{spec.name}' has no slot {unknown[0]}"
    values: dict[str, str] = {}
    for slot_name, slot in spec.slots.items():
        if slot_name not in args:
            if slot.required:
                return {}, f"Error: action '{spec.name}' needs slot '{slot_name}'"
            if slot.default is not None:
                rendered, err = _coerce_slot(slot, slot.default)
                if err or rendered is None:
                    return {}, f"Error: action '{spec.name}' slot '{slot_name}' {err}"
                values[slot_name] = rendered
            continue
        rendered, err = _coerce_slot(slot, args[slot_name])
        if err or rendered is None:
            return {}, f"Error: action '{spec.name}' slot '{slot_name}' {err}"
        values[slot_name] = rendered
    return values, None


def fill_argv(template: tuple[str, ...], values: dict[str, str]) -> list[str]:
    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(key)
        return values[key]

    return [_SLOT_RE.sub(_repl, token) for token in template]


def _parse_args(args: object) -> tuple[dict[str, Any] | None, str | None]:
    if args is None or args == "":
        return {}, None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None, "Error: args must be a JSON object"
    if not isinstance(args, dict):
        return None, "Error: args must be an object"
    return cast(dict[str, Any], args), None


def _catalog_doc(catalog: dict[str, ActionSpec]) -> str:
    if not catalog:
        summary = "none configured"
    else:
        parts: list[str] = []
        for spec in catalog.values():
            slot_bits: list[str] = []
            for slot_name, slot in spec.slots.items():
                if slot.default is not None:
                    slot_bits.append(f"{slot_name} default {slot.default}")
                else:
                    slot_bits.append(slot_name)
            parts.append(f"{spec.name} ({', '.join(slot_bits)})" if slot_bits else spec.name)
        summary = "; ".join(parts)
    return "\n".join(
        [
            "Run one pinned argv action. Slot values go in args.",
            "",
            f"name: one of {summary}",
            'args: slot object, for example {"path": "/etc/hosts"}',
            "cwd: working directory. Empty keeps the worker default.",
        ]
    )


def refresh_action_doc(catalog: dict[str, ActionSpec] | None = None) -> None:
    if catalog is None:
        catalog, _err = load_action_catalog()
    action.__doc__ = _catalog_doc(catalog)


def execute_action(
    name: str,
    args: object = None,
    *,
    cwd: str = "",
    catalog: dict[str, ActionSpec],
    system: str,
    run: Any,
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """Resolve *name* against *catalog* and run the filled argv via *run*."""
    spec = catalog.get(name)
    if spec is None:
        known = ", ".join(catalog) or "(none)"
        return f"Error: unknown action '{name}'. Known: {known}"
    parsed, err = _parse_args(args)
    if err or parsed is None:
        return err or "Error: args must be an object"
    values, err = bind_slots(spec, parsed)
    if err or values is None:
        return err or "Error: bad slots"
    template, err = template_for(spec, system)
    if err:
        return err
    try:
        argv = fill_argv(template, values)
    except KeyError as exc:
        return f"Error: action '{name}' is missing slot {exc.args[0]}"
    work_dir = cwd or None
    if work_dir:
        work_dir = str(Path(work_dir).expanduser())
        if not Path(work_dir).is_dir():
            return f"Error: cwd is not a directory: {work_dir}"
    shown = shlex.join(argv)
    try:
        completed = run(argv, work_dir, timeout)
    except subprocess.TimeoutExpired:
        return f"Error: action '{name}' timed out after {timeout}s — `{shown}`"
    except OSError as exc:
        return f"Error: {exc}"
    stdout = (getattr(completed, "stdout", None) or "").strip()
    stderr = (getattr(completed, "stderr", None) or "").strip()
    output = stdout
    if stderr:
        output = f"{output}\n{stderr}" if output else stderr
    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + "\n… (truncated)"
    code = int(getattr(completed, "returncode", 0) or 0)
    if code != 0:
        detail = output or "(no output)"
        return f"[exit {code}] $ {shown}\n\n{detail}"
    return f"$ {shown}\n\n{output}" if output else f"$ {shown}\n\n(completed successfully, no output)"


def _run_argv(argv: list[str], cwd: str | None, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )


@tool
def action(name: str, args: dict | None = None, cwd: str = "") -> str:
    """Run one pinned argv action. Slot values go in args.

    name: action id from tools.actions.allow
    args: slot object, for example {"path": "/etc/hosts"}
    cwd: working directory. Empty keeps the worker default.
    """
    catalog, err = load_action_catalog()
    refresh_action_doc(catalog)
    if err:
        return err
    return execute_action(
        name,
        args,
        cwd=cwd,
        catalog=catalog,
        system=platform.system(),
        run=_run_argv,
        timeout=action_timeout(),
    )


def register_action_tools(registry: ToolRegistry) -> int:
    """Register the ``action`` tool. Returns the number of tools registered."""
    refresh_action_doc()
    registry.register_category_hint(
        "Actions",
        "Pinned argv actions. Pass the action name and put slot values in args.",
    )
    registry.register(action, category="Actions")
    return 1
