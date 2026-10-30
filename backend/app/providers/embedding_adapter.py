"""Google Gemini Embedding Adapter for EduSense (TASK-M3-05 / US 2.10).

Adheres strictly to EduSense Invariant 4:
"Exactly one place calls Google Gemini: the shared LLM & Embedding Adapter (app/providers/).
Routers and worker tasks never call the Gemini SDK directly."
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from functools import lru_cache
from typing import Any

from google import genai
from google.genai import types

from app.core.config import settings

logger = logging.getLogger("edusense.providers.embedding")

# Default batch size for Gemini embedding requests
DEFAULT_EMBED_BATCH_SIZE: int = 100


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class EmbeddingAdapterError(Exception):
    """Base exception for all embedding adapter errors."""


class EmbeddingConfigurationError(EmbeddingAdapterError):
    """Raised when the embedding provider is misconfigured (e.g. missing API key)."""


class EmbeddingProviderError(EmbeddingAdapterError):
    """Raised when calling Google Gemini embedding API fails."""


# ---------------------------------------------------------------------------
# Embedding Adapter Implementation
# ---------------------------------------------------------------------------


class EmbeddingAdapter:
    """Production adapter for Google Gemini embedding models via google-genai SDK."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        dimension: int | None = None,
        client: Any = None,
        test_mode: bool = False,
        batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    ) -> None:
        """Initialize EmbeddingAdapter with configuration and client injection.

        Args:
            api_key: Optional Gemini API key override (defaults to settings.gemini_api_key).
            model: Optional embedding model override (defaults to settings.gemini_embedding_model).
            dimension: Target vector dimensionality (defaults to settings.gemini_embedding_dimension or 768).
            client: Optional pre-configured genai.Client or mock double for unit tests.
            test_mode: If True and api_key is missing, gracefully generates mock embeddings instead of failing.
            batch_size: Maximum texts per single embedding API call.
        """
        self.model: str = model or settings.gemini_embedding_model or "gemini-embedding-2"
        self.dimension: int = dimension or settings.gemini_embedding_dimension or 768
        self.batch_size: int = max(1, batch_size)
        self.test_mode: bool = test_mode

        resolved_api_key = api_key or settings.gemini_api_key

        if client is not None:
            self._client: Any = client
        elif self.test_mode:
            self._client = None
            logger.info("EmbeddingAdapter running in test mode with mock embedding generator.")
        elif resolved_api_key:
            self._client = genai.Client(api_key=resolved_api_key)
        else:
            raise EmbeddingConfigurationError(
                "Gemini API key is not configured. Set GEMINI_API_KEY environment variable or pass api_key."
            )

    def _mock_embed(self, text: str) -> list[float]:
        """Generate deterministic, unit-normalized 768-dim float vector for test mode."""
        clean_text = text.strip() or " "
        seed = int(hashlib.sha256(clean_text.encode("utf-8")).hexdigest(), 16)
        raw_vec = [((seed + i * 31) % 1000) / 1000.0 for i in range(self.dimension)]
        norm = sum(x * x for x in raw_vec) ** 0.5
        if norm > 0:
            return [round(x / norm, 6) for x in raw_vec]
        return [0.0] * self.dimension

    def _extract_embeddings(self, response: Any) -> list[list[float]]:
        """Extract float vector lists from Gemini API response object or test double."""
        vectors: list[list[float]] = []

        # 1. Official SDK EmbedContentResponse with .embeddings (list of ContentEmbedding)
        if hasattr(response, "embeddings") and response.embeddings is not None:
            for item in response.embeddings:
                if hasattr(item, "values"):
                    vectors.append([float(v) for v in item.values])
                elif isinstance(item, dict) and "values" in item:
                    vectors.append([float(v) for v in item["values"]])
                elif isinstance(item, (list, tuple)):
                    vectors.append([float(v) for v in item])

        # 2. Response with single .embedding
        elif hasattr(response, "embedding") and response.embedding is not None:
            item = response.embedding
            if hasattr(item, "values"):
                vectors.append([float(v) for v in item.values])
            elif isinstance(item, dict) and "values" in item:
                vectors.append([float(v) for v in item["values"]])
            elif isinstance(item, (list, tuple)):
                vectors.append([float(v) for v in item])

        # 3. Dictionary response from custom mocks or REST fallback
        elif isinstance(response, dict):
            if "embeddings" in response and isinstance(response["embeddings"], list):
                for item in response["embeddings"]:
                    if isinstance(item, dict) and "values" in item:
                        vectors.append([float(v) for v in item["values"]])
                    elif hasattr(item, "values"):
                        vectors.append([float(v) for v in item.values])
                    elif isinstance(item, (list, tuple)):
                        vectors.append([float(v) for v in item])
            elif "embedding" in response:
                item = response["embedding"]
                if isinstance(item, dict) and "values" in item:
                    vectors.append([float(v) for v in item["values"]])
                elif isinstance(item, (list, tuple)):
                    vectors.append([float(v) for v in item])

        # 4. Direct list of lists or floats
        elif isinstance(response, (list, tuple)):
            if response and isinstance(response[0], (list, tuple)):
                vectors = [[float(v) for v in inner] for inner in response]
            elif response and isinstance(response[0], (int, float)):
                vectors = [[float(v) for v in response]]

        if not vectors:
            raise EmbeddingProviderError(
                f"Could not extract embeddings from Gemini API response: {response}"
            )

        # Validate vector dimensionality matches target schema
        for idx, vec in enumerate(vectors):
            if len(vec) != self.dimension:
                raise EmbeddingProviderError(
                    f"Embedding vector at index {idx} has dimension {len(vec)}, expected {self.dimension}."
                )

        return vectors

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate vector embeddings for a list of texts synchronously.

        Batches requests into chunks of batch_size to adhere to API payload limits.

        Args:
            texts: List of strings to embed.

        Returns:
            List of 768-dimensional float vectors matching the order of input texts.

        Raises:
            EmbeddingProviderError: If the Gemini API call fails.
        """
        if not texts:
            return []

        if self._client is None and self.test_mode:
            return [self._mock_embed(t) for t in texts]

        all_vectors: list[list[float]] = []

        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            try:
                config = (
                    types.EmbedContentConfig(output_dimensionality=self.dimension)
                    if self.dimension
                    else None
                )
                response = self._client.models.embed_content(
                    model=self.model,
                    contents=batch,
                    config=config,
                )
                vectors = self._extract_embeddings(response)
                all_vectors.extend(vectors)
            except EmbeddingAdapterError:
                raise
            except Exception as exc:
                logger.error("Gemini sync embedding generation failed for batch %d: %s", i, exc)
                raise EmbeddingProviderError(f"Gemini embedding call failed: {exc}") from exc

        return all_vectors

    def embed_text(self, text: str) -> list[float]:
        """Generate a 768-dimensional vector embedding for a single text string synchronously.

        Args:
            text: Text string to embed.

        Returns:
            768-dimensional float vector.

        Raises:
            EmbeddingProviderError: If the Gemini API call fails.
        """
        vectors = self.embed_batch([text])
        if not vectors:
            raise EmbeddingProviderError("Gemini returned empty embedding vector.")
        return vectors[0]

    async def aembed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate vector embeddings for a list of texts asynchronously.

        Batches requests into chunks of batch_size and awaits Gemini's aio client.

        Args:
            texts: List of strings to embed.

        Returns:
            List of 768-dimensional float vectors matching input order.

        Raises:
            EmbeddingProviderError: If the Gemini API call fails.
        """
        if not texts:
            return []

        if self._client is None and self.test_mode:
            return [self._mock_embed(t) for t in texts]

        all_vectors: list[list[float]] = []

        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            try:
                config = (
                    types.EmbedContentConfig(output_dimensionality=self.dimension)
                    if self.dimension
                    else None
                )

                # Use aio if available on client
                if hasattr(self._client, "aio") and hasattr(self._client.aio, "models"):
                    res = self._client.aio.models.embed_content(
                        model=self.model,
                        contents=batch,
                        config=config,
                    )
                    response = await res if inspect.isawaitable(res) else res
                else:
                    # Sync client or mock fallback
                    res = self._client.models.embed_content(
                        model=self.model,
                        contents=batch,
                        config=config,
                    )
                    response = await res if inspect.isawaitable(res) else res

                vectors = self._extract_embeddings(response)
                all_vectors.extend(vectors)
            except EmbeddingAdapterError:
                raise
            except Exception as exc:
                logger.error("Gemini async embedding generation failed for batch %d: %s", i, exc)
                raise EmbeddingProviderError(f"Gemini async embedding call failed: {exc}") from exc

        return all_vectors

    async def aembed_text(self, text: str) -> list[float]:
        """Generate a 768-dimensional vector embedding for a single text string asynchronously.

        Args:
            text: Text string to embed.

        Returns:
            768-dimensional float vector.

        Raises:
            EmbeddingProviderError: If the Gemini API call fails.
        """
        vectors = await self.aembed_batch([text])
        if not vectors:
            raise EmbeddingProviderError("Gemini returned empty embedding vector.")
        return vectors[0]


@lru_cache
def get_embedding_adapter() -> EmbeddingAdapter:
    """FastAPI and dependency injection provider returning cached EmbeddingAdapter instance."""
    return EmbeddingAdapter(
        test_mode=settings.app_env == "test" or settings.debug,
    )
