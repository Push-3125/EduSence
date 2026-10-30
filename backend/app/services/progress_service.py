"""Academic Progress Service and DTOs (TASK-M5-07 / US 2.17, 2.18).

Adheres strictly to:
- Invariant 2: Layered architecture (Routers -> Services -> Repositories).
- Invariant 6: Strict tenant isolation (all operations scoped to authenticated user_id).
- Business Rules:
  1. Blended progress percentage from assignments and study plan items.
  2. Risk status evaluation:
     - 'overdue': any assignment past due date with status == 'pending'.
     - 'at_risk': progress_percent < 50% and exam within 7 days.
     - 'on_track': default healthy status.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends

from app.repositories.assignment_repo import (
    AssignmentRepository,
    get_assignment_repo,
)
from app.repositories.course_repo import CourseRepository, get_course_repo
from app.repositories.exam_repo import ExamRepository, get_exam_repo
from app.repositories.progress_repo import (
    ProgressRepository,
    get_progress_repo,
)
from app.repositories.study_plan_repo import (
    StudyPlanRepository,
    get_study_plan_repo,
)


class ProgressServiceError(Exception):
    """Base exception for progress service errors."""


class CourseNotFoundError(ProgressServiceError):
    """Raised when a course is not found or does not belong to the authenticated user."""


@dataclass
class CourseProgressDTO:
    """Decoupled DTO representing progress for an individual course."""

    course_id: uuid.UUID
    course_name: str
    course_code: str
    assignments_total: int
    assignments_completed: int
    study_items_total: int
    study_items_completed: int
    progress_percent: float
    risk_status: str
    last_calculated_at: datetime


@dataclass
class DashboardProgressDTO:
    """Decoupled DTO representing aggregated academic dashboard metrics."""

    total_courses: int
    overall_progress_percent: float
    on_track_count: int
    at_risk_count: int
    overdue_count: int
    courses: list[CourseProgressDTO]
    upcoming_urgent_deadlines: int


class ProgressService:
    """Service encapsulating course progress calculation, risk assessment, and dashboard analytics."""

    def __init__(
        self,
        progress_repo: ProgressRepository,
        course_repo: CourseRepository,
        assignment_repo: AssignmentRepository,
        study_plan_repo: StudyPlanRepository,
        exam_repo: ExamRepository,
    ) -> None:
        self.progress_repo = progress_repo
        self.course_repo = course_repo
        self.assignment_repo = assignment_repo
        self.study_plan_repo = study_plan_repo
        self.exam_repo = exam_repo

    async def calculate_course_progress(
        self, user_id: uuid.UUID, course_id: uuid.UUID
    ) -> CourseProgressDTO:
        """Calculate and persist progress for a single course owned by user.

        Steps:
        1. Verifies course ownership.
        2. Gathers actual assignments count (total, completed) via AssignmentRepository.
        3. Gathers study plan items count (total, completed) for this course via StudyPlanRepository.
        4. Checks upcoming exams via ExamRepository.
        5. Calculates progress_percent: blended percentage clamped [0.0, 100.0].
        6. Evaluates risk_status:
           - 'overdue': any assignment past due date with status == 'pending'.
           - 'at_risk': progress_percent < 50% and exam date is within 7 days.
           - 'on_track': healthy status.
        7. Saves via upsert_course_progress.
        8. Returns CourseProgressDTO.
        """
        # 1. Verify course ownership
        course = await self.course_repo.get_by_id_and_user(
            course_id=course_id, user_id=user_id
        )
        if not course:
            raise CourseNotFoundError(f"Course '{course_id}' was not found.")

        # 2. Gather actual assignments count
        assignments = await self.assignment_repo.list_by_course(
            user_id=user_id, course_id=course_id
        )
        assignments_total = len(assignments)
        assignments_completed = sum(1 for a in assignments if a.status == "completed")

        # 3. Gather study plan items count (excluding dismissed)
        study_items = await self.study_plan_repo.get_items_by_course(
            user_id=user_id, course_id=course_id
        )
        study_items_total = len(study_items)
        study_items_completed = sum(
            1 for item in study_items if item.status == "completed"
        )

        # 4. Check upcoming exams
        exams, _ = await self.exam_repo.list_by_user(
            user_id=user_id, course_id=course_id, upcoming_only=True, limit=100
        )

        # 5. Calculate blended progress_percent
        total_tasks = assignments_total + study_items_total
        completed_tasks = assignments_completed + study_items_completed
        if total_tasks > 0:
            raw_percent = (completed_tasks / total_tasks) * 100.0
            progress_percent = round(min(100.0, max(0.0, raw_percent)), 2)
        else:
            progress_percent = 0.0

        # 6. Evaluate risk_status
        now = datetime.now(UTC)
        seven_days_later = now + timedelta(days=7)

        # Check overdue pending assignments
        has_overdue_assignment = any(
            a.due_at is not None
            and (a.due_at if a.due_at.tzinfo else a.due_at.replace(tzinfo=UTC)) < now
            and a.status == "pending"
            for a in assignments
        )

        # Check upcoming exam within 7 days
        has_exam_within_7_days = any(
            now
            <= (e.exam_at if e.exam_at.tzinfo else e.exam_at.replace(tzinfo=UTC))
            <= seven_days_later
            for e in exams
        )

        if has_overdue_assignment:
            risk_status = "overdue"
        elif progress_percent < 50.0 and has_exam_within_7_days:
            risk_status = "at_risk"
        else:
            risk_status = "on_track"

        # 7. Persist via repository UPSERT
        progress = await self.progress_repo.upsert_course_progress(
            user_id=user_id,
            course_id=course_id,
            assignments_total=assignments_total,
            assignments_completed=assignments_completed,
            study_items_total=study_items_total,
            study_items_completed=study_items_completed,
            progress_percent=progress_percent,
            risk_status=risk_status,
        )

        # 8. Return decoupled DTO
        return CourseProgressDTO(
            course_id=course.id,
            course_name=course.name,
            course_code=course.course_code or "",
            assignments_total=progress.assignments_total,
            assignments_completed=progress.assignments_completed,
            study_items_total=progress.study_items_total,
            study_items_completed=progress.study_items_completed,
            progress_percent=float(progress.progress_percent),
            risk_status=progress.risk_status,
            last_calculated_at=progress.last_calculated_at,
        )

    async def recalculate_all_progress(
        self, user_id: uuid.UUID
    ) -> list[CourseProgressDTO]:
        """Recalculate and persist progress for all active courses owned by user."""
        courses, _ = await self.course_repo.list_by_user(
            user_id=user_id, status="active", limit=100
        )
        results: list[CourseProgressDTO] = []
        for course in courses:
            dto = await self.calculate_course_progress(
                user_id=user_id, course_id=course.id
            )
            results.append(dto)
        return results

    async def get_course_progress(
        self, user_id: uuid.UUID, course_id: uuid.UUID
    ) -> CourseProgressDTO:
        """Fetch updated progress for a specific course verifying ownership."""
        return await self.calculate_course_progress(user_id=user_id, course_id=course_id)

    async def list_course_progress(
        self, user_id: uuid.UUID
    ) -> list[CourseProgressDTO]:
        """List progress for all student courses."""
        return await self.recalculate_all_progress(user_id=user_id)

    async def get_dashboard(self, user_id: uuid.UUID) -> DashboardProgressDTO:
        """Get full aggregated academic progress dashboard for authenticated student."""
        courses_progress = await self.recalculate_all_progress(user_id=user_id)
        total_courses = len(courses_progress)

        if total_courses > 0:
            overall_progress_percent = round(
                sum(c.progress_percent for c in courses_progress) / total_courses, 2
            )
        else:
            overall_progress_percent = 0.0

        on_track_count = sum(1 for c in courses_progress if c.risk_status == "on_track")
        at_risk_count = sum(1 for c in courses_progress if c.risk_status == "at_risk")
        overdue_count = sum(1 for c in courses_progress if c.risk_status == "overdue")

        # Upcoming urgent deadlines (pending assignments due in next 7 days + exams in next 7 days)
        now = datetime.now(UTC)
        seven_days_later = now + timedelta(days=7)
        active_course_ids = {c.course_id for c in courses_progress}

        urgent_assignments = 0
        for course_id in active_course_ids:
            course_assignments = await self.assignment_repo.list_by_course(
                user_id=user_id, course_id=course_id
            )
            for a in course_assignments:
                if a.due_at and a.status == "pending":
                    due_at_utc = (
                        a.due_at if a.due_at.tzinfo else a.due_at.replace(tzinfo=UTC)
                    )
                    if now <= due_at_utc <= seven_days_later:
                        urgent_assignments += 1

        all_upcoming_exams, _ = await self.exam_repo.list_by_user(
            user_id=user_id, upcoming_only=True, limit=100
        )
        urgent_exams = 0
        for e in all_upcoming_exams:
            if e.course_id in active_course_ids:
                exam_at_utc = (
                    e.exam_at if e.exam_at.tzinfo else e.exam_at.replace(tzinfo=UTC)
                )
                if now <= exam_at_utc <= seven_days_later:
                    urgent_exams += 1

        upcoming_urgent_deadlines = urgent_assignments + urgent_exams

        return DashboardProgressDTO(
            total_courses=total_courses,
            overall_progress_percent=overall_progress_percent,
            on_track_count=on_track_count,
            at_risk_count=at_risk_count,
            overdue_count=overdue_count,
            courses=courses_progress,
            upcoming_urgent_deadlines=upcoming_urgent_deadlines,
        )


def get_progress_service(
    progress_repo: Annotated[ProgressRepository, Depends(get_progress_repo)],
    course_repo: Annotated[CourseRepository, Depends(get_course_repo)],
    assignment_repo: Annotated[AssignmentRepository, Depends(get_assignment_repo)],
    study_plan_repo: Annotated[StudyPlanRepository, Depends(get_study_plan_repo)],
    exam_repo: Annotated[ExamRepository, Depends(get_exam_repo)],
) -> ProgressService:
    """FastAPI dependency provider for ProgressService."""
    return ProgressService(
        progress_repo=progress_repo,
        course_repo=course_repo,
        assignment_repo=assignment_repo,
        study_plan_repo=study_plan_repo,
        exam_repo=exam_repo,
    )


__all__ = [
    "CourseNotFoundError",
    "CourseProgressDTO",
    "DashboardProgressDTO",
    "ProgressService",
    "ProgressServiceError",
    "get_progress_service",
]
