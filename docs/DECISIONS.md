# EduSense -- Architecture & Product Decision Log

This document records confirmed Product Owner decisions, unresolved questions requiring explicit authorization, and proposed architecture defaults.

Source of truth: `workflow/decisions.json` and `docs/project-documents/EduSense – Source-of-Truth Summary for AI Agents (English).md`.

---

## 1. Confirmed Product Owner Decisions

| ID | Title | Status | Approved By | Decision Summary |
| :--- | :--- | :---: | :--- | :--- |
| **DECISION-CONFIRMED-01** | Authentication Strategy | `APPROVED` | Product Owner / Architecture v1.1 | Email/password primary auth with Argon2id password hashing, JWT access token (15m), refresh session (7d), and single-use verification tokens. |
| **DECISION-CONFIRMED-02** | Service Architecture & Boundaries | `APPROVED` | Product Owner / Architecture v1.1 | Service-based architecture with shared PostgreSQL database: API Backend (FastAPI), AI Service (Python), Background Worker (Celery + Redis), Scheduler (Celery Beat), MinIO Object Storage. |
| **DECISION-CONFIRMED-03** | Database Schema & Vector Search | `APPROVED` | Product Owner / Architecture v1.1 | PostgreSQL 16 with pgvector extension enabled. 38 business and operational tables from `EduSense_PostgreSQL_Schema_Revised.sql` deployed as Alembic baseline migration `0001_initial_baseline.py`. |
| **DECISION-CONFIRMED-04** | Central AI Adapter Boundary | `APPROVED` | Product Owner / Architecture v1.1 | All Google Gemini calls MUST pass through a single shared Adapter module (`providers` package). Routers, workers, and tasks must NEVER import the Gemini SDK directly. |
| **DECISION-CONFIRMED-05** | Quota Management & Fail-Closed | `APPROVED` | Product Owner / Architecture v1.1 | Atomic Redis counters for quota reservation prior to LLM call with refund on failure. Fail-closed: if Redis is unreachable, reject the AI request. |
| **DECISION-CONFIRMED-06** | Object Storage Isolation | `APPROVED` | Product Owner / Architecture v1.1 | MinIO bucket `edusense-documents` is private. Client never accesses MinIO directly; uploads and downloads handled via API Backend and presigned URLs. |
| **DECISION-CONFIRMED-07** | Frontend Framework & Typing Standards | `APPROVED` | Product Owner | Vue 3 Single File Components (`<script setup lang="ts">`), Tailwind CSS, TypeScript with strict typing. All new code must be TypeScript; no bulk JS conversion. |

---

## 2. Unresolved Questions (Human Approval Gate Required)

These decisions represent critical product scope or security boundaries. Any task dependent on these decisions is marked `BLOCKED` until explicitly approved by the Product Owner.

### DECISION-PO-01: Social Login (OAuth2) Activation
- **Question**: Should Google OAuth2 social login be implemented for M1, or remain strictly email/password until Phase 2?
- **Proposed Default**: Default to email/password only for Phase 1. Task `TASK-M1-07` is `BLOCKED`.
- **Command to Approve**:
  ```bash
  python workflow/workflow.py approve --decision-id DECISION-PO-01 --value "Approved Google OAuth2 social login for Phase 1"
  ```

### DECISION-PO-02: Pro Subscription Pricing, Duration & Billing Rules
- **Question**: What is the official price (VND) and duration for the Pro plan (e.g. 50,000 VND / 30 days), and should live SePay webhook processing be enabled in development?
- **Proposed Default**: Mock payment mode in development. Do not activate live payments or charge real accounts until PO approves pricing. Task `TASK-M6-01` is `BLOCKED`.
- **Command to Approve**:
  ```bash
  python workflow/workflow.py approve --decision-id DECISION-PO-02 --value "Approved Pro plan price: 50,000 VND/30 days and sandbox webhook mode"
  ```

### DECISION-PO-03: Cross-User Document Sharing Scope
- **Question**: Should students be allowed to share documents or question banks publicly with other students in Phase 1?
- **Proposed Default**: Strict user-scoped isolation (R-06). Documents and vector searches are strictly private to `user_id`.
- **Command to Approve**:
  ```bash
  python workflow/workflow.py approve --decision-id DECISION-PO-03 --value "Approved strictly private tenant isolation for all documents"
  ```

---

## 3. Proposed Architecture Defaults

| ID | Title | Proposed Value | Reference |
| :--- | :--- | :--- | :--- |
| **DECISION-PROP-01** | Embedding Model & Vector Dimension | `gemini-embedding-2` with 768 dimensions | Source-of-Truth Section 14, Schema DDL |
| **DECISION-PROP-02** | Document Chunking Strategy | Parent chunk 1200-1500 tokens, child chunk 400-500 tokens, overlap 60-75 tokens | Source-of-Truth Section 14 |
| **DECISION-PROP-03** | File Upload Caps | 20 MB per file, max 100 pages for PDF, max 10 files per upload request | Source-of-Truth Section 14 |
| **DECISION-PROP-04** | Default AI Daily Quota Limits | Free plan = 10 AI requests/day; Pro plan = 150 requests/day; warning at 80% usage | Source-of-Truth Section 0.1 Invariant 7 |
