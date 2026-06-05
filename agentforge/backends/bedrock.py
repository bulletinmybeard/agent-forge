"""AWS Bedrock backend — talks to bedrock-runtime's Converse API.

Message shape on the wire uses the same Ollama-flavoured dicts that the rest
of the framework speaks. This backend translates in and out at its boundary:

- **Ollama → Bedrock** (``_to_bedrock_messages``):
  * ``system`` messages get extracted into a top-level ``system`` param.
  * ``user`` / ``assistant`` / ``tool`` role messages become Bedrock
    ``{"role", "content": [...]}`` dicts with typed content blocks.
  * ``tool_calls`` on assistant messages become ``toolUse`` blocks with
    synthesised ``toolUseId`` values (hash-based so same call → same id).
  * ``tool`` role messages become ``user`` turns with ``toolResult`` blocks
    referencing the matching ``toolUseId``.
  * ``images`` on user messages become ``image`` blocks with inline bytes.

- **Bedrock → Ollama** (``_wrap_response`` / stream normaliser):
  * ``content`` text blocks concatenate into ``ChatResponse.content``.
  * ``toolUse`` blocks become ``{"name", "arguments"}`` entries in
    ``ChatResponse.tool_calls`` — same shape Ollama produces.
  * Stream events collapse into the same ``{"content", "done", "raw"}``
    generator shape that OllamaBackend yields.

Streaming tool calls are tricky: Bedrock emits ``toolUse`` input as incremental
partial-JSON string deltas. The stream handler accumulates them per
content-block index and parses at ``contentBlockStop``.

Thread safety: boto3 Client instances are NOT thread-safe for individual calls,
but different Client instances are fine across threads. We hold one sync client
per backend instance; the sync chat/stream paths must not be called concurrently
on the same backend. The async paths run sync boto3 inside ``asyncio.to_thread``
with a per-call ephemeral client to avoid any cross-task sharing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import re as _re
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Any

from chalkbox.logging.bridge import get_logger

from .base import Backend

if TYPE_CHECKING:
    from ..client import ChatResponse
    from ..config import AIProfile

logger = get_logger(__name__)


class BedrockBackend(Backend):
    """Backend that talks to AWS Bedrock via the Converse API."""

    def __init__(self, profile: AIProfile) -> None:
        super().__init__(profile)
        # Lazy-import boto3 so users without Bedrock profiles don't pay the
        # import cost at module load. ImportError here is intentional — if a
        # Bedrock profile is selected, boto3 is required.
        import boto3
        from botocore.config import Config as BotoConfig

        if not profile.aws_region:
            raise ValueError(
                f"Bedrock profile '{profile.name}' requires aws_region. "
                f"Set ai.bedrock.aws_region or profile.aws_region."
            )

        # Per-profile retry/timeout config. Matches the Ollama backend's
        # `timeout` semantics where possible.
        boto_config = BotoConfig(
            region_name=profile.aws_region,
            read_timeout=profile.timeout,
            connect_timeout=min(profile.timeout, 60),
            retries={"max_attempts": 3, "mode": "adaptive"},
        )

        client_kwargs: dict[str, Any] = {
            "service_name": "bedrock-runtime",
            "region_name": profile.aws_region,
            "config": boto_config,
        }
        if profile.aws_access_key_id and profile.aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = profile.aws_access_key_id
            client_kwargs["aws_secret_access_key"] = profile.aws_secret_access_key
            if profile.aws_session_token:
                client_kwargs["aws_session_token"] = profile.aws_session_token

        self._client = boto3.client(**client_kwargs)
        self._boto3 = boto3
        self._boto_config = boto_config

    # -- non-streaming ------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,  # noqa: ARG002 — Ollama-only
    ) -> ChatResponse:
        params = self._build_converse_params(messages, tools, temperature, deep_think)
        self._log_request(params)
        response = self._client.converse(**params)
        return self._wrap_response(response)

    async def achat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,  # noqa: ARG002
    ) -> ChatResponse:
        # boto3 is synchronous; run in a worker thread so we don't block the loop.
        return await asyncio.to_thread(
            self.chat,
            messages,
            tools=tools,
            temperature=temperature,
            deep_think=deep_think,
        )

    # -- streaming ----------------------------------------------------------

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,  # noqa: ARG002
    ) -> Generator[dict, None, None]:
        params = self._build_converse_params(messages, tools, temperature, deep_think)
        self._log_request(params)
        response = self._client.converse_stream(**params)
        yield from self._normalise_stream(response["stream"])

    async def astream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,  # noqa: ARG002
    ) -> AsyncGenerator[dict, None]:
        # Pull the sync stream in a worker thread, hand chunks to the caller
        # through an asyncio.Queue. boto3's event stream is a blocking iterator.
        params = self._build_converse_params(messages, tools, temperature, deep_think)
        self._log_request(params)

        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        sentinel = object()

        def _producer() -> None:
            try:
                response = self._client.converse_stream(**params)
                for chunk in self._normalise_stream(response["stream"]):
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
            except Exception as exc:  # noqa: BLE001 — surfaced to consumer
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(sentinel), loop).result()

        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(None, _producer)
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            await task

    # -- request building ---------------------------------------------------

    def _is_anthropic_model(self) -> bool:
        """True only for Anthropic Claude Bedrock models.

        Bedrock prompt caching (cachePoint blocks) and extended thinking
        (`additionalModelRequestFields.thinking`) are Anthropic-specific on
        Bedrock today. Nova/Mistral/Qwen/GLM silently accept those fields but
        do nothing with them — gating here avoids shipping noise and makes
        it obvious in logs which models actually benefit.
        """
        model = (self._profile.model or "").lower()
        return "anthropic" in model

    def _build_converse_params(
        self,
        messages: list[dict[str, Any]],
        tools: list[Callable] | None,
        temperature: float | None,
        deep_think: bool = False,
    ) -> dict[str, Any]:
        system_blocks, bedrock_messages = self._to_bedrock_messages(messages)

        params: dict[str, Any] = {
            "modelId": self._profile.model,
            "messages": bedrock_messages,
            "inferenceConfig": self._build_inference_config(temperature, deep_think),
        }
        if system_blocks:
            params["system"] = system_blocks
        if tools:
            params["toolConfig"] = {
                "tools": [self._func_to_tool_spec(fn) for fn in tools],
            }

        # Prompt caching: append cachePoint blocks to system and tool arrays
        # so Bedrock caches the (stable) prefix across requests. Anthropic-only
        # today; other Bedrock families silently accept-and-ignore the field.
        if self._profile.prompt_caching and self._is_anthropic_model():
            if system_blocks:
                system_blocks.append({"cachePoint": {"type": "default"}})
            if tools and "toolConfig" in params:
                params["toolConfig"]["tools"].append({"cachePoint": {"type": "default"}})

        # top_k and other per-provider knobs go here. Anthropic + Mistral accept
        # `top_k` in additionalModelRequestFields; Nova accepts `top_k` too.
        # top_k is incompatible with extended thinking — skip when deep_think is active.
        extra: dict[str, Any] = {}
        if self._profile.top_k is not None and not deep_think:
            extra["top_k"] = self._profile.top_k
        # Extended thinking is Anthropic-only on Bedrock. Gate so non-Claude
        # profiles (Nova/Mistral/Qwen/GLM) with a stray thinking_budget don't
        # ship the field uselessly.
        if deep_think and self._profile.thinking_budget and self._is_anthropic_model():
            extra["thinking"] = {"type": "enabled", "budget_tokens": self._profile.thinking_budget}
        if extra:
            params["additionalModelRequestFields"] = extra

        return params

    def _build_inference_config(self, temperature: float | None, deep_think: bool = False) -> dict[str, Any]:
        p = self._profile
        cfg: dict[str, Any] = {}
        # Extended thinking is incompatible with temperature, topP, topK.
        # Some Claude 4.7+ models reject `temperature` outright — opt out via
        # profile.omit_temperature.
        if not deep_think:
            if not p.omit_temperature:
                cfg["temperature"] = temperature if temperature is not None else p.temperature
            if p.top_p is not None:
                cfg["topP"] = p.top_p
        if p.max_tokens:
            # The thinking-budget bump is only meaningful when extended thinking
            # actually engages — i.e. Anthropic model + deep_think + budget set.
            # For non-Claude profiles we always use the configured max_tokens.
            if deep_think and p.thinking_budget and p.max_tokens <= p.thinking_budget and self._is_anthropic_model():
                # maxTokens must exceed thinking_budget or Bedrock rejects the request.
                cfg["maxTokens"] = p.thinking_budget + 4000
            else:
                cfg["maxTokens"] = p.max_tokens
        if p.stop is not None:
            cfg["stopSequences"] = p.stop
        return cfg

    # -- message translation (Ollama → Bedrock) -----------------------------

    def _to_bedrock_messages(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split Ollama-shaped messages into ``(system, messages)`` for Converse.

        Walks the history maintaining a mapping from tool call ordinal to
        generated toolUseId so that the next turn's tool results can reference
        the right ids. The mapping resets per assistant turn.
        """
        system_blocks: list[dict[str, Any]] = []
        out: list[dict[str, Any]] = []

        # Track the most recent assistant turn's tool_uses in order so that
        # the following "tool" messages can be matched to their ids.
        pending_tool_ids: list[tuple[str, str]] = []  # [(toolUseId, name), ...]
        pending_tool_results: list[dict[str, Any]] = []

        def _flush_tool_results() -> None:
            """Emit a single user-role message with accumulated toolResult blocks."""
            if pending_tool_results:
                out.append({"role": "user", "content": list(pending_tool_results)})
                pending_tool_results.clear()

        for msg in messages:
            role = msg.get("role")

            if role == "system":
                text = msg.get("content") or ""
                if text:
                    system_blocks.append({"text": text})
                continue

            if role == "tool":
                # Match this result to the next pending toolUseId by order.
                if not pending_tool_ids:
                    logger.warning("Bedrock: received 'tool' message with no pending tool call — dropping")
                    continue
                tool_use_id, _name = pending_tool_ids.pop(0)
                result_text = msg.get("content") or ""
                pending_tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"text": result_text}],
                        }
                    }
                )
                continue

            # Any non-tool message terminates a batch of tool results.
            _flush_tool_results()
            pending_tool_ids.clear()

            content_blocks: list[dict[str, Any]] = []

            if role == "user":
                text = msg.get("content") or ""
                if text:
                    content_blocks.append({"text": text})
                for img in msg.get("images", []) or []:
                    block = self._image_block(img)
                    if block is not None:
                        content_blocks.append(block)
                for doc in msg.get("documents", []) or []:
                    content_blocks.append(
                        {
                            "document": {
                                "format": doc["format"],
                                "name": _sanitize_doc_name(doc.get("name", "document")),
                                "source": {"bytes": doc["data"]},
                            }
                        }
                    )
                if not content_blocks:
                    # Bedrock rejects empty content — stuff in a single space.
                    content_blocks.append({"text": " "})
                out.append({"role": "user", "content": content_blocks})
                continue

            if role == "assistant":
                text = msg.get("content") or ""
                if text:
                    content_blocks.append({"text": text})
                tool_calls = msg.get("tool_calls") or []
                for idx, tc in enumerate(tool_calls):
                    # Ollama's assistant tool_calls shape is either the raw
                    # model dict ({"function": {"name", "arguments"}}) or the
                    # simplified {"name", "arguments"} that ChatResponse uses.
                    fn = tc.get("function", tc)
                    name = fn.get("name") or tc.get("name") or "unknown"
                    args = fn.get("arguments") if "function" in tc else tc.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"raw": args}
                    args = args or {}
                    tool_use_id = self._make_tool_use_id(name, args, idx)
                    content_blocks.append(
                        {
                            "toolUse": {
                                "toolUseId": tool_use_id,
                                "name": name,
                                "input": args,
                            }
                        }
                    )
                    pending_tool_ids.append((tool_use_id, name))
                if not content_blocks:
                    content_blocks.append({"text": " "})
                out.append({"role": "assistant", "content": content_blocks})
                continue

            # Unknown role — fall through as a user turn with raw text.
            logger.debug("Bedrock: unknown role '%s' — treating as user", role)
            out.append({"role": "user", "content": [{"text": msg.get("content") or ""}]})

        _flush_tool_results()
        return system_blocks, out

    @staticmethod
    def _make_tool_use_id(name: str, args: dict, idx: int) -> str:
        """Generate a stable-ish toolUseId for a historical tool call.

        Bedrock requires that every ``toolUse`` block in the assistant history
        has a unique id, and every ``toolResult`` references one of them. We
        hash the (name, args, position) triple so the same historical call
        always gets the same id across retries.
        """
        payload = json.dumps({"n": name, "a": args, "i": idx}, sort_keys=True, default=str)
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        return f"tooluse_{digest}"

    def _image_block(self, img: Any) -> dict[str, Any] | None:
        """Convert an image attachment to a Bedrock ``image`` content block."""
        # Attachment.for_ollama_message() returns one of:
        #   - bytes                   (inline image data)
        #   - str path to a file      (when the attachment is backed by a file)
        #   - str base64              (legacy inline fallback)
        # Ollama accepts paths directly so that's the fast path upstream, but
        # Bedrock only takes raw bytes — we have to read the file ourselves.
        if isinstance(img, bytes):
            raw = img
        elif isinstance(img, str):
            # Try filesystem first — that's what Attachment hands us by default.
            p = _Path(img)
            if p.exists() and p.is_file():
                try:
                    raw = p.read_bytes()
                except OSError as exc:
                    logger.warning("Bedrock: failed to read image file %s: %s", img, exc)
                    return None
            else:
                # Fall back to base64 — keeps inline-data callers working.
                try:
                    raw = base64.b64decode(img, validate=True)
                except Exception:
                    logger.warning("Bedrock: image string is neither a readable file nor base64 — skipping")
                    return None
        else:
            logger.warning("Bedrock: unsupported image type %s — skipping", type(img).__name__)
            return None

        # Best-effort format detection from magic bytes. Bedrock supports
        # png/jpeg/gif/webp.
        fmt = _detect_image_format(raw)
        return {
            "image": {
                "format": fmt,
                "source": {"bytes": raw},
            }
        }

    # -- response wrapping (Bedrock → Ollama) -------------------------------

    def _wrap_response(self, raw: dict[str, Any]) -> ChatResponse:
        from ..client import ChatResponse  # noqa: PLC0415

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        message = (raw.get("output") or {}).get("message") or {}
        for block in message.get("content", []) or []:
            if "text" in block and block.get("type") != "thinking":
                content_parts.append(block["text"])
            elif block.get("type") == "thinking" and "thinking" in block:
                # Extended thinking format: {"type": "thinking", "thinking": "..."}
                thinking_parts.append(block["thinking"])
            elif "reasoningContent" in block:
                # Reasoning config format: {"reasoningContent": {"reasoningText": {"text": "..."}}}
                rc = block["reasoningContent"]
                rt = rc.get("reasoningText") or {}
                if rt.get("text"):
                    thinking_parts.append(rt["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(
                    {
                        "name": tu.get("name", ""),
                        "arguments": tu.get("input") or {},
                    }
                )

        content = "".join(content_parts)
        thinking = "\n".join(thinking_parts) if thinking_parts else None

        # Bedrock doesn't expose a "total_duration" field; use metrics if present.
        metrics = raw.get("metrics") or {}
        total_duration = None
        if "latencyMs" in metrics:
            total_duration = int(metrics["latencyMs"]) * 1_000_000  # ns, for parity

        # Extract token usage from Bedrock response
        usage = raw.get("usage") or {}
        prompt_tokens = usage.get("inputTokens", 0) or 0
        completion_tokens = usage.get("outputTokens", 0) or 0
        cache_read_tokens = usage.get("cacheReadInputTokens", 0) or 0
        cache_write_tokens = usage.get("cacheWriteInputTokens", 0) or 0

        if cache_read_tokens or cache_write_tokens:
            logger.debug(
                "Bedrock cache -- read=%d write=%d standard=%d",
                cache_read_tokens,
                cache_write_tokens,
                prompt_tokens,
            )

        return ChatResponse(
            content=content,
            thinking=thinking,
            tool_calls=tool_calls or None,
            raw=raw,
            model=self._profile.model,
            done_reason=raw.get("stopReason"),
            total_duration=total_duration,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )

    # -- streaming normalisation --------------------------------------------

    def _normalise_stream(self, events: Any) -> Generator[dict, None, None]:
        """Convert Bedrock converse_stream events to the Ollama-flavoured shape.

        Yields one dict per event relevant to the caller. Text deltas surface
        as incremental ``content`` chunks; tool-use deltas are accumulated and
        only surfaced once the block closes (caller can't usefully consume
        partial tool JSON).

        Token usage arrives in a ``metadata`` event that Bedrock emits *after*
        ``messageStop``. We buffer the done sentinel until we have seen the
        metadata (or the stream ends), then inject ``prompt_eval_count`` /
        ``eval_count`` so that the rest of the pipeline can capture tokens the
        same way it does for Ollama.
        """
        # Per-block state. Bedrock streams can interleave text + tool use blocks
        # by index, so we key on contentBlockIndex.
        tool_state: dict[int, dict[str, Any]] = {}
        thinking_state: dict[int, str] = {}
        done = False
        # Buffer the done sentinel until we see the metadata usage event.
        pending_done: dict[str, Any] | None = None

        for event in events:
            if "messageStart" in event:
                continue

            if "contentBlockStart" in event:
                start = event["contentBlockStart"]
                idx = start.get("contentBlockIndex", 0)
                tool_start = (start.get("start") or {}).get("toolUse")
                if tool_start:
                    tool_state[idx] = {
                        "toolUseId": tool_start.get("toolUseId", ""),
                        "name": tool_start.get("name", ""),
                        "input_buf": "",
                    }
                elif start.get("contentBlockType") == "reasoning":
                    thinking_state[idx] = ""
                continue

            if "contentBlockDelta" in event:
                delta_event = event["contentBlockDelta"]
                idx = delta_event.get("contentBlockIndex", 0)
                delta = delta_event.get("delta") or {}
                if "text" in delta:
                    yield {"content": delta["text"], "done": False, "raw": event}
                elif "toolUse" in delta:
                    # Accumulate partial input JSON string — not yielded live.
                    buf = tool_state.setdefault(
                        idx,
                        {"toolUseId": "", "name": "", "input_buf": ""},
                    )
                    buf["input_buf"] += delta["toolUse"].get("input", "") or ""
                elif "reasoningContent" in delta:
                    # Stream deltas: {"reasoningContent": {"text": "..."}} or {"thinking": "..."}
                    rc = delta["reasoningContent"]
                    text = rc.get("text") or rc.get("thinking") or ""
                    if idx in thinking_state:
                        thinking_state[idx] += text
                continue

            if "contentBlockStop" in event:
                idx = event["contentBlockStop"].get("contentBlockIndex", 0)
                if idx in tool_state:
                    # Parse the accumulated JSON so the caller can see it in
                    # the final aggregated response, but don't emit a text chunk
                    # — tool calls surface via the final full response that the
                    # caller assembles separately if needed.
                    buf = tool_state[idx]
                    if buf["input_buf"]:
                        try:
                            parsed = json.loads(buf["input_buf"])
                        except json.JSONDecodeError:
                            parsed = {"raw": buf["input_buf"]}
                        buf["input_parsed"] = parsed
                continue

            if "messageStop" in event:
                done = True
                stop_reason = event["messageStop"].get("stopReason")
                # Buffer the done sentinel — we'll inject token counts from the
                # upcoming ``metadata`` event before yielding it.
                pending_done = {
                    "content": "",
                    "done": True,
                    "raw": event,
                    "stop_reason": stop_reason,
                    # Ollama-compatible zero-placeholders; overwritten by metadata.
                    "prompt_eval_count": 0,
                    "eval_count": 0,
                    "tool_uses": [
                        {
                            "toolUseId": v.get("toolUseId", ""),
                            "name": v.get("name", ""),
                            "input": v.get("input_parsed") or {},
                        }
                        for v in tool_state.values()
                        if v.get("name")
                    ],
                    "thinking": "\n".join(v for v in thinking_state.values() if v) or None,
                }
                continue

            if "metadata" in event:
                # Usage stats arrive after messageStop. Inject into the buffered
                # done sentinel so callers can read prompt_eval_count / eval_count
                # exactly as they do for Ollama streaming responses.
                meta = event["metadata"]
                usage = meta.get("usage") or {}
                if pending_done is not None:
                    pending_done["prompt_eval_count"] = usage.get("inputTokens", 0) or 0
                    pending_done["eval_count"] = usage.get("outputTokens", 0) or 0
                    pending_done["cache_read_tokens"] = usage.get("cacheReadInputTokens", 0) or 0
                    pending_done["cache_write_tokens"] = usage.get("cacheWriteInputTokens", 0) or 0
                    yield pending_done
                    pending_done = None
                continue

        # Stream ended — flush any buffered done chunk (metadata never arrived).
        if pending_done is not None:
            yield pending_done
        elif not done:
            # Safety: always emit a done sentinel even if Bedrock cuts off early.
            yield {"content": "", "done": True, "raw": None, "prompt_eval_count": 0, "eval_count": 0}

    # -- tool spec generation -----------------------------------------------

    @staticmethod
    def _func_to_tool_spec(func: Callable) -> dict[str, Any]:
        """Convert a Python function to a Bedrock Converse ``toolSpec``."""
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or ""
        description = doc.split("\n\n")[0] if doc else func.__name__

        type_map = {
            int: "integer",
            float: "number",
            str: "string",
            bool: "boolean",
            list: "array",
            dict: "object",
        }

        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []

        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            ptype = type_map.get(param.annotation, "string")
            prop: dict[str, Any] = {"type": ptype}
            for line in doc.split("\n"):
                if pname in line and ":" in line:
                    prop["description"] = line.split(":", 1)[-1].strip()
                    break
            properties[pname] = prop
            if param.default is param.empty:
                required.append(pname)

        return {
            "toolSpec": {
                "name": func.__name__,
                "description": description,
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    }
                },
            }
        }

    # -- logging ------------------------------------------------------------

    def _log_request(self, params: dict[str, Any]) -> None:
        logger.debug(
            "Bedrock request — modelId=%s messages=%d tools=%d inferenceConfig=%s",
            params.get("modelId"),
            len(params.get("messages", [])),
            len(params.get("toolConfig", {}).get("tools", [])) if params.get("toolConfig") else 0,
            params.get("inferenceConfig"),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_doc_name(name: str) -> str:
    """Strip characters Bedrock rejects in document names.

    Allowed: alphanumeric, whitespace, hyphens, parentheses, square brackets.
    Everything else (underscores, dots, etc.) is replaced with a hyphen.
    Consecutive whitespace is collapsed to a single space.
    """
    # Strip the file extension (dots aren't allowed)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    # Replace disallowed chars with hyphen
    cleaned = _re.sub(r"[^a-zA-Z0-9\s\-\(\)\[\]]", "-", stem)
    # Collapse multiple whitespace
    cleaned = _re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "document"


def _detect_image_format(raw: bytes) -> str:
    """Best-effort image format detection from magic bytes."""
    if raw.startswith(b"\x89PNG"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw.startswith(b"GIF8"):
        return "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    # Fall back to png — Bedrock will reject outright if wrong.
    return "png"
