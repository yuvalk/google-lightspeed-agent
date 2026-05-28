"""Tests for application settings guards."""

import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from lightspeed_agent.api.app import _check_mcp_url_security
from lightspeed_agent.config import Settings


class TestSkipJwtProductionGuard:
    """Verify SKIP_JWT_VALIDATION cannot be enabled in Cloud Run."""

    def _env_without_k_service(self) -> dict[str, str]:
        """Return a copy of os.environ without K_SERVICE."""
        return {k: v for k, v in os.environ.items() if k != "K_SERVICE"}

    def test_skip_jwt_allowed_without_k_service(self):
        """SKIP_JWT_VALIDATION=true is fine when K_SERVICE is unset."""
        with patch.dict(os.environ, self._env_without_k_service(), clear=True):
            settings = Settings(skip_jwt_validation=True)
            assert settings.skip_jwt_validation is True

    def test_skip_jwt_blocked_in_cloud_run(self):
        """SKIP_JWT_VALIDATION=true must fail when K_SERVICE is set."""
        with (
            patch.dict(os.environ, {"K_SERVICE": "lightspeed-agent"}, clear=False),
            pytest.raises(ValidationError, match="not allowed in Cloud Run"),
        ):
            Settings(skip_jwt_validation=True)

    def test_no_skip_jwt_allowed_in_cloud_run(self):
        """SKIP_JWT_VALIDATION=false (default) is fine in Cloud Run."""
        with patch.dict(
            os.environ, {"K_SERVICE": "lightspeed-agent"}, clear=False
        ):
            settings = Settings(skip_jwt_validation=False)
            assert settings.skip_jwt_validation is False

    def test_skip_jwt_defaults_to_false(self):
        """Default value of skip_jwt_validation is False."""
        with patch.dict(
            os.environ,
            self._env_without_k_service()
            | {"SKIP_JWT_VALIDATION": "false"},
            clear=True,
        ):
            settings = Settings(skip_jwt_validation=False)
            assert settings.skip_jwt_validation is False


class TestMcpUrlSecurityCheck:
    """Verify _check_mcp_url_security catches insecure MCP URLs."""

    @staticmethod
    def _settings(
        transport_mode: str = "http",
        server_url: str = "http://localhost:8080",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            mcp_transport_mode=transport_mode,
            mcp_server_url=server_url,
        )

    def _env_without_k_service(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k != "K_SERVICE"}

    def test_stdio_mode_skips_check(self):
        """stdio transport doesn't use mcp_server_url — no warning or error."""
        with patch.dict(os.environ, {"K_SERVICE": "agent"}, clear=False):
            _check_mcp_url_security(
                self._settings(transport_mode="stdio", server_url="http://remote.example.com")
            )

    def test_https_url_no_warning(self, caplog):
        """HTTPS URL should never trigger a warning."""
        with (
            patch.dict(os.environ, self._env_without_k_service(), clear=True),
            caplog.at_level(logging.WARNING),
        ):
            _check_mcp_url_security(self._settings(server_url="https://mcp.example.com"))
        assert caplog.text == ""

    def test_https_case_insensitive(self, caplog):
        """Uppercase HTTPS scheme should be accepted."""
        with (
            patch.dict(os.environ, self._env_without_k_service(), clear=True),
            caplog.at_level(logging.WARNING),
        ):
            _check_mcp_url_security(self._settings(server_url="HTTPS://mcp.example.com"))
        assert caplog.text == ""

    def test_localhost_http_no_warning(self, caplog):
        """HTTP to localhost is safe — no warning."""
        with (
            patch.dict(os.environ, self._env_without_k_service(), clear=True),
            caplog.at_level(logging.WARNING),
        ):
            _check_mcp_url_security(self._settings(server_url="http://localhost:8080"))
        assert caplog.text == ""

    def test_ipv4_loopback_no_warning(self, caplog):
        """HTTP to 127.0.0.1 is safe — no warning."""
        with (
            patch.dict(os.environ, self._env_without_k_service(), clear=True),
            caplog.at_level(logging.WARNING),
        ):
            _check_mcp_url_security(self._settings(server_url="http://127.0.0.1:8080"))
        assert caplog.text == ""

    def test_ipv6_loopback_no_warning(self, caplog):
        """HTTP to [::1] is safe — no warning."""
        with (
            patch.dict(os.environ, self._env_without_k_service(), clear=True),
            caplog.at_level(logging.WARNING),
        ):
            _check_mcp_url_security(self._settings(server_url="http://[::1]:8080"))
        assert caplog.text == ""

    def test_non_localhost_http_warns_in_dev(self, caplog):
        """Non-localhost HTTP in dev (no K_SERVICE) logs a warning."""
        with (
            patch.dict(os.environ, self._env_without_k_service(), clear=True),
            caplog.at_level(logging.WARNING),
        ):
            _check_mcp_url_security(self._settings(server_url="http://mcp.example.com"))
        assert "unencrypted HTTP" in caplog.text
        assert "mcp.example.com" in caplog.text

    def test_non_localhost_http_raises_in_production(self):
        """Non-localhost HTTP in Cloud Run (K_SERVICE set) raises ValueError."""
        with (
            patch.dict(os.environ, {"K_SERVICE": "lightspeed-agent"}, clear=False),
            pytest.raises(ValueError, match="unencrypted HTTP"),
        ):
            _check_mcp_url_security(self._settings(server_url="http://mcp.example.com"))

    def test_empty_url_no_warning(self, caplog):
        """Empty URL should not trigger any check."""
        with (
            patch.dict(os.environ, self._env_without_k_service(), clear=True),
            caplog.at_level(logging.WARNING),
        ):
            _check_mcp_url_security(self._settings(server_url=""))
        assert caplog.text == ""
