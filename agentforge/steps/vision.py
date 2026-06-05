"""VisionStep — analyse images using a vision-capable model."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from chalkbox.logging.bridge import get_logger

from ..attachments import Attachment
from ..client import AIClient
from ..context import PipelineContext
from .base import BaseStep

logger = get_logger(__name__)

DEFAULT_VISION_PROMPT = "Describe what you see in the image(s) in detail."


class VisionStep(BaseStep):
    """Send images to a vision-capable model and store the description.

    The step uses its own :class:`AIClient` instance — typically configured
    with a vision profile (e.g., ``llava``, ``gemma3``, ``moondream``).

    Images can come from three sources (checked in order):

    1. ``images`` passed directly to the constructor.
    2. ``ctx.attachments`` that are detected as images.
    3. ``ctx.metadata["images"]`` — a list of paths or :class:`Attachment` objects
       added by a prior step.

    If no images are found, the step is skipped.

    The model's response is stored in ``ctx.result`` and also in
    ``ctx.metadata["vision_description"]`` so downstream steps can access
    both the text result and know it came from vision analysis.
    """

    def __init__(
        self,
        client: AIClient,
        *,
        prompt: str = DEFAULT_VISION_PROMPT,
        images: list[str | Path | Attachment] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        name: str = "VisionStep",
        condition: Callable[[PipelineContext], bool] | None = None,
    ) -> None:
        super().__init__(name=name, condition=condition)
        self._client = client
        self._prompt = prompt
        self._fixed_images = self._normalise_images(images) if images else []
        self._system_prompt = system_prompt
        self._temperature = temperature

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _normalise_images(
        items: list[str | Path | Attachment],
    ) -> list[Attachment]:
        """Convert a mixed list of paths / Attachments into Attachment objects."""
        result: list[Attachment] = []
        for item in items:
            if isinstance(item, Attachment):
                result.append(item)
            else:
                result.append(Attachment(item))
        return result

    def _collect_images(self, ctx: PipelineContext) -> list[Attachment]:
        """Gather images from all sources."""
        images: list[Attachment] = list(self._fixed_images)

        # From context attachments
        for att in ctx.attachments:
            if att.is_image and att not in images:
                images.append(att)

        # From metadata (a prior step may have put image paths here)
        meta_images = ctx.metadata.get("images")
        if meta_images:
            for item in meta_images:
                att = item if isinstance(item, Attachment) else Attachment(item)
                if att.is_image and att not in images:
                    images.append(att)

        return images

    # -- process ------------------------------------------------------------

    def process(self, ctx: PipelineContext) -> PipelineContext:
        images = self._collect_images(ctx)

        if not images:
            logger.info("VisionStep: no images found — skipping")
            return ctx

        logger.info(
            "VisionStep: analysing %d image(s): %s",
            len(images),
            [img.name for img in images],
        )

        # Use the query as the prompt if it looks like a question about the image,
        # otherwise fall back to the configured prompt
        prompt = ctx.query if ctx.query else self._prompt

        # Build messages
        messages: list[dict[str, Any]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = self._client.chat(
            messages,
            attachments=images,
            temperature=self._temperature,
        )

        ctx.result = response.content
        ctx.add_assistant_message(response.content)
        ctx.metadata["vision_description"] = response.content

        if response.thinking:
            ctx.thinking = response.thinking

        logger.info("VisionStep produced %d chars", len(ctx.result))
        return ctx
