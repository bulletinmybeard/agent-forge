"""Request body for Ollama ``POST /v1/systemone`` (Clef and other decision models)."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_QUESTION_TYPES = frozenset({"choice", "noul", "score"})
_QUESTION_KEYS = frozenset({"type", "instructions", "criteria"})
MAX_QUESTIONS = 64
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 8 * 1024 * 1024
MIN_CHOICE_OPTIONS = 2
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10


class SystemOneError(ValueError):
    """The body is not something the decision endpoint will accept."""


def build_systemone_body(
    model: str,
    state: str | dict | list,
    questions: str | dict,
    images: list[str] | None = None,
) -> dict[str, Any]:
    """Build the JSON body for ``/v1/systemone``.

    ``state`` and ``questions`` may already be decoded, or they may be JSON
    text. A state string that does not parse as a JSON object or array is
    sent as text.
    """
    if not model or not str(model).strip():
        raise SystemOneError("systemone needs a model")
    body: dict[str, Any] = {
        "model": str(model),
        "state": _parse_state(state),
        "questions": _parse_questions(questions),
    }
    if images:
        body["images"] = _check_images(images)
    return body


def _parse_state(state: str | dict | list) -> str | dict | list:
    if isinstance(state, (dict, list)):
        if not state:
            raise SystemOneError("state is empty")
        return state
    if not isinstance(state, str) or not state.strip():
        raise SystemOneError("state is empty")
    text = state.strip()
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemOneError(f"state looks like JSON but did not parse: {exc}") from exc
        if isinstance(parsed, (dict, list)):
            if not parsed:
                raise SystemOneError("state is empty")
            return parsed
    return state


def _parse_questions(questions: str | dict) -> dict[str, Any]:
    if isinstance(questions, str):
        text = questions.strip()
        if not text:
            raise SystemOneError("questions is empty")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemOneError(f"questions must be a JSON object: {exc}") from exc
    else:
        parsed = questions
    if not isinstance(parsed, dict) or not parsed:
        raise SystemOneError("questions must be a JSON object with 1 to 64 questions")
    if len(parsed) > MAX_QUESTIONS:
        raise SystemOneError(f"questions has {len(parsed)} entries; the limit is {MAX_QUESTIONS}")
    out: dict[str, Any] = {}
    for name, spec in parsed.items():
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise SystemOneError(f"question name {name!r} must be 1-100 chars of letters, digits, '_', '.', '-'")
        out[name] = _check_question(name, spec)
    return out


def _check_question(name: str, spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise SystemOneError(f"question {name!r} must be an object")
    extra = set(spec) - _QUESTION_KEYS
    if extra:
        raise SystemOneError(f"question {name!r} has unknown keys: {sorted(extra)}")
    qtype = spec.get("type")
    if qtype not in _QUESTION_TYPES:
        raise SystemOneError(f"question {name!r} type must be choice, noul, or score")
    instructions = spec.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise SystemOneError(f"question {name!r} needs instructions")
    cleaned: dict[str, Any] = {"type": qtype, "instructions": instructions}
    if "criteria" in spec and spec["criteria"] is not None:
        cleaned["criteria"] = _check_criteria(name, qtype, spec["criteria"])
    elif qtype in {"choice", "score"}:
        raise SystemOneError(f"question {name!r} {qtype} needs criteria")
    return cleaned


def _check_criteria(name: str, qtype: str, criteria: Any) -> Any:
    if qtype == "choice":
        if not isinstance(criteria, dict) or len(criteria) < MIN_CHOICE_OPTIONS:
            raise SystemOneError(f"question {name!r} choice needs at least {MIN_CHOICE_OPTIONS} options")
        if len(criteria) > MAX_CHOICE_OPTIONS:
            raise SystemOneError(
                f"question {name!r} choice has {len(criteria)} options; the cloud limit is {MAX_CHOICE_OPTIONS}"
            )
        for key, value in criteria.items():
            if not isinstance(key, str) or not key:
                raise SystemOneError(f"question {name!r} has an empty choice option")
            if value is not None and not isinstance(value, str):
                raise SystemOneError(f"question {name!r} option {key!r} must be a string or null")
        return criteria
    if qtype == "noul":
        if not isinstance(criteria, dict):
            raise SystemOneError(f"question {name!r} noul criteria must be an object")
        return criteria
    if not isinstance(criteria, list) or not MIN_SCORE_LEVELS <= len(criteria) <= MAX_SCORE_LEVELS:
        raise SystemOneError(
            f"question {name!r} score needs {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels, lowest first"
        )
    if not all(isinstance(item, str) and item.strip() for item in criteria):
        raise SystemOneError(f"question {name!r} score levels must be strings")
    return criteria


def _check_images(images: list[str]) -> list[str]:
    if len(images) > MAX_IMAGES:
        raise SystemOneError(f"{len(images)} images; the limit is {MAX_IMAGES}")
    total = 0
    checked: list[str] = []
    for raw in images:
        if not isinstance(raw, str) or not raw.strip():
            raise SystemOneError("image entries must be base64 strings")
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise SystemOneError("image is not valid base64") from exc
        size = len(decoded)
        if size > MAX_IMAGE_BYTES:
            raise SystemOneError("an image is over 4 MiB")
        total += size
        if total > MAX_IMAGE_TOTAL_BYTES:
            raise SystemOneError("images are over 8 MiB combined")
        checked.append(raw)
    return checked
