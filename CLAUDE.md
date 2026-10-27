@AGENTS.md

# Claude Code notes

Project instructions are in `AGENTS.md` (imported above). Claude Code does not read `.agents/` automatically,
so use the pointers below.

## Behavior
- Think before coding: state assumptions, surface alternatives, and ask when the requirement is unclear instead of choosing silently.
- Keep it simple: the minimum code that solves the problem, with no speculative features or abstractions.
- Make surgical changes: every changed line must trace back to the request; mention unrelated issues instead of fixing them.
- Work goal-driven: turn each task into checks you can run (test first for bugs), and verify before reporting.

## Rules to read before working in an area (`.agents/rules/`)
- AI / RAG / grading: `ai-rag.md`
- Architecture, new modules, dependencies: `architecture.md`
- Backend (`backend/**/*.py`): `backend.md`
- Database, migrations, vector search: `database.md`
- Frontend / UI (`frontend/src/**`): `frontend-ui.md`
- Auth, roles, uploads, secrets, prompts: `security.md`
- Tests: `testing.md`

## Workflows (`.agents/skills/<name>/SKILL.md`, read the file when the task matches)
- Feature work: `implement-feature` (with `task-planning`, `test-writer`, `agent-handoff`)
- Requirements and planning: `requirements-analysis`, `task-planning`, `test-plan`
- Quality: `code-review`, `security-audit`, `debugging`
- Design: `api-design`, `database-design`, `frontend-design`, `stitch-to-vue`
- Git: `git-workflow`

## Do not use
- `docs/reference/stitch-build/*` (React-based reference only). The frontend is Vue 3.

## Personal overrides
Put machine-specific preferences in `CLAUDE.local.md` (git-ignored), not in this file.
