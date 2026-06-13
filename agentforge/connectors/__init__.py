"""Connector plugin system — external service integrations via OAuth."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

if TYPE_CHECKING:
    from .base import ConnectorPlugin

logger = get_logger(__name__)


class ConnectorRegistry:
    """Registry of available connector type plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, ConnectorPlugin] = {}

    def register(self, plugin: ConnectorPlugin) -> None:
        self._plugins[plugin.connector_type] = plugin
        logger.info("Registered connector plugin: %s", plugin.connector_type)

    def get(self, connector_type: str) -> ConnectorPlugin | None:
        return self._plugins.get(connector_type)

    def list_types(self) -> list[dict]:
        return [
            {
                "type": p.connector_type,
                "display_name": p.display_name,
                "description": p.description,
                "default_aliases": p.default_aliases,
                "auth_type": getattr(p, "auth_type", "oauth"),
                "products": p.available_products() if hasattr(p, "available_products") else [],
            }
            for p in self._plugins.values()
            if getattr(p, "listable", True)
        ]

    def __contains__(self, connector_type: str) -> bool:
        return connector_type in self._plugins
