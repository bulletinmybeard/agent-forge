"""CommandGuard — LLM-powered safety classifier for shell commands.

Before the ``shell`` tool executes a command, the guard asks a small, fast
model whether the command is **destructive** (could cause data loss, system
damage, or security issues).  If the model says *yes*, the guard triggers the
registry's existing confirmation flow so the user can approve or reject.

Design principles:
  - Zero-config by default — works out of the box with sensible defaults.
  - Fast-path allowlist: obviously safe commands (version checks, reads) skip
    the LLM entirely.
  - The LLM prompt is tuned to err on the side of caution (false positives
    over false negatives).
  - Configurable via ``config.yaml → tools.shell.guard``.

Usage::

    from agentforge.tools.command_guard import get_guard

    guard = get_guard()
    if guard.is_destructive("rm -rf ~/projects"):
        # trigger confirmation ...

"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Fast-path: commands that are ALWAYS safe (skip LLM entirely)
# ---------------------------------------------------------------------------

_SAFE_PATTERNS = re.compile(
    r"^("
    # Version / info checks
    r"(npm|npx|node|python|python3|pip|pipx|poetry|cargo|make|cmake|"
    r"git|ruby|go|java|javac|rustc|gcc|g\+\+|clang|swift|php|perl|"
    r"docker|docker-compose|kubectl|terraform|aws|gcloud|az|n)\s+"
    r"(--version|-v|-V|version)|"
    # Read-only commands
    r"(ls|ll|la|dir|pwd|whoami|hostname|uname|date|cal|uptime|which|"
    r"where|type|file|wc|head|tail|cat|less|more|bat|"
    r"echo|printf|env|printenv|set|export|"
    r"df|du|free|top|htop|ps|lsof|netstat|ss|"
    r"ifconfig|ip|ping|dig|nslookup|host|traceroute|curl\s+.*-I|"
    r"git\s+(status|log|diff|branch|show|remote|tag|stash\s+list)|"
    r"docker\s+(ps|images|inspect|stats|logs|info|version)|"
    r"npm\s+(ls|list|outdated|view|info|search|audit)|"
    r"pip\s+(list|show|freeze|check)|"
    r"poetry\s+(show|check|env\s+info)|"
    r"cargo\s+(check|clippy|test|doc)|"
    r"find|grep|rg|ag|fd|fzf|tree|"
    r"jq|yq|sed\s+-n|awk)\b"
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Deterministic DESTRUCTIVE backstop (model-independent)
# ---------------------------------------------------------------------------
# Catastrophic patterns the LLM (especially a small `fast` model) can miss —
# e.g., a destructive clause buried in a long, read-only-looking compound. Checked
# BEFORE the SAFE fast-path and the LLM, and uses `search` (not anchored) so a
# safe-looking prefix (`ls && docker system prune`) can't shortcut past it. Holds
# even on fail-open. Boundary class keeps `confirm -f` etc. from matching `rm`.

_DESTRUCTIVE_PATTERNS = re.compile(
    r"(?:^|[\s;&|`$(])("
    r"rm\s+-[a-z]*[rf]"  # rm -rf / -r / -f (short flags)
    r"|rm\s[^\n]*--(?:recursive|force)\b"  # rm --recursive / --force (long flags)
    r"|find\b[^\n]*\s-delete\b"  # find ... -delete (rm bypass)
    r"|find\b[^\n]*-exec\s+rm\b"  # find ... -exec rm
    r"|mkfs\b|shred\b|fdisk\b|parted\b|truncate\s+-s\s*0"
    r"|dd\s+[^\n]*\bof=/dev/|>\s*/dev/(sd|nvme|disk)"  # write raw device
    r"|>{1,2}\s*/(?:etc|boot|bin|sbin|lib|sys)/"  # overwrite/append into system dirs
    r"|ch(?:mod|own)\s+-[a-zA-Z]*R[a-zA-Z]*\b[^\n]*\s/(?:etc|usr|bin|boot|lib|sys|var|root)\b"  # recursive chmod/chown on system paths
    r"|chmod\s+-[a-zA-Z]*R[a-zA-Z]*\b[^\n]*\b0?777\b"  # recursive chmod 777
    r"|mv\s[^\n]*\s/dev/null\b"  # mv into /dev/null (data loss)
    r"|docker(?:-|\s+)compose\b[^\n]*\bdown\b[^\n]*(?:-v\b|--volumes)"  # down -v (any flags between)
    r"|docker\s+(?:system\s+)?prune\b"  # docker (system) prune
    r"|docker\s+(?:volume|image|container|network)\s+(?:rm|prune)\b"
    r"|docker\s+rmi\b|docker\s+rm\s+-[a-z]*f"  # rmi / rm -f
    r"|git\s+(?:reset\s+--hard|clean\s+-[a-z]*f|push\s+[^\n]*--force)"
    r"|sed\s+-[a-z]*i"  # sed -i / -i.bak (in-place file edit)
    r"|perl\s+-[a-z]*[pi][a-z]*\s+-[a-z]*[pi]"  # perl -pi (in-place)
    r"|:\(\)\s*\{.*\}\s*;\s*:"  # fork bomb
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_guard_config() -> dict:
    """Return guard config from config.yaml → tools.shell.guard."""
    try:
        from agentforge.config import get_config

        cfg = get_config()
        return cfg._raw.get("tools", {}).get("shell", {}).get("guard", {})
    except Exception:
        return {}


def _is_enabled() -> bool:
    """Check if the guard is enabled in config."""
    cfg = _get_guard_config()
    return cfg.get("enabled", True)  # enabled by default


def _is_fast_path_enabled() -> bool:
    """Check if the fast-path regex allowlist is enabled in config."""
    cfg = _get_guard_config()
    return cfg.get("fast_path", True)  # enabled by default


def _is_fail_open() -> bool:
    """Whether to allow execution when the classifier can't reach the model.

    Defaults to False (fail CLOSED — treat as destructive so it needs confirm),
    so an attacker can't disable the guard by knocking the model offline. Set
    ``tools.shell.guard.fail_open: true`` to restore the old permissive default.
    """
    return _get_guard_config().get("fail_open", False)


def _get_model() -> str:
    """Return the model to use for guard classification."""
    cfg = _get_guard_config()
    return cfg.get("model", "")


# ---------------------------------------------------------------------------
# Classification prompt
# ---------------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "command_guard.md"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text()


# ---------------------------------------------------------------------------
# CommandGuard
# ---------------------------------------------------------------------------


class CommandGuard:
    """LLM-powered command safety classifier.

    Asks a small model whether a shell command is destructive before execution.
    Safe commands are fast-pathed without an LLM call.

    Uses ``AIClient`` under the hood so the same guard works against Ollama or
    Bedrock (whatever the ``fast`` profile resolves to).
    """

    def __init__(self, model: str = "") -> None:
        self._model_override = model or _get_model()
        self._client = None  # lazy init — holds an AIClient
        self.last_source: str = ""  # "fast-path", "llm", or "fail-open"

    def _get_client(self):
        """Lazily create an AIClient bound to a guard-tuned clone of `fast`.

        The shared profile must NEVER be mutated — it is cached in
        ConfigManager and reused by every other AIClient on the same name.
        Past versions set max_tokens=5 directly on the profile, which silently
        capped every other consumer (planner, agent, etc.) at 5 output tokens.
        We use dataclasses.replace to clone with the guard's overrides.
        """
        if self._client is None:
            from agentforge.client import AIClient
            from agentforge.config import get_config

            shared = get_config().get_profile("fast")
            guard_profile = dataclasses.replace(
                shared,
                max_tokens=5,  # guard returns one word — cap tight
                temperature=0.0,
                model=self._model_override or shared.model,
            )
            self._client = AIClient(profile=guard_profile)
        return self._client

    def classify(self, command: str) -> str:
        """Classify a command as DESTRUCTIVE, SUDO, or SAFE.

        Returns one of: "destructive", "sudo", or "safe".
        Also sets self.last_source for metadata.
        """
        if not _is_enabled():
            self.last_source = "disabled"
            return "safe"

        # Deterministic backstop: catastrophic patterns are DESTRUCTIVE without
        # trusting the model — runs before the SAFE fast-path so a safe-looking
        # prefix can't shortcut past a buried destructive clause.
        if _DESTRUCTIVE_PATTERNS.search(command):
            logger.debug("[guard] Deterministic DESTRUCTIVE: %s", command[:80])
            self.last_source = "deny-list"
            return "destructive"

        # Fast path: obviously safe commands skip the LLM
        if _is_fast_path_enabled() and _SAFE_PATTERNS.match(command.strip()):
            logger.debug("[guard] Fast-path SAFE: %s", command[:80])
            self.last_source = "fast-path"
            return "safe"

        try:
            client = self._get_client()
            logger.debug("[guard] Classifying command with %s: %s", client.profile.model, command[:120])

            response = client.chat(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Command: {command}"},
                ],
            )

            answer = (response.content or "").strip().upper()

            if "DESTRUCTIVE" in answer:
                verdict = "destructive"
            elif "SUDO" in answer:
                verdict = "sudo"
            else:
                verdict = "safe"

            self.last_source = "llm"
            logger.debug(
                "[guard] %s → %s (%s)",
                command[:80],
                verdict.upper(),
                client.profile.model,
            )
            return verdict

        except Exception as exc:
            if _is_fail_open():
                logger.warning("[guard] Classification failed (fail_open=true, allowing): %s", exc)
                self.last_source = "fail-open"
                return "safe"
            # Fail CLOSED: if the guard can't reach the model, treat as
            # destructive so the command needs confirmation rather than slipping
            # through unchecked (a stopped model must not disable the guard).
            logger.warning("[guard] Classification failed (failing closed, needs confirm): %s", exc)
            self.last_source = "fail-closed"
            return "destructive"

    def is_destructive(self, command: str) -> bool:
        """Classify a command as destructive or safe (legacy compatibility).

        Returns True if the command is potentially destructive and should
        require user confirmation before execution.
        """
        return self.classify(command) == "destructive"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_guard: CommandGuard | None = None


def get_guard() -> CommandGuard:
    """Return the module-level CommandGuard singleton."""
    global _guard
    if _guard is None:
        _guard = CommandGuard()
    return _guard
