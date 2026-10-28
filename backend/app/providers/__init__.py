"""LLM & Embedding Adapter provider package for EduSense (Invariant 4).

All Google Gemini generation and embedding operations MUST pass through this package.
Routers, services, and worker tasks never call the Gemini SDK directly.
"""

from app.providers.embedding_adapter import (
    EmbeddingAdapter,
    EmbeddingAdapterError,
    EmbeddingConfigurationError,
    EmbeddingProviderError,
    get_embedding_adapter,
)

__all__ = [
    "EmbeddingAdapter",
    "EmbeddingAdapterError",
    "EmbeddingConfigurationError",
    "EmbeddingProviderError",
    "get_embedding_adapter",
]
