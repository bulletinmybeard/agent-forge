"""Shared config loader for connector plugins — reads from agentforge/config.yaml.

Encryption-key precedence:
- The Fernet encryption key is read from the `CONNECTOR_ENCRYPTION_KEY` env var first,
  falling back to `connectors.encryption_key` in config.yaml.
- Prefer the env var or a secret manager. Storing the key in config.yaml alongside (or
  near) the encrypted data defeats at-rest encryption if the config leaks — both the key
  and the ciphertext end up in the same place. A one-time WARNING is logged when the key
  is sourced from config.yaml.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"
_cache: dict | None = None
_config_key_warned = False


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as fh:
            _cache = yaml.safe_load(fh) or {}
    else:
        _cache = {}
    return _cache


def get_connectors_config() -> dict:
    """Return the whole ``connectors`` config block from the config.yaml."""
    return _load().get("connectors", {}) or {}


def get_connector_config(connector_type: str) -> dict:
    """Return the config dict for a specific connector type.

    Reads from config.yaml -> connectors -> <connector_type>.
    """
    return get_connectors_config().get(connector_type, {})


def get_encryption_key() -> str:
    """Return the Fernet encryption key.

    Precedence: `CONNECTOR_ENCRYPTION_KEY` env var wins over the
    `connectors.encryption_key` value in config.yaml.

    The env var (or a secret manager) is preferred. Sourcing the key from config.yaml
    defeats at-rest encryption if the config leaks, since the key then sits next to the
    encrypted data — a one-time WARNING is logged in that case.

    # TODO: derive a per-connection key from the master key, e.g.
    #   HKDF(master_key, info=connection_id), so a single leaked key doesn't expose
    #   every connection's secrets. Out of scope here (would break existing ciphertext).
    """
    global _config_key_warned
    env_key = os.environ.get("CONNECTOR_ENCRYPTION_KEY")
    if env_key:
        return env_key
    config_key = _load().get("connectors", {}).get("encryption_key", "")
    if config_key and not _config_key_warned:
        _config_key_warned = True
        logger.warning(
            "Connector encryption key sourced from config.yaml. This defeats at-rest "
            "encryption if the config leaks (key sits next to the encrypted data). "
            "Prefer the CONNECTOR_ENCRYPTION_KEY env var or a secret manager."
        )
    return config_key


def get_credentials_dir() -> Path:
    """Resolve the directory containing client_secret.json.

    Same priority as the old gmail_tools.py:
    GMAIL_CREDENTIALS_DIR env → config.yaml google.gmail.credentials_dir → ~/.agentforge/
    """
    env_dir = os.environ.get("GMAIL_CREDENTIALS_DIR")
    if env_dir:
        return Path(env_dir).expanduser()

    # Check legacy config.yaml path (google.gmail.credentials_dir)
    cfg = _load()
    legacy_dir = cfg.get("google", {}).get("gmail", {}).get("credentials_dir")
    if legacy_dir:
        return Path(str(legacy_dir)).expanduser()

    # Check new connectors config path
    conn_dir = cfg.get("connectors", {}).get("credentials_dir")
    if conn_dir:
        return Path(str(conn_dir)).expanduser()

    return Path.home() / ".agentforge"
