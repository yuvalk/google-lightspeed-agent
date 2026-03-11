# Test Suite

## Running Tests

```bash
# All tests
python -m pytest

# Specific test file
python -m pytest tests/test_production_mode.py -v

# With coverage
python -m pytest --cov=lightspeed_agent
```

## Test Files

| File | Scope | Tests |
|------|-------|-------|
| `test_production_mode.py` | Production mode guards (startup + runtime) | 30 |
| `test_settings.py` | Settings validation (K_SERVICE guard) | 4 |
| `test_auth.py` | Token introspection, AuthenticatedUser model | 8 |
| `test_auth_middleware.py` | Authentication middleware dispatch | varies |
| `test_tools.py` | MCP config, skills, tool lists | 14 |
| `test_dcr.py` | Dynamic Client Registration | varies |
| `test_marketplace.py` | Marketplace provisioning | varies |
| `test_a2a.py` | A2A protocol endpoints | varies |
| `test_metering_repository.py` | Usage metering persistence | varies |
| `test_service_control.py` | Google Cloud Service Control | varies |
| `test_usage_plugin.py` | Usage tracking plugin | varies |

## Production Mode Test Matrix

`test_production_mode.py` covers all 10 production guards with both happy-path and error-path tests.

### Settings Validation (Startup Guards)

| Guard | Test Class | Error Tests | What's Tested |
|-------|-----------|-------------|---------------|
| 1 - Force Vertex AI | `TestProductionGuard1VertexAI` | 3 | `GOOGLE_API_KEY` set, `GOOGLE_GENAI_USE_VERTEXAI=false`, `GOOGLE_CLOUD_PROJECT` missing |
| 2 - Force JWT | `TestProductionGuard2JWT` | 1 | `SKIP_JWT_VALIDATION=true` |
| 3 - Disable debug | `TestProductionGuard3Debug` | 1 | `DEBUG=true` |
| 4 - Force HTTPS | `TestProductionGuard4HTTPS` | 2 | `AGENT_PROVIDER_URL` http, `MCP_SERVER_URL` http |
| 5 - Force PostgreSQL | `TestProductionGuard5PostgreSQL` | 1 | `DATABASE_URL` with sqlite |
| 6 - Force MCP http | `TestProductionGuard6MCPTransport` | 1 | `MCP_TRANSPORT_MODE=stdio` |
| 7 - Force JWT forwarding | `TestProductionGuard7JWTForwarding` | 2 | `LIGHTSPEED_CLIENT_ID` set, `LIGHTSPEED_CLIENT_SECRET` set |
| 9 - Require SSO | `TestProductionGuard9SSO` | 2 | `RED_HAT_SSO_CLIENT_ID` empty, `RED_HAT_SSO_CLIENT_SECRET` empty |
| 10 - Require DCR | `TestProductionGuard10DCR` | 3 | `DCR_ENABLED=false`, `DCR_INITIAL_ACCESS_TOKEN` empty, `DCR_ENCRYPTION_KEY` empty |

### Runtime Guards

| Guard | Test Class | Tests | What's Tested |
|-------|-----------|-------|---------------|
| 7 - MCP headers | `TestProductionGuard7MCPHeaders` | 3 | JWT forwarded in prod, empty dict when no JWT, service-account used in non-prod |
| 8 - CORS (agent) | `TestProductionGuard8CORSAgent` | 2 | CORSMiddleware absent in prod, present in non-prod |
| 8 - CORS (marketplace) | `TestProductionGuard8CORSMarketplace` | 2 | CORSMiddleware absent in prod, present in non-prod |

### Cross-Cutting Tests

| Test Class | Tests | What's Tested |
|-----------|-------|---------------|
| `TestProductionHappyPath` | 4 | Valid production config passes all guards, key fields asserted |
| `TestProductionMultipleViolations` | 2 | Multiple violations reported together, violation count in header |
| `TestProductionFalseBypassesGuards` | 1 | All insecure settings allowed when `PRODUCTION=false` |
