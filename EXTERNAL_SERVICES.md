# External Services Inventory

A comprehensive list of all external services used by the Google Lightspeed Agent.

## LLM Models

The agent uses Google Gemini models for all LLM inference. Two access paths are supported:

| Access Path | Description | Key Config |
|-------------|-------------|------------|
| **Google AI Studio** (default) | Direct API key access to Gemini | `GOOGLE_API_KEY` |
| **Vertex AI** | Enterprise access via GCP project | `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` |

| Setting | Default | Description |
|---------|---------|-------------|
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model used for agent responses |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` | Switch between AI Studio and Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Region for Vertex AI |

The model is configured in `src/lightspeed_agent/config/settings.py` and used via the Google Agent Development Kit (ADK) `LlmAgent` in `src/lightspeed_agent/core/agent.py`.

## Google Cloud Services

| Service | Purpose | Required | Key Config |
|---------|---------|----------|------------|
| **Gemini (AI Studio / Vertex AI)** | LLM for agent responses | Yes | `GOOGLE_API_KEY`, `GOOGLE_GENAI_USE_VERTEXAI`, `GEMINI_MODEL` |
| **Cloud Run** | Production serverless deployment (2 services: agent + handler) | For production | `deploy/cloudrun/` |
| **Cloud Pub/Sub** | Receives marketplace provisioning events asynchronously | For marketplace | Topic: `marketplace-entitlements` |
| **Commerce Procurement API** | Approve/manage marketplace accounts & entitlements | For marketplace | `https://cloudcommerceprocurement.googleapis.com/v1` |
| **Service Control API** | Usage metering & billing reporting to GCP Marketplace | For marketplace | `SERVICE_CONTROL_SERVICE_NAME`, `SERVICE_CONTROL_ENABLED` |
| **Cloud IAM** | Service account management, role bindings, token creation | For deployment | `deploy/cloudrun/setup.sh` |
| **Cloud Build** | Container image builds | For deployment | `deploy/cloudrun/deploy.sh` |
| **Artifact Registry** | Container image storage | For deployment | `gcr.io/{PROJECT_ID}/...` |

## Red Hat Services

| Service | Purpose | Required | Key Config |
|---------|---------|----------|------------|
| **Red Hat SSO (Keycloak)** | OAuth 2.0 auth, token introspection, Dynamic Client Registration | Yes | `RED_HAT_SSO_ISSUER` (default: `https://sso.redhat.com/auth/realms/redhat-external`) |
| **console.redhat.com (Insights APIs)** | Advisor, Inventory, Vulnerability, Remediations, Patch, Image Builder, RBAC, RHSM | Yes (via MCP) | `LIGHTSPEED_CLIENT_ID`, `LIGHTSPEED_CLIENT_SECRET` |
| **Red Hat Lightspeed MCP Server** | Sidecar gateway to Insights APIs | Yes | `MCP_SERVER_URL`, `MCP_TRANSPORT_MODE` |

## Databases

| Service | Purpose | Required | Key Config |
|---------|---------|----------|------------|
| **PostgreSQL** | Marketplace data (orders, entitlements, DCR clients) + agent sessions | Yes (production) | `DATABASE_URL`, `SESSION_DATABASE_URL` |
| **SQLite** | Development/testing fallback database | Dev only | Default in `DATABASE_URL` |

## Caching / Rate Limiting

| Service | Purpose | Required | Key Config |
|---------|---------|----------|------------|
| **Redis** | Distributed rate limiting across agent replicas | Yes (production) | `RATE_LIMIT_REDIS_URL` (default: `redis://localhost:6379/0`) |

## Observability (Optional)

| Service | Purpose | Required | Key Config |
|---------|---------|----------|------------|
| **OpenTelemetry Collector** | Distributed tracing export (gRPC or HTTP) | No | `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT` |
| **Jaeger** | Trace storage/visualization backend | No | `OTEL_EXPORTER_TYPE=jaeger` |
| **Zipkin** | Trace storage/visualization backend | No | `OTEL_EXPORTER_TYPE=zipkin` |

## Container Registries

| Registry | Image | Purpose |
|----------|-------|---------|
| `registry.access.redhat.com` | `ubi9/python-312-minimal` | Base image for agent & handler |
| `registry.redhat.io` | `rhel9/postgresql-16` | PostgreSQL for Podman deployments |
| `quay.io` | `red-hat-lightspeed-mcp` | MCP server sidecar |
| `ghcr.io` | `red-hat-lightspeed-mcp` | MCP server (alternate registry) |
| `docker.io` | `redis:7-alpine` | Redis for rate limiting |

## Google JWT Validation Endpoint

| Endpoint | Purpose |
|----------|---------|
| `https://www.googleapis.com/service_accounts/v1/metadata/x509/` | Fetches public keys to validate DCR software_statement JWTs from `cloud-agentspace@system.gserviceaccount.com` |

## Key Architectural Notes

1. **Two-service architecture**: The agent (port 8000) and marketplace handler (port 8001) are separate services with separate databases for security isolation.
2. **All external connections are configurable** via environment variables defined in `src/lightspeed_agent/config/settings.py`.
3. **Development can run with minimal services**: SQLite replaces PostgreSQL, JWT validation can be skipped, and the MCP server is optional for limited functionality.
4. **Production requires**: Gemini API, Red Hat SSO, PostgreSQL (x2), Redis, MCP server, and the Google Marketplace services (Pub/Sub, Procurement, Service Control) if marketplace integration is enabled.
