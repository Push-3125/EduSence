# EduSense – Source-of-Truth Summary for AI Agents

_Consolidated from three documents: Architecture Design v1.1 (Sept 30, 2026), User Stories v1.1, and EduSense_PostgreSQL_Schema.sql. Project: EduSense – AI-Powered Learning Support System for Students (Capstone 1, team C1SE.64, Duy Tan University)._

## 0. How to use this document (read first)

- **Precedence when sources conflict:** (1) Architecture Design → (2) User Stories → (3) SQL schema. The architecture is the authority. The current SQL schema deviates from it in several places (see Section 12); the schema must be fixed to match the architecture, never the other way around.
- Do not change container/module boundaries or add shortcut call paths on your own. If a task seems to require breaking a rule in 0.1, stop and ask the user instead of deciding yourself.
- Text marked “(inferred)” was deduced by the summarizer and is not stated verbatim in the source documents. “(unspecified)” means the source documents do not say; ask before coding.

### 0.1 Hard rules (invariants) – violating any of them is a design error

1. **The Web Client talks only to the API Backend** (REST/HTTPS + JWT). Never directly to the AI Service, DB, MinIO, or SePay.
2. **The API Backend is the single entry point.** Mandatory layering: Routers → Services → Repositories. Routers must not call Repositories. Only the Repository layer touches PostgreSQL. External communication goes through Clients (AI, Storage, Queue, Payment).
3. **The AI Service accepts only internal calls from the API Backend**, carrying an internal service token, `user_id`, `plan`, and `document_scope`.
4. **Exactly one place calls Gemini:** the LLM & Embedding Adapter (the `providers` package, shared by the AI Service and the Background Worker). No other module may import the Gemini SDK. The Adapter applies timeout/retry/circuit breaker and invokes the Usage Logger.
5. **API Backend / AI Service ↔ Background Worker communicate only through the queue** (Celery + Redis). No direct synchronous calls.
6. **Retrieval is separate from Generation.** Every LLM answer must pass `GroundingChecker` before being returned. With insufficient grounding, return “no relevant information found”; never infer.
7. **Check and reserve quota before calling Gemini** (atomic Redis counter increment; refund if the call fails). If Redis is unreachable the Quota Manager is **fail-closed** (treat quota as exhausted and reject the request).
8. **Every Worker task passes through the Idempotency Guard before its handler runs.**
9. **Retrieval always filters by permission** (user / `document_scope`). Never run an unscoped vector query.
10. **SePay webhook** is handled synchronously inside the API Backend: verify the signature and deduplicate using a UNIQUE `sepay_transaction_id`. Do not reuse the Worker's Idempotency Guard for this flow.
11. **The Quota Manager must not write to `notifications`.** It returns a `quota_warning` flag (at 80% usage); the API Backend creates the notification.
12. **The Notification Module only reads/displays notifications and marks them read.** It never generates them. Writers of `notifications`: Worker (deadlines/exams, email delivery results), Progress Module (study plan falling behind), API Backend (`quota_warning`).
13. **The LLM has no tool/action permissions.** Retrieved chunks are sanitized and placed in a separate data block with an instruction not to follow commands inside it (prompt-injection defense).
14. **Long AI tasks** (long summarization, question-bank generation, study planning): the API returns a `job_id` immediately; the client polls; status and result are stored in PostgreSQL.
15. **Document processing status has exactly 4 values:** `pending`, `processing`, `embedded`, `failed`. Set `embedded` only when `chunks_processed = total_chunks`.
16. **Shared packages:** `core` (Schemas/DTO, Config) is used by all three services; `providers` is used by the AI Service and the Worker. CI must check the boundaries of `providers/` and `clients/` (R-04).

## 1. Project overview

- **Problem:** Vietnamese university students juggle many disconnected tools (courses, assignments, exam schedules, notes, documents). General chatbots (ChatGPT, StudyX, Notion AI) are not connected to the student's own study materials.
- **Solution:** one platform combining study management with an AI assistant **grounded in the student's own documents (RAG)**, powered by Google Gemini.
- **Two workloads with different characteristics:** study management (must be fast and stable) and AI/RAG (high latency, variable per-token cost, third-party dependency). Hence not a plain CRUD architecture.
- **Actors:** Student, System Administrator (two-role RBAC). External systems: Google Gemini API, Email Service (SMTP), SePay (QR payment gateway), Object Storage (MinIO).
- **Phase 1 (MVP) scope:** accounts, courses, assignments, exam schedules, document upload, basic AI/RAG chatbot, basic AI usage quota.
- **Phase 2 scope:** document summarization, question-bank generation, AI study plan, reminders, AI cost control, Pro subscription, SePay payment, automatic downgrade on expiry.
- **Business goals:** predictable/controllable AI cost; study-management features stay stable even under heavy AI load; independent scaling of AI/document processing; protection of personal data (deletion on request, limited data sent to third-party AI).

## 2. Constraints and fixed technology

| Layer             | Technology                                                            |
| ----------------- | --------------------------------------------------------------------- |
| Frontend          | Vue.js (SPA)                                                          |
| Backend API       | Python, FastAPI, REST, Uvicorn                                        |
| AI Service        | Python (Uvicorn), Gemini 3.8 Flash (LLM) + Gemini Embedding           |
| Database          | PostgreSQL + pgvector (business data and vector embeddings in one DB) |
| Object Storage    | MinIO (S3 API): PDF, DOCX, images                                     |
| Queue / Scheduler | Celery + Redis (Celery Beat is the Scheduler)                         |
| Cache / Quota     | Redis                                                                 |
| Auth              | JWT (access + refresh) + RBAC                                         |
| Email             | SMTP                                                                  |
| Payment           | SePay API (QR / bank transfer)                                        |
| Deployment        | Docker, Docker Compose (no Kubernetes assumed)                        |
| CI/CD             | Git/GitHub, GitHub Actions                                            |
| Observability     | Sentry                                                                |

- The architecture is **service-based with a shared database** (three main services plus shared libraries), **not** full microservices.
- Initial target load: about 50–100 concurrent users. Not optimized for large scale, but the design must not block future scaling.
- The Gemini dependency must be isolated behind the Adapter so the provider can be replaced with minimal impact.
- The source mentions “Celery or RQ” in Section 4.1, but every diagram and other section uses **Celery** → use Celery.

## 3. Container-level architecture (C4 Level 2)

| Container             | Technology              | Responsibility                                                                                                             |
| --------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Web Client            | Vue.js                  | Student and Admin UI; no business logic. Pages: Student Pages, AI Assistant & Subscription Pages, Admin Dashboard          |
| API Backend           | FastAPI                 | Auth, RBAC, study-management business logic, subscription/payment, orchestration of AI Service calls and queue tasks       |
| AI Service            | Python                  | Chatbot, RAG Q&A, (short) summarization, question generation, study planning; quota; retrieval; prompt building; LLM calls |
| Background Worker     | Celery worker           | Text extraction + chunking, embedding generation, long-document summarization, email sending, subscription downgrade       |
| Scheduler             | Celery Beat             | Three scanners: Deadline, Subscription Expiry, Stuck-Task Requeue                                                          |
| Redis                 | Redis                   | Message broker, cache, AI quota counters, idempotency state                                                                |
| PostgreSQL + pgvector | PostgreSQL              | Business data + vector embeddings                                                                                          |
| MinIO                 | MinIO                   | Original document files                                                                                                    |
| External              | Gemini API, SMTP, SePay | See access matrix below                                                                                                    |

### 3.1 Access matrix – who may touch what

| Resource   | Who may access it                                                                                                                                   | Protocol                       |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| PostgreSQL | API Backend, AI Service, Worker, Scheduler (direct read/write)                                                                                      | SQL (5432)                     |
| Redis      | API Backend (push tasks, cache), AI Service (quota, cache, enqueue long summarization), Scheduler (push tasks), Worker (consume tasks, idempotency) | Redis/Celery (6379)            |
| MinIO      | **Only** API Backend (writes uploads) and Worker (reads files to process)                                                                           | S3 API                         |
| Gemini API | **Only** AI Service and Worker, through the same Adapter                                                                                            | HTTPS/REST                     |
| SMTP email | **Only** the Worker                                                                                                                                 | SMTP                           |
| SePay      | **Only** the API Backend (create payment request + receive webhook)                                                                                 | HTTPS/REST, webhook over HTTPS |

### 3.2 Communication channels

- Web Client → API Backend: REST/HTTPS with JWT (access token 15 min, refresh token 7 days).
- API Backend → AI Service: internal REST with internal service token + `user_id` + `plan` + `document_scope`.
- Worker / AI Service → Gemini: through the shared Adapter.
- Long AI tasks: API Backend returns `job_id`; Web Client polls the job status.
- All components → data: through the repository layer; the presentation layer never touches the DB.

## 4. Per-container details (C4 Level 3 + Module View)

### 4.1 API Backend (FastAPI)

**Code structure (Level 4):** `routers/` · `services/` · `repositories/` · `clients/` · `schemas/` (DTOs) · `core/` (config, JWT/RBAC security).

- **Routers (8 groups):** Auth, Schedules, Documents, Progress, Notifications, Admin, Subscriptions, AI/Chat. They validate input and call only Services.
- **Services:** Auth, Schedule, Document, Progress, Notification, Admin, Subscription, Chat.
- **Repositories:** User, Schedule, Document, Notification, Subscription (shared CRUD).
- **Clients:** AI Client (internal REST), Storage Client (MinIO/S3), Queue Client (Celery/Redis), Payment Client (SePay).

| Component                   | Responsibility                                                                                                                                                                                                                                                                                                                                   |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Auth Module                 | Registration, login, password recovery via emailed verification code, profile update, JWT creation/decoding, RBAC checks (student/admin). Blocks requests before they reach business modules                                                                                                                                                     |
| Schedule Management         | CRUD for Course, Assignment, Exam, Class Schedule (FR02, FR14). Stores AI-suggested study plans; students can edit, accept, or dismiss them (FR08)                                                                                                                                                                                               |
| Document Module             | Upload and metadata management (FR03), search/filter (FR13). Stores files via the Storage Client and triggers async processing via the Queue Client. Stores AI-generated questions in the student's personal question bank (FR07) and serves them for revision/practice, reading through the Repository layer **without calling the AI Service** |
| Progress Module             | Aggregates study progress (FR15) from Assignment/Exam data and question-practice results. **Never calls AI**, only computes. When progress falls behind it writes a record to `notifications`                                                                                                                                                    |
| Notification Module         | Reads the notification list and marks notifications read (FR12). Does not generate notifications                                                                                                                                                                                                                                                 |
| Administration & Monitoring | Account/role management, AI quota and plan configuration, service status, task monitoring and retry of failed tasks, usage statistics, subscription/payment reports (FR16)                                                                                                                                                                       |
| Subscription Module         | Plan purchase and status, receives and verifies SePay webhooks, updates `subscriptions` and `payment_transactions` on successful payment                                                                                                                                                                                                         |
| AI Request Client           | The only layer that calls the AI Service. Business modules must not call the AI Service directly                                                                                                                                                                                                                                                 |
| Payment Client              | Calls SePay to create a payment request and obtain the QR code                                                                                                                                                                                                                                                                                   |

- The Chat Service forwards all AI requests (chat, RAG, summarization, content generation, study planning) to the AI Service and returns the response to the Web Client.
- When an AI Service response carries the `quota_warning` flag, the API Backend creates an “AI quota about to be reached” notification.

### 4.2 AI Service

**Code structure:** `api/` · `quota/` · `retrieval/` · `generation/` · `providers/` (shared) · `core/`.

| Component                           | Responsibility                                                                                                                                                                                                   |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Internal API Layer (FastAPI router) | Entry point. Verifies the service token and enforces the document access scope (`document_scope`)                                                                                                                |
| Quota Manager                       | Rejects requests when quota is exceeded; reserves quota before the Gemini call and refunds it on failure; applies per-plan limits (Free/Pro) received from the API Backend; returns `quota_warning` at 80% usage |
| Request Router                      | Routes by task type: chat/RAG, summarization, question generation, study planning                                                                                                                                |
| Retrieval Module                    | Embeds the question and queries top-K chunks from pgvector, **only within documents the user may access**                                                                                                        |
| Prompt Builder                      | Builds the prompt from question + retrieved context + conversation history. Sanitizes chunks and places them in a separate data block                                                                            |
| Generation Module                   | Sends the prompt via the LLM Adapter and receives text; **must** run the grounding check before returning                                                                                                        |
| LLM & Embedding Adapter             | The single abstraction that calls Gemini; timeout/retry/circuit breaker; shared with the Worker                                                                                                                  |
| Usage Logger                        | Records tokens/cost of every Gemini call in the DB. Called by the Adapter, so Worker calls are logged too                                                                                                        |

- **Short** summarization is handled synchronously by the AI Service. Documents **above a threshold**: the AI Service checks quota, then enqueues the work for the Worker (the exact threshold is unspecified).
- Retrieval and Generation exchange results only; neither interferes with the other's internals.

### 4.3 Background Worker (Celery)

**Code structure:** `dispatcher/` · `idempotency/` · `tasks/` · `providers/` (shared) · `core/`.

- **Task Dispatcher:** receives tasks from Redis, reads the type (`document_index`, `summarize_long`, `send_email`, `downgrade_subscription`), and routes to the handler.
- **Idempotency Guard:** tracks each `idempotency_key` with three states: `in_progress`, `done`, `failed`. A task is skipped only when `done`; `done` is set only after the handler succeeds, so a Celery retry after failure can run again. A task stuck in `in_progress` beyond a time threshold may be re-run. For subscription downgrade, the state is kept in **PostgreSQL**, not only Redis. It runs **before** every handler.
- **Document Processing Task:** reads the file from MinIO, extracts text, splits into chunks, computes a fixed identifier per chunk (document_id + sequence number), UPSERTs into the vector DB, and sets status `embedded` only after all chunks are written.
- **Summarization Task:** summarizes long documents asynchronously and writes results to PostgreSQL.
- **Email Sender Task:** sends account-verification, password-recovery, and reminder emails via SMTP; checks the Idempotency Guard to avoid duplicates; records the result (success/failure) in `notifications`.
- **Subscription Downgrade Task:** triggered by the Subscription Expiry Scanner; checks idempotency to avoid downgrading twice; sets the plan to Free in PostgreSQL.
- **Provider Adapter:** reuses the exact same class as the AI Service's LLM & Embedding Adapter; no separate Gemini-calling logic in the Worker.

### 4.4 Scheduler (Celery Beat)

- Runs only on its own timer and never calls the Worker directly. It pushes tasks into Redis; the Worker is the only consumer.
- **Deadline Scanner:** scans upcoming assignment/exam deadlines and enqueues reminder tasks.
- **Subscription Expiry Scanner:** scans expired subscriptions and enqueues downgrade tasks.
- **Stuck-Task Requeue Scanner:** re-enqueues tasks stuck in `processing` beyond a threshold (R-01).

### 4.5 Web Client (Vue.js)

- **Student Pages:** authentication, schedule, documents, progress.
- **AI Assistant & Subscription Pages:** chat, study planning, plan purchase; displays the SePay QR code and subscription status returned by the API Backend.
- **Admin Dashboard:** accounts, quota, monitoring.
- All requests go only to the API Backend.

## 5. Main processing flows

### 5.1 Document-based Q&A (RAG) – the main flow (Figure 13)

1. The student sends a question from the Web Client → API Backend (with JWT).
2. The API Backend authenticates, determines the `document_scope` the student may access, and forwards the request to the AI Service via the AI Client (with service token, `user_id`, `plan`).
3. The AI Service Quota Manager checks and **reserves quota**: the Redis counter is incremented atomically **before** the Gemini call.
   - Quota exceeded → return an error immediately (“usage limit reached”), **stop the flow**, no Gemini call.
   - Quota available → continue.
4. The Request Router sends the request to the Retrieval Module: embed the question (via the Adapter) and query **top-K = 5** chunks in pgvector, only within the permitted scope.
5. The Prompt Builder sanitizes the chunks and builds the prompt (question + context + history). The Generation Module calls Gemini through the Adapter.
6. `GroundingChecker` compares the answer with the retrieved chunks:
   - Grounded → answer with source **citations**.
   - Not grounded → answer “no relevant information found”.
7. Token/cost usage is logged to PostgreSQL (for reporting). **The Redis counter is the source of truth for the daily quota**; a failed request is refunded.
8. The result returns to the Web Client. The response may carry `quota_warning` → the API Backend creates a notification.

### 5.2 Document ingestion – the highest-risk area (R-01)

1. The student uploads → the API Backend **validates the file** (type, size, page limit configured by Admin, malware check).
2. The API Backend stores the original in MinIO (Storage Client), creates a document record with status `pending`, and enqueues a `document_index` task (Queue Client).
3. Worker: Task Dispatcher → Idempotency Guard → DocumentProcessingTask.
4. **Claim with an atomic UPDATE** `... WHERE status = 'pending'` → `processing`. A worker whose UPDATE changes 0 rows skips the document.
5. Read the file from MinIO, extract text, split into chunks, set `total_chunks`.
6. For each chunk: `chunk_id` = deterministic hash of (`document_id` + chunk index); embed via the Adapter; **UPSERT** into the vector DB; then increment `chunks_processed` with an atomic UPDATE.
7. Only when `chunks_processed = total_chunks` set `embedded`. On error set `failed`.
8. Celery Beat re-queues tasks stuck in `processing` beyond the threshold.

### 5.3 Long AI tasks (long summarization, question generation, study planning)

- The API Backend returns a `job_id` immediately; the Web Client polls; status and result are stored in PostgreSQL.
- Summarization: short documents → synchronous in the AI Service; above the threshold → the AI Service checks quota, then enqueues `summarize_long` for the Worker.
- On failure (e.g., Gemini timeout) → notify the student and allow retry.

### 5.4 Email deadline reminders

Deadline Scanner (Beat) scans the DB → enqueues reminder tasks in Redis → Worker: Idempotency Guard → Email Sender Task sends via SMTP → writes the result to `notifications` → Notification Module displays it. No duplicates even when multiple scheduler runs overlap. On email failure: retry, and never block the user.

### 5.5 Pro payment (SePay) and downgrade

1. The student buys a plan → Subscription Service → Payment Client creates a payment request and gets a QR from SePay → Web Client shows the QR.
2. SePay calls the **webhook** on the API Backend. The API Backend verifies the signature, checks that `sepay_transaction_id` has not been processed (UNIQUE in the DB), then updates `subscriptions` and `payment_transactions`.
3. On expiry: Subscription Expiry Scanner → `downgrade_subscription` task → Worker (idempotency state in PostgreSQL) → plan set back to Free.

### 5.6 Level 4 class diagrams (follow these when coding)

**A. Document Processing Task (Figure 10)**

| Class                  | Attributes / methods                                                                           | Role                                                                      |
| ---------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| TaskDispatcher         | `dispatch(task: Task)`                                                                         | Calls IdempotencyGuard before handing off to DocumentProcessingTask       |
| IdempotencyGuard       | `store: RedisClient`; `isDone(key)`, `markInProgress(key)`, `markDone(key)`, `markFailed(key)` | Stores `in_progress`/`done`/`failed`; `done` is set only after completion |
| DocumentProcessingTask | `documentId: UUID`; `run()`, `extractText(file: bytes)`, `splitChunks(text)`                   | Orchestrator: read file, extract text, split chunks                       |
| ChunkIdGenerator       | `generate(documentId, index) -> str`                                                           | Fixed ID so retries create no duplicates                                  |
| Chunk                  | `index: int`, `text: str`, `embedding: List<float>`                                            | Temporary object before saving                                            |
| EmbeddingProvider      | `embed(text) -> List<float>`                                                                   | Part of the Provider Adapter; calls Gemini                                |
| VectorRepository       | `upsertChunk(chunkId, chunk)`, `updateDocumentStatus(documentId, status)`                      | UPSERTs chunks; sets `embedded` only after all chunks are written         |

**B. Generation Module (Figure 11)**

| Class            | Methods                                            | Role                                                                                                     |
| ---------------- | -------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| RequestRouter    | `route(request: ChatRequest)`                      | Receives requests that passed the quota check and routes to RetrievalModule                              |
| RetrievalModule  | `retrieveTopK(question, userId, k) -> List<Chunk>` | Top-K within the user's permitted scope                                                                  |
| PromptBuilder    | `build(question, chunks) -> str`                   | Combines question + chunks into a prompt                                                                 |
| GenerationModule | `generate(prompt) -> RawAnswer`                    | Calls LLMProvider and **must** pass the result through GroundingChecker; never return the raw LLM answer |
| GroundingChecker | `isGrounded(answer, chunks) -> bool`               | Insufficient grounding → status “no relevant information found”                                          |
| AnswerResult     | `status`, `answer`, `sources: List<Source>`        | Final result (answered / no grounding found + citations)                                                 |
| LLMProvider      | `generate(prompt) -> RawAnswer`                    | Part of the Provider Adapter, shared with EmbeddingProvider                                              |

## 6. Measurable quality requirements (QAS) – use as test criteria

| QAS    | Attribute      | Mandatory measures                                                                                                                                                                                                                                                                       |
| ------ | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| QAS-01 | Performance    | Business APIs (documents, assignments, exams): P95 ≤ 3 s and error rate < 1% at 50 concurrent users. AI chat/RAG: P95 ≤ 15 s for successful requests (until the full answer is returned). Long tasks run asynchronously                                                                  |
| QAS-02 | Reliability    | Non-AI functions ≥ 99% available even if Gemini is down. Gemini timeout 10 s, max 1 retry (worst case about 20 s). Circuit breaker opens after 5 failures and retries after 30 s. Fallback message within 30 s. Error rate < 1%                                                          |
| QAS-03 | Security       | 100% of APIs reject invalid tokens (401) and wrong roles (403). Passwords hashed with bcrypt. Access token 15 min, refresh token 7 days. 0 high-severity OWASP ZAP findings. Security events logged, no data exposed                                                                     |
| QAS-04 | Cost Control   | 100% of Gemini calls (AI Service and Worker) logged with tokens/cost. 100% of AI requests (including summarization) after quota exhaustion rejected **before** any Gemini call. Default quota: **Free 10 AI requests/day, Pro 150 AI requests/day**. Warning at 80%. Redis down → reject |
| QAS-05 | AI/RAG Quality | Top-K = 5. On a 30-question golden set (≥ 10 unanswerable): citations ≥ 90%, accuracy ≥ 80%, faithfulness ≥ 85%, ≥ 90% of unanswerable queries return “no information found”. Add prompt-injection cases to the golden set                                                               |

## 7. Supplementary NFRs and applied tactics

| NFR                     | Design requirement                                                                                                                                                   | Where applied                     |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| NFR05 Scalability       | Scale instances independently per container                                                                                                                          | AI Service, Worker                |
| NFR10 Disaster Recovery | Periodic backup, RPO 24 h, RTO 4 h, daily backup stored **outside the Docker Host**                                                                                  | PostgreSQL, MinIO                 |
| NFR04 Security          | Uploaded-file validation; login attempt limiting; secrets (Gemini, SePay keys) in environment configuration only, never in code; restricted CORS                     | API Backend (Auth, Core)          |
| NFR06 Maintainability   | Provider abstraction via the Adapter                                                                                                                                 | LLM & Embedding Adapter           |
| NFR03 Reliability       | Idempotency Guard prevents duplicate sending                                                                                                                         | Email Sender Task, Worker         |
| NFR07 Cost Control      | Admin module for quota management and monitoring                                                                                                                     | Administration & Monitoring       |
| NFR02 Usability         | Handled at UI level, verified by usability testing                                                                                                                   | Web Client                        |
| NFR08 Observability     | Correlation ID across containers; structured logs; health-check endpoint on each service; Sentry                                                                     | API Backend, AI Service, Worker   |
| NFR09 Legal & Privacy   | Per-user retrieval scope; cascade deletion of user data; user consent before documents are sent to Gemini; check the data-processing terms of the Gemini plan in use | Retrieval Module, Auth, DB, MinIO |

## 8. Architectural risks and mitigations (the Agent must implement these)

| #    | Risk                                                      | Mandatory mitigation                                                                                                                                                                                                                                                                                                                        |
| ---- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R-01 | Embeddings incomplete while the document is reported done | (1) `chunk_id` = deterministic hash of document_id + chunk index, written with UPSERT. (2) Four statuses: pending, processing, embedded, failed. (3) Columns `total_chunks` and `chunks_processed` (atomic increment); `embedded` only when equal. (4) Claim tasks with `UPDATE ... WHERE status='pending'`. (5) Beat re-queues stuck tasks |
| R-02 | Dependence on a single provider (Gemini)                  | All calls go through the Adapter; a new provider needs only a new adapter                                                                                                                                                                                                                                                                   |
| R-03 | AI cost exceeds expectations                              | Real-time Quota Manager; Usage Logger to tune quotas                                                                                                                                                                                                                                                                                        |
| R-04 | Boundaries between services and shared packages violated  | Automated CI checks on `providers/` and `clients/`; periodic architecture reviews                                                                                                                                                                                                                                                           |
| R-05 | RAG quality depends on extraction/chunking                | Grounding check; improve chunking progressively from real data                                                                                                                                                                                                                                                                              |
| R-06 | Cross-account data access via Retrieval                   | Centralize access control in the Retrieval Module; add cross-account tests                                                                                                                                                                                                                                                                  |
| R-07 | Redis is broker + cache + quota store (shared dependency) | Phase 1: single instance. Phase 2: split into one for cache and one for broker + quota (AOF persistence). Quota Manager is **fail-closed** in all phases                                                                                                                                                                                    |
| R-08 | Prompt injection via uploaded documents                   | Sanitize chunks; input/output filtering; separate data block with an instruction not to follow commands; no tool/action permissions for the AI; injection tests in the golden set; periodic security testing                                                                                                                                |
| R-09 | SePay webhook duplicated or spoofed                       | Verify signature; UNIQUE `sepay_transaction_id` in the DB; do not reuse the Worker's Idempotency Guard (synchronous flow)                                                                                                                                                                                                                   |

## 9. Deployment view

- **One Docker Host**, recommended minimum 8 GB RAM, 4 vCPU, 50+ GB SSD. The browser speaks only HTTPS (port 443) to the Reverse Proxy.
- **Reverse Proxy (Nginx/Traefik):** terminates TLS, routes `/api` to the API Backend and static content to the Web Client.

| Container                     | Recommended resources                  |
| ----------------------------- | -------------------------------------- |
| Web Client (static Vue build) | 0.5 vCPU, 256 MB RAM, 200 MB storage   |
| API Backend (Uvicorn)         | 1 vCPU, 1 GB RAM                       |
| AI Service (Uvicorn)          | 1 vCPU, 1 GB RAM                       |
| Background Worker (Celery)    | 1 vCPU, 1–2 GB RAM                     |
| Scheduler (Celery Beat)       | 0.25 vCPU, 256 MB RAM                  |
| PostgreSQL                    | 1 vCPU, 2 GB RAM, 20 GB SSD (growable) |
| Redis                         | 0.5 vCPU, 512 MB RAM                   |
| MinIO                         | 0.5 vCPU, 512 MB RAM, 20–50 GB         |

- External services: Gemini API (HTTPS, only from the AI Service and Worker), SMTP (only from the Worker), SePay (two-way HTTPS, only with the API Backend).
- Internal ports: PostgreSQL 5432, Redis 6379.

## 10. User Stories (25) and mapping to modules

Priority: H = high, M = medium, L = low. A story with dependencies has priority lower than or equal to its dependencies. The Module column maps to the architecture (mappings not stated in the source are marked inferred).

| #    | Story                                         | Actor   | Pri | Key acceptance criteria                                                                                                                                                                                                                | Module                                 |
| ---- | --------------------------------------------- | ------- | --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| 2.1  | Register an account                           | Student | H   | Form with full name, email, password (or social login). Validate email format and password strength. Password stored hashed, never plaintext. Duplicate email shows a clear error. After registration, redirect to login or auto-login | Auth, Email Sender                     |
| 2.2  | Log in and log out                            | Student | H   | Email/password or social account. Issue JWT access + refresh tokens. Invalid credentials give a generic error that does not reveal which part was wrong. Automatic token-expiry handling. Logout invalidates the session tokens        | Auth                                   |
| 2.3  | Recover a forgotten password                  | Student | H   | Request by email; time-limited code/link (e.g., 15 min), single use. New password must meet the policy. After reset, all existing sessions are invalidated                                                                             | Auth, Email Sender                     |
| 2.4  | Update personal profile                       | Student | M   | Edit full name, display name, major, email (re-verification on email change). Only the owner or an Admin may edit                                                                                                                      | Auth                                   |
| 2.5  | Restrict access by role (RBAC)                | System  | H   | Every request validated against JWT + role. Students cannot reach Admin endpoints (403, no internal details). Only an Admin can change roles                                                                                           | Auth, Core/Security                    |
| 2.6  | Manage courses                                | Student | H   | Create (name, description, semester, instructor), edit, delete. Delete requires confirmation and states the effect on linked assignments/exams/documents                                                                               | Schedule Mgmt                          |
| 2.7  | Manage assignments                            | Student | H   | Linked to a course, with due date, priority, status (Not Started / In Progress / Completed). Feeds the calendar (FR02), reminders (FR10), progress (FR09)                                                                              | Schedule Mgmt                          |
| 2.8  | Manage exam schedules                         | Student | H   | Subject, date, time, location; edit/delete. Shown on the aggregated calendar; feeds reminders and progress alerts                                                                                                                      | Schedule Mgmt                          |
| 2.9  | View an aggregated calendar                   | Student | H   | Day/week/month views combining classes, exams, deadlines; distinguishable by color/icon; click for details; auto-updates when data changes                                                                                             | Schedule Mgmt, Web Client (inferred)   |
| 2.10 | Manage class schedule                         | Student | M   | Add/edit/delete sessions linked to a course (day, time, location). **Warn on overlapping entries.** Feeds study planning (FR08) and the calendar (FR02)                                                                                | Schedule Mgmt                          |
| 2.11 | Upload and organize learning documents        | Student | H   | PDF/DOCX/images; validate type and size; assign course and tags. Document appears in the course list after processing. **Only the uploader can access it (multi-tenant isolation)**                                                    | Document, Worker                       |
| 2.12 | Search and filter documents                   | Student | M   | Search by title, tag, course name; filter by course, file type, tag; sort by name, upload date, modified date; empty-state; open a document or start an AI action from results                                                         | Document (FR13)                        |
| 2.13 | Chat with the general AI assistant            | Student | H   | Start a new conversation, send questions, history saved per session, responses labeled “AI-Generated”                                                                                                                                  | Chat Service, AI Service               |
| 2.14 | Ask questions grounded in my documents (RAG)  | Student | M   | Select document/course scope. Answers **only from the documents**, with citations to specific passages. If nothing is found, explicitly say “no information found”. Visible AI disclaimer                                              | AI Service (Retrieval + Generation)    |
| 2.15 | Get an AI-generated document summary          | Student | M   | Select one or more documents; runs asynchronously; summary is saved, viewable or downloadable; on failure notify and allow retry                                                                                                       | AI Service, Worker (Summarization)     |
| 2.16 | Generate a practice question bank             | Student | L   | Generate multiple-choice/essay questions from a document; save to a personal question bank; include answers/explanations                                                                                                               | AI Service, Document (FR07)            |
| 2.17 | Get an AI-assisted study plan                 | Student | L   | Uses courses, assignments, exams, progress; view on dashboard; edit/accept/dismiss each item; accepted items appear in the calendar/task list                                                                                          | AI Service, Schedule Mgmt (FR08)       |
| 2.18 | View my study progress dashboard              | Student | H   | Assignment completion and revision progress by course; alert when behind or a deadline approaches; students see only their own data                                                                                                    | Progress                               |
| 2.19 | View academic performance statistics          | Student | H   | Number of courses, completed vs pending assignments, upcoming exams, plan-completion rate; highlight low-progress courses; classify on track / at risk / overdue                                                                       | Progress                               |
| 2.20 | Receive automated deadline reminders          | Student | M   | Periodic scan of exams/deadlines/tasks; reminder email before deadlines; **no duplicate emails**, even with concurrent scheduler runs; email outage must not block users, failures are retried                                         | Scheduler, Worker (Email), Idempotency |
| 2.21 | View and manage in-app notifications          | Student | M   | Auto-created for deadlines, exams, delayed plans, AI quota alerts; categorized (assignments, exams, progress, study plans, AI); mark read/unread with visible unread count; deep-link to related content                               | Notification (read) + writers          |
| 2.22 | Stay within my AI usage quota                 | Student | M   | Track AI requests per period (daily/monthly); warn near the threshold; reject over-quota requests with a clear message; log every AI request                                                                                           | Quota Manager, Usage Logger            |
| 2.23 | Manage student accounts (Admin)               | Admin   | H   | Paginated/searchable list with status; activate/suspend; a suspended student cannot log in and sees a clear message                                                                                                                    | Administration                         |
| 2.24 | Monitor and configure AI usage quotas (Admin) | Admin   | H   | System-wide AI usage (requests, tokens) by user/period; change a user's quota or the platform default; **takes effect without restart**; see users near or over quota                                                                  | Administration                         |
| 2.25 | View system statistics and health (Admin)     | Admin   | H   | Total files and storage, total requests, job/queue status; refresh without degrading performance; Admin only                                                                                                                           | Administration                         |

- Note: a Student may edit/delete only records they created. All personal data must be isolated per user.

## 11. Current database schema (EduSense_PostgreSQL_Schema.sql)

Target: PostgreSQL 15+ / FastAPI / SQLAlchemy / Alembic. UUID primary keys (`gen_random_uuid()`). Most FKs to `users` use `ON DELETE CASCADE` (supports deletion on request). **This is the current state and is incomplete relative to the architecture – see Section 12.**

| #   | Table              | Key columns                                                                                                                                                                     | Constraint notes                                                                                                 |
| --- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| 1   | users              | email, password_hash, is_active, is_email_verified, last_login_at                                                                                                               | UNIQUE(email). `is_active` is used for Admin suspend                                                             |
| 2   | user_profiles      | user_id, full_name, avatar_url, phone_number, university, major, academic_year                                                                                                  | FK users CASCADE; academic_year 1–10                                                                             |
| 3   | roles              | name, description                                                                                                                                                               | UNIQUE(name)                                                                                                     |
| 4   | user_roles         | user_id, role_id                                                                                                                                                                | FK CASCADE on both                                                                                               |
| 5   | courses            | user_id, course_code, name, description, semester, academic_year, status                                                                                                        | UNIQUE(user_id, course_code, semester, academic_year); status: active/archived                                   |
| 6   | assignments        | course_id, title, description, due_at, priority, status, completed_at                                                                                                           | priority: low/medium/high; status: pending/in_progress/completed/overdue                                         |
| 7   | exams              | course_id, title, exam_at, duration_minutes, location, notes                                                                                                                    | exam_at NOT NULL                                                                                                 |
| 8   | class_schedules    | course_id, day_of_week (0–6), start_time, end_time, location, start_date, end_date                                                                                              | end_time > start_time; end_date ≥ start_date                                                                     |
| 9   | class_sessions     | schedule_id, starts_at, ends_at, location, status                                                                                                                               | UNIQUE(schedule_id, starts_at); status: scheduled/completed/cancelled                                            |
| 10  | documents          | user_id, course_id, title, file_url, status                                                                                                                                     | Metadata + S3 object key only. status: pending/processing/completed/failed. course_id SET NULL                   |
| 11  | document_chunks    | document_id, chunk_index, content, page_number, token_count, embedding                                                                                                          | UNIQUE(document_id, chunk_index); embedding is `DOUBLE PRECISION[]`                                              |
| 12  | ai_conversations   | user_id, course_id, title, conversation_type                                                                                                                                    | type: general/rag                                                                                                |
| 13  | ai_messages        | conversation_id, content, model_name, prompt_tokens, completion_tokens, latency_ms                                                                                              | content must not be blank                                                                                        |
| 14  | answer_citations   | message_id, chunk_id, citation_order, relevance_score, quoted_text                                                                                                              | UNIQUE(message_id, chunk_id) and UNIQUE(message_id, citation_order); score 0–1                                   |
| 15  | document_summaries | document_id, user_id, summary_type, content, model_name                                                                                                                         | type: brief/general/detailed                                                                                     |
| 16  | question_banks     | user_id, course_id, document_id, name, description                                                                                                                              | course/document SET NULL                                                                                         |
| 17  | questions          | question_bank_id, question_type, content, options (JSONB), correct_answer, explanation, difficulty                                                                              | type: multiple_choice/essay; multiple_choice requires `options` to be a JSON array; difficulty: easy/medium/hard |
| 18  | study_plans        | user_id, name, description, start_date, end_date, status, generated_by_ai                                                                                                       | status: draft/active/completed/archived                                                                          |
| 19  | study_plan_items   | study_plan_id, course_id, title, description, scheduled_start, scheduled_end, status, completed_at, display_order                                                               | status: pending/in_progress/completed/dismissed                                                                  |
| 20  | course_progress    | user_id, course_id, assignments_total/completed, study_items_total/completed, progress_percent, risk_status, last_calculated_at                                                 | UNIQUE(user_id, course_id); risk: on_track/at_risk/overdue                                                       |
| 21  | notifications      | user_id, notification_type, title, content, channel, scheduled_at, sent_at, status                                                                                              | channel: in_app/email; status: pending/sent/failed/cancelled                                                     |
| 22  | ai_quotas          | user_id, period_start, period_end, request_limit, token_limit, requests_used, tokens_used                                                                                       | UNIQUE(user_id, period_start, period_end); used ≤ limit                                                          |
| 23  | ai_usage_logs      | user_id, conversation_id, message_id, feature, provider (default google), model_name, prompt/completion/total_tokens, estimated_cost, latency_ms, request_status, error_message | total_tokens = prompt + completion; status: success/failed/timeout/rejected                                      |

## 12. Deviations and gaps between the SQL schema, the architecture, and the User Stories

**Handling principle:** follow the architecture. The Agent should create Alembic migrations to fix these; for items marked “unspecified”, ask the user before finalizing table/column names.

| #   | Issue                                                                                                                                                                                                       | Evidence                                                                            | Action                                                                                                                                            |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `documents.status` uses `completed`, defaulting to `completed`                                                                                                                                              | Architecture (R-01): `pending/processing/embedded/failed`                           | Rename `completed` to `embedded`; default must be `pending`                                                                                       |
| 2   | `documents` lacks `total_chunks` and `chunks_processed`                                                                                                                                                     | R-01                                                                                | Add two integer columns (counted with atomic UPDATE)                                                                                              |
| 3   | `document_chunks.embedding` is `DOUBLE PRECISION[]`, not pgvector; no `CREATE EXTENSION vector`; no vector index                                                                                            | Architecture: PostgreSQL + pgvector, top-K = 5                                      | Use type `vector(N)` and an index (HNSW/IVFFlat). N depends on the Gemini Embedding model (unspecified)                                           |
| 4   | Chunk `id` is a random UUID                                                                                                                                                                                 | R-01 requires an ID that is deterministic from document_id + index                  | Use `ON CONFLICT (document_id, chunk_index)` for UPSERT, or generate a deterministic UUIDv5 as `id` (inferred)                                    |
| 5   | Missing `subscriptions` and `payment_transactions`                                                                                                                                                          | Architecture 4.3.2, R-09                                                            | Add both tables; `payment_transactions.sepay_transaction_id` UNIQUE; store plan (Free/Pro), expiry, status. Exact columns unspecified             |
| 6   | Missing a job table for long AI tasks (`job_id`, status, result, error)                                                                                                                                     | Architecture 3.4                                                                    | Add a job table (name/columns unspecified)                                                                                                        |
| 7   | No place for the downgrade idempotency state in PostgreSQL                                                                                                                                                  | Architecture 4.3.3                                                                  | Add a column/table; approach unspecified                                                                                                          |
| 8   | No table/columns for email-verification codes, password-reset codes, refresh tokens/sessions (needed for invalidation on logout/reset), or login-attempt limiting                                           | US 2.1–2.4, NFR04                                                                   | Add a storage mechanism (DB or Redis); unspecified                                                                                                |
| 9   | `user_roles.id` and `user_profiles.id` have no DEFAULT; missing UNIQUE(user_id, role_id) and UNIQUE(user_id) on profiles (1-to-1)                                                                           | Schema quality                                                                      | Add `gen_random_uuid()` defaults and the UNIQUE constraints                                                                                       |
| 10  | Missing `display_name` (profile) and `instructor` (courses)                                                                                                                                                 | US 2.4, 2.6                                                                         | Add the columns                                                                                                                                   |
| 11  | Social login (US 2.1/2.2) is absent from the architecture's Auth Module and from the schema (no provider column)                                                                                            | US vs architecture                                                                  | **Ask the user** whether to build it. Default: email/password first                                                                               |
| 12  | `notifications` lacks `is_read`/`read_at`, topic category, deep-link info, and a dedup key                                                                                                                  | US 2.20, 2.21                                                                       | Add the corresponding columns                                                                                                                     |
| 13  | `ai_messages` lacks `role` (user/assistant)                                                                                                                                                                 | US 2.13 (chat history)                                                              | Add a `role` column                                                                                                                               |
| 14  | Almost no table has `created_at`/`updated_at` (only `last_calculated_at`, `last_login_at`)                                                                                                                  | US 2.12 (sort by upload/modified date), chat history, audit                         | Add timestamps where needed                                                                                                                       |
| 15  | `documents` lacks file type, size, and tags                                                                                                                                                                 | US 2.11, 2.12                                                                       | Add file_type/size columns and a tag mechanism (separate table or array; unspecified)                                                             |
| 16  | `ai_quotas` is per period with `token_limit`; no place for plan-level default configuration                                                                                                                 | Architecture: quota per request/day, Free 10, Pro 150; Redis is the source of truth | Redis decides real-time quota; the DB is for reporting and overrides. A place for default config is needed (US 2.24)                              |
| 17  | Missing a table for question-practice results                                                                                                                                                               | Progress Module uses question-practice results                                      | Add a table (unspecified)                                                                                                                         |
| 18  | `study_plan_items` has no “accepted” status                                                                                                                                                                 | US 2.17 (accept/dismiss)                                                            | Decide a mapping (e.g., `pending` = accepted; unspecified)                                                                                        |
| 19  | `assignments.status = pending` corresponds to “Not Started”; `overdue` is a computed state                                                                                                                  | US 2.7                                                                              | Keep this mapping                                                                                                                                 |
| 20  | No audit/security-event table and no Admin configuration table (upload limits, per-plan quota)                                                                                                              | QAS-03, NFR04, US 2.24                                                              | Add (unspecified)                                                                                                                                 |
| 21  | No indexes beyond PK/UNIQUE; the script opens `BEGIN;` but has **no `COMMIT;`**                                                                                                                             | Schema quality, QAS-01                                                              | Add `COMMIT;` and indexes on FKs, `user_id`, `due_at`, `exam_at`, `status`                                                                        |
| 22  | `class_sessions` and `course_progress` are not mentioned in the architecture                                                                                                                                | Schema                                                                              | Keep them; use as intended: sessions generated from schedules; `course_progress` is a precomputed table written by the Progress Module (inferred) |
| 23  | Document inconsistencies: User Stories cover page says 2025 (elsewhere 2026); progress FR code is FR09 (US) vs FR15 (architecture); US 2.22 says daily/monthly quota but the architecture only states daily | Cross-review                                                                        | Treat as typos; follow the architecture (daily quota)                                                                                             |

## 13. Execution guidance for AI Agents

### 13.1 Suggested repository layout (inferred, follows Level 4)

`web_client/` · `api_backend/{routers,services,repositories,clients,schemas,core}` · `ai_service/{api,quota,retrieval,generation,core}` · `worker/{dispatcher,idempotency,tasks,core}` + `scheduler/` · shared libraries `shared/core` (Schemas/DTO, Config) and `shared/providers` (LLM & Embedding Adapter + Usage Logger).

### 13.2 Implementation order

1. **Phase 1:** Auth/RBAC → course/assignment/exam → upload + ingestion (Worker) → basic AI/RAG chat → basic quota.
2. **Phase 2:** summarization, question bank, study plan, reminders (Scheduler + Email), AI cost control, Pro + SePay, automatic downgrade.

### 13.3 Mandatory test matrix

| Group                          | Test cases                                                                                                                                                                                                                                                         |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Architecture (import/CI tests) | Routers do not import Repositories; only `providers/` imports the Gemini SDK; Web Client calls only the API Backend; Worker/AI Service never call Gemini outside the Adapter                                                                                       |
| Security                       | Every endpoint: no token → 401; wrong role → 403; Student calling an Admin endpoint → 403; cross-account access (documents, dashboard, RAG) blocked (R-06); bcrypt; access 15 min / refresh 7 days; login-attempt limiting; invalid type/oversized upload rejected |
| Quota                          | Reserve before the Gemini call; refund on failure; `quota_warning` at 80%; Free 10/day, Pro 150/day; Redis down → reject (fail-closed); quota change takes effect without restart; summarization also counts                                                       |
| Adapter / Reliability          | Timeout 10 s, max 1 retry; circuit breaker opens after 5 failures, retries after 30 s; fallback message within 30 s; non-AI features keep working when Gemini is down; every call logged by the Usage Logger                                                       |
| Idempotency / Worker           | Duplicate task skipped when `done`; retry after `failed` still runs; task stuck in `in_progress` can re-run; downgrade never runs twice; reminder emails not duplicated under parallel schedulers                                                                  |
| Ingestion                      | Retry creates no duplicate chunks; `embedded` only when `chunks_processed = total_chunks`; two workers on one document → only one claims it; stuck tasks re-queued                                                                                                 |
| RAG                            | 30-question golden set (≥ 10 unanswerable): citations ≥ 90%, accuracy ≥ 80%, faithfulness ≥ 85%, ≥ 90% “no information found”; prompt-injection tests; top-K = 5; an LLM answer is never returned before passing GroundingChecker                                  |
| Payment                        | Webhook with a bad signature rejected; duplicate `sepay_transaction_id` not reprocessed; expiry downgrades to Free                                                                                                                                                 |
| Performance                    | 50 concurrent users: P95 ≤ 3 s, error rate < 1% for business APIs; RAG P95 ≤ 15 s                                                                                                                                                                                  |
| Observability                  | Correlation ID across containers; health check on each service; structured logs; Sentry                                                                                                                                                                            |

### 13.4 What the Agent must NOT do

- Do not let the Web Client call the AI Service, Worker, DB, MinIO, or SePay directly.
- Do not write SQL in Routers or Services; go through Repositories.
- Do not call Gemini outside the Adapter; do not duplicate Gemini logic in the Worker.
- Do not return an LLM answer before the grounding check; do not infer when there is no grounding.
- Do not let the Quota Manager write notifications; do not let the Notification Module generate notifications.
- Do not set `embedded` before all chunks are written.
- Do not put secrets (Gemini, SePay keys) in code.
- Do not change the architecture to “fit” the current SQL schema.

## 14. Open questions

- **RAG:** Proposed default: `gemini-embedding-2`, 768 dimensions, pgvector HNSW with cosine distance. Top-K = 5; parent chunk 1,200–1,500 tokens, child chunk 400–500 tokens, overlap 60–75 tokens. Validate through RAG evaluation before freezing.
- **Summarization:** Synchronous for documents ≤3 pages and ≤8,000 extracted characters; otherwise enqueue a Celery Worker task.
- **Stuck tasks and reminders:** Treat tasks in `in_progress` without updates for >5 minutes as stuck. Celery Beat scans every 2 minutes and requeues eligible tasks. Scan deadlines every 5 minutes; remind students 24 hours and 1 hour before deadlines. Enforce idempotency.
- **SePay and Pro:** Verify webhooks using SePay’s official signature scheme and a UNIQUE `sepay_transaction_id`. Proposed Pro duration: 30 days; price remains a Product Owner decision. Do not activate live payments before pricing and billing rules are approved.
- **Uploads:** Support PDF, DOCX, and images. Proposed limits: 20 MB/file, 100 pages/PDF, 10 files/request. Admin may configure limits within fixed backend security caps.
- **Social login and quota:** Default to email/password first; ask before implementing social login. Use daily quotas: Free = 10 AI requests/day, Pro = 150/day. Redis is the real-time quota source of truth; fail closed when Redis is unavailable.
- **New database tables:** Add `background_jobs` (job status/result/error), `verification_tokens` (hashed token/expiry/consumption), `refresh_sessions` (session expiry/revocation), `practice_attempts` and `practice_answer_results` (scores/feedback/rubric), `tags` and `document_tags`, and `system_settings`/`quota_policies`. Add `subscriptions` and `payment_transactions` for the approved Pro/SePay flow. Confirm exact columns against the existing schema before migration.
- **Accepted study-plan items:** Add an explicit `accepted` status to `study_plan_items`; `pending` means not yet accepted, and `dismissed` means rejected. Store `accepted_at`. Only accepted items appear in the calendar/task list. Prevent duplicate schedule entries.

**Implementation rule:** These are proposed defaults where the source documents are silent. Follow the Architecture Design over the existing SQL schema, avoid duplicate tables or enums, and report unresolved conflicts before coding.
