---
name: agent-handoff
description: Defines the report format for finishing a task or handing work to another agent or session. Use when completing a subtask, delegating to a subagent, or ending a long session.
---

# Agent Handoff

## When delegating
Give the subagent a self-contained brief, because it does not see this conversation:
goal, relevant file paths, constraints (read-only or allowed edits), expected output format, and what it must not do.

## When reporting back
1. Status: done, partial, or blocked.
2. Summary of what was done, in a few sentences.
3. Files changed (path and one-line reason).
4. Commands run with real results (passed, failed, not run).
5. Open issues, assumptions made, and decisions that need the user.
6. Suggested next step.

## Constraints
- Report only what actually happened; mark unverified items as unverified.
- Do not hide failures or skipped checks.
