# EduSense -- Backend Implementation Backlog (M0 through M6)

Machine-readable source of truth: `workflow/backlog.json`.
Interactive management CLI: `python workflow/workflow.py status` and `python workflow/workflow.py run-task`.

---

## Milestone Summary

| Milestone | Scope | Tasks | Status |
| :--- | :--- | :---: | :---: |
| **M0** | Backend Foundation & Infrastructure | 6 | `COMPLETED` |
| **M0.5** | Repository-Native Workflow & Verification Pipeline | 4 | `IN_PROGRESS` |
| **M1** | Authentication, RBAC & Profile Management | 7 | `PENDING` (1 Blocked) |
| **M2** | Academic Study Management Core (Courses, Assignments, Exams) | 5 | `PENDING` |
| **M3** | Document Storage & Asynchronous Ingestion Pipeline | 6 | `PENDING` |
| **M4** | Core AI Service & Document-Grounded RAG Engine | 8 | `PENDING` |
| **M5** | Advanced AI Features & Background Automations | 9 | `PENDING` |
| **M6** | Subscriptions, Payment Gateway & Administration | 6 | `PENDING` (1 Blocked) |

---

## Detailed Task Catalog

### Milestone M0: Backend Foundation & Infrastructure (`COMPLETED`)
- [x] **TASK-M0-01**: Docker Compose & Infrastructure Setup (`edusense-postgres`, `edusense-redis`, `edusense-minio`).
- [x] **TASK-M0-02**: FastAPI Core Configuration & Async Database Session (`pydantic-settings`, SQLAlchemy 2.0 `asyncpg`).
- [x] **TASK-M0-03**: Alembic Async Baseline Migration (38 Tables + `pgvector` extension `0001_initial_baseline.py`).
- [x] **TASK-M0-04**: Health & Readiness Probes with Error Envelope (`/health`, `/health/ready`).
- [x] **TASK-M0-05**: Unit Tests & Pytest Asyncio Sandbox Verification (`tests/test_health.py`).
- [x] **TASK-M0-06**: GitHub Actions Backend CI Pipeline (`.github/workflows/ci.yml`).

### Milestone M0.5: Repository-Native Workflow & Verification Pipeline (`IN_PROGRESS`)
- [x] **TASK-M0.5-01**: Machine-Readable Backlog & Decision Log Specification (`workflow/backlog.json`, `workflow/decisions.json`).
- [x] **TASK-M0.5-02**: Self-Contained Workflow Automation CLI (`workflow/workflow.py`).
- [x] **TASK-M0.5-03**: Architecture Boundary Enforcement Checker (AST import verification).
- [ ] **TASK-M0.5-04**: Root Instructions Alignment & CI Integration (`AGENTS.md`, `ci.yml`, documentation).

### Milestone M1: Authentication, RBAC & Profile Management (`PENDING`)
- [ ] **TASK-M1-01**: Password Hashing Service (Argon2id) & Security Primitives (`app/core/security.py`).
- [ ] **TASK-M1-02**: Email/Password Registration Endpoint (US 2.1) & Verification Token.
- [ ] **TASK-M1-03**: JWT Authentication, Login, Refresh Session & Revocation (US 2.2).
- [ ] **TASK-M1-04**: Password Reset Flow via Single-Use Verification Token (US 2.3).
- [x] **TASK-M1-05**: User Profile CRUD & Student Information (US 2.4).
- [ ] **TASK-M1-06**: Login Attempt Limiting & Audit Security Logging (NFR04).
- [ ] **TASK-M1-07**: `[BLOCKED]` Social Login (Google OAuth2) Integration (US 2.1) -- Blocked by `DECISION-PO-01`.

### Milestone M2: Academic Study Management Core (`PENDING`)
- [ ] **TASK-M2-01**: Courses CRUD Repository, Service & Router (US 2.5).
- [ ] **TASK-M2-02**: Assignments Management with Due Dates & Priority (US 2.6).
- [ ] **TASK-M2-03**: Exam Schedule Tracking with Location & Dates (US 2.7).
- [ ] **TASK-M2-04**: Weekly Recurring Class Schedule Management (US 2.9).
- [ ] **TASK-M2-05**: Consolidated Academic Calendar Aggregation API (US 2.8).

### Milestone M3: Document Storage & Ingestion Pipeline (`PENDING`)
- [ ] **TASK-M3-01**: MinIO Object Storage Client & Presigned URL Provider (`app/clients/storage_client.py`).
- [ ] **TASK-M3-02**: Document Metadata & File Upload Validation (US 2.10).
- [ ] **TASK-M3-03**: Celery Worker Setup & Task Idempotency Guard (Invariant 8).
- [ ] **TASK-M3-04**: Document Text Extraction (PDF/DOCX/Images) & Chunking Service.
- [ ] **TASK-M3-05**: Embedding Generation & pgvector Storage (US 2.10).
- [ ] **TASK-M3-06**: Document Search, Filtering & Tag Management API (US 2.11).

### Milestone M4: Core AI Service & Document-Grounded RAG (`PENDING`)
- [ ] **TASK-M4-01**: Central Gemini Adapter with Circuit Breaker & Retry (Invariant 4).
- [ ] **TASK-M4-02**: Atomic Quota Reservation & Redis Fail-Closed Manager (US 2.21, Invariant 7).
- [ ] **TASK-M4-03**: AI Usage Logging & Token Tracking (`app/providers/usage_logger.py`).
- [ ] **TASK-M4-04**: General AI Chatbot Conversation & Message History (US 2.12).
- [ ] **TASK-M4-05**: Permission-Scoped pgvector Vector Retrieval (Invariant 9).
- [ ] **TASK-M4-06**: Prompt Injection Sanitization & Data Block Isolation (Invariant 13).
- [ ] **TASK-M4-07**: GroundingChecker & Citation Verification Service (Invariant 6, US 2.13).
- [ ] **TASK-M4-08**: Golden Dataset RAG Evaluation & Accuracy Test Suite (Test Matrix 13.3).

### Milestone M5: Advanced AI Features & Background Automations (`PENDING`)
- [ ] **TASK-M5-01**: Document Summarization Service (Sync short / Async long) (US 2.14).
- [ ] **TASK-M5-02**: Multiple-Choice & Essay Question Bank Generation (US 2.15).
- [ ] **TASK-M5-03**: Subjective/Essay Answer Grading & Rubric Evaluation (US 2.15).
- [ ] **TASK-M5-04**: Practice Attempts & Score Tracking API (US 2.15).
- [ ] **TASK-M5-05**: AI Study & Revision Plan Generation (US 2.16).
- [ ] **TASK-M5-06**: Study Plan Item Acceptance, Dismissal & Calendar Sync.
- [ ] **TASK-M5-07**: Academic Progress Dashboard & Analytics (US 2.17, 2.18).
- [ ] **TASK-M5-08**: Celery Beat Schedulers: Deadline & Exam Email Reminders (US 2.19).
- [ ] **TASK-M5-09**: Notifications API & In-App Alerts (US 2.20, Invariant 12).

### Milestone M6: Subscriptions, Payment Gateway & Administration (`PENDING`)
- [ ] **TASK-M6-01**: `[BLOCKED]` SePay QR Payment Creation & Webhook Handler (Invariant 10) -- Blocked by `DECISION-PO-02`.
- [ ] **TASK-M6-02**: Subscription Lifecycle & Atomic Plan Transition Service.
- [ ] **TASK-M6-03**: Celery Beat Subscription Expiry Scanner & Auto-Downgrade Worker.
- [ ] **TASK-M6-04**: Admin Student Accounts Management (US 2.22).
- [ ] **TASK-M6-05**: Admin AI Usage Analytics & Quota Policy Configuration (US 2.23).
- [ ] **TASK-M6-06**: Admin System Storage & Resource Consumption Reports (US 2.24).
