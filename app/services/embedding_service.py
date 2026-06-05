import logging

import ollama

from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    DEFAULT_TIMEOUT = 600.0

    def __init__(self) -> None:
        profile = settings.ollama.get_profile("embedding")
        self._host = profile.host
        self._headers = profile.headers
        self._sync_client = ollama.Client(
            host=profile.host,
            timeout=self.DEFAULT_TIMEOUT,
            headers=profile.headers,
        )
        self._async_client = ollama.AsyncClient(
            host=profile.host,
            timeout=self.DEFAULT_TIMEOUT,
            headers=profile.headers,
        )
        self._model = profile.model
        logger.info("EmbeddingService using profile '%s' → %s @ %s", profile.name, profile.model, profile.host)

    def _get_client(self, timeout: float | None = None) -> ollama.Client:
        """Return the default client, or a one-off client with a custom timeout."""
        if timeout is None or timeout == self.DEFAULT_TIMEOUT:
            return self._sync_client
        return ollama.Client(host=self._host, timeout=timeout, headers=self._headers)

    def embed(self, text: str) -> list[float]:
        response = self._sync_client.embed(model=self._model, input=text, keep_alive=settings.embedding.keep_alive)
        return response.embeddings[0]

    def embed_batch(self, texts: list[str], timeout: float | None = None) -> list[list[float]]:
        """Embed multiple texts in a single call (Ollama supports batch embed)."""
        if not texts:
            return []
        client = self._get_client(timeout)
        response = client.embed(model=self._model, input=texts, keep_alive=settings.embedding.keep_alive)
        return response.embeddings

    async def aembed(self, text: str) -> list[float]:
        response = await self._async_client.embed(
            model=self._model, input=text, keep_alive=settings.embedding.keep_alive
        )
        return response.embeddings[0]

    def get_dimension(self) -> int:
        try:
            test_embedding = self.embed("dimension test")
            return len(test_embedding)
        except Exception:
            logger.warning("Could not detect dimension, using configured default: %d", settings.embedding.dimension)
            return settings.embedding.dimension


embedding_service = EmbeddingService()
