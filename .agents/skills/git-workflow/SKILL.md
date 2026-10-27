---
name: git-workflow
description: Guides branching, commits, pull requests, and conflict handling with Git. Use when creating branches, committing, preparing a pull request, or resolving merge conflicts.
---

# Git Workflow

## Workflow
1. Run `git status` and `git diff` before any commit; confirm only intended files changed.
2. Work on a branch named by type: `feature/<topic>`, `fix/<topic>`, `chore/<topic>`.
3. Make small commits with Conventional Commit messages (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).
4. Before a pull request: run the project's tests/lint, then write a description with summary, changes, how it was tested, and risks.
5. Conflicts: understand both sides first, keep intended behavior from both, then rerun tests.

## Constraints
- Never commit secrets, `.env`, tokens, or generated/binary clutter; check `.gitignore`.
- Never force-push, `reset --hard`, `clean -fd`, or rewrite shared history without explicit approval.
- Do not commit unrelated changes together.
- On Windows, do not reformat whole files only to change line endings.

## Output
- Branch name, commit list, and pull request description when requested
