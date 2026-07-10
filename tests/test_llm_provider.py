"""Tests for multi-model LLM provider settings and model factory."""

import os
from unittest.mock import MagicMock, patch

import pytest
from google.adk.models import Gemini
from pydantic import ValidationError

from lightspeed_agent.config import Settings, get_settings
from lightspeed_agent.core.agent import _create_model

# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_default_provider_is_gemini():
    s = Settings(google_api_key="test-key")
    assert s.llm_provider == "gemini"


def test_default_gemini_model():
    s = Settings(google_api_key="test-key")
    assert s.gemini_model == "gemini-3.5-flash"


def test_llm_model_defaults_to_none():
    s = Settings(google_api_key="test-key")
    assert s.llm_model is None


def test_litellm_provider_with_model_validates():
    s = Settings(
        google_api_key="test-key",
        llm_provider="litellm",
        llm_model="openai/gpt-4o",
    )
    assert s.llm_provider == "litellm"
    assert s.llm_model == "openai/gpt-4o"


def test_litellm_provider_without_model_settings_valid():
    s = Settings(google_api_key="test-key", llm_provider="litellm")
    assert s.llm_provider == "litellm"
    assert s.llm_model is None


def test_litellm_provider_without_model_create_model_raises():
    s = Settings(google_api_key="test-key", llm_provider="litellm")
    with pytest.raises(ValueError, match="LLM_PROVIDER=litellm requires LLM_MODEL"):
        _create_model(s)


def test_invalid_provider_raises():
    with pytest.raises(ValidationError):
        Settings(google_api_key="test-key", llm_provider="invalid")


def test_llm_model_optional_for_gemini():
    s = Settings(google_api_key="test-key", llm_provider="gemini")
    assert s.llm_model is None


def test_llm_api_key_optional():
    s = Settings(google_api_key="test-key")
    assert s.llm_api_key is None

    s2 = Settings(google_api_key="test-key", llm_api_key="sk-test")
    assert s2.llm_api_key == "sk-test"


def test_llm_api_base_optional():
    s = Settings(google_api_key="test-key")
    assert s.llm_api_base is None

    s2 = Settings(google_api_key="test-key", llm_api_base="http://localhost:8080/v1")
    assert s2.llm_api_base == "http://localhost:8080/v1"


# ---------------------------------------------------------------------------
# Model factory — Gemini provider
# ---------------------------------------------------------------------------


def test_gemini_provider_creates_gemini_model():
    s = Settings(google_api_key="test-key", gemini_model="gemini-2.5-flash")
    model = _create_model(s)
    assert isinstance(model, Gemini)
    assert model.model == "gemini-2.5-flash"


def test_gemini_provider_with_llm_model_override():
    s = Settings(
        google_api_key="test-key",
        gemini_model="gemini-2.5-flash",
        llm_model="gemini-2.0-flash",
    )
    model = _create_model(s)
    assert isinstance(model, Gemini)
    assert model.model == "gemini-2.0-flash"


def test_gemini_provider_strips_litellm_prefix():
    s = Settings(
        google_api_key="test-key",
        gemini_model="gemini-2.5-flash",
        llm_model="vertex_ai/gemini-2.5-flash",
    )
    model = _create_model(s)
    assert isinstance(model, Gemini)
    assert model.model == "gemini-2.5-flash"


def test_gemini_provider_includes_retry_options():
    s = Settings(
        google_api_key="test-key",
        gemini_http_retry_attempts=3,
        gemini_http_retry_initial_delay=2.0,
    )
    model = _create_model(s)
    assert isinstance(model, Gemini)
    assert model.retry_options is not None
    assert model.retry_options.attempts == 3
    assert model.retry_options.initial_delay == 2.0


# ---------------------------------------------------------------------------
# Model factory — LiteLLM provider
# ---------------------------------------------------------------------------


def test_litellm_provider_creates_litellm_model():
    s = Settings(
        google_api_key="test-key",
        llm_provider="litellm",
        llm_model="openai/gpt-4o",
    )
    with patch("google.adk.models.lite_llm.LiteLlm") as mock_cls:
        mock_cls.return_value = MagicMock()
        _create_model(s)
        mock_cls.assert_called_once_with(model="openai/gpt-4o")


def test_litellm_provider_passes_api_key():
    s = Settings(
        google_api_key="test-key",
        llm_provider="litellm",
        llm_model="openai/gpt-4o",
        llm_api_key="sk-test-key",
    )
    with patch("google.adk.models.lite_llm.LiteLlm") as mock_cls:
        mock_cls.return_value = MagicMock()
        _create_model(s)
        mock_cls.assert_called_once_with(model="openai/gpt-4o", api_key="sk-test-key")


def test_litellm_provider_passes_api_base():
    s = Settings(
        google_api_key="test-key",
        llm_provider="litellm",
        llm_model="openai/my-model",
        llm_api_base="http://localhost:8080/v1",
    )
    with patch("google.adk.models.lite_llm.LiteLlm") as mock_cls:
        mock_cls.return_value = MagicMock()
        _create_model(s)
        mock_cls.assert_called_once_with(
            model="openai/my-model",
            api_base="http://localhost:8080/v1",
        )


def test_litellm_provider_minimal_config():
    from google.adk.models.lite_llm import LiteLlm

    s = Settings(
        google_api_key="test-key",
        llm_provider="litellm",
        llm_model="anthropic/claude-sonnet-4-20250514",
    )
    model = _create_model(s)
    assert isinstance(model, LiteLlm)
    assert model.model == "anthropic/claude-sonnet-4-20250514"


def test_litellm_provider_passes_all_kwargs():
    s = Settings(
        google_api_key="test-key",
        llm_provider="litellm",
        llm_model="anthropic/claude-sonnet-4-20250514",
        llm_api_key="sk-ant-xxx",
        llm_api_base="https://proxy.example.com/v1",
    )
    with patch("google.adk.models.lite_llm.LiteLlm") as mock_cls:
        mock_cls.return_value = MagicMock()
        _create_model(s)
        mock_cls.assert_called_once_with(
            model="anthropic/claude-sonnet-4-20250514",
            api_key="sk-ant-xxx",
            api_base="https://proxy.example.com/v1",
        )


def test_litellm_import_error_raises_runtime_error():
    s = Settings(
        google_api_key="test-key",
        llm_provider="litellm",
        llm_model="openai/gpt-4o",
    )
    with (
        patch.dict("sys.modules", {"google.adk.models.lite_llm": None}),
        pytest.raises(RuntimeError, match="requires the 'litellm' package"),
    ):
        _create_model(s)


# ---------------------------------------------------------------------------
# Environment variable integration
# ---------------------------------------------------------------------------


def test_settings_reads_llm_provider_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "litellm")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o")
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.llm_provider == "litellm"
        assert s.llm_model == "openai/gpt-4o"
    finally:
        os.environ.pop("LLM_PROVIDER", None)
        os.environ.pop("LLM_MODEL", None)
        get_settings.cache_clear()


def test_settings_reads_llm_model_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-2.0-flash")
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.llm_model == "gemini-2.0-flash"
    finally:
        os.environ.pop("LLM_MODEL", None)
        get_settings.cache_clear()


def test_settings_reads_llm_api_key_env(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-123")
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.llm_api_key == "sk-test-123"
    finally:
        os.environ.pop("LLM_API_KEY", None)
        get_settings.cache_clear()


def test_settings_reads_llm_api_base_env(monkeypatch):
    monkeypatch.setenv("LLM_API_BASE", "http://localhost:8080/v1")
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.llm_api_base == "http://localhost:8080/v1"
    finally:
        os.environ.pop("LLM_API_BASE", None)
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Service account credentials
# ---------------------------------------------------------------------------


def test_google_application_credentials_defaults_to_none():
    s = Settings(google_api_key="test-key")
    assert s.google_application_credentials is None


def test_google_application_credentials_accepts_path():
    s = Settings(google_api_key="test-key", google_application_credentials="/path/to/sa.json")
    assert s.google_application_credentials == "/path/to/sa.json"


def test_setup_environment_sets_google_application_credentials(monkeypatch, tmp_path):
    from lightspeed_agent.core.agent import _setup_environment

    creds_file = tmp_path / "sa-key.json"
    creds_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(creds_file))
    get_settings.cache_clear()
    try:
        _setup_environment()
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(creds_file)
    finally:
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        get_settings.cache_clear()


def test_setup_environment_warns_when_credentials_file_missing(monkeypatch, caplog):
    from lightspeed_agent.core.agent import _setup_environment

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/path/sa.json")
    get_settings.cache_clear()
    try:
        _setup_environment()
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/nonexistent/path/sa.json"
        assert "GOOGLE_APPLICATION_CREDENTIALS path does not exist" in caplog.text
    finally:
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        get_settings.cache_clear()


def test_setup_environment_skips_google_application_credentials_when_not_set(monkeypatch):
    from lightspeed_agent.core.agent import _setup_environment

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    get_settings.cache_clear()
    try:
        _setup_environment()
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ
    finally:
        get_settings.cache_clear()


def test_setup_environment_sets_vertexai_env_vars(monkeypatch):
    """Test that VERTEXAI_PROJECT and VERTEXAI_LOCATION are set for LiteLLM."""
    from lightspeed_agent.core.agent import _setup_environment

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    get_settings.cache_clear()
    try:
        _setup_environment()
        assert os.environ["VERTEXAI_PROJECT"] == "my-project"
        assert os.environ["VERTEXAI_LOCATION"] == "global"
    finally:
        os.environ.pop("VERTEXAI_PROJECT", None)
        os.environ.pop("VERTEXAI_LOCATION", None)
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        os.environ.pop("GOOGLE_CLOUD_LOCATION", None)
        get_settings.cache_clear()


def test_settings_reads_google_application_credentials_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/opt/keys/sa.json")
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.google_application_credentials == "/opt/keys/sa.json"
    finally:
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        get_settings.cache_clear()
