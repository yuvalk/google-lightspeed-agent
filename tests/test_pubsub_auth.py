"""Tests for Pub/Sub OIDC verification and endpoint separation."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from google.auth import exceptions as google_auth_exceptions

from lightspeed_agent.auth.middleware import AuthenticationMiddleware

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def marketplace_app():
    """Create a test marketplace app."""
    from lightspeed_agent.marketplace.app import create_app

    return create_app()


@pytest.fixture
def marketplace_client(marketplace_app, db_session):
    """Create a test client for the marketplace handler."""
    return TestClient(marketplace_app, raise_server_exceptions=False)


def _pubsub_body(event_type: str = "ENTITLEMENT_ACTIVE") -> dict:
    """Build a minimal Pub/Sub request body."""
    data = {
        "eventType": event_type,
        "entitlement": {"id": "order-123", "plan": "standard"},
        "account": {"id": "account-456"},
    }
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return {"message": {"messageId": "msg-1", "data": encoded}}


# ---------------------------------------------------------------------------
# 1. Pub/Sub OIDC Verification
# ---------------------------------------------------------------------------

class TestPubSubOIDCVerification:
    """Tests for Google OIDC token verification on /pubsub."""

    def test_pubsub_rejects_missing_auth_header(self, marketplace_client):
        """POST /pubsub without Authorization header returns 401."""
        resp = marketplace_client.post("/pubsub", json=_pubsub_body())
        assert resp.status_code == 401

    def test_pubsub_rejects_non_bearer_auth(self, marketplace_client):
        """POST /pubsub with non-Bearer auth returns 401."""
        resp = marketplace_client.post(
            "/pubsub",
            json=_pubsub_body(),
            headers={"Authorization": "Basic abc123"},
        )
        assert resp.status_code == 401

    @patch("lightspeed_agent.marketplace.router.google_id_token.verify_oauth2_token")
    def test_pubsub_rejects_invalid_oidc_token(self, mock_verify, marketplace_client):
        """POST /pubsub with invalid OIDC token returns 401."""
        mock_verify.side_effect = ValueError("Token expired")
        resp = marketplace_client.post(
            "/pubsub",
            json=_pubsub_body(),
            headers={"Authorization": "Bearer bad-token"},
        )
        assert resp.status_code == 401

    @patch("lightspeed_agent.marketplace.router.google_id_token.verify_oauth2_token")
    def test_pubsub_rejects_wrong_issuer_token(self, mock_verify, marketplace_client):
        """POST /pubsub with wrong-issuer OIDC token returns 401."""
        mock_verify.side_effect = google_auth_exceptions.GoogleAuthError("Wrong issuer")
        resp = marketplace_client.post(
            "/pubsub",
            json=_pubsub_body(),
            headers={"Authorization": "Bearer wrong-issuer-token"},
        )
        assert resp.status_code == 401

    @patch(
        "lightspeed_agent.marketplace.router.get_procurement_service",
    )
    @patch("lightspeed_agent.marketplace.router.google_id_token.verify_oauth2_token")
    def test_pubsub_accepts_valid_oidc_token(
        self, mock_verify, mock_get_svc, marketplace_client
    ):
        """POST /pubsub with valid OIDC token processes the event."""
        mock_verify.return_value = {"iss": "accounts.google.com", "sub": "123"}
        mock_svc = MagicMock()
        mock_svc.process_event = AsyncMock()
        mock_get_svc.return_value = mock_svc

        resp = marketplace_client.post(
            "/pubsub",
            json=_pubsub_body(),
            headers={"Authorization": "Bearer valid-google-token"},
        )
        assert resp.status_code == 200
        mock_verify.assert_called_once()

    @patch(
        "lightspeed_agent.marketplace.router.get_procurement_service",
    )
    @patch("lightspeed_agent.marketplace.router.google_id_token.verify_oauth2_token")
    def test_pubsub_passes_audience_to_verifier(
        self, mock_verify, mock_get_svc, marketplace_client
    ):
        """Verify the audience setting is passed to token verification."""
        mock_verify.return_value = {"iss": "accounts.google.com"}
        mock_svc = MagicMock()
        mock_svc.process_event = AsyncMock()
        mock_get_svc.return_value = mock_svc

        with patch(
            "lightspeed_agent.marketplace.router.get_settings"
        ) as mock_settings:
            settings = MagicMock()
            settings.pubsub_audience = "https://my-service.run.app"
            settings.service_control_service_name = ""
            mock_settings.return_value = settings

            marketplace_client.post(
                "/pubsub",
                json=_pubsub_body(),
                headers={"Authorization": "Bearer valid-token"},
            )

        _, kwargs = mock_verify.call_args
        assert kwargs.get("audience") == "https://my-service.run.app"


# ---------------------------------------------------------------------------
# 2. Endpoint Separation
# ---------------------------------------------------------------------------

class TestEndpointSeparation:
    """Tests verifying /dcr and /pubsub are properly separated."""

    def test_dcr_rejects_pubsub_message(self, marketplace_client):
        """POST /dcr with Pub/Sub message body returns 400."""
        resp = marketplace_client.post("/dcr", json=_pubsub_body())
        assert resp.status_code == 400
        assert "software_statement" in resp.json()["detail"]

    @patch("lightspeed_agent.marketplace.router.get_dcr_service")
    def test_dcr_accepts_software_statement(
        self, mock_get_dcr, marketplace_client
    ):
        """POST /dcr with software_statement still works."""
        mock_dcr = MagicMock()
        mock_result = MagicMock()
        mock_result.client_id = "client-123"
        mock_result.client_secret = "secret-456"
        mock_result.client_secret_expires_at = 0
        mock_dcr.register_client = AsyncMock(return_value=mock_result)
        mock_get_dcr.return_value = mock_dcr

        # DCRError check: result is not a DCRError instance
        with patch(
            "lightspeed_agent.marketplace.router.DCRError", new=type("DCRError", (), {})
        ):
            resp = marketplace_client.post(
                "/dcr", json={"software_statement": "some.jwt.token"}
            )

        assert resp.status_code == 201
        assert resp.json()["client_id"] == "client-123"

    def test_dcr_rejects_empty_body(self, marketplace_client):
        """POST /dcr with empty JSON body returns 400."""
        resp = marketplace_client.post("/dcr", json={})
        assert resp.status_code == 400

    @patch(
        "lightspeed_agent.marketplace.router.get_procurement_service",
    )
    @patch("lightspeed_agent.marketplace.router.google_id_token.verify_oauth2_token")
    def test_pubsub_rejects_non_message_body(
        self, mock_verify, mock_get_svc, marketplace_client
    ):
        """POST /pubsub with non-message body returns 400 (after OIDC passes)."""
        mock_verify.return_value = {"iss": "accounts.google.com"}
        resp = marketplace_client.post(
            "/pubsub",
            json={"software_statement": "some.jwt.token"},
            headers={"Authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 3. Auth Middleware — PUBLIC_PREFIXES removed
# ---------------------------------------------------------------------------

class TestAuthMiddlewarePublicPaths:
    """Tests verifying the blanket /marketplace/ prefix bypass is removed."""

    def test_no_public_prefixes_attribute(self):
        """AuthenticationMiddleware no longer has PUBLIC_PREFIXES."""
        assert not hasattr(AuthenticationMiddleware, "PUBLIC_PREFIXES")

    def test_marketplace_pubsub_is_public(self):
        """The /marketplace/pubsub path is public (has its own OIDC auth)."""
        middleware = AuthenticationMiddleware.__new__(AuthenticationMiddleware)
        assert middleware._is_public("/marketplace/pubsub", "POST")

    def test_marketplace_dcr_not_explicitly_public(self):
        """/marketplace/dcr is not in PUBLIC_PATHS (protected by its own service auth)."""
        assert "/marketplace/dcr" not in AuthenticationMiddleware.PUBLIC_PATHS

    def test_no_blanket_marketplace_prefix_bypass(self):
        """No blanket prefix bypass exists for /marketplace/ paths."""
        # The old PUBLIC_PREFIXES = ("/marketplace/",) allowed any /marketplace/* path
        # to skip auth. Verify this is gone.
        for attr in dir(AuthenticationMiddleware):
            val = getattr(AuthenticationMiddleware, attr, None)
            if isinstance(val, tuple | list):
                for item in val:
                    if isinstance(item, str):
                        assert not item.startswith("/marketplace"), (
                            f"Found blanket marketplace bypass in {attr}: {item}"
                        )
