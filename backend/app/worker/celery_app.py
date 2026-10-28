"""Celery application configuration for EduSense background worker and scheduler."""

from celery import Celery

from app.core.config import settings

# Initialize Celery application for EduSense background tasks
celery_app = Celery(
    "edusense",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

# Configure Celery settings adhering to Invariant 8 & 9
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    task_routes={
        "app.worker.tasks.document.*": {"queue": "documents"},
        "app.worker.tasks.email.*": {"queue": "emails"},
        "app.worker.tasks.subscription.*": {"queue": "subscriptions"},
    },
)

# Auto-discover tasks in the worker package
celery_app.autodiscover_tasks(["app.worker"])
