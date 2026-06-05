"""sandbox_conf — shared fixtures and helpers for sandbox test scripts.

Analogous to conftest.py in pytest.  Import after ``import sandbox``::

    import sandbox          # must remain first
    import sandbox_conf as conf

    # ANSI
    print(conf.c(conf.GREEN + conf.BOLD, "hello"))
    print(conf.profile_tag("agent"))

    # Framework bootstrap
    conf.bootstrap()
    registry = conf.make_registry()          # prints "Tool registry: N tools"
    registry = conf.make_registry(False)     # silent

    # Argparse helpers
    parser = argparse.ArgumentParser(...)
    conf.add_common_args(parser)             # adds --profile / --verbose
    conf.add_common_args(parser, with_list=True)   # also adds --list

    # Burst scripts (bootstrap + client + reasoning-off + banner)
    ai, reasoning_off = conf.make_burst_client(args)
    conf.print_burst_header(args, ai, "Burst test", "5 prompts", reasoning_off)

    # Results
    conf.print_summary(passed, failed, elapsed)
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from agentforge.client import AIClient
from agentforge.config import get_config, reset_config
from agentforge.tools import ToolRegistry, register_all_tools

# ── Quiet noisy loggers ───────────────────────────────────────────────────────
# Every Ollama chat call logs a POST /api/chat line at INFO; the Bedrock path
# adds botocore/urllib3 chatter; framework startup + recovery flow add their
# own lines. None of it is useful for sandbox runs, so we raise everything to
# WARNING at import time. Set SANDBOX_LOG_LEVEL=ERROR for live demos where
# you want a fully clean terminal even on tests that exercise error paths;
# set it to INFO if you actually want to see the requests.

_LOG_LEVEL = os.environ.get("SANDBOX_LOG_LEVEL", "WARNING").upper()
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",  # Ollama / generic HTTP client
    "botocore",
    "urllib3",  # Bedrock / AWS SDK
    "agentforge",  # framework.* (config, redactor, agent, etc.)
    "web",  # web.server.* (queue dispatch_compat, etc.)
    "saq",  # SAQ background worker (Enqueuing Job ...)
    "chalkbox",  # chalkbox.logging.* if any leaks
    "redis",  # raw redis-py client
)
for _noisy in _NOISY_LOGGERS:
    logging.getLogger(_noisy).setLevel(_LOG_LEVEL)

# ── Framework config path ─────────────────────────────────────────────────────
# scripts/ → sandbox/ → repo root → framework-config.yaml

FW_CONFIG_PATH: Path = Path(__file__).resolve().parents[2] / "framework-config.yaml"

# ── ANSI colour codes ─────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
CYAN = "\033[96m"
GREY = "\033[90m"
BLUE = "\033[94m"

SEP = "─" * 72

_PROFILE_COLOUR: dict[str, str] = {
    "fast": "\033[92m",  # green
    "default": "\033[94m",  # blue
    "thinker": "\033[95m",  # magenta
    "agent": "\033[93m",  # yellow
    "vision": "\033[96m",  # cyan
}


def c(code: str, text: str) -> str:
    """Wrap *text* in ANSI escape *code* and append a reset."""
    return f"{code}{text}{RESET}"


def profile_tag(name: str) -> str:
    """Return a coloured ``[profile]`` badge, e.g., ``[agent]`` in yellow."""
    colour = _PROFILE_COLOUR.get(name, GREY)
    return c(colour + BOLD, f"[{name}]")


# ── Framework bootstrap ───────────────────────────────────────────────────────


def bootstrap(provider: str | None = None) -> None:
    """Reset and reload the framework config from the standard path."""
    if provider and provider != "auto":
        os.environ["AGENTFORGE_PROVIDER"] = provider
    reset_config()
    get_config(FW_CONFIG_PATH)
    if os.environ.get("SANDBOX_QUIET", "").lower() not in ("1", "true", "yes"):
        _print_active_config()


# ── Active-config banner ──────────────────────────────────────────────────────
# Profiles surfaced at the top of every sandbox script so it's obvious which
# provider + models are in use. Set SANDBOX_QUIET=1 to suppress.

_BANNER_ROLES: tuple[str, ...] = (
    "fast",
    "agent",
    "agent-heavy",
    "web-search",
    "vision",
    "local-embedding",
)


def _print_active_config() -> None:
    """Print the active provider + resolved model for the key role profiles."""
    try:
        cfg = get_config()
    except Exception as exc:
        print(c(GREY, f"(active config unavailable: {exc})"))
        return

    # _provider_override is the effective override after env+YAML merge.
    # Empty / None means "no global override — each profile keeps its own provider".
    override = getattr(cfg, "_provider_override", None) or "(none — per-profile)"
    print(c(GREY, SEP))
    print(c(BOLD, "  Active config"))
    print(c(GREY, f"    Provider override : {override}"))
    print(c(GREY, "    Profiles:"))
    for role in _BANNER_ROLES:
        try:
            p = cfg.get_profile(role)
        except Exception:
            continue
        prov = (p.provider or "ollama").lower()
        prov_tag = c(CYAN, prov) if prov == "bedrock" else c(GREEN, prov)
        print(c(GREY, f"      {role:<18} ") + prov_tag + c(GREY, f"  {p.model}"))
    print(c(GREY, SEP))


def resolution_chain(profile_name: str) -> list[str]:
    """Walk the inheritance chain for *profile_name* and return one
    human-readable hop string per step. Mirrors :func:`_resolve_profile` so
    active provider overrides (AGENTFORGE_PROVIDER / ai.provider_override) show
    up as explicit redirects in the chain.

    Returns a list like::

        ["cloud-heavy",
         "mistral-large  (parent → override deepinfra-qwen3-5-397b-a17b)",
         "deepinfra-qwen3-5-397b-a17b  model=Qwen/Qwen3.5-397B-A17B"]
    """
    try:
        cfg = get_config()
    except Exception as exc:
        return [f"(config unavailable: {exc})"]

    profiles_raw = cfg.get("ai.profiles") or {}
    override_map = getattr(cfg, "_provider_override_map", {}) or {}
    override_active = bool(getattr(cfg, "_provider_override", None))

    hops: list[str] = []
    visited: set[str] = set()
    current: str | None = profile_name
    first_hop = True

    while current and current not in visited:
        visited.add(current)
        data = profiles_raw.get(current) or {}

        # First-hop direct-select override: --profile points at an abstract
        # model profile that itself sits in the override map.
        if first_hop and override_active and current in override_map:
            mapped = override_map[current]
            hops.append(f"{current}  [override -> {mapped}]")
            current = mapped
            first_hop = False
            continue

        parent = data.get("profile")
        if override_active and parent and parent in override_map and current not in override_map:
            mapped = override_map[parent]
            hops.append(f"{current}  (parent {parent} -> override {mapped})")
            current = mapped
            first_hop = False
            continue

        model = data.get("model")
        if model and not parent:
            hops.append(f"{current}  model={model}")
        else:
            hops.append(current)
        current = parent
        first_hop = False

    return hops


def print_resolution_chain(profile_name: str) -> None:
    """Print the profile resolution chain in grey, one hop per line."""
    hops = resolution_chain(profile_name)
    print(c(GREY, "  resolution chain:"))
    for hop in hops:
        print(c(GREY, f"    -> {hop}"))


def make_registry(print_total: bool = True):
    """Create, populate, and return a :class:`ToolRegistry`."""
    registry = ToolRegistry()
    total = register_all_tools(registry)
    if print_total:
        print(c(GREY, f"Tool registry: {total} tools registered"))
    return registry


# ── Burst-script helpers ──────────────────────────────────────────────────────
# test_burst.py, test_burst_continuous.py and test_burst_content.py share the
# same setup dance: parse args, bootstrap, build an AIClient, disable model
# reasoning, print a banner. These helpers hold that shared logic so each script
# only defines its own prompts and per-call loop.

# Full backend list the burst scripts accept on --provider. add_common_args'
# default list is the narrower auto/ollama/bedrock.
WIDE_PROVIDERS: tuple[str, ...] = (
    "auto",
    "ollama",
    "bedrock",
    "copilot",
    "deepinfra",
    "groq",
    "openrouter",
)

# Per-provider request keys that turn OFF model reasoning/thinking, so burst
# timings measure the answer rather than hidden chain-of-thought tokens. Merged
# into the resolved profile's extra_body (the framework's request escape hatch).
# bedrock / copilot need no entry — their cloud-light models don't reason by
# default. DeepInfra's key is model-dependent; verify via the completion= count.
NO_REASONING_BODY: dict[str, dict] = {
    "ollama": {"think": False},
    "openrouter": {"reasoning": {"enabled": False}},
    "deepinfra": {"chat_template_kwargs": {"enable_thinking": False}},
    "groq": {"reasoning_effort": "none"},
}

# DeepInfra strict-validates `chat_template_kwargs`. Only models whose Jinja
# chat template exposes an `enable_thinking` variable accept it -- that's
# Qwen3 and the GLM-4.x/5.x family. Sending it to Mistral / Llama / Gemma /
# Anthropic models returns HTTP 400. Prefix-match the model id against this
# list and skip the kwarg if it's known not to reason by default.
_DEEPINFRA_NO_THINKING_PREFIXES = (
    "mistralai/",
    "meta-llama/",
    "google/gemma",
    "anthropic/",
    "bytedance/",
)


def disable_reasoning(ai: AIClient) -> bool:
    """Turn off model reasoning for *ai*'s resolved provider.

    Merges the provider-specific keys from NO_REASONING_BODY into the profile's
    extra_body. Returns True when a toggle was applied, False when the provider
    has none, or when the resolved model on DeepInfra is in a non-reasoning
    family (skipping the toggle avoids a strict-validation 400).
    """
    provider = (ai.profile.provider or "").lower()
    body = NO_REASONING_BODY.get(provider)
    if not body:
        return False
    if provider == "deepinfra" and "chat_template_kwargs" in body:
        model = (ai.profile.model or "").lower()
        if any(model.startswith(prefix) for prefix in _DEEPINFRA_NO_THINKING_PREFIXES):
            return False
    ai.profile.extra_body = {**ai.profile.extra_body, **body}
    return True


def make_burst_client(args, *, no_reasoning: bool = True) -> tuple[AIClient, bool]:
    """Bootstrap the framework and build an AIClient from parsed *args*.

    Collapses the bootstrap + AIClient construction every burst script repeats.
    Unless *no_reasoning* is False, also disables model reasoning for the
    resolved provider. Returns ``(ai, reasoning_disabled)``.
    """
    bootstrap(provider=args.provider)
    ai = AIClient(profile=args.profile)
    reasoning_disabled = disable_reasoning(ai) if no_reasoning else False
    return ai, reasoning_disabled


def print_burst_header(args, ai: AIClient, title: str, subtitle: str, reasoning_disabled: bool) -> None:
    """Print the standard burst-script banner.

    Layout: *title* + profile/model, the profile resolution chain, the reasoning
    state, then *subtitle* describing the run shape.
    """
    print(c(BOLD, f"{title}  -  profile: {args.profile}  model: {ai.profile.model}"))
    print_resolution_chain(args.profile)
    state = "disabled" if reasoning_disabled else f"no toggle for provider '{ai.profile.provider}'"
    print(c(GREY, f"reasoning: {state}"))
    print(c(GREY, subtitle))
    print(SEP)


# ── Common argparse arguments ─────────────────────────────────────────────────


def add_common_args(
    parser,
    *,
    default_profile: str = "agent",
    with_list: bool = False,
    with_provider: bool = True,
    wide_provider: bool = False,
    with_all_provider: bool = False,
    with_provider_exclude: bool = False,
) -> None:
    """Add ``--profile`` and ``--verbose`` to *parser*."""
    parser.add_argument(
        "--profile",
        default=default_profile,
        help=f"AI profile to use (default: {default_profile})",
    )
    if with_provider:
        base = list(WIDE_PROVIDERS) if wide_provider else ["auto", "ollama", "bedrock"]
        valid_names = set(base) | ({"all"} if with_all_provider else set())

        if with_all_provider:
            # Comma-separated lists + "all" — use type= validator instead of choices=.
            def _provider_arg(s: str) -> str:
                parts = [p.strip().lower() for p in s.split(",") if p.strip()]
                if not parts:
                    raise argparse.ArgumentTypeError("--provider must not be empty")
                if "all" in parts and len(parts) > 1:
                    raise argparse.ArgumentTypeError("--provider 'all' cannot be combined with other values")
                bad = [p for p in parts if p not in valid_names]
                if bad:
                    raise argparse.ArgumentTypeError(
                        f"unknown provider(s): {', '.join(bad)}. Choose from: {', '.join(sorted(valid_names))}"
                    )
                return s.lower()

            parser.add_argument(
                "--provider",
                default="auto",
                type=_provider_arg,
                metavar="PROVIDER[,PROVIDER...]|all",
                help=(
                    "Force provider(s) for this run. Single name, "
                    "comma-separated list, or 'all' to sweep. "
                    f"Choices: {', '.join(sorted(valid_names))}."
                ),
            )
        else:
            parser.add_argument(
                "--provider",
                default="auto",
                choices=base,
                help="Force provider for this run (auto = YAML default): " + ", ".join(base),
            )

        if with_provider_exclude:
            parser.add_argument(
                "--exclude",
                default=None,
                metavar="PROVIDER[,PROVIDER...]",
                help="Comma-separated providers to skip (useful with --provider all).",
            )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed per-step output",
    )
    if with_list:
        parser.add_argument(
            "--list",
            action="store_true",
            help="List available cases/scenarios and exit",
        )


# ── Run-wide token tracker (auto-applied to every sandbox script) ─────────────
# Sandbox scripts don't run inside a chat session — each one calls AIClient.chat
# directly, no SQLite history, no cumulative state. But every ChatResponse
# carries prompt/completion/cache_read/cache_write token counts; this module
# monkey-patches AIClient.chat once on import so every sandbox script gets a
# free run-wide total printed at the end of `print_summary()`.
#
# Set SANDBOX_NO_TOKEN_TRACK=1 to disable the patch entirely.

from dataclasses import dataclass


@dataclass
class TokenTotals:
    """Cumulative token counts across every chat call in the current process."""

    prompt: int = 0
    completion: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0
    # Track the model name from the most recent chat call so cost estimation
    # can pick the right pricing tier even when multiple models are in play.
    last_model: str = ""

    def record(self, response) -> None:
        """Add one ChatResponse worth of tokens to the running totals."""
        self.prompt += int(getattr(response, "prompt_tokens", 0) or 0)
        self.completion += int(getattr(response, "completion_tokens", 0) or 0)
        self.cache_read += int(getattr(response, "cache_read_tokens", 0) or 0)
        self.cache_write += int(getattr(response, "cache_write_tokens", 0) or 0)
        self.calls += 1
        model = getattr(response, "model", "") or ""
        if model:
            self.last_model = model


_RUN_TOKENS = TokenTotals()


def _install_token_patch() -> None:
    """Monkey-patch AIClient.chat once so every sandbox script tracks tokens.

    Idempotent — the wrapper sets a sentinel attribute and skips re-patching.
    """
    if os.environ.get("SANDBOX_NO_TOKEN_TRACK", "").lower() in ("1", "true", "yes"):
        return
    try:
        from agentforge.client import AIClient
    except ImportError:
        return  # framework not on sys.path yet — silently skip

    if getattr(AIClient.chat, "_sandbox_token_tracked", False):
        return  # already patched

    _original_chat = AIClient.chat

    def _instrumented_chat(self, *args, **kwargs):
        resp = _original_chat(self, *args, **kwargs)
        try:
            # Streaming chat returns a generator, not a ChatResponse — skip those.
            if hasattr(resp, "prompt_tokens"):
                _RUN_TOKENS.record(resp)
        except Exception:
            pass  # never let tracking break a real chat call
        return resp

    _instrumented_chat._sandbox_token_tracked = True  # type: ignore[attr-defined]
    AIClient.chat = _instrumented_chat


_install_token_patch()


_TOKENS_PRINTED = False


def print_tokens(totals: TokenTotals | None = None) -> None:
    """Print the compact token-usage line. Called automatically by print_summary()
    and by an atexit hook (so scripts that bypass print_summary still get the line).
    Idempotent — only prints once per process."""
    global _TOKENS_PRINTED
    if _TOKENS_PRINTED:
        return
    t = totals if totals is not None else _RUN_TOKENS
    if t.calls == 0:
        return  # nothing to report

    # Colour key matches test_bedrock_cache.py: green = read (free win),
    # yellow = write (one-time cost).
    parts = [
        f"prompt={t.prompt:,}",
        f"completion={t.completion:,}",
        c(GREEN, f"cache_rd={t.cache_read:,}"),
        c(YELLOW, f"cache_wr={t.cache_write:,}"),
    ]
    print(c(GREY, "Tokens: ") + "  ".join(parts) + c(GREY, f"  | calls={t.calls}"))
    _TOKENS_PRINTED = True


# Make sure the token line shows up at the bottom of every sandbox script,
# even ones that don't call print_summary() (e.g., test_parallel_meta_compare).
# atexit fires on normal interpreter shutdown and sys.exit() calls.
import atexit

atexit.register(print_tokens)


# ── Result summary ────────────────────────────────────────────────────────────


def print_summary(passed: int, failed: int, elapsed: float) -> None:
    """Print a coloured pass/fail summary block + the run's token totals."""
    colour = GREEN if failed == 0 else RED
    print("=" * 72)
    print(c(BOLD + colour, f"Results: {passed} passed, {failed} failed  ({elapsed:.1f}s total)"))
    print("=" * 72)
    print_tokens()


# ── Shared system-prompt rules ────────────────────────────────────────────────
# Sandbox-wide brevity guidance. Models default to verbose, hand-holding answers
# that bury the actual result. Append these rules to any sandbox script's
# system prompt to get a terse, demo-ready response.
#
# Scripts that genuinely need verbose structured output (e.g.,
# test_parallel_meta_compare) should NOT apply this and define their own
# format requirements instead.

BREVITY_RULES = """\
Final answer style:
  - Lead with the answer in one short sentence.
  - Add at most 2-3 lines of supporting detail.
  - No preamble ("Here is...", "I'll help you..."), no closing offers
    ("Would you like me to...").
  - Use lists or tables only when the user explicitly asked for multiple items.
  - Don't restate what the user asked.
"""


def brief(system_prompt: str) -> str:
    """Return *system_prompt* with the sandbox brevity rules appended.

    Usage::

        system = conf.brief(textwrap.dedent('''\\
            You are a helpful assistant with access to tools.
            ...
        '''))
    """
    return f"{system_prompt.rstrip()}\n\n{BREVITY_RULES}"


# ── Markdown rendering ────────────────────────────────────────────────────────


def render_md(text: str) -> None:
    """Print *text* nicely rendered as Markdown in the terminal.

    Uses :mod:`rich` if available — headings get colour, tables align, lists
    indent — otherwise falls back to a plain ``print``. The original markdown
    string is unchanged; only the terminal display is styled. The web UI
    (Agent Chat / Worklog) renders the same markdown to HTML on its own.
    """
    if not text:
        return
    try:
        from rich.console import Console
        from rich.markdown import Markdown

        Console().print(Markdown(text))
    except ImportError:
        print(text)
