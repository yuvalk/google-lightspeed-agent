"""Tests for usage tracking plugin persistence behavior."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from lightspeed_agent.api.a2a import usage_plugin


class TestUsageTrackingPlugin:
    """Tests for persistence behavior in usage plugin callbacks."""

    @pytest.mark.asyncio
    async def test_before_run_persists_request_increment_when_order_present(self):
        """Persist request_count=1 for a valid request order."""
        repo = MagicMock()
        repo.increment_usage = AsyncMock()
        original_get_repo = usage_plugin.get_usage_repository
        original_get_order = usage_plugin.get_request_order_id
        usage_plugin.get_usage_repository = lambda: repo
        usage_plugin.get_request_order_id = lambda: "order-123"
        try:
            plugin = usage_plugin.UsageTrackingPlugin()
            await plugin.before_run_callback(invocation_context=None)
        finally:
            usage_plugin.get_usage_repository = original_get_repo
            usage_plugin.get_request_order_id = original_get_order

        repo.increment_usage.assert_awaited_once_with(
            order_id="order-123",
            request_count=1,
            input_tokens=0,
            output_tokens=0,
            tool_calls=0,
        )

    @pytest.mark.asyncio
    async def test_before_run_skips_persistence_when_order_missing(self):
        """Do not persist increments if request context has no order."""
        repo = MagicMock()
        repo.increment_usage = AsyncMock()
        original_get_repo = usage_plugin.get_usage_repository
        original_get_order = usage_plugin.get_request_order_id
        usage_plugin.get_usage_repository = lambda: repo
        usage_plugin.get_request_order_id = lambda: None
        try:
            plugin = usage_plugin.UsageTrackingPlugin()
            await plugin.before_run_callback(invocation_context=None)
        finally:
            usage_plugin.get_usage_repository = original_get_repo
            usage_plugin.get_request_order_id = original_get_order

        repo.increment_usage.assert_not_called()

    @pytest.mark.asyncio
    async def test_after_model_persists_input_output_tokens(self):
        """Persist LLM token counts for a valid order."""
        repo = MagicMock()
        repo.increment_usage = AsyncMock()
        llm_response = MagicMock()
        llm_response.usage_metadata = MagicMock(
            prompt_token_count=11, candidates_token_count=7
        )
        original_get_repo = usage_plugin.get_usage_repository
        original_get_order = usage_plugin.get_request_order_id
        usage_plugin.get_usage_repository = lambda: repo
        usage_plugin.get_request_order_id = lambda: "order-abc"
        try:
            plugin = usage_plugin.UsageTrackingPlugin()
            await plugin.after_model_callback(callback_context=None, llm_response=llm_response)
        finally:
            usage_plugin.get_usage_repository = original_get_repo
            usage_plugin.get_request_order_id = original_get_order

        repo.increment_usage.assert_awaited_once_with(
            order_id="order-abc",
            request_count=0,
            input_tokens=11,
            output_tokens=7,
            tool_calls=0,
        )

    @pytest.mark.asyncio
    async def test_after_tool_persists_tool_call_increment(self):
        """Persist tool_calls=1 for tool callback events."""
        repo = MagicMock()
        repo.increment_usage = AsyncMock()
        original_get_repo = usage_plugin.get_usage_repository
        original_get_order = usage_plugin.get_request_order_id
        usage_plugin.get_usage_repository = lambda: repo
        usage_plugin.get_request_order_id = lambda: "order-tools"
        tool = MagicMock()
        tool.name = "test_tool"
        try:
            plugin = usage_plugin.UsageTrackingPlugin()
            await plugin.after_tool_callback(
                tool=tool,
                tool_args={},
                tool_context=None,
                result={},
            )
        finally:
            usage_plugin.get_usage_repository = original_get_repo
            usage_plugin.get_request_order_id = original_get_order

        repo.increment_usage.assert_awaited_once_with(
            order_id="order-tools",
            request_count=0,
            input_tokens=0,
            output_tokens=0,
            tool_calls=1,
        )

