"""Health check and readiness verification endpoints."""

import asyncio
from typing import Any

from fastapi import APIRouter, Response, status
from redis.asyncio import from_url as redis_from_url
from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session_factory

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 2.0


async def check_database() -> dict[str, Any]:
    """Verify PostgreSQL connectivity and query execution."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            async with async_session_factory() as session:
                result = await session.execute(text("SELECT 1"))
                row = result.scalar_one_or_none()
                if row == 1:
                    return {"status": "ok"}
                return {"status": "error", "message": f"Unexpected query result: {row}"}
    except TimeoutError:
        return {"status": "error", "message": f"Database timed out after {CHECK_TIMEOUT_SECONDS}s"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


async def check_redis() -> dict[str, Any]:
    """Verify Redis server connectivity with PING."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            client = redis_from_url(
                settings.redis_url,
                socket_connect_timeout=CHECK_TIMEOUT_SECONDS,
                socket_timeout=CHECK_TIMEOUT_SECONDS,
            )
            try:
                pong = await client.ping()
                if pong:
                    return {"status": "ok"}
                return {"status": "error", "message": "Redis PING returned False"}
            finally:
                await client.aclose()
    except TimeoutError:
        return {"status": "error", "message": f"Redis timed out after {CHECK_TIMEOUT_SECONDS}s"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _check_minio_sync() -> dict[str, Any]:
    """Synchronous check for MinIO client bucket inspection."""
    from minio import Minio

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    # Check if bucket exists, or just query bucket list / existence
    client.bucket_exists(settings.minio_bucket_name)
    return {"status": "ok"}


async def check_minio() -> dict[str, Any]:
    """Verify MinIO object storage connectivity."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            return await asyncio.to_thread(_check_minio_sync)
    except TimeoutError:
        return {"status": "error", "message": f"MinIO timed out after {CHECK_TIMEOUT_SECONDS}s"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@router.get("/health", status_code=status.HTTP_200_OK)
def health_liveness() -> dict[str, str]:
    """Liveness probe: returns 200 if the process is responsive."""
    return {"status": "ok", "app": settings.app_name}


@router.get("/health/ready")
async def health_readiness(response: Response) -> dict[str, Any]:
    """Readiness probe: verifies connectivity to PostgreSQL, Redis, and MinIO."""
    db_result, redis_result, minio_result = await asyncio.gather(
        check_database(),
        check_redis(),
        check_minio(),
        return_exceptions=False,
    )

    checks = {
        "database": db_result,
        "redis": redis_result,
        "minio": minio_result,
    }

    all_healthy = all(res.get("status") == "ok" for res in checks.values())

    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unhealthy",
            "app": settings.app_name,
            "checks": checks,
        }

    return {
        "status": "ready",
        "app": settings.app_name,
        "checks": checks,
    }
