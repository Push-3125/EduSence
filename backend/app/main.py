from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    practice,
    question_banks,
    schedules,
    study_plans,
    summaries,
    tags,
    users,
)
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.lifespan import lifespan

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    lifespan=lifespan,
)

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
app.include_router(jobs.router)
