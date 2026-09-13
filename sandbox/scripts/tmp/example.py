"""Cloud storage tools — Put.io and Premiumize.me management.

Provides tools for browsing, searching, downloading, and transferring files
on Put.io and Premiumize.me cloud storage services.

Configuration (config.yaml or environment variables)::

    cloud:
      putio:
        oauth_token: "YOUR_PUTIO_TOKEN"   # or PUTIO_TOKEN env var
      premiumize:
        api_key: "YOUR_PM_KEY"            # or PREMIUMIZE_API_KEY env var

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.cloud_tools import register_cloud_tools

    registry = ToolRegistry()
    register_cloud_tools(registry)
"""

from __future__ import annotations

import json
import os
import re
import ssl
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode, urlparse
from urllib.request import Request, urlopen

import yaml
from chalkbox.logging.bridge import get_logger

from agentforge.tools.registry import tool

if TYPE_CHECKING:
    from agentforge.tools.registry import ToolRegistry

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PUTIO_BASE = "https://api.put.io/v2"
_PM_BASE = "https://www.premiumize.me/api"
_REQUEST_TIMEOUT = 20  # seconds

# Absolute path to config.yaml — same resolution strategy as
# app/config.py so this works regardless of the worker process's CWD.
# cloud_tools.py lives at agentforge/tools/cloud_tools.py
# parents[1] = project root
_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

# Module-level cache — loaded once per worker process lifetime.
_config_cache: dict | None = None


def _get_config() -> dict:
    """Load and cache config.yaml using an absolute path.

    Uses the same absolute-path strategy as ``app/config.py`` so it works
    regardless of the worker process's working directory.
    """
    global _config_cache
    if _config_cache is None:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH) as fh:
                _config_cache = yaml.safe_load(fh) or {}
            logger.debug("cloud_tools: loaded config from %s", _CONFIG_PATH)
        else:
            logger.warning("cloud_tools: config.yaml not found at %s", _CONFIG_PATH)
            _config_cache = {}
    return _config_cache


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_putio_token() -> str:
    """Return the Put.io OAuth token.

    Priority: config.yaml (absolute path) → PUTIO_TOKEN env var → empty string.
    """
    token = _get_config().get("cloud", {}).get("putio", {}).get("oauth_token", "")
    if token:
        return str(token)
    return os.environ.get("PUTIO_TOKEN", "")


def _get_premiumize_key() -> str:
    """Return the Premiumize.me API key.

    Priority: config.yaml (absolute path) → PREMIUMIZE_API_KEY env var → empty string.
    """
    key = _get_config().get("cloud", {}).get("premiumize", {}).get("api_key", "")
    if key:
        return str(key)
    return os.environ.get("PREMIUMIZE_API_KEY", "")
