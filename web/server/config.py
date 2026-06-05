"""Configuration for the AgentForge Chat web server."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class ServerSettings(BaseSettings):
    """Settings loaded from environment variables or .env file."""

    host: str = "0.0.0.0"
    port: int = 8200
    cors_origins: str = "*"

    model_config = {"env_prefix": "AGENTFORGE_WEB_"}


settings = ServerSettings()
