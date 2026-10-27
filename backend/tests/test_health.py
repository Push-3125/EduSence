"""Unit tests for health check endpoints and standardized error handling."""

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest_asyncio.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """Yield an asynchronous HTTP test client with in-process ASGI transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_health_liveness(async_client: AsyncClient) -> None:
    """Verify /health returns 200 and ok status."""
    response = await async_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "app" in data


@pytest.mark.asyncio
async def test_health_readiness_all_healthy(async_client: AsyncClient) -> None:
    """Verify /health/ready returns 200 when all infrastructure services respond."""
    with (
        patch("app.api.health.check_database", return_value={"status": "ok"}),
        patch("app.api.health.check_redis", return_value={"status": "ok"}),
        patch("app.api.health.check_minio", return_value={"status": "ok"}),
    ):
        response = await async_client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["checks"]["database"]["status"] == "ok"
        assert data["checks"]["redis"]["status"] == "ok"
        assert data["checks"]["minio"]["status"] == "ok"


@pytest.mark.asyncio
async def test_health_readiness_database_failure(async_client: AsyncClient) -> None:
    """Verify /health/ready returns 503 when database connectivity fails."""
    with (
        patch(
            "app.api.health.check_database",
            return_value={"status": "error", "message": "Connection refused"},
        ),
        patch("app.api.health.check_redis", return_value={"status": "ok"}),
        patch("app.api.health.check_minio", return_value={"status": "ok"}),
    ):
        response = await async_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["checks"]["database"]["status"] == "error"
        assert data["checks"]["redis"]["status"] == "ok"


@pytest.mark.asyncio
async def test_health_readiness_redis_failure(async_client: AsyncClient) -> None:
    """Verify /health/ready returns 503 when Redis connectivity fails."""
    with (
        patch("app.api.health.check_database", return_value={"status": "ok"}),
        patch(
            "app.api.health.check_redis",
            return_value={"status": "error", "message": "Redis timeout"},
        ),
        patch("app.api.health.check_minio", return_value={"status": "ok"}),
    ):
        response = await async_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["checks"]["redis"]["status"] == "error"


@pytest.mark.asyncio
async def test_health_readiness_minio_failure(async_client: AsyncClient) -> None:
    """Verify /health/ready returns 503 when MinIO connectivity fails."""
    with (
        patch("app.api.health.check_database", return_value={"status": "ok"}),
        patch("app.api.health.check_redis", return_value={"status": "ok"}),
        patch(
            "app.api.health.check_minio",
            return_value={"status": "error", "message": "Bucket not found"},
        ),
    ):
        response = await async_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["checks"]["minio"]["status"] == "error"


@pytest.mark.asyncio
async def test_error_envelope_not_found(async_client: AsyncClient) -> None:
    """Verify standardized error envelope for 404 Not Found."""
    response = await async_client.get("/api/v1/non-existent-endpoint")
    assert response.status_code == 404
    data = response.json()
    assert data["success"] is False
    assert "error" in data
    assert data["error"]["code"] == "HTTP_404"
    assert "message" in data["error"]
