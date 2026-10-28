"""Comprehensive tests for CircuitBreaker and GeminiAdapter (TASK-M4-01).

Validates:
- EduSense Invariant 4: Exactly one place calls Google Gemini: the shared LLM & Embedding Adapter.
- Thread-safe CircuitBreaker state transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED.
- Fast fail when circuit is OPEN (raising CircuitBreakerOpenError / GeminiCircuitOpenError).
- Sync (call) and async (acall) circuit breaker protection.
- GeminiAdapter mock / test mode execution without real API credentials.
- GeminiAdapter with mock google-genai client and real response types.
- Structured output parsing with Pydantic BaseModel schemas.
- Exponential backoff retry for transient errors (429, 503, timeouts).
- Token usage extraction (prompt_tokens, completion_tokens, total_tokens).
- Strict error mapping to domain exceptions.
"""

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import errors, types
from pydantic import BaseModel, Field

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.providers.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
)
from app.providers.gemini_adapter import (
    ChatMessage,
    GeminiAdapter,
    GeminiAdapterError,
    GeminiCircuitOpenError,
    GeminiConfigurationError,
    GeminiResponse,
    get_gemini_adapter,
)

# ---------------------------------------------------------------------------
# Test Schemas & Helpers
# ---------------------------------------------------------------------------


class FlashcardItem(BaseModel):
    question: str = Field(..., description="The flashcard question")
    answer: str = Field(..., description="The answer")
    difficulty: int = Field(default=1, description="Difficulty level from 1 to 5")


class StudySummary(BaseModel):
    topic: str
    key_points: list[str]
    confidence_score: float = 0.95


def _make_mock_response(
    text: str = "",
    prompt_tokens: int = 10,
    completion_tokens: int = 10,
    total_tokens: int = 20,
    finish_reason: types.FinishReason = types.FinishReason.STOP,
    parsed: Any = None,
) -> types.GenerateContentResponse:
    """Helper to construct official SDK types.GenerateContentResponse instances."""
    parts = [types.Part.from_text(text=text)] if text else []
    candidate = types.Candidate(
        content=types.Content(parts=parts),
        finish_reason=finish_reason,
    )
    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt_tokens,
        candidates_token_count=completion_tokens,
        total_token_count=total_tokens,
    )
    return types.GenerateContentResponse(
        candidates=[candidate],
        usage_metadata=usage,
        parsed=parsed,
    )


# ---------------------------------------------------------------------------
# Part 1: CircuitBreaker Unit Tests
# ---------------------------------------------------------------------------


def test_circuit_breaker_initial_state() -> None:
    """Verifies that circuit breaker starts in CLOSED state with 0 failures and successes."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0
    assert cb.success_count == 0
    assert cb.last_failure_time is None


def test_circuit_breaker_sync_success_in_closed_state() -> None:
    """Verifies that successful calls in CLOSED state return result and keep failure_count at 0."""
    cb = CircuitBreaker(failure_threshold=3)

    def _good_call(x: int) -> int:
        return x * 2

    res = cb.call(_good_call, 21)
    assert res == 42
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_circuit_breaker_async_success_in_closed_state() -> None:
    """Verifies that async successful calls in CLOSED state return result."""
    cb = CircuitBreaker(failure_threshold=3)

    async def _async_good_call(text: str) -> str:
        return f"Echo: {text}"

    res = await cb.acall(_async_good_call, "EduSense")
    assert res == "Echo: EduSense"
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


def test_circuit_breaker_transitions_to_open_after_n_failures() -> None:
    """Verifies that reaching failure_threshold transitions state from CLOSED to OPEN."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=5.0)

    def _failing_call() -> None:
        raise ValueError("Service unavailable")

    # 1st failure
    with pytest.raises(ValueError):
        cb.call(_failing_call)
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 1

    # 2nd failure
    with pytest.raises(ValueError):
        cb.call(_failing_call)
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 2

    # 3rd failure -> exceeds/reaches threshold 3
    with pytest.raises(ValueError):
        cb.call(_failing_call)
    assert cb.state == CircuitState.OPEN
    assert cb.failure_count == 3
    assert cb.last_failure_time is not None


def test_circuit_breaker_fast_fails_when_open() -> None:
    """Verifies that when OPEN, calls fail fast with CircuitBreakerOpenError without invoking target."""
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0)
    mock_func = MagicMock()

    # Trip the circuit to OPEN
    for _ in range(2):
        try:
            cb.call(MagicMock(side_effect=RuntimeError("Err")))
        except RuntimeError:
            pass

    assert cb.state == CircuitState.OPEN

    # Subsequent sync call must raise CircuitBreakerOpenError immediately
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        cb.call(mock_func)
    assert "Circuit breaker is OPEN" in str(exc_info.value)
    mock_func.assert_not_called()


@pytest.mark.asyncio
async def test_circuit_breaker_async_fast_fails_when_open() -> None:
    """Verifies async acall fast fails when circuit is OPEN."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0)

    try:
        cb.call(MagicMock(side_effect=RuntimeError("Fail")))
    except RuntimeError:
        pass

    assert cb.state == CircuitState.OPEN
    mock_async = AsyncMock()

    with pytest.raises(CircuitBreakerOpenError):
        await cb.acall(mock_async)
    mock_async.assert_not_called()


def test_circuit_breaker_transitions_to_half_open_after_recovery_timeout() -> None:
    """Verifies that OPEN transitions to HALF_OPEN once recovery_timeout has elapsed."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.08)

    try:
        cb.call(MagicMock(side_effect=RuntimeError("Crash")))
    except RuntimeError:
        pass

    assert cb.state == CircuitState.OPEN

    # Sleep slightly longer than recovery_timeout
    time.sleep(0.1)

    assert cb.state == CircuitState.HALF_OPEN
    assert cb.success_count == 0


def test_circuit_breaker_half_open_to_closed_on_consecutive_successes() -> None:
    """Verifies that in HALF_OPEN, meeting half_open_success_threshold transitions back to CLOSED."""
    cb = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=0.05,
        half_open_success_threshold=2,
    )

    try:
        cb.call(MagicMock(side_effect=RuntimeError("Fail")))
    except RuntimeError:
        pass

    time.sleep(0.07)
    assert cb.state == CircuitState.HALF_OPEN

    # 1st success in HALF_OPEN
    res1 = cb.call(lambda: "Success 1")
    assert res1 == "Success 1"
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.success_count == 1

    # 2nd success in HALF_OPEN -> reaches threshold 2 -> transitions to CLOSED
    res2 = cb.call(lambda: "Success 2")
    assert res2 == "Success 2"
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0
    assert cb.success_count == 0


def test_circuit_breaker_half_open_to_open_on_failure() -> None:
    """Verifies that any failure in HALF_OPEN immediately trips circuit back to OPEN."""
    cb = CircuitBreaker(
        failure_threshold=2,
        recovery_timeout=0.05,
        half_open_success_threshold=2,
    )

    # Trip to OPEN
    for _ in range(2):
        try:
            cb.call(MagicMock(side_effect=RuntimeError("Fail")))
        except RuntimeError:
            pass

    time.sleep(0.07)
    assert cb.state == CircuitState.HALF_OPEN

    # 1 success
    cb.call(lambda: "OK")
    assert cb.state == CircuitState.HALF_OPEN

    # 1 failure -> trips back to OPEN immediately
    with pytest.raises(RuntimeError):
        cb.call(MagicMock(side_effect=RuntimeError("Half-open failure")))

    assert cb.state == CircuitState.OPEN
    assert cb.success_count == 0


def test_circuit_breaker_reset() -> None:
    """Verifies that calling reset() restores initial CLOSED state."""
    cb = CircuitBreaker(failure_threshold=1)
    try:
        cb.call(MagicMock(side_effect=RuntimeError("Fail")))
    except RuntimeError:
        pass
    assert cb.state == CircuitState.OPEN

    cb.reset()
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0
    assert cb.success_count == 0
    assert cb.last_failure_time is None


# ---------------------------------------------------------------------------
# Part 2: GeminiAdapter Unit Tests
# ---------------------------------------------------------------------------


def test_gemini_adapter_test_mode_generate_text() -> None:
    """Verifies synchronous text generation in test/mock mode."""
    adapter = GeminiAdapter(test_mode=True)
    prompt = "Summarize the key differences between TCP and UDP."

    response = adapter.generate_text(prompt, temperature=0.5)

    assert isinstance(response, GeminiResponse)
    assert response.model == adapter.model
    assert "TCP" in response.text or "Mock Gemini" in response.text
    assert response.prompt_tokens > 0
    assert response.completion_tokens > 0
    assert response.total_tokens == response.prompt_tokens + response.completion_tokens
    assert response.finish_reason == "STOP"


@pytest.mark.asyncio
async def test_gemini_adapter_test_mode_agenerate_text() -> None:
    """Verifies asynchronous text generation in test/mock mode."""
    adapter = GeminiAdapter(test_mode=True)
    prompt = "What is Amdahl's Law in parallel computing?"

    response = await adapter.agenerate_text(prompt)

    assert isinstance(response, GeminiResponse)
    assert response.prompt_tokens > 0
    assert response.completion_tokens > 0
    assert response.finish_reason == "STOP"


def test_gemini_adapter_test_mode_generate_chat() -> None:
    """Verifies multi-turn chat generation in test/mock mode."""
    adapter = GeminiAdapter(test_mode=True)
    messages = [
        ChatMessage(role="system", content="You are a helpful university AI tutor."),
        ChatMessage(role="user", content="Can you explain Dijkstra's algorithm?"),
        ChatMessage(role="model", content="Dijkstra's algorithm finds shortest paths in graphs."),
        ChatMessage(role="user", content="Does it work with negative edge weights?"),
    ]

    response = adapter.generate_chat(messages, temperature=0.7)

    assert isinstance(response, GeminiResponse)
    assert response.prompt_tokens > 0
    assert response.completion_tokens > 0
    assert response.finish_reason == "STOP"


@pytest.mark.asyncio
async def test_gemini_adapter_test_mode_agenerate_chat() -> None:
    """Verifies async multi-turn chat in test/mock mode."""
    adapter = GeminiAdapter(test_mode=True)
    messages = [ChatMessage(role="user", content="Hello, EduSense!")]

    response = await adapter.agenerate_chat(messages)

    assert isinstance(response, GeminiResponse)
    assert response.prompt_tokens > 0


def test_gemini_adapter_test_mode_generate_structured() -> None:
    """Verifies structured output generation in test/mock mode creates a valid Pydantic instance."""
    adapter = GeminiAdapter(test_mode=True)

    result = adapter.generate_structured(
        prompt="Generate a flashcard for Database Normalization (3NF)",
        response_schema=FlashcardItem,
        temperature=0.2,
    )

    assert isinstance(result, FlashcardItem)
    assert len(result.question) > 0
    assert len(result.answer) > 0
    assert isinstance(result.difficulty, int)


@pytest.mark.asyncio
async def test_gemini_adapter_test_mode_agenerate_structured() -> None:
    """Verifies async structured output in test/mock mode with complex schema."""
    adapter = GeminiAdapter(test_mode=True)

    result = await adapter.agenerate_structured(
        prompt="Generate a study summary for Chapter 4",
        response_schema=StudySummary,
    )

    assert isinstance(result, StudySummary)
    assert len(result.topic) > 0
    assert isinstance(result.key_points, list)
    assert len(result.key_points) > 0
    assert isinstance(result.confidence_score, float)


def test_gemini_adapter_mock_client_generate_content() -> None:
    """Verifies GeminiAdapter calls official google-genai client and extracts tokens accurately."""
    mock_client = MagicMock()
    mock_response = _make_mock_response(
        text="Relational databases use ACID transactions.",
        prompt_tokens=18,
        completion_tokens=12,
        total_tokens=30,
        finish_reason=types.FinishReason.STOP,
    )
    mock_client.models.generate_content.return_value = mock_response

    adapter = GeminiAdapter(client=mock_client)
    res = adapter.generate_text("Explain ACID transactions.")

    assert res.text == "Relational databases use ACID transactions."
    assert res.prompt_tokens == 18
    assert res.completion_tokens == 12
    assert res.total_tokens == 30
    assert res.finish_reason == "STOP"
    mock_client.models.generate_content.assert_called_once()


@pytest.mark.asyncio
async def test_gemini_adapter_mock_client_async_generate_content() -> None:
    """Verifies async call using mock client's aio.models.generate_content."""
    mock_client = MagicMock()
    mock_response = _make_mock_response(
        text="Async response from Gemini.",
        prompt_tokens=10,
        completion_tokens=8,
        total_tokens=18,
        finish_reason=types.FinishReason.STOP,
    )
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    adapter = GeminiAdapter(client=mock_client)
    res = await adapter.agenerate_text("Async prompt")

    assert res.text == "Async response from Gemini."
    assert res.prompt_tokens == 10
    assert res.completion_tokens == 8
    assert res.total_tokens == 18
    mock_client.aio.models.generate_content.assert_called_once()


def test_gemini_adapter_structured_parsing_with_json_markdown() -> None:
    """Verifies structured output parsing when model returns markdown-fenced ```json ... ``` text."""
    mock_client = MagicMock()
    json_text = (
        '```json\n{"question": "What is 2NF?", "answer": "1NF + no partial dependency", "difficulty": 2}\n```'
    )
    mock_response = _make_mock_response(
        text=json_text,
        prompt_tokens=20,
        completion_tokens=20,
        total_tokens=40,
    )
    mock_client.models.generate_content.return_value = mock_response

    adapter = GeminiAdapter(client=mock_client)
    flashcard = adapter.generate_structured("Generate 2NF card", FlashcardItem)

    assert isinstance(flashcard, FlashcardItem)
    assert flashcard.question == "What is 2NF?"
    assert flashcard.answer == "1NF + no partial dependency"
    assert flashcard.difficulty == 2


def test_gemini_adapter_structured_parsing_with_sdk_parsed_attribute() -> None:
    """Verifies structured output parsing when official SDK populates response.parsed."""
    mock_client = MagicMock()
    expected_obj = FlashcardItem(
        question="What is an index?",
        answer="A data structure that improves retrieval speed",
        difficulty=3,
    )
    mock_response = _make_mock_response(
        text='{"question": "What is an index?", "answer": "...", "difficulty": 3}',
        parsed=expected_obj,
    )
    mock_client.models.generate_content.return_value = mock_response

    adapter = GeminiAdapter(client=mock_client)
    flashcard = adapter.generate_structured("Explain index", FlashcardItem)

    assert flashcard == expected_obj


def test_gemini_adapter_transient_error_retry_with_exponential_backoff() -> None:
    """Verifies that transient errors (e.g. HTTP 429) trigger exponential backoff retry and succeed."""
    mock_client = MagicMock()

    api_429_err = errors.APIError(429, {"message": "Resource exhausted / Rate limit exceeded"})
    success_response = _make_mock_response(
        text="Recovered after transient rate limit.",
        prompt_tokens=5,
        completion_tokens=5,
        total_tokens=10,
    )

    # Fail twice with 429, then succeed on 3rd attempt
    mock_client.models.generate_content.side_effect = [
        api_429_err,
        api_429_err,
        success_response,
    ]

    adapter = GeminiAdapter(
        client=mock_client,
        max_retries=3,
        initial_retry_delay=0.01,  # Short delay for fast unit tests
    )

    res = adapter.generate_text("Prompt needing retry")

    assert res.text == "Recovered after transient rate limit."
    assert mock_client.models.generate_content.call_count == 3


def test_gemini_adapter_circuit_breaker_triggering_after_repeated_failures() -> None:
    """Verifies that repeated persistent failures trip the circuit breaker and raise GeminiCircuitOpenError."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=2.0)
    mock_client = MagicMock()

    # Client constantly fails with a non-transient or persistent error
    mock_client.models.generate_content.side_effect = RuntimeError("Persistent connection drop")

    adapter = GeminiAdapter(
        client=mock_client,
        circuit_breaker=cb,
        max_retries=0,  # 0 retries so each generate_text call is 1 circuit breaker failure
    )

    # Call 1, 2, 3 -> all raise GeminiAdapterError
    for _ in range(3):
        with pytest.raises(GeminiAdapterError) as exc_info:
            adapter.generate_text("Persistent failure")
        assert not isinstance(exc_info.value, GeminiCircuitOpenError)

    # Circuit breaker is now OPEN
    assert cb.state == CircuitState.OPEN

    # 4th call must raise GeminiCircuitOpenError without calling mock_client
    with pytest.raises(GeminiCircuitOpenError) as open_exc_info:
        adapter.generate_text("Call when circuit is open")

    assert "circuit breaker" in str(open_exc_info.value).lower()
    assert mock_client.models.generate_content.call_count == 3


def test_gemini_adapter_token_usage_tracking_extraction() -> None:
    """Verifies accurate token usage extraction from different response shapes."""
    mock_client = MagicMock()
    mock_response = _make_mock_response(
        text="Token test",
        prompt_tokens=105,
        completion_tokens=210,
        total_tokens=315,
        finish_reason=types.FinishReason.MAX_TOKENS,
    )
    mock_client.models.generate_content.return_value = mock_response

    adapter = GeminiAdapter(client=mock_client)
    res = adapter.generate_text("Count tokens")

    assert res.prompt_tokens == 105
    assert res.completion_tokens == 210
    assert res.total_tokens == 315
    assert res.finish_reason == "MAX_TOKENS"


def test_gemini_adapter_validation_and_configuration_errors() -> None:
    """Verifies that invalid arguments raise GeminiAdapterError or GeminiConfigurationError."""
    adapter = GeminiAdapter(test_mode=True)

    # Empty prompt
    with pytest.raises(GeminiAdapterError) as exc1:
        adapter.generate_text("   ")
    assert "cannot be empty" in str(exc1.value)

    # Invalid temperature (< 0 or > 2.0)
    with pytest.raises(GeminiConfigurationError):
        adapter.generate_text("Valid prompt", temperature=-0.1)

    with pytest.raises(GeminiConfigurationError):
        adapter.generate_text("Valid prompt", temperature=2.5)

    # Invalid response schema (not a BaseModel subclass)
    with pytest.raises(GeminiConfigurationError):
        adapter.generate_structured("Valid prompt", str)  # type: ignore[arg-type,type-var]

    # Empty model identifier
    with pytest.raises(GeminiConfigurationError):
        GeminiAdapter(model="  ")


def test_gemini_adapter_empty_chat_messages_raises_error() -> None:
    """Verifies that passing empty list of chat messages raises GeminiAdapterError."""
    adapter = GeminiAdapter(test_mode=True)
    with pytest.raises(GeminiAdapterError) as exc:
        adapter.generate_chat([])
    assert "messages list cannot be empty" in str(exc.value)

    with pytest.raises(GeminiAdapterError) as async_exc:
        import asyncio
        asyncio.run(adapter.agenerate_chat([]))
    assert "messages list cannot be empty" in str(async_exc.value)


def test_get_gemini_adapter_singleton() -> None:
    """Verifies that get_gemini_adapter() returns a singleton cached instance."""
    adapter1 = get_gemini_adapter()
    adapter2 = get_gemini_adapter()
    assert adapter1 is adapter2
    assert isinstance(adapter1, GeminiAdapter)
