"""Comprehensive tests for AI Usage Logging & Token Tracking (TASK-M4-03 / Invariant 4 & 7).

Validates:
- Invariant 4 & Invariant 7: Never log prompts, document content, or user messages in plain form.
  Only record tokens, model, feature, latency, status, and cost.
- Database Check Constraints:
  * ck_ai_usage_logs_tokens: prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0
    AND total_tokens = prompt_tokens + completion_tokens
  * ck_ai_usage_logs_cost: estimated_cost >= 0
  * ck_ai_usage_logs_latency: latency_ms IS NULL OR latency_ms >= 0
  * ck_ai_usage_logs_status: request_status IN ('success', 'failed', 'timeout', 'rejected')
- AIUsageRepository:
  * create_log parameter validation, automatic sum calculation, check constraints.
  * Tenant-isolated user queries (list_user_logs scoped by user_id).
  * Sliding-window aggregation (get_user_summary).
- AIUsageLogger:
  * Accurate cost calculations across Gemini Flash, Pro, and Embedding models.
  * Secret and credential redaction in error messages.
  * Graceful degradation: database failures never crash student AI requests.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.providers.usage_logger import (
    MODEL_PRICING_TABLE,
    AIUsageLogger,
    calculate_cost,
    get_ai_usage_logger,
    get_model_pricing,
    sanitize_error_message,
)
from app.repositories.ai_usage_repo import (
    VALID_REQUEST_STATUSES,
    AIUsageLog,
    AIUsageRepository,
    UsageSummary,
    get_ai_usage_repo,
)

# ---------------------------------------------------------------------------
# In-Memory FakeAIUsageRepository Double for Isolated Unit Testing
# ---------------------------------------------------------------------------


class FakeAIUsageRepository(AIUsageRepository):
    """In-memory AIUsageRepository double enforcing identical check constraints as PostgreSQL."""

    def __init__(self) -> None:
        self.logs: dict[uuid.UUID, AIUsageLog] = {}

    async def create_log(
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
        estimated_cost: Decimal | float = 0.0,
        provider: str = "google",
    ) -> AIUsageLog:
        # Enforce check constraints in the fake double
        if prompt_tokens < 0:
            raise ValueError("prompt_tokens must be non-negative (>= 0)")
        if completion_tokens < 0:
            raise ValueError("completion_tokens must be non-negative (>= 0)")

        computed_total = prompt_tokens + completion_tokens
        if total_tokens is None:
            resolved_total = computed_total
        else:
            if total_tokens < 0:
                raise ValueError("total_tokens must be non-negative (>= 0)")
            if total_tokens != computed_total:
                raise ValueError(
                    f"total_tokens ({total_tokens}) does not match prompt_tokens + completion_tokens ({computed_total})"
                )
            resolved_total = total_tokens

        if latency_ms is not None and latency_ms < 0:
            raise ValueError("latency_ms must be non-negative (>= 0) or None")

        if request_status not in VALID_REQUEST_STATUSES:
            raise ValueError(
                f"Invalid request_status '{request_status}'. Must be one of {sorted(VALID_REQUEST_STATUSES)}"
            )

        cost_decimal = (
            Decimal(str(estimated_cost))
            if not isinstance(estimated_cost, Decimal)
            else estimated_cost
        )
        if cost_decimal < Decimal(0):
            raise ValueError("estimated_cost must be non-negative (>= 0)")

        cost_decimal = cost_decimal.quantize(Decimal("0.000001"))

        log_id = uuid.uuid4()
        log = AIUsageLog(
            id=log_id,
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            feature=feature.strip().lower(),
            provider=provider.strip().lower(),
            model_name=model_name.strip(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=resolved_total,
            estimated_cost=cost_decimal,
            latency_ms=latency_ms,
            request_status=request_status,
            error_message=error_message,
            created_at=datetime.now(UTC),
        )
        self.logs[log_id] = log
        return log

    async def get_user_summary(
        self,
        user_id: uuid.UUID,
        days: int = 30,
        now: datetime | None = None,
    ) -> UsageSummary:
        if days <= 0:
            raise ValueError("days must be greater than 0")

        ref_time = now or datetime.now(UTC)
        cutoff = ref_time - timedelta(days=days)

        user_logs = [
            l
            for l in self.logs.values()
            if l.user_id == user_id
            and (l.created_at.replace(tzinfo=UTC) if l.created_at.tzinfo is None else l.created_at)
            >= cutoff
        ]

        total_requests = len(user_logs)
        total_prompt = sum(l.prompt_tokens for l in user_logs)
        total_comp = sum(l.completion_tokens for l in user_logs)
        total_tokens = sum(l.total_tokens for l in user_logs)
        total_cost = float(sum(l.estimated_cost for l in user_logs))

        return UsageSummary(
            total_requests=total_requests,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_comp,
            total_tokens=total_tokens,
            total_cost=round(total_cost, 6),
        )

    async def list_user_logs(
        self,
        user_id: uuid.UUID,
        limit: int = 50,
        skip: int = 0,
    ) -> list[AIUsageLog]:
        clamped_limit = max(1, min(limit, 100))
        offset = max(0, skip)

        user_logs = [l for l in self.logs.values() if l.user_id == user_id]
        user_logs.sort(key=lambda l: l.created_at, reverse=True)
        return user_logs[offset : offset + clamped_limit]


# ---------------------------------------------------------------------------
# Test Suite 1: Pricing Calculations & Heuristics
# ---------------------------------------------------------------------------


def test_pricing_exact_match() -> None:
    """Verify exact model pricing resolution from official reference table."""
    flash_pricing = get_model_pricing("gemini-2.5-flash")
    assert flash_pricing.input_cost_per_million == 0.075
    assert flash_pricing.output_cost_per_million == 0.30

    pro_pricing = get_model_pricing("gemini-1.5-pro")
    assert pro_pricing.input_cost_per_million == 1.25
    assert pro_pricing.output_cost_per_million == 5.00

    embed_pricing = get_model_pricing("text-embedding-004")
    assert embed_pricing.input_cost_per_million == 0.02
    assert embed_pricing.output_cost_per_million == 0.0


def test_pricing_model_prefix_and_case_insensitivity() -> None:
    """Verify models/ prefix is stripped and casing is normalized."""
    pricing1 = get_model_pricing("models/gemini-2.5-flash")
    assert pricing1 == MODEL_PRICING_TABLE["gemini-2.5-flash"]

    pricing2 = get_model_pricing("  GEMINI-2.0-FLASH  ")
    assert pricing2 == MODEL_PRICING_TABLE["gemini-2.0-flash"]


def test_pricing_heuristics_fallback() -> None:
    """Verify keyword heuristics for embedding, pro, flash-8b, and unknown models."""
    embed_h = get_model_pricing("custom-embedding-v2")
    assert embed_h.input_cost_per_million == 0.02
    assert embed_h.output_cost_per_million == 0.0

    pro_h = get_model_pricing("gemini-ultra-pro-preview")
    assert pro_h.input_cost_per_million == 1.25
    assert pro_h.output_cost_per_million == 5.00

    flash_8b_h = get_model_pricing("gemini-flash-8b-custom")
    assert flash_8b_h.input_cost_per_million == 0.0375
    assert flash_8b_h.output_cost_per_million == 0.15

    unknown = get_model_pricing("unknown-futuristic-model")
    # Falls back to default Flash pricing
    assert unknown.input_cost_per_million == 0.075
    assert unknown.output_cost_per_million == 0.30


def test_calculate_cost_values() -> None:
    """Verify mathematical calculation of token cost."""
    # 1,000,000 prompt tokens + 1,000,000 completion tokens on Flash
    cost = calculate_cost("gemini-2.5-flash", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost == pytest.approx(0.375, rel=1e-5)

    # 10,000 prompt tokens + 5,000 completion tokens on Flash
    # (10_000 * 0.075 / 1M) + (5_000 * 0.30 / 1M) = 0.00075 + 0.0015 = 0.00225
    cost = calculate_cost("gemini-2.5-flash", prompt_tokens=10_000, completion_tokens=5_000)
    assert cost == pytest.approx(0.00225, rel=1e-5)

    # Embedding cost (only prompt tokens cost money, output is $0)
    cost = calculate_cost("text-embedding-004", prompt_tokens=500_000, completion_tokens=0)
    assert cost == pytest.approx(0.01, rel=1e-5)

    # Zero tokens
    assert calculate_cost("gemini-2.5-flash", prompt_tokens=0, completion_tokens=0) == 0.0

    # Negative tokens clamped to 0
    assert calculate_cost("gemini-2.5-flash", prompt_tokens=-100, completion_tokens=-50) == 0.0


# ---------------------------------------------------------------------------
# Test Suite 2: Error Message Sanitization (Invariant 4 & 7)
# ---------------------------------------------------------------------------


def test_sanitize_error_message_none_and_empty() -> None:
    """Verify None and empty strings return None."""
    assert sanitize_error_message(None) is None
    assert sanitize_error_message("") is None
    assert sanitize_error_message("   ") is None


def test_sanitize_error_message_redaction() -> None:
    """Verify API keys, bearer tokens, and secrets are redacted from error logs."""
    raw = (
        "Upstream API failure: api_key='AIzaSyDummyKeyForTestingPurposes1234' "
        "Authorization: Bearer secret_session_token_xyz123456789. Request aborted."
    )
    sanitized = sanitize_error_message(raw)
    assert sanitized is not None
    assert "AIzaSyDummyKeyForTestingPurposes1234" not in sanitized
    assert "secret_session_token_xyz123456789" not in sanitized
    assert "[REDACTED]" in sanitized or "[REDACTED_GEMINI_KEY]" in sanitized


def test_sanitize_error_message_truncation() -> None:
    """Verify excessively long error messages are truncated."""
    long_msg = "A" * 1000
    sanitized = sanitize_error_message(long_msg, max_length=100)
    assert sanitized is not None
    assert len(sanitized) == 103  # 100 + "..."
    assert sanitized.endswith("...")


# ---------------------------------------------------------------------------
# Test Suite 3: AIUsageRepository Check Constraints & Methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_create_log_auto_calculates_total_tokens() -> None:
    """Verify total_tokens defaults to prompt_tokens + completion_tokens if not provided."""
    repo = FakeAIUsageRepository()
    user_id = uuid.uuid4()

    log = await repo.create_log(
        user_id=user_id,
        feature="chat",
        model_name="gemini-2.5-flash",
        prompt_tokens=150,
        completion_tokens=350,
        total_tokens=None,  # Not provided
        latency_ms=450,
        request_status="success",
        estimated_cost=0.00012,
    )

    assert log.prompt_tokens == 150
    assert log.completion_tokens == 350
    assert log.total_tokens == 500
    assert log.latency_ms == 450
    assert log.request_status == "success"
    assert log.estimated_cost == Decimal("0.000120")


@pytest.mark.asyncio
async def test_repository_check_constraint_tokens_sum_mismatch() -> None:
    """Verify ValueError is raised when explicit total_tokens != prompt + completion."""
    repo = FakeAIUsageRepository()
    user_id = uuid.uuid4()

    with pytest.raises(ValueError, match="does not match prompt_tokens"):
        await repo.create_log(
            user_id=user_id,
            feature="chat",
            model_name="gemini-2.5-flash",
            prompt_tokens=100,
            completion_tokens=100,
            total_tokens=999,  # Mismatched!
        )


@pytest.mark.asyncio
async def test_repository_check_constraint_negative_tokens() -> None:
    """Verify ValueError is raised when prompt_tokens, completion_tokens, or total_tokens < 0."""
    repo = FakeAIUsageRepository()
    user_id = uuid.uuid4()

    with pytest.raises(ValueError, match="prompt_tokens must be non-negative"):
        await repo.create_log(
            user_id=user_id,
            feature="chat",
            model_name="gemini-2.5-flash",
            prompt_tokens=-1,
            completion_tokens=10,
        )

    with pytest.raises(ValueError, match="completion_tokens must be non-negative"):
        await repo.create_log(
            user_id=user_id,
            feature="chat",
            model_name="gemini-2.5-flash",
            prompt_tokens=10,
            completion_tokens=-5,
        )


@pytest.mark.asyncio
async def test_repository_check_constraint_negative_latency() -> None:
    """Verify ValueError is raised when latency_ms is negative."""
    repo = FakeAIUsageRepository()
    user_id = uuid.uuid4()

    with pytest.raises(ValueError, match="latency_ms must be non-negative"):
        await repo.create_log(
            user_id=user_id,
            feature="rag",
            model_name="gemini-2.5-flash",
            latency_ms=-10,
        )


@pytest.mark.asyncio
async def test_repository_check_constraint_negative_cost() -> None:
    """Verify ValueError is raised when estimated_cost is negative."""
    repo = FakeAIUsageRepository()
    user_id = uuid.uuid4()

    with pytest.raises(ValueError, match="estimated_cost must be non-negative"):
        await repo.create_log(
            user_id=user_id,
            feature="rag",
            model_name="gemini-2.5-flash",
            estimated_cost=-0.05,
        )


@pytest.mark.asyncio
async def test_repository_check_constraint_invalid_status() -> None:
    """Verify ValueError is raised when request_status is not in allowed enum."""
    repo = FakeAIUsageRepository()
    user_id = uuid.uuid4()

    with pytest.raises(ValueError, match="Invalid request_status"):
        await repo.create_log(
            user_id=user_id,
            feature="rag",
            model_name="gemini-2.5-flash",
            request_status="pending",  # Invalid! Must be success, failed, timeout, rejected
        )


@pytest.mark.asyncio
async def test_repository_all_valid_statuses() -> None:
    """Verify all four valid statuses can be persisted without error."""
    repo = FakeAIUsageRepository()
    user_id = uuid.uuid4()

    for status in ("success", "failed", "timeout", "rejected"):
        log = await repo.create_log(
            user_id=user_id,
            feature="chat",
            model_name="gemini-2.5-flash",
            request_status=status,
            error_message="Sample error" if status != "success" else None,
        )
        assert log.request_status == status


@pytest.mark.asyncio
async def test_repository_list_user_logs_tenant_scoped() -> None:
    """Verify list_user_logs strictly scopes results to the requested user_id."""
    repo = FakeAIUsageRepository()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    # Create logs for user A
    for i in range(3):
        await repo.create_log(
            user_id=user_a,
            feature="chat",
            model_name="gemini-2.5-flash",
            prompt_tokens=i * 10,
            completion_tokens=i * 20,
        )

    # Create logs for user B
    for _ in range(5):
        await repo.create_log(
            user_id=user_b,
            feature="summary",
            model_name="gemini-2.5-flash",
            prompt_tokens=50,
            completion_tokens=100,
        )

    logs_a = await repo.list_user_logs(user_a)
    logs_b = await repo.list_user_logs(user_b)

    assert len(logs_a) == 3
    assert all(l.user_id == user_a for l in logs_a)
    assert len(logs_b) == 5
    assert all(l.user_id == user_b for l in logs_b)


@pytest.mark.asyncio
async def test_repository_get_user_summary_aggregation() -> None:
    """Verify get_user_summary aggregates requests, prompt tokens, completion tokens, and cost."""
    repo = FakeAIUsageRepository()
    user_id = uuid.uuid4()

    await repo.create_log(
        user_id=user_id,
        feature="chat",
        model_name="gemini-2.5-flash",
        prompt_tokens=100,
        completion_tokens=200,
        estimated_cost=0.0001,
    )
    await repo.create_log(
        user_id=user_id,
        feature="rag",
        model_name="gemini-2.5-flash",
        prompt_tokens=500,
        completion_tokens=300,
        estimated_cost=0.0005,
    )

    summary = await repo.get_user_summary(user_id=user_id, days=30)

    assert summary.total_requests == 2
    assert summary.total_prompt_tokens == 600
    assert summary.total_completion_tokens == 500
    assert summary.total_tokens == 1100
    assert summary.total_cost == pytest.approx(0.0006, rel=1e-5)


@pytest.mark.asyncio
async def test_repository_get_user_summary_invalid_days() -> None:
    """Verify ValueError is raised if days <= 0."""
    repo = FakeAIUsageRepository()
    user_id = uuid.uuid4()

    with pytest.raises(ValueError, match="days must be greater than 0"):
        await repo.get_user_summary(user_id=user_id, days=0)


# ---------------------------------------------------------------------------
# Test Suite 4: SQL AIUsageRepository Statements & AsyncSession Integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_ai_usage_repository_create_log_executes_flush() -> None:
    """Verify SQL AIUsageRepository adds entity to session and flushes."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()

    sql_repo = AIUsageRepository(mock_session)
    user_id = uuid.uuid4()

    log = await sql_repo.create_log(
        user_id=user_id,
        feature="summary",
        model_name="gemini-2.5-flash",
        prompt_tokens=120,
        completion_tokens=80,
        latency_ms=250,
        request_status="success",
    )

    assert mock_session.add.called
    assert mock_session.flush.called
    assert log.user_id == user_id
    assert log.total_tokens == 200
    assert log.feature == "summary"


@pytest.mark.asyncio
async def test_sql_ai_usage_repository_summary_query() -> None:
    """Verify SQL AIUsageRepository builds aggregated select statement correctly."""
    mock_session = AsyncMock()
    # Mock row returned by session.execute(stmt).one()
    mock_row = MagicMock()
    mock_row.total_requests = 10
    mock_row.total_prompt_tokens = 5000
    mock_row.total_completion_tokens = 2500
    mock_row.total_tokens = 7500
    mock_row.total_cost = Decimal("0.005500")

    mock_result = MagicMock()
    mock_result.one.return_value = mock_row
    mock_session.execute.return_value = mock_result

    sql_repo = AIUsageRepository(mock_session)
    user_id = uuid.uuid4()

    summary = await sql_repo.get_user_summary(user_id=user_id, days=7)

    assert mock_session.execute.called
    assert summary.total_requests == 10
    assert summary.total_prompt_tokens == 5000
    assert summary.total_completion_tokens == 2500
    assert summary.total_tokens == 7500
    assert summary.total_cost == pytest.approx(0.0055, rel=1e-5)


@pytest.mark.asyncio
async def test_sql_ai_usage_repository_list_logs_pagination() -> None:
    """Verify SQL AIUsageRepository builds paginated select statement correctly."""
    mock_session = AsyncMock()
    mock_log = AIUsageLog(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        feature="chat",
        model_name="gemini-2.5-flash",
        prompt_tokens=50,
        completion_tokens=50,
        total_tokens=100,
        estimated_cost=Decimal("0.000100"),
        request_status="success",
    )
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_log]
    mock_session.execute.return_value = mock_result

    sql_repo = AIUsageRepository(mock_session)
    user_id = uuid.uuid4()

    logs = await sql_repo.list_user_logs(user_id=user_id, limit=25, skip=10)

    assert mock_session.execute.called
    assert len(logs) == 1
    assert logs[0] == mock_log


# ---------------------------------------------------------------------------
# Test Suite 5: AIUsageLogger Telemetry & Invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_logger_successful_call() -> None:
    """Verify AIUsageLogger logs success with calculated cost and total tokens."""
    repo = FakeAIUsageRepository()
    logger = AIUsageLogger(repository=repo)
    user_id = uuid.uuid4()

    log = await logger.log_ai_call(
        user_id=user_id,
        feature="chat",
        model_name="gemini-2.5-flash",
        prompt_tokens=200,
        completion_tokens=800,
        latency_ms=310,
        request_status="success",
    )

    assert log is not None
    assert log.user_id == user_id
    assert log.total_tokens == 1000
    assert log.request_status == "success"
    # Cost for 200 prompt + 800 completion on gemini-2.5-flash:
    # (200 * 0.075 / 1M) + (800 * 0.30 / 1M) = 0.000015 + 0.00024 = 0.000255
    expected_cost = Decimal("0.000255")
    assert log.estimated_cost == expected_cost


@pytest.mark.asyncio
async def test_usage_logger_failed_call_sanitizes_error() -> None:
    """Verify AIUsageLogger records failed call and redacts sensitive credentials."""
    repo = FakeAIUsageRepository()
    logger = AIUsageLogger(repository=repo)
    user_id = uuid.uuid4()

    sensitive_error = "Gemini API error 400: invalid key api_key='AIzaSyAbc123SecretKeyHere456'"

    log = await logger.log_ai_call(
        user_id=user_id,
        feature="rag",
        model_name="gemini-2.5-flash",
        prompt_tokens=500,
        completion_tokens=0,
        latency_ms=120,
        request_status="failed",
        error_message=sensitive_error,
    )

    assert log is not None
    assert log.request_status == "failed"
    assert "AIzaSyAbc123SecretKeyHere456" not in (log.error_message or "")
    assert "[REDACTED]" in (log.error_message or "") or "[REDACTED_GEMINI_KEY]" in (
        log.error_message or ""
    )


@pytest.mark.asyncio
async def test_usage_logger_timeout_and_rejected_statuses() -> None:
    """Verify timeout and rejected statuses are preserved."""
    repo = FakeAIUsageRepository()
    logger = AIUsageLogger(repository=repo)
    user_id = uuid.uuid4()

    log_timeout = await logger.log_ai_call(
        user_id=user_id,
        feature="summary",
        model_name="gemini-2.5-flash",
        prompt_tokens=2000,
        completion_tokens=0,
        latency_ms=10000,
        request_status="timeout",
        error_message="Upstream request timed out after 10.0s",
    )
    assert log_timeout is not None
    assert log_timeout.request_status == "timeout"

    log_rejected = await logger.log_ai_call(
        user_id=user_id,
        feature="questions",
        model_name="gemini-2.5-flash",
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=5,
        request_status="rejected",
        error_message="Daily quota exceeded: circuit rejected request",
    )
    assert log_rejected is not None
    assert log_rejected.request_status == "rejected"


@pytest.mark.asyncio
async def test_usage_logger_status_normalization() -> None:
    """Verify unrecognized request_status is safely normalized to 'failed'."""
    repo = FakeAIUsageRepository()
    logger = AIUsageLogger(repository=repo)
    user_id = uuid.uuid4()

    log = await logger.log_ai_call(
        user_id=user_id,
        feature="chat",
        model_name="gemini-2.5-flash",
        request_status="aborted_unexpectedly",  # Unknown status
    )
    assert log is not None
    assert log.request_status == "failed"


@pytest.mark.asyncio
async def test_usage_logger_graceful_degradation_on_db_exception() -> None:
    """Verify database exceptions do NOT crash the caller; returns None safely."""
    failing_repo = AsyncMock()
    failing_repo.create_log.side_effect = RuntimeError("PostgreSQL connection timeout!")

    logger = AIUsageLogger(repository=failing_repo)
    user_id = uuid.uuid4()

    # Must return None and NOT raise RuntimeError
    result = await logger.log_ai_call(
        user_id=user_id,
        feature="chat",
        model_name="gemini-2.5-flash",
        prompt_tokens=100,
        completion_tokens=50,
    )

    assert result is None


@pytest.mark.asyncio
async def test_usage_logger_unconfigured_repository_graceful() -> None:
    """Verify unconfigured repository returns None without raising."""
    logger = AIUsageLogger(repository=None)
    user_id = uuid.uuid4()

    result = await logger.log_ai_call(
        user_id=user_id,
        feature="chat",
        model_name="gemini-2.5-flash",
    )
    assert result is None


def test_dependency_providers() -> None:
    """Verify FastAPI dependency providers instantiate correctly."""
    mock_session = AsyncMock()
    repo = get_ai_usage_repo(mock_session)
    assert isinstance(repo, AIUsageRepository)

    logger = get_ai_usage_logger(repo)
    assert isinstance(logger, AIUsageLogger)
    assert logger.repository is repo
