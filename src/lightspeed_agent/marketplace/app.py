"""Marketplace Handler FastAPI Application.

This is a separate service from the Agent that handles:
1. Pub/Sub events from Google Cloud Marketplace (async provisioning)
2. DCR requests from Gemini Enterprise (sync client registration)

The service exposes a single /dcr endpoint that handles both flows
using smart routing based on request content.
"""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from lightspeed_agent.config import get_settings
from lightspeed_agent.marketplace.router import router as handler_router
from lightspeed_agent.probes import start_probe_server, stop_probe_server
from lightspeed_agent.ratelimit import RateLimitMiddleware, get_redis_rate_limiter
from lightspeed_agent.security import RequestBodyLimitMiddleware, SecurityHeadersMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager for startup/shutdown events."""
    settings = get_settings()

    # Startup: Initialize database
    try:
        from lightspeed_agent.db import init_database

        logger.info("Initializing database: %s", settings.database_url.split("@")[-1])
        await init_database()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize database: %s", e)
        raise

    # Startup: Validate DCR configuration (fail-fast on Cloud Run)
    # This ensures DCR_ENCRYPTION_KEY is valid BEFORE the service becomes ready,
    # preventing silent failures when the first DCR request arrives.
    try:
        from lightspeed_agent.dcr import get_dcr_service

        logger.info("Validating DCR service configuration")
        get_dcr_service()  # Triggers DCRService.__init__() validation
        logger.info("DCR service initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize DCR service: %s", e)
        raise

    # Startup: Verify Redis connectivity for rate limiting
    try:
        await get_redis_rate_limiter().verify_connection()
        logger.info("Rate limiter Redis backend is reachable")
    except Exception as e:
        logger.error("Rate limiter Redis backend is unavailable: %s", e)
        raise

    # Startup: Start the probe server on a separate port
    async def _check_database() -> None:
        from lightspeed_agent.db import get_engine

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.exec_driver_sql("SELECT 1")

    async def _check_redis() -> None:
        await get_redis_rate_limiter().verify_connection()

    probe_port = int(os.getenv("HANDLER_PROBE_PORT", "8003"))
    logger.info("Starting probe server on port %d", probe_port)
    await start_probe_server(
        probe_port,
        "marketplace-handler",
        readiness_checks={"database": _check_database, "redis": _check_redis},
    )

    yield

    # Shutdown: Stop the probe server
    await stop_probe_server()

    # Shutdown: Close database connection
    try:
        from lightspeed_agent.db import close_database

        logger.info("Closing database connection")
        await close_database()
    except Exception as e:
        logger.error("Failed to close database: %s", e)

    # Shutdown: Close Redis connection used by rate limiter
    try:
        await get_redis_rate_limiter().close()
    except Exception as e:
        logger.error("Failed to close rate limiter Redis connection: %s", e)


def create_app() -> FastAPI:
    """Create and configure the Marketplace Handler FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    settings = get_settings()

    app = FastAPI(
        title="Marketplace Handler",
        description="Handles Google Cloud Marketplace provisioning and DCR",
        version="0.1.0",
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        lifespan=lifespan,
    )

    # Include the main handler router
    # This provides the /dcr endpoint that handles both Pub/Sub and DCR
    app.include_router(handler_router)

    # Add rate limiting middleware for /dcr endpoint (IP-based, no auth on this service)
    app.add_middleware(RateLimitMiddleware, rate_limited_paths={"/dcr"})

    # Add security headers middleware (HSTS, X-Content-Type-Options, X-Frame-Options)
    app.add_middleware(SecurityHeadersMiddleware)

    # Add request body size limit (1 MB — Pub/Sub messages and DCR requests are small)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=1 * 1024 * 1024)

    # Add CORS middleware (same policy as agent service).
    # The marketplace handler is server-to-server (Pub/Sub push, DCR from Gemini),
    # so CORS is only needed for dev tools / debugging.
    # Middleware execution order:
    #   CORS -> BodyLimit -> SecurityHeaders -> RateLimit -> Handler
    cors_origins = settings.cors_origins_list
    if settings.debug and not cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["POST"],
            allow_headers=["Content-Type", "Accept"],
        )
    elif cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["POST"],
            allow_headers=["Content-Type", "Accept"],
        )

    return app
