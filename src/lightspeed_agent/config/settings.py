"""Application settings and configuration management."""

import logging
import os
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Production Mode
    production: bool = Field(
        default=False,
        description="Enable production mode. Enforces security guards.",
    )

    # Google AI / Gemini Configuration
    google_genai_use_vertexai: bool = Field(
        default=False,
        description="Use Vertex AI instead of Google AI Studio",
    )
    google_api_key: str | None = Field(
        default=None,
        description="Google AI Studio API key",
    )
    google_cloud_project: str | None = Field(
        default=None,
        description="Google Cloud project ID for Vertex AI",
    )
    google_cloud_location: str = Field(
        default="us-central1",
        description="Google Cloud location for Vertex AI",
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description="Gemini model to use",
    )

    # Red Hat SSO Configuration
    red_hat_sso_issuer: str = Field(
        default="https://sso.redhat.com/auth/realms/redhat-external",
        description="Red Hat SSO issuer URL",
    )
    red_hat_sso_client_id: str = Field(
        default="",
        description="OAuth client ID for Red Hat SSO",
    )
    red_hat_sso_client_secret: str = Field(
        default="",
        description="OAuth client secret for Red Hat SSO",
    )
    # Red Hat Lightspeed MCP Configuration
    lightspeed_client_id: str = Field(
        default="",
        description="Lightspeed service account client ID",
    )
    lightspeed_client_secret: str = Field(
        default="",
        description="Lightspeed service account client secret",
    )
    mcp_transport_mode: Literal["stdio", "http", "sse"] = Field(
        default="stdio",
        description="MCP server transport mode",
    )
    mcp_server_url: str = Field(
        default="http://localhost:8080",
        description="MCP server URL for http/sse modes",
    )
    mcp_read_only: bool = Field(
        default=True,
        description="Enable read-only mode for MCP tools",
    )

    # Agent Configuration
    agent_provider_url: str = Field(
        default="https://localhost:8000",
        description="Agent provider URL for AgentCard",
    )
    agent_name: str = Field(
        default="lightspeed_agent",
        description="Agent name (must be a valid Python identifier)",
    )
    agent_description: str = Field(
        default="Red Hat Lightspeed Agent for Google Cloud",
        description="Agent description",
    )
    agent_host: str = Field(
        default="0.0.0.0",
        description="Server host",
    )
    agent_port: int = Field(
        default=8000,
        description="Server port",
    )

    # Marketplace Handler Configuration
    # The marketplace handler is a separate service that handles DCR and Pub/Sub events
    marketplace_handler_url: str = Field(
        default="",
        description="URL of the marketplace handler service for DCR. If empty, uses agent_provider_url.",
    )

    # Google Cloud Service Control
    service_control_service_name: str = Field(
        default="",
        description="Service name for Google Cloud Service Control (e.g., myservice.gcpmarketplace.example.com)",
    )
    service_control_enabled: bool = Field(
        default=True,
        description="Enable usage reporting to Google Cloud Service Control",
    )
    # Metering recovery: stale claim release and backfill
    metering_stale_claim_minutes: int = Field(
        default=15,
        description="Release rows claimed longer than this (worker crash recovery)",
    )
    metering_backfill_max_age_hours: int = Field(
        default=168,
        description="Backfill only periods within this many hours (default 7 days)",
    )
    metering_backfill_limit_per_run: int = Field(
        default=20,
        description="Max unreported periods to process per backfill run",
    )

    # Rate Limiting (Redis-backed)
    rate_limit_requests_per_minute: int = Field(
        default=60,
        description="Global requests per minute limit",
    )
    rate_limit_requests_per_hour: int = Field(
        default=1000,
        description="Global requests per hour limit",
    )
    rate_limit_redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis URL for distributed rate limiting",
    )
    rate_limit_redis_timeout_ms: int = Field(
        default=200,
        description="Redis operation timeout in milliseconds",
    )
    rate_limit_key_prefix: str = Field(
        default="lightspeed:ratelimit",
        description="Redis key prefix for rate limiting data",
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level",
    )
    log_format: Literal["json", "text"] = Field(
        default="json",
        description="Log format",
    )

    # DCR (Dynamic Client Registration) Configuration
    dcr_enabled: bool = Field(
        default=True,
        description="Enable real DCR with Red Hat SSO (Keycloak). When disabled, uses pre-seeded credentials from the database.",
    )
    dcr_initial_access_token: str = Field(
        default="",
        description="Keycloak Initial Access Token for creating OAuth clients via DCR",
    )
    dcr_client_name_prefix: str = Field(
        default="gemini-order-",
        description="Prefix for OAuth client names created via DCR",
    )
    dcr_encryption_key: str = Field(
        default="",
        description="Fernet encryption key for DCR client secrets (generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')",
    )

    # Database Configuration
    # Marketplace database: stores accounts, entitlements, DCR clients, usage records
    # This is shared between the marketplace handler and agent for order validation
    database_url: str = Field(
        default="sqlite+aiosqlite:///./lightspeed_agent.db",
        description="Marketplace database URL (PostgreSQL for production). Stores accounts, entitlements, DCR clients.",
    )
    database_pool_size: int = Field(
        default=5,
        description="Database connection pool size",
    )
    database_pool_max_overflow: int = Field(
        default=10,
        description="Maximum overflow connections beyond pool size",
    )

    # Session database: stores ADK sessions, conversation history, memory
    # Separate from marketplace DB for security isolation - each agent can have its own
    session_database_url: str = Field(
        default="",
        description="Session database URL for ADK sessions. If empty, uses DATABASE_URL. For security isolation, use a separate database.",
    )

    # Agent required scope for token introspection
    agent_required_scope: str = Field(
        default="agent:insights",
        description="OAuth scope required in access tokens. Checked via token introspection.",
    )

    @property
    def keycloak_introspection_endpoint(self) -> str:
        """Get the Keycloak token introspection endpoint URL."""
        return f"{self.red_hat_sso_issuer}/protocol/openid-connect/token/introspect"

    @property
    def keycloak_token_endpoint(self) -> str:
        """Get the Keycloak token endpoint URL."""
        return f"{self.red_hat_sso_issuer}/protocol/openid-connect/token"

    @property
    def keycloak_admin_api_base(self) -> str:
        """Get the Keycloak Admin REST API base URL.

        Derived from the issuer by inserting /admin before /realms/.
        E.g. https://host/auth/realms/myrealm -> https://host/auth/admin/realms/myrealm
        """
        return self.red_hat_sso_issuer.replace("/realms/", "/admin/realms/", 1)

    @property
    def keycloak_dcr_endpoint(self) -> str:
        """Get the Keycloak DCR endpoint URL."""
        # Red Hat SSO issuer format: https://sso.redhat.com/auth/realms/redhat-external
        # DCR endpoint: https://sso.redhat.com/auth/realms/redhat-external/clients-registrations/openid-connect
        return f"{self.red_hat_sso_issuer}/clients-registrations/openid-connect"

    # Development Settings
    debug: bool = Field(
        default=False,
        description="Enable debug mode",
    )
    skip_jwt_validation: bool = Field(
        default=False,
        description="Skip JWT validation (development only)",
    )

    # OpenTelemetry Configuration
    otel_enabled: bool = Field(
        default=False,
        description="Enable OpenTelemetry tracing",
    )
    otel_service_name: str = Field(
        default="lightspeed_agent",
        description="Service name for OpenTelemetry traces",
    )
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317",
        description="OTLP exporter endpoint (gRPC)",
    )
    otel_exporter_otlp_http_endpoint: str = Field(
        default="http://localhost:4318",
        description="OTLP exporter endpoint (HTTP)",
    )
    otel_exporter_type: Literal["otlp", "otlp-http", "jaeger", "zipkin", "console"] = Field(
        default="otlp",
        description="Telemetry exporter type",
    )
    otel_traces_sampler: Literal["always_on", "always_off", "traceidratio", "parentbased_always_on", "parentbased_always_off", "parentbased_traceidratio"] = Field(
        default="always_on",
        description="Trace sampling strategy",
    )
    otel_traces_sampler_arg: float = Field(
        default=1.0,
        description="Sampler argument (e.g., ratio for traceidratio)",
    )

    @model_validator(mode="after")
    def _block_skip_jwt_in_production(self) -> Self:
        """Block SKIP_JWT_VALIDATION when running on Cloud Run (K_SERVICE)."""
        if os.getenv("K_SERVICE") and self.skip_jwt_validation:
            raise ValueError(
                "SKIP_JWT_VALIDATION=true is not allowed when running on "
                "Cloud Run (K_SERVICE is set). Remove SKIP_JWT_VALIDATION "
                "or set it to false."
            )
        return self

    @model_validator(mode="after")
    def _enforce_production_guards(self) -> Self:
        """Enforce all production security guards when PRODUCTION=true.

        Collects all violations and raises a single error so the operator
        can fix everything at once.
        """
        if not self.production:
            return self

        logger.info("Production mode enabled — enforcing security guards")

        violations: list[str] = []

        # Guard 1: Force Vertex AI
        if self.google_api_key:
            violations.append(
                "Guard 1 (Vertex AI): GOOGLE_API_KEY must not be set in "
                "production. Remove it from your environment."
            )
        if not self.google_genai_use_vertexai:
            violations.append(
                "Guard 1 (Vertex AI): GOOGLE_GENAI_USE_VERTEXAI must be true "
                "in production. Set GOOGLE_GENAI_USE_VERTEXAI=true."
            )
        if not self.google_cloud_project:
            violations.append(
                "Guard 1 (Vertex AI): GOOGLE_CLOUD_PROJECT must be set in "
                "production. Set it to your GCP project ID."
            )

        # Guard 2: Force JWT validation
        if self.skip_jwt_validation:
            violations.append(
                "Guard 2 (JWT): SKIP_JWT_VALIDATION must not be true in "
                "production. Remove it or set SKIP_JWT_VALIDATION=false."
            )

        # Guard 3: Disable debug
        if self.debug:
            violations.append(
                "Guard 3 (Debug): DEBUG must not be true in production. "
                "Remove it or set DEBUG=false."
            )

        # Guard 4: Force HTTPS on all URLs
        if not self.agent_provider_url.startswith("https://"):
            violations.append(
                "Guard 4 (HTTPS): AGENT_PROVIDER_URL must start with "
                f"https:// in production. Got: {self.agent_provider_url}"
            )
        if not self.mcp_server_url.startswith("https://"):
            violations.append(
                "Guard 4 (HTTPS): MCP_SERVER_URL must start with https:// "
                f"in production. Got: {self.mcp_server_url}"
            )

        # Guard 5: Force PostgreSQL
        if "sqlite" in self.database_url.lower():
            violations.append(
                "Guard 5 (PostgreSQL): DATABASE_URL must not use SQLite in "
                "production. Use PostgreSQL "
                "(e.g., postgresql+asyncpg://user:pass@host/db)."
            )

        # Guard 6: Force MCP http transport
        if self.mcp_transport_mode != "http":
            violations.append(
                "Guard 6 (MCP transport): MCP_TRANSPORT_MODE must be 'http' "
                f"in production. Got: {self.mcp_transport_mode}"
            )

        # Guard 7: Force JWT forwarding to MCP (no service-account creds)
        if self.lightspeed_client_id:
            violations.append(
                "Guard 7 (JWT forwarding): LIGHTSPEED_CLIENT_ID must not be "
                "set in production. Remove it so the user JWT is forwarded "
                "to MCP instead."
            )
        if self.lightspeed_client_secret:
            violations.append(
                "Guard 7 (JWT forwarding): LIGHTSPEED_CLIENT_SECRET must not "
                "be set in production. Remove it so the user JWT is forwarded "
                "to MCP instead."
            )

        # Guard 8: No CORS middleware (enforced at runtime in app.py)
        # — nothing to validate here; checked in create_app()

        # Guard 9: Require SSO credentials
        if not self.red_hat_sso_client_id:
            violations.append(
                "Guard 9 (SSO): RED_HAT_SSO_CLIENT_ID must be set in "
                "production. Configure your Red Hat SSO client ID."
            )
        if not self.red_hat_sso_client_secret:
            violations.append(
                "Guard 9 (SSO): RED_HAT_SSO_CLIENT_SECRET must be set in "
                "production. Configure your Red Hat SSO client secret."
            )

        # Guard 10: Require DCR configuration
        if not self.dcr_enabled:
            violations.append(
                "Guard 10 (DCR): DCR_ENABLED must be true in production. "
                "Set DCR_ENABLED=true."
            )
        if not self.dcr_initial_access_token:
            violations.append(
                "Guard 10 (DCR): DCR_INITIAL_ACCESS_TOKEN must be set in "
                "production. Configure your Keycloak Initial Access Token."
            )
        if not self.dcr_encryption_key:
            violations.append(
                "Guard 10 (DCR): DCR_ENCRYPTION_KEY must be set in "
                "production. Generate one with: python -c "
                "'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            )

        if violations:
            header = (
                f"Production mode validation failed with "
                f"{len(violations)} violation(s):"
            )
            details = "\n  - ".join(violations)
            raise ValueError(f"{header}\n  - {details}")

        return self


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()
