"""Token validation via Red Hat SSO token introspection (RFC 7662).

Instead of verifying JWT signatures locally with JWKS, this module POSTs
the Bearer token to the Red Hat SSO introspection endpoint and checks the
``active`` flag and required scope.  The agent authenticates to the
introspection endpoint using its own client credentials (Resource Server
pattern), so tokens issued to *any* client in the realm can be validated.

Reference: https://github.com/ljogeiger/GE-A2A-Marketplace-Agent/tree/main/2_oauth
"""

from __future__ import annotations

import logging
import os
import warnings
from datetime import UTC, datetime
from typing import Any

import httpx

from lightspeed_agent.auth.models import AuthenticatedUser
from lightspeed_agent.config import Settings, get_settings

logger = logging.getLogger(__name__)


class TokenValidationError(Exception):
    """Raised when a token is invalid or inactive (HTTP 401)."""


class InsufficientScopeError(Exception):
    """Raised when a token is valid but lacks the required scope (HTTP 403)."""


class DisallowedScopeError(Exception):
    """Raised when a token carries scopes outside the allowed set (HTTP 403)."""


class TokenIntrospector:
    """Validate Bearer tokens via the Red Hat SSO introspection endpoint.

    The agent authenticates to the introspection endpoint with its own
    ``RED_HAT_SSO_CLIENT_ID`` / ``RED_HAT_SSO_CLIENT_SECRET`` (HTTP Basic
    Auth).  Red Hat SSO returns ``{"active": true/false, …}``; we then check
    that the required scope is present.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._introspection_url = self._settings.sso_introspection_endpoint
        self._client_id = self._settings.red_hat_sso_client_id
        self._client_secret = self._settings.red_hat_sso_client_secret
        self._required_scopes = self._settings.required_scopes_list
        self._allowed_scopes = self._settings.allowed_scopes_list
        self._validate_scope_config()

    def _validate_scope_config(self) -> None:
        """Validate scope configuration at startup.

        Checks (skipped when ``skip_jwt_validation`` is enabled):

        1. ``AGENT_REQUIRED_SCOPE`` must not be empty on Cloud Run.
        2. ``AGENT_ALLOWED_SCOPES`` must not be empty on Cloud Run.
        3. Every required scope must also be in the allowed list (hard error).
        4. Warn if the allowed list contains scopes beyond the required set.
        """
        if self._settings.skip_jwt_validation:
            return

        is_production = bool(os.getenv("K_SERVICE"))
        required = set(self._required_scopes)
        allowed = set(self._allowed_scopes)

        if is_production and not required:
            raise ValueError(
                "AGENT_REQUIRED_SCOPE must not be empty in production "
                f"(K_SERVICE={os.getenv('K_SERVICE')}). "
                "Set it to the OAuth scopes tokens must carry, "
                "e.g. AGENT_REQUIRED_SCOPE=api.console,api.ocm"
            )

        if is_production and not allowed:
            raise ValueError(
                "AGENT_ALLOWED_SCOPES must not be empty in production "
                f"(K_SERVICE={os.getenv('K_SERVICE')}). "
                "Set it to the full allowlist of permitted scopes, "
                "e.g. AGENT_ALLOWED_SCOPES=openid,profile,email,api.console,api.ocm"
            )

        not_allowed = required - allowed
        if not_allowed:
            raise ValueError(
                f"AGENT_REQUIRED_SCOPE contains scope(s) not in AGENT_ALLOWED_SCOPES: "
                f"{', '.join(sorted(not_allowed))}. "
                "Every required scope must also be allowed, otherwise all tokens "
                "will be rejected."
            )

        extra = allowed - required
        if extra:
            warnings.warn(
                f"AGENT_ALLOWED_SCOPES contains scope(s) beyond AGENT_REQUIRED_SCOPE: "
                f"{', '.join(sorted(extra))}. "
                "Tokens carrying only these scopes will pass the allowlist check "
                "but are not required to be present.",
                stacklevel=2,
            )

    async def validate_token(self, token: str) -> AuthenticatedUser:
        """Validate a Bearer token via introspection.

        Args:
            token: Raw Bearer token string.

        Returns:
            AuthenticatedUser with claims from the introspection response.

        Raises:
            TokenValidationError: Token is inactive or introspection failed.
            InsufficientScopeError: Token is active but missing the required scope.
        """
        if self._settings.skip_jwt_validation:
            logger.warning("Token validation skipped — development mode")
            return self._create_dev_user()

        data = await self._introspect(token)

        if not data.get("active"):
            raise TokenValidationError("Token is not active")

        # Check required scopes
        scopes = self._parse_scopes(data)
        missing = [s for s in self._required_scopes if s not in scopes]
        if missing:
            raise InsufficientScopeError(
                f"Token is missing required scope(s): {', '.join(missing)}"
            )

        # Check that token does not carry scopes beyond the allowlist
        disallowed = [s for s in scopes if s not in self._allowed_scopes]
        if disallowed:
            raise DisallowedScopeError(
                f"Token carries disallowed scope(s): {', '.join(disallowed)}"
            )

        return self._to_user(data, scopes)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _introspect(self, token: str) -> dict[str, Any]:
        """POST to the introspection endpoint."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._introspection_url,
                    data={"token": token, "token_type_hint": "access_token"},
                    auth=(self._client_id, self._client_secret),
                    timeout=30.0,
                )

            if response.status_code != 200:
                logger.error(
                    "Introspection endpoint returned %d: %s",
                    response.status_code,
                    response.text,
                )
                raise TokenValidationError(
                    f"Introspection request failed (HTTP {response.status_code})"
                )

            result: dict[str, Any] = response.json()
            return result

        except httpx.RequestError as exc:
            logger.exception("HTTP error calling introspection endpoint: %s", exc)
            raise TokenValidationError(
                f"HTTP error calling introspection endpoint: {exc}"
            ) from exc

    @staticmethod
    def _parse_scopes(data: dict[str, Any]) -> list[str]:
        scope_str = data.get("scope", "")
        return scope_str.split() if scope_str else []

    def _to_user(self, data: dict[str, Any], scopes: list[str]) -> AuthenticatedUser:
        """Map an introspection response to an AuthenticatedUser."""
        # client_id: azp (authorized party) or client_id field
        client_id = data.get("azp") or data.get("client_id", "")

        # Token expiry
        exp = data.get("exp")
        token_exp = (
            datetime.fromtimestamp(exp, tz=UTC)
            if exp
            else datetime.now(UTC).replace(year=2099)
        )

        metadata: dict[str, str] = {}
        if data.get("order_id"):
            metadata["order_id"] = data["order_id"]
        elif data.get("org_id"):
            metadata["order_id"] = data["org_id"]

        return AuthenticatedUser(
            user_id=data.get("sub", ""),
            client_id=client_id,
            username=data.get("preferred_username"),
            email=data.get("email"),
            name=data.get("name"),
            org_id=data.get("org_id"),
            scopes=scopes,
            token_exp=token_exp,
            metadata=metadata,
        )

    def _create_dev_user(self) -> AuthenticatedUser:
        """Return a default user when validation is skipped."""
        return AuthenticatedUser(
            user_id="dev-user",
            client_id="dev-client",
            username="developer",
            email="dev@example.com",
            name="Development User",
            org_id="dev-org",
            scopes=["openid", "profile", "email", "api.console", "api.ocm"],
            token_exp=datetime.now(UTC).replace(year=2099),
            metadata={"order_id": "dev-order"},
        )


# ------------------------------------------------------------------
# Global singleton
# ------------------------------------------------------------------

_introspector: TokenIntrospector | None = None


def get_token_introspector() -> TokenIntrospector:
    """Get the global TokenIntrospector instance."""
    global _introspector
    if _introspector is None:
        _introspector = TokenIntrospector()
    return _introspector
