"""Autonomous Task Orchestrator Runner binding FSM, Adapters, GitHub, and Notifications."""

import json
from pathlib import Path
from typing import Any, Optional

from .adapters.antigravity import get_antigravity_adapter
from .adapters.base import AntigravityAdapter
from .adapters.github import GitHubAdapter
from .adapters.notifier import Notifier
from .models import EventType, TaskExecutionSpec, TaskState
from .state_machine import TaskStateMachine

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKLOG_PATH = REPO_ROOT / "workflow" / "backlog.json"
DECISIONS_PATH = REPO_ROOT / "workflow" / "decisions.json"


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


class OrchestratorRunner:
    """Executes single task lifecycle deterministically with strict gates."""

    def __init__(
        self,
        backlog_path: Optional[Path] = None,
        decisions_path: Optional[Path] = None,
        state_machine: Optional[TaskStateMachine] = None,
        adapter: Optional[AntigravityAdapter] = None,
        github_adapter: Optional[GitHubAdapter] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self.backlog_path = backlog_path or BACKLOG_PATH
        self.decisions_path = decisions_path or DECISIONS_PATH
        self.fsm = state_machine or TaskStateMachine()
        self.adapter = adapter or get_antigravity_adapter()
        self.github = github_adapter or GitHubAdapter()
        self.notifier = notifier or Notifier()

    def select_next_eligible_task(self) -> Optional[dict[str, Any]]:
        """Identify next eligible pending task with all dependencies and approvals met."""
        backlog = _load_json(self.backlog_path)
        tasks = backlog.get("tasks", [])
        completed_ids = {t["id"] for t in tasks if t.get("status") == "completed"}

        for t in tasks:
            if t.get("status") == "pending":
                deps = t.get("dependencies", [])
                deps_met = all(d in completed_ids for d in deps)
                decisions_clear = not t.get("unresolved_decisions")
                if deps_met and decisions_clear:
                    return t
        return None

    def execute_next_task(
        self,
        target_task_id: Optional[str] = None,
        actor: str = "orchestrator",
        feedback: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute exactly one task through the approved scope and transition to REVIEW."""
        backlog = _load_json(self.backlog_path)
        tasks = backlog.get("tasks", [])

        # 1. Resolve task
        task_data = None
        if target_task_id:
            task_data = next((t for t in tasks if t["id"] == target_task_id), None)
            if not task_data:
                return {"status": "error", "message": f"Task '{target_task_id}' not found in backlog."}
        else:
            task_data = self.select_next_eligible_task()
            if not task_data:
                return {
                    "status": "idle",
                    "message": "No eligible pending tasks found. All remaining tasks may be blocked or completed.",
                }

        task_id = task_data["id"]

        # 2. Check blockers
        if task_data.get("unresolved_decisions") or (task_data.get("requires_approval") and task_data.get("approval_status") != "approved"):
            self.fsm.process_event(
                EventType.ENCOUNTERED_BLOCKER,
                task_id=task_id,
                actor=actor,
                details={"reason": "Task blocked by unresolved PO decisions or approval requirement."},
            )
            msg = f"Task {task_id} is BLOCKED by pending approvals: {task_data.get('unresolved_decisions')}"
            self.notifier.notify(f"Task {task_id} Blocked", msg, level="WARN")
            return {"status": "blocked", "task_id": task_id, "message": msg}

        # 3. Transition to RUNNING
        if self.fsm.state.current_state not in (TaskState.READY, TaskState.CHANGES_REQUESTED):
            if self.fsm.state.current_task_id == task_id and self.fsm.state.current_state == TaskState.RUNNING:
                # Already running (e.g. resuming)
                pass
            else:
                self.fsm.reset_for_next_task(task_id)

        self.fsm.process_event(EventType.START_TASK, task_id=task_id, actor=actor)

        # 4. Prepare branch
        branch_ok, branch_name = self.github.ensure_task_branch(task_id)
        if not branch_ok:
            self.fsm.process_event(
                EventType.TASK_FAILED,
                task_id=task_id,
                actor=actor,
                details={"error": branch_name},
            )
            return {"status": "failed", "task_id": task_id, "message": f"Branch setup failed: {branch_name}"}

        # 5. Build TaskExecutionSpec
        spec = TaskExecutionSpec(
            task_id=task_id,
            milestone=task_data.get("milestone", "M0.5"),
            title=task_data.get("title", ""),
            category=task_data.get("category", "backend"),
            dependencies=task_data.get("dependencies", []),
            affected_files=task_data.get("affected_files", []),
            verification_commands=task_data.get("verification_commands", []),
            review_feedback=feedback or self.fsm.state.review_feedback,
            attempt_number=self.fsm.state.attempt_number,
        )

        # 6. Execute via Adapter
        self.notifier.notify(
            f"Executing {task_id}",
            f"Starting task execution on branch '{branch_name}' (Attempt {spec.attempt_number})...",
            level="INFO",
        )
        res = self.adapter.execute_task(spec)

        if not res.success:
            err_msg = res.error_message or "Task verification failed."
            new_state, _ = self.fsm.process_event(
                EventType.TASK_FAILED,
                task_id=task_id,
                actor=actor,
                details={"error": err_msg, "attempt": spec.attempt_number},
            )
            self.notifier.notify(
                f"Task {task_id} Failed",
                f"Task execution failed on attempt {spec.attempt_number}. Current state: {new_state}.\nError: {err_msg}",
                level="ERROR",
            )
            return {
                "status": "failed",
                "task_id": task_id,
                "current_state": new_state,
                "error": err_msg,
            }

        # 7. Commit changes and prepare PR / Review Artifact
        commit_ok, commit_ref = self.github.stage_and_commit(task_id, spec.title, spec.affected_files)
        pr_body = (
            f"## Task Summary\n"
            f"- **Task ID:** {task_id}\n"
            f"- **Milestone:** {spec.milestone}\n"
            f"- **Title:** {spec.title}\n"
            f"- **Attempt:** {spec.attempt_number}\n\n"
            f"## Changed Files\n"
            + "\n".join(f"- `{f}`" for f in spec.affected_files)
            + f"\n\n## Verification Results\n"
            + "\n".join(f"- `{v.command}`: {'PASSED' if v.passed else 'FAILED'}" for v in res.verification_results)
        )
        pr_ok, pr_msg, pr_ref = self.github.create_or_update_pr(task_id, spec.title, pr_body)

        # 8. Transition to REVIEW
        self.fsm.process_event(
            EventType.TASK_COMPLETED_SUCCESS,
            task_id=task_id,
            actor=actor,
            details={"branch_name": branch_name, "pr_url": pr_ref, "commit": commit_ref},
        )

        # 9. Notify user
        report = (
            f"Task {task_id} ({spec.title}) execution and verification SUCCEEDED.\n"
            f"Branch: {branch_name}\n"
            f"Review Artifact: {pr_ref or pr_msg}\n"
            f"Current state: REVIEW (Awaiting explicit human approval or changes request).\n"
            f"To approve: python workflow/workflow.py review --task-id {task_id} --decision APPROVED --reviewer <PO_USER>\n"
            f"To request changes: python workflow/workflow.py review --task-id {task_id} --decision CHANGES_REQUESTED --feedback '<NOTES>'"
        )
        self.notifier.notify(f"Task {task_id} Ready for Review", report, level="INFO")

        return {
            "status": "review_ready",
            "task_id": task_id,
            "branch": branch_name,
            "pr": pr_ref,
            "state": TaskState.REVIEW,
        }

    def process_review_decision(
        self,
        task_id: str,
        reviewer: str,
        decision: str,
        feedback: Optional[str] = None,
    ) -> dict[str, Any]:
        """Process explicit human review decision (APPROVED or CHANGES_REQUESTED)."""
        if self.fsm.state.current_task_id != task_id:
            return {"status": "error", "message": f"Active task in state machine is '{self.fsm.state.current_task_id}', not '{task_id}'."}

        if self.fsm.state.current_state != TaskState.REVIEW:
            return {"status": "error", "message": f"Task '{task_id}' is in state '{self.fsm.state.current_state}', not REVIEW."}

        # 1. Verify reviewer identity and permissions
        auth_ok, auth_msg = self.github.verify_approver(reviewer)
        if not auth_ok:
            return {"status": "error", "message": f"Permission denied: {auth_msg}"}

        dec_upper = decision.upper().strip()

        # 2. Handle CHANGES_REQUESTED
        if dec_upper in ("CHANGES_REQUESTED", "REJECT", "REVISE"):
            notes = feedback or "Reviewer requested changes."
            self.fsm.process_event(
                EventType.REQUEST_CHANGES,
                task_id=task_id,
                actor=reviewer,
                details={"feedback": notes},
            )
            self.notifier.notify(
                f"Changes Requested on {task_id}",
                f"Reviewer '{reviewer}' requested changes on task {task_id}.\nFeedback: {notes}\n"
                f"State transitioned to CHANGES_REQUESTED. Resume with: python workflow/workflow.py orchestrate --resume",
                level="WARN",
            )
            return {"status": "changes_requested", "task_id": task_id, "feedback": notes}

        # 3. Handle APPROVED
        if dec_upper == "APPROVED":
            # Check required CI status
            ci_ok, ci_msg = self.github.check_ci_status()
            if not ci_ok:
                return {
                    "status": "error",
                    "message": f"Cannot approve task: Required CI verification checks did not pass:\n{ci_msg}",
                }

            # Transition REVIEW -> APPROVED -> DONE
            self.fsm.process_event(
                EventType.APPROVE_TASK,
                task_id=task_id,
                actor=reviewer,
                details={"approver": reviewer, "ci_status": ci_msg},
            )
            self.fsm.process_event(EventType.FINALIZE_TASK, task_id=task_id, actor=reviewer)

            # Update canonical backlog.json
            backlog = _load_json(self.backlog_path)
            for t in backlog.get("tasks", []):
                if t["id"] == task_id:
                    t["status"] = "completed"
                    t["evidence"] = f"Approved by {reviewer} with passing CI verification ({ci_msg})"
                    break
            _save_json(self.backlog_path, backlog)

            self.notifier.notify(
                f"Task {task_id} Approved & Completed",
                f"Task {task_id} approved by {reviewer}. CI verified. Status recorded as DONE in canonical backlog.",
                level="INFO",
            )

            # Check if all tasks in M0.5-M6 are done
            completion = self.check_project_completion()
            if completion["is_complete"]:
                self.notifier.notify("Project Milestones Completed!", completion["report"], level="INFO")

            return {
                "status": "completed",
                "task_id": task_id,
                "approved_by": reviewer,
                "project_completion": completion["is_complete"],
            }

        return {"status": "error", "message": f"Unsupported decision '{decision}'. Choose 'APPROVED' or 'CHANGES_REQUESTED'."}

    def check_project_completion(self) -> dict[str, Any]:
        """Detect when every required task in milestones M0.5–M6 is completed and generate completion evidence."""
        backlog = _load_json(self.backlog_path)
        tasks = backlog.get("tasks", [])
        m_tasks = [t for t in tasks if t.get("milestone") in ("M0.5", "M1", "M2", "M3", "M4", "M5", "M6")]

        completed = [t for t in m_tasks if t.get("status") == "completed"]
        is_complete = len(completed) == len(m_tasks) and len(m_tasks) > 0

        report = f"# Project Completion Report (Milestones M0.5–M6)\n\n"
        report += f"- Total Scope Tasks: {len(m_tasks)}\n"
        report += f"- Completed Tasks: {len(completed)}\n"
        report += f"- Overall Milestone Completion: {len(completed)}/{len(m_tasks)} ({len(completed)/len(m_tasks)*100:.1f}%)\n"

        if is_complete:
            report += "\nAll required milestones M0.5 through M6 are fully completed with passing verification suites.\n"

        return {"is_complete": is_complete, "report": report}
