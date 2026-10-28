"""Admin system storage and background job resource reports API endpoints (US 2.24 / TASK-M6-06).

Adheres strictly to:
- Invariant 2: Router stays thin; no business logic or raw SQL in router.
- Invariant 6: Strict RBAC on admin endpoints (require_admin).
- Standard envelope: {"success": true, "data": ..., "meta": ..., "message": ...}.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.v1.admin.users import require_admin
from app.services.admin_service import (
    AdminSystemService,
    get_admin_system_service,
)
from app.services.auth_service import UserDTO

router = APIRouter(prefix="/api/v1/admin/system", tags=["admin-system"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class StorageMimeTypeData(BaseModel):
    """Storage metrics grouped by MIME type."""

    mime_type: str
    count: int
    total_bytes: int


class StorageStatusData(BaseModel):
    """Storage document counts grouped by processing status."""

    status: str
    count: int


class SystemStorageData(BaseModel):
    """System-wide document storage and vector chunk representation."""

    total_documents: int
    total_bytes: int
    average_bytes: float
    total_chunks: int
    embedded_chunks: int
    by_mime_type: list[StorageMimeTypeData]
    by_status: list[StorageStatusData]


class TopStorageUserData(BaseModel):
    """Top student storage consumer representation."""

    user_id: uuid.UUID
    email: str
    full_name: str
    document_count: int
    total_bytes: int


class JobTaskTypeData(BaseModel):
    """Background job telemetry grouped by task type."""

    task_type: str
    count: int


class JobStatusData(BaseModel):
    """Background job telemetry grouped by status."""

    status: str
    count: int


class SystemJobsData(BaseModel):
    """Background Celery job execution telemetry representation."""

    days: int
    total_jobs: int
    completed_jobs: int
    failed_jobs: int
    running_jobs: int
    queued_jobs: int
    cancelled_jobs: int
    failure_rate: float
    worker_status: str
    by_task_type: list[JobTaskTypeData]
    by_status: list[JobStatusData]


class SystemOverviewData(BaseModel):
    """Unified system dashboard metrics representation."""

    total_users: int
    active_users: int
    total_documents: int
    total_bytes: int
    total_chunks: int
    embedded_chunks: int
    total_ai_requests: int
    total_jobs: int


# ---------------------------------------------------------------------------
# Standard Envelopes
# ---------------------------------------------------------------------------


class SystemOverviewResponse(BaseModel):
    """Standard envelope for system overview dashboard."""

    success: bool = True
    data: SystemOverviewData
    meta: dict[str, Any] = Field(default_factory=dict)
    message: str = "System overview retrieved successfully."


class SystemStorageResponse(BaseModel):
    """Standard envelope for document and vector storage telemetry."""

    success: bool = True
    data: SystemStorageData
    meta: dict[str, Any] = Field(default_factory=dict)
    message: str = "System storage report retrieved successfully."


class TopStorageUsersResponse(BaseModel):
    """Standard envelope for top students by storage volume."""

    success: bool = True
    data: list[TopStorageUserData]
    meta: dict[str, Any] = Field(default_factory=dict)
    message: str = "Top storage users retrieved successfully."


class SystemJobsResponse(BaseModel):
    """Standard envelope for background Celery jobs telemetry."""

    success: bool = True
    data: SystemJobsData
    meta: dict[str, Any] = Field(default_factory=dict)
    message: str = "System jobs telemetry report retrieved successfully."


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/overview",
    response_model=SystemOverviewResponse,
    summary="Get unified system dashboard overview",
)
async def get_system_overview(
    admin: Annotated[UserDTO, Depends(require_admin)],
    service: Annotated[AdminSystemService, Depends(get_admin_system_service)],
) -> SystemOverviewResponse:
    """Return unified high-level system metrics."""
    overview = await service.get_system_overview()
    return SystemOverviewResponse(
        data=SystemOverviewData(
            total_users=overview.total_users,
            active_users=overview.active_users,
            total_documents=overview.total_documents,
            total_bytes=overview.total_bytes,
            total_chunks=overview.total_chunks,
            embedded_chunks=overview.embedded_chunks,
            total_ai_requests=overview.total_ai_requests,
            total_jobs=overview.total_jobs,
        ),
        meta={"generated_at": datetime.now(UTC).isoformat()},
    )


@router.get(
    "/storage",
    response_model=SystemStorageResponse,
    summary="Get document storage & vector database metrics",
)
async def get_system_storage(
    admin: Annotated[UserDTO, Depends(require_admin)],
    service: Annotated[AdminSystemService, Depends(get_admin_system_service)],
) -> SystemStorageResponse:
    """Return detailed system storage and vector chunk breakdown."""
    report = await service.get_storage_report()
    return SystemStorageResponse(
        data=SystemStorageData(
            total_documents=report.total_documents,
            total_bytes=report.total_bytes,
            average_bytes=report.average_bytes,
            total_chunks=report.total_chunks,
            embedded_chunks=report.embedded_chunks,
            by_mime_type=[
                StorageMimeTypeData(
                    mime_type=m.mime_type,
                    count=m.count,
                    total_bytes=m.total_bytes,
                )
                for m in report.by_mime_type
            ],
            by_status=[
                StorageStatusData(
                    status=s.status,
                    count=s.count,
                )
                for s in report.by_status
            ],
        ),
        meta={"generated_at": datetime.now(UTC).isoformat()},
    )


@router.get(
    "/storage/top-users",
    response_model=TopStorageUsersResponse,
    summary="Get top students by storage consumption",
)
async def get_top_storage_users(
    admin: Annotated[UserDTO, Depends(require_admin)],
    service: Annotated[AdminSystemService, Depends(get_admin_system_service)],
    limit: int = Query(
        10,
        ge=1,
        le=100,
        description="Maximum number of students to return (1..100)",
    ),
) -> TopStorageUsersResponse:
    """Return ranked list of students with the largest stored file volume."""
    users = await service.get_top_storage_users(limit=limit)
    return TopStorageUsersResponse(
        data=[
            TopStorageUserData(
                user_id=u.user_id,
                email=u.email,
                full_name=u.full_name,
                document_count=u.document_count,
                total_bytes=u.total_bytes,
            )
            for u in users
        ],
        meta={
            "total_returned": len(users),
            "limit": limit,
            "generated_at": datetime.now(UTC).isoformat(),
        },
    )


@router.get(
    "/jobs",
    response_model=SystemJobsResponse,
    summary="Get background Celery job execution telemetry",
)
async def get_system_jobs(
    admin: Annotated[UserDTO, Depends(require_admin)],
    service: Annotated[AdminSystemService, Depends(get_admin_system_service)],
    days: int = Query(
        30,
        ge=1,
        le=365,
        description="Lookback window in days (1..365)",
    ),
) -> SystemJobsResponse:
    """Return background Celery job execution telemetry and failure rates."""
    jobs_report = await service.get_jobs_report(days=days)
    return SystemJobsResponse(
        data=SystemJobsData(
            days=jobs_report.days,
            total_jobs=jobs_report.total_jobs,
            completed_jobs=jobs_report.completed_jobs,
            failed_jobs=jobs_report.failed_jobs,
            running_jobs=jobs_report.running_jobs,
            queued_jobs=jobs_report.queued_jobs,
            cancelled_jobs=jobs_report.cancelled_jobs,
            failure_rate=jobs_report.failure_rate,
            worker_status=jobs_report.worker_status,
            by_task_type=[
                JobTaskTypeData(task_type=t.task_type, count=t.count)
                for t in jobs_report.by_task_type
            ],
            by_status=[
                JobStatusData(status=s.status, count=s.count)
                for s in jobs_report.by_status
            ],
        ),
        meta={
            "days": days,
            "generated_at": datetime.now(UTC).isoformat(),
        },
    )
