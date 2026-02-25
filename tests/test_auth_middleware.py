"""Tests for auth middleware order/client authorization checks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lightspeed_agent.auth.middleware import AuthenticationMiddleware
from lightspeed_agent.marketplace.models import Entitlement, EntitlementState


class TestAuthenticationMiddleware:
    """Tests for order/client authorization helper in middleware."""

    @pytest.fixture
    def middleware(self):
        """Create middleware with a no-op ASGI app."""
        return AuthenticationMiddleware(app=lambda scope, receive, send: None)

    @pytest.mark.asyncio
    async def test_is_authorized_for_order_allows_active_mapped_client(self, middleware):
        """Allow when entitlement is active and mapped client matches."""
        entitlement = Entitlement(
            id="order-1",
            account_id="account-1",
            provider_id="provider-1",
            state=EntitlementState.ACTIVE,
        )
        entitlement_repo = MagicMock()
        entitlement_repo.get = AsyncMock(return_value=entitlement)
        dcr_repo = MagicMock()
        dcr_repo.get_by_order_id = AsyncMock(return_value=MagicMock(client_id="client-1"))

        with patch(
            "lightspeed_agent.marketplace.repository.get_entitlement_repository",
            return_value=entitlement_repo,
        ), patch(
            "lightspeed_agent.dcr.repository.get_dcr_client_repository",
            return_value=dcr_repo,
        ):
            allowed = await middleware._is_authorized_for_order(
                order_id="order-1",
                client_id="client-1",
            )

        assert allowed is True

    @pytest.mark.asyncio
    async def test_is_authorized_for_order_denies_inactive_entitlement(self, middleware):
        """Deny when entitlement is not active."""
        entitlement = Entitlement(
            id="order-2",
            account_id="account-1",
            provider_id="provider-1",
            state=EntitlementState.CANCELLED,
        )
        entitlement_repo = MagicMock()
        entitlement_repo.get = AsyncMock(return_value=entitlement)
        dcr_repo = MagicMock()
        dcr_repo.get_by_order_id = AsyncMock(return_value=MagicMock(client_id="client-2"))

        with patch(
            "lightspeed_agent.marketplace.repository.get_entitlement_repository",
            return_value=entitlement_repo,
        ), patch(
            "lightspeed_agent.dcr.repository.get_dcr_client_repository",
            return_value=dcr_repo,
        ):
            allowed = await middleware._is_authorized_for_order(
                order_id="order-2",
                client_id="client-2",
            )

        assert allowed is False

    @pytest.mark.asyncio
    async def test_is_authorized_for_order_denies_client_mismatch(self, middleware):
        """Deny when token client_id differs from order's mapped DCR client."""
        entitlement = Entitlement(
            id="order-3",
            account_id="account-1",
            provider_id="provider-1",
            state=EntitlementState.ACTIVE,
        )
        entitlement_repo = MagicMock()
        entitlement_repo.get = AsyncMock(return_value=entitlement)
        dcr_repo = MagicMock()
        dcr_repo.get_by_order_id = AsyncMock(
            return_value=MagicMock(client_id="client-other")
        )

        with patch(
            "lightspeed_agent.marketplace.repository.get_entitlement_repository",
            return_value=entitlement_repo,
        ), patch(
            "lightspeed_agent.dcr.repository.get_dcr_client_repository",
            return_value=dcr_repo,
        ):
            allowed = await middleware._is_authorized_for_order(
                order_id="order-3",
                client_id="client-3",
            )

        assert allowed is False

