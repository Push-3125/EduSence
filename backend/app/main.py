from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.api import health
from app.api.v1 import (
    assignments,
    auth,
    calendar,
    chat,
    courses,
    documents,
    exams,
    jobs,
    notifications,
    payments,
    practice,
    progress,
    question_banks,
    schedules,
    study_plans,
    subscriptions,
    summaries,
    tags,
    users,
)
from app.api.v1.admin import (
    quotas as admin_quotas,
)
from app.api.v1.admin import (
    system as admin_system,
)
from app.api.v1.admin import (
    users as admin_users,
)
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.lifespan import lifespan

API_DESCRIPTION = """
# EduSense API — Intelligent University Self-Study Support System

EduSense is an AI-powered personal learning support platform for university students.
It provides academic management (courses, assignments, exams, schedules), secure document ingestion with MinIO and pgvector embeddings, and intelligent Google Gemini AI capabilities (RAG tutoring, summarization, question generation, essay grading, and study planning).

## 🚀 Postman / API Client Quickstart

1. **Base URL**: `http://localhost:8000` (or variable `{{baseUrl}}`).
2. **Authentication**:
   - Register a student account via `POST /api/v1/auth/register` or log in via `POST /api/v1/auth/login`.
   - Copy the `access_token` from the response.
   - Set Authorization Header: `Authorization: Bearer <access_token>`.
3. **Response Envelope**:
   - **Success**: `{"success": true, "data": <payload>, "meta": <pagination_or_extra>}`
   - **Error**: `{"success": false, "error": {"code": "ERROR_CODE", "message": "Reason", "details": ...}}`
"""

OPENAPI_TAGS = [
    {
        "name": "health",
        "description": "System health and readiness probes for uptime monitoring and container orchestration.",
    },
    {
        "name": "auth",
        "description": "User registration, Argon2id authentication, JWT issuance, refresh sessions, password recovery, and Google OAuth2.",
    },
    {
        "name": "users",
        "description": "Student profile management, university/department information, and preferences.",
    },
    {
        "name": "courses",
        "description": "Academic course CRUD operations, syllabus tracking, and enrolled subjects.",
    },
    {
        "name": "assignments",
        "description": "Course assignments, submission deadlines, priorities, and completion tracking.",
    },
    {
        "name": "exams",
        "description": "Midterm and final exam schedules, exam rooms, and target goals.",
    },
    {
        "name": "schedules",
        "description": "Weekly recurring timetable for lectures, lab sessions, and classroom assignments.",
    },
    {
        "name": "calendar",
        "description": "Unified academic calendar aggregation across courses, exams, assignments, and study plans.",
    },
    {
        "name": "documents",
        "description": "MinIO object storage, presigned URLs, document text extraction (PDF/DOCX/Images), and pgvector chunk indexing.",
    },
    {
        "name": "tags",
        "description": "Document categorization and custom tagging.",
    },
    {
        "name": "chat",
        "description": "AI conversational tutor with multi-turn chat memory and document citations.",
    },
    {
        "name": "summaries",
        "description": "AI-powered document summarization (sync short documents, async worker jobs for large documents).",
    },
    {
        "name": "question-banks",
        "description": "Automated question generation (multiple-choice and subjective essay questions) grounded in study materials.",
    },
    {
        "name": "practice",
        "description": "Interactive quiz practice attempts, real-time score tracking, and subjective essay evaluation.",
    },
    {
        "name": "study-plans",
        "description": "AI-assisted personalized study plan generation, item acceptance, and calendar synchronization.",
    },
    {
        "name": "progress",
        "description": "Academic progress dashboard, completion rates, study streaks, and subject mastery analytics.",
    },
    {
        "name": "jobs",
        "description": "Asynchronous Celery background job status polling.",
    },
    {
        "name": "notifications",
        "description": "In-app alerts, deadline reminders, and notification preferences.",
    },
    {
        "name": "payments",
        "description": "VietQR payment generation and secure SePay webhook callback handling.",
    },
    {
        "name": "subscriptions",
        "description": "Student subscription tier management (Free vs Pro) and AI quota allocations.",
    },
    {
        "name": "admin-users",
        "description": "Administrator student account management, account activation, and status suspension.",
    },
    {
        "name": "admin-quotas",
        "description": "Administrator system-wide AI token monitoring and quota policy management.",
    },
    {
        "name": "admin-ai-quotas",
        "description": "Administrator AI usage analytics and quota allocation controls.",
    },
    {
        "name": "admin-system",
        "description": "Administrator infrastructure storage usage, database table metrics, and health reports.",
    },
]

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=API_DESCRIPTION,
    servers=[
        {"url": "http://localhost:8000", "description": "Local Development Server"},
        {"url": "http://127.0.0.1:8000", "description": "Localhost IP"},
    ],
    openapi_tags=OPENAPI_TAGS,
    debug=settings.debug,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    lifespan=lifespan,
)


def custom_openapi() -> dict[str, object]:
    """Generate enhanced OpenAPI specification with Bearer JWT security scheme."""
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
        servers=app.servers,
    )
    components = openapi_schema.setdefault("components", {})
    if isinstance(components, dict):
        security_schemes = components.setdefault("securitySchemes", {})
        if isinstance(security_schemes, dict) and "HTTPBearer" in security_schemes:
            scheme = security_schemes["HTTPBearer"]
            if isinstance(scheme, dict):
                scheme["bearerFormat"] = "JWT"
                scheme["description"] = (
                    "Enter JWT Access Token obtained from POST /api/v1/auth/login"
                )
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]

# CORS Middleware
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Standardized Error Envelope Handlers
register_exception_handlers(app)


# Swagger / OpenAPI Specification Endpoints
@app.get("/swagger.json", include_in_schema=False)
async def swagger_json_spec() -> JSONResponse:
    """Return OpenAPI/Swagger specification JSON for API client tools like Postman."""
    return JSONResponse(content=app.openapi())


# Include Routers
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(courses.router)
app.include_router(assignments.router)
app.include_router(exams.router)
app.include_router(schedules.router)
app.include_router(calendar.router)
app.include_router(documents.router)
app.include_router(tags.router)
app.include_router(chat.router)
app.include_router(summaries.router)
app.include_router(question_banks.router)
app.include_router(practice.router)
app.include_router(study_plans.router)
app.include_router(progress.router)
app.include_router(jobs.router)
app.include_router(notifications.router)
app.include_router(payments.router)
app.include_router(subscriptions.router)
app.include_router(admin_users.router)
app.include_router(admin_quotas.router)
app.include_router(admin_quotas.alias_router)
app.include_router(admin_system.router)

