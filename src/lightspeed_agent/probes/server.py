"""Lightweight probe server for health and readiness checks."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_server: uvicorn.Server | None = None
_task: asyncio.Task | None = None  # type: ignore[type-arg]

# Type alias for readiness check functions.
# Each check is an async callable that returns None on success, raises on failure.
ReadinessCheck = Callable[[], Awaitable[None]]


def create_probe_app(
    service_name: str,
    *,
    readiness_checks: dict[str, ReadinessCheck] | None = None,
) -> FastAPI:
    """Create a minimal FastAPI app with health and readiness endpoints.

    Args:
        service_name: Name of the service (included in responses).
        readiness_checks: Optional mapping of check name to async callable.
            Each callable should return None on success or raise on failure.
    """
    app = FastAPI(title=f"{service_name} probes", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy", "service": service_name}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        if not readiness_checks:
            return JSONResponse({"status": "ready", "service": service_name})

        checks: dict[str, str] = {}
        for name, check in readiness_checks.items():
            try:
                await check()
                checks[name] = "ok"
            except Exception as exc:
                checks[name] = f"failed: {exc}"

        all_passed = all(v == "ok" for v in checks.values())
        status = "ready" if all_passed else "not_ready"
        status_code = 200 if all_passed else 503

        return JSONResponse(
            content={"status": status, "service": service_name, "checks": checks},
            status_code=status_code,
        )

    return app


async def start_probe_server(
    port: int,
    service_name: str,
    *,
    readiness_checks: dict[str, ReadinessCheck] | None = None,
) -> None:
    """Start the probe server as a background asyncio task.

    Args:
        port: Port to bind the probe server on.
        service_name: Name of the service (included in probe responses).
        readiness_checks: Optional mapping of check name to async callable
            for the /ready endpoint.
    """
    global _server, _task  # noqa: PLW0603

    if _server is not None or _task is not None:
        await stop_probe_server()

    app = create_probe_app(service_name, readiness_checks=readiness_checks)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )
    _server = uvicorn.Server(config)
    _task = asyncio.create_task(_server.serve())
    logger.info("Probe server started on port %d for service %s", port, service_name)


async def stop_probe_server() -> None:
    """Signal the probe server to shut down gracefully."""
    global _server, _task  # noqa: PLW0603

    if _server is not None:
        _server.should_exit = True
    if _task is not None:
        await _task
        _task = None
    _server = None
    logger.info("Probe server stopped")
