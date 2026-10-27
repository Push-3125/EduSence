---
name: security-audit
description: Audits a codebase or change for authentication, authorization, input validation, secrets, injection, file handling, and dependency risks. Use when asked for a security review or before release.
---

# Security Audit

## Workflow
1. Identify entry points: routes, handlers, file uploads, background jobs, external calls.
2. Check authentication and authorization on the server side for each protected operation.
3. Check input validation and output encoding at system boundaries.
4. Search for hardcoded secrets, tokens, and unsafe config (read-only search; never print secret values).
5. Check for injection risks (SQL, command, template, prompt injection if LLMs are used).
6. Check file handling, error responses, logging of sensitive data, and rate limiting.
7. Review dependency and configuration risks visible in the repository.

## Constraints
- Read-only audit; do not modify files unless asked.
- Never run destructive or intrusive tests against live systems.
- Separate confirmed findings from hypotheses.

## Output
For each finding: severity, location, evidence, impact, recommended fix, suggested verification test.
