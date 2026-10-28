"""Centralized AI usage logging, token tracking, and cost calculation (TASK-M4-03 / Invariant 4 & 7).

Adheres strictly to:
- Invariant 4: Exactly one place calls Google Gemini: the shared LLM & Embedding Adapter (app/providers/).
  Usage tracking is integrated at the provider boundary.
- Invariant 4 & Invariant 7: Never log prompts, document content, or user messages in plain form.
  Only record tokens, model, feature, latency, status, and cost.
- Graceful Degradation: Telemetry logging failures must never crash student-facing AI requests.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from fastapi import Depends

from app.repositories.ai_usage_repo import (
    VALID_REQUEST_STATUSES,
    AIUsageLog,
    AIUsageRepository,
    get_ai_usage_repo,
)

logger = logging.getLogger("edusense.providers.usage_logger")


# ---------------------------------------------------------------------------
# Pricing Definitions & Cost Calculator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    """Pricing configuration for an AI model in USD per 1,000,000 tokens."""

    input_cost_per_million: float
    output_cost_per_million: float


# Upstream Google Gemini pricing reference table (USD per 1M tokens)
# Source: Google Cloud Vertex AI & Google AI Studio official pricing (Gemini 2.5/2.0/1.5)
MODEL_PRICING_TABLE: dict[str, ModelPricing] = {
    # Gemini Flash models ($0.075 / 1M prompt, $0.30 / 1M completion)
    "gemini-2.5-flash": ModelPricing(input_cost_per_million=0.075, output_cost_per_million=0.30),
    "gemini-2.0-flash": ModelPricing(input_cost_per_million=0.075, output_cost_per_million=0.30),
    "gemini-1.5-flash": ModelPricing(input_cost_per_million=0.075, output_cost_per_million=0.30),
    "gemini-1.5-flash-8b": ModelPricing(
        input_cost_per_million=0.0375, output_cost_per_million=0.15
    ),
    # Gemini Pro models ($1.25 / 1M prompt, $5.00 / 1M completion)
    "gemini-2.5-pro": ModelPricing(input_cost_per_million=1.25, output_cost_per_million=5.00),
    "gemini-1.5-pro": ModelPricing(input_cost_per_million=1.25, output_cost_per_million=5.00),
    # Gemini Embedding models ($0.02 / 1M prompt, $0.00 output)
    "text-embedding-004": ModelPricing(input_cost_per_million=0.02, output_cost_per_million=0.0),
    "gemini-embedding-exp-03-07": ModelPricing(
        input_cost_per_million=0.02, output_cost_per_million=0.0
    ),
}

DEFAULT_MODEL_PRICING = ModelPricing(input_cost_per_million=0.075, output_cost_per_million=0.30)


def get_model_pricing(model_name: str) -> ModelPricing:
    """Resolve pricing configuration for a given Gemini model name.

    Handles model aliases, prefixes (e.g. 'models/'), and fallback defaults.

    Args:
        model_name: Name of the AI model.

    Returns:
        ModelPricing instance with input/output rates per million tokens.
    """
    normalized = model_name.strip().lower().removeprefix("models/")

    # 1. Exact match
    if normalized in MODEL_PRICING_TABLE:
        return MODEL_PRICING_TABLE[normalized]

    # 2. Embedding heuristics
    if "embed" in normalized:
        return ModelPricing(input_cost_per_million=0.02, output_cost_per_million=0.0)

    # 3. Pro heuristics
    if "pro" in normalized:
        return ModelPricing(input_cost_per_million=1.25, output_cost_per_million=5.00)

    # 4. Flash 8b heuristics
    if "8b" in normalized:
        return ModelPricing(input_cost_per_million=0.0375, output_cost_per_million=0.15)

    # 5. Flash default
    if "flash" in normalized:
        return ModelPricing(input_cost_per_million=0.075, output_cost_per_million=0.30)

    return DEFAULT_MODEL_PRICING


def calculate_cost(
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int = 0,
) -> float:
    """Calculate estimated cost in USD for token consumption.

    Args:
        model_name: Model identifier (e.g. 'gemini-2.5-flash').
        prompt_tokens: Number of prompt/input tokens (>= 0).
        completion_tokens: Number of completion/output tokens (>= 0).

    Returns:
        Estimated cost rounded to 6 decimal places.
    """
    prompt_cnt = max(0, prompt_tokens)
    comp_cnt = max(0, completion_tokens)
    pricing = get_model_pricing(model_name)

    prompt_cost = (prompt_cnt / 1_000_000.0) * pricing.input_cost_per_million
    completion_cost = (comp_cnt / 1_000_000.0) * pricing.output_cost_per_million
    total_cost = prompt_cost + completion_cost
    return round(total_cost, 6)


# ---------------------------------------------------------------------------
# Error Sanitizer (Invariant 4 & 7)
# ---------------------------------------------------------------------------

# Patterns for sensitive tokens, API keys, and credentials
_CREDENTIAL_PATTERNS = [
    (re.compile(r"(?i)api[_-]?key['\":\s=]+([a-zA-Z0-9_\-]{8,})"), "api_key=[REDACTED]"),
    (re.compile(r"(?i)bearer\s+([a-zA-Z0-9_\-\.]{15,})"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)AIza[0-9A-Za-z\-_]{35}"), "[REDACTED_GEMINI_KEY]"),
    (re.compile(r"(?i)password['\":\s=]+([^\s,;]+)"), "password=[REDACTED]"),
]


def sanitize_error_message(message: str | None, max_length: int = 500) -> str | None:
    """Sanitize error messages to prevent leaking secrets, credentials, or prompts.

    Args:
        message: Raw error message or None.
        max_length: Maximum allowed string length (defaults to 500).

    Returns:
        Sanitized, redacted string, or None if input was empty.
    """
    if message is None:
        return None

    sanitized = message.strip()
    if not sanitized:
        return None

    # Redact credentials and sensitive substrings
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)

    # Clamping length to prevent large memory/db footprint
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."

    return sanitized


# ---------------------------------------------------------------------------
# AI Usage Logger
# ---------------------------------------------------------------------------


class AIUsageLogger:
    """Centralized telemetry logger for AI token consumption and usage costs (Invariant 4 & 7).

    Guarantees:
    - Invariant 4 & Invariant 7: Never accepts, stores, or logs prompt text, document content,
      or user conversation messages in plain form.
    - Graceful Degradation: All repository/database logging failures are caught and logged
      without raising exceptions, ensuring AI calls to students never fail due to background telemetry.
    """

    def __init__(self, repository: AIUsageRepository | None = None) -> None:
        """Initialize the AIUsageLogger.

        Args:
            repository: Optional active AIUsageRepository instance.
        """
        self._repository = repository

    @property
    def repository(self) -> AIUsageRepository | None:
        """Access the underlying repository instance."""
        return self._repository

    @staticmethod
    def calculate_cost(
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int = 0,
    ) -> float:
        """Calculate estimated cost in USD for token consumption."""
        return calculate_cost(model_name, prompt_tokens, completion_tokens)

    async def log_ai_call(
        self,
        user_id: uuid.UUID,
        feature: str,
        model_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int | None = None,
        latency_ms: int | None = None,
        request_status: str = "success",
        error_message: str | None = None,
        conversation_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        estimated_cost: float | Decimal | None = None,
        provider: str = "google",
        repository: AIUsageRepository | None = None,
    ) -> AIUsageLog | None:
        """Record an AI invocation to the database with token metrics and cost.

        Adheres strictly to Invariant 4 & 7:
        - NEVER takes prompt or document parameters.
        - Automatically computes total_tokens = prompt_tokens + completion_tokens if not provided.
        - Automatically calculates estimated_cost using current model pricing if not provided.
        - Sanitizes error messages to redact any leaked keys or tokens.
        - Employs graceful degradation: catches all persistence errors and logs them, returning None.

        Args:
            user_id: UUID of the student/user making the request.
            feature: AI feature identifier ('chat', 'rag', 'summary', 'questions', 'embedding', etc.).
            model_name: Model identifier (e.g. 'gemini-2.5-flash').
            prompt_tokens: Input/prompt token count (>= 0).
            completion_tokens: Output/completion token count (>= 0).
            total_tokens: Total token count (defaults to prompt_tokens + completion_tokens).
            latency_ms: Request latency in milliseconds (>= 0 or None).
            request_status: Status ('success', 'failed', 'timeout', 'rejected').
            error_message: Optional raw error message (will be sanitized).
            conversation_id: Optional UUID of the associated conversation.
            message_id: Optional UUID of the associated message.
            estimated_cost: Optional explicit cost. If None, calculated via calculate_cost().
            provider: AI provider name (default 'google').
            repository: Optional repository override (falls back to self._repository).

        Returns:
            The created AIUsageLog instance, or None if logging failed (graceful degradation).
        """
        target_repo = repository or self._repository
        if target_repo is None:
            logger.warning(
                "AIUsageLogger: No repository provided or configured; skipping persistent logging."
            )
            return None

        # Normalize request status to valid set
        normalized_status = request_status.strip().lower() if request_status else "success"
        if normalized_status not in VALID_REQUEST_STATUSES:
            normalized_status = "failed"

        # Token metrics
        safe_prompt_tokens = max(0, prompt_tokens)
        safe_completion_tokens = max(0, completion_tokens)
        if total_tokens is None:
            safe_total_tokens = safe_prompt_tokens + safe_completion_tokens
        else:
            safe_total_tokens = max(0, total_tokens)

        # Cost calculation
        if estimated_cost is None:
            cost_value = self.calculate_cost(
                model_name, safe_prompt_tokens, safe_completion_tokens
            )
        else:
            cost_value = float(estimated_cost)
            if cost_value < 0:
                cost_value = 0.0

        # Latency clamp
        safe_latency = max(0, latency_ms) if latency_ms is not None else None

        # Sanitize error message
        sanitized_error = sanitize_error_message(error_message)

        try:
            return await target_repo.create_log(
                user_id=user_id,
                feature=feature,
                model_name=model_name,
                prompt_tokens=safe_prompt_tokens,
                completion_tokens=safe_completion_tokens,
                total_tokens=safe_total_tokens,
                latency_ms=safe_latency,
                request_status=normalized_status,
                error_message=sanitized_error,
                conversation_id=conversation_id,
                message_id=message_id,
                estimated_cost=Decimal(str(cost_value)),
                provider=provider,
            )
        except Exception:
            # Graceful degradation: never crash student AI flow because of telemetry logging failure
            logger.exception(
                "Graceful degradation: Failed to persist AI usage log for user %s",
                user_id,
            )
            return None


def get_ai_usage_logger(
    repo: Annotated[AIUsageRepository, Depends(get_ai_usage_repo)],
) -> AIUsageLogger:
    """FastAPI dependency provider for AIUsageLogger with injected repository."""
    return AIUsageLogger(repository=repo)


__all__ = [
    "AIUsageLogger",
    "ModelPricing",
    "calculate_cost",
    "get_ai_usage_logger",
    "get_model_pricing",
    "sanitize_error_message",
]
