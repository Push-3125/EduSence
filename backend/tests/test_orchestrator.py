"""Comprehensive Unit & Integration Test Suite for EduSense Autonomous Task Orchestrator.

Verifies:
1. Deterministic state transitions across the full lifecycle (READY -> RUNNING -> REVIEW -> CHANGES_REQUESTED -> APPROVED -> DONE).
2. Illegal transition rejection (StateTransitionError).
3. Idempotent event processing (duplicate event IDs cause no state change or duplicate audit entries).
4. Retry limits enforcement (fails after max_retries).
5. State persistence and safe crash recovery across runner restarts.
6. Approver authorization and CI status verification gates.
7. Secret redaction in reports and notifications.
8. End-to-end execution of a harmless dummy task.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from workflow.orchestrator.adapters.antigravity import (
    InteractiveFallbackAdapter,
    SimulatedAdapter,
)
from workflow.orchestrator.adapters.github import GitHubAdapter, _run_git
from workflow.orchestrator.adapters.notifier import redact_secrets
from workflow.orchestrator.models import (
    EventType,
    TaskExecutionSpec,
    TaskState,
)
from workflow.orchestrator.runner import OrchestratorRunner
from workflow.orchestrator.state_machine import StateTransitionError, TaskStateMachine

# ==============================================================================
# 1. STATE MACHINE & TRANSITION TESTS
# ==============================================================================


def test_happy_path_state_transitions():
    """Verify normal task progression: READY -> RUNNING -> REVIEW -> APPROVED -> DONE."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        state_file = Path(tf.name)

    try:
        fsm = TaskStateMachine(state_file=state_file, max_retries=3)
        assert fsm.state.current_state == TaskState.READY

        # 1. Start Task
        st, trans = fsm.process_event(EventType.START_TASK, task_id="TASK-T1")
        assert st == TaskState.RUNNING
        assert trans is True
        assert fsm.state.attempt_number == 1

        # 2. Complete Execution -> REVIEW
        st, trans = fsm.process_event(
            EventType.TASK_COMPLETED_SUCCESS,
            task_id="TASK-T1",
            details={"branch_name": "task/task-t1", "pr_url": "https://github.com/PR/1"},
        )
        assert st == TaskState.REVIEW
        assert trans is True

        # 3. Approve -> APPROVED
        st, trans = fsm.process_event(
            EventType.APPROVE_TASK,
            task_id="TASK-T1",
            actor="Product Owner",
        )
        assert st == TaskState.APPROVED
        assert trans is True

        # 4. Finalize -> DONE
        st, trans = fsm.process_event(EventType.FINALIZE_TASK, task_id="TASK-T1")
        assert st == TaskState.DONE
        assert trans is True
        assert len(fsm.state.history) == 4
    finally:
        if state_file.exists():
            state_file.unlink()


def test_illegal_state_transitions_raise_error():
    """Verify that illegal or out-of-order transitions are strictly rejected."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        state_file = Path(tf.name)

    try:
        fsm = TaskStateMachine(state_file=state_file)
        assert fsm.state.current_state == TaskState.READY

        # Cannot approve while in READY
        with pytest.raises(StateTransitionError):
            fsm.process_event(EventType.APPROVE_TASK, task_id="TASK-T2")

        # Cannot finalize while in READY
        with pytest.raises(StateTransitionError):
            fsm.process_event(EventType.FINALIZE_TASK, task_id="TASK-T2")

        # Transition to RUNNING
        fsm.process_event(EventType.START_TASK, task_id="TASK-T2")

        # Cannot start again while RUNNING
        with pytest.raises(StateTransitionError):
            fsm.process_event(EventType.START_TASK, task_id="TASK-T2")
    finally:
        if state_file.exists():
            state_file.unlink()


def test_idempotent_event_processing():
    """Verify duplicate event IDs are ignored without side effects or duplicate history."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        state_file = Path(tf.name)

    try:
        fsm = TaskStateMachine(state_file=state_file)
        eid = "event-id-fixed-12345"

        # First dispatch
        st1, trans1 = fsm.process_event(EventType.START_TASK, task_id="TASK-T3", event_id=eid)
        assert st1 == TaskState.RUNNING
        assert trans1 is True
        assert fsm.state.attempt_number == 1
        assert len(fsm.state.history) == 1

        # Duplicate dispatch with exact same event_id
        st2, trans2 = fsm.process_event(EventType.START_TASK, task_id="TASK-T3", event_id=eid)
        assert st2 == TaskState.RUNNING
        assert trans2 is False  # No new transition
        assert fsm.state.attempt_number == 1  # Not incremented again
        assert len(fsm.state.history) == 1  # No duplicate audit log entry
    finally:
        if state_file.exists():
            state_file.unlink()


def test_changes_requested_and_resume_cycle():
    """Verify REVIEW -> CHANGES_REQUESTED -> RUNNING (attempt 2) cycle."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        state_file = Path(tf.name)

    try:
        fsm = TaskStateMachine(state_file=state_file)
        fsm.process_event(EventType.START_TASK, task_id="TASK-T4")
        fsm.process_event(EventType.TASK_COMPLETED_SUCCESS, task_id="TASK-T4")
        assert fsm.state.current_state == TaskState.REVIEW

        # Reviewer requests changes
        feedback_note = "Please fix input validation on line 42."
        st, trans = fsm.process_event(
            EventType.REQUEST_CHANGES,
            task_id="TASK-T4",
            actor="Push-3125",
            details={"feedback": feedback_note},
        )
        assert st == TaskState.CHANGES_REQUESTED
        assert fsm.state.review_feedback == feedback_note

        # Resume task with feedback -> RUNNING (attempt 2)
        st, trans = fsm.process_event(EventType.START_TASK, task_id="TASK-T4")
        assert st == TaskState.RUNNING
        assert trans is True
        assert fsm.state.attempt_number == 2
    finally:
        if state_file.exists():
            state_file.unlink()


def test_retry_limits_and_failure():
    """Verify task transitions to READY on failure under max_retries, and FAILED when limit exceeded."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        state_file = Path(tf.name)

    try:
        fsm = TaskStateMachine(state_file=state_file, max_retries=2)

        # Attempt 1: Fail -> resets to READY for retry
        fsm.process_event(EventType.START_TASK, task_id="TASK-T5")
        st, _ = fsm.process_event(EventType.TASK_FAILED, task_id="TASK-T5")
        assert st == TaskState.READY
        assert fsm.state.attempt_number == 1

        # Attempt 2: Fail -> reaches max_retries (2), transitions to FAILED
        fsm.process_event(EventType.START_TASK, task_id="TASK-T5")
        st, _ = fsm.process_event(EventType.TASK_FAILED, task_id="TASK-T5")
        assert st == TaskState.FAILED
    finally:
        if state_file.exists():
            state_file.unlink()


def test_crash_recovery_across_restart():
    """Verify state persistence across crash and runner restart."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        state_file = Path(tf.name)

    try:
        # Session 1: crash while RUNNING
        fsm1 = TaskStateMachine(state_file=state_file)
        fsm1.process_event(EventType.START_TASK, task_id="TASK-CRASH-TEST")
        assert fsm1.state.current_state == TaskState.RUNNING

        # Session 2: Fresh runner restart reading from same state file
        fsm2 = TaskStateMachine(state_file=state_file)
        assert fsm2.state.current_state == TaskState.RUNNING
        assert fsm2.state.current_task_id == "TASK-CRASH-TEST"

        rec = fsm2.recover()
        assert rec["needs_intervention"] is True
        assert rec["state"] == TaskState.RUNNING
        assert rec["recovered_task_id"] == "TASK-CRASH-TEST"
    finally:
        if state_file.exists():
            state_file.unlink()


# ==============================================================================
# 2. SECURITY, APPROVAL & SECRET REDACTION TESTS
# ==============================================================================


def test_approver_authorization():
    """Verify only authorized Product Owner accounts can approve tasks."""
    github = GitHubAdapter()

    # Authorized user
    ok, msg = github.verify_approver("Push-3125")
    assert ok is True

    # Product Owner role
    ok, msg = github.verify_approver("Product Owner")
    assert ok is True

    # Unauthorized attacker
    ok, msg = github.verify_approver("unauthorized_random_hacker")
    assert ok is False
    assert "Unauthorized" in msg


def test_secret_redaction():
    """Verify secrets (Gemini keys, GitHub tokens, passwords) are scrubbed from outputs."""
    raw = (
        "Logs: GEMINI_API_KEY=AIzaSyA12345678901234567890123456789012 "
        "and GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
        "and password: 'super_secret_password_123'"
    )
    scrubbed = redact_secrets(raw)
    assert "AIzaSy" not in scrubbed
    assert "ghp_abcdef" not in scrubbed
    assert "super_secret_password_123" not in scrubbed
    assert "[REDACTED" in scrubbed


def test_interactive_fallback_adapter_does_not_fake():
    """Verify fallback adapter reports missing setup and does not fake execution."""
    adapter = InteractiveFallbackAdapter()
    avail, diag = adapter.is_available()
    assert avail is False
    assert "Neither Google Antigravity Python SDK" in diag

    spec = TaskExecutionSpec(
        task_id="TASK-TEST",
        milestone="M0.5",
        title="Test",
        category="backend",
    )
    res = adapter.execute_task(spec)
    assert res.success is False
    assert "Antigravity runtime unavailable" in (res.error_message or "")


# ==============================================================================
# 3. END-TO-END HARMLESS DUMMY TASK WORKFLOW TEST
# ==============================================================================


def test_dummy_task_end_to_end_lifecycle(tmp_path):
    """Test full cycle: select -> run dummy task -> verify -> stage review -> approve -> complete."""
    _, orig_branch, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    try:
        dummy_backlog = tmp_path / "backlog.json"
        dummy_state = tmp_path / "orchestrator_state.json"
        dummy_decisions = tmp_path / "decisions.json"
        dummy_target_file = tmp_path / "test_artifact.txt"

        # Setup test backlog
        initial_backlog = {
            "milestones": [{"id": "M0.5", "name": "Test Milestone", "status": "in_progress"}],
            "tasks": [
                {
                    "id": "TASK-DUMMY-TEST",
                    "milestone": "M0.5",
                    "title": "Harmless Dummy Integration Test Task",
                    "category": "test",
                    "dependencies": [],
                    "status": "pending",
                    "requires_approval": False,
                    "approval_status": "not_required",
                    "unresolved_decisions": [],
                    "affected_files": [str(dummy_target_file.relative_to(REPO_ROOT).as_posix()) if dummy_target_file.is_relative_to(REPO_ROOT) else "workflow/tests/dummy_target.txt"],
                    "verification_commands": ["python -c \"print('Verification passed')\""],
                }
            ],
        }
        dummy_backlog.write_text(json.dumps(initial_backlog), encoding="utf-8")
        dummy_decisions.write_text(json.dumps({"confirmed_decisions": [], "unresolved_questions": []}), encoding="utf-8")

        fsm = TaskStateMachine(state_file=dummy_state)
        adapter = SimulatedAdapter()
        runner = OrchestratorRunner(
            backlog_path=dummy_backlog,
            decisions_path=dummy_decisions,
            state_machine=fsm,
            adapter=adapter,
        )

        # 1. Execute task step -> Transitions to REVIEW
        res = runner.execute_next_task(target_task_id="TASK-DUMMY-TEST")
        assert res["status"] == "review_ready"
        assert fsm.state.current_state == TaskState.REVIEW

        # 2. Attempt approval by unauthorized user -> Fails
        auth_fail_res = runner.process_review_decision(
            task_id="TASK-DUMMY-TEST",
            reviewer="unauthorized_guest",
            decision="APPROVED",
        )
        assert auth_fail_res["status"] == "error"
        assert fsm.state.current_state == TaskState.REVIEW  # Still in REVIEW

        # 3. Request changes -> Transitions to CHANGES_REQUESTED
        chg_res = runner.process_review_decision(
            task_id="TASK-DUMMY-TEST",
            reviewer="Push-3125",
            decision="CHANGES_REQUESTED",
            feedback="Please add extra comment in artifact.",
        )
        assert chg_res["status"] == "changes_requested"
        assert fsm.state.current_state == TaskState.CHANGES_REQUESTED

        # 4. Resume task with feedback -> Transitions to RUNNING -> REVIEW (attempt 2)
        resume_res = runner.execute_next_task(target_task_id="TASK-DUMMY-TEST")
        assert resume_res["status"] == "review_ready"
        assert fsm.state.current_state == TaskState.REVIEW
        assert fsm.state.attempt_number == 2

        # 5. Authorize and approve by PO -> Completes task and updates backlog
        app_res = runner.process_review_decision(
            task_id="TASK-DUMMY-TEST",
            reviewer="Push-3125",
            decision="APPROVED",
        )
        assert app_res["status"] == "completed"
        assert fsm.state.current_state == TaskState.DONE

        # Verify backlog was updated
        updated_backlog = json.loads(dummy_backlog.read_text(encoding="utf-8"))
        completed_task = updated_backlog["tasks"][0]
        assert completed_task["status"] == "completed"
        assert "Approved by Push-3125" in completed_task["evidence"]
    finally:
        if orig_branch and orig_branch != "HEAD":
            _run_git(["checkout", orig_branch])
            _run_git(["branch", "-D", "task/task-dummy-test"])
