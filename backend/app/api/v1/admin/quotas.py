"""Admin AI usage analytics and quota policy configuration API endpoints (US 2.23 / TASK-M6-05).

Adheres strictly to:
- Invariant 2: Router stays thin; no business logic or raw SQL in router.
- Invariant 6: Strict RBAC on admin endpoints (require_admin).
- Standard envelope: {"success": true, "data": ..., "meta": ..., "message": ...}.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.v1.admin.users import require_admin
from app.services.admin_service import (
    AdminActionError,
    AdminAIService,
    QuotaPolicyDTO,
    get_admin_ai_service,
)
from app.services.auth_service import UserDTO

router = APIRouter(prefix="/api/v1/admin/ai", tags=["admin-ai-quotas"])
alias_router = APIRouter(prefix="/api/v1/admin/quotas", tags=["admin-quotas"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class QuotaPolicyData(BaseModel):
    """Quota policy response representation."""

    id: uuid.UUID
    plan_id: uuid.UUID
    plan_code: str
    plan_name: str
    request_limit_per_day: int
    token_limit_per_day: int | None = None
    warning_threshold_percent: int
    updated_by: uuid.UUID | None = None
    updater_email: str | None = None
    created_at: datetime
    updated_at: datetime


class UpdateQuotaPolicyRequest(BaseModel):
    """Payload schema for modifying plan-level daily AI quotas."""

    request_limit_per_day: int = Field(
        ...,
        ge=0,
        description="Daily request limit ceiling for users on this plan (must be >= 0)",
    )
    token_limit_per_day: int | None = Field(
        default=None,
        ge=0,
        description="Optional daily token limit ceiling for users on this plan (must be >= 0 or None)",
    )
    warning_threshold_percent: int = Field(
        default=80,
        ge=1,
        le=100,
        description="Warning threshold percentage triggering approaching-limit flag (1..100)",
    )


class QuotaPolicyListResponse(BaseModel):
    """Standard envelope for list of quota policies."""

    success: bool = True
    data: list[QuotaPolicyData]
    message: str = "Quota policies retrieved successfully."


class QuotaPolicySingleResponse(BaseModel):
    """Standard envelope for single updated quota policy."""

    success: bool = True
    data: QuotaPolicyData
    message: str = "Quota policy updated successfully."


class FeatureAnalyticsData(BaseModel):
    """Aggregated usage metrics by AI feature."""

    feature: str
    total_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    average_latency_ms: float


class ModelAnalyticsData(BaseModel):
    """Aggregated usage metrics by AI model."""

    model_name: str
    total_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float


class StatusAnalyticsData(BaseModel):
    """Aggregated request counts by outcome status."""

    request_status: str
    total_requests: int


class AIAnalyticsData(BaseModel):
    """Aggregated AI system analytics and breakdowns."""

    total_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    average_latency_ms: float
    by_feature: list[FeatureAnalyticsData]
    by_model: list[ModelAnalyticsData]
    by_status: list[StatusAnalyticsData]


class AIAnalyticsResponse(BaseModel):
    """Standard envelope for AI analytics metrics."""

    success: bool = True
    data: AIAnalyticsData
    meta: dict[str, Any] | None = None
    message: str = "AI usage analytics retrieved successfully."


class TopUserItemData(BaseModel):
    """Top AI consuming user item."""

    user_id: uuid.UUID
    email: str
    full_name: str
    total_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float


class TopUsersResponse(BaseModel):
    """Standard envelope for top AI consumers."""

    success: bool = True
    data: list[TopUserItemData]
    meta: dict[str, Any] | None = None
    message: str = "Top AI consumers retrieved successfully."


class AITelemetryLogData(BaseModel):
    """Raw AI telemetry log record."""

    id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    feature: str
    provider: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    latency_ms: int | None = None
    request_status: str
    error_message: str | None = None
    created_at: datetime


class PaginationMeta(BaseModel):
    """Pagination metadata block."""

    page: int
    page_size: int
    total_count: int
    total_pages: int


class AITelemetryLogsResponse(BaseModel):
    """Standard envelope for paginated telemetry logs."""

    success: bool = True
    data: list[AITelemetryLogData]
    meta: PaginationMeta
    message: str = "AI telemetry logs retrieved successfully."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dto_to_quota_policy_data(dto: QuotaPolicyDTO) -> QuotaPolicyData:
    return QuotaPolicyData(
        id=dto.id,
        plan_id=dto.plan_id,
        plan_code=dto.plan_code,
        plan_name=dto.plan_name,
        request_limit_per_day=dto.request_limit_per_day,
        token_limit_per_day=dto.token_limit_per_day,
        warning_threshold_percent=dto.warning_threshold_percent,
        updated_by=dto.updated_by,
        updater_email=dto.updater_email,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints (/api/v1/admin/ai/...)
# ---------------------------------------------------------------------------


@router.get(
    "/quotas",
    response_model=QuotaPolicyListResponse,
    status_code=status.HTTP_200_OK,
    summary="List plan quota policies",
)
async def list_quota_policies(
    _: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminAIService, Depends(get_admin_ai_service)],
) -> QuotaPolicyListResponse:
    """Retrieve all plan-level AI quota limits with updater details."""
    dtos = await admin_service.get_quota_policies()
    data = [_dto_to_quota_policy_data(dto) for dto in dtos]
    return QuotaPolicyListResponse(
        success=True,
        data=data,
        message="Quota policies retrieved successfully.",
    )


@router.patch(
    "/quotas/{plan_code}",
    response_model=QuotaPolicySingleResponse,
    status_code=status.HTTP_200_OK,
    summary="Update daily limits and warning threshold for a plan",
)
async def update_quota_policy(
    plan_code: str,
    payload: UpdateQuotaPolicyRequest,
    admin: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminAIService, Depends(get_admin_ai_service)],
    request: Request,
) -> QuotaPolicySingleResponse:
    """Update daily AI request/token quotas and threshold for a plan code with audit logging."""
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    try:
        dto = await admin_service.update_quota_policy(
            plan_code=plan_code,
            request_limit_per_day=payload.request_limit_per_day,
            token_limit_per_day=payload.token_limit_per_day,
            warning_threshold_percent=payload.warning_threshold_percent,
            admin_user=admin,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except AdminActionError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return QuotaPolicySingleResponse(
        success=True,
        data=_dto_to_quota_policy_data(dto),
        message=f"Quota policy for '{plan_code}' updated successfully.",
    )


@router.get(
    "/analytics",
    response_model=AIAnalyticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Aggregated system-wide AI usage analytics",
)
async def get_ai_analytics(
    _: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminAIService, Depends(get_admin_ai_service)],
    days: Annotated[
        int, Query(ge=1, le=365, description="Lookback window in days")
    ] = 30,
) -> AIAnalyticsResponse:
    """Retrieve overall AI usage metrics and breakdowns by feature, model, and status."""
    try:
        analytics = await admin_service.get_ai_analytics(days=days)
    except AdminActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    data = AIAnalyticsData(
        total_requests=analytics.total_requests,
        prompt_tokens=analytics.prompt_tokens,
        completion_tokens=analytics.completion_tokens,
        total_tokens=analytics.total_tokens,
        estimated_cost=analytics.estimated_cost,
        average_latency_ms=analytics.average_latency_ms,
        by_feature=[
            FeatureAnalyticsData(
                feature=f.feature,
                total_requests=f.total_requests,
                prompt_tokens=f.prompt_tokens,
                completion_tokens=f.completion_tokens,
                total_tokens=f.total_tokens,
                estimated_cost=f.estimated_cost,
                average_latency_ms=f.average_latency_ms,
            )
            for f in analytics.by_feature
        ],
        by_model=[
            ModelAnalyticsData(
                model_name=m.model_name,
                total_requests=m.total_requests,
                prompt_tokens=m.prompt_tokens,
                completion_tokens=m.completion_tokens,
                total_tokens=m.total_tokens,
                estimated_cost=m.estimated_cost,
            )
            for m in analytics.by_model
        ],
        by_status=[
            StatusAnalyticsData(
                request_status=s.request_status,
                total_requests=s.total_requests,
            )
            for s in analytics.by_status
        ],
    )

    return AIAnalyticsResponse(
        success=True,
        data=data,
        meta={"days": days},
        message="AI usage analytics retrieved successfully.",
    )


@router.get(
    "/top-users",
    response_model=TopUsersResponse,
    status_code=status.HTTP_200_OK,
    summary="Top AI consuming users",
)
async def get_top_ai_users(
    _: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminAIService, Depends(get_admin_ai_service)],
    limit: Annotated[
        int, Query(ge=1, le=100, description="Max users to return")
    ] = 10,
    days: Annotated[
        int, Query(ge=1, le=365, description="Lookback window in days")
    ] = 30,
) -> TopUsersResponse:
    """Retrieve students consuming the most AI tokens within a sliding time window."""
    try:
        consumers = await admin_service.get_top_ai_consumers(limit=limit, days=days)
    except AdminActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    data = [
        TopUserItemData(
            user_id=c.user_id,
            email=c.email,
            full_name=c.full_name,
            total_requests=c.total_requests,
            prompt_tokens=c.prompt_tokens,
            completion_tokens=c.completion_tokens,
            total_tokens=c.total_tokens,
            estimated_cost=c.estimated_cost,
        )
        for c in consumers
    ]

    return TopUsersResponse(
        success=True,
        data=data,
        meta={"limit": limit, "days": days},
        message="Top AI consumers retrieved successfully.",
    )


@router.get(
    "/logs",
    response_model=AITelemetryLogsResponse,
    status_code=status.HTTP_200_OK,
    summary="Paginated AI telemetry logs",
)
async def get_ai_telemetry_logs(
    _: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminAIService, Depends(get_admin_ai_service)],
    page: Annotated[int, Query(ge=1, description="Page number (1-indexed)")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=100, description="Items per page")
    ] = 50,
    feature: Annotated[
        str | None, Query(description="Filter by feature name")
    ] = None,
    status_val: Annotated[
        str | None, Query(alias="status", description="Filter by status")
    ] = None,
    request_status: Annotated[
        str | None, Query(description="Filter by request status")
    ] = None,
    user_id: Annotated[
        uuid.UUID | None, Query(description="Filter by student user UUID")
    ] = None,
) -> AITelemetryLogsResponse:
    """Retrieve raw AI telemetry and audit logs with pagination and filters."""
    resolved_status = request_status or status_val
    result = await admin_service.get_telemetry_logs(
        page=page,
        page_size=page_size,
        feature=feature,
        request_status=resolved_status,
        user_id=user_id,
    )

    data = [
        AITelemetryLogData(
            id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            message_id=item.message_id,
            feature=item.feature,
            provider=item.provider,
            model_name=item.model_name,
            prompt_tokens=item.prompt_tokens,
            completion_tokens=item.completion_tokens,
            total_tokens=item.total_tokens,
            estimated_cost=item.estimated_cost,
            latency_ms=item.latency_ms,
            request_status=item.request_status,
            error_message=item.error_message,
            created_at=item.created_at,
        )
        for item in result.items
    ]

    return AITelemetryLogsResponse(
        success=True,
        data=data,
        meta=PaginationMeta(
            page=result.page,
            page_size=result.page_size,
            total_count=result.total_count,
            total_pages=result.total_pages,
        ),
        message="AI telemetry logs retrieved successfully.",
    )


# ---------------------------------------------------------------------------
# Alias routes (/api/v1/admin/quotas)
# ---------------------------------------------------------------------------


@alias_router.get(
    "",
    response_model=QuotaPolicyListResponse,
    status_code=status.HTTP_200_OK,
    summary="List plan quota policies (alias)",
)
async def list_quota_policies_alias(
    admin: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminAIService, Depends(get_admin_ai_service)],
) -> QuotaPolicyListResponse:
    """Alias for /api/v1/admin/ai/quotas."""
    return await list_quota_policies(admin, admin_service)


@alias_router.patch(
    "/{plan_code}",
    response_model=QuotaPolicySingleResponse,
    status_code=status.HTTP_200_OK,
    summary="Update plan quota policy (alias)",
)
async def update_quota_policy_alias(
    plan_code: str,
    payload: UpdateQuotaPolicyRequest,
    admin: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminAIService, Depends(get_admin_ai_service)],
    request: Request,
) -> QuotaPolicySingleResponse:
    """Alias for /api/v1/admin/ai/quotas/{plan_code}."""
    return await update_quota_policy(
        plan_code=plan_code,
        payload=payload,
        admin=admin,
        admin_service=admin_service,
        request=request,
    )


__all__ = [
    "AIAnalyticsData",
    "AIAnalyticsResponse",
    "AITelemetryLogData",
    "AITelemetryLogsResponse",
    "FeatureAnalyticsData",
    "ModelAnalyticsData",
    "PaginationMeta",
    "QuotaPolicyData",
    "QuotaPolicyListResponse",
    "QuotaPolicySingleResponse",
    "StatusAnalyticsData",
    "TopUserItemData",
    "TopUsersResponse",
    "UpdateQuotaPolicyRequest",
    "alias_router",
    "router",
]
