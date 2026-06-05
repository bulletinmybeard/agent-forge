"""Standalone configuration for the chunking mappers.

This is offline tooling, so it is deliberately decoupled from the service config
in `app/config.py`. Mapper settings come from environment variables. The `db`
mapper additionally reads a `databases:` block from `config.yaml` (the same file
the service uses), so live-DB connection strings live in one place.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _load_config_yaml() -> dict[str, Any]:
    """Load the YAML the db mapper reads its `databases:` block from.

    Path comes from `AGENTFORGE_CHUNKING_CONFIG`, default `config.yaml` in the
    working directory. A missing file is fine (returns an empty dict).
    """
    path = Path(os.environ.get("AGENTFORGE_CHUNKING_CONFIG", "config.yaml"))
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


@dataclass
class DatabaseConnectionConfig:
    """One entry from the `databases:` block in `config.yaml`."""

    name: str
    url: str
    source_name: str
    schema: str | None = None  # PG schema, e.g., "public"


@dataclass
class MapperSettings:
    # Where chunk JSON is written. Keep it under ./data so the compose stack's
    # ./data -> /app/data mount makes the chunks visible to the indexer.
    chunks_output_dir: str = field(default_factory=lambda: _env("AGENTFORGE_CHUNKS_DIR", "data/chunks"))
    # Directory the OpenAPI mapper scans when run with --all.
    openapi_schemas_dir: str = field(
        default_factory=lambda: _env("AGENTFORGE_OPENAPI_SCHEMAS_DIR", "data/openapi-schemas")
    )
    # OpenAPI schemas with at most this many fields get inlined into endpoints.
    inline_schema_max_fields: int = field(default_factory=lambda: int(_env("AGENTFORGE_INLINE_SCHEMA_MAX_FIELDS", "5")))


@dataclass
class Settings:
    log_level: str = field(default_factory=lambda: _env("AGENTFORGE_LOG_LEVEL", "INFO"))
    mapper: MapperSettings = field(default_factory=MapperSettings)

    @property
    def databases(self) -> dict[str, DatabaseConnectionConfig]:
        raw = _load_config_yaml().get("databases", {})
        if not isinstance(raw, dict):
            return {}
        return {
            name: DatabaseConnectionConfig(
                name=name,
                url=cfg["url"],
                source_name=cfg.get("source_name", name),
                schema=cfg.get("schema"),
            )
            for name, cfg in raw.items()
            if isinstance(cfg, dict) and cfg.get("url")
        }


settings = Settings()
