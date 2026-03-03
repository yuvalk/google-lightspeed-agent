"""Tests for DB-backed usage metering repository."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from lightspeed_agent.db import UsageRecordModel, get_session
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
    async def test_claim_then_mark_reported_by_ids(self, db_session):
        """Claim rows, report, then mark by IDs."""
        repo = UsageRepository()

        await repo.increment_usage(
            order_id="order-3",
            request_count=5,
            input_tokens=100,
            output_tokens=50,
            tool_calls=2,
        )

        now = datetime.now(UTC)
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=2)

        claimed = await repo.claim_unreported_rows_for_reporting(
            order_id="order-3",
            start_time=start,
            end_time=end,
        )
        assert len(claimed) == 1
        assert claimed[0].request_count == 5
        claimed_ids = [r.id for r in claimed]

        # After claim, re-claim returns empty (rows are claimed by us)
        re_claimed = await repo.claim_unreported_rows_for_reporting(
            order_id="order-3",
            start_time=start,
            end_time=end,
        )
        assert re_claimed == []

        marked = await repo.mark_reported_by_ids(
            ids=claimed_ids,
            reported_at=now,
        )
        assert marked == 1

        # Rows are now reported; claim returns empty
        after_mark = await repo.claim_unreported_rows_for_reporting(
            order_id="order-3",
            start_time=start,
            end_time=end,
        )
        assert after_mark == []

    @pytest.mark.asyncio
    async def test_claim_then_release(self, db_session):
        """Claim rows, report fails, release - rows become available again."""
        repo = UsageRepository()

        await repo.increment_usage(
            order_id="order-4",
            request_count=3,
            input_tokens=50,
        )

        now = datetime.now(UTC)
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=2)

        claimed = await repo.claim_unreported_rows_for_reporting(
            order_id="order-4",
            start_time=start,
            end_time=end,
        )
        assert len(claimed) == 1
        claimed_ids = [r.id for r in claimed]

        released = await repo.release_claimed_rows(ids=claimed_ids)
        assert released == 1

        # After release, rows can be claimed again
        re_claimed = await repo.claim_unreported_rows_for_reporting(
            order_id="order-4",
            start_time=start,
            end_time=end,
        )
        assert len(re_claimed) == 1
        assert re_claimed[0].request_count == 3
        assert re_claimed[0].input_tokens == 50

    @pytest.mark.asyncio
    async def test_release_stale_claimed_rows(self, db_session):
        """Release rows claimed longer than threshold (worker crash recovery)."""
        repo = UsageRepository()

        await repo.increment_usage(
            order_id="order-5",
            request_count=2,
            input_tokens=20,
        )

        now = datetime.now(UTC)
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=2)

        claimed = await repo.claim_unreported_rows_for_reporting(
            order_id="order-5",
            start_time=start,
            end_time=end,
        )
        assert len(claimed) == 1
        claimed_ids = [r.id for r in claimed]

        # Simulate stale claim: set reporting_started_at to 20 minutes ago
        stale_threshold = now - timedelta(minutes=20)
        async with get_session() as session:
            await session.execute(
                update(UsageRecordModel)
                .where(UsageRecordModel.id.in_(claimed_ids))
                .values(reporting_started_at=stale_threshold)
            )

        # Release stale (older than 15 min)
        released = await repo.release_stale_claimed_rows(older_than_minutes=15)
        assert released == 1

        # Rows can be claimed again
        re_claimed = await repo.claim_unreported_rows_for_reporting(
            order_id="order-5",
            start_time=start,
            end_time=end,
        )
        assert len(re_claimed) == 1

    @pytest.mark.asyncio
    async def test_get_unreported_periods(self, db_session):
        """Return distinct unreported periods for backfill."""
        repo = UsageRepository()

        # Create usage in two periods
        base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        period1_start = base - timedelta(hours=2)
        period1_end = period1_start + timedelta(hours=1)
        period2_start = base - timedelta(hours=1)
        period2_end = period2_start + timedelta(hours=1)

        # Insert directly to control period (increment_usage uses current hour)
        async with get_session() as session:
            session.add(
                UsageRecordModel(
                    order_id="order-6",
                    request_count=1,
                    input_tokens=10,
                    output_tokens=5,
                    tool_calls=0,
                    period_start=period1_start,
                    period_end=period1_end,
                    reported=False,
                )
            )
            session.add(
                UsageRecordModel(
                    order_id="order-6",
                    request_count=2,
                    input_tokens=20,
                    output_tokens=10,
                    tool_calls=1,
                    period_start=period2_start,
                    period_end=period2_end,
                    reported=False,
                )
            )
            # Session commits on exit

        # older_than = current hour start; both periods are older
        older_than = base
        periods = await repo.get_unreported_periods(
            older_than=older_than,
            max_age_hours=168,
            limit=10,
        )
        assert len(periods) == 2
        # Ordered by period_end ASC
        assert periods[0] == ("order-6", period1_start, period1_end)
        assert periods[1] == ("order-6", period2_start, period2_end)

