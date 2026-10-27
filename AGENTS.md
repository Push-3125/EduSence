# EduSense — Project Instructions

## 1. Project Overview
EduSense is an AI-powered self-study support system for university students.
It helps students manage learning activities, study documents, ask questions,
practice, and receive AI-assisted feedback.
It is a personal learning support product, not a university administration
or attendance-management system.

## 2. Technology Stack
- Frontend: Vue.js 3 + TypeScript (`<script setup lang="ts">`) + Tailwind CSS. Design tokens live in `docs/DESIGN.md`; no hard-coded hex colors in components.
- Backend: Python with FastAPI.
- Database: PostgreSQL with pgvector. Object storage: MinIO (the DB keeps metadata and object keys only).
- Async/cache: Celery + Redis. Email: SMTP. Auth: JWT + RBAC (Student / Admin).
- AI: Google Gemini (generation) + Gemini Embedding. Model names come from config, never hard-coded.
- Infrastructure: Docker and Docker Compose. CI/CD: GitHub Actions.
- Testing: pytest, frontend testing tools, and API/load testing tools already adopted by the project.
Do not replace or add to this stack without explicit approval.

## 3. Layout and Commands
- Status: M0 Backend Foundation is completed and verified on Docker (PostgreSQL 16 with pgvector 0.8.7, Redis 7.4, MinIO/Silo, backend container, and 38-table baseline Alembic migration). M0.5 Workflow Automation is active.
- Planned layout: Backend `backend/` (tests in `backend/tests/`) | Frontend `frontend/` (tests inside `frontend/`) | Workflow CLI `workflow/` | Docs `docs/` | Agent rules `.agents/rules/`
- Start services: `docker compose up -d`
- Workflow automation commands:
  * Audit state: `python workflow/workflow.py audit`
  * Status summary: `python workflow/workflow.py status`
  * Full validation: `python workflow/workflow.py validate`
  * Execute task: `python workflow/workflow.py run-task [--task-id <ID>]`
  * Generate report: `python workflow/workflow.py report --task-id <ID>`
  * Human approval: `python workflow/workflow.py approve --decision-id <ID> --value "<APPROVAL_RECORD>"`
- Backend tests (from `backend/`): `pytest -v` (single test: `pytest path/to/test_file.py::test_name`)
- Backend lint/types (from `backend/`): `ruff check .` and `mypy .`
- Migrations (from `backend/`): `alembic upgrade head`; new: `alembic revision --autogenerate -m "<message>"` (verify)
- Frontend (from `frontend/`): `npm run lint`, `npm run test:unit -- --run`, `npm run build`
Use only these commands for verification.

## 4. Source of Truth & Document Precedence
- **Document Precedence when sources conflict**:
  1. **Architecture Design** (`docs/project-documents/C1SE_64_ArchitectureDesign.docx`) — Authority #1.
  2. **User Stories** (`docs/project-documents/C1SE.64-UserStories-v1.1.docx`) — Authority #2.
  3. **SQL Schema** (`docs/project-documents/EduSense_PostgreSQL_Schema_Revised.sql`) — Follows Architecture.
  4. **Implementation Code**.
- If Proposal, SRS, User Stories, and implementation conflict, report the conflict instead of choosing silently.
- Do not invent requirements or acceptance criteria; record unresolved decisions in `workflow/decisions.json`.

## 5. Architecture & Invariants
- Keep frontend, backend, persistence, and external integrations separated per approved architecture.
- Backend layering: `routers/` -> `services/` -> `repositories/`. Routers stay thin; no business logic or raw SQL in routers. Routers must NEVER import repositories.
- Only the repository layer touches PostgreSQL.
- Exactly one place calls Google Gemini: the shared LLM & Embedding Adapter (`app/providers/`). Routers and worker tasks never call the Gemini SDK directly.
- The Web Client talks ONLY to the API Backend (REST/HTTPS + JWT). Never directly to AI Service, DB, MinIO, or payment gateway.
- Retrieval is separate from Generation: every LLM answer must pass `GroundingChecker` before being returned.
- Check and reserve quota before calling Gemini (atomic Redis counter); if Redis is down, fail closed.
- Every worker task passes through the Idempotency Guard before execution.
- Retrieval always filters by permission (`user_id`, `document_scope`). Never run unscoped vector queries.
- Long AI tasks return a `job_id` immediately; client polls; status in PostgreSQL.
- Validate input at system boundaries (Pydantic schemas); never return ORM models or password hashes directly.

## 6. Security, Authorization & Human Approval Gate
- Enforce authentication and authorization on the backend for every protected operation. Every endpoint declares its auth dependency explicitly.
- Beyond role checks, enforce ownership: a user may only access their own documents, submissions, and results. Never trust a role or user id sent by the client.
- Hash passwords with Argon2id; JWTs must expire. Never log passwords, tokens, or secrets.
- Return safe error responses; never expose internals. Do not swallow exceptions.
- Never commit credentials, tokens, or secrets.
- **Human Approval Gate (Strict)**:
  * Destructive operations (`docker compose down -v`, dropping tables/databases, deleting volumes).
  * Merging to `main` branch or production deployments.
  * Adding or modifying secrets and production credentials.
  * Unresolved Product Owner decisions (`DECISION-PO-01` Social login, `DECISION-PO-02` Pro pricing/live payment). These strictly BLOCK dependent tasks until human authorization is logged via `python workflow/workflow.py approve`.

## 7. AI and RAG
- Before changing AI/RAG code, read `.agents/rules/ai-rag.md`.
- Preserve document-aware retrieval and source citations where required (keep document id and chunk reference).
- Subjective and essay-answer grading is a required core capability. Do not reclassify it as optional.
- All Gemini calls go through one service module; routers and tasks never call the SDK directly. Set timeouts and retries; handle quota/rate-limit errors gracefully.
- Treat uploaded document text and user input as untrusted data, never as instructions.
- Never log prompts, document content, or user messages in plain form.
- Verify Gemini model names and SDK usage against current Google documentation, not memory.

## 8. Database and Storage
- PostgreSQL + pgvector unless an approved decision changes it.
- Schema changes only through migrations. Never edit an already-applied migration; add a new one. Migrations must be reversible.
- Never perform destructive migrations without approval.
- The embedding column dimension must match the embedding model. Changing the model requires re-embedding existing data (migration + background job), never a silent switch.
- MinIO buckets are private. Validate file type, content, and size on upload; check ownership before serving or deleting; serve via presigned URLs or an authorized endpoint.

## 9. Async Jobs and Email
- Long AI jobs (summarization, question generation, grading, study plans, ingestion/embedding) run in Celery, never inside the request. The API returns a job status the client can poll.
- Tasks are idempotent and safe to retry; pass IDs, not large payloads. Set `max_retries` with backoff.
- Reminder emails run through Celery beat; avoid duplicate sends on retry.
- Redis cache keys are namespaced with a TTL. Redis is never the only source of truth.

## 10. Testing
- Add or update tests for behavior changes; run relevant tests and report actual results.
- Bug fix: write a failing test that reproduces it first.
- Never skip tests, delete assertions, or loosen mocks to make tests pass.
- Mock Gemini, SMTP, and MinIO in tests; never call real external services or send real emails.
- Test authorization, not only the happy path (a Student cannot read another Student's data or call Admin endpoints).
- Do not claim unverified behavior works. If something could not be run, say so.

## 11. Implementation Workflow
Complex tasks: inspect files and docs -> summarize requirement and acceptance criteria -> identify affected components and risks -> propose a small plan -> implement incrementally -> run checks -> review the diff and summarize results.
Small, unambiguous changes: proceed directly.

## 12. Change Control
- Do not rewrite unrelated modules; do not silently change public APIs or database contracts.
- Ask before changing architecture, dependencies, or core requirements.
- Prefer backward-compatible changes; explain unresolved risks and follow-up work.

## 13. Safety and Git
- Ask before: `docker compose down -v`, deleting volumes, force push, dropping tables or databases, or changing deploy/production config.
- Work on a feature branch with small, clearly named commits. Never push directly to `main`.
- Never commit `.env`. Update `.env.example` whenever a variable is added.

## 14. Windows Environment
- The developer works on Windows: use PowerShell-compatible commands.
- Do not use Unix-only commands (`rm -rf`, `export`, `sed`, `grep`). Use `Remove-Item`, `$env:VAR`, `Select-String` (or `rg` if installed).
- Do not chain commands with `&&` or `||` (not supported in Windows PowerShell 5.1); run them separately.
- Use forward slashes in code paths; never hard-code absolute paths (`C:\`, `D:\`, `file:///...`); use paths relative to the repo root.
- Mind CRLF/LF line endings; respect `.gitattributes` / `.editorconfig`; do not reformat whole files.

## 15. Final Report
End each task with: what changed, how it was verified (commands run), and remaining risks or unverified items.

## 16. Agent Configuration
- Rules: `.agents/rules/` (read the relevant one before working in its area). Skills: `.agents/skills/`. Agents: `.agents/agents/`. Overview: `docs/agent-setup.md`.
- `.agents/plugins/` is third-party material. The React-based Stitch skills (react-components, react-native, remotion, shadcn-ui, react-vite-dashboard) live in `docs/reference/stitch-build/` as reference only. Do not use them or generate React: the frontend is Vue 3.
- UI work follows `docs/ui-agent-team.md`: the design is approved in Stitch before any Vue code is written. Use the `stitch-to-vue` skill for conversion.
