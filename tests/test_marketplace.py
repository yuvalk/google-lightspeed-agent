"""Tests for Marketplace Procurement integration."""

from unittest.mock import patch

import pytest

from lightspeed_agent.marketplace.models import (
    Account,
    AccountState,
    Entitlement,
    EntitlementState,
    EntitlementInfo,
    ProcurementEvent,
    ProcurementEventType,
)
from lightspeed_agent.marketplace.repository import (
    AccountRepository,
    EntitlementRepository,
)
from lightspeed_agent.marketplace.service import ProcurementService


class TestModels:
    """Tests for marketplace data models."""

    def test_procurement_event_parsing(self):
        """Test parsing a procurement event."""
        event_data = {
            "eventId": "event-123",
            "eventType": "ENTITLEMENT_ACTIVE",
            "providerId": "provider-123",
            "entitlement": {
                "id": "entitlement-456",
                "updateTime": "2024-01-01T00:00:00Z",
            },
        }

        event = ProcurementEvent(**event_data)

        assert event.event_id == "event-123"
        assert event.event_type == ProcurementEventType.ENTITLEMENT_ACTIVE
        assert event.provider_id == "provider-123"
        assert event.entitlement.id == "entitlement-456"

    def test_account_event_parsing(self):
        """Test parsing an account event."""
        event_data = {
            "eventId": "event-789",
            "eventType": "ACCOUNT_ACTIVE",
            "providerId": "provider-123",
            "account": {
                "id": "account-456",
            },
        }

        event = ProcurementEvent(**event_data)

        assert event.event_type == ProcurementEventType.ACCOUNT_ACTIVE
        assert event.account.id == "account-456"

    def test_all_event_types_valid(self):
        """Test all event types are valid enum values."""
        event_types = [
            "ACCOUNT_ACTIVE",
            "ACCOUNT_DELETED",
            "ENTITLEMENT_CREATION_REQUESTED",
            "ENTITLEMENT_ACTIVE",
            "ENTITLEMENT_CANCELLED",
        ]

        for event_type in event_types:
            assert ProcurementEventType(event_type) is not None

    def test_entitlement_info_with_product(self):
        """Test EntitlementInfo includes product field."""
        info = EntitlementInfo(
            id="order-123",
            product="products/my-agent.endpoints.project.cloud.goog",
        )

        assert info.product == "products/my-agent.endpoints.project.cloud.goog"

    def test_entitlement_info_without_product(self):
        """Test EntitlementInfo product defaults to None."""
        info = EntitlementInfo(id="order-123")

        assert info.product is None


class TestAccountRepository:
    """Tests for account repository."""

    @pytest.fixture
    def repo(self, db_session):
        """Create a fresh repository."""
        return AccountRepository()

    @pytest.mark.asyncio
    async def test_create_account(self, repo):
        """Test creating an account."""
        account = Account(
            id="account-123",
            provider_id="provider-456",
            state=AccountState.ACTIVE,
        )

        created = await repo.create(account)

        assert created.id == "account-123"
        assert await repo.get("account-123") is not None

    @pytest.mark.asyncio
    async def test_get_account(self, repo):
        """Test getting an account."""
        account = Account(id="account-123", provider_id="provider-456")
        await repo.create(account)

        retrieved = await repo.get("account-123")

        assert retrieved is not None
        assert retrieved.id == "account-123"

    @pytest.mark.asyncio
    async def test_get_nonexistent_account(self, repo):
        """Test getting a nonexistent account."""
        result = await repo.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_account(self, repo):
        """Test updating an account."""
        account = Account(
            id="account-123",
            provider_id="provider-456",
            state=AccountState.PENDING,
        )
        await repo.create(account)

        account.state = AccountState.ACTIVE
        updated = await repo.update(account)

        assert updated.state == AccountState.ACTIVE

    @pytest.mark.asyncio
    async def test_is_valid_account(self, repo):
        """Test account validity check."""
        account = Account(
            id="account-123",
            provider_id="provider-456",
            state=AccountState.ACTIVE,
        )
        await repo.create(account)

        assert await repo.is_valid("account-123") is True
        assert await repo.is_valid("nonexistent") is False


class TestEntitlementRepository:
    """Tests for entitlement repository."""

    @pytest.fixture
    def repo(self, db_session):
        """Create a fresh repository."""
        return EntitlementRepository()

    @pytest.mark.asyncio
    async def test_create_entitlement(self, repo):
        """Test creating an entitlement."""
        entitlement = Entitlement(
            id="order-123",
            account_id="account-456",
            provider_id="provider-789",
            state=EntitlementState.ACTIVE,
        )

        created = await repo.create(entitlement)

        assert created.id == "order-123"
        assert await repo.get("order-123") is not None


class TestProcurementService:
    """Tests for procurement service."""

    @pytest.fixture
    def service(self, db_session):
        """Create a fresh service."""
        return ProcurementService(
            entitlement_repo=EntitlementRepository(),
        )

    @pytest.mark.asyncio
    async def test_process_entitlement_active(self, service):
        """Test processing ENTITLEMENT_ACTIVE event."""
        event = ProcurementEvent(
            event_id="event-123",
            event_type=ProcurementEventType.ENTITLEMENT_ACTIVE,
            provider_id="provider-123",
            entitlement={"id": "order-456"},
        )

        await service.process_event(event)

        assert await service.is_valid_order("order-456")

    @pytest.mark.asyncio
    async def test_is_valid_account_active(self, service):
        """Test is_valid_account returns True for active accounts via Procurement API."""
        with patch.object(
            service, "_get_account_state", return_value="ACCOUNT_ACTIVE"
        ):
            result = await service.is_valid_account("account-123")

        assert result is True

    @pytest.mark.asyncio
    async def test_is_valid_account_not_active(self, service):
        """Test is_valid_account returns False for non-active accounts."""
        with patch.object(
            service, "_get_account_state", return_value="ACCOUNT_SUSPENDED"
        ):
            result = await service.is_valid_account("account-123")

        assert result is False

    @pytest.mark.asyncio
    async def test_is_valid_account_api_error(self, service):
        """Test is_valid_account returns False on API error."""
        with patch.object(service, "_get_account_state", return_value=None):
            result = await service.is_valid_account("account-123")

        assert result is False

    @pytest.mark.asyncio
    async def test_account_events_not_handled(self, service):
        """Test that account events are not in the handler dispatch."""
        event = ProcurementEvent(
            event_id="event-123",
            event_type=ProcurementEventType.ACCOUNT_ACTIVE,
            provider_id="provider-123",
            account={"id": "account-456"},
        )

        # Should not raise — just logs a warning about no handler
        await service.process_event(event)
