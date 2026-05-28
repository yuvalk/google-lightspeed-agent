"""Tests for scope configuration validation in TokenIntrospector."""

import warnings

import pytest

from lightspeed_agent.auth.introspection import TokenIntrospector
from lightspeed_agent.config import Settings


def _make_settings(**overrides) -> Settings:
    """Create Settings with sensible defaults for scope tests."""
    defaults = {
        "google_api_key": "test-key",
        "red_hat_sso_client_id": "test-client-id",
        "red_hat_sso_client_secret": "test-client-secret",
        "skip_jwt_validation": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestScopeConfigValidation:
    """Tests for TokenIntrospector._validate_scope_config."""

    def test_default_config_passes(self):
        """Default scope settings should pass validation."""
        settings = _make_settings()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            TokenIntrospector(settings=settings)

    def test_required_not_subset_of_allowed_raises(self):
        """Required scopes missing from allowed list must raise ValueError."""
        settings = _make_settings(
            agent_required_scope="api.console,api.ocm",
            agent_allowed_scopes="openid,api.console",
        )
        with pytest.raises(ValueError, match="api.ocm"):
            TokenIntrospector(settings=settings)

    def test_empty_required_on_production_raises(self, monkeypatch):
        """Empty AGENT_REQUIRED_SCOPE on Cloud Run must raise ValueError."""
        monkeypatch.setenv("K_SERVICE", "lightspeed-agent")
        settings = _make_settings(
            agent_required_scope="",
            agent_allowed_scopes="openid,profile",
        )
        with pytest.raises(ValueError, match="AGENT_REQUIRED_SCOPE must not be empty"):
            TokenIntrospector(settings=settings)

    def test_empty_allowed_on_production_raises(self, monkeypatch):
        """Empty AGENT_ALLOWED_SCOPES on Cloud Run must raise ValueError."""
        monkeypatch.setenv("K_SERVICE", "lightspeed-agent")
        settings = _make_settings(
            agent_required_scope="api.console",
            agent_allowed_scopes="",
        )
        with pytest.raises(ValueError, match="AGENT_ALLOWED_SCOPES must not be empty"):
            TokenIntrospector(settings=settings)

    def test_allowed_superset_of_required_warns(self):
        """Allowed scopes beyond required should emit a warning."""
        settings = _make_settings(
            agent_required_scope="api.console,api.ocm",
            agent_allowed_scopes="openid,email,api.console,api.ocm",
        )
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            TokenIntrospector(settings=settings)
            scope_warnings = [x for x in w if "AGENT_ALLOWED_SCOPES" in str(x.message)]
            assert len(scope_warnings) == 1
            assert "email" in str(scope_warnings[0].message)
            assert "openid" in str(scope_warnings[0].message)

    def test_allowed_equals_required_no_warning(self):
        """No warning when allowed and required scopes match exactly."""
        settings = _make_settings(
            agent_required_scope="api.console,api.ocm",
            agent_allowed_scopes="api.console,api.ocm",
        )
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            TokenIntrospector(settings=settings)
            scope_warnings = [x for x in w if "AGENT_ALLOWED_SCOPES" in str(x.message)]
            assert len(scope_warnings) == 0

    def test_skip_jwt_validation_bypasses_all_checks(self):
        """All scope checks are skipped when skip_jwt_validation is True."""
        settings = _make_settings(
            skip_jwt_validation=True,
            agent_required_scope="api.console,api.ocm",
            agent_allowed_scopes="openid",  # contradictory — should not raise
        )
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            TokenIntrospector(settings=settings)
            scope_warnings = [x for x in w if "AGENT_ALLOWED_SCOPES" in str(x.message)]
            assert len(scope_warnings) == 0

    def test_empty_scopes_outside_production_no_error(self):
        """Empty scopes outside production should not raise (no K_SERVICE)."""
        settings = _make_settings(
            agent_required_scope="",
            agent_allowed_scopes="",
        )
        # No K_SERVICE set, and no required/allowed means no subset violation either
        TokenIntrospector(settings=settings)
