"""Header provider for MCP toolset to inject authentication credentials."""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lightspeed_agent.auth.middleware import get_request_access_token
from lightspeed_agent.config import get_settings

if TYPE_CHECKING:
    from google.adk.agents.readonly_context import ReadonlyContext

logger = logging.getLogger(__name__)


def create_mcp_header_provider():
    """Create a header provider function for McpToolset.

    The returned function provides authentication headers for MCP requests.

    Priority logic:
      1. If LIGHTSPEED_CLIENT_ID and LIGHTSPEED_CLIENT_SECRET are configured,
         send them as ``lightspeed-client-id`` / ``lightspeed-client-secret``
         headers (existing behaviour).
      2. Otherwise, forward the incoming request's JWT token as an
         ``Authorization: Bearer <token>`` header so the MCP server can
         authenticate on behalf of the calling user.

    Returns:
        A callable that takes ReadonlyContext and returns headers dict.
    """

    def header_provider(context: "ReadonlyContext") -> dict[str, str]:
        """Provide headers for MCP requests.

        Args:
            context: The readonly context (unused, but required by interface).

        Returns:
            Dictionary of headers to include in MCP requests.
        """
        settings = get_settings()

        # --- Priority 1: Lightspeed service-account credentials ---
        # Skipped in production mode (Guard 7 enforces JWT forwarding)
        if not settings.production:
            if settings.lightspeed_client_id and settings.lightspeed_client_secret:
                logger.debug("Using lightspeed credentials from environment")
                return {
                    "lightspeed-client-id": settings.lightspeed_client_id,
                    "lightspeed-client-secret": settings.lightspeed_client_secret,
                }

        # --- Priority 2: Forward the caller's JWT token ---
        token_info = get_request_access_token()
        if token_info is not None:
            token, token_exp = token_info
            now = datetime.now(UTC)
            if settings.production:
                logger.info(
                    "Production mode: forwarding user JWT to MCP server"
                )
            if now >= token_exp:
                logger.warning(
                    "Access token expired at %s (now %s); "
                    "forwarding anyway — MCP server will reject it",
                    token_exp.isoformat(),
                    now.isoformat(),
                )
            return {"Authorization": f"Bearer {token}"}

        if settings.production:
            logger.error(
                "No MCP credentials available: production mode requires "
                "a user JWT but no access token found in request context"
            )
        else:
            logger.warning(
                "No MCP credentials available: lightspeed credentials not "
                "configured and no access token in request context"
            )
        return {}

    return header_provider
