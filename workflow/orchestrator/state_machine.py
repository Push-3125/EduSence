"""Deterministic Finite State Machine managing persistent task lifecycle and audit trail."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any, Optional
import uuid

from .models import (
    AuditLogEntry,
    EventType,
    OrchestratorState,
    TaskState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_STATE_FILE = REPO_ROOT / "workflow" / "orchestrator_state.json"


class StateTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


class TaskStateMachine:
    """Deterministic finite state machine with atomic persistence, retry limits, and audit trails."""

    def __init__(self, state_file: Optional[Path] = None, max_retries: int = 3) -> None:
        self.state_file = state_file or DEFAULT_STATE_FILE
        self.max_retries = max_retries
        self.state = self.load_or_initialize()

    def load_or_initialize(self) -> OrchestratorState:
        """Load persistent state from disk or initialize fresh state."""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                return OrchestratorState.model_validate(data)
            except Exception as e:
                print(f"[WARN] Failed to load state file {self.state_file}: {e}. Initializing fresh state.")
        return OrchestratorState(max_retries=self.max_retries)

    def persist(self) -> None:
        """Atomically persist state machine snapshot to disk using temp file rename."""
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        payload = self.state.model_dump_json(indent=2)
        # Write to temp file in same directory for atomic replace
        with tempfile.NamedTemporaryFile("w", dir=str(self.state_file.parent), delete=False, encoding="utf-8") as tf:
            tf.write(payload)
            tf.write("\n")
            temp_name = tf.name

        # Atomically replace
        Path(temp_name).replace(self.state_file)

    def process_event(
        self,
        event_type: EventType,
        task_id: str,
        actor: str = "orchestrator",
        event_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> tuple[TaskState, bool]:
        """Process state machine event with strict idempotency and transition verification.

        Returns:
            tuple[TaskState, bool]: (resulting_state, was_transitioned)
        """
        details = details or {}
        eid = event_id or str(uuid.uuid4())

        # 1. Idempotency Check: if event was already processed, return current state without side effects
        if eid in self.state.processed_events:
            return self.state.current_state, False

        current = self.state.current_state
        next_state: TaskState

        # 2. Transition Rules Matrix
        if event_type == EventType.START_TASK:
            if current not in (TaskState.READY, TaskState.CHANGES_REQUESTED):
                raise StateTransitionError(
                    f"Cannot START_TASK from state '{current}'. Expected READY or CHANGES_REQUESTED."
                )
            self.state.current_task_id = task_id
            self.state.attempt_number += 1
            next_state = TaskState.RUNNING

        elif event_type == EventType.TASK_COMPLETED_SUCCESS:
            if current != TaskState.RUNNING:
                raise StateTransitionError(
                    f"Cannot complete task from state '{current}'. Expected RUNNING."
                )
            self.state.branch_name = details.get("branch_name", self.state.branch_name)
            self.state.pr_url = details.get("pr_url", self.state.pr_url)
            next_state = TaskState.REVIEW

        elif event_type == EventType.TASK_FAILED:
            if current != TaskState.RUNNING:
                raise StateTransitionError(
                    f"Cannot fail task from state '{current}'. Expected RUNNING."
                )
            if self.state.attempt_number >= self.state.max_retries:
                next_state = TaskState.FAILED
            else:
                next_state = TaskState.READY

        elif event_type == EventType.REQUEST_CHANGES:
            if current != TaskState.REVIEW:
                raise StateTransitionError(
                    f"Cannot request changes from state '{current}'. Expected REVIEW."
                )
            self.state.review_feedback = details.get("feedback", "Changes requested by reviewer.")
            next_state = TaskState.CHANGES_REQUESTED

        elif event_type == EventType.APPROVE_TASK:
            if current != TaskState.REVIEW:
                raise StateTransitionError(
                    f"Cannot approve task from state '{current}'. Expected REVIEW."
                )
            self.state.approver = details.get("approver", actor)
            self.state.approved_at = datetime.now(timezone.utc).isoformat()
            next_state = TaskState.APPROVED

        elif event_type == EventType.FINALIZE_TASK:
            if current != TaskState.APPROVED:
                raise StateTransitionError(
                    f"Cannot finalize task from state '{current}'. Expected APPROVED."
                )
            next_state = TaskState.DONE

        elif event_type == EventType.ENCOUNTERED_BLOCKER:
            next_state = TaskState.BLOCKED

        elif event_type == EventType.UNBLOCK_TASK:
            if current != TaskState.BLOCKED:
                raise StateTransitionError(
                    f"Cannot unblock task from state '{current}'. Expected BLOCKED."
                )
            next_state = TaskState.READY

        else:
            raise StateTransitionError(f"Unknown event type: {event_type}")

        # 3. Record Audit Log Entry
        audit = AuditLogEntry(
            event_id=eid,
            event_type=event_type,
            task_id=task_id,
            from_state=current,
            to_state=next_state,
            actor=actor,
            details=details,
        )
        self.state.history.append(audit)
        self.state.processed_events.append(eid)
        self.state.current_state = next_state
        self.persist()

        return next_state, True

    def reset_for_next_task(self, new_task_id: str) -> None:
        """Reset state machine for executing the next eligible task after completion."""
        self.state.current_task_id = new_task_id
        self.state.current_state = TaskState.READY
        self.state.attempt_number = 0
        self.state.branch_name = None
        self.state.pr_number = None
        self.state.pr_url = None
        self.state.review_feedback = None
        self.state.approver = None
        self.state.approved_at = None
        self.persist()

    def recover(self) -> dict[str, Any]:
        """Inspect persisted state after runner restart and determine safe recovery action."""
        current = self.state.current_state
        task_id = self.state.current_task_id
        recovery_info = {
            "recovered_task_id": task_id,
            "state": current,
            "attempt": self.state.attempt_number,
            "needs_intervention": False,
            "recommended_action": "proceed",
        }

        if current == TaskState.RUNNING:
            # Runner terminated unexpectedly while executing task
            recovery_info["needs_intervention"] = True
            recovery_info["recommended_action"] = "check_working_tree_and_retry_or_fail"
        elif current == TaskState.REVIEW:
            recovery_info["recommended_action"] = "await_approval_or_changes"
        elif current == TaskState.BLOCKED:
            recovery_info["recommended_action"] = "resolve_blocker"
        elif current == TaskState.DONE:
            recovery_info["recommended_action"] = "schedule_next_task"

        return recovery_info
