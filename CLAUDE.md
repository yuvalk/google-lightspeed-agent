# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Red Hat Lightspeed Agent for Google Cloud — an A2A-ready (Agent-to-Agent protocol) AI agent providing access to Red Hat Insights, built on Google Agent Development Kit (ADK), Gemini 3.5 Flash, and a Red Hat Lightspeed MCP server sidecar. Integrates with Google Cloud Marketplace for provisioning, billing, and metering.

## Common Commands

### Development

Always use a Python virtual environment — do not install dependencies system-wide.

```bash
python3.12 -m venv .venv              # Create virtual environment
source .venv/bin/activate             # Activate it
pip install -e ".[agent,dev]"         # Install all dependencies (agent + dev)
# Optional: pip install -e ".[agent,dev,telemetry]"  # Include Jaeger/Zipkin exporters
python -m lightspeed_agent.main       # Run the agent API server (port 8000)
```

### Testing

Ensure the virtual environment is activated before running tests.

```bash
make test                              # Run all tests
python -m pytest tests/ -v             # Equivalent direct command
python -m pytest tests/test_auth.py -v # Run a single test file
python -m pytest tests/test_auth.py::test_function_name -v  # Run a single test
```

Tests use in-memory SQLite and `SKIP_JWT_VALIDATION=true` (set automatically in `tests/conftest.py`). The test suite is async — `asyncio_mode = "auto"` is configured in pyproject.toml.

To run with coverage:
```bash
python -m pytest tests/ -v --cov=src/lightspeed_agent --cov-report=term-missing
```

### Dependencies

Lock files pin exact versions with hashes. After editing `pyproject.toml`, run `make lock` and commit both together. For CVE fixes in transitive deps, add them as direct deps in `pyproject.toml` with the safe version, then `make lock`. See `CONTRIBUTING.md` for detailed workflows.

```bash
make lock                              # Regenerate all lock files
make check-lock                        # Verify lock files match pyproject.toml
make audit                             # Scan for known CVEs (pip-audit)
```

### Linting & Type Checking
```bash
make lint                              # Run both ruff and mypy
ruff check src/ tests/                 # Linter only
mypy src/lightspeed_agent/ --ignore-missing-imports  # Type checker only
```

Ruff rules: `E, F, I, N, W, UP, B, C4, SIM`. Line length: 100. Target: Python 3.12.

### Container Build (Podman)
```bash
make build                             # Build both images (agent + marketplace handler)
make run                               # Start all pods
make stop                              # Stop and remove pods
make logs                              # Agent logs
make help                              # List all available targets
```

### Shell Tests (deploy.sh)

Changes to `deploy/cloudrun/deploy.sh` must include test updates in `tests/shell/`. CI enforces that every function in `deploy.sh` is referenced in the bats tests.

```bash
make test-shell                        # Run bats tests
shellcheck deploy/cloudrun/*.sh        # Lint shell scripts
bash tests/shell/check_coverage.sh     # Verify all functions have tests
```

### Before Pushing

```bash
make lint && make test
```

If you modified `pyproject.toml`, also run `make lock` and commit the lock files together.

If you modified `deploy/cloudrun/deploy.sh`, also run `make test-shell` and ensure `bash tests/shell/check_coverage.sh` passes.

When your changes affect architecture, configuration, APIs, or behavior, update the relevant docs in the same PR:
- `docs/` — detailed reference documentation (architecture, authentication, configuration, marketplace, MCP integration)
- `deploy/` — deployment READMEs and scripts (Cloud Run, OpenShift, Podman) and `cloudbuild.yaml` when changing deployment-related config, env vars, or infrastructure
- `CLAUDE.md` — only if the change affects common commands, gotchas, or key architectural concepts described here
- `README.md` — only if the change affects the project overview or quickstart

## Common Gotchas

- **SQLite vs PostgreSQL**: `ARRAY(String)` columns use a JSON variant for SQLite (see `db/models.py:StringList`). If adding array-type columns, use `StringList` — raw `ARRAY` will break tests.
- **Dev-only bypasses blocked in prod**: `SKIP_JWT_VALIDATION=true` raises on startup when running on Cloud Run. Don't rely on it for integration testing in deployed environments.
- **MCP tools are loaded at startup**: Changes to MCP server tool definitions require an agent restart to take effect.
- **Two databases, two URLs**: Marketplace data (`DATABASE_URL`) and ADK sessions (`SESSION_DATABASE_URL`) are separate. Mixing them up causes silent data loss or missing tables.

## Architecture

### Two-Service Design

The system runs as two separate FastAPI services with separate concerns:

1. **Lightspeed Agent** (port 8000, `src/lightspeed_agent/main.py`) — The AI agent service. Scales to zero on Cloud Run. Handles A2A protocol requests (JSON-RPC 2.0 at `/`), serves the AgentCard at `/.well-known/agent.json`. Uses ADK `LlmAgent` with MCP tools loaded from the sidecar and ADK AI Skills for modular behavioral instructions.

2. **Marketplace Handler** (port 8001, `src/lightspeed_agent/marketplace/app.py`) — Always-on service for Google Cloud Marketplace Pub/Sub provisioning events and Dynamic Client Registration (DCR). Has separate `/dcr` (DCR requests) and `/pubsub` (Pub/Sub events with Google OIDC verification) endpoints.

### Authentication Flow

JWT tokens from Red Hat SSO flow through three layers:
1. `auth/middleware.py` validates Bearer tokens — only `POST /` is protected; all other paths are public
2. Token is stored in `contextvars` for the request lifecycle (user_id, org_id, order_id, client_id, request_id)
3. `tools/mcp_headers.py` forwards the caller's JWT to the MCP server so it can authenticate with console.redhat.com on the user's behalf

Setting `SKIP_JWT_VALIDATION=true` bypasses auth (dev only, blocked when running on Cloud Run).

### MCP Integration

The agent loads tools from a Red Hat Lightspeed MCP server running as a sidecar:
- Read-only mode (`MCP_READ_ONLY=true`) filters to a safe subset of tools
- MCP toolset creation is in `tools/insights_tools.py`; config in `tools/mcp_config.py`

### ADK AI Skills

Agent behavioral instructions use ADK's progressive-disclosure Skills system instead of a monolithic system prompt. Each skill is a `SKILL.md` file with YAML frontmatter (L1: name + description loaded at startup) and a markdown body (L2: full instructions loaded on-demand by the LLM).

- **Bundled skills** (`core/skills/`): tool-invocation-rules, multi-step-workflows, pagination-handling, efficient-counting, error-handling, guardrails-safety, response-formatting — always loaded
- **External skills** (`SKILLS_DIR` env var): deployment-specific skills loaded alongside bundled ones; same-name skills override bundled defaults
- A2A AgentCard skills (`tools/a2a_skills.py`) are a separate concept — they describe agent capabilities for the A2A protocol, not LLM behavioral instructions

### Middleware Stack

CORS → body size limits → security headers → rate limiting (60 req/min, 1000 req/hour) → JWT auth. See `api/app.py` for ordering and configuration.

## Configuration

All configuration is via environment variables, managed through Pydantic settings in `config/settings.py`. See `.env.example` for the complete list (30+ vars) and `docs/configuration.md` for detailed documentation.

Key variables: `GOOGLE_API_KEY` (or Vertex AI), `GEMINI_MODEL` (default: `gemini-3.5-flash`), `DATABASE_URL`, `SESSION_DATABASE_URL`, `MCP_TRANSPORT_MODE`, `MCP_SERVER_URL`, `RED_HAT_SSO_CLIENT_ID`/`SECRET`. Dev-only bypasses: `SKIP_JWT_VALIDATION`, `SKIP_DCR_JWT_VALIDATION`, `SKIP_ORDER_VALIDATION`.

