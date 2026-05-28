"""Tests for database SSL/TLS enforcement."""

import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDatabaseSslProductionGuard:
    """Verify DATABASE_REQUIRE_SSL warning in agent lifespan for PostgreSQL in Cloud Run."""

    def _env_without_k_service(self) -> dict[str, str]:
        """Return a copy of os.environ without K_SERVICE."""
        return {k: v for k, v in os.environ.items() if k != "K_SERVICE"}

    @pytest.fixture()
    def _mock_lifespan_deps(self):
        """Mock heavy lifespan dependencies so only the SSL warning path runs."""
        with (
            patch("lightspeed_agent.api.app.get_redis_rate_limiter") as mock_rl,
            patch("lightspeed_agent.api.app.start_probe_server", new_callable=AsyncMock),
            patch("lightspeed_agent.api.app.stop_probe_server", new_callable=AsyncMock),
            patch("lightspeed_agent.db.init_database", new_callable=AsyncMock),
            patch("lightspeed_agent.db.close_database", new_callable=AsyncMock),
        ):
            mock_rl.return_value.verify_connection = AsyncMock()
            mock_rl.return_value.close = AsyncMock()
            yield

    @pytest.mark.usefixtures("_mock_lifespan_deps")
    async def test_ssl_disabled_with_postgresql_warns_in_cloud_run(self, caplog):
        """database_require_ssl=False with PostgreSQL logs a warning in Cloud Run."""
        from lightspeed_agent.api.app import lifespan

        mock_settings = MagicMock()
        mock_settings.database_require_ssl = False
        mock_settings.database_url = "postgresql+asyncpg://u:p@localhost:5432/db"
        mock_settings.debug = False
        mock_settings.cors_allowed_origins = ""
        mock_settings.service_control_enabled = False
        mock_settings.agent_probe_port = 0

        with (
            patch("lightspeed_agent.api.app.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {"K_SERVICE": "lightspeed-agent"}, clear=False),
            caplog.at_level(logging.WARNING, logger="lightspeed_agent.api.app"),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                pass

        assert any("DATABASE_REQUIRE_SSL" in r.message for r in caplog.records)

    @pytest.mark.usefixtures("_mock_lifespan_deps")
    async def test_ssl_enabled_with_postgresql_no_warning_in_cloud_run(self, caplog):
        """database_require_ssl=True with PostgreSQL emits no warning in Cloud Run."""
        from lightspeed_agent.api.app import lifespan

        mock_settings = MagicMock()
        mock_settings.database_require_ssl = True
        mock_settings.database_url = "postgresql+asyncpg://u:p@localhost:5432/db"
        mock_settings.debug = False
        mock_settings.cors_allowed_origins = ""
        mock_settings.service_control_enabled = False
        mock_settings.agent_probe_port = 0

        with (
            patch("lightspeed_agent.api.app.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {"K_SERVICE": "lightspeed-agent"}, clear=False),
            caplog.at_level(logging.WARNING, logger="lightspeed_agent.api.app"),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                pass

        assert not any("DATABASE_REQUIRE_SSL" in r.message for r in caplog.records)

    @pytest.mark.usefixtures("_mock_lifespan_deps")
    async def test_ssl_disabled_no_warning_without_k_service(self, caplog):
        """database_require_ssl=False outside Cloud Run emits no warning."""
        from lightspeed_agent.api.app import lifespan

        mock_settings = MagicMock()
        mock_settings.database_require_ssl = False
        mock_settings.database_url = "postgresql+asyncpg://u:p@localhost:5432/db"
        mock_settings.debug = False
        mock_settings.cors_allowed_origins = ""
        mock_settings.service_control_enabled = False
        mock_settings.agent_probe_port = 0

        with (
            patch("lightspeed_agent.api.app.get_settings", return_value=mock_settings),
            patch.dict(os.environ, self._env_without_k_service(), clear=True),
            caplog.at_level(logging.WARNING, logger="lightspeed_agent.api.app"),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                pass

        assert not any("DATABASE_REQUIRE_SSL" in r.message for r in caplog.records)

    @pytest.mark.usefixtures("_mock_lifespan_deps")
    async def test_ssl_not_required_for_sqlite(self, caplog):
        """SQLite is exempt from SSL warning even in Cloud Run."""
        from lightspeed_agent.api.app import lifespan

        mock_settings = MagicMock()
        mock_settings.database_require_ssl = False
        mock_settings.database_url = "sqlite+aiosqlite:///./test.db"
        mock_settings.debug = False
        mock_settings.cors_allowed_origins = ""
        mock_settings.service_control_enabled = False
        mock_settings.agent_probe_port = 0

        with (
            patch("lightspeed_agent.api.app.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {"K_SERVICE": "lightspeed-agent"}, clear=False),
            caplog.at_level(logging.WARNING, logger="lightspeed_agent.api.app"),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                pass

        assert not any("DATABASE_REQUIRE_SSL" in r.message for r in caplog.records)


class TestDatabaseSslEngineKwargs:
    """Verify get_engine() passes SSL connect_args when configured."""

    @patch("lightspeed_agent.db.base.create_async_engine")
    @patch("lightspeed_agent.db.base.get_settings")
    def test_ssl_enabled_adds_connect_args(self, mock_get_settings, mock_create_engine):
        """SSL connect_args are passed to create_async_engine when enabled."""
        import lightspeed_agent.db.base as db_base_module

        mock_settings = MagicMock()
        mock_settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/testdb"
        mock_settings.debug = False
        mock_settings.database_pool_size = 5
        mock_settings.database_pool_max_overflow = 10
        mock_settings.database_require_ssl = True
        mock_get_settings.return_value = mock_settings

        db_base_module._engine = None
        try:
            db_base_module.get_engine()
            mock_create_engine.assert_called_once()
            call_kwargs = mock_create_engine.call_args
            assert call_kwargs.kwargs.get("connect_args", {}).get("ssl") is True
        finally:
            db_base_module._engine = None

    @patch("lightspeed_agent.db.base.create_async_engine")
    @patch("lightspeed_agent.db.base.get_settings")
    def test_ssl_disabled_no_ssl_in_connect_args(self, mock_get_settings, mock_create_engine):
        """SSL connect_args are not passed when database_require_ssl is False."""
        import lightspeed_agent.db.base as db_base_module

        mock_settings = MagicMock()
        mock_settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/testdb"
        mock_settings.debug = False
        mock_settings.database_pool_size = 5
        mock_settings.database_pool_max_overflow = 10
        mock_settings.database_require_ssl = False
        mock_get_settings.return_value = mock_settings

        db_base_module._engine = None
        try:
            db_base_module.get_engine()
            mock_create_engine.assert_called_once()
            call_kwargs = mock_create_engine.call_args
            connect_args = call_kwargs.kwargs.get("connect_args", {})
            assert "ssl" not in connect_args
        finally:
            db_base_module._engine = None


class TestSessionDatabaseSsl:
    """Verify session service passes SSL connect_args for PostgreSQL."""

    @patch("lightspeed_agent.api.a2a.a2a_setup.get_settings")
    @patch(
        "lightspeed_agent.api.a2a.session_service.RetryingDatabaseSessionService",
    )
    def test_session_ssl_passed_for_postgresql(
        self, mock_session_service, mock_get_settings
    ):
        """SSL connect_args are passed to RetryingDatabaseSessionService when enabled."""
        from lightspeed_agent.api.a2a.a2a_setup import _get_session_service

        mock_settings = MagicMock()
        mock_settings.session_backend = "database"
        mock_settings.session_database_url = (
            "postgresql+asyncpg://user:pass@localhost:5432/sessions"
        )
        mock_settings.database_require_ssl = True
        mock_get_settings.return_value = mock_settings

        _get_session_service()

        mock_session_service.assert_called_once()
        call_kwargs = mock_session_service.call_args
        assert call_kwargs.kwargs.get("connect_args") == {"ssl": True}

    @patch("lightspeed_agent.api.a2a.a2a_setup.get_settings")
    @patch(
        "lightspeed_agent.api.a2a.session_service.RetryingDatabaseSessionService",
    )
    def test_session_ssl_not_passed_when_disabled(
        self, mock_session_service, mock_get_settings
    ):
        """SSL connect_args are not passed when database_require_ssl is False."""
        from lightspeed_agent.api.a2a.a2a_setup import _get_session_service

        mock_settings = MagicMock()
        mock_settings.session_backend = "database"
        mock_settings.session_database_url = (
            "postgresql+asyncpg://user:pass@localhost:5432/sessions"
        )
        mock_settings.database_require_ssl = False
        mock_get_settings.return_value = mock_settings

        _get_session_service()

        mock_session_service.assert_called_once()
        call_kwargs = mock_session_service.call_args
        assert "connect_args" not in call_kwargs.kwargs
