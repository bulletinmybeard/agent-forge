"""AgentForge — composable pipelines and agent loops over pluggable LLM backends."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-forge")
except PackageNotFoundError:  # source checkout, not installed
    __version__ = "0.0.0+unknown"
