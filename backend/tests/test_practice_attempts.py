"""Comprehensive tests for Practice Attempts & Score Tracking API (TASK-M5-04 / US 2.15).

Validates:
- Invariant 2 (Layering): Routers -> Services -> Repositories. Routers NEVER import repositories.
- Invariant 6 (Strict Tenant Isolation): User A cannot start, view, or submit User B's practice attempts.
- Invariant 11 (Essay Grading Integration): Integrates with GradingService for objective & subjective grading.
- Lifecycle: Start Attempt -> List -> Get -> Submit -> Bank Statistics.
- Statistical Aggregation: Total attempts, completed attempts, average score, highest score.
- Error Handling:
  * 401 Unauthorized when token missing or invalid.
  * 404 Not Found for unowned/nonexistent banks and attempts.
  * 409 Conflict when submitting an already graded attempt.
  * 422 Unprocessable Entity when submitting answers with invalid question IDs.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.practice import router as practice_router
from app.core.security import create_access_token
from app.main import app
from app.providers.gemini_adapter import (
    GeminiAdapter,
    GeminiCircuitOpenError,
    GeminiRateLimitError,
    GeminiTimeoutError,
    get_gemini_adapter,
)
from app.providers.usage_logger import (
    AIUsageLog,
    AIUsageLogger,
    get_ai_usage_logger,
)
from app.repositories.practice_repo import (
    PracticeAnswerResult,
    PracticeAttempt,
    PracticeRepository,
    get_practice_repo,
)
from app.repositories.question_repo import (
    Question,
    QuestionBank,
    QuestionRepository,
    get_question_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.grading_service import (
    AttemptNotFoundError,
    EssayGradingResult,
    GradingService,
    QuestionBankNotFoundError,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    RubricCriterionGrade,
    get_grading_service,
)
from app.services.practice_service import (
    AttemptDetailDTO,
    PracticeService,
    StartAttemptDTO,
    get_practice_service,
)
from app.services.quota_service import (
    AIQuotaService,
    QuotaStatus,
    get_quota_service,
)
from app.services.sanitizer import PromptSanitizer, get_prompt_sanitizer

# Ensure practice router is mounted on app
if not any(
    getattr(route, "path", "").startswith("/api/v1/practice") for route in app.routes
):
    app.include_router(practice_router)


# ---------------------------------------------------------------------------
# Test Doubles & In-Memory Fakes
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication in tests."""

    def __init__(self) -> None:
        self.users: dict[uuid.UUID, User] = {}
        self.roles: dict[str, Role] = {
            "student": Role(
                id=uuid.uuid4(),
                name="student",
                description="Default student role.",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        }

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.users.get(user_id)

    async def create_user(
        self,
        email: str,
        password_hash: str = "dummy_hash",
        full_name: str = "Test Student",
        role_name: str = "student",
        current_plan_code: str = "free",
        is_email_verified: bool = True,
        avatar_url: str | None = None,
    ) -> User:
        user_id = uuid.uuid4()
        role = self.roles.get(role_name)
        if not role:
            role = Role(
                id=uuid.uuid4(),
                name=role_name,
                description=f"{role_name} role",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            self.roles[role_name] = role

        user = User(
            id=user_id,
            email=email.lower().strip(),
            password_hash=password_hash,
            is_active=True,
            is_email_verified=is_email_verified,
            current_plan_code=current_plan_code,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        user.roles = [role]
        user.profile = UserProfile(
            user_id=user_id,
            full_name=full_name.strip(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.users[user_id] = user
        return user


class FakeQuestionRepository(QuestionRepository):
    """In-memory QuestionRepository double."""

    def __init__(self) -> None:
        self.banks: dict[uuid.UUID, QuestionBank] = {}
        self.questions: dict[uuid.UUID, Question] = {}

    async def create_bank(
        self,
        user_id: uuid.UUID,
        name: str,
        description: str | None = None,
        course_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> QuestionBank:
        now = datetime.now(UTC)
        b_id = uuid.uuid4()
        bank = QuestionBank(
            id=b_id,
            user_id=user_id,
            name=name,
            description=description,
            course_id=course_id,
            document_id=document_id,
            created_at=now,
            updated_at=now,
        )
        bank.questions = []
        self.banks[b_id] = bank
        return bank

    async def add_questions(
        self,
        bank_id: uuid.UUID,
        questions_data: list[dict[str, Any]],
    ) -> list[Question]:
        created: list[Question] = []
        now = datetime.now(UTC)
        for item in questions_data:
            q_id = uuid.uuid4()
            q = Question(
                id=q_id,
                question_bank_id=bank_id,
                question_type=item["question_type"],
                content=item["content"],
                options=item.get("options"),
                correct_answer=item.get("correct_answer"),
                explanation=item.get("explanation"),
                difficulty=item.get("difficulty", "medium"),
                created_at=now,
                updated_at=now,
            )
            self.questions[q_id] = q
            created.append(q)
            if bank_id in self.banks:
                self.banks[bank_id].questions.append(q)
        return created

    async def get_bank(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID,
    ) -> QuestionBank | None:
        bank = self.banks.get(bank_id)
        if not bank or bank.user_id != user_id:
            return None
        bank.questions = [
            q for q in self.questions.values() if q.question_bank_id == bank_id
        ]
        return bank


class FakePracticeRepository(PracticeRepository):
    """In-memory PracticeRepository double enforcing tenant isolation and DB constraints."""

    def __init__(self, question_repo: FakeQuestionRepository | None = None) -> None:
        self.attempts: dict[uuid.UUID, PracticeAttempt] = {}
        self.results: dict[uuid.UUID, list[PracticeAnswerResult]] = {}
        self.question_repo = question_repo

    async def create_attempt(
        self,
        user_id: uuid.UUID,
        question_bank_id: uuid.UUID,
        max_score: float = 100.0,
    ) -> PracticeAttempt:
        if max_score < 0:
            raise ValueError("max_score must be >= 0")

        now = datetime.now(UTC)
        attempt_id = uuid.uuid4()
        attempt = PracticeAttempt(
            id=attempt_id,
            user_id=user_id,
            question_bank_id=question_bank_id,
            status="in_progress",
            max_score=max_score,
            total_score=None,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        attempt.results = []
        if self.question_repo and question_bank_id in self.question_repo.banks:
            attempt.bank = self.question_repo.banks[question_bank_id]
        self.attempts[attempt_id] = attempt
        self.results[attempt_id] = []
        return attempt

    async def get_attempt(
        self,
        user_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> PracticeAttempt | None:
        attempt = self.attempts.get(attempt_id)
        if not attempt or attempt.user_id != user_id:
            return None
        attempt.results = list(self.results.get(attempt_id, []))
        if self.question_repo and attempt.question_bank_id in self.question_repo.banks:
            attempt.bank = self.question_repo.banks[attempt.question_bank_id]
        return attempt

    async def list_attempts(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[PracticeAttempt], int]:
        user_attempts = [
            a
            for a in self.attempts.values()
            if a.user_id == user_id
            and (bank_id is None or a.question_bank_id == bank_id)
        ]
        user_attempts.sort(key=lambda a: a.created_at, reverse=True)
        total = len(user_attempts)
        paged = user_attempts[skip : skip + limit]
        for a in paged:
            a.results = list(self.results.get(a.id, []))
            if self.question_repo and a.question_bank_id in self.question_repo.banks:
                a.bank = self.question_repo.banks[a.question_bank_id]
        return paged, total

    async def save_answer_results(
        self,
        attempt_id: uuid.UUID,
        results_data: list[dict[str, Any]],
        total_score: float,
        status: str = "graded",
    ) -> PracticeAttempt:
        attempt = self.attempts.get(attempt_id)
        if not attempt:
            raise ValueError(f"Practice attempt {attempt_id} not found.")

        now = datetime.now(UTC)
        attempt.total_score = total_score
        attempt.status = status
        attempt.graded_at = now
        if attempt.submitted_at is None:
            attempt.submitted_at = now

        saved_results: list[PracticeAnswerResult] = []
        for data in results_data:
            q_id = data["question_id"]
            question_obj = (
                self.question_repo.questions.get(q_id)
                if self.question_repo
                else None
            )
            r = PracticeAnswerResult(
                id=uuid.uuid4(),
                attempt_id=attempt.id,
                question_id=q_id,
                answer_text=data.get("answer_text"),
                score=data.get("score"),
                max_score=data.get("max_score"),
                feedback=data.get("feedback"),
                rationale=data.get("rationale"),
                rubric_breakdown=data.get("rubric_breakdown"),
                reference_answer=data.get("reference_answer"),
                grader_model=data.get("grader_model"),
                graded_at=now,
                created_at=now,
                updated_at=now,
            )
            r.question = question_obj  # type: ignore[assignment]
            saved_results.append(r)

        self.results[attempt_id] = saved_results
        attempt.results = saved_results
        if self.question_repo and attempt.question_bank_id in self.question_repo.banks:
            attempt.bank = self.question_repo.banks[attempt.question_bank_id]
        return attempt


class FakeUsageLogger(AIUsageLogger):
    """In-memory telemetry logger test double verifying token records without prompt leaks."""

    def __init__(self) -> None:
        super().__init__(repository=None)
        self.logged_calls: list[dict[str, Any]] = []

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
        estimated_cost: float | Any = None,
        provider: str = "google",
        repository: Any = None,
    ) -> AIUsageLog | None:
        record = {
            "user_id": user_id,
            "feature": feature,
            "model_name": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
            "latency_ms": latency_ms,
            "request_status": request_status,
        }
        self.logged_calls.append(record)
        return None


class FakeQuotaService(AIQuotaService):
    """In-memory quota manager supporting simulated exhaustion and fail-closed errors."""

    def __init__(self) -> None:
        self.is_exhausted: bool = False
        self.is_redis_down: bool = False

    @asynccontextmanager
    async def reserve(
        self,
        user_id: uuid.UUID | str,
        plan_code: str = "free",
        target_date: Any = None,
    ) -> AsyncIterator[QuotaStatus]:
        if self.is_redis_down:
            raise QuotaServiceUnavailableError("Redis is offline (fail-closed).")
        if self.is_exhausted:
            raise QuotaExceededError("Daily AI quota exhausted.")

        uid = uuid.UUID(str(user_id)) if not isinstance(user_id, uuid.UUID) else user_id
        now = datetime.now(UTC)
        yield QuotaStatus(
            user_id=uid,
            plan_code=plan_code,
            limit=10,
            used=1,
            remaining=9,
            reset_at=now + timedelta(days=1),
            is_warning=False,
            can_proceed=True,
        )


class MockGeminiAdapter(GeminiAdapter):
    """Mock GeminiAdapter for structured output grading testing."""

    def __init__(self, response_double: EssayGradingResult | None = None) -> None:
        self.model = "gemini-2.5-flash"
        self.response_double = response_double
        self.call_count = 0
        self.simulate_circuit_open = False
        self.simulate_rate_limit = False
        self.simulate_timeout = False

    async def agenerate_structured(
        self,
        prompt: str,
        response_schema: type[Any],
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> Any:
        self.call_count += 1
        if self.simulate_circuit_open:
            raise GeminiCircuitOpenError("Circuit is OPEN")
        if self.simulate_rate_limit:
            raise GeminiRateLimitError("Rate limit reached")
        if self.simulate_timeout:
            raise GeminiTimeoutError("Request timed out")

        if self.response_double is not None:
            return self.response_double

        return EssayGradingResult(
            score=45.0,
            max_score=50.0,
            feedback="Bài làm phân tích mạch lạc, đúng trọng tâm.",
            rationale="Đầy đủ luận điểm và giải thích chính xác.",
            rubric_breakdown=[
                RubricCriterionGrade(
                    criterion_name="Content Accuracy",
                    max_points=25.0,
                    awarded_points=23.0,
                    comment="Khái niệm chuẩn xác.",
                ),
                RubricCriterionGrade(
                    criterion_name="Completeness",
                    max_points=15.0,
                    awarded_points=14.0,
                    comment="Đủ các luận cứ.",
                ),
                RubricCriterionGrade(
                    criterion_name="Structure",
                    max_points=10.0,
                    awarded_points=8.0,
                    comment="Lập luận chặt chẽ.",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def fake_question_repo() -> FakeQuestionRepository:
    return FakeQuestionRepository()


@pytest.fixture
def fake_practice_repo(fake_question_repo: FakeQuestionRepository) -> FakePracticeRepository:
    return FakePracticeRepository(question_repo=fake_question_repo)


@pytest.fixture
def fake_usage_logger() -> FakeUsageLogger:
    return FakeUsageLogger()


@pytest.fixture
def fake_quota_service() -> FakeQuotaService:
    return FakeQuotaService()


@pytest.fixture
def mock_gemini_adapter() -> MockGeminiAdapter:
    return MockGeminiAdapter()


@pytest.fixture
def sanitizer() -> PromptSanitizer:
    return PromptSanitizer()


@pytest.fixture
def grading_service(
    fake_practice_repo: FakePracticeRepository,
    fake_question_repo: FakeQuestionRepository,
    mock_gemini_adapter: MockGeminiAdapter,
    fake_quota_service: FakeQuotaService,
    fake_usage_logger: FakeUsageLogger,
    sanitizer: PromptSanitizer,
) -> GradingService:
    return GradingService(
        practice_repo=fake_practice_repo,
        question_repo=fake_question_repo,
        gemini_adapter=mock_gemini_adapter,
        quota_service=fake_quota_service,
        usage_logger=fake_usage_logger,
        sanitizer=sanitizer,
    )


@pytest.fixture
def practice_service(
    fake_practice_repo: FakePracticeRepository,
    fake_question_repo: FakeQuestionRepository,
    grading_service: GradingService,
) -> PracticeService:
    return PracticeService(
        practice_repo=fake_practice_repo,
        question_repo=fake_question_repo,
        grading_service=grading_service,
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
    fake_practice_repo: FakePracticeRepository,
    practice_service: PracticeService,
    grading_service: GradingService,
    fake_usage_logger: FakeUsageLogger,
    fake_quota_service: FakeQuotaService,
    mock_gemini_adapter: MockGeminiAdapter,
    sanitizer: PromptSanitizer,
) -> AsyncGenerator[AsyncClient, None]:
    """Provide AsyncClient with all dependencies mocked and overridden."""
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_question_repo] = lambda: fake_question_repo
    app.dependency_overrides[get_practice_repo] = lambda: fake_practice_repo
    app.dependency_overrides[get_practice_service] = lambda: practice_service
    app.dependency_overrides[get_grading_service] = lambda: grading_service
    app.dependency_overrides[get_ai_usage_logger] = lambda: fake_usage_logger
    app.dependency_overrides[get_quota_service] = lambda: fake_quota_service
    app.dependency_overrides[get_gemini_adapter] = lambda: mock_gemini_adapter
    app.dependency_overrides[get_prompt_sanitizer] = lambda: sanitizer

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_headers(user_id: uuid.UUID, role: str = "student") -> dict[str, str]:
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(minutes=60),
    )
    return {"Authorization": f"Bearer {token}"}


def get_error_message(resp_json: dict[str, Any]) -> str:
    """Extract error message whether in detail or standardized envelope format."""
    if "detail" in resp_json and isinstance(resp_json["detail"], str):
        return resp_json["detail"]
    if "error" in resp_json and isinstance(resp_json["error"], dict):
        return str(resp_json["error"].get("message", ""))
    return ""


# ---------------------------------------------------------------------------
# Test Cases: Lifecycle & Features
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_practice_attempt_lifecycle_end_to_end(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
) -> None:
    """Validate full end-to-end lifecycle: Start -> List -> Get -> Submit -> Check Statistics."""
    # 1. Create student and question bank
    student = await fake_user_repo.create_user(email="alice@university.edu")
    headers = make_auth_headers(student.id)

    bank = await fake_question_repo.create_bank(
        user_id=student.id,
        name="Data Structures & Algorithms Bank",
        description="Core practice questions",
    )
    created_questions = await fake_question_repo.add_questions(
        bank.id,
        [
            {
                "question_type": "multiple_choice",
                "content": "What is the average time complexity of quicksort?",
                "options": ["O(n)", "O(n log n)", "O(n^2)", "O(1)"],
                "correct_answer": "O(n log n)",
                "explanation": "Quicksort has average complexity O(n log n).",
                "difficulty": "medium",
            },
            {
                "question_type": "essay",
                "content": "Explain how Dijkstra's algorithm finds the shortest path.",
                "options": None,
                "correct_answer": "Dijkstra uses a greedy strategy and priority queue.",
                "explanation": "Covers non-negative edge weights and relaxation.",
                "difficulty": "hard",
            },
        ],
    )
    mcq_q = created_questions[0]
    essay_q = created_questions[1]

    # 2. Start attempt (POST /banks/{bank_id}/attempts) -> 201 Created
    start_resp = await client.post(
        f"/api/v1/practice/banks/{bank.id}/attempts",
        headers=headers,
    )
    assert start_resp.status_code == 201
    start_data = start_resp.json()
    assert start_data["success"] is True
    attempt_id = uuid.UUID(start_data["data"]["attempt_id"])
    assert start_data["data"]["bank_id"] == str(bank.id)
    assert start_data["data"]["bank_name"] == bank.name
    assert start_data["data"]["status"] == "in_progress"
    assert start_data["data"]["question_count"] == 2

    # 3. List attempts (GET /attempts) -> 200 OK
    list_resp = await client.get("/api/v1/practice/attempts", headers=headers)
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["success"] is True
    assert len(list_data["data"]) == 1
    assert list_data["data"][0]["id"] == str(attempt_id)
    assert list_data["data"][0]["status"] == "in_progress"
    assert list_data["meta"]["total"] == 1

    # 4. Get attempt details before submit (GET /attempts/{attempt_id}) -> 200 OK
    get_resp = await client.get(
        f"/api/v1/practice/attempts/{attempt_id}", headers=headers
    )
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["success"] is True
    assert get_data["data"]["id"] == str(attempt_id)
    assert get_data["data"]["status"] == "in_progress"
    assert get_data["data"]["total_score"] is None
    # Questions are loaded in results for student to solve
    assert len(get_data["data"]["results"]) == 2
    assert get_data["data"]["results"][0]["score"] is None

    # 5. Submit attempt (POST /attempts/{attempt_id}/submit) -> 200 OK
    submit_payload = {
        "answers": [
            {
                "question_id": str(mcq_q.id),
                "answer_text": "O(n log n)",
            },
            {
                "question_id": str(essay_q.id),
                "answer_text": "Dijkstra algorithm iteratively selects vertex with smallest tentative distance and relaxes outgoing edges.",
            },
        ]
    }
    submit_resp = await client.post(
        f"/api/v1/practice/attempts/{attempt_id}/submit",
        json=submit_payload,
        headers=headers,
    )
    assert submit_resp.status_code == 200
    submit_data = submit_resp.json()
    assert submit_data["success"] is True
    res_data = submit_data["data"]
    assert res_data["id"] == str(attempt_id)
    assert res_data["status"] == "graded"
    assert res_data["total_score"] is not None
    assert res_data["total_score"] > 0
    assert res_data["percentage"] is not None
    assert len(res_data["results"]) == 2

    # Check MCQ result (score: 50.0, feedback contains 'Chính xác')
    mcq_res = next(r for r in res_data["results"] if r["question_id"] == str(mcq_q.id))
    assert mcq_res["score"] == 50.0
    assert "Chính xác" in (mcq_res["feedback"] or "")

    # Check Essay result (score: 45.0 from mock, feedback, rubric breakdown)
    essay_res = next(r for r in res_data["results"] if r["question_id"] == str(essay_q.id))
    assert essay_res["score"] == 45.0
    assert len(essay_res["rubric_breakdown"]) == 3

    # 6. Get attempt details after submit (GET /attempts/{attempt_id}) -> 200 OK
    get_after = await client.get(
        f"/api/v1/practice/attempts/{attempt_id}", headers=headers
    )
    assert get_after.status_code == 200
    after_data = get_after.json()["data"]
    assert after_data["status"] == "graded"
    assert after_data["total_score"] == 95.0
    assert after_data["submitted_at"] is not None
    assert after_data["graded_at"] is not None

    # 7. Check bank statistics (GET /banks/{bank_id}/statistics) -> 200 OK
    stats_resp = await client.get(
        f"/api/v1/practice/banks/{bank.id}/statistics", headers=headers
    )
    assert stats_resp.status_code == 200
    stats = stats_resp.json()["data"]
    assert stats["bank_id"] == str(bank.id)
    assert stats["total_attempts"] == 1
    assert stats["completed_attempts"] == 1
    assert stats["average_score"] == 95.0
    assert stats["highest_score"] == 95.0
    assert stats["max_possible_score"] == 100.0


@pytest.mark.asyncio
async def test_multiple_attempts_and_statistical_aggregation(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
) -> None:
    """Verify statistical metrics (average_score, highest_score) across multiple attempts."""
    student = await fake_user_repo.create_user(email="bob@university.edu")
    headers = make_auth_headers(student.id)

    bank = await fake_question_repo.create_bank(user_id=student.id, name="Physics 101")
    questions = await fake_question_repo.add_questions(
        bank.id,
        [
            {
                "question_type": "multiple_choice",
                "content": "Speed of light in vacuum?",
                "options": ["3e8 m/s", "1e6 m/s"],
                "correct_answer": "3e8 m/s",
                "explanation": "c = 3x10^8 m/s",
                "difficulty": "easy",
            }
        ],
    )
    q = questions[0]

    # Attempt 1: Correct answer (Score: 100.0)
    a1_resp = await client.post(f"/api/v1/practice/banks/{bank.id}/attempts", headers=headers)
    a1_id = a1_resp.json()["data"]["attempt_id"]
    await client.post(
        f"/api/v1/practice/attempts/{a1_id}/submit",
        json={"answers": [{"question_id": str(q.id), "answer_text": "3e8 m/s"}]},
        headers=headers,
    )

    # Attempt 2: Incorrect answer (Score: 0.0)
    a2_resp = await client.post(f"/api/v1/practice/banks/{bank.id}/attempts", headers=headers)
    a2_id = a2_resp.json()["data"]["attempt_id"]
    await client.post(
        f"/api/v1/practice/attempts/{a2_id}/submit",
        json={"answers": [{"question_id": str(q.id), "answer_text": "1e6 m/s"}]},
        headers=headers,
    )

    # Attempt 3: In progress, not submitted yet
    a3_resp = await client.post(f"/api/v1/practice/banks/{bank.id}/attempts", headers=headers)
    assert a3_resp.status_code == 201

    # Check statistics aggregation
    stats_resp = await client.get(
        f"/api/v1/practice/banks/{bank.id}/statistics", headers=headers
    )
    assert stats_resp.status_code == 200
    stats = stats_resp.json()["data"]
    assert stats["total_attempts"] == 3
    assert stats["completed_attempts"] == 2
    assert stats["average_score"] == 50.0  # (100.0 + 0.0) / 2
    assert stats["highest_score"] == 100.0
    assert stats["max_possible_score"] == 100.0


@pytest.mark.asyncio
async def test_bank_statistics_with_no_attempts(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
) -> None:
    """Verify statistics for a bank with zero attempts returns zeroed metrics."""
    student = await fake_user_repo.create_user(email="empty@university.edu")
    headers = make_auth_headers(student.id)

    bank = await fake_question_repo.create_bank(user_id=student.id, name="Empty Attempts Bank")

    stats_resp = await client.get(
        f"/api/v1/practice/banks/{bank.id}/statistics", headers=headers
    )
    assert stats_resp.status_code == 200
    stats = stats_resp.json()["data"]
    assert stats["total_attempts"] == 0
    assert stats["completed_attempts"] == 0
    assert stats["average_score"] == 0.0
    assert stats["highest_score"] == 0.0
    assert stats["max_possible_score"] == 100.0


# ---------------------------------------------------------------------------
# Test Cases: Tenant Isolation & Security
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_tenant_isolation_user_a_cannot_access_user_b_attempt(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
) -> None:
    """Strict tenant isolation: User B cannot start attempt on, view, submit, or check stats for User A's bank."""
    user_a = await fake_user_repo.create_user(email="usera@university.edu")
    user_b = await fake_user_repo.create_user(email="userb@university.edu")

    headers_a = make_auth_headers(user_a.id)
    headers_b = make_auth_headers(user_b.id)

    # User A creates bank and questions
    bank_a = await fake_question_repo.create_bank(user_id=user_a.id, name="User A Bank")
    questions = await fake_question_repo.add_questions(
        bank_a.id,
        [
            {
                "question_type": "multiple_choice",
                "content": "Tenant isolation question?",
                "options": ["A", "B"],
                "correct_answer": "A",
            }
        ],
    )

    # 1. User B cannot start attempt on User A's bank -> 404
    start_resp_b = await client.post(
        f"/api/v1/practice/banks/{bank_a.id}/attempts", headers=headers_b
    )
    assert start_resp_b.status_code == 404

    # User A starts an attempt
    start_resp_a = await client.post(
        f"/api/v1/practice/banks/{bank_a.id}/attempts", headers=headers_a
    )
    assert start_resp_a.status_code == 201
    attempt_a_id = start_resp_a.json()["data"]["attempt_id"]

    # 2. User B cannot view User A's attempt -> 404
    get_resp_b = await client.get(
        f"/api/v1/practice/attempts/{attempt_a_id}", headers=headers_b
    )
    assert get_resp_b.status_code == 404

    # 3. User B cannot submit answers to User A's attempt -> 404
    submit_resp_b = await client.post(
        f"/api/v1/practice/attempts/{attempt_a_id}/submit",
        json={"answers": [{"question_id": str(questions[0].id), "answer_text": "A"}]},
        headers=headers_b,
    )
    assert submit_resp_b.status_code == 404

    # 4. User B listing attempts does NOT see User A's attempt
    list_resp_b = await client.get("/api/v1/practice/attempts", headers=headers_b)
    assert list_resp_b.status_code == 200
    assert len(list_resp_b.json()["data"]) == 0

    # 5. User B cannot access statistics of User A's bank -> 404
    stats_resp_b = await client.get(
        f"/api/v1/practice/banks/{bank_a.id}/statistics", headers=headers_b
    )
    assert stats_resp_b.status_code == 404


@pytest.mark.asyncio
async def test_submitting_already_graded_attempt_returns_409(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
) -> None:
    """Submitting answers to an already graded attempt returns 409 Conflict."""
    student = await fake_user_repo.create_user(email="conflict@university.edu")
    headers = make_auth_headers(student.id)

    bank = await fake_question_repo.create_bank(user_id=student.id, name="Double Submit Bank")
    q = (
        await fake_question_repo.add_questions(
            bank.id,
            [
                {
                    "question_type": "multiple_choice",
                    "content": "Sample MCQ?",
                    "options": ["1", "2"],
                    "correct_answer": "1",
                }
            ],
        )
    )[0]

    start_resp = await client.post(f"/api/v1/practice/banks/{bank.id}/attempts", headers=headers)
    attempt_id = start_resp.json()["data"]["attempt_id"]

    # First submission -> 200 OK
    payload = {"answers": [{"question_id": str(q.id), "answer_text": "1"}]}
    first_resp = await client.post(
        f"/api/v1/practice/attempts/{attempt_id}/submit",
        json=payload,
        headers=headers,
    )
    assert first_resp.status_code == 200
    assert first_resp.json()["data"]["status"] == "graded"

    # Second submission on same attempt -> 409 Conflict
    second_resp = await client.post(
        f"/api/v1/practice/attempts/{attempt_id}/submit",
        json=payload,
        headers=headers,
    )
    assert second_resp.status_code == 409
    error_detail = get_error_message(second_resp.json())
    assert "status 'graded'" in error_detail or "graded again" in error_detail


@pytest.mark.asyncio
async def test_submit_answers_with_invalid_question_id_returns_422(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
) -> None:
    """Submitting answers with a question ID that does not belong to the bank returns 422."""
    student = await fake_user_repo.create_user(email="invalid_q@university.edu")
    headers = make_auth_headers(student.id)

    bank = await fake_question_repo.create_bank(user_id=student.id, name="Invalid Q Bank")
    await fake_question_repo.add_questions(
        bank.id,
        [
            {
                "question_type": "multiple_choice",
                "content": "Valid question?",
                "options": ["A", "B"],
                "correct_answer": "A",
            }
        ],
    )

    start_resp = await client.post(f"/api/v1/practice/banks/{bank.id}/attempts", headers=headers)
    attempt_id = start_resp.json()["data"]["attempt_id"]

    # Submit with an alien question UUID
    alien_qid = uuid.uuid4()
    payload = {"answers": [{"question_id": str(alien_qid), "answer_text": "Arbitrary"}]}

    resp = await client.post(
        f"/api/v1/practice/attempts/{attempt_id}/submit",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 422
    assert "does not belong" in get_error_message(resp.json())


@pytest.mark.asyncio
async def test_submit_partial_answers_succeeds_unanswered_get_zero(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
) -> None:
    """Submitting partial answers grades missing questions as 0 without failing."""
    student = await fake_user_repo.create_user(email="partial@university.edu")
    headers = make_auth_headers(student.id)

    bank = await fake_question_repo.create_bank(user_id=student.id, name="Partial Answer Bank")
    questions = await fake_question_repo.add_questions(
        bank.id,
        [
            {
                "question_type": "multiple_choice",
                "content": "Question 1?",
                "options": ["A", "B"],
                "correct_answer": "A",
            },
            {
                "question_type": "multiple_choice",
                "content": "Question 2?",
                "options": ["C", "D"],
                "correct_answer": "C",
            },
        ],
    )

    start_resp = await client.post(f"/api/v1/practice/banks/{bank.id}/attempts", headers=headers)
    attempt_id = start_resp.json()["data"]["attempt_id"]

    # Student only answers Question 1, leaves Question 2 omitted
    payload = {"answers": [{"question_id": str(questions[0].id), "answer_text": "A"}]}

    resp = await client.post(
        f"/api/v1/practice/attempts/{attempt_id}/submit",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_score"] == 50.0  # 1 out of 2 correct
    q2_result = next(r for r in data["results"] if r["question_id"] == str(questions[1].id))
    assert q2_result["score"] == 0.0


# ---------------------------------------------------------------------------
# Test Cases: Authentication & Authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_requests_return_401(client: AsyncClient) -> None:
    """All practice endpoints return 401 Unauthorized when token is missing."""
    dummy_uuid = uuid.uuid4()

    # 1. Start attempt
    r1 = await client.post(f"/api/v1/practice/banks/{dummy_uuid}/attempts")
    assert r1.status_code == 401

    # 2. List attempts
    r2 = await client.get("/api/v1/practice/attempts")
    assert r2.status_code == 401

    # 3. Get attempt
    r3 = await client.get(f"/api/v1/practice/attempts/{dummy_uuid}")
    assert r3.status_code == 401

    # 4. Submit attempt
    r4 = await client.post(
        f"/api/v1/practice/attempts/{dummy_uuid}/submit",
        json={"answers": []},
    )
    assert r4.status_code == 401

    # 5. Bank statistics
    r5 = await client.get(f"/api/v1/practice/banks/{dummy_uuid}/statistics")
    assert r5.status_code == 401


# ---------------------------------------------------------------------------
# Test Cases: Filtering, Pagination & AI Failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_attempts_filtering_by_bank_id_and_pagination(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
) -> None:
    """Listing attempts properly filters by bank_id and obeys skip/limit pagination."""
    student = await fake_user_repo.create_user(email="filter@university.edu")
    headers = make_auth_headers(student.id)

    bank1 = await fake_question_repo.create_bank(user_id=student.id, name="Bank One")
    bank2 = await fake_question_repo.create_bank(user_id=student.id, name="Bank Two")

    # Create 3 attempts on Bank 1, 1 attempt on Bank 2
    for _ in range(3):
        await client.post(f"/api/v1/practice/banks/{bank1.id}/attempts", headers=headers)
    await client.post(f"/api/v1/practice/banks/{bank2.id}/attempts", headers=headers)

    # Filter by bank1
    resp_b1 = await client.get(
        f"/api/v1/practice/attempts?bank_id={bank1.id}", headers=headers
    )
    assert resp_b1.status_code == 200
    assert len(resp_b1.json()["data"]) == 3

    # Filter by bank2
    resp_b2 = await client.get(
        f"/api/v1/practice/attempts?bank_id={bank2.id}", headers=headers
    )
    assert resp_b2.status_code == 200
    assert len(resp_b2.json()["data"]) == 1

    # Pagination: skip=1, limit=2 on all
    resp_paged = await client.get(
        "/api/v1/practice/attempts?skip=1&limit=2", headers=headers
    )
    assert resp_paged.status_code == 200
    assert len(resp_paged.json()["data"]) == 2
    assert resp_paged.json()["meta"]["total"] == 4
    assert resp_paged.json()["meta"]["skip"] == 1
    assert resp_paged.json()["meta"]["limit"] == 2


@pytest.mark.asyncio
async def test_quota_exhausted_returns_429(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
    fake_quota_service: FakeQuotaService,
) -> None:
    """When daily AI quota is exhausted, submitting essay answers returns 429 Too Many Requests."""
    student = await fake_user_repo.create_user(email="quota@university.edu")
    headers = make_auth_headers(student.id)

    bank = await fake_question_repo.create_bank(user_id=student.id, name="Quota Bank")
    q = (
        await fake_question_repo.add_questions(
            bank.id,
            [
                {
                    "question_type": "essay",
                    "content": "Explain AI quota management.",
                    "options": None,
                    "correct_answer": "Tokens are tracked.",
                }
            ],
        )
    )[0]

    start_resp = await client.post(f"/api/v1/practice/banks/{bank.id}/attempts", headers=headers)
    attempt_id = start_resp.json()["data"]["attempt_id"]

    # Simulate quota exhausted
    fake_quota_service.is_exhausted = True

    submit_resp = await client.post(
        f"/api/v1/practice/attempts/{attempt_id}/submit",
        json={"answers": [{"question_id": str(q.id), "answer_text": "Answer"}]},
        headers=headers,
    )
    assert submit_resp.status_code == 429
    assert "quota" in get_error_message(submit_resp.json()).lower()


@pytest.mark.asyncio
async def test_quota_service_down_fails_closed_with_503(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_question_repo: FakeQuestionRepository,
    fake_quota_service: FakeQuotaService,
) -> None:
    """When quota manager is down (Redis offline), system fails closed with 503."""
    student = await fake_user_repo.create_user(email="redis_down@university.edu")
    headers = make_auth_headers(student.id)

    bank = await fake_question_repo.create_bank(user_id=student.id, name="Redis Down Bank")
    q = (
        await fake_question_repo.add_questions(
            bank.id,
            [
                {
                    "question_type": "essay",
                    "content": "Fail closed test?",
                    "options": None,
                    "correct_answer": "Fail closed.",
                }
            ],
        )
    )[0]

    start_resp = await client.post(f"/api/v1/practice/banks/{bank.id}/attempts", headers=headers)
    attempt_id = start_resp.json()["data"]["attempt_id"]

    fake_quota_service.is_redis_down = True

    submit_resp = await client.post(
        f"/api/v1/practice/attempts/{attempt_id}/submit",
        json={"answers": [{"question_id": str(q.id), "answer_text": "Answer"}]},
        headers=headers,
    )
    assert submit_resp.status_code == 503
    assert "fail-closed" in get_error_message(submit_resp.json()).lower()


# ---------------------------------------------------------------------------
# Test Cases: Direct Service Layer Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_practice_service_direct_unit_tests(
    practice_service: PracticeService,
    fake_question_repo: FakeQuestionRepository,
) -> None:
    """Directly test PracticeService methods, domain exceptions, and edge cases."""
    user_id = uuid.uuid4()
    bank = await fake_question_repo.create_bank(user_id=user_id, name="Direct Unit Test Bank")

    # 1. Nonexistent bank raises QuestionBankNotFoundError
    with pytest.raises(QuestionBankNotFoundError):
        await practice_service.start_attempt(user_id=user_id, bank_id=uuid.uuid4())

    with pytest.raises(QuestionBankNotFoundError):
        await practice_service.get_bank_statistics(user_id=user_id, bank_id=uuid.uuid4())

    # 2. Nonexistent attempt raises AttemptNotFoundError
    with pytest.raises(AttemptNotFoundError):
        await practice_service.get_attempt(user_id=user_id, attempt_id=uuid.uuid4())

    with pytest.raises(AttemptNotFoundError):
        await practice_service.submit_attempt(
            user_id=user_id, attempt_id=uuid.uuid4(), answers=[]
        )

    # 3. Start attempt succeeds
    start_dto = await practice_service.start_attempt(user_id=user_id, bank_id=bank.id)
    assert isinstance(start_dto, StartAttemptDTO)
    assert start_dto.bank_name == "Direct Unit Test Bank"
    assert start_dto.status == "in_progress"

    # 4. Get attempt returns AttemptDetailDTO
    detail_dto = await practice_service.get_attempt(
        user_id=user_id, attempt_id=start_dto.attempt_id
    )
    assert isinstance(detail_dto, AttemptDetailDTO)
    assert detail_dto.id == start_dto.attempt_id
    assert detail_dto.status == "in_progress"
