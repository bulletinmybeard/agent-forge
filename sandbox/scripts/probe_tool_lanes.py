"""probe_tool_lanes.py — verify every tool-using lane can actually call tools.

For each provider and each agent/tool/search lane it resolves the configured
model and runs two live checks against the real backend (via AIClient):

  1. init+tools  — send a prompt WITH a tool spec; did the model emit a tool call?
  2. round-trip  — feed the tool result back; did the model produce a final
                   answer without erroring?

This reproduces the three provider traps we hit curating the picks by hand:
  - DeepInfra Gemini : 500 on the round-trip (missing thought_signature) -> round-trip x
  - OpenRouter Hermes: 404 "no endpoints support tool use" on init       -> init x (plain chat ok)
  - Bedrock Sonnet   : extended-thinking over-tooling                    -> flagged (thinking_budget)

When init errors WITH tools, the probe retries a plain (no-tool) chat to tell
"model can't do function calling" apart from "model/auth is down".

Lanes that resolve to the same (provider, model, reasoning) signature are probed
once and shared, so a provider that maps every lane onto one model costs one probe.

PAID: hitting bedrock/deepinfra/openrouter spends real tokens. Default --provider
is the local Ollama; pass --provider all (or a comma list) to sweep the cloud ones.

  python sandbox/scripts/probe_tool_lanes.py                      # ollama only
  python sandbox/scripts/probe_tool_lanes.py --provider all       # every provider
  python sandbox/scripts/probe_tool_lanes.py --provider bedrock,openrouter
  python sandbox/scripts/probe_tool_lanes.py --provider all --lanes agent,web-search
"""

import sandbox  # noqa: F401 — MUST be first (patches sys.path + env bootstrap)

import argparse
import os
import time

import sandbox_conf as conf
from agentforge.client import AIClient
from agentforge.config import get_config

# Lanes that drive tool loops in production (web GUI modes + agent runners).
DEFAULT_LANES = [
    "agent",
    "agent-heavy",
    "web-search",
    "tool",
    "default",
    "cloud-heavy",
    "coding",
    "cloud-coder",
    "log-analyzer",
    "deep-reasoner",
    "light-agent",
    "fast-worker",
]

# Providers that ship a profiles/providers/<name>.yaml. "all" expands to these.
ALL_PROVIDERS = ["ollama", "bedrock", "deepinfra", "openrouter"]

# Status of a single lane probe.
OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_MARK = {OK: "*", WARN: "!", FAIL: "x", SKIP: "-"}
_COLOUR = {OK: conf.GREEN, WARN: conf.YELLOW, FAIL: conf.RED, SKIP: conf.GREY}

# Reasoning lanes (parse_thinking / thinking_budget) spend most of their output
# budget on the <think> block, which gets stripped — so a tight cap leaves no
# final answer and the round-trip looks "empty". Give them this much more room
# so they always have space to answer after thinking.
_REASONING_TOKEN_MULT = 4

# Canned tool output fed on the round-trip turn — keeps the probe offline.
_TOOL_RESULT = (
    "example.com — 'Example Domain. This domain is for use in illustrative "
    "examples in documents. You may use this domain without prior coordination.'"
)


def web_fetch(url: str) -> str:
    """Fetch the readable text content at a URL."""
    # never executed — the probe constructs the tool result by hand
    return _TOOL_RESULT


def _short(exc: object, n: int = 160) -> str:
    """One-line, length-capped rendering of an exception."""
    s = " ".join(str(exc).split())
    return s if len(s) <= n else s[: n - 2] + ".."


def _trunc(s: str | None, n: int) -> str:
    s = s or "?"
    return s if len(s) <= n else s[: n - 2] + ".."


def _fmt_dur(seconds: float) -> str:
    """Compact wall-clock: sub-second as ms, else one-decimal seconds."""
    return f"{seconds * 1000:.0f}ms" if seconds < 1.0 else f"{seconds:.1f}s"


def _signature(p: object) -> tuple:
    """Dedup key: same model + reasoning state behaves identically for tools."""
    reasoning = bool(getattr(p, "thinking_budget", None)) or bool(getattr(p, "parse_thinking", False))
    return (getattr(p, "provider", None) or "ollama", getattr(p, "model", "?"), reasoning)


def _flags(p: object) -> str:
    f = []
    tb = getattr(p, "thinking_budget", None)
    if tb:
        f.append(f"thinking_budget={tb}")
    if getattr(p, "parse_thinking", False):
        f.append("parse_thinking")
    return ",".join(f)


def _plain_chat_ok(ai: AIClient) -> tuple[bool, str]:
    """Does a no-tool chat work? Distinguishes 'no tool support' from 'lane down'."""
    try:
        r = ai.chat([{"role": "user", "content": "Reply with the single word: ok"}], enable_fallbacks=False)
        return bool(getattr(r, "content", "")), ""
    except Exception as exc:  # noqa: BLE001 — probe classifies, never raises
        return False, _short(exc)


def _probe(lane: str, max_tokens: int) -> tuple[str, str, str, str]:
    """Run init+tools then round-trip for one lane. Returns (init, rt, status, note)."""
    ai = AIClient(profile=lane)
    is_reasoning = bool(getattr(ai.profile, "thinking_budget", None)) or bool(
        getattr(ai.profile, "parse_thinking", False)
    )
    cap = max_tokens * _REASONING_TOKEN_MULT if is_reasoning else max_tokens
    try:
        ai.profile.max_tokens = min(ai.profile.max_tokens or cap, cap)
    except Exception:  # noqa: BLE001
        pass

    sys_msg = {
        "role": "system",
        "content": (
            "You are a tool-using assistant. When asked to fetch a URL you MUST "
            "call the web_fetch tool; do not answer from memory."
        ),
    }
    user_msg = {"role": "user", "content": "Fetch https://example.com and summarise it in one sentence."}

    # turn 1 — init with tools
    try:
        r1 = ai.chat([sys_msg, user_msg], tools=[web_fetch], enable_fallbacks=False)
    except Exception as exc:  # noqa: BLE001
        plain_ok, plain_err = _plain_chat_ok(ai)
        if plain_ok:
            return "tools_x", "-", FAIL, f"tools rejected but plain chat ok -> no function calling: {_short(exc)}"
        return "error", "-", FAIL, f"init failed (no tools either) -> model/auth down: {plain_err or _short(exc)}"

    if not getattr(r1, "tool_calls", None):
        return "no_call", "-", WARN, "model answered without a tool call (declined, or truncated -> raise --max-tokens)"

    # turn 2 — round-trip (mirror the assistant/tool message shape from agent.py)
    assistant = {
        "role": "assistant",
        "content": r1.content or "",
        "tool_calls": [{"function": {"name": tc["name"], "arguments": tc["arguments"]}} for tc in r1.tool_calls],
    }
    reasoning = getattr(r1, "reasoning_details", None)
    if reasoning:
        assistant["reasoning_details"] = reasoning
    tool_msg = {"role": "tool", "content": _TOOL_RESULT}

    try:
        r2 = ai.chat([sys_msg, user_msg, assistant, tool_msg], tools=[web_fetch], enable_fallbacks=False)
    except Exception as exc:  # noqa: BLE001
        return "call", "error", FAIL, f"round-trip rejected the tool result: {_short(exc)}"

    if r2.content and r2.content.strip():
        return "call", "ok", OK, ""
    return "call", "empty", WARN, "round-trip returned empty content"


def _row(mark_status: str, lane: str, model: str, init: str, rt: str, dur: str, note: str) -> None:
    colour = _COLOUR.get(mark_status, conf.RESET)
    mark = _MARK.get(mark_status, "?")
    line = f"  {mark} {lane:<13} {_trunc(model, 33):<33} {init:<8} {rt:<8} {dur:>7}  {note}".rstrip()
    print(conf.c(colour, line))


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe tool-using lanes for tool support + a clean round-trip.")
    conf.add_common_args(
        parser,
        default_profile="agent",
        wide_provider=True,
        with_all_provider=True,
        with_provider_exclude=True,
    )
    parser.add_argument("--lanes", default=None, help="comma list of lanes to probe (default: the tool-lane set)")
    parser.add_argument(
        "--max-tokens", type=int, default=512, help="cap per-call output tokens to limit cost (default 512)"
    )
    args = parser.parse_args()

    lanes = [x.strip() for x in args.lanes.split(",")] if args.lanes else DEFAULT_LANES

    raw = args.provider
    if raw == "all":
        providers = list(ALL_PROVIDERS)
    elif "," in raw:
        providers = [p.strip() for p in raw.split(",") if p.strip()]
    elif raw == "auto":
        providers = ["auto"]
    else:
        providers = [raw]
    if getattr(args, "exclude", None):
        skip = {p.strip() for p in args.exclude.split(",")}
        providers = [p for p in providers if p not in skip]

    os.environ["SANDBOX_QUIET"] = "1"  # suppress per-bootstrap banner; we print our own
    start = time.perf_counter()
    passed = failed = warned = 0

    for prov in providers:
        conf.bootstrap(provider=(None if prov == "auto" else prov))
        cfg = get_config()
        active = getattr(cfg, "_provider_override", None) or "(per-profile)"
        print()
        print(conf.c(conf.BOLD, f"PROVIDER: {prov}") + conf.c(conf.GREY, f"   (active override: {active})"))
        print(conf.c(conf.GREY, conf.SEP))
        print(
            conf.c(conf.GREY, f"    {'lane':<13} {'model':<33} {'init':<8} {'rndtrip':<8} {'time':>7}  flags / notes")
        )

        cache: dict[tuple, tuple[str, str, str, str, float]] = {}
        prov_elapsed = 0.0  # wall time actually spent probing this provider (unique models only)
        for lane in lanes:
            try:
                profile = cfg.get_profile(lane)
            except Exception as exc:  # noqa: BLE001
                _row(SKIP, lane, "?", "skip", "-", "-", f"unresolved: {_short(exc)}")
                continue

            note_parts = [_flags(profile)]
            resolved_prov = getattr(profile, "provider", None) or "ollama"
            if prov not in ("auto", "ollama") and resolved_prov != prov:
                note_parts.append(f"provider={resolved_prov} (tier fallback)")

            sig = _signature(profile)
            if sig in cache:
                init, rt, status, note, dur = cache[sig]
                note = (note + "  [shared]").strip()
            else:
                t0 = time.perf_counter()
                init, rt, status, note = _probe(lane, args.max_tokens)
                dur = time.perf_counter() - t0
                prov_elapsed += dur
                cache[sig] = (init, rt, status, note, dur)

            note_parts.append(note)
            full_note = "  ".join(x for x in note_parts if x)
            _row(status, lane, profile.model, init, rt, _fmt_dur(dur), full_note)

            if status == OK:
                passed += 1
            elif status == FAIL:
                failed += 1
            elif status == WARN:
                warned += 1

        print(conf.c(conf.GREY, f"  -> {len(cache)} unique model(s), {prov_elapsed:.1f}s probing {prov}"))

    elapsed = time.perf_counter() - start
    print()
    if warned:
        print(conf.c(conf.YELLOW, f"  {warned} lane(s) WARN (declined / empty / truncated — not necessarily broken)"))
    conf.print_summary(passed, failed, elapsed)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
