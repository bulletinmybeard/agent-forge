"""getpass-based sudo secret provider for the interactive CLI."""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable

from .sudo_cache import SudoCredentialCache


class CliSudoProvider:
    def __init__(
        self,
        getpass_fn: Callable[[str], str] = getpass.getpass,
        isatty: Callable[[], bool] = sys.stdin.isatty,
        ttl_seconds: int = 300,
    ) -> None:
        self._getpass = getpass_fn
        self._isatty = isatty
        self._cache = SudoCredentialCache(ttl_seconds=ttl_seconds)

    def get(self, label: str) -> str | None:
        cached = self._cache.get(label)
        if cached is not None:
            return cached
        if not self._isatty():
            return None
        secret = self._getpass("Enter sudo password: ").strip()
        if not secret:
            return None
        self._cache.set(label, secret)
        return secret

    def invalidate(self, label: str) -> None:
        self._cache.invalidate(label)
