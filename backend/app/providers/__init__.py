"""LLM & Embedding Adapter provider package for EduSense (Invariant 4).

All Google Gemini generation and embedding operations MUST pass through this package.
Routers, services, and worker tasks never call the Gemini SDK directly.
"""

from app.providers.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
)
from app.providers.embedding_adapter import (
    EmbeddingAdapter,
    EmbeddingAdapterError,
    EmbeddingConfigurationError,
    EmbeddingProviderError,
    get_embedding_adapter,
)
from app.providers.gemini_adapter import (
    ChatMessage,
    GeminiAdapter,
    GeminiAdapterError,
    GeminiCircuitOpenError,
    GeminiConfigurationError,
    GeminiRateLimitError,
    GeminiResponse,
    GeminiTimeoutError,
    get_gemini_adapter,
)
from app.providers.usage_logger import (
    AIUsageLogger,
    ModelPricing,
    calculate_cost,
    get_ai_usage_logger,
    get_model_pricing,
    sanitize_error_message,
)

__all__ = [
    "AIUsageLogger",
    "ChatMessage",
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "CircuitState",
    "EmbeddingAdapter",
    "EmbeddingAdapterError",
    "EmbeddingConfigurationError",
    "EmbeddingProviderError",
    "GeminiAdapter",
    "GeminiAdapterError",
    "GeminiCircuitOpenError",
    "GeminiConfigurationError",
    "GeminiRateLimitError",
    "GeminiResponse",
    "GeminiTimeoutError",
    "ModelPricing",
    "calculate_cost",
    "get_ai_usage_logger",
    "get_embedding_adapter",
    "get_gemini_adapter",
    "get_model_pricing",
    "sanitize_error_message",
]
