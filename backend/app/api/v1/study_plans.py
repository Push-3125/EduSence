"""Study Plans API router for student revision and study plans (TASK-M5-05 / US 2.16).

Adheres strictly to:
- Invariant 2: Router stays thin; delegates all logic to StudyPlanService. Never imports repositories.
- Invariant 6: Strict tenant isolation (all operations scoped to authenticated user_id).
- Invariant 7: Handles QuotaExceededError (429) and QuotaServiceUnavailableError (503 fail-closed).
- Standard Envelope: { success: true, data: ..., message: ... } for all successful responses.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import get_current_active_user
from app.providers.gemini_adapter import (
    GeminiCircuitOpenError,
    GeminiRateLimitError,
    GeminiTimeoutError,
)
from app.services.auth_service import UserDTO
from app.services.study_plan_service import (
    InvalidPlanDateError,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    StudyPlanDTO,
    StudyPlanItemDTO,
    StudyPlanNotFoundError,
    StudyPlanService,
    get_study_plan_service,
)

router = APIRouter(prefix="/api/v1", tags=["study-plans"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------


class GenerateStudyPlanRequest(BaseModel):
    """Request payload for AI-driven study plan generation."""

    name: str | None = Field(
        default=None, max_length=255, description="Optional custom study plan name"
    )
    start_date: date = Field(..., description="Plan start date (YYYY-MM-DD)")
    end_date: date = Field(..., description="Plan end date (YYYY-MM-DD)")
    target_goals: str | None = Field(
        default=None, description="Optional student revision goals and focus areas"
    )


class CreateStudyPlanRequest(BaseModel):
    """Request payload for manual study plan creation."""

    name: str = Field(..., min_length=1, max_length=255, description="Plan name")
    start_date: date = Field(..., description="Plan start date (YYYY-MM-DD)")
    end_date: date = Field(..., description="Plan end date (YYYY-MM-DD)")
    description: str | None = Field(default=None, description="Optional description")


class UpdatePlanItemStatusRequest(BaseModel):
    """Request payload for updating status of a study plan item."""

    status: str = Field(
        ...,
        description="Target status ('pending', 'accepted', 'in_progress', 'completed', 'dismissed')",
    )


class BulkItemActionRequest(BaseModel):
    """Request payload for bulk updating items in a study plan."""

    action: str = Field(
        ...,
        description="Action to perform: 'accept' or 'dismiss'",
    )
    item_ids: list[uuid.UUID] | None = Field(
        default=None,
        description="Optional list of item UUIDs to update. If omitted, all pending items in the plan are targeted.",
    )


class BulkItemActionData(BaseModel):
    """Data payload for bulk item action response."""

    updated_count: int


class BulkItemActionResponse(BaseModel):
    """Standardized bulk item action response envelope."""

    success: bool = True
    data: BulkItemActionData
    message: str = "Study plan items updated successfully."


class StudyPlanResponse(BaseModel):
    """Standardized single study plan response envelope."""

    success: bool = True
    data: StudyPlanDTO
    message: str = "Study plan retrieved successfully."


class StudyPlanListResponse(BaseModel):
    """Standardized study plan list response envelope."""

    success: bool = True
    data: list[StudyPlanDTO]
    message: str = "Study plans retrieved successfully."
    meta: dict[str, Any] | None = None


class StudyPlanItemResponse(BaseModel):
    """Standardized single study plan item response envelope."""

    success: bool = True
    data: StudyPlanItemDTO
    message: str = "Study plan item updated successfully."


class DeleteResponse(BaseModel):
    """Standardized deletion response envelope."""

    success: bool = True
    data: Any | None = None
    message: str = "Study plan deleted successfully."


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/study-plans/generate",
    response_model=StudyPlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate AI study & revision plan",
)
async def generate_study_plan(
    body: GenerateStudyPlanRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
) -> StudyPlanResponse:
    """Generate an AI-driven study and revision plan taking academic schedule and exams into account."""
    try:
        plan = await study_plan_service.generate_ai_study_plan(
            user_id=current_user.id,
            name=body.name,
            start_date=body.start_date,
            end_date=body.end_date,
            target_goals=body.target_goals,
            plan_code=current_user.current_plan_code,
        )
        return StudyPlanResponse(
            success=True,
            data=plan,
            message="Study plan generated successfully.",
        )
    except InvalidPlanDateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily AI study plan generation quota limit reached.",
        ) from exc
    except QuotaServiceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quota service temporarily unavailable (fail-closed).",
        ) from exc
    except GeminiRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Google Gemini rate limit reached. Please try again shortly.",
        ) from exc
    except GeminiCircuitOpenError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service circuit breaker open due to high upstream failure rate.",
        ) from exc
    except GeminiTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI service timed out while generating study plan.",
        ) from exc


@router.post(
    "/study-plans",
    response_model=StudyPlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create study plan manually",
)
async def create_study_plan(
    body: CreateStudyPlanRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
) -> StudyPlanResponse:
    """Create an empty study plan manually without AI generation."""
    try:
        plan = await study_plan_service.create_plan(
            user_id=current_user.id,
            name=body.name,
            start_date=body.start_date,
            end_date=body.end_date,
            description=body.description,
        )
        return StudyPlanResponse(
            success=True,
            data=plan,
            message="Study plan created successfully.",
        )
    except InvalidPlanDateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get(
    "/study-plans",
    response_model=StudyPlanListResponse,
    status_code=status.HTTP_200_OK,
    summary="List student study plans",
)
async def list_study_plans(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
    skip: int = Query(default=0, ge=0, description="Offset for pagination"),
    limit: int = Query(default=50, ge=1, le=100, description="Page limit"),
    plan_status: str | None = Query(
        default=None, alias="status", description="Optional filter by status ('draft', 'active', etc.)"
    ),
) -> StudyPlanListResponse:
    """Retrieve all study plans belonging to the authenticated student."""
    plans, total = await study_plan_service.list_plans(
        user_id=current_user.id,
        skip=skip,
        limit=limit,
        status=plan_status,
    )
    return StudyPlanListResponse(
        success=True,
        data=plans,
        message="Study plans retrieved successfully.",
        meta={"total": total, "skip": skip, "limit": limit},
    )


@router.get(
    "/study-plans/{id}",
    response_model=StudyPlanResponse,
    status_code=status.HTTP_200_OK,
    summary="Get study plan details",
)
async def get_study_plan(
    id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
) -> StudyPlanResponse:
    """Fetch details of a specific study plan and its scheduled items."""
    try:
        plan = await study_plan_service.get_plan(user_id=current_user.id, plan_id=id)
        return StudyPlanResponse(
            success=True,
            data=plan,
            message="Study plan retrieved successfully.",
        )
    except StudyPlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete(
    "/study-plans/{id}",
    response_model=DeleteResponse,
    status_code=status.HTTP_200_OK,
    summary="Delete a study plan",
)
async def delete_study_plan(
    id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
) -> DeleteResponse:
    """Delete a study plan and cascade delete all its items."""
    try:
        await study_plan_service.delete_plan(user_id=current_user.id, plan_id=id)
        return DeleteResponse(
            success=True,
            data=None,
            message="Study plan deleted successfully.",
        )
    except StudyPlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.patch(
    "/study-plans/{id}/items/{item_id}",
    response_model=StudyPlanItemResponse,
    status_code=status.HTTP_200_OK,
    summary="Update study plan item status",
)
async def update_study_plan_item_status(
    id: uuid.UUID,
    item_id: uuid.UUID,
    body: UpdatePlanItemStatusRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
) -> StudyPlanItemResponse:
    """Update status of a study plan item (e.g. accepted, in_progress, completed, dismissed)."""
    try:
        item = await study_plan_service.update_item_status(
            user_id=current_user.id,
            plan_id=id,
            item_id=item_id,
            status=body.status,
        )
        return StudyPlanItemResponse(
            success=True,
            data=item,
            message="Study plan item status updated successfully.",
        )
    except StudyPlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post(
    "/study-plans/{plan_id}/items/{item_id}/accept",
    response_model=StudyPlanItemResponse,
    status_code=status.HTTP_200_OK,
    summary="Accept a study plan item",
)
async def accept_study_plan_item(
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
) -> StudyPlanItemResponse:
    """Accept a study plan item (sets status to 'accepted' and accepted_at to now)."""
    try:
        item = await study_plan_service.accept_item(
            user_id=current_user.id,
            plan_id=plan_id,
            item_id=item_id,
        )
        return StudyPlanItemResponse(
            success=True,
            data=item,
            message="Study plan item accepted successfully.",
        )
    except StudyPlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/study-plans/{plan_id}/items/{item_id}/dismiss",
    response_model=StudyPlanItemResponse,
    status_code=status.HTTP_200_OK,
    summary="Dismiss a study plan item",
)
async def dismiss_study_plan_item(
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
) -> StudyPlanItemResponse:
    """Dismiss a study plan item (sets status to 'dismissed')."""
    try:
        item = await study_plan_service.dismiss_item(
            user_id=current_user.id,
            plan_id=plan_id,
            item_id=item_id,
        )
        return StudyPlanItemResponse(
            success=True,
            data=item,
            message="Study plan item dismissed successfully.",
        )
    except StudyPlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/study-plans/{plan_id}/items/{item_id}/complete",
    response_model=StudyPlanItemResponse,
    status_code=status.HTTP_200_OK,
    summary="Complete a study plan item",
)
async def complete_study_plan_item(
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
) -> StudyPlanItemResponse:
    """Complete a study plan item (sets status to 'completed' and completed_at to now)."""
    try:
        item = await study_plan_service.complete_item(
            user_id=current_user.id,
            plan_id=plan_id,
            item_id=item_id,
        )
        return StudyPlanItemResponse(
            success=True,
            data=item,
            message="Study plan item completed successfully.",
        )
    except StudyPlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/study-plans/{plan_id}/items/bulk",
    response_model=BulkItemActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Bulk accept or dismiss study plan items",
)
async def bulk_study_plan_items_action(
    plan_id: uuid.UUID,
    body: BulkItemActionRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    study_plan_service: Annotated[StudyPlanService, Depends(get_study_plan_service)],
) -> BulkItemActionResponse:
    """Perform bulk accept or dismiss action on pending items of a study plan."""
    try:
        updated_count = await study_plan_service.bulk_action(
            user_id=current_user.id,
            plan_id=plan_id,
            action=body.action,
            item_ids=body.item_ids,
        )
        return BulkItemActionResponse(
            success=True,
            data=BulkItemActionData(updated_count=updated_count),
            message=f"Study plan items updated successfully ({updated_count} items).",
        )
    except StudyPlanNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
