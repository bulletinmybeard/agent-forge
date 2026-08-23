"""Fence-aware extraction of inline ``<think>`` CoT blocks.

Models with ``parse_thinking`` often wrap real chain-of-thought in
``<think>...</think>``.  Naive ``re.sub`` of those tags also destroys
*quoted* examples of the same tags inside fenced code or inline code
(e.g., a docstring explaining ``re.sub(r"<think>…</think>", …)``), which
is exactly how final answers about this backend get corrupted.

Protect fenced + inline code first, strip CoT only from the remaining
prose, then restore the protected spans.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\1).|\n)*?)\1")
_THINK_RE = re.compile(r"<think>(.*?)</think>\s*", re.DOTALL)


def strip_inline_think(content: str) -> tuple[str, str | None]:
    """Remove CoT ``<think>`` blocks outside code; return ``(content, thinking)``.

    *thinking* is the joined non-empty CoT bodies, or ``None`` if none.
    Code fences and inline ``...`` spans are left untouched so quoted tags
    in source examples survive.
    """
    if not content or "<think>" not in content:
        return content, None

    fences: list[str] = []

    def _save_fence(m: re.Match[str]) -> str:
        fences.append(m.group(0))
        return f"\0FENCE{len(fences) - 1}\0"

    protected = _FENCE_RE.sub(_save_fence, content)

    inlines: list[str] = []

    def _save_inline(m: re.Match[str]) -> str:
        inlines.append(m.group(0))
        return f"\0INLINE{len(inlines) - 1}\0"

    protected = _INLINE_CODE_RE.sub(_save_inline, protected)

    parts: list[str] = []

    def _extract(m: re.Match[str]) -> str:
        body = (m.group(1) or "").strip()
        if body:
            parts.append(body)
        return ""

    stripped = _THINK_RE.sub(_extract, protected)

    def _restore_inline(m: re.Match[str]) -> str:
        return inlines[int(m.group(1))]

    def _restore_fence(m: re.Match[str]) -> str:
        return fences[int(m.group(1))]

    restored = re.sub(r"\0INLINE(\d+)\0", _restore_inline, stripped)
    restored = re.sub(r"\0FENCE(\d+)\0", _restore_fence, restored)
    restored = restored.strip()

    thinking = "\n".join(parts) if parts else None
    return restored, thinking
