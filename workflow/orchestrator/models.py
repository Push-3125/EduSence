"""Domain models and data schemas for EduSense Autonomous Task Orchestrator."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class TaskState(str, Enum):
    """Explicit state of a task in the Orchestrator finite state machine."""
    READY = "READY"
    RUNNING = "RUNNING"
    REVIEW = "REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"
    DONE = "DONE"
    FAILED = "FAILED"


class EventType(str, Enum):
    """Explicit event triggers that transition the FSM between states."""
    START_TASK = "START_TASK"
    TASK_COMPLETED_SUCCESS = "TASK_COMPLETED_SUCCESS"
    TASK_FAILED = "TASK_FAILED"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    APPROVE_TASK = "APPROVE_TASK"
    FINALIZE_TASK = "FINALIZE_TASK"
    ENCOUNTERED_BLOCKER = "ENCOUNTERED_BLOCKER"
    UNBLOCK_TASK = "UNBLOCK_TASK"


class AuditLogEntry(BaseModel):
    """Immutable audit record for every state transition in the orchestrator."""
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_id: str
    event_type: EventType
    task_id: str
    from_state: TaskState
    to_state: TaskState
    actor: str
    details: dict[str, Any] = Field(default_factory=dict)


class TaskExecutionSpec(BaseModel):
    """Complete, self-contained specification passed to the execution adapter."""
    task_id: str
    milestone: str
    title: str
    category: str
    dependencies: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    review_feedback: Optional[str] = None
    attempt_number: int = 1
    conversation_id: Optional[str] = None


class VerificationResult(BaseModel):
    """Individual verification command execution record."""
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    passed: bool


class TaskExecutionResult(BaseModel):
    """Result returned by the execution adapter."""
    success: bool
    task_id: str
    attempt_number: int
    conversation_id: Optional[str] = None
    modified_files: list[str] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    error_message: Optional[str] = None
    agent_output: str = ""


class OrchestratorState(BaseModel):
    """Persistent orchestrator state machine snapshot for recovery after runner restart."""
    current_task_id: Optional[str] = None
    conversation_id: Optional[str] = None
    current_state: TaskState = TaskState.READY
    attempt_number: int = 0
    max_retries: int = 3
    branch_name: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    review_feedback: Optional[str] = None
    approver: Optional[str] = None
    approved_at: Optional[str] = None
    processed_events: list[str] = Field(default_factory=list)
    history: list[AuditLogEntry] = Field(default_factory=list)
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
