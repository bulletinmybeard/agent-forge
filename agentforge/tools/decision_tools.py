"""Clef decision tool — Ollama ``POST /v1/systemone``."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger
from ollama import ResponseError

from agentforge.backends.systemone import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_TOTAL_BYTES,
    MAX_IMAGES,
    SystemOneError,
)

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)

_PROFILE = "ollama-clef"


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _load_images(raw: str | list | None) -> list[str] | None:
    """Read local image files and return base64, or None when none were given."""
    if raw is None:
        return None
    if isinstance(raw, list):
        parsed: object = raw
    else:
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemOneError(f"image_paths must be a JSON list of file paths: {exc}") from exc
    if not isinstance(parsed, list):
        raise SystemOneError("image_paths must be a JSON list of file paths")
    if not parsed:
        return None
    if len(parsed) > MAX_IMAGES:
        raise SystemOneError(f"{len(parsed)} images; the limit is {MAX_IMAGES}")
    images: list[str] = []
    total = 0
    for item in parsed:
        if not isinstance(item, str) or not item.strip():
            raise SystemOneError("image_paths entries must be file paths")
        path = Path(item).expanduser()
        if not path.is_file():
            raise SystemOneError(f"image not found: {item}")
        size = path.stat().st_size
        if size == 0:
            raise SystemOneError(f"image is empty: {item}")
        if size > MAX_IMAGE_BYTES:
            raise SystemOneError(f"{item} is over 4 MiB")
        total += size
        if total > MAX_IMAGE_TOTAL_BYTES:
            raise SystemOneError("images are over 8 MiB combined")
        images.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    return images


@tool(
    hint=(
        "Clef decision pass (Ollama /v1/systemone). Returns probabilities for "
        "choice, noul (yes/no), or score questions. Not a chat completion."
    ),
)
def decide(state: str, questions: str, image_paths: str = "") -> str:
    """Score state with Clef through Ollama /v1/systemone. Returns probabilities, not prose.

    state: Text, or a JSON object or array, to judge.
    questions: JSON object, 1-64 questions. Keys are names. Each value has type (choice, noul, score), instructions, and criteria. choice criteria is {option: description}. noul criteria is optional {true, false}. score criteria is 2-10 levels, lowest first.
    image_paths: Optional JSON list of image file paths. Up to 4 images, 4 MiB each, 8 MiB total.
    """
    try:
        from agentforge.client import AIClient

        images = _load_images(image_paths)
        client = AIClient(profile=_PROFILE)
        return _dumps(client.systemone(state, questions, images))
    except SystemOneError as exc:
        return _dumps({"error": str(exc)})
    except ResponseError as exc:
        return _dumps({"error": str(exc), "status": exc.status_code})
    except Exception as exc:
        logger.warning("decide failed: %s", exc)
        return _dumps({"error": str(exc)})


def register_decision_tools(registry: ToolRegistry) -> int:
    """Register the Clef decision tool. It errors until Ollama can serve clef."""
    registry.register_category_hint(
        "Decision",
        "decide scores a state with Clef. It returns probabilities, not prose.",
    )
    registry.register(decide, category="Decision")
    logger.debug("Registered decision tool: %s", decide.__name__)
    return 1
