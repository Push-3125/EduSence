---
name: code-review
description: Reviews code changes for bugs, security issues, maintainability, and regression risk. Use when reviewing code, pull requests, or diffs.
---

# Code Review

## Workflow
1. Inspect the diff and the surrounding code.
2. Understand the intended behavior and requirements.
3. Check correctness, edge cases, and error handling.
4. Check security and authorization boundaries.
5. Evaluate maintainability, duplication, and coupling.
6. Check whether tests cover the change.
7. Report findings by severity with file and line references.

## Constraints
- Review before suggesting a rewrite; do not modify files unless asked.
- Do not present speculative issues as confirmed vulnerabilities.
- Prioritize actionable findings over style preferences.

## Output
- Critical findings
- High-risk findings
- Other findings
- Missing tests
- Overall assessment
