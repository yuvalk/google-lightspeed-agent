"""Header provider for MCP toolset to inject authentication credentials."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from lightspeed_agent.auth.middleware import (
    get_request_access_token,
    get_request_id,
    get_request_org_id,
    get_request_user_id,
)
from lightspeed_agent.config import get_settings

if TYPE_CHECKING:
    from google.adk.agents.readonly_context import ReadonlyContext

logger = logging.getLogger(__name__)


def create_mcp_header_provider() -> Callable[["ReadonlyContext"], dict[str, str]]:
    """Create a header provider function for McpToolset.

    The returned function forwards the incoming request's JWT token as an
    ``Authorization: Bearer <token>`` header so the MCP server can
    authenticate on behalf of the calling user.

    Returns:
        A callable that takes ReadonlyContext and returns headers dict.
    """

    # TODO: Implement RFC 8693 token exchange for production deployments
    # to obtain a scoped token instead of forwarding the full-scope JWT.
    mcp_url = get_settings().mcp_server_url
    parsed = urlparse(mcp_url)
    if parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        logger.warning(
            'Forwarding full-scope JWT to non-localhost MCP server. '
            'Consider implementing token exchange (RFC 8693) for production.',
        )

    def header_provider(context: "ReadonlyContext") -> dict[str, str]:
        """Provide headers for MCP requests.

        Forwards the caller's JWT token to the MCP server.

        Args:
            context: The readonly context (unused, but required by interface).

        Returns:
            Dictionary of headers to include in MCP requests.
        """
        token_info = get_request_access_token()
        if token_info is not None:
            token, token_exp = token_info
            now = datetime.now(UTC)

            logger.info(
                "Forwarding JWT to MCP server "
                "(event_type=mcp_jwt_forwarded, user_id=%s, org_id=%s, "
                "request_id=%s, token_expiry=%s)",
                get_request_user_id(),
                get_request_org_id(),
                get_request_id(),
                token_exp.isoformat(),
            )

            if now >= token_exp:
                logger.warning(
                    "Access token expired at %s (now %s); "
                    "forwarding anyway — MCP server will reject it",
                    token_exp.isoformat(),
                    now.isoformat(),
                )
            return {"Authorization": f"Bearer {token}"}

        logger.warning(
            "No MCP credentials available: no access token in request context"
        )
        return {}

    return header_provider
