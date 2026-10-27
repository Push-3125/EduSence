# EduSense Autonomous Task Orchestrator Documentation

The **EduSense Autonomous Task Orchestrator** is a deterministic, evidence-driven automation engine designed to coordinate milestone execution (M0.5 through M6) with strict human-in-the-loop approval gates, architecture boundary enforcement, and safe recovery mechanisms.

---

## 1. Core Architecture & Finite State Machine

The orchestrator operates as a deterministic finite state machine (FSM). It executes exactly one task per invocation within its declared scope (`affected_files`) and persists every state transition atomically in `workflow/orchestrator_state.json`.

```
 +---------+         (start)          +-----------+
 | BLOCKED | <----------------------> |   READY   |
 +---------+   (missing dep/PO dec)   +-----------+
                                            |
                                            | [START_TASK]
                                            v
                                      +-----------+   (fail < max_retries)
                                      |  RUNNING  | -----------------------+
                                      +-----------+                        |
                                       |         |                         |
               [TASK_SUCCEEDED]        |         | [TASK_FAILED_MAX]       |
                                       v         v                         |
                                  +--------+  +--------+                   |
                                  | REVIEW |  | FAILED |                   |
                                  +--------+  +--------+                   |
                                    |    ^                                 |
           [REQUEST_CHANGES]        |    |                                 |
                   +----------------+    | [RESUME]                        |
                   v                     |                                 |
         +-------------------+           |                                 |
         | CHANGES_REQUESTED | ----------+                                 |
         +-------------------+                                             |
                   |                                                       |
                   | [APPROVE_TASK] (PO Auth + Passing CI)                 |
                   v                                                       v
             +----------+                                            +-----------+
             | APPROVED | -----------------------------------------> |   READY   |
             +----------+             [FINALIZE]                     +-----------+
                   |
                   v
               +------+
               | DONE |
               +------+
```

### State Descriptions:
- **`READY`**: Task dependencies are satisfied and no unresolved Product Owner decisions block execution.
- **`RUNNING`**: Task is being executed by the adapter within approved scope. Attempt count is tracked.
- **`REVIEW`**: Implementation succeeded, all validation checks passed (`ruff`, `mypy`, `pytest`, architecture boundaries), branch & PR / review artifact prepared. Awaiting human decision.
- **`CHANGES_REQUESTED`**: Reviewer requested revisions. Stores reviewer notes and resumes execution with feedback context.
- **`BLOCKED`**: Execution halted due to unresolved decisions (`unresolved_decisions`), missing credentials, or dependencies.
- **`APPROVED`**: Explicit approval verified from an authorized Product Owner and CI validation checks passed.
- **`DONE`**: Task marked completed in `workflow/backlog.json`, audit trail committed, next eligible task scheduled.
- **`FAILED`**: Retries exhausted (`attempt_number >= max_retries`, default 3) or fatal invariant violation. Stops safely.

---

## 2. CLI Usage & Commands

All operations are executed via `workflow/workflow.py`:

### Inspecting State & Progress
```powershell
# View overall milestone backlog and blocked tasks
python workflow/workflow.py status

# Inspect orchestrator state machine snapshot, active task, and audit trail
python workflow/workflow.py orchestrator-status

# Audit repository git state, Docker infrastructure, and health endpoints
python workflow/workflow.py audit
```

### Executing Tasks
```powershell
# Run next eligible task or specific task
python workflow/workflow.py orchestrate [--task-id TASK_ID]

# Run harmless dummy task end-to-end (simulation mode, leaves production tasks untouched)
python workflow/workflow.py orchestrate --dummy

# Resume task currently in progress or after changes requested
python workflow/workflow.py orchestrate --resume [--feedback "Fix notes"]

# Audit runner state and inspect restart recovery action
python workflow/workflow.py orchestrate --recover
```

### Review & Human Approval Flow
```powershell
# Approve a task awaiting review (Requires authorized PO username and passing CI)
python workflow/workflow.py review --task-id <TASK_ID> --decision APPROVED --reviewer <PO_USERNAME>

# Request revisions with feedback context
python workflow/workflow.py review --task-id <TASK_ID> --decision CHANGES_REQUESTED --reviewer <PO_USERNAME> --feedback "Fix input validation"
```

### Unblocking Product Owner Decisions
```powershell
# Authorize an unresolved decision in workflow/decisions.json and unblock dependent tasks
python workflow/workflow.py approve --decision-id DECISION-PO-01 --value "Approved specification"
```

### Validation Pipeline
```powershell
# Run the 4-step validation pipeline (Architecture AST boundaries, Ruff, Mypy, Pytest)
python workflow/workflow.py validate
```

---

## 3. Configuration & Prerequisites

### 3.1 GitHub Integration (`GITHUB_TOKEN`)
- Fine-grained Personal Access Token or default GitHub Actions token.
- Permissions: `contents: write`, `pull_requests: write`, `checks: read`.
- If `GITHUB_TOKEN` is not set in `.env`, the orchestrator generates a local review artifact in `workflow/reviews/PR_<TASK_ID>.md` and validates CI locally.

### 3.2 Product Owner Access Control
- Authorized approvers are managed in `workflow/decisions.json` and via the `AUTHORIZED_APPROVERS` environment variable.
- Default authorized reviewers: `Push-3125`, `Product Owner`.
- Review attempts by unauthorized accounts are strictly rejected with an explicit permission error.

### 3.3 Google Antigravity Integration
The orchestrator supports three adapter modes:
1. **Python SDK (`AntigravitySDKAdapter`):** Requires `pip install google-antigravity` and `GEMINI_API_KEY` in `.env`.
2. **CLI (`AntigravityCLIAdapter`):** Requires the headless `agy` CLI binary on the system PATH.
3. **Simulated (`SimulatedAdapter`):** Used with `--dummy` for testing workflows without requiring external APIs or faking results.
4. **Fallback (`InteractiveFallbackAdapter`):** If neither SDK nor CLI is installed, reports the exact installation steps required. Never fakes execution results.

### 3.4 Notifications
- **Console / Standard Output:** Always active, logs structured status and transition alerts.
- **Webhook Channel (Slack / Discord):** Configure `NOTIFICATION_WEBHOOK_URL` in `.env`.
- All outputs pass through a secret sanitization filter (`redact_secrets`) that scrubs API keys, tokens, and passwords.

---

## 4. Failure Handling & Crash Recovery

### Retry Limits
- Each task allows up to 3 attempts (`max_retries = 3`).
- Transient failures transition `RUNNING` → `READY` (incrementing attempt count).
- When attempts exceed `max_retries`, the task transitions to `FAILED` and halts orchestration.

### Crash Recovery
- State is saved atomically to `workflow/orchestrator_state.json` on every transition using atomic file replacement (`tempfile` + `replace`).
- If the runner crashes or restarts while `RUNNING`, running `python workflow/workflow.py orchestrate --recover` detects the interrupted task, inspects the working tree, and guides the user to resume safely.
