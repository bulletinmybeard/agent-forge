"""Secret redaction engine — scans text for credentials and obfuscates them.

Uses `detect-secrets <https://github.com/Yelp/detect-secrets>`_ as the
scanning backend.  Every message destined for the LLM (via :class:`AIClient`)
and every message persisted to storage (SQLite / Qdrant) is run through
:func:`redact` which returns a cleaned copy plus a list of findings.

Architecture
~~~~~~~~~~~~
* **Singleton** — :func:`get_redactor` returns a lazily-initialised module-level
  instance so the plugin set is loaded once.
* **Pure text in / text out** — no file I/O, no side-effects.  The caller
  decides what to do with findings (emit WS warnings, log, etc.).
* **Graceful fallback** — if ``detect-secrets`` is not installed, the redactor
  becomes a transparent pass-through and logs a one-time warning.

Configuration
~~~~~~~~~~~~~
Controlled via ``config.yaml → secret_redaction`` (see :mod:`agentforge.config`):

.. code-block:: yaml

   secret_redaction:
     enabled: true
     placeholder: "[REDACTED:{type}]"
     log_findings: true
     extra_patterns:            # additional regex patterns to catch
       - name: "ConnectionString"
         pattern: "(?i)(mongodb\\+srv|postgres(?:ql)?|mysql|redis)://[^\\s\"']+"
       - name: "BearerToken"
         pattern: "(?i)bearer\\s+[a-z0-9\\-_\\.]{20,}"

Environment overrides
~~~~~~~~~~~~~~~~~~~~~
* ``SECRET_REDACTION_ENABLED`` — ``true`` / ``false``
* ``SECRET_REDACTION_PLACEHOLDER`` — e.g., ``"[SCRUBBED]"``
"""

from __future__ import annotations

import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class SecretFinding:
    """A single detected secret occurrence."""

    secret_type: str  # e.g., "Base64HighEntropyString", "AWSKeyDetector"
    secret_value: str  # the raw matched text (never logged/serialised)
    line_number: int  # 1-based line within the scanned text
    placeholder: str  # the replacement string that was inserted


@dataclass
class RedactionResult:
    """Return value of :meth:`SecretRedactor.redact`."""

    text: str  # the cleaned text with secrets replaced
    findings: list[SecretFinding] = field(default_factory=list)

    @property
    def had_secrets(self) -> bool:
        return len(self.findings) > 0


# ---------------------------------------------------------------------------
# Attempt to import detect-secrets
# ---------------------------------------------------------------------------

_DETECT_SECRETS_AVAILABLE = False
try:
    from detect_secrets.core.scan import scan_line  # type: ignore[import-untyped]
    from detect_secrets.settings import default_settings, transient_settings  # type: ignore[import-untyped]

    _DETECT_SECRETS_AVAILABLE = True
except Exception as _import_err:
    # Catch *any* exception (not just ImportError) — detect-secrets may be
    # installed but fail to import due to dependency conflicts, wrong Python
    # version, or a broken transitive import.  Log the real cause so it's
    # diagnosable instead of silently falling back.
    scan_line = None  # type: ignore[assignment]
    default_settings = None  # type: ignore[assignment]
    transient_settings = None  # type: ignore[assignment]
    # This runs at module-load time before the logger may be configured, but
    # the message will be visible in any log output that captures WARNING+.
    logger.warning(
        "Failed to import detect-secrets: %s: %s",
        type(_import_err).__name__,
        _import_err,
    )


# ---------------------------------------------------------------------------
# Built-in extra patterns (complement detect-secrets defaults)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Default detect-secrets plugin list (excludes entropy detectors)
# ---------------------------------------------------------------------------
# The default detect-secrets settings enable Base64HighEntropyString and
# HexHighEntropyString which use Shannon entropy to flag any "random-looking"
# string.  On typical YAML config / conversation text this produces 1,000+
# false positives per scan, and because each "[REDACTED:Base64 High Entropy
# String]" replacement is LONGER than the original match, the text balloons
# to millions of tokens — causing 400 "prompt too long" errors from the model.
#
# We keep only the *specific* named detectors (JWT, AWS, Slack, private keys,
# keywords, etc.) and rely on our regex patterns for connection strings,
# bearer tokens, and other secrets the named detectors miss.
_DEFAULT_DS_PLUGINS: list[dict[str, Any]] = [
    {"name": "ArtifactoryDetector"},
    {"name": "AWSKeyDetector"},
    {"name": "BasicAuthDetector"},
    {"name": "CloudantDetector"},
    {"name": "GitHubTokenDetector"},
    {"name": "IbmCloudIamDetector"},
    {"name": "IbmCosHmacDetector"},
    {"name": "JwtTokenDetector"},
    {"name": "KeywordDetector"},
    {"name": "MailchimpDetector"},
    {"name": "NpmDetector"},
    {"name": "PrivateKeyDetector"},
    {"name": "SlackDetector"},
    {"name": "SoftlayerDetector"},
    {"name": "SquareOAuthDetector"},
    {"name": "StripeDetector"},
    {"name": "TwilioKeyDetector"},
]


_BUILTIN_EXTRA_PATTERNS: list[dict[str, str]] = [
    # Connection strings — covers bare protocols AND SQLAlchemy driver variants:
    #   postgres://  postgresql+psycopg2://  mysql+pymysql://  mongodb+srv://
    #   redis://  mssql+pyodbc://  sqlite:///  mariadb+mariadbconnector://  etc.
    {
        "name": "ConnectionString",
        "pattern": (
            r"(?i)(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|mariadb|redis|mssql|sqlite)"
            r"(?:\+[a-z0-9_]+)?://[^\s\"'`]+"
        ),
    },
    # Slack tokens (bot, user, app-level, refresh, session)
    #   xoxb-  (bot token)      xoxp-  (user token)
    #   xoxa-  (app token)      xoxr-  (refresh token)
    #   xoxs-  (session token)  xapp-  (app-level token, newer format)
    {
        "name": "SlackToken",
        "pattern": r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b|\bxapp-[a-zA-Z0-9\-]{10,}\b",
    },
    # Slack incoming-webhook URLs — full path leaks the workspace + secret token
    {
        "name": "SlackWebhook",
        "pattern": r"https://hooks\.slack\.com/services/T[A-Za-z0-9/]+",
    },
    # Bearer tokens in Authorization headers
    {
        "name": "BearerToken",
        "pattern": r"(?i)bearer\s+(?P<value>[a-z0-9\-_\.]{20,})",
    },
    {
        "name": "GenericKeyAssignment",
        "pattern": (
            r"(?i)(?:api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?token"
            r"|auth[_-]?token|oauth[_-]?token|private[_-]?key|client[_-]?secret|client[_-]?id"
            r"|bot[_-]?token|app[_-]?token|session[_-]?token"
            r"|refresh[_-]?token|signing[_-]?key|encryption[_-]?key"
            r"|consumer[_-]?key|consumer[_-]?secret|account[_-]?sid|webhook[_-]?url)"
            r"""\s*[:=]\s*['"](?P<value>[^\s'"]{8,})['"]"""
        ),
    },
    # PEM private key blocks — RSA / EC / OPENSSH / DSA / PGP / generic.
    # DOTALL so the whole multi-line block between the BEGIN/END markers is
    # masked as one match.
    {
        "name": "PrivateKey",
        "pattern": (
            r"(?s)-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
        ),
    },
    # Google OAuth client secrets (GOCSPX-…) — used in OAuth client configs.
    {
        "name": "GoogleOAuthClientSecret",
        "pattern": r"\bGOCSPX-[A-Za-z0-9_-]+",
    },
    {
        "name": "GoogleOAuthClientID",
        "pattern": r"\b\d{6,}-[a-z0-9]{12,}\.apps\.googleusercontent\.com\b",
    },
    {
        "name": "CloudflareAccessClientID",
        "pattern": r"\b[0-9a-f]{32}\.access\b",
    },
    # AWS-style access key IDs (AKIA…)
    {
        "name": "AWSAccessKeyID",
        "pattern": r"\bAKIA[0-9A-Z]{16}\b",
    },
    # AWS secret access keys — 40-char base64-ish value in an assignment.
    # Keyed off common variable names to avoid masking unrelated 40-char blobs.
    {
        "name": "AWSSecretAccessKey",
        "pattern": (
            r"(?i)aws[_-]?secret[_-]?access[_-]?key"
            r"""\s*[:=]\s*['"]?(?P<value>[A-Za-z0-9/+=]{40})['"]?"""
        ),
    },
    # AWS STS session tokens — Base64 blobs starting with the JSON prefix
    # {"orig  →  IQoJb3JpZ2lu  in Base64.  Typically 400–1200 chars.
    {
        "name": "AWSSessionToken",
        "pattern": r"IQoJb3JpZ2lu[A-Za-z0-9+/=]{50,}",
    },
    # GitHub personal access tokens (ghp_, gho_, ghs_, ghu_, github_pat_)
    {
        "name": "GitHubToken",
        "pattern": r"\b(ghp_[a-zA-Z0-9]{20,}|gho_[a-zA-Z0-9]{20,}|ghs_[a-zA-Z0-9]{20,}"
        r"|ghu_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{22,})\b",
    },
    # Google OAuth access tokens (ya29.…) — short-lived bearer tokens used in
    # Authorization headers against googleapis.com. Typically 100–200 chars.
    {
        "name": "GoogleOAuthAccessToken",
        "pattern": r"\bya29\.[A-Za-z0-9_\-]{20,}\b",
    },
    # Google OAuth refresh tokens (1//…) — long-lived, stored in gmail_token.json.
    # Base64-url alphabet; typical length 40–110 chars.
    {
        "name": "GoogleOAuthRefreshToken",
        "pattern": r"\b1//[0-9A-Za-z_\-]{30,}\b",
    },
]


# ---------------------------------------------------------------------------
# SecretRedactor
# ---------------------------------------------------------------------------


class SecretRedactor:
    """Scans arbitrary text for secrets and returns a redacted copy.

    Thread-safe: all mutable state is behind a lock.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        placeholder: str = "[REDACTED:{type}]",
        log_findings: bool = True,
        extra_patterns: list[dict[str, str]] | None = None,
        detect_secrets_plugins: list[dict[str, Any]] | None = None,
        detect_high_entropy: bool = True,
        high_entropy_min_length: int = 20,
        high_entropy_min_entropy: float = 3.5,
    ) -> None:
        self._enabled = enabled
        self._placeholder_template = placeholder
        self._log_findings = log_findings
        self._lock = threading.Lock()

        self._detect_high_entropy = detect_high_entropy
        self._he_min_entropy = high_entropy_min_entropy
        self._he_run_re = re.compile(r"[A-Za-z0-9]{%d,}" % max(8, int(high_entropy_min_length)))

        # Compile extra regex patterns (built-in + user-supplied)
        all_extra = list(_BUILTIN_EXTRA_PATTERNS)
        if extra_patterns:
            all_extra.extend(extra_patterns)
        self._extra_patterns: list[tuple[str, re.Pattern[str]]] = []
        for pat in all_extra:
            try:
                compiled = re.compile(pat["pattern"])
                self._extra_patterns.append((pat["name"], compiled))
            except re.error as exc:
                logger.warning(
                    "Invalid extra secret pattern %r: %s",
                    pat.get("name", "?"),
                    exc,
                )

        # detect-secrets plugin config — use our curated list (no entropy
        # detectors) unless the caller explicitly overrides.
        self._ds_plugins = detect_secrets_plugins if detect_secrets_plugins is not None else _DEFAULT_DS_PLUGINS

        if not _DETECT_SECRETS_AVAILABLE:
            logger.warning(
                "detect-secrets is not installed — secret redaction will use "
                "regex-only fallback.  Install with: pip install detect-secrets",
            )

    # -- public API ---------------------------------------------------------

    def redact(self, text: str) -> RedactionResult:
        """Scan *text* for secrets and return a :class:`RedactionResult`.

        If the redactor is disabled or *text* is empty, returns the input
        unchanged with no findings.
        """
        if not self._enabled or not text:
            return RedactionResult(text=text)

        findings: list[SecretFinding] = []

        # Phase 1: detect-secrets line-by-line scan
        if _DETECT_SECRETS_AVAILABLE:
            findings.extend(self._scan_with_detect_secrets(text))

        # Phase 2: extra regex patterns (catch things detect-secrets misses)
        findings.extend(self._scan_with_regex(text))

        # Phase 3: high-entropy catch-all (unknown-format tokens under any key)
        if self._detect_high_entropy:
            findings.extend(self._scan_high_entropy_runs(text))

        if not findings:
            return RedactionResult(text=text)

        # Deduplicate by (line, value) — the SAME value found on different lines
        seen: set[tuple[int, str]] = set()
        unique_findings: list[SecretFinding] = []
        for f in findings:
            key = (f.line_number, f.secret_value)
            if key not in seen:
                seen.add(key)
                unique_findings.append(f)

        # Apply replacements (longest match first to avoid partial overlaps)
        lines = text.splitlines(keepends=True)
        line_start: list[int] = []
        _off = 0
        for _ln in lines:
            line_start.append(_off)
            _off += len(_ln)

        spans: list[tuple[int, int, str]] = []
        for f in unique_findings:
            val = f.secret_value
            if "\n" in val:
                idx = text.find(val)  # multi-line (PEM): long + unique
            else:
                li = f.line_number - 1
                col = lines[li].find(val) if 0 <= li < len(lines) else -1
                idx = line_start[li] + col if col >= 0 else text.find(val)
            if idx >= 0:
                spans.append((idx, idx + len(val), f.placeholder))

        # Earliest first, longest first on ties; skip any span overlapping one
        # already accepted (the outermost/longest wins).
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        out: list[str] = []
        cursor = 0
        for start, end, placeholder in spans:
            if start < cursor:
                continue  # inside an already-redacted span
            out.append(text[cursor:start])
            out.append(placeholder)
            cursor = end
        out.append(text[cursor:])
        cleaned = "".join(out)

        if self._log_findings:
            types = ", ".join(f.secret_type for f in unique_findings)
            logger.warning(
                "Redacted %d secret(s) from text (%d chars): [%s]",
                len(unique_findings),
                len(text),
                types,
            )

        return RedactionResult(text=cleaned, findings=unique_findings)

    def redact_messages(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[SecretFinding]]:
        """Redact secrets from a list of chat messages (``{"role": …, "content": …}``).

        Returns a new list (shallow copy) with redacted content, plus the
        aggregated findings from all messages.
        """
        if not self._enabled:
            return messages, []

        all_findings: list[SecretFinding] = []
        cleaned_messages: list[dict[str, Any]] = []

        for msg in messages:
            msg_copy = msg.copy()
            content = msg_copy.get("content")
            if isinstance(content, str) and content:
                result = self.redact(content)
                msg_copy["content"] = result.text
                all_findings.extend(result.findings)
            cleaned_messages.append(msg_copy)

        return cleaned_messages, all_findings

    # -- detect-secrets scanning --------------------------------------------

    def _scan_with_detect_secrets(self, text: str) -> list[SecretFinding]:
        """Run detect-secrets ``scan_line`` on each line of *text*."""
        findings: list[SecretFinding] = []
        lines = text.splitlines()

        ctx = transient_settings({"plugins_used": self._ds_plugins}) if self._ds_plugins else default_settings()

        with self._lock:
            with ctx:
                for line_num, line in enumerate(lines, 1):
                    if not line.strip():
                        continue
                    try:
                        for potential_secret in scan_line(line):
                            secret_val = potential_secret.secret_value
                            if secret_val:
                                placeholder = self._make_placeholder(potential_secret.type)
                                findings.append(
                                    SecretFinding(
                                        secret_type=potential_secret.type,
                                        secret_value=secret_val,
                                        line_number=line_num,
                                        placeholder=placeholder,
                                    ),
                                )
                    except Exception as exc:
                        # Fail-open at line granularity: this line is NOT scanned by
                        # detect-secrets, so a secret it would have caught could pass
                        # through. The regex scanner (_scan_with_regex) still runs over
                        # the full text as a backstop. Log loudly so it's noticed.
                        logger.warning("detect-secrets scan error on line %d (line unscanned): %s", line_num, exc)

        return findings

    # -- regex fallback scanning --------------------------------------------

    def _scan_with_regex(self, text: str) -> list[SecretFinding]:
        """Run extra regex patterns against the text.

        Patterns compiled with ``re.DOTALL`` (e.g., multi-line PEM blocks) are
        run against the full text so they can span line breaks; all others are
        scanned line-by-line to keep accurate 1-based line numbers.
        """
        findings: list[SecretFinding] = []
        lines = text.splitlines()

        for name, pattern in self._extra_patterns:
            # Assignment-style patterns expose a `value` group so we redact only
            # the secret value, not the key name / prefix that precedes it.
            has_value = "value" in pattern.groupindex
            if pattern.flags & re.DOTALL:
                for match in pattern.finditer(text):
                    matched = match.group("value") if has_value else match.group(0)
                    line_num = text.count("\n", 0, match.start()) + 1
                    findings.append(
                        SecretFinding(
                            secret_type=name,
                            secret_value=matched,
                            line_number=line_num,
                            placeholder=self._make_placeholder(name),
                        ),
                    )
                continue
            for line_num, line in enumerate(lines, 1):
                for match in pattern.finditer(line):
                    matched = match.group("value") if has_value else match.group(0)
                    placeholder = self._make_placeholder(name)
                    findings.append(
                        SecretFinding(
                            secret_type=name,
                            secret_value=matched,
                            line_number=line_num,
                            placeholder=placeholder,
                        ),
                    )

        return findings

    # -- high-entropy catch-all ---------------------------------------------

    @staticmethod
    def _shannon_entropy(s: str) -> float:
        n = len(s)
        if n == 0:
            return 0.0
        return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())

    @staticmethod
    def _char_classes(s: str) -> int:
        """How many of {lowercase, uppercase, digit} appear — a token usually has >=2."""
        return sum(
            (
                bool(re.search(r"[a-z]", s)),
                bool(re.search(r"[A-Z]", s)),
                bool(re.search(r"[0-9]", s)),
            )
        )

    def _scan_high_entropy_runs(self, text: str) -> list[SecretFinding]:
        """Flag contiguous random-looking runs (unknown-format tokens, any key).

        Catches secrets that don't match a known signature and sit under a key
        name we don't recognise — the residual the keyword/signature layers miss.
        Requires a long contiguous run (no separators) with >=2 character classes
        and high entropy, which excludes word/separator identifiers and prose.
        """
        findings: list[SecretFinding] = []
        placeholder = self._make_placeholder("HighEntropyString")
        for line_num, line in enumerate(text.splitlines(), 1):
            for match in self._he_run_re.finditer(line):
                run = match.group(0)
                start, end = match.start(), match.end()
                # Skip path/URL segments: a run flanked by '/' is part of a
                # filesystem path or URL, not a secret. Redacting it would corrupt
                # the path the agent passes to tools (read_file, find_files, ...).
                if (start > 0 and line[start - 1] == "/") or (end < len(line) and line[end] == "/"):
                    continue
                if self._char_classes(run) >= 2 and self._shannon_entropy(run) >= self._he_min_entropy:
                    findings.append(
                        SecretFinding(
                            secret_type="HighEntropyString",
                            secret_value=run,
                            line_number=line_num,
                            placeholder=placeholder,
                        ),
                    )
        return findings

    # -- helpers ------------------------------------------------------------

    def _make_placeholder(self, secret_type: str) -> str:
        """Format the placeholder template with the secret type."""
        return self._placeholder_template.replace("{type}", secret_type)


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_instance: SecretRedactor | None = None
_init_lock = threading.Lock()


def get_redactor() -> SecretRedactor:
    """Return the module-level :class:`SecretRedactor` singleton.

    Lazily initialised from ``config.yaml → secret_redaction`` on first call.
    """
    global _instance
    if _instance is not None:
        return _instance

    with _init_lock:
        if _instance is not None:
            return _instance

        # Read config
        enabled = os.getenv("SECRET_REDACTION_ENABLED", "").lower()
        if enabled in ("0", "false", "no"):
            _instance = SecretRedactor(enabled=False)
            logger.info("Secret redaction disabled via environment")
            return _instance

        try:
            from .config import get_config

            cfg = get_config()
            sr_cfg = cfg.get("secret_redaction") or {}
            if isinstance(sr_cfg, dict) and sr_cfg:
                _instance = SecretRedactor(
                    enabled=sr_cfg.get("enabled", True),
                    placeholder=sr_cfg.get("placeholder", "[REDACTED:{type}]"),
                    log_findings=sr_cfg.get("log_findings", True),
                    extra_patterns=sr_cfg.get("extra_patterns"),
                    detect_secrets_plugins=sr_cfg.get("detect_secrets_plugins"),
                    detect_high_entropy=sr_cfg.get("detect_high_entropy", True),
                    high_entropy_min_length=sr_cfg.get("high_entropy_min_length", 20),
                    high_entropy_min_entropy=sr_cfg.get("high_entropy_min_entropy", 3.5),
                )
            else:
                _instance = SecretRedactor()
        except Exception as exc:
            logger.warning("Failed to load secret_redaction config: %s — using defaults", exc)
            _instance = SecretRedactor()

        if _instance._enabled:
            ds_plugin_names = [p.get("name", "?") for p in (_instance._ds_plugins or [])]
            logger.info(
                "Secret redaction initialised (detect-secrets=%s, ds_plugins=%d [%s], extra_patterns=%d)",
                "available" if _DETECT_SECRETS_AVAILABLE else "unavailable",
                len(ds_plugin_names),
                ", ".join(ds_plugin_names) if ds_plugin_names else "defaults",
                len(_instance._extra_patterns),
            )

        return _instance


def reset_redactor() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    with _init_lock:
        _instance = None
