"""Tests for DB-backed usage metering repository."""

from datetime import UTC, datetime, timedelta

import pytest

from lightspeed_agent.metering.repository import UsageRepository


class TestUsageRepository:
    """Tests for DB-backed usage aggregation and reporting state."""

    @pytest.mark.asyncio
    async def test_increment_and_aggregate_usage_by_order(self, db_session):
        """Aggregates persisted increments by order."""
        repo = UsageRepository()

        await repo.increment_usage(
            order_id="order-1",
            request_count=1,
            input_tokens=10,
            output_tokens=5,
            tool_calls=2,
        )
        await repo.increment_usage(
            order_id="order-1",
            request_count=3,
            input_tokens=7,
            output_tokens=8,
            tool_calls=1,
        )

        usage = await repo.get_usage_by_order()
        assert "order-1" in usage
        assert usage["order-1"]["total_requests"] == 4
        assert usage["order-1"]["total_input_tokens"] == 17
        assert usage["order-1"]["total_output_tokens"] == 13
        assert usage["order-1"]["total_tokens"] == 30
        assert usage["order-1"]["total_tool_calls"] == 3

    @pytest.mark.asyncio
    async def test_get_unreported_and_mark_reported_for_period(self, db_session):
        """Returns unreported metrics and clears them after marking reported."""
        repo = UsageRepository()

        await repo.increment_usage(
            order_id="order-2",
            request_count=2,
            input_tokens=20,
            output_tokens=9,
            tool_calls=4,
        )

        now = datetime.now(UTC)
        # Use a broad window around "now" to ensure the current-hour bucket
        # [period_start, period_end) is always included.
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=2)

        metrics = await repo.get_unreported_usage(
            order_id="order-2",
            start_time=start,
            end_time=end,
        )
        assert metrics["send_message_requests"] == 2
        assert metrics["input_tokens"] == 20
        assert metrics["output_tokens"] == 9
        assert metrics["mcp_tool_calls"] == 4

        marked = await repo.mark_reported_for_period(
            order_id="order-2",
            start_time=start,
            end_time=end,
            reported_at=now,
        )
        assert marked == 1

        metrics_after = await repo.get_unreported_usage(
            order_id="order-2",
            start_time=start,
            end_time=end,
        )
        assert metrics_after == {}

