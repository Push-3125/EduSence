"""Admin user management service handling student listing, details, suspension/activation, and plan overrides (US 2.22)."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends

from app.repositories.ai_usage_repo import (
    AIUsageRepository,
    SystemAnalyticsResult,
    get_ai_usage_repo,
)
from app.repositories.audit_repo import AuditRepository, get_audit_repo
from app.repositories.payment_repo import PaymentRepository, get_payment_repo
from app.repositories.quota_repo import (
    QuotaPolicyNotFoundError,
    QuotaPolicyRepository,
    get_quota_repo,
)
from app.repositories.user_repo import User, UserRepository, get_user_repo
from app.services.auth_service import UserDTO
from app.services.quota_service import AIQuotaService, get_quota_service


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


@dataclass
class QuotaPolicyDTO:
    """Decoupled DTO representing a subscription plan quota policy."""

    id: uuid.UUID
    plan_id: uuid.UUID
    plan_code: str
    plan_name: str
    request_limit_per_day: int
    token_limit_per_day: int | None
    warning_threshold_percent: int
    updated_by: uuid.UUID | None
    updater_email: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class TopAIUserDTO:
    """Decoupled DTO representing a top AI consumer."""

    user_id: uuid.UUID
    email: str
    full_name: str
    total_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float


@dataclass
class TelemetryLogItemDTO:
    """Decoupled DTO representing an AI usage telemetry log entry."""

    id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    feature: str
    provider: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    latency_ms: int | None
    request_status: str
    error_message: str | None
    created_at: datetime


@dataclass
class PaginatedTelemetryLogsDTO:
    """Paginated collection of AI telemetry logs with pagination metadata."""

    items: list[TelemetryLogItemDTO]
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


class AdminAIService:
    """Service handling administrator AI usage analytics and quota policy management (US 2.23)."""

    def __init__(
        self,
        quota_repo: QuotaPolicyRepository,
        ai_usage_repo: AIUsageRepository,
        audit_repo: AuditRepository,
        quota_service: AIQuotaService | None = None,
    ) -> None:
        self.quota_repo = quota_repo
        self.ai_usage_repo = ai_usage_repo
        self.audit_repo = audit_repo
        self.quota_service = quota_service

    async def get_quota_policies(self) -> list[QuotaPolicyDTO]:
        """Fetch all subscription plan quota policies with joined plan and updater info."""
        policies = await self.quota_repo.get_all_policies()
        dtos: list[QuotaPolicyDTO] = []
        for p in policies:
            plan_code = p.plan.code if p.plan else "unknown"
            plan_name = p.plan.name if p.plan else plan_code.capitalize()
            updater_email = p.updater.email if p.updater else None

            dtos.append(
                QuotaPolicyDTO(
                    id=p.id,
                    plan_id=p.plan_id,
                    plan_code=plan_code,
                    plan_name=plan_name,
                    request_limit_per_day=p.request_limit_per_day,
                    token_limit_per_day=p.token_limit_per_day,
                    warning_threshold_percent=p.warning_threshold_percent,
                    updated_by=p.updated_by,
                    updater_email=updater_email,
                    created_at=p.created_at,
                    updated_at=p.updated_at,
                )
            )
        return dtos

    async def update_quota_policy(
        self,
        plan_code: str,
        request_limit_per_day: int,
        token_limit_per_day: int | None,
        warning_threshold_percent: int,
        admin_user: UserDTO | User,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> QuotaPolicyDTO:
        """Update quota policy for a subscription plan tier, log audit event, and update cache."""
        norm_code = plan_code.strip().lower()

        if request_limit_per_day < 0:
            raise AdminActionError("request_limit_per_day must be non-negative (>= 0).")
        if token_limit_per_day is not None and token_limit_per_day < 0:
            raise AdminActionError("token_limit_per_day must be non-negative (>= 0) or None.")
        if not (1 <= warning_threshold_percent <= 100):
            raise AdminActionError("warning_threshold_percent must be between 1 and 100.")

        try:
            updated_policy = await self.quota_repo.update_policy(
                plan_code=norm_code,
                request_limit_per_day=request_limit_per_day,
                token_limit_per_day=token_limit_per_day,
                warning_threshold_percent=warning_threshold_percent,
                updated_by=admin_user.id,
            )
        except QuotaPolicyNotFoundError as exc:
            raise AdminActionError(str(exc)) from exc

        # Update in-memory defaults on AIQuotaService if present
        if self.quota_service:
            if norm_code == "free":
                self.quota_service.update_limits(
                    free_limit=request_limit_per_day,
                    warning_threshold_percent=float(warning_threshold_percent),
                )
            elif norm_code == "pro":
                self.quota_service.update_limits(
                    pro_limit=request_limit_per_day,
                    warning_threshold_percent=float(warning_threshold_percent),
                )

        # Log security audit event (US 2.23 / Invariant 6)
        await self.audit_repo.log_security_event(
            event_type="quota_policy_updated",
            actor_user_id=admin_user.id,
            severity="info",
            ip_address=ip_address,
            user_agent=user_agent,
            details={
                "plan_code": norm_code,
                "request_limit_per_day": request_limit_per_day,
                "token_limit_per_day": token_limit_per_day,
                "warning_threshold_percent": warning_threshold_percent,
            },
        )

        plan_code_val = updated_policy.plan.code if updated_policy.plan else norm_code
        plan_name_val = updated_policy.plan.name if updated_policy.plan else norm_code.capitalize()
        updater_email = updated_policy.updater.email if updated_policy.updater else None

        return QuotaPolicyDTO(
            id=updated_policy.id,
            plan_id=updated_policy.plan_id,
            plan_code=plan_code_val,
            plan_name=plan_name_val,
            request_limit_per_day=updated_policy.request_limit_per_day,
            token_limit_per_day=updated_policy.token_limit_per_day,
            warning_threshold_percent=updated_policy.warning_threshold_percent,
            updated_by=updated_policy.updated_by,
            updater_email=updater_email,
            created_at=updated_policy.created_at,
            updated_at=updated_policy.updated_at,
        )

    async def get_ai_analytics(self, days: int = 30) -> SystemAnalyticsResult:
        """Fetch system-wide aggregated AI metrics and breakdowns."""
        if days < 1 or days > 365:
            raise AdminActionError("days must be between 1 and 365.")
        now = datetime.now(UTC)
        start_time = now - timedelta(days=days)
        return await self.ai_usage_repo.get_system_analytics(
            start_time=start_time,
            end_time=now,
        )

    async def get_top_ai_consumers(
        self,
        limit: int = 10,
        days: int = 30,
    ) -> list[TopAIUserDTO]:
        """Fetch top AI token consumers over the specified time window."""
        clamped_limit = max(1, min(limit, 100))
        if days < 1 or days > 365:
            raise AdminActionError("days must be between 1 and 365.")
        start_time = datetime.now(UTC) - timedelta(days=days)
        items = await self.ai_usage_repo.get_top_users(
            limit=clamped_limit,
            start_time=start_time,
        )
        return [
            TopAIUserDTO(
                user_id=item.user_id,
                email=item.email,
                full_name=item.full_name,
                total_requests=item.total_requests,
                prompt_tokens=item.prompt_tokens,
                completion_tokens=item.completion_tokens,
                total_tokens=item.total_tokens,
                estimated_cost=item.estimated_cost,
            )
            for item in items
        ]

    async def get_telemetry_logs(
        self,
        page: int = 1,
        page_size: int = 50,
        feature: str | None = None,
        request_status: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> PaginatedTelemetryLogsDTO:
        """Fetch paginated AI telemetry logs with filters."""
        clamped_page = max(1, page)
        clamped_page_size = max(1, min(page_size, 100))
        skip = (clamped_page - 1) * clamped_page_size

        logs, total_count = await self.ai_usage_repo.get_telemetry_logs(
            limit=clamped_page_size,
            skip=skip,
            feature=feature,
            request_status=request_status,
            user_id=user_id,
        )

        total_pages = math.ceil(total_count / clamped_page_size) if total_count > 0 else 0

        dtos = [
            TelemetryLogItemDTO(
                id=log.id,
                user_id=log.user_id,
                conversation_id=log.conversation_id,
                message_id=log.message_id,
                feature=log.feature,
                provider=log.provider,
                model_name=log.model_name,
                prompt_tokens=log.prompt_tokens,
                completion_tokens=log.completion_tokens,
                total_tokens=log.total_tokens,
                estimated_cost=float(log.estimated_cost),
                latency_ms=log.latency_ms,
                request_status=log.request_status,
                error_message=log.error_message,
                created_at=log.created_at,
            )
            for log in logs
        ]

        return PaginatedTelemetryLogsDTO(
            items=dtos,
            total_count=total_count,
            page=clamped_page,
            page_size=clamped_page_size,
            total_pages=total_pages,
        )


def get_admin_ai_service(
    quota_repo: Annotated[QuotaPolicyRepository, Depends(get_quota_repo)],
    ai_usage_repo: Annotated[AIUsageRepository, Depends(get_ai_usage_repo)],
    audit_repo: Annotated[AuditRepository, Depends(get_audit_repo)],
    quota_service: Annotated[AIQuotaService, Depends(get_quota_service)],
) -> AdminAIService:
    """FastAPI dependency provider for AdminAIService."""
    return AdminAIService(
        quota_repo=quota_repo,
        ai_usage_repo=ai_usage_repo,
        audit_repo=audit_repo,
        quota_service=quota_service,
    )
