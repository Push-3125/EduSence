---
name: task-planning
description: Breaks a requirement or feature into small ordered tasks with dependencies, affected files, risks, and a definition of done. Use before implementing any non-trivial change.
---

# Task Planning

## Workflow
1. Restate the goal and the acceptance criteria in one short paragraph. If criteria are missing, say so.
2. Inspect the code and docs to identify affected modules and files.
3. Split the work into small tasks, each independently verifiable (ideally one commit each).
4. Order tasks by dependency; flag tasks that can run in parallel.
5. For each task give: goal, files likely touched, how it will be verified, and its risk.
6. List risks, unknowns, and decisions that need the user's approval.

## Constraints
- Present the plan and wait for approval before implementing, unless the change is small and unambiguous.
- Do not include tasks outside the requested scope.

## Output
- Numbered task list with dependencies
- Verification method per task
- Risks and open decisions
