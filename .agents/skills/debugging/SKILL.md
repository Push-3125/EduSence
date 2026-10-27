---
name: debugging
description: Finds the root cause of a bug through reproduction, isolation, and evidence before fixing it. Use when something fails, a test breaks, or behavior differs from expectation.
---

# Debugging

## Workflow
1. Reproduce the problem and record the exact command, input, and error.
2. Read the full error and stack trace; identify the first failing frame in project code.
3. Form hypotheses and test each with evidence (logs, a minimal script, a focused test), not guesses.
4. Identify the root cause and explain why it produces the observed symptom.
5. Apply the smallest fix that addresses the root cause.
6. Add or update a regression test that fails before the fix and passes after.
7. Rerun the relevant tests and report real results.

## Constraints
- Do not patch symptoms, silence errors, or weaken tests to make them pass.
- Do not claim a bug is fixed unless it was reproduced first and verified afterward.
- Say clearly when the cause is still a hypothesis.

## Output
- Reproduction steps, root cause, fix, regression test, verification results, remaining risks
