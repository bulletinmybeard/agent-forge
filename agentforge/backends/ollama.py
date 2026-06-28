"""Ollama backend — wraps the ``ollama`` Python client."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncGenerator, Callable, Generator
from typing import TYPE_CHECKING, Any

from chalkbox.logging.bridge import get_logger
from ollama import AsyncClient, Client

from ..typing_utils import callable_name
from .base import Backend

if TYPE_CHECKING:
    from ..client import ChatResponse
    from ..config import AIProfile

logger = get_logger(__name__)


class OllamaBackend(Backend):
    """Backend that talks to a local or remote Ollama server."""

    def __init__(self, profile: AIProfile) -> None:
        super().__init__(profile)
        kwargs = self._build_client_kwargs()
        self._sync = Client(**kwargs)
        self._async = AsyncClient(**kwargs)

    # -- non-streaming ------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,  # noqa: ARG002 -- Bedrock-only
        keep_alive: bool | None = None,
    ) -> ChatResponse:
        params = self._build_chat_params(messages, False, tools, temperature, keep_alive)
        self._log_request(params)
        response = self._sync.chat(**params)
        return self._wrap_response(response)

    async def achat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,  # noqa: ARG002 -- Bedrock-only
        keep_alive: bool | None = None,
    ) -> ChatResponse:
        params = self._build_chat_params(messages, False, tools, temperature, keep_alive)
        self._log_request(params)
        response = await self._async.chat(**params)
        return self._wrap_response(response)

    # -- streaming ----------------------------------------------------------

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,  # noqa: ARG002 -- Bedrock-only
        keep_alive: bool | None = None,
    ) -> Generator[dict, None, None]:
        params = self._build_chat_params(messages, True, tools, temperature, keep_alive)
        self._log_request(params)
        stream = self._sync.chat(**params)
        for chunk in stream:
            yield {
                "content": chunk.message.content or "",
                "done": getattr(chunk, "done", False),
                "raw": chunk,
            }

    async def astream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,  # noqa: ARG002 -- Bedrock-only
        keep_alive: bool | None = None,
    ) -> AsyncGenerator[dict, None]:
        params = self._build_chat_params(messages, True, tools, temperature, keep_alive)
        self._log_request(params)
        stream = await self._async.chat(**params)
        async for chunk in stream:
            yield {
                "content": chunk.message.content or "",
                "done": getattr(chunk, "done", False),
                "raw": chunk,
            }

    # -- internals ----------------------------------------------------------

    def _build_client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self._profile.host,
            "timeout": self._profile.timeout,
        }
        if self._profile.headers:
            kwargs["headers"] = self._profile.headers
        return kwargs

    def _build_ollama_options(self, temperature: float | None = None) -> dict[str, Any]:
        p = self._profile
        opts: dict[str, Any] = {
            "temperature": temperature if temperature is not None else p.temperature,
        }
        if p.max_tokens:
            opts["num_predict"] = p.max_tokens
        if p.top_p is not None:
            opts["top_p"] = p.top_p
        if p.top_k is not None:
            opts["top_k"] = p.top_k
        if p.repeat_penalty is not None:
            opts["repeat_penalty"] = p.repeat_penalty
        if p.stop is not None:
            opts["stop"] = p.stop
        return opts

    def _build_chat_params(
        self,
        messages: list[dict],
        stream: bool,
        tools: list[Callable] | None,
        temperature: float | None,
        keep_alive: bool | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self._profile.model,
            "messages": messages,
            "stream": stream,
            "options": self._build_ollama_options(temperature),
        }

        # keep_alive: per-call > profile > omit
        effective = keep_alive if keep_alive is not None else self._profile.keep_alive
        if effective is not None:
            params["keep_alive"] = 0 if not effective else True

        if tools:
            params["tools"] = [self._func_to_tool_spec(fn) for fn in tools]

        # Profile escape hatch — a nested `options` dict merges into sampling
        # options; every other key (e.g., `think`) is set at the top level of
        # the chat() call.
        for key, value in self._profile.extra_body.items():
            if key == "options" and isinstance(value, dict):
                params["options"].update(value)
            else:
                params[key] = value

        return params

    # -- response wrapping --------------------------------------------------

    def _wrap_response(self, raw: Any) -> ChatResponse:
        from ..client import ChatResponse  # noqa: PLC0415  — avoid circular at import time

        content = raw.message.content or ""
        thinking: str | None = None

        # Strip <think> blocks when profile says to
        if self._profile.parse_thinking and content:
            match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
            if match:
                thinking = match.group(1).strip()
                content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()

        # Extract tool calls (native or fallback from JSON content)
        tool_calls = None
        if hasattr(raw.message, "tool_calls") and raw.message.tool_calls:
            tool_calls = [
                {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in raw.message.tool_calls
            ]
        elif content:
            tool_calls = self._extract_tool_calls_from_text(content)

        return ChatResponse(
            content=content,
            thinking=thinking,
            tool_calls=tool_calls,
            raw=raw,
            model=getattr(raw, "model", self._profile.model),
            done_reason=getattr(raw, "done_reason", None),
            total_duration=getattr(raw, "total_duration", None),
            prompt_tokens=getattr(raw, "prompt_eval_count", 0) or 0,
            completion_tokens=getattr(raw, "eval_count", 0) or 0,
        )

    # -- tool spec generation -----------------------------------------------

    @staticmethod
    def _func_to_tool_spec(func: Callable) -> dict:
        """Convert a Python function to an Ollama tool specification."""
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or ""
        description = doc.split("\n")[0] if doc else callable_name(func)

        type_map = {int: "integer", float: "number", str: "string", bool: "boolean", list: "array", dict: "object"}

        properties: dict[str, dict] = {}
        required: list[str] = []

        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            ptype = type_map.get(param.annotation, "string")
            properties[pname] = {"type": ptype}

            # Try to pull a one-liner description from the docstring
            for line in doc.split("\n"):
                if pname in line and ":" in line:
                    properties[pname]["description"] = line.split(":", 1)[-1].strip()
                    break

            if param.default is param.empty:
                required.append(pname)

        return {
            "type": "function",
            "function": {
                "name": callable_name(func),
                "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        }

    @staticmethod
    def _extract_tool_calls_from_text(text: str) -> list[dict] | None:
        """Fallback: pull tool calls from JSON in the model's text output."""
        calls = []
        for match in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL):
            try:
                obj = json.loads(match.group(0))
                if isinstance(obj, dict) and "name" in obj:
                    calls.append(
                        {
                            "name": obj["name"],
                            "arguments": obj.get("arguments", {}),
                        }
                    )
            except (json.JSONDecodeError, KeyError):
                continue
        return calls or None

    # -- logging ------------------------------------------------------------

    def _log_request(self, params: dict) -> None:
        logger.debug(
            "Chat request — model=%s messages=%d stream=%s tools=%s options=%s",
            params["model"],
            len(params["messages"]),
            params["stream"],
            len(params["tools"]) if params.get("tools") else 0,
            {k: v for k, v in params.get("options", {}).items()},
        )
        # Full per-message dump — only at DEBUG so it doesn't flood INFO logs.
        for i, msg in enumerate(params["messages"]):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            content_len = len(content) if content else 0
            has_tool_calls = "tool_calls" in msg
            has_images = "images" in msg
            logger.debug(
                "  msg[%d] role=%s content_len=%d tool_calls=%s images=%s | %s",
                i,
                role,
                content_len,
                has_tool_calls,
                has_images,
                (content[:200] + "...") if content_len > 200 else content,
            )
