---
name: implement-feature
description: Implements a feature in EduSense from requirement to verified change, with approval gates. Use when asked to build, add, or extend a feature in the EduSense backend or frontend.
---

# Implement Feature (EduSense)

## Workflow
1. Read the relevant files in docs/ and the existing code that the feature touches.
2. Summarize the requirement, acceptance criteria, affected components, and risks. If criteria are missing or conflicting, stop and ask.
3. Propose a small plan (see task-planning). Wait for approval for anything non-trivial.
4. Implement incrementally in the established layers: route -> service -> persistence; component -> composable/store -> API client.
5. Enforce authorization on the backend for every protected operation.
6. Add tests for success, validation, boundary, failure, and permission cases (see test-writer).
7. Run the project's verification commands from AGENTS.md and report real results.
8. Review the final diff for unrelated changes, then report using agent-handoff.

## Constraints
- Do not change the stack, public API contracts, or database schema without approval.
- Do not invent requirements; record open questions instead.
- Keep changes focused; do not refactor unrelated code.

## Done when
- Acceptance criteria are met or gaps are reported
- Tests added and actually run
- Diff reviewed and summarized
