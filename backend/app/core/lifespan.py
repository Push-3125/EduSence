"""Application lifespan event handlers for FastAPI."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from app.core.config import settings
from app.core.database import engine

logger = logging.getLogger("edusense.lifespan")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycle."""
    logger.info("Starting up %s (env=%s, debug=%s)...", settings.app_name, settings.app_env, settings.debug)

    yield

    logger.info("Shutting down %s: closing database connection pool...", settings.app_name)
    await engine.dispose()
    logger.info("Database engine disposed cleanly.")
