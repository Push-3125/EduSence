-- EduSense - PostgreSQL database schema (fresh-install DDL)
-- Target: PostgreSQL 15+ / FastAPI / SQLAlchemy / Alembic
-- Source of truth: Architecture Design > User Stories > previous SQL schema.
-- Existing databases must be upgraded with reviewed Alembic migrations; do not rerun this file on a populated DB.
-- Proposed default: vector(768) / gemini-embedding-2. Validate the selected model/dimension before freezing.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- 0. subscription_plans: plan catalogue (Pro remains inactive until pricing/billing is approved)
CREATE TABLE subscription_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(20) NOT NULL,
    name VARCHAR(100) NOT NULL,
    price_vnd BIGINT,
    duration_days INTEGER,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_subscription_plans_code UNIQUE (code),
    CONSTRAINT ck_subscription_plans_price CHECK (price_vnd IS NULL OR price_vnd >= 0),
    CONSTRAINT ck_subscription_plans_duration CHECK (duration_days IS NULL OR duration_days > 0)
);

-- Seed only the plan metadata needed by the schema. Pro price is deliberately unspecified;
-- 30 days is a proposed duration and the Pro plan is disabled pending Product Owner approval.
INSERT INTO subscription_plans (code, name, price_vnd, duration_days, is_active)
VALUES
    ('free', 'Free', 0, NULL, TRUE),
    ('pro', 'Pro', NULL, 30, FALSE)
ON CONFLICT (code) DO NOTHING;

-- 1. users
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    current_plan_code VARCHAR(20) NOT NULL DEFAULT 'free',
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_users_email UNIQUE (email),
    CONSTRAINT fk_users_current_plan FOREIGN KEY (current_plan_code)
        REFERENCES subscription_plans(code) ON UPDATE CASCADE ON DELETE RESTRICT
);

-- 2. user_profiles
CREATE TABLE user_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    display_name VARCHAR(100),
    avatar_url TEXT,
	phone_number VARCHAR(20),
    university VARCHAR(255),
    major VARCHAR(255),
    academic_year SMALLINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_profiles_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT uq_user_profiles_user UNIQUE (user_id),
    CONSTRAINT ck_user_profiles_academic_year
        CHECK (academic_year IS NULL OR academic_year BETWEEN 1 AND 10)
);

-- 3. roles
CREATE TABLE roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(50) NOT NULL,
    description VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_roles_name UNIQUE (name)
);

-- Required RBAC roles. Idempotent so this seed is safe for repeatable bootstrap scripts.
INSERT INTO roles (name, description)
VALUES
    ('student', 'Standard student role.'),
    ('admin', 'System Administrator role.')
ON CONFLICT (name) DO NOTHING;

-- 4. user_roles
CREATE TABLE user_roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    role_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_roles_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_user_roles_role FOREIGN KEY (role_id)
        REFERENCES roles(id) ON DELETE CASCADE,
    CONSTRAINT uq_user_roles_user_role UNIQUE (user_id, role_id)
);

-- 5. courses
CREATE TABLE courses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    course_code VARCHAR(50),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    instructor VARCHAR(255),
    semester VARCHAR(50),
    academic_year VARCHAR(20),
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_courses_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT uq_courses_user_code_semester_year
        UNIQUE (user_id, course_code, semester, academic_year),
    CONSTRAINT ck_courses_status CHECK (status IN ('active', 'archived'))
);

-- 6. assignments
CREATE TABLE assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id UUID NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    due_at TIMESTAMPTZ,
    priority VARCHAR(10) NOT NULL DEFAULT 'medium',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_assignments_course FOREIGN KEY (course_id)
        REFERENCES courses(id) ON DELETE CASCADE,
    CONSTRAINT ck_assignments_priority CHECK (priority IN ('low', 'medium', 'high')),
    CONSTRAINT ck_assignments_status CHECK (status IN ('pending', 'in_progress', 'completed'))
);

-- 7. exams
CREATE TABLE exams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id UUID NOT NULL,
    title VARCHAR(255) NOT NULL,
    exam_at TIMESTAMPTZ NOT NULL,
    duration_minutes INTEGER,
    location VARCHAR(255),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_exams_course FOREIGN KEY (course_id)
        REFERENCES courses(id) ON DELETE CASCADE
);

-- 8. class_schedules
CREATE TABLE class_schedules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id UUID NOT NULL,
    day_of_week SMALLINT NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    location VARCHAR(255),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_class_schedules_course FOREIGN KEY (course_id)
        REFERENCES courses(id) ON DELETE CASCADE,
    CONSTRAINT ck_class_schedules_day CHECK (day_of_week BETWEEN 0 AND 6),
    CONSTRAINT ck_class_schedules_time CHECK (end_time > start_time),
    CONSTRAINT ck_class_schedules_date CHECK (end_date >= start_date)
);

-- 9. class_sessions
CREATE TABLE class_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id UUID NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    location VARCHAR(255),
    status VARCHAR(20) NOT NULL DEFAULT 'scheduled',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_class_sessions_schedule FOREIGN KEY (schedule_id)
        REFERENCES class_schedules(id) ON DELETE CASCADE,
    CONSTRAINT uq_class_sessions_schedule_start UNIQUE (schedule_id, starts_at),
    CONSTRAINT ck_class_sessions_time CHECK (ends_at > starts_at),
    CONSTRAINT ck_class_sessions_status CHECK (status IN ('scheduled', 'completed', 'cancelled'))
);

-- 10. documents: metadata + private MinIO/S3 object key; binary data is not stored in PostgreSQL
-- Processing status must be exactly: pending, processing, embedded, failed.
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    course_id UUID,
    title VARCHAR(255) NOT NULL,
    original_file_name VARCHAR(255) NOT NULL,
    storage_object_key TEXT NOT NULL,
    file_type VARCHAR(100) NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    page_count INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    total_chunks INTEGER NOT NULL DEFAULT 0,
    chunks_processed INTEGER NOT NULL DEFAULT 0,
    processing_error_code VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_documents_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_documents_course FOREIGN KEY (course_id)
        REFERENCES courses(id) ON DELETE SET NULL,
    CONSTRAINT uq_documents_id_user UNIQUE (id, user_id),
    CONSTRAINT ck_documents_status CHECK (status IN ('pending', 'processing', 'embedded', 'failed')),
    CONSTRAINT ck_documents_file_size CHECK (file_size_bytes >= 0),
    CONSTRAINT ck_documents_file_type CHECK (
        file_type IN (
            'application/pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        ) OR file_type LIKE 'image/%'
    ),
    CONSTRAINT ck_documents_page_count CHECK (page_count IS NULL OR page_count > 0),
    CONSTRAINT ck_documents_chunk_counts CHECK (
        total_chunks >= 0 AND chunks_processed >= 0 AND chunks_processed <= total_chunks
    ),
    -- Empty/unextractable documents must be marked failed, never embedded.
    CONSTRAINT ck_documents_embedded_complete CHECK (
        status <> 'embedded' OR (total_chunks > 0 AND chunks_processed = total_chunks)
    )
);

-- 11. document_chunks: extracted text and pgvector embeddings; UPSERT on (document_id, chunk_index) keeps retries idempotent
CREATE TABLE document_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    page_number INTEGER,
    token_count INTEGER,
    embedding vector(768) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_document_chunks_document FOREIGN KEY (document_id)
        REFERENCES documents(id) ON DELETE CASCADE,
    CONSTRAINT uq_document_chunks_index UNIQUE (document_id, chunk_index),
    CONSTRAINT ck_document_chunks_index CHECK (chunk_index >= 0),
    CONSTRAINT ck_document_chunks_page CHECK (page_number IS NULL OR page_number > 0),
    CONSTRAINT ck_document_chunks_tokens CHECK (token_count IS NULL OR token_count >= 0),
    CONSTRAINT ck_document_chunks_content CHECK (length(btrim(content)) > 0)
);

-- 12. ai_conversations
CREATE TABLE ai_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    course_id UUID,
    title VARCHAR(255),
    conversation_type VARCHAR(20) NOT NULL DEFAULT 'general',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_conversations_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_ai_conversations_course FOREIGN KEY (course_id)
        REFERENCES courses(id) ON DELETE SET NULL,
    CONSTRAINT ck_ai_conversations_type CHECK (conversation_type IN ('general', 'rag'))
);

-- 13. ai_messages
CREATE TABLE ai_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL,
    content TEXT NOT NULL,
    model_name VARCHAR(100),
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    role VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_messages_conversation FOREIGN KEY (conversation_id)
        REFERENCES ai_conversations(id) ON DELETE CASCADE,
    CONSTRAINT ck_ai_messages_role CHECK (role IN ('user', 'assistant')),
    CONSTRAINT ck_ai_messages_prompt_tokens CHECK (prompt_tokens >= 0),
    CONSTRAINT ck_ai_messages_completion_tokens CHECK (completion_tokens >= 0),
    CONSTRAINT ck_ai_messages_latency CHECK (latency_ms IS NULL OR latency_ms >= 0),
    CONSTRAINT ck_ai_messages_content CHECK (length(btrim(content)) > 0)
);

-- 14. answer_citations: maps an assistant answer to the source chunks used
CREATE TABLE answer_citations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL,
    chunk_id UUID NOT NULL,
    citation_order SMALLINT NOT NULL,
    relevance_score REAL,
    quoted_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_answer_citations_message FOREIGN KEY (message_id)
        REFERENCES ai_messages(id) ON DELETE CASCADE,
    CONSTRAINT fk_answer_citations_chunk FOREIGN KEY (chunk_id)
        REFERENCES document_chunks(id) ON DELETE CASCADE,
    CONSTRAINT uq_answer_citations_message_chunk UNIQUE (message_id, chunk_id),
    CONSTRAINT uq_answer_citations_order UNIQUE (message_id, citation_order),
    CONSTRAINT ck_answer_citations_order CHECK (citation_order > 0),
    CONSTRAINT ck_answer_citations_score CHECK (relevance_score IS NULL OR relevance_score BETWEEN 0 AND 1)
);

-- 15. document_summaries
CREATE TABLE document_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL,
    user_id UUID NOT NULL,
    summary_type VARCHAR(20) NOT NULL DEFAULT 'general',
    content TEXT NOT NULL,
    model_name VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_document_summaries_document FOREIGN KEY (document_id)
        REFERENCES documents(id) ON DELETE CASCADE,
    CONSTRAINT fk_document_summaries_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT ck_document_summaries_type CHECK (summary_type IN ('brief', 'general', 'detailed'))
);

-- 16. question_banks
CREATE TABLE question_banks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    course_id UUID,
    document_id UUID,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_question_banks_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_question_banks_course FOREIGN KEY (course_id)
        REFERENCES courses(id) ON DELETE SET NULL,
    CONSTRAINT fk_question_banks_document FOREIGN KEY (document_id)
        REFERENCES documents(id) ON DELETE SET NULL
);

-- 17. questions
CREATE TABLE questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question_bank_id UUID NOT NULL,
    question_type VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    options JSONB,
    correct_answer TEXT,
    explanation TEXT,
    difficulty VARCHAR(10) NOT NULL DEFAULT 'medium',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_questions_bank FOREIGN KEY (question_bank_id)
        REFERENCES question_banks(id) ON DELETE CASCADE,
    CONSTRAINT ck_questions_type CHECK (question_type IN ('multiple_choice', 'essay')),
    CONSTRAINT ck_questions_difficulty CHECK (difficulty IN ('easy', 'medium', 'hard')),
    CONSTRAINT ck_questions_options CHECK (
        (question_type = 'multiple_choice' AND options IS NOT NULL AND jsonb_typeof(options) = 'array')
        OR (question_type = 'essay')
    )
);

-- 18. study_plans
CREATE TABLE study_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    generated_by_ai BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_study_plans_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT ck_study_plans_date CHECK (end_date >= start_date),
    CONSTRAINT ck_study_plans_status CHECK (status IN ('draft', 'active', 'completed', 'archived'))
);

-- 19. study_plan_items
CREATE TABLE study_plan_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    study_plan_id UUID NOT NULL,
    course_id UUID,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    scheduled_start TIMESTAMPTZ NOT NULL,
    scheduled_end TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    completed_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_study_plan_items_plan FOREIGN KEY (study_plan_id)
        REFERENCES study_plans(id) ON DELETE CASCADE,
    CONSTRAINT fk_study_plan_items_course FOREIGN KEY (course_id)
        REFERENCES courses(id) ON DELETE SET NULL,
    CONSTRAINT ck_study_plan_items_time CHECK (scheduled_end IS NULL OR scheduled_end > scheduled_start),
    CONSTRAINT ck_study_plan_items_status CHECK (status IN ('pending', 'accepted', 'in_progress', 'completed', 'dismissed')),
    CONSTRAINT ck_study_plan_items_accepted_at CHECK (
        status NOT IN ('accepted', 'in_progress', 'completed') OR accepted_at IS NOT NULL
    ),
    CONSTRAINT ck_study_plan_items_order CHECK (display_order >= 0)
);

-- 20. course_progress
CREATE TABLE course_progress (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    course_id UUID NOT NULL,
    assignments_total INTEGER NOT NULL DEFAULT 0,
    assignments_completed INTEGER NOT NULL DEFAULT 0,
    study_items_total INTEGER NOT NULL DEFAULT 0,
    study_items_completed INTEGER NOT NULL DEFAULT 0,
    progress_percent NUMERIC(5,2) NOT NULL DEFAULT 0,
    risk_status VARCHAR(20) NOT NULL DEFAULT 'on_track',
    last_calculated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_course_progress_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_course_progress_course FOREIGN KEY (course_id)
        REFERENCES courses(id) ON DELETE CASCADE,
    CONSTRAINT uq_course_progress_user_course UNIQUE (user_id, course_id),
    CONSTRAINT ck_course_progress_counts CHECK (
        assignments_total >= 0 AND assignments_completed >= 0
        AND study_items_total >= 0 AND study_items_completed >= 0
        AND assignments_completed <= assignments_total
        AND study_items_completed <= study_items_total
    ),
    CONSTRAINT ck_course_progress_percent CHECK (progress_percent BETWEEN 0 AND 100),
    CONSTRAINT ck_course_progress_risk CHECK (risk_status IN ('on_track', 'at_risk', 'overdue'))
);

-- 21. notifications: this module reads/displays and marks read; writers are Worker, Progress, and API Backend.
CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    notification_type VARCHAR(30) NOT NULL,
    category VARCHAR(30) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    channel VARCHAR(20) NOT NULL DEFAULT 'in_app',
    target_url TEXT,
    deduplication_key VARCHAR(255),
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    read_at TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_notifications_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT uq_notifications_dedup UNIQUE (user_id, channel, deduplication_key),
    CONSTRAINT ck_notifications_category CHECK (category IN ('assignments', 'exams', 'progress', 'study_plans', 'ai')),
    CONSTRAINT ck_notifications_channel CHECK (channel IN ('in_app', 'email')),
    CONSTRAINT ck_notifications_status CHECK (status IN ('pending', 'sent', 'failed', 'cancelled')),
    CONSTRAINT ck_notifications_read_state CHECK (NOT is_read OR read_at IS NOT NULL)
);

-- 22. ai_quotas: per-user/per-period records for reporting and individual overrides.
-- Redis remains the real-time quota source of truth; the architecture specifies daily limits.
-- NULL request_limit/token_limit means inherit the applicable plan-level default from quota_policies.
CREATE TABLE ai_quotas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    request_limit INTEGER,
    token_limit BIGINT,
    requests_used INTEGER NOT NULL DEFAULT 0,
    tokens_used BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_quotas_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT uq_ai_quotas_user_period UNIQUE (user_id, period_start, period_end),
    CONSTRAINT ck_ai_quotas_period CHECK (period_end >= period_start),
    CONSTRAINT ck_ai_quotas_limits CHECK (
        (request_limit IS NULL OR request_limit >= 0)
        AND (token_limit IS NULL OR token_limit >= 0)
    ),
    -- Usage may temporarily exceed a newly lowered limit; Redis enforces admission in real time.
    CONSTRAINT ck_ai_quotas_usage CHECK (requests_used >= 0 AND tokens_used >= 0)
);

-- 23. ai_usage_logs
CREATE TABLE ai_usage_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    conversation_id UUID,
    message_id UUID,
    feature VARCHAR(30) NOT NULL,
    provider VARCHAR(50) NOT NULL DEFAULT 'google',
    model_name VARCHAR(100) NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost NUMERIC(12,6) NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    request_status VARCHAR(20) NOT NULL DEFAULT 'success',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ai_usage_logs_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_ai_usage_logs_conversation FOREIGN KEY (conversation_id)
        REFERENCES ai_conversations(id) ON DELETE SET NULL,
    CONSTRAINT fk_ai_usage_logs_message FOREIGN KEY (message_id)
        REFERENCES ai_messages(id) ON DELETE SET NULL,
    CONSTRAINT ck_ai_usage_logs_tokens CHECK (
        prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0
        AND total_tokens = prompt_tokens + completion_tokens
    ),
    CONSTRAINT ck_ai_usage_logs_cost CHECK (estimated_cost >= 0),
    CONSTRAINT ck_ai_usage_logs_latency CHECK (latency_ms IS NULL OR latency_ms >= 0),
    CONSTRAINT ck_ai_usage_logs_status CHECK (request_status IN ('success', 'failed', 'timeout', 'rejected'))
);

-- 24. quota_policies: plan-level quota defaults, editable by Admin without application restart.
CREATE TABLE quota_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL,
    request_limit_per_day INTEGER NOT NULL,
    token_limit_per_day BIGINT,
    warning_threshold_percent SMALLINT NOT NULL DEFAULT 80,
    updated_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_quota_policies_plan FOREIGN KEY (plan_id)
        REFERENCES subscription_plans(id) ON DELETE CASCADE,
    CONSTRAINT fk_quota_policies_updated_by FOREIGN KEY (updated_by)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_quota_policies_plan UNIQUE (plan_id),
    CONSTRAINT ck_quota_policies_requests CHECK (request_limit_per_day >= 0),
    CONSTRAINT ck_quota_policies_tokens CHECK (token_limit_per_day IS NULL OR token_limit_per_day >= 0),
    CONSTRAINT ck_quota_policies_warning CHECK (warning_threshold_percent BETWEEN 1 AND 100)
);

-- Architecture defaults: Free = 10 requests/day; Pro = 150 requests/day; token limits unspecified.
INSERT INTO quota_policies (plan_id, request_limit_per_day, token_limit_per_day, warning_threshold_percent)
SELECT id, CASE code WHEN 'free' THEN 10 WHEN 'pro' THEN 150 END, NULL, 80
FROM subscription_plans
WHERE code IN ('free', 'pro')
ON CONFLICT (plan_id) DO NOTHING;

-- 25. subscriptions: current and historical plan entitlements.
-- In the payment-webhook and downgrade-task flows, update users.current_plan_code and the
-- corresponding subscription state in the same PostgreSQL transaction to prevent drift.
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    plan_id UUID NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_subscriptions_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_subscriptions_plan FOREIGN KEY (plan_id)
        REFERENCES subscription_plans(id) ON DELETE RESTRICT,
    CONSTRAINT ck_subscriptions_status CHECK (status IN ('pending', 'active', 'expired', 'cancelled')),
    CONSTRAINT ck_subscriptions_period CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at)
);

-- A user can have at most one active subscription at a time.
CREATE UNIQUE INDEX uq_subscriptions_one_active_per_user
    ON subscriptions(user_id) WHERE status = 'active';

-- 26. payment_transactions: SePay webhook deduplication is enforced by UNIQUE sepay_transaction_id.
CREATE TABLE payment_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    subscription_id UUID,
    provider VARCHAR(30) NOT NULL DEFAULT 'sepay',
    sepay_transaction_id VARCHAR(255),
    reference_code VARCHAR(255),
    amount_vnd BIGINT NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'VND',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    provider_payload JSONB,
    webhook_received_at TIMESTAMPTZ,
    paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_payment_transactions_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_payment_transactions_subscription FOREIGN KEY (subscription_id)
        REFERENCES subscriptions(id) ON DELETE SET NULL,
    CONSTRAINT uq_payment_transactions_sepay_id UNIQUE (sepay_transaction_id),
    CONSTRAINT ck_payment_transactions_amount CHECK (amount_vnd >= 0),
    CONSTRAINT ck_payment_transactions_status CHECK (status IN ('pending', 'succeeded', 'failed', 'refunded'))
);

-- 27. background_jobs: persisted status/result for long AI tasks; API returns job_id and clients poll.
CREATE TABLE background_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID,
    job_type VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    progress SMALLINT NOT NULL DEFAULT 0,
    result JSONB,
    error_code VARCHAR(100),
    error_message TEXT,
    idempotency_key VARCHAR(255) NOT NULL,
    attempt_count SMALLINT NOT NULL DEFAULT 0,
    max_attempts SMALLINT NOT NULL DEFAULT 3,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_background_jobs_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT uq_background_jobs_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT ck_background_jobs_status CHECK (status IN ('queued', 'processing', 'succeeded', 'failed', 'cancelled')),
    CONSTRAINT ck_background_jobs_progress CHECK (progress BETWEEN 0 AND 100),
    CONSTRAINT ck_background_jobs_attempts CHECK (attempt_count >= 0 AND max_attempts > 0)
);

-- 28. task_idempotency: PostgreSQL-backed idempotency state is mandatory for subscription downgrade.
-- Other worker tasks may additionally use the Redis Idempotency Guard defined by the architecture.
CREATE TABLE task_idempotency (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key VARCHAR(255) NOT NULL,
    task_type VARCHAR(50) NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'in_progress',
    result JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_task_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT ck_task_idempotency_state CHECK (state IN ('in_progress', 'done', 'failed'))
);

-- 29. verification_tokens: hashed, single-use email-verification/password-reset tokens.
CREATE TABLE verification_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    purpose VARCHAR(30) NOT NULL,
    token_hash VARCHAR(255) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    attempt_count SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_verification_tokens_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT uq_verification_tokens_hash UNIQUE (token_hash),
    CONSTRAINT ck_verification_tokens_purpose CHECK (purpose IN ('email_verification', 'password_reset')),
    CONSTRAINT ck_verification_tokens_attempts CHECK (attempt_count >= 0)
);

-- 30. refresh_sessions: supports logout and revocation of all sessions after password reset.
CREATE TABLE refresh_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    token_hash VARCHAR(255) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    user_agent TEXT,
    ip_address INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_refresh_sessions_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT uq_refresh_sessions_token_hash UNIQUE (token_hash)
);

-- 31. login_attempts: persistent audit/rate-limit evidence; apply throttling in Auth/Core as well.
CREATE TABLE login_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identifier_hash VARCHAR(255) NOT NULL,
    ip_address INET,
    succeeded BOOLEAN NOT NULL DEFAULT FALSE,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 32. practice_attempts: one student attempt at a generated question bank.
CREATE TABLE practice_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    question_bank_id UUID NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'in_progress',
    total_score NUMERIC(10,2),
    max_score NUMERIC(10,2),
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    submitted_at TIMESTAMPTZ,
    graded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_practice_attempts_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_practice_attempts_bank FOREIGN KEY (question_bank_id)
        REFERENCES question_banks(id) ON DELETE CASCADE,
    CONSTRAINT ck_practice_attempts_status CHECK (status IN ('in_progress', 'submitted', 'graded', 'failed')),
    CONSTRAINT ck_practice_attempts_scores CHECK (
        (total_score IS NULL OR total_score >= 0)
        AND (max_score IS NULL OR max_score >= 0)
        AND (total_score IS NULL OR max_score IS NULL OR total_score <= max_score)
    )
);

-- 33. practice_answer_results: includes essay grading, feedback, rationale, and rubric breakdown.
CREATE TABLE practice_answer_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id UUID NOT NULL,
    question_id UUID NOT NULL,
    answer_text TEXT,
    score NUMERIC(10,2),
    max_score NUMERIC(10,2),
    feedback TEXT,
    rationale TEXT,
    rubric_breakdown JSONB,
    reference_answer TEXT,
    grader_model VARCHAR(100),
    graded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_practice_answer_results_attempt FOREIGN KEY (attempt_id)
        REFERENCES practice_attempts(id) ON DELETE CASCADE,
    CONSTRAINT fk_practice_answer_results_question FOREIGN KEY (question_id)
        REFERENCES questions(id) ON DELETE CASCADE,
    CONSTRAINT uq_practice_answer_results_attempt_question UNIQUE (attempt_id, question_id),
    CONSTRAINT ck_practice_answer_results_scores CHECK (
        (score IS NULL OR score >= 0)
        AND (max_score IS NULL OR max_score >= 0)
        AND (score IS NULL OR max_score IS NULL OR score <= max_score)
    )
);

-- 34. tags: tags are owned by individual users; composite FK below prevents cross-user document tagging.
CREATE TABLE tags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    name VARCHAR(100) NOT NULL,
    normalized_name VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_tags_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT uq_tags_user_normalized_name UNIQUE (user_id, normalized_name),
    CONSTRAINT uq_tags_id_user UNIQUE (id, user_id),
    CONSTRAINT ck_tags_name CHECK (length(btrim(name)) > 0)
);

-- 35. document_tags: enforce that the document and tag belong to the same user.
CREATE TABLE document_tags (
    document_id UUID NOT NULL,
    tag_id UUID NOT NULL,
    user_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_document_tags PRIMARY KEY (document_id, tag_id),
    CONSTRAINT fk_document_tags_document_owner FOREIGN KEY (document_id, user_id)
        REFERENCES documents(id, user_id) ON DELETE CASCADE,
    CONSTRAINT fk_document_tags_tag_owner FOREIGN KEY (tag_id, user_id)
        REFERENCES tags(id, user_id) ON DELETE CASCADE,
    CONSTRAINT fk_document_tags_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
);

-- 36. system_settings: Admin-configurable system limits (e.g., upload limits); never store secrets here.
CREATE TABLE system_settings (
    key VARCHAR(100) PRIMARY KEY,
    value_json JSONB NOT NULL,
    description TEXT,
    updated_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_system_settings_updated_by FOREIGN KEY (updated_by)
        REFERENCES users(id) ON DELETE SET NULL
);

-- Proposed upload defaults; backend fixed security caps must remain in force even if Admin edits these settings.
INSERT INTO system_settings (key, value_json, description)
VALUES (
    'upload_limits',
    '{"max_file_size_mb":20,"max_pdf_pages":100,"max_files_per_request":10,"allowed_mime_types":["application/pdf","application/vnd.openxmlformats-officedocument.wordprocessingml.document","image/jpeg","image/png","image/webp"]}'::jsonb,
    'Proposed defaults for upload validation; enforce independently in the backend.'
)
ON CONFLICT (key) DO NOTHING;

-- 37. audit_security_events: security/audit trail; avoid storing secrets or unnecessary sensitive payloads.
CREATE TABLE audit_security_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID,
    target_user_id UUID,
    event_type VARCHAR(100) NOT NULL,
    severity VARCHAR(20) NOT NULL DEFAULT 'info',
    ip_address INET,
    user_agent TEXT,
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_audit_events_actor FOREIGN KEY (actor_user_id)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_audit_events_target FOREIGN KEY (target_user_id)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT ck_audit_events_severity CHECK (severity IN ('info', 'warning', 'error', 'critical'))
);

-- Indexes for expected lookups and foreign-key joins (primary/unique indexes are implicit).
-- SECURITY INVARIANT: every vector-retrieval SQL query MUST enforce ownership/document_scope
-- inside that same SQL query (e.g., JOIN documents and WHERE documents.user_id = :user_id
-- plus the permitted document/course scope). NEVER fetch unscoped vector candidates and filter
-- authorization afterward. If filtered ANN search returns too few rows, tune HNSW ef_search
-- or enable pgvector iterative scans while keeping the authorization predicate in SQL.
-- Final retrieval Top-K = 5. Any oversampling is only within the authorized SQL query.
CREATE INDEX idx_user_roles_role_id ON user_roles(role_id);
CREATE INDEX idx_courses_user_id ON courses(user_id);
CREATE INDEX idx_assignments_course_due_at ON assignments(course_id, due_at);
CREATE INDEX idx_assignments_status_due_at ON assignments(status, due_at);
CREATE INDEX idx_exams_course_exam_at ON exams(course_id, exam_at);
CREATE INDEX idx_exams_exam_at ON exams(exam_at);
CREATE INDEX idx_class_schedules_course_id ON class_schedules(course_id);
CREATE INDEX idx_class_sessions_starts_at ON class_sessions(starts_at);
CREATE INDEX idx_documents_user_status_created ON documents(user_id, status, created_at DESC);
CREATE INDEX idx_documents_status_updated_at ON documents(status, updated_at);
CREATE INDEX idx_documents_course_id ON documents(course_id);
CREATE INDEX idx_document_chunks_document_id ON document_chunks(document_id);
CREATE INDEX idx_document_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_ai_conversations_user_created ON ai_conversations(user_id, created_at DESC);
CREATE INDEX idx_ai_messages_conversation_created ON ai_messages(conversation_id, created_at);
CREATE INDEX idx_answer_citations_chunk_id ON answer_citations(chunk_id);
CREATE INDEX idx_document_summaries_document_id ON document_summaries(document_id);
CREATE INDEX idx_document_summaries_user_created ON document_summaries(user_id, created_at DESC);
CREATE INDEX idx_question_banks_user_created ON question_banks(user_id, created_at DESC);
CREATE INDEX idx_questions_bank_id ON questions(question_bank_id);
CREATE INDEX idx_study_plans_user_status ON study_plans(user_id, status);
CREATE INDEX idx_study_plan_items_plan_status_start ON study_plan_items(study_plan_id, status, scheduled_start);
CREATE INDEX idx_course_progress_course_id ON course_progress(course_id);
CREATE INDEX idx_notifications_user_read_created ON notifications(user_id, is_read, created_at DESC);
CREATE INDEX idx_ai_quotas_period ON ai_quotas(period_start, period_end);
CREATE INDEX idx_ai_usage_logs_user_created ON ai_usage_logs(user_id, created_at DESC);
CREATE INDEX idx_ai_usage_logs_conversation_id ON ai_usage_logs(conversation_id);
CREATE INDEX idx_ai_usage_logs_message_id ON ai_usage_logs(message_id);
CREATE INDEX idx_ai_usage_logs_feature_created ON ai_usage_logs(feature, created_at DESC);
CREATE INDEX idx_subscriptions_user_status_ends ON subscriptions(user_id, status, ends_at);
CREATE INDEX idx_subscriptions_status_ends_at ON subscriptions(status, ends_at);
CREATE INDEX idx_subscriptions_plan_id ON subscriptions(plan_id);
CREATE INDEX idx_payment_transactions_user_created ON payment_transactions(user_id, created_at DESC);
CREATE INDEX idx_background_jobs_status_queued ON background_jobs(status, queued_at);
CREATE INDEX idx_background_jobs_user_created ON background_jobs(user_id, created_at DESC);
CREATE INDEX idx_verification_tokens_user_purpose_expiry ON verification_tokens(user_id, purpose, expires_at);
CREATE INDEX idx_refresh_sessions_user_revoked_expiry ON refresh_sessions(user_id, revoked_at, expires_at);
CREATE INDEX idx_login_attempts_identifier_time ON login_attempts(identifier_hash, attempted_at DESC);
CREATE INDEX idx_login_attempts_ip_time ON login_attempts(ip_address, attempted_at DESC);
CREATE INDEX idx_practice_attempts_user_created ON practice_attempts(user_id, created_at DESC);
CREATE INDEX idx_practice_attempts_bank_id ON practice_attempts(question_bank_id);
CREATE INDEX idx_practice_answer_results_question_id ON practice_answer_results(question_id);
CREATE INDEX idx_practice_answer_results_attempt_id ON practice_answer_results(attempt_id);
CREATE INDEX idx_tags_user_id ON tags(user_id);
CREATE INDEX idx_document_tags_tag_id ON document_tags(tag_id);
CREATE INDEX idx_audit_security_events_created ON audit_security_events(created_at DESC);
CREATE INDEX idx_audit_security_events_actor_created ON audit_security_events(actor_user_id, created_at DESC);

-- Notes for implementation:
-- * Social login is intentionally not represented yet; Source-of-Truth Summary says email/password first and asks before adding it.
-- * Pro pricing is NULL and Pro is inactive until Product Owner approval; the 30-day duration is a proposal only.
-- * pgvector dimension 768 and upload defaults are proposed values from the current decision summary, not permanent invariants.
-- * users.current_plan_code and subscriptions must be kept consistent atomically by payment/downgrade services.
-- * updated_at is application-maintained unless a trigger is explicitly added by a later migration.
COMMIT;
