"""Drop private tool-plugin modules here (gitignored except this file).

Each module exposes ``register(registry) -> int``. Load via AGENTFORGE_TOOL_PLUGINS::

    AGENTFORGE_TOOL_PLUGINS=plugins.cloud_tools:register_cloud_tools,plugins.hub_tools:register_hub_tools

``plugins.hub_tools`` lives only in this folder (there is no ``agentforge.tools.hub_tools``).
"""
