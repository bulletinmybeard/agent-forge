"""Provider backends for AIClient — Ollama and Bedrock."""

from .base import Backend
from .ollama import OllamaBackend

__all__ = ["Backend", "OllamaBackend"]
