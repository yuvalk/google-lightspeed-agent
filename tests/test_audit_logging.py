"""Tests for audit logging: context filter, contextvars, and enhanced plugin events."""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from lightspeed_agent.api.a2a.logging_plugin import AgentLoggingPlugin
from lightspeed_agent.auth.middleware import (
    _request_access_token,
    _request_id,
    _request_order_id,
    _request_org_id,
    _request_user_id,
    get_request_id,
    get_request_org_id,
    get_request_user_id,
)
from lightspeed_agent.logging.filters import AuditContextFilter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_audit_contextvars():
    """Reset all audit contextvars before and after each test."""
    tokens = [
        _request_user_id.set(None),
        _request_org_id.set(None),
        _request_order_id.set(None),
        _request_id.set(None),
        _request_access_token.set(None),
    ]
    yield
    for var, tok in zip(
        [_request_user_id, _request_org_id, _request_order_id, _request_id, _request_access_token],
        tokens,
        strict=False,
    ):
        var.reset(tok)


def _set_audit_context(
    user_id: str = "test-user",
    org_id: str = "test-org",
    order_id: str = "test-order",
    request_id: str = "test-request-id",
) -> None:
    """Populate audit contextvars for testing."""
    _request_user_id.set(user_id)
    _request_org_id.set(org_id)
    _request_order_id.set(order_id)
    _request_id.set(request_id)


# ===========================================================================
# AuditContextFilter tests
# ===========================================================================


class TestAuditContextFilter:
    """Tests for the AuditContextFilter logging filter."""

    def _make_record(self, msg: str = "hello") -> logging.LogRecord:
        return logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_filter_injects_fields_when_context_set(self):
        """Audit fields appear on log record when contextvars are populated."""
        _set_audit_context(
            user_id="user-42",
            org_id="org-7",
            order_id="order-99",
            request_id="req-abc",
        )

        f = AuditContextFilter()
        record = self._make_record()
        result = f.filter(record)

        assert result is True
        assert record.user_id == "user-42"  # type: ignore[attr-defined]
        assert record.org_id == "org-7"  # type: ignore[attr-defined]
        assert record.order_id == "order-99"  # type: ignore[attr-defined]
        assert record.request_id == "req-abc"  # type: ignore[attr-defined]

    def test_filter_defaults_to_empty_string_when_no_context(self):
        """Audit fields default to empty string when contextvars are unset."""
        f = AuditContextFilter()
        record = self._make_record()
        f.filter(record)

        assert record.user_id == ""  # type: ignore[attr-defined]
        assert record.org_id == ""  # type: ignore[attr-defined]
        assert record.order_id == ""  # type: ignore[attr-defined]
        assert record.request_id == ""  # type: ignore[attr-defined]

    def test_filter_always_returns_true(self):
        """Filter enriches but never suppresses log records."""
        f = AuditContextFilter()
        record = self._make_record()
        assert f.filter(record) is True

    def test_filter_partial_context(self):
        """Only populated fields are non-empty; others default to empty."""
        _request_user_id.set("user-only")
        f = AuditContextFilter()
        record = self._make_record()
        f.filter(record)

        assert record.user_id == "user-only"  # type: ignore[attr-defined]
        assert record.org_id == ""  # type: ignore[attr-defined]
        assert record.order_id == ""  # type: ignore[attr-defined]
        assert record.request_id == ""  # type: ignore[attr-defined]


# ===========================================================================
# ContextVar getter tests
# ===========================================================================


class TestContextVarGetters:
    """Tests for the audit contextvar getter functions."""

    def test_get_request_user_id_returns_none_by_default(self):
        assert get_request_user_id() is None

    def test_get_request_org_id_returns_none_by_default(self):
        assert get_request_org_id() is None

    def test_get_request_id_returns_none_by_default(self):
        assert get_request_id() is None

    def test_get_request_user_id_returns_set_value(self):
        _request_user_id.set("u-1")
        assert get_request_user_id() == "u-1"

    def test_get_request_org_id_returns_set_value(self):
        _request_org_id.set("o-1")
        assert get_request_org_id() == "o-1"

    def test_get_request_id_returns_set_value(self):
        _request_id.set("r-1")
        assert get_request_id() == "r-1"


# ===========================================================================
# AgentLoggingPlugin audit event tests
# ===========================================================================


class TestAgentLoggingPluginAuditEvents:
    """Tests for event_type classification and audit context in logging plugin."""

    @pytest.fixture
    def plugin(self):
        with patch(
            "lightspeed_agent.api.a2a.logging_plugin.get_settings"
        ) as mock_settings:
            settings = MagicMock()
            settings.agent_logging_detail = "basic"
            mock_settings.return_value = settings
            yield AgentLoggingPlugin()

    @pytest.fixture(autouse=True)
    def _populate_audit_context(self):
        """Populate audit contextvars for plugin test assertions."""
        _set_audit_context()

    @pytest.mark.asyncio
    async def test_before_run_includes_event_type_and_audit_fields(self, plugin, caplog):
        ctx = MagicMock()
        ctx.invocation_id = "inv-1"
        ctx.agent = MagicMock(name="test_agent")

        with caplog.at_level(logging.INFO):
            await plugin.before_run_callback(invocation_context=ctx)

        assert "event_type=agent_run_started" in caplog.text
        assert "user_id=test-user" in caplog.text
        assert "org_id=test-org" in caplog.text
        assert "order_id=test-order" in caplog.text
        assert "request_id=test-request-id" in caplog.text
        assert "inv-1" in caplog.text

    @pytest.mark.asyncio
    async def test_after_run_includes_event_type(self, plugin, caplog):
        ctx = MagicMock()
        ctx.invocation_id = "inv-2"

        with caplog.at_level(logging.INFO):
            await plugin.after_run_callback(invocation_context=ctx)

        assert "event_type=agent_run_completed" in caplog.text
        assert "inv-2" in caplog.text
        assert "user_id=test-user" in caplog.text

    @pytest.mark.asyncio
    async def test_before_model_includes_event_type(self, plugin, caplog):
        ctx = MagicMock()
        ctx.agent_name = "test_agent"

        with caplog.at_level(logging.INFO):
            await plugin.before_model_callback(
                callback_context=ctx, llm_request=MagicMock()
            )

        assert "event_type=llm_call_started" in caplog.text
        assert "user_id=test-user" in caplog.text

    @pytest.mark.asyncio
    async def test_after_model_includes_event_type_and_tokens(self, plugin, caplog):
        ctx = MagicMock()
        llm_response = MagicMock()
        llm_response.usage_metadata = MagicMock(
            prompt_token_count=100, candidates_token_count=50
        )
        llm_response.model_version = "gemini-2.5-flash-001"

        with caplog.at_level(logging.INFO):
            await plugin.after_model_callback(
                callback_context=ctx, llm_response=llm_response
            )

        assert "event_type=llm_call_completed" in caplog.text
        assert "input_tokens=100" in caplog.text
        assert "output_tokens=50" in caplog.text
        assert "user_id=test-user" in caplog.text

    @pytest.mark.asyncio
    async def test_on_model_error_includes_event_type(self, plugin, caplog):
        ctx = MagicMock()
        error = RuntimeError("API quota exceeded")

        with caplog.at_level(logging.ERROR):
            await plugin.on_model_error_callback(
                callback_context=ctx, llm_request=MagicMock(), error=error
            )

        assert "event_type=llm_call_failed" in caplog.text
        assert "API quota exceeded" in caplog.text
        assert "user_id=test-user" in caplog.text

    @pytest.mark.asyncio
    async def test_before_tool_includes_event_type(self, plugin, caplog):
        tool = MagicMock()
        tool.name = "get_advisories"

        with caplog.at_level(logging.INFO):
            await plugin.before_tool_callback(
                tool=tool,
                tool_args={"system_id": "abc-123"},
                tool_context=MagicMock(),
            )

        assert "event_type=tool_call_started" in caplog.text
        assert "get_advisories" in caplog.text
        assert "user_id=test-user" in caplog.text

    @pytest.mark.asyncio
    async def test_after_tool_includes_event_type_and_data_source(self, plugin, caplog):
        tool = MagicMock()
        tool.name = "get_advisories"

        with caplog.at_level(logging.INFO):
            await plugin.after_tool_callback(
                tool=tool,
                tool_args={},
                tool_context=MagicMock(),
                result={"data": "test"},
            )

        assert "event_type=tool_call_completed" in caplog.text
        assert "data_source=get_advisories" in caplog.text
        assert "user_id=test-user" in caplog.text

    @pytest.mark.asyncio
    async def test_on_tool_error_includes_event_type(self, plugin, caplog):
        tool = MagicMock()
        tool.name = "get_advisories"
        error = ConnectionError("MCP server unreachable")

        with caplog.at_level(logging.ERROR):
            await plugin.on_tool_error_callback(
                tool=tool,
                tool_args={},
                tool_context=MagicMock(),
                error=error,
            )

        assert "event_type=tool_call_failed" in caplog.text
        assert "get_advisories" in caplog.text
        assert "MCP server unreachable" in caplog.text
        assert "user_id=test-user" in caplog.text


class TestAgentLoggingPluginDetailedAudit:
    """Tests for detailed mode with audit context."""

    @pytest.fixture
    def plugin(self):
        with patch(
            "lightspeed_agent.api.a2a.logging_plugin.get_settings"
        ) as mock_settings:
            settings = MagicMock()
            settings.agent_logging_detail = "detailed"
            mock_settings.return_value = settings
            yield AgentLoggingPlugin()

    @pytest.fixture(autouse=True)
    def _populate_audit_context(self):
        _set_audit_context()

    @pytest.mark.asyncio
    async def test_before_tool_detailed_includes_args_and_audit(self, plugin, caplog):
        tool = MagicMock()
        tool.name = "get_advisories"

        with caplog.at_level(logging.INFO):
            await plugin.before_tool_callback(
                tool=tool,
                tool_args={"system_id": "abc-123"},
                tool_context=MagicMock(),
            )

        assert "event_type=tool_call_started" in caplog.text
        assert "abc-123" in caplog.text
        assert "user_id=test-user" in caplog.text

    @pytest.mark.asyncio
    async def test_after_tool_detailed_includes_result_and_data_source(self, plugin, caplog):
        tool = MagicMock()
        tool.name = "get_advisories"

        with caplog.at_level(logging.INFO):
            await plugin.after_tool_callback(
                tool=tool,
                tool_args={},
                tool_context=MagicMock(),
                result={"advisories": ["CVE-2024-1234"]},
            )

        assert "event_type=tool_call_completed" in caplog.text
        assert "data_source=get_advisories" in caplog.text
        assert "CVE-2024-1234" in caplog.text
        assert "user_id=test-user" in caplog.text


# ===========================================================================
# MCP header forwarding audit tests
# ===========================================================================


class TestMCPHeaderAuditLogging:
    """Tests for audit logging in MCP header provider."""

    def test_header_provider_logs_jwt_forwarding(self, caplog):
        """JWT forwarding emits audit log with user context."""
        _set_audit_context(user_id="user-1", org_id="org-1", request_id="req-1")
        token_exp = datetime.now(UTC) + timedelta(hours=1)
        _request_access_token.set(("test-token", token_exp))

        from lightspeed_agent.tools.mcp_headers import create_mcp_header_provider

        provider = create_mcp_header_provider()
        with caplog.at_level(logging.INFO):
            headers = provider(MagicMock())

        assert headers == {"Authorization": "Bearer test-token"}
        assert "event_type=mcp_jwt_forwarded" in caplog.text
        assert "user_id=user-1" in caplog.text
        assert "org_id=org-1" in caplog.text
        assert "request_id=req-1" in caplog.text

    def test_header_provider_does_not_log_token(self, caplog):
        """JWT token value must never appear in audit logs."""
        _set_audit_context()
        token_exp = datetime.now(UTC) + timedelta(hours=1)
        _request_access_token.set(("super-secret-jwt-token", token_exp))

        from lightspeed_agent.tools.mcp_headers import create_mcp_header_provider

        provider = create_mcp_header_provider()
        with caplog.at_level(logging.INFO):
            provider(MagicMock())

        assert "super-secret-jwt-token" not in caplog.text

    def test_header_provider_warns_when_no_token(self, caplog):
        """Warning log when no token is available."""
        from lightspeed_agent.tools.mcp_headers import create_mcp_header_provider

        provider = create_mcp_header_provider()
        with caplog.at_level(logging.WARNING):
            headers = provider(MagicMock())

        assert headers == {}
        assert "No MCP credentials available" in caplog.text


# ===========================================================================
# All callbacks return None tests (unchanged behavior)
# ===========================================================================


class TestAllCallbacksReturnNone:
    """Verify all callbacks return None (no data modification)."""

    @pytest.fixture
    def plugin(self):
        with patch(
            "lightspeed_agent.api.a2a.logging_plugin.get_settings"
        ) as mock_settings:
            settings = MagicMock()
            settings.agent_logging_detail = "basic"
            mock_settings.return_value = settings
            yield AgentLoggingPlugin()

    @pytest.mark.asyncio
    async def test_all_callbacks_return_none(self, plugin):
        ctx = MagicMock()
        ctx.invocation_id = "inv-1"
        ctx.agent = MagicMock(name="agent")

        tool = MagicMock()
        tool.name = "test_tool"

        llm_response = MagicMock()
        llm_response.usage_metadata = None
        llm_response.model_version = None
        llm_response.content = None

        assert await plugin.before_run_callback(invocation_context=ctx) is None
        assert await plugin.after_run_callback(invocation_context=ctx) is None
        assert (
            await plugin.before_model_callback(
                callback_context=ctx, llm_request=MagicMock()
            )
            is None
        )
        assert (
            await plugin.after_model_callback(
                callback_context=ctx, llm_response=llm_response
            )
            is None
        )
        assert (
            await plugin.on_model_error_callback(
                callback_context=ctx, llm_request=MagicMock(), error=RuntimeError("test")
            )
            is None
        )
        assert (
            await plugin.before_tool_callback(
                tool=tool, tool_args={}, tool_context=MagicMock()
            )
            is None
        )
        assert (
            await plugin.after_tool_callback(
                tool=tool, tool_args={}, tool_context=MagicMock(), result={}
            )
            is None
        )
        assert (
            await plugin.on_tool_error_callback(
                tool=tool,
                tool_args={},
                tool_context=MagicMock(),
                error=RuntimeError("test"),
            )
            is None
        )
