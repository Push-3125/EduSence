"""Admin user management service handling student listing, details, suspension/activation, and plan overrides (US 2.22)."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends

from app.repositories.audit_repo import AuditRepository, get_audit_repo
from app.repositories.payment_repo import PaymentRepository, get_payment_repo
from app.repositories.user_repo import User, UserRepository, get_user_repo
from app.services.auth_service import UserDTO


class AdminServiceError(Exception):
    """Base exception for admin user management service operations."""


class AdminUserNotFoundError(AdminServiceError):
    """Raised when a student account is not found or does not have student role."""


class AdminActionError(AdminServiceError):
    """Raised when an admin action violates business rules (e.g. self-suspension)."""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class StudentProfileDTO:
    """Decoupled DTO representing a student profile."""

    id: uuid.UUID | None
    user_id: uuid.UUID
    full_name: str
    display_name: str | None = None
    avatar_url: str | None = None
    phone_number: str | None = None
    university: str | None = None
    major: str | None = None
    academic_year: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class StudentSubscriptionDTO:
    """Decoupled DTO representing a student's active subscription information."""

    id: uuid.UUID | None
    plan_code: str
    plan_name: str
    status: str
    starts_at: datetime | None
    ends_at: datetime | None
    is_active: bool
    days_remaining: int = 0


@dataclass
class StudentListItemDTO:
    """Decoupled DTO for student summary in paginated lists."""

    id: uuid.UUID
    email: str
    full_name: str
    display_name: str | None
    avatar_url: str | None
    is_active: bool
    is_email_verified: bool
    current_plan_code: str
    roles: list[str]
    university: str | None
    major: str | None
    created_at: datetime
    last_login_at: datetime | None


@dataclass
class StudentDetailDTO:
    """Decoupled DTO containing full student account details, profile, and subscription."""

    id: uuid.UUID
    email: str
    full_name: str
    display_name: str | None
    avatar_url: str | None
    is_active: bool
    is_email_verified: bool
    current_plan_code: str
    roles: list[str]
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
    profile: StudentProfileDTO | None
    subscription: StudentSubscriptionDTO | None


@dataclass
class PaginatedStudentsDTO:
    """Paginated collection of student accounts with metadata."""

    items: list[StudentListItemDTO]
    total_count: int
    page: int
    page_size: int
    total_pages: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AdminUserService:
    """Service handling administrator operations on student accounts."""

    def __init__(
        self,
        user_repo: UserRepository,
        audit_repo: AuditRepository,
        payment_repo: PaymentRepository,
    ) -> None:
        self.user_repo = user_repo
        self.audit_repo = audit_repo
        self.payment_repo = payment_repo

    async def list_students(
        self,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
        is_active: bool | None = None,
        plan_code: str | None = None,
    ) -> PaginatedStudentsDTO:
        """Fetch a paginated list of students with optional search and filters."""
        users, total_count = await self.user_repo.list_users(
            page=page,
            page_size=page_size,
            search=search,
            is_active=is_active,
            plan_code=plan_code,
            role_name="student",
        )

        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0

        items: list[StudentListItemDTO] = []
        for user in users:
            full_name = user.profile.full_name if user.profile else user.email
            display_name = user.profile.display_name if user.profile else None
            avatar_url = user.profile.avatar_url if user.profile else None
            university = user.profile.university if user.profile else None
            major = user.profile.major if user.profile else None
            roles = [r.name for r in (user.roles or [])]

            items.append(
                StudentListItemDTO(
                    id=user.id,
                    email=user.email,
                    full_name=full_name,
                    display_name=display_name,
                    avatar_url=avatar_url,
                    is_active=user.is_active,
                    is_email_verified=user.is_email_verified,
                    current_plan_code=user.current_plan_code,
                    roles=roles,
                    university=university,
                    major=major,
                    created_at=user.created_at,
                    last_login_at=user.last_login_at,
                )
            )

        return PaginatedStudentsDTO(
            items=items,
            total_count=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    async def get_student_detail(self, student_id: uuid.UUID) -> StudentDetailDTO:
        """Fetch comprehensive details for a specific student account.

        Raises:
            AdminUserNotFoundError: If student is not found or not assigned the student role.
        """
        user = await self.user_repo.get_by_id(student_id)
        if not user:
            raise AdminUserNotFoundError(f"Student with ID '{student_id}' not found.")

        roles = [r.name.lower() for r in (user.roles or [])]
        if "student" not in roles:
            raise AdminUserNotFoundError(f"User with ID '{student_id}' is not a student.")

        profile_dto: StudentProfileDTO | None = None
        if user.profile:
            profile_dto = StudentProfileDTO(
                id=user.profile.id,
                user_id=user.profile.user_id,
                full_name=user.profile.full_name,
                display_name=user.profile.display_name,
                avatar_url=user.profile.avatar_url,
                phone_number=user.profile.phone_number,
                university=user.profile.university,
                major=user.profile.major,
                academic_year=user.profile.academic_year,
                created_at=user.profile.created_at,
                updated_at=user.profile.updated_at,
            )

        sub_dto = await self._build_subscription_dto(user)

        full_name = user.profile.full_name if user.profile else user.email
        display_name = user.profile.display_name if user.profile else None
        avatar_url = user.profile.avatar_url if user.profile else None

        return StudentDetailDTO(
            id=user.id,
            email=user.email,
            full_name=full_name,
            display_name=display_name,
            avatar_url=avatar_url,
            is_active=user.is_active,
            is_email_verified=user.is_email_verified,
            current_plan_code=user.current_plan_code,
            roles=[r.name for r in (user.roles or [])],
            created_at=user.created_at,
            updated_at=user.updated_at,
            last_login_at=user.last_login_at,
            profile=profile_dto,
            subscription=sub_dto,
        )

    async def update_student_status(
        self,
        admin_user: UserDTO,
        student_id: uuid.UUID,
        is_active: bool,
        reason: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> StudentDetailDTO:
        """Activate or suspend a student account with session revocation and audit logging.

        Raises:
            AdminActionError: If an admin attempts to suspend their own account.
            AdminUserNotFoundError: If student is not found or not a student.
        """
        # Self-suspension guard
        if student_id == admin_user.id:
            raise AdminActionError("Administrators cannot suspend their own account.")

        user = await self.user_repo.get_by_id(student_id)
        if not user:
            raise AdminUserNotFoundError(f"Student with ID '{student_id}' not found.")

        roles = [r.name.lower() for r in (user.roles or [])]
        if "student" not in roles:
            raise AdminUserNotFoundError(f"User with ID '{student_id}' is not a student.")

        # Update active status
        await self.user_repo.update_user_status(student_id, is_active)

        if not is_active:
            # Revoke all active sessions on suspension
            await self.user_repo.revoke_all_user_sessions(student_id)
            event_type = "account_suspended"
            severity = "warning"
        else:
            event_type = "account_activated"
            severity = "info"

        # Log security audit trail
        await self.audit_repo.log_security_event(
            event_type=event_type,
            actor_user_id=admin_user.id,
            target_user_id=student_id,
            severity=severity,
            ip_address=ip_address,
            user_agent=user_agent,
            details={
                "action": "suspend" if not is_active else "activate",
                "is_active": is_active,
                "reason": reason,
            },
        )

        return await self.get_student_detail(student_id)

    async def override_student_plan(
        self,
        admin_user: UserDTO,
        student_id: uuid.UUID,
        plan_code: str,
        duration_days: int | None = None,
        reason: str = "",
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> StudentDetailDTO:
        """Manually override a student's subscription plan with audit logging.

        Raises:
            AdminActionError: If plan does not exist.
            AdminUserNotFoundError: If student is not found or not a student.
        """
        norm_plan_code = plan_code.strip().lower()
        plan = await self.payment_repo.get_plan_by_code(norm_plan_code)
        if not plan:
            raise AdminActionError(f"Subscription plan '{plan_code}' does not exist.")

        user = await self.user_repo.get_by_id(student_id)
        if not user:
            raise AdminUserNotFoundError(f"Student with ID '{student_id}' not found.")

        roles = [r.name.lower() for r in (user.roles or [])]
        if "student" not in roles:
            raise AdminUserNotFoundError(f"User with ID '{student_id}' is not a student.")

        # Override subscription via payment repository
        await self.payment_repo.override_user_subscription(
            user_id=student_id,
            plan=plan,
            duration_days=duration_days,
        )

        # Log audit trail
        await self.audit_repo.log_security_event(
            event_type="plan_override",
            actor_user_id=admin_user.id,
            target_user_id=student_id,
            severity="info",
            ip_address=ip_address,
            user_agent=user_agent,
            details={
                "plan_code": norm_plan_code,
                "duration_days": duration_days,
                "reason": reason,
            },
        )

        return await self.get_student_detail(student_id)

    async def _build_subscription_dto(self, user: User) -> StudentSubscriptionDTO:
        """Internal helper to construct StudentSubscriptionDTO for a user."""
        active_sub = await self.payment_repo.get_active_subscription(user_id=user.id)
        now = datetime.now(UTC)

        if active_sub:
            ends_at = active_sub.ends_at
            if ends_at is not None and ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=UTC)

            is_active = active_sub.status == "active" and (ends_at is None or ends_at > now)
            days_remaining = 0
            if is_active and ends_at:
                diff_seconds = (ends_at - now).total_seconds()
                days_remaining = max(1, math.ceil(diff_seconds / 86400.0))

            plan_code = active_sub.plan.code if active_sub.plan else user.current_plan_code
            plan_name = active_sub.plan.name if active_sub.plan else plan_code.capitalize()

            return StudentSubscriptionDTO(
                id=active_sub.id,
                plan_code=plan_code,
                plan_name=plan_name,
                status=active_sub.status,
                starts_at=active_sub.starts_at,
                ends_at=active_sub.ends_at,
                is_active=is_active,
                days_remaining=days_remaining,
            )

        # Free tier fallback
        free_plan = await self.payment_repo.get_plan_by_code(user.current_plan_code)
        plan_name = free_plan.name if free_plan else user.current_plan_code.capitalize()

        return StudentSubscriptionDTO(
            id=None,
            plan_code=user.current_plan_code,
            plan_name=plan_name,
            status="active" if user.current_plan_code == "free" else "inactive",
            starts_at=None,
            ends_at=None,
            is_active=(user.current_plan_code == "free"),
            days_remaining=0,
        )


def get_admin_user_service(
    user_repo: Annotated[UserRepository, Depends(get_user_repo)],
    audit_repo: Annotated[AuditRepository, Depends(get_audit_repo)],
    payment_repo: Annotated[PaymentRepository, Depends(get_payment_repo)],
) -> AdminUserService:
    """FastAPI dependency provider for AdminUserService."""
    return AdminUserService(
        user_repo=user_repo,
        audit_repo=audit_repo,
        payment_repo=payment_repo,
    )
