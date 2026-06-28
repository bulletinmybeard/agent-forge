"""Profile-aware AI client — delegates to a provider backend (Ollama, Bedrock, ...).

The public API is unchanged from the pre-backend refactor: ``chat`` / ``achat``
return a :class:`ChatResponse` (or a streaming generator) regardless of which
provider is backing the profile. All provider-specific quirks live in the
backend modules under :mod:`agentforge.backends`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Generator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, overload

from chalkbox.logging.bridge import get_logger

from .attachments import Attachment
from .backends._retry import backoff_seconds, classify_model_error
from .backends.base import Backend
from .backends.ollama import OllamaBackend
from .config import AIProfile, ConfigManager, get_config
from .secret_redactor import RedactionResult, get_redactor

logger = get_logger(__name__)

# Type alias for the optional per-call fallback observer.
FallbackHook = Callable[[str, str, BaseException], None]


# ---------------------------------------------------------------------------
# Per-request model chain
# ---------------------------------------------------------------------------
# A request can touch several models (query refiner -> agent -> any fallback /
# error-recovery escalation -> answer refiner). Every LLM call funnels through
# AIClient, so AIClient records the model that actually answered into this
# request-scoped contextvar. The run summary then surfaces the de-duped chain.
#
# Mirrors `_request_provider_override` (config.py): contextvars propagate
# through asyncio tasks AND run_in_executor / asyncio.to_thread. The value is a
# shared list mutated in place — callers must `reset_models_used()` once in the
# parent context per request and only APPEND afterwards (a `.set()` from a
# worker thread would not propagate back to the parent).
_models_used: ContextVar[list[str] | None] = ContextVar("agentforge_models_used", default=None)


def reset_models_used() -> None:
    """Begin a fresh per-request model chain. Call once at request start."""
    _models_used.set([])


def add_model_used(model: str | None) -> None:
    """Record a model that just answered, collapsing consecutive duplicates so
    the chain reads as transitions (e.g., ministral-3 -> mistral-large)."""
    if not model:
        return
    chain = _models_used.get()
    if chain is None:
        return  # tracking not initialised for this request — skip silently
    if not chain or chain[-1] != model:
        chain.append(model)


def get_models_used() -> list[str]:
    """Return the per-request model chain (consecutive duplicates collapsed)."""
    return list(_models_used.get() or [])


# ---------------------------------------------------------------------------
# Response wrapper
# ---------------------------------------------------------------------------


@dataclass
class ChatResponse:
    """Normalised response from the model."""

    content: str = ""
    thinking: str | None = None  # populated when deep_think + parse_thinking
    # Provider-native reasoning trace (OpenRouter `reasoning_details`). Opaque
    # structured payload — captured so it can be echoed back on the assistant
    # message in the next turn. Interleaved-reasoning models (MiniMax M2,
    # Nemotron, ...) lose the thread if their reasoning isn't round-tripped.
    reasoning_details: list[dict] | None = None
    tool_calls: list[dict] | None = None
    raw: Any = None  # original backend response object
    model: str = ""
    done_reason: str | None = None
    total_duration: int | None = None  # nanoseconds
    # Token usage — populated from backend response when available
    prompt_tokens: int = 0  # input/prompt tokens consumed
    completion_tokens: int = 0  # output/completion tokens generated
    # Bedrock prompt caching metrics (0 when caching is off or provider is Ollama)
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------


def _build_backend(profile: AIProfile) -> Backend:
    """Pick and construct the backend for a profile.

    Defaults to Ollama; Bedrock lands in phase 2. Unknown providers raise.
    """
    provider = (profile.provider or "ollama").lower()
    if provider == "ollama":
        return OllamaBackend(profile)
    if provider == "bedrock":
        # Lazy import so agentforge only pulls boto3 when a Bedrock profile
        # is actually in use.
        from .backends.bedrock import BedrockBackend

        return BedrockBackend(profile)
    if provider in ("deepinfra", "openrouter"):
        # Both expose an OpenAI-compatible REST API — the backend
        # differs only in base_url + api_key, which come off the profile.
        from .backends.openai_compat import OpenAICompatibleBackend

        return OpenAICompatibleBackend(profile)
    raise ValueError(f"Unknown provider '{provider}' on profile '{profile.name}'")


# ---------------------------------------------------------------------------
# AIClient
# ---------------------------------------------------------------------------


class AIClient:
    """Profile-aware client with sync/async chat, streaming, deep thinking, and tool calling."""

    def __init__(
        self,
        *,
        profile: str | AIProfile | None = None,
        config: ConfigManager | None = None,
        config_path: str | None = None,
    ) -> None:
        self._config = config or get_config(config_path)

        # Resolve profile
        if isinstance(profile, AIProfile):
            self._profile = profile
        else:
            self._profile = self._config.get_profile(profile)

        self._backend: Backend = _build_backend(self._profile)
        # Fallback backends are built lazily on first use so a profile with a
        # never-triggered fallback chain doesn't pay backend init cost.
        self._fallback_backends: dict[str, Backend] = {}

        self._last_redaction: RedactionResult | None = None

        logger.debug(
            "AIClient ready — profile=%s provider=%s model=%s host=%s",
            self._profile.name,
            self._profile.provider,
            self._profile.model,
            self._profile.host,
        )

    # -- properties ---------------------------------------------------------

    @property
    def last_redaction(self) -> RedactionResult | None:
        """The most recent :class:`RedactionResult` from a ``chat``/``achat`` call.

        Returns ``None`` if no secrets were found on the last call.  Callers
        (e.g., the WebSocket endpoint) can inspect this to emit a user-facing
        warning after the model call completes.
        """
        return self._last_redaction

    @property
    def profile(self) -> AIProfile:
        return self._profile

    @property
    def model(self) -> str:
        return self._profile.model

    @property
    def backend(self) -> Backend:
        return self._backend

    # -- profile switching --------------------------------------------------

    def switch_profile(self, name: str) -> None:
        """Switch to a different profile at runtime."""
        self._profile = self._config.get_profile(name)
        self._backend = _build_backend(self._profile)
        # Drop the fallback backend cache — the new profile has its own chain.
        self._fallback_backends = {}
        logger.info(
            "Switched to profile '%s' (provider=%s, model=%s)",
            name,
            self._profile.provider,
            self._profile.model,
        )

    # -- fallback chain plumbing -------------------------------------------

    def _resolve_fallback_chain(self) -> list[AIProfile]:
        """Return ``[primary, *resolved_fallbacks]`` for the current profile.

        Fallback names are looked up in the live config — unresolvable or
        duplicate names are skipped with a WARN. The primary is always first.
        """
        chain: list[AIProfile] = [self._profile]
        seen = {self._profile.name}
        for name in self._profile.fallbacks:
            if name in seen:
                logger.debug("AIClient[%s]: skipping duplicate fallback '%s'", self._profile.name, name)
                continue
            try:
                prof = self._config.get_profile(name)
            except ValueError:
                logger.warning(
                    "AIClient[%s]: fallback profile '%s' not defined — skipping",
                    self._profile.name,
                    name,
                )
                continue
            chain.append(prof)
            seen.add(name)
        return chain

    def _backend_for(self, profile: AIProfile) -> Backend:
        """Return a backend for *profile*, building once per AIClient instance."""
        if profile is self._profile or profile.name == self._profile.name:
            return self._backend
        cached = self._fallback_backends.get(profile.name)
        if cached is None:
            cached = _build_backend(profile)
            self._fallback_backends[profile.name] = cached
            logger.debug(
                "AIClient[%s]: built fallback backend for '%s' (provider=%s, model=%s)",
                self._profile.name,
                profile.name,
                profile.provider,
                profile.model,
            )
        return cached

    @staticmethod
    def _is_cancellation(exc: BaseException) -> bool:
        """True for exceptions that must NEVER trigger a fallback."""
        return isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError, SystemExit))

    def _notify_fallback(
        self,
        prev: AIProfile,
        nxt: AIProfile,
        exc: BaseException,
        idx: int,
        total_fallbacks: int,
        on_fallback: FallbackHook | None,
    ) -> None:
        decision = classify_model_error(exc)
        logger.warning(
            "AIClient[%s]: %s — falling back to '%s' (%d/%d)",
            prev.name,
            decision.reason,
            nxt.name,
            idx,
            total_fallbacks,
        )
        if on_fallback is not None:
            try:
                on_fallback(prev.name, nxt.name, exc)
            except Exception:  # noqa: BLE001 — observer must never break the run
                logger.debug("on_fallback callback raised", exc_info=True)

    # -- chat (sync) --------------------------------------------------------

    @overload
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: list[Attachment] | None = None,
        stream: Literal[False] = False,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
        enable_fallbacks: bool = True,
        retries_per_profile: int = 0,
        on_fallback: FallbackHook | None = None,
    ) -> ChatResponse: ...

    @overload
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: list[Attachment] | None = None,
        stream: Literal[True],
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
        enable_fallbacks: bool = True,
        retries_per_profile: int = 0,
        on_fallback: FallbackHook | None = None,
    ) -> Generator[dict, None, None]: ...

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: list[Attachment] | None = None,
        stream: bool = False,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
        enable_fallbacks: bool = True,
        retries_per_profile: int = 0,
        on_fallback: FallbackHook | None = None,
    ) -> ChatResponse | Generator[dict, None, None]:
        """Synchronous chat with the model."""
        # Extended thinking: Bedrock with thinking_budget uses native thinking
        # blocks; everything else uses the prompt-engineering fallback.
        use_native_thinking = (
            deep_think and self._profile.provider == "bedrock" and self._profile.thinking_budget is not None
        )
        if not use_native_thinking:
            messages = self._prepare_messages(messages, deep_think)

        messages = self._apply_attachments(messages, attachments)
        messages, redaction = self._redact_messages(messages)

        if redaction and redaction.had_secrets:
            logger.warning(
                "Redacted %d secret(s) before sending to model (%s)",
                len(redaction.findings),
                self._profile.model,
            )
            # Store the latest redaction result so callers (e.g., ws_endpoint)
            # can inspect it and emit a user-facing warning.
            self._last_redaction = redaction

        if stream:
            return self._stream_with_fallback(
                messages,
                tools=tools,
                temperature=temperature,
                deep_think=use_native_thinking,
                keep_alive=keep_alive,
                enable_fallbacks=enable_fallbacks,
                on_fallback=on_fallback,
            )
        return self._chat_with_fallback(
            messages,
            tools=tools,
            temperature=temperature,
            deep_think=use_native_thinking,
            keep_alive=keep_alive,
            enable_fallbacks=enable_fallbacks,
            retries_per_profile=retries_per_profile,
            on_fallback=on_fallback,
        )

    # -- chat (async) -------------------------------------------------------

    @overload
    async def achat(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: list[Attachment] | None = None,
        stream: Literal[False] = False,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
        enable_fallbacks: bool = True,
        retries_per_profile: int = 0,
        on_fallback: FallbackHook | None = None,
    ) -> ChatResponse: ...

    @overload
    async def achat(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: list[Attachment] | None = None,
        stream: Literal[True],
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
        enable_fallbacks: bool = True,
        retries_per_profile: int = 0,
        on_fallback: FallbackHook | None = None,
    ) -> AsyncGenerator[dict, None]: ...

    async def achat(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: list[Attachment] | None = None,
        stream: bool = False,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
        enable_fallbacks: bool = True,
        retries_per_profile: int = 0,
        on_fallback: FallbackHook | None = None,
    ) -> ChatResponse | AsyncGenerator[dict, None]:
        """Asynchronous chat with the model. Same interface as :meth:`chat`."""
        use_native_thinking = (
            deep_think and self._profile.provider == "bedrock" and self._profile.thinking_budget is not None
        )
        if not use_native_thinking:
            messages = self._prepare_messages(messages, deep_think)

        messages = self._apply_attachments(messages, attachments)
        messages, redaction = self._redact_messages(messages)

        if redaction and redaction.had_secrets:
            logger.warning(
                "Redacted %d secret(s) before sending to model (%s)",
                len(redaction.findings),
                self._profile.model,
            )
            self._last_redaction = redaction

        if stream:
            return self._astream_with_fallback(
                messages,
                tools=tools,
                temperature=temperature,
                deep_think=use_native_thinking,
                keep_alive=keep_alive,
                enable_fallbacks=enable_fallbacks,
                on_fallback=on_fallback,
            )
        return await self._achat_with_fallback(
            messages,
            tools=tools,
            temperature=temperature,
            deep_think=use_native_thinking,
            keep_alive=keep_alive,
            enable_fallbacks=enable_fallbacks,
            retries_per_profile=retries_per_profile,
            on_fallback=on_fallback,
        )

    # -- chat with fallback (sync) -----------------------------------------

    def _chat_with_fallback(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None,
        temperature: float | None,
        deep_think: bool,
        keep_alive: bool | None,
        enable_fallbacks: bool,
        retries_per_profile: int,
        on_fallback: FallbackHook | None,
    ) -> ChatResponse:
        chain = self._resolve_fallback_chain() if enable_fallbacks else [self._profile]
        max_attempts = max(1, retries_per_profile + 1)
        last_exc: BaseException | None = None

        for idx, profile in enumerate(chain):
            backend = self._backend_for(profile)
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = backend.chat(
                        messages,
                        tools=tools,
                        temperature=temperature,
                        deep_think=deep_think,
                        keep_alive=keep_alive,
                    )
                    add_model_used(resp.model or profile.model)
                    return resp
                except BaseException as exc:
                    if self._is_cancellation(exc):
                        raise
                    last_exc = exc
                    decision = classify_model_error(exc)
                    if decision.retryable and attempt < max_attempts:
                        sleep_s = backoff_seconds(attempt)
                        logger.warning(
                            "AIClient[%s]: attempt %d/%d failed (%s) — retrying in %.1fs",
                            profile.name,
                            attempt,
                            max_attempts,
                            decision.reason,
                            sleep_s,
                        )
                        time.sleep(sleep_s)
                        continue
                    break  # advance to next profile

            next_idx = idx + 1
            if next_idx < len(chain) and last_exc is not None:
                self._notify_fallback(
                    profile,
                    chain[next_idx],
                    last_exc,
                    next_idx,
                    len(chain) - 1,
                    on_fallback,
                )

        assert last_exc is not None  # loop only exits via return or exception
        raise last_exc

    # -- chat with fallback (async) ----------------------------------------

    async def _achat_with_fallback(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None,
        temperature: float | None,
        deep_think: bool,
        keep_alive: bool | None,
        enable_fallbacks: bool,
        retries_per_profile: int,
        on_fallback: FallbackHook | None,
    ) -> ChatResponse:
        chain = self._resolve_fallback_chain() if enable_fallbacks else [self._profile]
        max_attempts = max(1, retries_per_profile + 1)
        last_exc: BaseException | None = None

        for idx, profile in enumerate(chain):
            backend = self._backend_for(profile)
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await backend.achat(
                        messages,
                        tools=tools,
                        temperature=temperature,
                        deep_think=deep_think,
                        keep_alive=keep_alive,
                    )
                    add_model_used(resp.model or profile.model)
                    return resp
                except BaseException as exc:
                    if self._is_cancellation(exc):
                        raise
                    last_exc = exc
                    decision = classify_model_error(exc)
                    if decision.retryable and attempt < max_attempts:
                        sleep_s = backoff_seconds(attempt)
                        logger.warning(
                            "AIClient[%s]: attempt %d/%d failed (%s) — retrying in %.1fs",
                            profile.name,
                            attempt,
                            max_attempts,
                            decision.reason,
                            sleep_s,
                        )
                        await asyncio.sleep(sleep_s)
                        continue
                    break

            next_idx = idx + 1
            if next_idx < len(chain) and last_exc is not None:
                self._notify_fallback(
                    profile,
                    chain[next_idx],
                    last_exc,
                    next_idx,
                    len(chain) - 1,
                    on_fallback,
                )

        assert last_exc is not None
        raise last_exc

    # -- stream with fallback (sync) ---------------------------------------

    def _stream_with_fallback(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None,
        temperature: float | None,
        deep_think: bool,
        keep_alive: bool | None,
        enable_fallbacks: bool,
        on_fallback: FallbackHook | None,
    ) -> Generator[dict, None, None]:
        # Stream fallback fires only BEFORE the first chunk is yielded —
        # once chunks are on the wire, partial output makes a swap unsafe.
        chain = self._resolve_fallback_chain() if enable_fallbacks else [self._profile]
        last_exc: BaseException | None = None

        for idx, profile in enumerate(chain):
            backend = self._backend_for(profile)
            gen = backend.stream(
                messages,
                tools=tools,
                temperature=temperature,
                deep_think=deep_think,
                keep_alive=keep_alive,
            )
            try:
                first = next(gen)
            except StopIteration:
                return  # empty stream is a successful no-op
            except BaseException as exc:
                if self._is_cancellation(exc):
                    raise
                last_exc = exc
                next_idx = idx + 1
                if next_idx < len(chain):
                    self._notify_fallback(
                        profile,
                        chain[next_idx],
                        exc,
                        next_idx,
                        len(chain) - 1,
                        on_fallback,
                    )
                continue
            # First chunk obtained — commit to this profile.
            add_model_used(profile.model)
            yield first
            yield from gen
            return

        assert last_exc is not None
        raise last_exc

    # -- stream with fallback (async) --------------------------------------

    async def _astream_with_fallback(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None,
        temperature: float | None,
        deep_think: bool,
        keep_alive: bool | None,
        enable_fallbacks: bool,
        on_fallback: FallbackHook | None,
    ) -> AsyncGenerator[dict, None]:
        chain = self._resolve_fallback_chain() if enable_fallbacks else [self._profile]
        last_exc: BaseException | None = None

        for idx, profile in enumerate(chain):
            backend = self._backend_for(profile)
            agen = backend.astream(
                messages,
                tools=tools,
                temperature=temperature,
                deep_think=deep_think,
                keep_alive=keep_alive,
            )
            try:
                first = await agen.__anext__()
            except StopAsyncIteration:
                return
            except BaseException as exc:
                if self._is_cancellation(exc):
                    raise
                last_exc = exc
                next_idx = idx + 1
                if next_idx < len(chain):
                    self._notify_fallback(
                        profile,
                        chain[next_idx],
                        exc,
                        next_idx,
                        len(chain) - 1,
                        on_fallback,
                    )
                continue
            add_model_used(profile.model)
            yield first
            async for chunk in agen:
                yield chunk
            return

        assert last_exc is not None
        raise last_exc

    # -- secret redaction ---------------------------------------------------

    @staticmethod
    def _redact_messages(messages: list[dict]) -> tuple[list[dict], RedactionResult | None]:
        """Scan all messages for secrets and return redacted copies.

        Returns ``(messages, combined_result)`` where *combined_result* is
        ``None`` when nothing was redacted (fast path).
        """
        try:
            redactor = get_redactor()
        except Exception:
            return messages, None

        cleaned, findings = redactor.redact_messages(messages)
        if not findings:
            return messages, None

        combined = RedactionResult(
            text="",  # not meaningful for a multi-message redaction
            findings=findings,
        )
        return cleaned, combined

    # -- message preparation ------------------------------------------------

    @staticmethod
    def _prepare_messages(messages: list[dict], deep_think: bool) -> list[dict]:
        """Clone messages and optionally wrap the last user message for chain-of-thought."""
        msgs = [m.copy() for m in messages]

        if deep_think and msgs and msgs[-1]["role"] == "user":
            original = msgs[-1]["content"]
            msgs[-1]["content"] = (
                "Think deeply about the following problem. "
                "Show your reasoning step by step inside <think>...</think> tags, "
                "then provide your final answer.\n\n"
                f"{original}"
            )

        return msgs

    def _apply_attachments(self, messages: list[dict], attachments: list[Attachment] | None) -> list[dict]:
        """Inject attachments into the last user message.

        - **Images** -> added to the ``images`` list. Backends translate to provider format.
        - **Documents** (Bedrock only) -> added to ``documents`` list as native blocks.
        - **Text / documents** (Ollama or unsupported format) -> text appended to prompt.
        """
        if not attachments:
            return messages

        user_idx: int | None = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                user_idx = i
                break

        if user_idx is None:
            logger.warning("No user message found -- cannot attach files")
            return messages

        msg = messages[user_idx].copy()
        images: list[bytes | str] = list(msg.get("images", []))
        documents: list[dict] = list(msg.get("documents", []))
        extra_text_parts: list[str] = []

        # Formats that Bedrock handles natively as document blocks
        bedrock_doc_formats = {
            ".pdf": "pdf",
            ".csv": "csv",
            ".doc": "doc",
            ".docx": "docx",
            ".xls": "xls",
            ".xlsx": "xlsx",
            ".html": "html",
            ".txt": "txt",
            ".md": "md",
        }

        is_bedrock = self._profile.provider == "bedrock"

        for att in attachments:
            if att.is_image:
                img_val = att.for_ollama_message()
                if img_val is not None:
                    images.append(img_val)
                    logger.debug("Attached image: %s", att.name)
            elif is_bedrock and att.path:
                ext = att.path.suffix.lower()
                fmt = bedrock_doc_formats.get(ext)
                if fmt:
                    try:
                        raw = att.read_bytes()
                        documents.append({"name": att.name or "document", "format": fmt, "data": raw})
                        logger.debug("Attached document block (%s): %s", fmt, att.name)
                        continue
                    except (FileNotFoundError, OSError) as exc:
                        logger.warning(
                            "Could not read document %s: %s -- falling back to text",
                            att.name,
                            exc,
                        )
                # Unsupported format or read error -- fall through to text
                text = att.as_context_text()
                if text:
                    label = att.name or "attachment"
                    extra_text_parts.append(f"\n\n--- Attached file: {label} ---\n{text}")
                    logger.debug("Attached text (%d chars): %s", len(text), att.name)
            else:
                text = att.as_context_text()
                if text:
                    label = att.name or "attachment"
                    extra_text_parts.append(f"\n\n--- Attached file: {label} ---\n{text}")
                    logger.debug("Attached text (%d chars): %s", len(text), att.name)

        if images:
            msg["images"] = images
        if documents:
            msg["documents"] = documents
        if extra_text_parts:
            msg["content"] = msg.get("content", "") + "".join(extra_text_parts)

        messages = list(messages)
        messages[user_idx] = msg
        return messages
