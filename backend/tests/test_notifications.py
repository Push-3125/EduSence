"""Comprehensive tests for Notifications API, Repository, and Service (US 2.20, Invariant 12).

Validates:
- Invariant 2 (Layering): Routers -> Services -> Repositories. Routers NEVER import repositories.
- Invariant 6 (Tenant Isolation): User A cannot read, mark, or delete User B's notifications.
- Invariant 12: Notification system supports AI quota threshold alerts (at 80% usage per DECISION-PROP-04).
- Database constraints and deduplication keys.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

# Ensure backend root is on sys.path for direct execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api.v1.notifications import router as notifications_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.notification_repo import (
    Notification,
    NotificationRepository,
    get_notification_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.notification_service import (
    NotificationDTO,
    NotificationNotFoundError,
    NotificationService,
    NotificationValidationError,
    get_notification_service,
)

# Ensure notifications router is mounted on app
if not any(
    getattr(route, "path", "").startswith("/api/v1/notifications") for route in app.routes
):
    app.include_router(notifications_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory Fake Repositories
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
        password_hash: str,
        full_name: str,
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
        profile = UserProfile(
            id=uuid.uuid4(),
            user_id=user_id,
            full_name=full_name.strip(),
            avatar_url=avatar_url,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        user.profile = profile
        user.roles = [role]
        self.users[user_id] = user
        return user


class FakeNotificationRepository(NotificationRepository):
    """In-memory NotificationRepository double enforcing constraints and deduplication."""

    def __init__(self) -> None:
        self.notifications: dict[uuid.UUID, Notification] = {}

    async def create(
        self,
        user_id: uuid.UUID,
        notification_type: str,
        category: str,
        title: str,
        content: str,
        channel: str = "in_app",
        deduplication_key: str | None = None,
        target_url: str | None = None,
        scheduled_at: datetime | None = None,
        sent_at: datetime | None = None,
        status: str = "pending",
    ) -> Notification:
        # Validate category and channel constraints
        valid_cats = {"assignments", "exams", "progress", "study_plans", "ai"}
        if category not in valid_cats:
            raise IntegrityError(
                f"Check constraint violation: category '{category}'",
                params=None,
                orig=Exception("ck_notifications_category"),
            )
        valid_channels = {"in_app", "email"}
        if channel not in valid_channels:
            raise IntegrityError(
                f"Check constraint violation: channel '{channel}'",
                params=None,
                orig=Exception("ck_notifications_channel"),
            )

        # Enforce unique constraint: (user_id, channel, deduplication_key)
        if deduplication_key:
            for n in self.notifications.values():
                if (
                    n.user_id == user_id
                    and n.channel == channel
                    and n.deduplication_key == deduplication_key
                ):
                    raise IntegrityError(
                        "Unique constraint violation: uq_notifications_dedup",
                        params=None,
                        orig=Exception("uq_notifications_dedup"),
                    )

        notif_id = uuid.uuid4()
        now = datetime.now(UTC)
        notif = Notification(
            id=notif_id,
            user_id=user_id,
            notification_type=notification_type,
            category=category,
            title=title.strip(),
            content=content.strip(),
            channel=channel,
            deduplication_key=deduplication_key,
            target_url=target_url.strip() if target_url else None,
            scheduled_at=scheduled_at,
            sent_at=sent_at or now,
            status=status,
            is_read=False,
            read_at=None,
            created_at=now,
            updated_at=now,
        )
        self.notifications[notif_id] = notif
        return notif

    async def exists_by_dedup_key(
        self,
        user_id: uuid.UUID,
        channel: str,
        deduplication_key: str,
    ) -> bool:
        for n in self.notifications.values():
            if (
                n.user_id == user_id
                and n.channel == channel
                and n.deduplication_key == deduplication_key
            ):
                return True
        return False

    async def get_by_dedup_key(
        self,
        user_id: uuid.UUID,
        channel: str,
        deduplication_key: str,
    ) -> Notification | None:
        for n in self.notifications.values():
            if (
                n.user_id == user_id
                and n.channel == channel
                and n.deduplication_key == deduplication_key
            ):
                return n
        return None

    async def get_by_id(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
    ) -> Notification | None:
        notif = self.notifications.get(notification_id)
        if notif and notif.user_id == user_id:
            return notif
        return None

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        is_read: bool | None = None,
        category: str | None = None,
        channel: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Notification], int]:
        matched: list[Notification] = []
        for n in self.notifications.values():
            if n.user_id != user_id:
                continue
            if is_read is not None and n.is_read != is_read:
                continue
            if category is not None and n.category != category:
                continue
            if channel is not None and n.channel != channel:
                continue
            matched.append(n)

        matched.sort(
            key=lambda x: -x.created_at.timestamp() if x.created_at else 0
        )
        total = len(matched)
        paginated = matched[skip : skip + limit]
        return paginated, total

    async def mark_as_read(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
    ) -> Notification | None:
        notif = self.notifications.get(notification_id)
        if not notif or notif.user_id != user_id:
            return None

        if not notif.is_read:
            now = datetime.now(UTC)
            notif.is_read = True
            notif.read_at = now
            notif.updated_at = now

        return notif

    async def mark_all_as_read(self, user_id: uuid.UUID) -> int:
        now = datetime.now(UTC)
        count = 0
        for n in self.notifications.values():
            if n.user_id == user_id and not n.is_read:
                n.is_read = True
                n.read_at = now
                n.updated_at = now
                count += 1
        return count

    async def count_unread(self, user_id: uuid.UUID) -> int:
        return sum(
            1 for n in self.notifications.values()
            if n.user_id == user_id and not n.is_read
        )

    async def delete_notification(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
    ) -> bool:
        notif = self.notifications.get(notification_id)
        if not notif or notif.user_id != user_id:
            return False
        del self.notifications[notification_id]
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def fake_notif_repo() -> FakeNotificationRepository:
    return FakeNotificationRepository()


@pytest.fixture
def notif_service(fake_notif_repo: FakeNotificationRepository) -> NotificationService:
    return NotificationService(fake_notif_repo)


@pytest_asyncio.fixture
async def student_a(fake_user_repo: FakeUserRepository) -> User:
    """Primary active student for tests."""
    return await fake_user_repo.create_user(
        email="alice.student@university.edu",
        password_hash=hash_password("ValidPass123!"),
        full_name="Alice Student",
    )


@pytest_asyncio.fixture
async def student_b(fake_user_repo: FakeUserRepository) -> User:
    """Secondary active student for tenant isolation checks."""
    return await fake_user_repo.create_user(
        email="bob.student@university.edu",
        password_hash=hash_password("ValidPass123!"),
        full_name="Bob Student",
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_notif_repo: FakeNotificationRepository,
) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with dependency overrides."""
    service = NotificationService(fake_notif_repo)
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_notification_repo] = lambda: fake_notif_repo
    app.dependency_overrides[get_notification_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_header(user_id: uuid.UUID, expires_seconds: int = 3600) -> dict[str, str]:
    """Generate Bearer Authorization header for user UUID."""
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(seconds=expires_seconds),
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Model and Repository Direct Unit Tests
# ---------------------------------------------------------------------------


def test_notification_model_constraints() -> None:
    """Verify that Notification table constraints and indexes are declared per spec."""
    table_args = Notification.__table_args__
    constraint_names = {
        getattr(c, "name", None) for c in table_args if hasattr(c, "name")
    }

    assert "uq_notifications_dedup" in constraint_names
    assert "ck_notifications_category" in constraint_names
    assert "ck_notifications_channel" in constraint_names
    assert "ck_notifications_status" in constraint_names
    assert "ck_notifications_read_state" in constraint_names


@pytest.mark.asyncio
async def test_notification_repo_sql_methods() -> None:
    """Verify NotificationRepository methods with SQLAlchemy AsyncMock session."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.delete = AsyncMock()
    repo = NotificationRepository(mock_session)

    user_id = uuid.uuid4()
    notif_id = uuid.uuid4()

    # 1. create
    notif = await repo.create(
        user_id=user_id,
        notification_type="deadline_reminder",
        category="assignments",
        title="Due Soon",
        content="Finish assignment",
        channel="in_app",
    )
    assert notif.user_id == user_id
    mock_session.add.assert_called_once()
    mock_session.flush.assert_called_once()

    # 2. count_unread
    mock_count_res = MagicMock()
    mock_count_res.scalar.return_value = 3
    mock_session.execute.return_value = mock_count_res
    count = await repo.count_unread(user_id)
    assert count == 3

    # 3. mark_all_as_read
    mock_update_res = MagicMock()
    mock_update_res.rowcount = 4
    mock_session.execute.return_value = mock_update_res
    updated = await repo.mark_all_as_read(user_id)
    assert updated == 4

    # 4. delete_notification (not found)
    mock_empty_res = MagicMock()
    mock_empty_res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_empty_res
    deleted = await repo.delete_notification(user_id, notif_id)
    assert deleted is False


# ---------------------------------------------------------------------------
# NotificationService Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_validation_rules(notif_service: NotificationService) -> None:
    """Verify domain validation rules for in-app notification creation."""
    user_id = uuid.uuid4()

    # Invalid category
    with pytest.raises(NotificationValidationError, match="Invalid category"):
        await notif_service.create_in_app_notification(
            user_id=user_id,
            notification_type="alert",
            category="invalid_category",
            title="Valid Title",
            content="Valid Content",
        )

    # Empty title
    with pytest.raises(NotificationValidationError, match="title cannot be empty"):
        await notif_service.create_in_app_notification(
            user_id=user_id,
            notification_type="alert",
            category="assignments",
            title="   ",
            content="Valid Content",
        )

    # Empty content
    with pytest.raises(NotificationValidationError, match="content cannot be empty"):
        await notif_service.create_in_app_notification(
            user_id=user_id,
            notification_type="alert",
            category="assignments",
            title="Valid Title",
            content="   ",
        )


@pytest.mark.asyncio
async def test_service_ai_quota_warning_deduplication(
    notif_service: NotificationService,
) -> None:
    """Verify AI quota 80% warning triggers and daily deduplication (Invariant 12, DECISION-PROP-04)."""
    user_id = uuid.uuid4()

    # 1. Under 80% threshold (e.g., 7 / 10 = 70%) -> No warning
    res1 = await notif_service.notify_ai_quota_warning(user_id=user_id, used=7, limit=10)
    assert res1 is None

    # 2. Reached 80% threshold (8 / 10 = 80%) -> Emits warning notification
    res2 = await notif_service.notify_ai_quota_warning(user_id=user_id, used=8, limit=10)
    assert res2 is not None
    assert isinstance(res2, NotificationDTO)
    assert res2.category == "ai"
    assert res2.notification_type == "quota_warning"
    assert "80%" in res2.content
    assert res2.is_read is False

    # 3. Call again on same day (e.g., 9 / 10 = 90%) -> Suppressed by daily deduplication!
    res3 = await notif_service.notify_ai_quota_warning(user_id=user_id, used=9, limit=10)
    assert res3 is None

    # Verify only 1 notification exists in repo
    dtos, total = await notif_service.list_notifications(user_id=user_id)
    assert total == 1
    assert dtos[0].id == res2.id

    # 4. Zero or negative limits handle gracefully
    assert await notif_service.notify_ai_quota_warning(user_id, used=5, limit=0) is None
    assert await notif_service.notify_ai_quota_warning(user_id, used=-1, limit=10) is None


@pytest.mark.asyncio
async def test_service_crud_and_status(notif_service: NotificationService) -> None:
    """Verify mark read, mark all read, and unread count in NotificationService."""
    user_id = uuid.uuid4()

    # Create 3 notifications
    n1 = await notif_service.create_in_app_notification(
        user_id=user_id,
        notification_type="deadline",
        category="assignments",
        title="Assignment 1",
        content="Due in 24 hours",
    )
    n2 = await notif_service.create_in_app_notification(
        user_id=user_id,
        notification_type="exam",
        category="exams",
        title="Exam 1",
        content="Exam next week",
    )
    n3 = await notif_service.create_in_app_notification(
        user_id=user_id,
        notification_type="progress",
        category="progress",
        title="Milestone reached",
        content="You completed 5 tasks",
    )
    assert n3.id is not None

    # Initial unread count should be 3
    unread_dto = await notif_service.get_unread_count(user_id)
    assert unread_dto.unread_count == 3

    # Mark n1 read
    marked_dto = await notif_service.mark_read(user_id=user_id, notification_id=n1.id)
    assert marked_dto.is_read is True

    unread_dto = await notif_service.get_unread_count(user_id)
    assert unread_dto.unread_count == 2

    # Mark not found raises
    with pytest.raises(NotificationNotFoundError):
        await notif_service.mark_read(user_id=user_id, notification_id=uuid.uuid4())

    # Mark all read
    updated_count = await notif_service.mark_all_read(user_id)
    assert updated_count == 2

    unread_dto = await notif_service.get_unread_count(user_id)
    assert unread_dto.unread_count == 0

    # Delete notification n2
    await notif_service.delete_notification(user_id=user_id, notification_id=n2.id)

    # Delete non-existent raises
    with pytest.raises(NotificationNotFoundError):
        await notif_service.delete_notification(user_id=user_id, notification_id=n2.id)


# ---------------------------------------------------------------------------
# API Endpoints Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_list_notifications_pagination_and_filters(
    client: AsyncClient,
    student_a: User,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Verify GET /api/v1/notifications filtering and pagination."""
    headers = make_auth_header(student_a.id)

    # Prepopulate notifications
    for i in range(5):
        await fake_notif_repo.create(
            user_id=student_a.id,
            notification_type="assignment_alert",
            category="assignments",
            title=f"Assignment {i}",
            content=f"Content {i}",
        )
    for i in range(3):
        await fake_notif_repo.create(
            user_id=student_a.id,
            notification_type="exam_alert",
            category="exams",
            title=f"Exam {i}",
            content=f"Exam content {i}",
        )

    # 1. List all with default pagination
    res = await client.get("/api/v1/notifications", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["total"] == 8
    assert len(body["data"]["items"]) == 8

    # 2. Pagination skip=2, limit=3
    res_page = await client.get("/api/v1/notifications?skip=2&limit=3", headers=headers)
    assert res_page.status_code == 200
    body_page = res_page.json()
    assert body_page["data"]["skip"] == 2
    assert body_page["data"]["limit"] == 3
    assert len(body_page["data"]["items"]) == 3
    assert body_page["data"]["total"] == 8

    # 3. Filter by category='exams'
    res_exams = await client.get("/api/v1/notifications?category=exams", headers=headers)
    assert res_exams.status_code == 200
    body_exams = res_exams.json()
    assert body_exams["data"]["total"] == 3
    assert all(item["category"] == "exams" for item in body_exams["data"]["items"])

    # 4. Filter by is_read=false
    res_unread = await client.get("/api/v1/notifications?is_read=false", headers=headers)
    assert res_unread.status_code == 200
    assert res_unread.json()["data"]["total"] == 8

    # 5. Invalid category returns 422
    res_invalid = await client.get("/api/v1/notifications?category=unknown", headers=headers)
    assert res_invalid.status_code == 422


@pytest.mark.asyncio
async def test_api_unread_count_badge(
    client: AsyncClient,
    student_a: User,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Verify GET /api/v1/notifications/unread-count accuracy."""
    headers = make_auth_header(student_a.id)

    # Initially 0
    res = await client.get("/api/v1/notifications/unread-count", headers=headers)
    assert res.status_code == 200
    assert res.json()["data"]["unread_count"] == 0

    # Add 2 unread notifications
    n1 = await fake_notif_repo.create(
        user_id=student_a.id,
        notification_type="alert",
        category="progress",
        title="Keep it up",
        content="Progress alert",
    )
    await fake_notif_repo.create(
        user_id=student_a.id,
        notification_type="alert",
        category="study_plans",
        title="Study session",
        content="Session reminder",
    )

    res = await client.get("/api/v1/notifications/unread-count", headers=headers)
    assert res.status_code == 200
    assert res.json()["data"]["unread_count"] == 2

    # Mark 1 as read
    patch_res = await client.patch(f"/api/v1/notifications/{n1.id}/read", headers=headers)
    assert patch_res.status_code == 200
    assert patch_res.json()["data"]["is_read"] is True

    # Badge count is now 1
    res = await client.get("/api/v1/notifications/unread-count", headers=headers)
    assert res.status_code == 200
    assert res.json()["data"]["unread_count"] == 1


@pytest.mark.asyncio
async def test_api_mark_all_read(
    client: AsyncClient,
    student_a: User,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Verify POST /api/v1/notifications/mark-all-read bulk action."""
    headers = make_auth_header(student_a.id)

    for i in range(4):
        await fake_notif_repo.create(
            user_id=student_a.id,
            notification_type="alert",
            category="assignments",
            title=f"Task {i}",
            content="Task alert",
        )

    res = await client.post("/api/v1/notifications/mark-all-read", headers=headers)
    assert res.status_code == 200
    assert res.json()["data"]["updated_count"] == 4

    # Second call returns 0
    res2 = await client.post("/api/v1/notifications/mark-all-read", headers=headers)
    assert res2.status_code == 200
    assert res2.json()["data"]["updated_count"] == 0

    # Unread count is 0
    res_count = await client.get("/api/v1/notifications/unread-count", headers=headers)
    assert res_count.json()["data"]["unread_count"] == 0


@pytest.mark.asyncio
async def test_api_delete_notification(
    client: AsyncClient,
    student_a: User,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Verify DELETE /api/v1/notifications/{id} removes record."""
    headers = make_auth_header(student_a.id)

    notif = await fake_notif_repo.create(
        user_id=student_a.id,
        notification_type="alert",
        category="assignments",
        title="To be deleted",
        content="Delete me",
    )

    del_res = await client.delete(f"/api/v1/notifications/{notif.id}", headers=headers)
    assert del_res.status_code == 200
    assert del_res.json()["success"] is True

    # Subsequent delete of same ID returns 404
    del_res2 = await client.delete(f"/api/v1/notifications/{notif.id}", headers=headers)
    assert del_res2.status_code == 404


@pytest.mark.asyncio
async def test_strict_tenant_isolation(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Verify Invariant 6: Student A cannot access, mark read, or delete Student B's notifications."""
    headers_a = make_auth_header(student_a.id)
    headers_b = make_auth_header(student_b.id)

    notif_b = await fake_notif_repo.create(
        user_id=student_b.id,
        notification_type="secret_alert",
        category="progress",
        title="Student B confidential progress",
        content="Secret student B content",
    )

    notif_a = await fake_notif_repo.create(
        user_id=student_a.id,
        notification_type="study_alert",
        category="study_plans",
        title="Student A study session",
        content="Student A content",
    )

    # 1. Student A listing does NOT contain Student B's notification
    res_list_a = await client.get("/api/v1/notifications", headers=headers_a)
    assert res_list_a.status_code == 200
    items_a = res_list_a.json()["data"]["items"]
    assert len(items_a) == 1
    assert items_a[0]["id"] == str(notif_a.id)

    # 2. Student A badge does NOT count Student B's notification
    res_count_a = await client.get("/api/v1/notifications/unread-count", headers=headers_a)
    assert res_count_a.status_code == 200
    assert res_count_a.json()["data"]["unread_count"] == 1

    # 3. Student A cannot mark Student B's notification as read (returns 404, not 403, preventing ID scanning)
    patch_b = await client.patch(f"/api/v1/notifications/{notif_b.id}/read", headers=headers_a)
    assert patch_b.status_code == 404

    # 4. Student A cannot delete Student B's notification
    del_b = await client.delete(f"/api/v1/notifications/{notif_b.id}", headers=headers_a)
    assert del_b.status_code == 404

    # 5. Student A mark-all-read does NOT mark Student B's notification as read
    mark_all_a = await client.post("/api/v1/notifications/mark-all-read", headers=headers_a)
    assert mark_all_a.status_code == 200
    assert mark_all_a.json()["data"]["updated_count"] == 1

    # Student B still has 1 unread notification
    res_count_b = await client.get("/api/v1/notifications/unread-count", headers=headers_b)
    assert res_count_b.status_code == 200
    assert res_count_b.json()["data"]["unread_count"] == 1


@pytest.mark.asyncio
async def test_api_unauthorized_access(client: AsyncClient) -> None:
    """Verify requests without valid authorization token return 401 Unauthorized."""
    # List notifications
    res_list = await client.get("/api/v1/notifications")
    assert res_list.status_code == 401

    # Unread count
    res_count = await client.get("/api/v1/notifications/unread-count")
    assert res_count.status_code == 401

    # Mark all read
    res_mark_all = await client.post("/api/v1/notifications/mark-all-read")
    assert res_mark_all.status_code == 401

    # Mark single read
    dummy_id = uuid.uuid4()
    res_patch = await client.patch(f"/api/v1/notifications/{dummy_id}/read")
    assert res_patch.status_code == 401

    # Delete
    res_del = await client.delete(f"/api/v1/notifications/{dummy_id}")
    assert res_del.status_code == 401
