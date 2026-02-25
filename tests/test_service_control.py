"""Tests for Google Cloud Service Control integration."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lightspeed_agent.service_control.models import (
    CheckError,
    CheckErrorCode,
    CheckResponse,
    ReportResponse,
    UsageReport,
)
from lightspeed_agent.service_control.reporter import ReportResult, UsageReporter
from lightspeed_agent.service_control.scheduler import ReportingScheduler


class TestModels:
    """Tests for Service Control data models."""

    def test_check_error(self):
        """Test CheckError model."""
        error = CheckError(
            code=CheckErrorCode.SERVICE_NOT_ACTIVATED,
            detail="Service not activated for consumer",
        )

        assert error.code == CheckErrorCode.SERVICE_NOT_ACTIVATED
        assert "not activated" in error.detail

    def test_check_response_valid(self):
        """Test valid CheckResponse."""
        response = CheckResponse(
            operation_id="op-123",
            check_errors=[],
        )

        assert response.is_valid is True
        assert response.should_block_service is False

    def test_check_response_with_blocking_error(self):
        """Test CheckResponse with blocking error."""
        response = CheckResponse(
            operation_id="op-123",
            check_errors=[
                CheckError(
                    code=CheckErrorCode.BILLING_DISABLED,
                    detail="Billing disabled",
                )
            ],
        )

        assert response.is_valid is False
        assert response.should_block_service is True

    def test_check_response_with_non_blocking_error(self):
        """Test CheckResponse with non-blocking error."""
        response = CheckResponse(
            operation_id="op-123",
            check_errors=[
                CheckError(
                    code=CheckErrorCode.IP_ADDRESS_BLOCKED,
                    detail="IP blocked",
                )
            ],
        )

        assert response.is_valid is False
        assert response.should_block_service is False

    def test_report_response_success(self):
        """Test successful ReportResponse."""
        response = ReportResponse(
            report_errors=[],
            service_config_id="config-123",
        )

        assert response.is_success is True

    def test_report_response_with_errors(self):
        """Test ReportResponse with errors."""
        response = ReportResponse(
            report_errors=[{"operation_id": "op-123", "error": "failed"}],
        )

        assert response.is_success is False

    def test_usage_report(self):
        """Test UsageReport model."""
        now = datetime.utcnow()
        report = UsageReport(
            order_id="order-123",
            consumer_id="project:test-project",
            start_time=now - timedelta(hours=1),
            end_time=now,
            metrics={"api_calls": 100, "input_tokens": 5000},
        )

        assert report.order_id == "order-123"
        assert report.metrics["api_calls"] == 100
        assert report.reported is False
        assert report.retry_count == 0


class TestUsageReporter:
    """Tests for UsageReporter.

    Note: UsageReporter uses per-order deltas from UsageTrackingPlugin.
    """

    @pytest.fixture
    def mock_client(self):
        """Create mock Service Control client."""
        client = MagicMock()
        client.check_and_report = AsyncMock(return_value=(True, None))
        return client

    @pytest.fixture
    def reporter(self, mock_client):
        """Create reporter with mocked dependencies."""
        return UsageReporter(
            service_control_client=mock_client,
        )

    def test_map_metrics(self, reporter):
        """Test metric mapping."""
        internal = {
            "api_calls": 100,
            "input_tokens": 5000,
            "unknown_metric": 10,
        }

        mapped = reporter.map_metrics(internal)

        assert mapped["api_calls"] == 100
        assert mapped["input_tokens"] == 5000
        assert "unknown_metric" not in mapped

    def test_map_metrics_skips_zero_values(self, reporter):
        """Test that zero values are skipped."""
        internal = {
            "api_calls": 100,
            "input_tokens": 0,
        }

        mapped = reporter.map_metrics(internal)

        assert "api_calls" in mapped
        assert "input_tokens" not in mapped

    @pytest.mark.asyncio
    async def test_report_usage_success(self, reporter, mock_client):
        """Test successful usage report."""
        # Mock get_consumer_id and _get_usage_delta
        with patch.object(
            reporter, "get_consumer_id", return_value="project:test-project"
        ), patch.object(
            reporter,
            "_get_usage_delta",
            new=AsyncMock(return_value={"send_message_requests": 10}),
        ):
            now = datetime.utcnow()
            result = await reporter.report_usage(
                order_id="order-123",
                start_time=now - timedelta(hours=1),
                end_time=now,
            )

            assert result.success is True
            assert result.order_id == "order-123"
            assert result.consumer_id == "project:test-project"

    @pytest.mark.asyncio
    async def test_report_usage_no_consumer_id(self, reporter):
        """Test report fails when consumer ID not found."""
        with patch.object(reporter, "get_consumer_id", return_value=None):
            now = datetime.utcnow()
            result = await reporter.report_usage(
                order_id="order-123",
                start_time=now - timedelta(hours=1),
                end_time=now,
            )

            assert result.success is False
            assert "consumer ID" in result.error_message

    @pytest.mark.asyncio
    async def test_report_usage_queues_failed(self, reporter, mock_client):
        """Test that failed reports are queued for retry."""
        mock_client.check_and_report = AsyncMock(
            return_value=(False, "Service unavailable")
        )

        with patch.object(
            reporter, "get_consumer_id", return_value="project:test-project"
        ), patch.object(
            reporter,
            "_get_usage_delta",
            new=AsyncMock(return_value={"send_message_requests": 10}),
        ):
            now = datetime.utcnow()
            result = await reporter.report_usage(
                order_id="order-123",
                start_time=now - timedelta(hours=1),
                end_time=now,
            )

            assert result.success is False
            assert reporter.get_failed_reports_count() == 1

    @pytest.mark.asyncio
    async def test_report_all_usage(self, reporter):
        """Test reporting for all orders."""
        with patch.object(
            reporter, "get_consumer_id", return_value="project:test-project"
        ), patch.object(
            reporter, "_get_active_order_ids", return_value=["order-123", "order-456"]
        ), patch.object(
            reporter,
            "_get_usage_delta",
            new=AsyncMock(return_value={"send_message_requests": 10}),
        ):
            now = datetime.utcnow()
            results = await reporter.report_all_usage(
                start_time=now - timedelta(hours=1),
                end_time=now,
            )

            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_retry_failed_reports(self, reporter, mock_client):
        """Test retrying failed reports."""
        # Queue a failed report
        reporter._failed_reports.append(
            UsageReport(
                order_id="order-123",
                consumer_id="project:test-project",
                start_time=datetime.utcnow() - timedelta(hours=1),
                end_time=datetime.utcnow(),
                metrics={"api_calls": 100},
                retry_count=0,
            )
        )

        with patch.object(
            reporter, "get_consumer_id", return_value="project:test-project"
        ):
            results = await reporter.retry_failed_reports()

            assert len(results) == 1
            assert results[0].success is True
            assert reporter.get_failed_reports_count() == 0

    @pytest.mark.asyncio
    async def test_retry_gives_up_after_max_retries(self, reporter, mock_client):
        """Test that retry gives up after max attempts."""
        mock_client.check_and_report = AsyncMock(
            return_value=(False, "Still failing")
        )

        # Queue a report that has exceeded max retries
        reporter._failed_reports.append(
            UsageReport(
                order_id="order-123",
                consumer_id="project:test-project",
                start_time=datetime.utcnow() - timedelta(hours=1),
                end_time=datetime.utcnow(),
                metrics={"api_calls": 100},
                retry_count=3,  # Already at max
            )
        )

        with patch.object(
            reporter, "get_consumer_id", return_value="project:test-project"
        ):
            results = await reporter.retry_failed_reports()

            # Should not retry, report should be dropped
            assert len(results) == 0
            assert reporter.get_failed_reports_count() == 0


class TestReportingScheduler:
    """Tests for ReportingScheduler."""

    @pytest.fixture
    def mock_reporter(self):
        """Create mock reporter."""
        reporter = MagicMock()
        reporter.report_hourly = AsyncMock(return_value=[])
        reporter.retry_failed_reports = AsyncMock(return_value=[])
        reporter.get_failed_reports_count = MagicMock(return_value=0)
        reporter.get_reporting_stats = MagicMock(
            return_value={
                "failed_reports_pending": 0,
                "orders_tracked": 5,
                "last_report_times": {},
            }
        )
        return reporter

    @pytest.fixture
    def scheduler(self, mock_reporter):
        """Create scheduler with mock reporter."""
        return ReportingScheduler(
            reporter=mock_reporter,
            hourly_interval_seconds=3600,
            retry_interval_seconds=300,
        )

    def test_initial_state(self, scheduler):
        """Test scheduler initial state."""
        assert scheduler.is_running is False

    def test_get_status(self, scheduler):
        """Test getting scheduler status."""
        status = scheduler.get_status()

        assert status["running"] is False
        assert status["hourly_interval_seconds"] == 3600
        assert status["retry_interval_seconds"] == 300
        assert status["orders_tracked"] == 5

    @pytest.mark.asyncio
    async def test_start_stop(self, scheduler):
        """Test starting and stopping scheduler."""
        await scheduler.start()
        assert scheduler.is_running is True

        await scheduler.stop()
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_run_immediate_report(self, scheduler, mock_reporter):
        """Test running immediate report."""
        await scheduler.run_immediate_report()

        mock_reporter.report_hourly.assert_called_once()

    def test_set_failure_callback(self, scheduler):
        """Test setting failure callback."""
        callback = MagicMock()
        scheduler.set_failure_callback(callback)

        assert scheduler._on_report_failure is callback
