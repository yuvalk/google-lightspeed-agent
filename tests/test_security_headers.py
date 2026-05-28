"""Tests for security headers middleware."""

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from lightspeed_agent.security.middleware import SecurityHeadersMiddleware

# Expected header values (single source of truth for assertions)
EXPECTED_HSTS = "max-age=31536000; includeSubDomains"
EXPECTED_XCTO = "nosniff"
EXPECTED_XFO = "DENY"


@pytest.fixture
def app_with_security_headers():
    """Create a minimal FastAPI app with security headers middleware."""
    app = FastAPI()

    @app.get("/ok")
    async def ok_endpoint():
        return {"status": "ok"}

    @app.get("/not-found")
    async def not_found_endpoint():
        raise HTTPException(status_code=404, detail="not found")

    @app.get("/error")
    async def error_endpoint():
        raise HTTPException(status_code=500, detail="internal server error")

    @app.post("/submit")
    async def post_endpoint():
        return {"accepted": True}

    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _assert_security_headers(response):
    """Assert all three security headers are present with correct values."""
    assert response.headers["strict-transport-security"] == EXPECTED_HSTS
    assert response.headers["x-content-type-options"] == EXPECTED_XCTO
    assert response.headers["x-frame-options"] == EXPECTED_XFO


class TestSecurityHeadersMiddleware:
    """Tests for SecurityHeadersMiddleware."""

    @pytest.mark.asyncio
    async def test_security_headers_present_on_success(self, app_with_security_headers):
        """All three security headers appear on a normal 200 response."""
        transport = ASGITransport(app=app_with_security_headers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ok")

        assert response.status_code == 200
        _assert_security_headers(response)

    @pytest.mark.asyncio
    async def test_security_headers_present_on_404(self, app_with_security_headers):
        """Security headers are present even on 404 error responses."""
        transport = ASGITransport(app=app_with_security_headers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/not-found")

        assert response.status_code == 404
        _assert_security_headers(response)

    @pytest.mark.asyncio
    async def test_security_headers_present_on_500(self, app_with_security_headers):
        """Security headers are present even on 500 error responses."""
        transport = ASGITransport(app=app_with_security_headers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/error")

        assert response.status_code == 500
        _assert_security_headers(response)

    @pytest.mark.asyncio
    async def test_security_headers_present_on_post(self, app_with_security_headers):
        """Security headers are present regardless of HTTP method."""
        transport = ASGITransport(app=app_with_security_headers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/submit")

        assert response.status_code == 200
        _assert_security_headers(response)

    @pytest.mark.asyncio
    async def test_hsts_value_exact(self, app_with_security_headers):
        """Strict-Transport-Security has the exact required value."""
        transport = ASGITransport(app=app_with_security_headers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ok")

        assert response.headers["strict-transport-security"] == EXPECTED_HSTS
