"""OpenAI-compatible backend — powers DeepInfra and OpenRouter.

Both providers expose the standard OpenAI REST schema at a custom base
URL and authenticate with a bearer token. The tool-spec format that the agent
loop already emits (via Ollama's ``_func_to_tool_spec``) is byte-identical
to OpenAI's ``tools`` payload, so we reuse it verbatim.

Streaming is minimalist: we yield text deltas chunk by chunk and rely on
the non-streaming path for tool calls (matches the Ollama backend's
behaviour — the agent loop only streams for UI rendering, never for
tool dispatch).
"""

from __future__ import annotations

import inspect
import json
import re
import typing
from collections.abc import AsyncGenerator, Callable, Generator
from typing import TYPE_CHECKING, Any

import httpx
from chalkbox.logging.bridge import get_logger

from .base import Backend

if TYPE_CHECKING:
    from ..client import ChatResponse
    from ..config import AIProfile

logger = get_logger(__name__)

# Some chat models served over an OpenAI-compatible API (notably Claude on
# DeepInfra / OpenRouter) ignore the structured tool_calls channel and emit
# Anthropic-style tool-use markup as plain TEXT, e.g.,
#   <function_calls><invoke name="web_fetch">
#     <parameter name="url">https://...</parameter></invoke></function_calls>
# We parse that out of the content into real tool calls so the agent loop can
# dispatch them instead of relaying the markup to the user as a final answer.
_XML_INVOKE_RE = re.compile(
    r'<(?:antml:)?invoke\s+name="([^"]+)"\s*>(.*?)</(?:antml:)?invoke>',
    re.DOTALL,
)
_XML_PARAM_RE = re.compile(
    r'<(?:antml:)?parameter\s+name="([^"]+)"\s*>(.*?)</(?:antml:)?parameter>',
    re.DOTALL,
)
_XML_FUNCALLS_BLOCK_RE = re.compile(
    r"<(?:antml:)?function_calls>.*?</(?:antml:)?function_calls>",
    re.DOTALL,
)


def _extract_xml_tool_calls(content: str) -> tuple[list[dict] | None, str]:
    """Pull Anthropic-style <function_calls> tool-use markup out of text content.

    Returns ``(tool_calls or None, cleaned_content)``. A parameter value is
    parsed as JSON when possible (numbers, bools, arrays, objects) and otherwise
    kept as the literal string.
    """
    if "invoke" not in content:
        return None, content
    calls: list[dict] = []
    for name, body in _XML_INVOKE_RE.findall(content):
        args: dict[str, Any] = {}
        for pname, pval in _XML_PARAM_RE.findall(body):
            val = pval.strip()
            try:
                args[pname] = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                args[pname] = val
        calls.append({"name": name.strip(), "arguments": args})
    if not calls:
        return None, content
    cleaned = _XML_FUNCALLS_BLOCK_RE.sub("", content)
    cleaned = _XML_INVOKE_RE.sub("", cleaned)
    return calls, cleaned.strip()


# Per-provider API-key env var, surfaced in the "missing api_key" error so the
# user knows which variable to export. Unknown providers fall back to a derived
# ``<PROVIDER>_API_KEY`` name.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "deepinfra": "DEEPINFRA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


# ---------------------------------------------------------------------------
# Tool-spec translation — OpenAI uses the same shape as the Ollama backend
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    int: "integer",
    float: "number",
    str: "string",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _func_to_openai_tool(func: Callable) -> dict:
    """Convert a Python callable to an OpenAI ``tools`` entry.

    Uses ``typing.get_type_hints`` so annotations keep resolving through
    ``from __future__ import annotations`` (which stringifies them at def
    time). Falls back to ``"string"`` for anything we can't map.
    """
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ""
    description = doc.split("\n")[0] if doc else func.__name__
    try:
        hints = typing.get_type_hints(func)
    except Exception:  # forward-refs can raise — degrade gracefully
        hints = {}

    properties: dict[str, dict] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname == "self":
            continue
        ptype = _TYPE_MAP.get(hints.get(pname), "string")
        properties[pname] = {"type": ptype}
        for line in doc.split("\n"):
            if pname in line and ":" in line:
                properties[pname]["description"] = line.split(":", 1)[-1].strip()
                break
        if param.default is param.empty:
            required.append(pname)

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# ---------------------------------------------------------------------------
# Message translation — Ollama-shaped history → OpenAI chat/completions payload
# ---------------------------------------------------------------------------


def _translate_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate internal (Ollama-shaped) messages into OpenAI chat format.

    Key differences handled here:
      * ``tool_calls`` arguments are serialised to JSON strings (OpenAI requires)
        and each call gets a synthetic ``id`` so tool-result messages can refer
        to it. IDs are generated per assistant turn (``call_{turn}_{idx}``).
      * Tool-result messages (``role == "tool"``) get a ``tool_call_id`` pulled
        from the preceding assistant turn's synthetic IDs, in order.
      * Images on user messages are currently dropped with a warning (vision
        support would need a provider-specific content-array format).
    """
    out: list[dict[str, Any]] = []
    pending_tool_ids: list[str] = []
    tool_idx = 0  # cursor into pending_tool_ids for the next tool result
    turn = 0

    for msg in messages:
        role = msg.get("role")

        if role == "assistant":
            new_msg: dict[str, Any] = {"role": "assistant"}
            content = msg.get("content")
            if content:
                new_msg["content"] = content

            calls = msg.get("tool_calls")
            if calls:
                pending_tool_ids = []
                tool_idx = 0
                translated_calls: list[dict[str, Any]] = []
                for idx, call in enumerate(calls):
                    call_id = call.get("id") or f"call_{turn}_{idx}"
                    pending_tool_ids.append(call_id)
                    fn = call.get("function") or {
                        "name": call.get("name"),
                        "arguments": call.get("arguments"),
                    }
                    arguments = fn.get("arguments")
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments)
                    elif arguments is None:
                        arguments = "{}"
                    translated_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": fn.get("name") or call.get("name", ""),
                                "arguments": arguments,
                            },
                        }
                    )
                new_msg["tool_calls"] = translated_calls
                # OpenAI schema requires content to be present (null is fine) when
                # tool_calls are included.
                new_msg.setdefault("content", None)
            # Echo the provider's reasoning trace back so interleaved-reasoning
            # models (MiniMax M2, Nemotron, ...) keep their thread across turns.
            reasoning_details = msg.get("reasoning_details")
            if reasoning_details:
                new_msg["reasoning_details"] = reasoning_details
            turn += 1
            out.append(new_msg)
            continue

        if role == "tool":
            tool_call_id = msg.get("tool_call_id")
            if not tool_call_id and tool_idx < len(pending_tool_ids):
                tool_call_id = pending_tool_ids[tool_idx]
                tool_idx += 1
            if not tool_call_id:
                # Fabricate one — better than dropping the message entirely.
                tool_call_id = "call_orphan"
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": str(msg.get("content", "")),
                }
            )
            continue

        # user / system / anything else — carry through with images stripped.
        new_msg = {"role": role, "content": msg.get("content", "")}
        if msg.get("images"):
            logger.warning(
                "openai_compat: images on user message are not supported by this provider; dropping %d attachment(s)",
                len(msg["images"]),
            )
        out.append(new_msg)

    return out


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class OpenAICompatibleBackend(Backend):
    """Backend that talks to any OpenAI-compatible REST endpoint.

    Currently used for ``provider=deepinfra`` and ``provider=openrouter``
    — they share the same wire format but use different base URLs and
    API keys.
    """

    def __init__(self, profile: AIProfile) -> None:
        super().__init__(profile)
        base_url = (getattr(profile, "base_url", None) or profile.host or "").rstrip("/")
        if not base_url:
            raise ValueError(
                f"Profile '{profile.name}' (provider={profile.provider}) has no "
                f"base_url — set ai.{profile.provider}.base_url in config.yaml "
                f"or override on the profile."
            )
        if not profile.api_key:
            env_var = _PROVIDER_ENV_VAR.get(profile.provider, f"{(profile.provider or '').upper()}_API_KEY")
            raise ValueError(
                f"Profile '{profile.name}' (provider={profile.provider}) has no "
                f"api_key — set ai.{profile.provider}.api_key in config.yaml "
                f"or export the matching env var ({env_var})."
            )
        self._base_url = base_url
        self._chat_url = f"{base_url}/chat/completions"

    # -- non-streaming ------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,  # noqa: ARG002 — not used; kept for interface parity
        keep_alive: bool | None = None,  # noqa: ARG002 — Ollama-specific
    ) -> ChatResponse:
        payload = self._build_payload(messages, tools, temperature, stream=False)
        self._log_request(payload)
        with httpx.Client(timeout=self._profile.timeout) as client:
            resp = client.post(self._chat_url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        return self._wrap_response(data)

    async def achat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,  # noqa: ARG002
        keep_alive: bool | None = None,  # noqa: ARG002
    ) -> ChatResponse:
        payload = self._build_payload(messages, tools, temperature, stream=False)
        self._log_request(payload)
        async with httpx.AsyncClient(timeout=self._profile.timeout) as client:
            resp = await client.post(self._chat_url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        return self._wrap_response(data)

    # -- streaming ----------------------------------------------------------

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,  # noqa: ARG002
        keep_alive: bool | None = None,  # noqa: ARG002
    ) -> Generator[dict, None, None]:
        payload = self._build_payload(messages, tools, temperature, stream=True)
        self._log_request(payload)
        with httpx.Client(timeout=self._profile.timeout) as client:
            with client.stream("POST", self._chat_url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    for chunk in _parse_sse_line(line, self._profile.model):
                        yield chunk

    async def astream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,  # noqa: ARG002
        keep_alive: bool | None = None,  # noqa: ARG002
    ) -> AsyncGenerator[dict, None]:
        payload = self._build_payload(messages, tools, temperature, stream=True)
        self._log_request(payload)
        async with httpx.AsyncClient(timeout=self._profile.timeout) as client:
            async with client.stream("POST", self._chat_url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    for chunk in _parse_sse_line(line, self._profile.model):
                        yield chunk

    # -- internals ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._profile.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[Callable] | None,
        temperature: float | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        p = self._profile
        payload: dict[str, Any] = {
            "model": p.model,
            "messages": _translate_messages(messages),
            "temperature": temperature if temperature is not None else p.temperature,
        }
        if p.max_tokens:
            payload["max_tokens"] = p.max_tokens
        if p.top_p is not None:
            payload["top_p"] = p.top_p
        if p.stop is not None:
            payload["stop"] = p.stop
        if tools:
            payload["tools"] = [_func_to_openai_tool(fn) for fn in tools]
        if stream:
            payload["stream"] = True
        # Profile escape hatch — provider knobs the typed fields don't cover
        # (e.g., {"reasoning": {"enabled": false}}). Wins over standard keys.
        if p.extra_body:
            payload.update(p.extra_body)
        return payload

    def _log_request(self, payload: dict[str, Any]) -> None:
        logger.debug(
            "openai_compat[%s] → %s model=%s messages=%d tools=%d",
            self._profile.provider,
            self._chat_url,
            payload.get("model"),
            len(payload.get("messages", [])),
            len(payload.get("tools", [])),
        )

    def _wrap_response(self, data: dict[str, Any]) -> ChatResponse:
        from ..client import ChatResponse  # noqa: PLC0415 — avoid circular at import

        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else {}) or {}
        content = message.get("content") or ""
        thinking: str | None = None

        # Provider-native reasoning. OpenRouter surfaces reasoning-model output
        # in `reasoning` (text) and `reasoning_details` (structured), separate
        # from `content`. Capture both: the text for display, the structured
        # payload to echo back on the next turn's assistant message —
        # interleaved-reasoning models (MiniMax M2, Nemotron) lose their thread
        # if it isn't round-tripped.
        reasoning_text = message.get("reasoning")
        if isinstance(reasoning_text, str) and reasoning_text.strip():
            thinking = reasoning_text.strip()
        reasoning_details = message.get("reasoning_details")
        if not isinstance(reasoning_details, list) or not reasoning_details:
            reasoning_details = None

        # Fallback for providers that inline <think> tags in content instead of
        # using the dedicated reasoning field.
        if self._profile.parse_thinking and content and not thinking:
            match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
            if match:
                thinking = match.group(1).strip()
                content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()

        tool_calls: list[dict] | None = None
        raw_calls = message.get("tool_calls")
        if raw_calls:
            tool_calls = []
            for tc in raw_calls:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else {}
                    except json.JSONDecodeError:
                        logger.warning(
                            "openai_compat: tool_call arguments failed JSON parse (%s): %s",
                            fn.get("name"),
                            args[:200],
                        )
                        args = {}
                tool_calls.append(
                    {
                        "name": fn.get("name", ""),
                        "arguments": args or {},
                    }
                )

        # Fallback: some models (Claude on DeepInfra / OpenRouter) emit
        # Anthropic-style tool-use XML as text instead of using the structured
        # tool_calls channel. Recover those so the agent loop dispatches them
        # rather than relaying the markup as a final answer.
        if not tool_calls and content:
            xml_calls, content = _extract_xml_tool_calls(content)
            if xml_calls:
                tool_calls = xml_calls
                logger.warning(
                    "openai_compat: recovered %d tool call(s) from inline tool-use "
                    "XML (model wrote markup as text instead of using the tool_calls "
                    "channel)",
                    len(xml_calls),
                )

        usage = data.get("usage") or {}
        finish_reason = choices[0].get("finish_reason") if choices else None

        return ChatResponse(
            content=content,
            thinking=thinking,
            reasoning_details=reasoning_details,
            tool_calls=tool_calls,
            raw=data,
            model=data.get("model") or self._profile.model,
            done_reason=finish_reason,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        )


# ---------------------------------------------------------------------------
# SSE chunk parsing (module-level so the sync and async streams share it)
# ---------------------------------------------------------------------------


def _parse_sse_line(line: str, model: str) -> list[dict]:
    """Parse a single SSE line into zero, one, or two chunk dicts.

    Each dict shape matches the Ollama backend's streaming output:
        {"content": str, "done": bool, "raw": Any}
    """
    if not line or not line.startswith("data:"):
        return []
    payload = line[len("data:") :].strip()
    if not payload:
        return []
    if payload == "[DONE]":
        return [{"content": "", "done": True, "raw": None, "model": model}]
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        logger.debug("openai_compat: skipped un-parseable SSE line: %s", payload[:200])
        return []

    choices = obj.get("choices") or []
    if not choices:
        return []
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content") or ""
    finish_reason = choice.get("finish_reason")
    return [
        {
            "content": content,
            "done": finish_reason is not None,
            "raw": obj,
            "model": obj.get("model") or model,
        }
    ]
