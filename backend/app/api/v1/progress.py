"""Academic Progress & Analytics API router (TASK-M5-07 / US 2.17, 2.18).

Adheres strictly to:
- Invariant 2: Router stays thin; delegates all logic to ProgressService. Never imports repositories.
- Invariant 6: Strict tenant isolation (all operations scoped to authenticated user_id).
- Standard Envelope: { success: true, data: ..., message: ... } for all successful responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.progress_service import (
    CourseNotFoundError,
    CourseProgressDTO,
    DashboardProgressDTO,
    ProgressService,
    get_progress_service,
)

router = APIRouter(prefix="/api/v1/progress", tags=["progress"])


# ---------------------------------------------------------------------------
# Response Schemas
# ---------------------------------------------------------------------------


class CourseProgressResponseData(BaseModel):
    """Payload schema for an individual course progress report."""

    course_id: uuid.UUID = Field(..., description="Course UUID")
    course_name: str = Field(..., description="Course name")
    course_code: str = Field(..., description="Course code or empty string")
    assignments_total: int = Field(..., description="Total assignments count")
    assignments_completed: int = Field(..., description="Completed assignments count")
    study_items_total: int = Field(..., description="Total study plan items count")
    study_items_completed: int = Field(..., description="Completed study plan items count")
    progress_percent: float = Field(..., description="Completion percentage between 0 and 100")
    risk_status: str = Field(..., description="Risk category: on_track, at_risk, overdue")
    last_calculated_at: datetime = Field(..., description="Timestamp of calculation")


class DashboardProgressResponseData(BaseModel):
    """Payload schema for aggregated academic progress dashboard."""

    total_courses: int = Field(..., description="Total active courses count")
    overall_progress_percent: float = Field(..., description="Average completion percentage")
    on_track_count: int = Field(..., description="Count of courses on track")
    at_risk_count: int = Field(..., description="Count of courses at risk")
    overdue_count: int = Field(..., description="Count of courses with overdue items")
    courses: list[CourseProgressResponseData] = Field(..., description="List of course progress details")
    upcoming_urgent_deadlines: int = Field(..., description="Count of urgent pending items within 7 days")


class CourseProgressResponse(BaseModel):
    """Standard envelope response for a single course progress."""

    success: bool = True
    data: CourseProgressResponseData
    message: str = "Course progress retrieved successfully."


class CourseProgressListResponse(BaseModel):
    """Standard envelope response for listing course progress items."""

    success: bool = True
    data: list[CourseProgressResponseData]
    message: str = "Courses progress retrieved successfully."


class DashboardProgressResponse(BaseModel):
    """Standard envelope response for aggregated dashboard analytics."""

    success: bool = True
    data: DashboardProgressResponseData
    message: str = "Academic progress dashboard retrieved successfully."


# ---------------------------------------------------------------------------
# Helper serialization functions
# ---------------------------------------------------------------------------


def _to_course_progress_data(dto: CourseProgressDTO) -> CourseProgressResponseData:
    return CourseProgressResponseData(
        course_id=dto.course_id,
        course_name=dto.course_name,
        course_code=dto.course_code,
        assignments_total=dto.assignments_total,
        assignments_completed=dto.assignments_completed,
        study_items_total=dto.study_items_total,
        study_items_completed=dto.study_items_completed,
        progress_percent=dto.progress_percent,
        risk_status=dto.risk_status,
        last_calculated_at=dto.last_calculated_at,
    )


def _to_dashboard_data(dto: DashboardProgressDTO) -> DashboardProgressResponseData:
    return DashboardProgressResponseData(
        total_courses=dto.total_courses,
        overall_progress_percent=dto.overall_progress_percent,
        on_track_count=dto.on_track_count,
        at_risk_count=dto.at_risk_count,
        overdue_count=dto.overdue_count,
        courses=[_to_course_progress_data(c) for c in dto.courses],
        upcoming_urgent_deadlines=dto.upcoming_urgent_deadlines,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/dashboard",
    response_model=DashboardProgressResponse,
    status_code=status.HTTP_200_OK,
)
async def get_dashboard(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    progress_service: Annotated[ProgressService, Depends(get_progress_service)],
) -> DashboardProgressResponse:
    """Retrieve the full aggregated academic progress dashboard for the authenticated student."""
    dto = await progress_service.get_dashboard(user_id=current_user.id)
    return DashboardProgressResponse(
        success=True,
        data=_to_dashboard_data(dto),
        message="Academic progress dashboard retrieved successfully.",
    )


@router.get(
    "/courses",
    response_model=CourseProgressListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_course_progress(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    progress_service: Annotated[ProgressService, Depends(get_progress_service)],
) -> CourseProgressListResponse:
    """List calculated academic progress for all courses owned by the authenticated student."""
    items = await progress_service.list_course_progress(user_id=current_user.id)
    return CourseProgressListResponse(
        success=True,
        data=[_to_course_progress_data(i) for i in items],
        message="Courses progress retrieved successfully.",
    )


@router.get(
    "/courses/{course_id}",
    response_model=CourseProgressResponse,
    status_code=status.HTTP_200_OK,
)
async def get_course_progress(
    course_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    progress_service: Annotated[ProgressService, Depends(get_progress_service)],
) -> CourseProgressResponse:
    """Retrieve progress metrics for a specific course verifying ownership."""
    try:
        dto = await progress_service.get_course_progress(
            user_id=current_user.id, course_id=course_id
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc

    return CourseProgressResponse(
        success=True,
        data=_to_course_progress_data(dto),
        message="Course progress retrieved successfully.",
    )


@router.post(
    "/recalculate",
    response_model=CourseProgressListResponse,
    status_code=status.HTTP_200_OK,
)
async def recalculate_progress(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    progress_service: Annotated[ProgressService, Depends(get_progress_service)],
) -> CourseProgressListResponse:
    """Trigger recalculation and persist progress for all active courses."""
    items = await progress_service.recalculate_all_progress(user_id=current_user.id)
    return CourseProgressListResponse(
        success=True,
        data=[_to_course_progress_data(i) for i in items],
        message="Academic progress recalculated successfully.",
    )
