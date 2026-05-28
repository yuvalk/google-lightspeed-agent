"""Tests for Redis rate limiter TLS validation."""

import os
from unittest.mock import patch

import pytest

from lightspeed_agent.config import Settings


class TestRedisRateLimiterTlsValidation:
    """Verify TLS checks in RedisRateLimiter.__init__."""

    def _make_settings(self, **overrides: str) -> Settings:
        """Create a Settings instance with the given rate-limit overrides."""
        return Settings(**overrides)

    def test_plain_redis_allowed_without_k_service(self):
        """Plain redis:// is fine when K_SERVICE is unset (local dev)."""
        from lightspeed_agent.ratelimit.middleware import RedisRateLimiter

        env = {k: v for k, v in os.environ.items() if k != "K_SERVICE"}
        settings = self._make_settings(rate_limit_redis_url="redis://localhost:6379/0")
        with (
            patch.dict(os.environ, env, clear=True),
            patch("lightspeed_agent.ratelimit.middleware.get_settings", return_value=settings),
        ):
            limiter = RedisRateLimiter()
            assert limiter is not None

    def test_plain_redis_blocked_in_cloud_run(self):
        """Plain redis:// must fail when K_SERVICE is set."""
        from lightspeed_agent.ratelimit.middleware import RedisRateLimiter

        settings = self._make_settings(rate_limit_redis_url="redis://localhost:6379/0")
        with (
            patch.dict(os.environ, {"K_SERVICE": "lightspeed-agent"}, clear=False),
            patch("lightspeed_agent.ratelimit.middleware.get_settings", return_value=settings),
            pytest.raises(ValueError, match="Redis TLS is required in Cloud Run"),
        ):
            RedisRateLimiter()

    def test_rediss_without_ca_cert_blocked(self):
        """rediss:// without CA cert must fail."""
        from lightspeed_agent.ratelimit.middleware import RedisRateLimiter

        settings = self._make_settings(
            rate_limit_redis_url="rediss://localhost:6380/0",
            rate_limit_redis_ca_cert="",
        )
        with (
            patch("lightspeed_agent.ratelimit.middleware.get_settings", return_value=settings),
            pytest.raises(ValueError, match="RATE_LIMIT_REDIS_CA_CERT must be set"),
        ):
            RedisRateLimiter()

    def test_rediss_with_ca_cert_allowed(self):
        """rediss:// with CA cert is accepted."""
        from lightspeed_agent.ratelimit.middleware import RedisRateLimiter

        settings = self._make_settings(
            rate_limit_redis_url="rediss://localhost:6380/0",
            rate_limit_redis_ca_cert="/certs/ca.pem",
        )
        with patch(
            "lightspeed_agent.ratelimit.middleware.get_settings", return_value=settings
        ):
            limiter = RedisRateLimiter()
            assert limiter is not None
