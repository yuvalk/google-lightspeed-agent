# STRIDE Threat Model -- Red Hat Lightspeed Agent for Google Cloud

| Field       | Value                                           |
|-------------|-------------------------------------------------|
| Compliance  | SEC-RA-REQ-2 (Red Hat Threat Modeling)           |
| Date        | 2026-03-31                                       |
| Version     | 1.0                                              |
| Authors     | Security Engineering Team                        |
| Status      | Initial Release                                  |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Overview](#2-system-overview)
3. [Architecture Diagram](#3-architecture-diagram)
4. [Data Flow Diagrams](#4-data-flow-diagrams)
5. [Trust Boundaries](#5-trust-boundaries)
6. [STRIDE Threat Analysis](#6-stride-threat-analysis)
   - [6.1 Spoofing](#61-spoofing)
   - [6.2 Tampering](#62-tampering)
   - [6.3 Repudiation](#63-repudiation)
   - [6.4 Information Disclosure](#64-information-disclosure)
   - [6.5 Denial of Service](#65-denial-of-service)
   - [6.6 Elevation of Privilege](#66-elevation-of-privilege)
7. [Risk Summary Matrix](#7-risk-summary-matrix)
8. [Mitigation Status Summary](#8-mitigation-status-summary)
9. [Remediation Roadmap](#9-remediation-roadmap)
10. [Appendix A: Data Classification](#10-appendix-a-data-classification)
11. [Appendix B: Security Controls Inventory](#11-appendix-b-security-controls-inventory)

---

## 1. Executive Summary

This document presents a comprehensive STRIDE threat model for the Red Hat Lightspeed Agent for Google Cloud, an AI-powered Agent-to-Agent (A2A) service that provides natural language access to Red Hat Insights APIs. This analysis was performed to satisfy SEC-RA-REQ-2 (Red Hat Threat Modeling requirement) and to identify, classify, and prioritize security risks prior to general availability.

The system exposes sensitive Red Hat infrastructure data (system inventory, CVEs, advisories, subscriptions) through an LLM-powered conversational interface, integrates with Google Cloud Marketplace for commercial provisioning and billing, and relies on multiple external trust boundaries including Google Gemini, Red Hat SSO (Keycloak), and Google Cloud Pub/Sub.

**Key findings:**

- **34 threats** identified across all six STRIDE categories.
- **10 threats rated UNMITIGATED**, including 4 marked CRITICAL.
- **Critical risks** center on: unauthenticated Pub/Sub event ingestion (T-3/D-6/E-3), absence of LLM input size limits (D-3), and unbounded MCP tool call amplification (D-5).
- **13 threats are Partially Mitigated** and require additional hardening.
- **11 threats are Mitigated** with existing controls but should be monitored.
- **Additional risks** identified in CORS configuration (T-6), supply chain (T-7), CI/CD pipeline (T-8), TLS management (S-5), network policies (I-6), and secrets rotation (E-5).

Immediate remediation is recommended for all CRITICAL and UNMITIGATED threats before the next production release.

---

## 2. System Overview

The google-lightspeed-agent is an AI-powered A2A (Agent-to-Agent) service providing natural language access to Red Hat Insights APIs. Supported Insights capabilities include:

- **Advisor** -- configuration analysis and recommendations
- **Inventory** -- system inventory queries and management
- **Vulnerability** -- CVE analysis and remediation guidance
- **Remediations** -- playbook creation and issue resolution
- **Patch** -- system patching and update management
- **Image Builder** -- custom RHEL image creation
- **Subscriptions** -- activation keys and subscription information

The service is built with Google Agent Development Kit (ADK) and uses Gemini 2.5 Flash as the underlying LLM. It integrates with Google Cloud Marketplace for provisioning and billing via a separate handler service.

### Two-Service Architecture

| Service              | Port | Description                                                            |
|----------------------|------|------------------------------------------------------------------------|
| Lightspeed Agent     | 8000 | Main AI agent. A2A JSON-RPC 2.0 protocol. Scales to zero on Cloud Run |
| Marketplace Handler  | 8001 | Google Cloud Marketplace integration. Pub/Sub events. DCR endpoint     |

### Supporting Infrastructure

| Component      | Port        | Description                                    |
|----------------|-------------|------------------------------------------------|
| MCP Sidecar    | 8081        | Red Hat Insights MCP server (tool provider)    |
| PostgreSQL     | 5432, 5433  | Marketplace DB + Session DB (separate instances)|
| Redis          | 6379        | Distributed rate limiting backend              |

---

## 3. Architecture Diagram

```
                        TRUST BOUNDARY: INTERNET (Zone 1 - Untrusted)
 ========================================================================================
 |                                                                                      |
 |   +-----------+        +-------------------+        +---------------------+          |
 |   | End Users |        | Gemini Enterprise |        | Google Cloud        |          |
 |   | (A2A      |        | (DCR initiator)   |        | Pub/Sub             |          |
 |   |  Clients) |        +--------+----------+        +----------+----------+          |
 |   +-----+-----+                 |                              |                     |
 |         |                       |                              |                     |
 =========|=======================|==============================|======================
          |                       |                              |
          | HTTPS/A2A             | HTTPS/JWT                   | HTTPS/POST
          | JSON-RPC 2.0         | software_statement           | (NO signature
          | Bearer Token          |                              |  verification!)
          |                       |                              |
 =========|=======================|==============================|======================
 |        |    TRUST BOUNDARY: GOOGLE CLOUD SERVICES (Zone 2 - Semi-Trusted)            |
 |        |                       |                              |                     |
 |   +----v----+            +-----v------+               +-------v--------+            |
 |   | Cloud   |            | Cloud Run  |               | Cloud Run      |            |
 |   | Run LB  |            | (Handler)  |               | (Handler)      |            |
 |   +----+----+            +-----+------+               +-------+--------+            |
 |        |                       |                              |                     |
 =========|=======================|==============================|======================
          |                       |                              |
 =========|=======================|==============================|======================
 |        |    TRUST BOUNDARY: INTERNAL SERVICES (Zone 3 - Trusted)                     |
 |        |                       |                              |                     |
 |   +----v-----------------------v------------------------------v--------+            |
 |   |                                                                     |            |
 |   |   +-------------------+      +-------------------+                 |            |
 |   |   | Lightspeed Agent  |      | Marketplace       |                 |            |
 |   |   | (port 8000)       |      | Handler           |                 |            |
 |   |   |                   |      | (port 8001)       |                 |            |
 |   |   | - A2A JSON-RPC    |      | - /dcr endpoint   |                 |            |
 |   |   | - Auth Middleware  |      | - /marketplace/*  |                 |            |
 |   |   | - Rate Limiting   |      | - Pub/Sub handler |                 |            |
 |   |   +--------+----------+      +--------+----------+                 |            |
 |   |            |                           |                            |            |
 |   |            |  stdio/HTTP               |                            |            |
 |   |            v                           |                            |            |
 |   |   +-------------------+                |                            |            |
 |   |   | MCP Sidecar       |                |                            |            |
 |   |   | (port 8081)       +-----> Red Hat Insights APIs (External)      |            |
 |   |   +-------------------+                |                            |            |
 |   |                                        |                            |            |
 |   +-----------------------------------------+----------------------------+            |
 |                                                                                      |
 =========|=======================|==============================|======================
          |                       |                              |
 =========|=======================|==============================|======================
 |        |    TRUST BOUNDARY: DATA STORES (Zone 4 - Highly Trusted)                    |
 |        |                       |                              |                     |
 |   +----v---------+     +------v---------+     +--------------v----+                 |
 |   | PostgreSQL   |     | PostgreSQL     |     | Redis             |                 |
 |   | Marketplace  |     | Sessions       |     | (port 6379)       |                 |
 |   | (port 5432)  |     | (port 5433)    |     | Rate limiting     |                 |
 |   | - Accounts   |     | - ADK sessions |     |                   |                 |
 |   | - Entitle.   |     | - Conversation |     +-------------------+                 |
 |   | - DCR clients|     |   history      |                                           |
 |   | - Usage      |     +----------------+                                           |
 |   +--------------+                                                                   |
 |                                                                                      |
 ========================================================================================

 External Dependencies (Zone 2):
 +------------------+     +------------------+     +------------------+
 | Google Gemini    |     | Red Hat SSO      |     | Google Procurement|
 | API (LLM)       |     | (Keycloak)       |     | API               |
 | - Query + Tools  |     | - Token Introsp. |     | - Account valid.  |
 | - All user data  |     | - DCR endpoint   |     | - Order valid.    |
 |   sent here      |     | - Scope check    |     | - Entitlements    |
 +------------------+     +------------------+     +------------------+
```

---

## 4. Data Flow Diagrams

### 4.1 User Query Flow (A2A)

```
 Client                Agent (8000)           Keycloak          Gemini API        MCP (8081)        Insights APIs
   |                       |                     |                  |                 |                   |
   |  POST / (JSON-RPC)    |                     |                  |                 |                   |
   |  Authorization: Bearer|                     |                  |                 |                   |
   |---------------------->|                     |                  |                 |                   |
   |                       |                     |                  |                 |                   |
   |                       |  POST /introspect   |                  |                 |                   |
   |                       |  (client_credentials)|                 |                 |                   |
   |                       |-------------------->|                  |                 |                   |
   |                       |  {active:true,      |                  |                 |                   |
   |                       |   scope:agent:..}   |                  |                 |                   |
   |                       |<--------------------|                  |                 |                   |
   |                       |                     |                  |                 |                   |
   |                       |  Rate limit check   |                  |                 |                   |
   |                       |  (Redis EVAL)       |                  |                 |                   |
   |                       |                     |                  |                 |                   |
   |                       |  Order validation   |                  |                 |                   |
   |                       |  (DB lookup)        |                  |                 |                   |
   |                       |                     |                  |                 |                   |
   |                       |  Send prompt + context                 |                 |                   |
   |                       |--------------------------------->|                 |                   |
   |                       |                     |             |  tool_call()    |                   |
   |                       |                     |             |---------------->|                   |
   |                       |                     |             |                 |  API request      |
   |                       |                     |             |                 |  (Bearer token)   |
   |                       |                     |             |                 |------------------>|
   |                       |                     |             |                 |  API response     |
   |                       |                     |             |                 |<------------------|
   |                       |                     |             |  tool_result    |                   |
   |                       |                     |             |<----------------|                   |
   |                       |  LLM response       |             |                 |                   |
   |                       |<---------------------------------|                 |                   |
   |  JSON-RPC response   |                     |                  |                 |                   |
   |<----------------------|                     |                  |                 |                   |
```

### 4.2 Marketplace Provisioning Flow

```
 Google Pub/Sub         Handler (8001)        Procurement API      PostgreSQL
   |                       |                     |                    |
   |  POST /marketplace/   |                     |                    |
   |  pubsub               |                     |                    |
   |  (NO auth!)           |                     |                    |
   |---------------------->|                     |                    |
   |                       |                     |                    |
   |                       |  Parse event type   |                    |
   |                       |  (ENTITLEMENT_      |                    |
   |                       |   ACTIVE, etc.)     |                    |
   |                       |                     |                    |
   |                       |  GET /accounts/{id} |                    |
   |                       |-------------------->|                    |
   |                       |  Account details    |                    |
   |                       |<--------------------|                    |
   |                       |                     |                    |
   |                       |  INSERT/UPDATE      |                    |
   |                       |  account +          |                    |
   |                       |  entitlement        |                    |
   |                       |-------------------------------------------->|
   |                       |                     |                    |
   |  200 OK               |                     |                    |
   |<----------------------|                     |                    |
```

### 4.3 Dynamic Client Registration (DCR) Flow

```
 Gemini Enterprise      Handler (8001)        Google Certs          Keycloak DCR       PostgreSQL
   |                       |                     |                    |                    |
   |  POST /dcr            |                     |                    |                    |
   |  {software_statement} |                     |                    |                    |
   |---------------------->|                     |                    |                    |
   |                       |                     |                    |                    |
   |                       |  Decode JWT header  |                    |                    |
   |                       |  Extract kid        |                    |                    |
   |                       |                     |                    |                    |
   |                       |  GET /certs (cached)|                    |                    |
   |                       |-------------------->|                    |                    |
   |                       |  X.509 certificates |                    |                    |
   |                       |<--------------------|                    |                    |
   |                       |                     |                    |                    |
   |                       |  Verify RS256 sig   |                    |                    |
   |                       |  Check iss, aud, exp|                    |                    |
   |                       |                     |                    |                    |
   |                       |  POST /clients-     |                    |                    |
   |                       |  registrations/oidc |                    |                    |
   |                       |  (Initial Access    |                    |                    |
   |                       |   Token)            |                    |                    |
   |                       |-------------------------------------------->|                    |
   |                       |  {client_id,        |                    |                    |
   |                       |   client_secret}    |                    |                    |
   |                       |<--------------------------------------------|                    |
   |                       |                     |                    |                    |
   |                       |  Encrypt secret     |                    |                    |
   |                       |  (Fernet)           |                    |                    |
   |                       |  INSERT client      |                    |                    |
   |                       |------------------------------------------------------------>|
   |                       |                     |                    |                    |
   |  {client_id,          |                     |                    |                    |
   |   client_secret}      |                     |                    |                    |
   |<----------------------|                     |                    |                    |
```

### 4.4 Token Validation Flow

```
 Request                Auth Middleware        Keycloak Introspection    DB (Order Check)
   |                       |                          |                       |
   |  Authorization:       |                          |                       |
   |  Bearer <token>       |                          |                       |
   |---------------------->|                          |                       |
   |                       |                          |                       |
   |                       |  Is path public?         |                       |
   |                       |  (PUBLIC_PATHS check)    |                       |
   |                       |                          |                       |
   |                       |  SKIP_JWT_VALIDATION?    |                       |
   |                       |  (if true: ALLOW ALL)    |                       |
   |                       |                          |                       |
   |                       |  POST /introspect        |                       |
   |                       |  auth=(client_id,secret) |                       |
   |                       |------------------------->|                       |
   |                       |  {active: true/false,    |                       |
   |                       |   scope: "...",          |                       |
   |                       |   azp: "client_id"}      |                       |
   |                       |<-------------------------|                       |
   |                       |                          |                       |
   |                       |  Check: active == true   |                       |
   |                       |  Check: scope contains   |                       |
   |                       |    "agent:insights"      |                       |
   |                       |                          |                       |
   |                       |  Resolve order_id        |                       |
   |                       |  from DCR client table   |                       |
   |                       |------------------------------------------>|
   |                       |  Verify entitlement      |                       |
   |                       |  state == ACTIVE         |                       |
   |                       |<------------------------------------------|
   |                       |                          |                       |
   |  ALLOW / DENY         |                          |                       |
   |<----------------------|                          |                       |
```

---

## 5. Trust Boundaries

| Zone | Trust Level    | Components                                                                 | Description                                                                                          |
|------|----------------|----------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| 1    | Untrusted      | Internet, end users, external A2A clients                                  | All inbound traffic. No implicit trust. Must be authenticated and authorized before processing.       |
| 2    | Semi-Trusted   | Google Cloud services (Pub/Sub, Marketplace, Gemini API, Cloud Run)        | Google-managed infrastructure. Trust is based on contractual agreements and Google's security posture. |
| 3    | Trusted        | Internal services (Lightspeed Agent, Marketplace Handler, MCP sidecar)     | Application code under our control. Trusted but must still enforce least privilege internally.        |
| 4    | Highly Trusted | Data stores (PostgreSQL instances, Redis), secrets (env vars, Secret Mgr)  | Contains sensitive customer data, credentials, and encryption keys. Strictest access controls.        |

### Trust Boundary Crossings

| Crossing                        | From Zone | To Zone | Protocol        | Authentication              | Notes                                      |
|---------------------------------|-----------|---------|-----------------|-----------------------------|--------------------------------------------|
| Client to Agent                 | 1         | 3       | HTTPS/JSON-RPC  | Bearer token (Keycloak)     | Primary attack surface                     |
| Pub/Sub to Handler              | 2         | 3       | HTTPS/POST      | **NONE** (CRITICAL GAP)     | No signature verification on events        |
| Gemini Enterprise to DCR        | 1         | 3       | HTTPS/POST      | software_statement JWT      | Google-signed JWT validates origin          |
| Agent to Gemini API             | 3         | 2       | HTTPS           | API key / service account   | All user data crosses this boundary        |
| Agent to MCP Sidecar            | 3         | 3       | stdio/HTTP      | Bearer token pass-through   | Same pod, but crosses process boundary     |
| MCP to Insights APIs            | 3         | 1       | HTTPS           | Bearer token                | Token forwarded from original request      |
| Agent to Keycloak               | 3         | 2       | HTTPS           | Client credentials          | Token introspection                        |
| Handler to Keycloak DCR         | 3         | 2       | HTTPS           | Initial Access Token        | OAuth client creation                      |
| Handler to Procurement API      | 3         | 2       | HTTPS           | Service account             | Account and order validation               |
| Services to PostgreSQL          | 3         | 4       | TCP             | Connection string (password)| Unencrypted by default                     |
| Agent to Redis                  | 3         | 4       | TCP             | Connection URL              | Rate limit state                           |

---

## 6. STRIDE Threat Analysis

### 6.1 Spoofing

| Threat ID | Component | Threat Description | Attack Vector | Impact | Likelihood | Risk | Mitigation Status | Current Mitigation | Recommended Action |
|-----------|-----------|-------------------|---------------|--------|------------|------|-------------------|-------------------|-------------------|
| S-1 | Authentication Middleware (`auth/middleware.py:92`) | **Authentication Bypass via Development Mode.** `SKIP_JWT_VALIDATION=true` disables all authentication. If accidentally enabled in production, any request is accepted without token validation. | Set flag in production environment via misconfigured deployment, leaked `.env`, or CI/CD error. Forge arbitrary tokens. Access all resources and all customers' data. | High | Medium | **High** | Partially Mitigated | Flag exists with warning log (`auth/introspection.py:63`). No production safeguard prevents accidental enabling. The dev user gets full `agent:insights` scope and a hard-coded `dev-order` order ID (`auth/introspection.py:151-160`). | Add startup check that fails hard if `SKIP_JWT_VALIDATION=true` and environment is production (e.g., check `GOOGLE_CLOUD_PROJECT` or explicit `ENVIRONMENT=production` var). Add monitoring and alerting for this flag. |
| S-2 | DCR Service (`dcr/google_jwt.py`) | **Forged DCR Software Statement JWT.** If Google's certificate service is compromised or an attacker performs a MITM attack on the certificate fetch URL, forged JWTs could be accepted for client registration. | MITM on certificate fetch from `googleapis.com`. Craft JWT with valid-appearing signature. Register unauthorized OAuth clients linked to fabricated orders. | High | Low | **Medium** | Mitigated | Hard-coded issuer validation (`dcr/google_jwt.py:20-23`). RS256 algorithm enforced (`dcr/google_jwt.py:199-204`). Certificate cache with 1hr TTL. HTTPS fetch with `httpx`. Audience verification against `agent_provider_url`. Required claims check (`iss`, `iat`, `exp`, `aud`, `sub`). | Consider certificate pinning for Google's cert URL. Log certificate refresh events for audit. Monitor for unexpected issuer changes. |
| S-3 | MCP Integration (`config/settings.py:56-63`) | **MCP Service Account Credential Theft.** `LIGHTSPEED_CLIENT_ID` and `LIGHTSPEED_CLIENT_SECRET` in environment variables could be exposed through logs, error messages, container escape, or process listing. | Extract credentials via container escape, `/proc` filesystem, debug endpoint, or verbose error response. Authenticate to MCP server as the agent. Access all Red Hat Insights data for any customer. | High | Medium | **High** | Partially Mitigated | Credentials stored as Kubernetes Secrets (not ConfigMap). However, the `Settings` pydantic model includes all secrets without `__repr__` redaction. Database URL logged with partial masking (`db/base.py:51`). | Override `Settings.__repr__` and `__str__` to exclude secret fields. Implement log redaction middleware. Use Secret Manager (GCP) with runtime injection instead of env vars. |
| S-4 | Rate Limiting (`ratelimit/middleware.py:267-290`) | **Client Impersonation via Rate Limit Manipulation.** If token claims are manipulated before introspection, one client's rate limit quota could be attributed to another, enabling rate limit evasion or targeted DoS. | Forge or tamper with OAuth token `azp` claim to attribute requests to a different principal's rate limit bucket. | Medium | Low | **Medium** | Mitigated | Server-side token introspection validates all claims before they are used (`auth/introspection.py:49-78`). The `client_id` used for rate limiting is extracted from the verified introspection response, not from the raw JWT. Rate limit principal resolution happens after authentication (`ratelimit/middleware.py:267-290`). | No additional action required. Continue to ensure rate limit keys are derived only from validated claims. |
| S-5 | External Communications (`auth/introspection.py`, `dcr/google_jwt.py`) | **TLS Certificate Validation Bypass / MITM.** HTTPS is assumed for all external communications (Keycloak, Google APIs, Gemini), but no certificate pinning or Certificate Transparency monitoring is implemented. A compromised CA or rogue certificate could enable MITM attacks on critical authentication flows. | Obtain rogue TLS certificate for Keycloak or Google APIs via compromised CA. Intercept introspection responses or certificate fetch requests. Modify authentication decisions or inject forged certificates. | High | Low | **Medium** | Partially Mitigated | All external calls use HTTPS via `httpx` with default CA verification. Google certificate fetch uses HTTPS (`dcr/google_jwt.py:42-101`). Keycloak introspection uses HTTPS (`auth/introspection.py:84-111`). No certificate pinning. No CT log monitoring. | Implement certificate pinning for critical endpoints (Keycloak introspection, Google cert service). Add Certificate Transparency (CT) log monitoring. Document certificate rotation procedures. Monitor for certificate anomalies. |

### 6.2 Tampering

| Threat ID | Component | Threat Description | Attack Vector | Impact | Likelihood | Risk | Mitigation Status | Current Mitigation | Recommended Action |
|-----------|-----------|-------------------|---------------|--------|------------|------|-------------------|-------------------|-------------------|
| T-1 | Database Layer (`db/base.py`, `db/models.py`) | **SQL Injection.** Improperly constructed queries could allow SQL injection, enabling data exfiltration or modification. | Craft malicious input that passes through to a raw SQL query. Extract or modify database contents. | High | Low | **Medium** | Mitigated | SQLAlchemy ORM with parameterized queries used throughout (`db/base.py:32-52`, `db/models.py`). All repository methods use the ORM pattern. No raw SQL found in the codebase. | Maintain ban on raw SQL in code reviews. Add static analysis rule (e.g., `bandit` check for raw SQL). |
| T-2 | DCR Service (`dcr/service.py:66-92`) | **DCR Client Secret Compromise via Weak Encryption.** Fernet encryption key could be weak, leaked, or lost on restart. If `DCR_ENCRYPTION_KEY` is not configured, an ephemeral key is generated at runtime, meaning all encrypted secrets become unrecoverable after restart. | Exploit missing `DCR_ENCRYPTION_KEY` configuration. Restart the service to lose the ephemeral key. All previously encrypted client secrets become permanently inaccessible, causing service disruption. Alternatively, extract the key from environment to decrypt all stored secrets. | High | Medium | **High** | Mitigated | Fernet encryption applied to all stored client secrets (`dcr/service.py:80-92`). Key configurable via `DCR_ENCRYPTION_KEY` env var. Warning logged if key is not set (`dcr/service.py:90`). | Require `DCR_ENCRYPTION_KEY` to be set when `DCR_ENABLED=true` (fail on startup if missing). Implement key rotation procedure. Store key in GCP Secret Manager with versioning. |
| T-3 | Marketplace Handler (`marketplace/router.py`, `auth/middleware.py:63`) | **Marketplace Pub/Sub Event Forgery (CRITICAL).** Pub/Sub messages are NOT verified with cryptographic signatures. The `/marketplace/pubsub` endpoint is listed as a public path. The entire `/marketplace/` prefix is public. An attacker could forge marketplace events to create fake accounts and activate entitlements without payment. | POST crafted JSON to `/marketplace/pubsub` with a fabricated `ENTITLEMENT_ACTIVE` event. Trigger account creation and entitlement activation. Gain service access without Google Cloud Marketplace purchase. | High | Medium | **High** | **UNMITIGATED** | **NONE.** `/marketplace/pubsub` is in `PUBLIC_PATHS` (`auth/middleware.py:63`). All paths starting with `/marketplace/` are public via `PUBLIC_PREFIXES` (`auth/middleware.py:67-69`). No Pub/Sub push subscription JWT verification. No request origin validation. | **CRITICAL** -- Implement Google Pub/Sub push subscription JWT signature verification (verify the `Authorization: Bearer` token Google sends with push messages). Add IP allowlisting for Google's Pub/Sub IP ranges. Split DCR and Pub/Sub into separate authenticated endpoints. |
| T-4 | LLM Integration (`core/agent.py:13-63`) | **LLM Prompt Injection.** User input is forwarded to Gemini without sanitization. Malicious prompts could manipulate LLM behavior to reveal system instructions, bypass behavioral constraints, or generate harmful outputs. | Craft jailbreak prompt submitted via A2A JSON-RPC. Reveal system instruction text. Bypass read-only constraints. Cause agent to produce misleading security recommendations. | Medium | Medium | **Medium** | Partially Mitigated | System instruction set in `core/agent.py:13-63` defines agent behavior. `MCP_READ_ONLY` setting limits available tools to read-only operations (`config/settings.py:72-75`). No prompt sanitization or injection detection applied to user input. | Add prompt injection detection (e.g., input classification model). Implement input length limits. Add content filtering on outputs. Consider guardrail models. |
| T-5 | MCP Integration (`tools/mcp_config.py`) | **MCP Tool Output Manipulation.** MCP server responses are not validated against expected schemas. A compromised or buggy MCP server could return falsified data that the LLM presents as authoritative. | Compromise MCP sidecar or intercept stdio/HTTP communication. Return falsified vulnerability data (e.g., hide critical CVEs). Agent presents false data to users as verified Insights output. | High | Low | **Medium** | **UNMITIGATED** | No response schema validation. No integrity checks on MCP tool outputs. Agent trusts MCP responses implicitly. | Implement response schema validation for MCP tool outputs. Add integrity checks (e.g., response signing). Monitor for anomalous MCP response patterns. |
| T-6 | CORS Configuration (`api/app.py:150-152`, `marketplace/app.py:86-88`) | **CORS Misconfiguration -- Wildcard Origin with Credentials.** Both the Agent and Marketplace Handler configure CORS with `allow_origins=["*"]` and `allow_credentials=True`. While browsers should reject this per the CORS spec (wildcard origin cannot be combined with credentials), this represents a defense-in-depth gap and indicates the configuration has not been hardened for production. A misconfigured or non-compliant HTTP client could exploit this. | Host malicious page that makes cross-origin requests to the agent API. If browser does not enforce CORS correctly (or if a non-browser client is used), credentials could be sent cross-origin. Steal user tokens or session data. | Medium | Medium | **Medium** | **UNMITIGATED** | CORS middleware added with development-mode wildcard (`api/app.py:151` comment: "Allow all origins for development"). Both services affected. No environment-specific CORS configuration. | **Restrict `allow_origins` to known client origins in production** (e.g., specific Gemini Enterprise URLs). Set `allow_credentials=False` if credentials are not needed cross-origin. Make CORS origins configurable via environment variable. Add CORS configuration to deployment checklist. |
| T-7 | Container Build (`Containerfile`, `pyproject.toml`) | **Supply Chain / Dependency Compromise.** The application depends on external Python packages (google-adk, google-genai, httpx, sqlalchemy, cryptography, etc.) and a base container image (UBI 9 Python 3.12). A compromised dependency or base image could inject malicious code into the production environment. | Compromise a PyPI package used by the application. Inject malicious code into a transitive dependency. Publish backdoored base image. Exploit during build or runtime. | High | Low | **Medium** | Partially Mitigated | Trivy CVE scanning in CI (`cloudbuild.yaml`). UBI 9 base image from Red Hat (trusted registry). Multi-stage build minimizes attack surface. `requirements-agent.txt` and `requirements-handler.txt` pin dependencies. | Pin all dependency versions with hashes. Use private PyPI mirror. Implement SBOM (Software Bill of Materials) generation. Add container image signing and attestation (cosign/sigstore). Monitor dependency advisories. Review transitive dependencies. |
| T-8 | CI/CD Pipeline (`cloudbuild.yaml`, `deploy/cloudrun/`) | **CI/CD Pipeline Tampering.** Cloud Build configuration and deployment scripts could be modified to inject malicious code, alter environment variables, or deploy compromised images. Service accounts used by CI/CD may have excessive permissions. | Compromise developer credentials with Cloud Build access. Modify `cloudbuild.yaml` to skip security scans or inject malicious build steps. Alter deployment scripts to set `SKIP_JWT_VALIDATION=true`. Push compromised image to registry. | High | Low | **High** | Partially Mitigated | Cloud Build uses `cloudbuild.yaml` from version-controlled repository. Trivy scanning step exists. Deployment scripts require explicit execution. | Implement branch protection rules requiring PR review for CI/CD config changes. Restrict Cloud Build service account permissions (least privilege). Add image digest verification in deployment manifests. Implement deployment approval gates. Separate CI/CD service accounts for build vs. deploy. |

### 6.3 Repudiation

| Threat ID | Component | Threat Description | Attack Vector | Impact | Likelihood | Risk | Mitigation Status | Current Mitigation | Recommended Action |
|-----------|-----------|-------------------|---------------|--------|------------|------|-------------------|-------------------|-------------------|
| R-1 | Logging System (`config/settings.py:152-159`) | **Insufficient Audit Logging.** Logs are written to stdout in JSON format without integrity protection. In a containerized environment, logs could be tampered with, deleted, or lost during container restarts. No cryptographic log integrity (signing or WORM storage). | Access container logs. Delete or modify audit entries. Cover tracks after unauthorized access. Dispute that an action occurred. | High | Low | **High** | Partially Mitigated | JSON-formatted structured logging (`config/settings.py:152-159`). Authentication events logged with user ID (`auth/middleware.py:125`). Marketplace events logged with event IDs. Log level configurable. | Implement centralized immutable logging (WORM storage, e.g., Google Cloud Logging with organization-level log sinks). Add cryptographic log signing. Set log retention policies. Enable log-based alerting. |
| R-2 | Usage Tracking (`api/a2a/usage_plugin.py`) | **Token Usage Non-Repudiation Gap.** Insufficient audit trail linking tokens to specific actions. Users could deny making specific requests or claim actions were taken without their authorization. | User performs destructive action via agent. Denies having made the request. Insufficient logging makes it impossible to prove the action chain. | Medium | Medium | **Medium** | Partially Mitigated | User identity logged on authentication (`auth/middleware.py:119-125`). Usage tracking plugin exists for metering (`api/a2a/usage_plugin.py`). Order ID tracked per request. | Log complete audit trail per request: `user_id`, `client_id`, `order_id`, `timestamp`, `action`, `request_id`, `tool_calls`, `result_summary`. Correlate with MCP tool invocations. Store in immutable audit log. |
| R-3 | DCR Service (`dcr/service.py:112-168`) | **DCR Registration Audit Gap.** No immutable audit log of which Google account registered which OAuth client, when, and with what claims. Critical for investigating credential misuse. | Malicious actor registers OAuth client via DCR. Later denies involvement. Insufficient audit trail makes attribution difficult. | Medium | Low | **Medium** | Partially Mitigated | DCR registration has logging at INFO level (`dcr/service.py:124`, `dcr/service.py:297-301`, `dcr/service.py:384-388`). JWT claims (order_id, account_id) are logged. | Log all registration attempts (success and failure) with full context: JWT claims, source IP, timestamp, resulting client_id. Store in immutable audit log separate from application logs. Add alerting for unusual registration patterns. |

### 6.4 Information Disclosure

| Threat ID | Component | Threat Description | Attack Vector | Impact | Likelihood | Risk | Mitigation Status | Current Mitigation | Recommended Action |
|-----------|-----------|-------------------|---------------|--------|------------|------|-------------------|-------------------|-------------------|
| I-1 | Error Handling (`config/settings.py:235-238`) | **Error Message Information Leakage.** In debug mode, detailed error information including stack traces, file paths, internal URLs, and database connection strings may be exposed to clients. | Set `DEBUG=true` in production (misconfiguration). Trigger errors. Extract internal architecture details, file paths, dependency versions from stack traces. | Medium | Medium | **Medium** | Partially Mitigated | Debug mode configurable via `DEBUG` env var (`config/settings.py:235-238`), defaults to `false`. SQLAlchemy echo controlled by debug flag (`db/base.py:41`). Standard JSON-RPC error format used in production responses. | Enforce `DEBUG=false` in production deployment manifests. Add startup validation (similar to S-1). Implement error sanitization middleware that strips internal details regardless of debug flag. |
| I-2 | Database Layer (`db/models.py`, `db/base.py`) | **Marketplace Customer Data Exposure.** The agent service has full database access to all marketplace accounts, entitlements, and DCR client records. A container compromise or SQL injection would expose all customer data across all tenants. | Compromise agent container. Access database with full privileges. Extract all customer accounts, entitlement details, encrypted client secrets, and order information. | High | Low | **High** | Partially Mitigated | Separate databases for marketplace data (port 5432) and sessions (port 5433). Agent queries scoped by `client_id` during normal operation. Database URL partially masked in logs (`db/base.py:51`). | Implement PostgreSQL Row-Level Security (RLS) to enforce tenant isolation at the database level. Create a read-only database user for the agent service. Enable encryption-at-rest for both PostgreSQL instances. Implement database activity monitoring. |
| I-3 | Marketplace Service (`marketplace/service.py`) | **Sensitive Data in Application Logs.** Marketplace event processing logs include account IDs, entitlement IDs, order IDs, and potentially pricing information. These logs flow to stdout and any configured log aggregator. | Access container logs or log aggregation system. Extract customer account identifiers, order details, and business relationship information. | Medium | Medium | **Medium** | **UNMITIGATED** | No log redaction for sensitive marketplace fields. All event data logged at INFO level. No access controls specific to marketplace logs. | Implement log redaction for sensitive fields (account IDs, order IDs). Restrict log access to authorized personnel. Use structured logging with sensitivity tagging. Apply data masking in log aggregation. |
| I-4 | Session Management (ADK Session Store) | **Conversation History Exposure.** ADK session storage in PostgreSQL contains full conversation history which may include system hostnames, IP addresses, CVE identifiers, vulnerability details, and infrastructure topology discussed by users. | Access session database. Extract conversation history. Obtain detailed knowledge of customer infrastructure, unpatched vulnerabilities, and security posture. | High | Low | **Medium** | Partially Mitigated | Session database is separate from marketplace database (different connection string, `config/settings.py:197-200`). Logical isolation between session and business data. | Implement session data encryption at the application level (encrypt conversation content before storage). Add TTL-based session cleanup (auto-delete sessions after configurable period). Audit access to session database. Implement data retention policies. |
| I-6 | Network Configuration (`deploy/podman/*.yaml`) | **Internal Service Data Exposure via Missing Network Policies.** No network policies restrict pod-to-pod communication. The agent pod can access the marketplace PostgreSQL directly (and vice versa). Redis has no authentication and is accessible from any pod on the network. MCP sidecar listens on `0.0.0.0:8081` without authentication. | Compromise any container in the pod network. Access Redis directly (no auth) to manipulate rate limit state. Connect to marketplace PostgreSQL from agent pod. Access MCP sidecar from non-agent container. | High | Low | **Medium** | **UNMITIGATED** | Pod-internal networking isolates services from the internet. Separate PostgreSQL instances for marketplace and sessions. No Kubernetes NetworkPolicies defined. No Redis authentication configured. MCP sidecar binds to all interfaces without auth. | Implement Kubernetes NetworkPolicies restricting pod-to-pod traffic. Enable Redis AUTH (require password). Restrict MCP sidecar to localhost or add mTLS. Define egress policies limiting which services can reach external endpoints. Add network segmentation between agent and marketplace handler. |
| I-5 | LLM Integration (`core/agent.py`) | **Data Sent to External LLM.** All user queries and Red Hat Insights API responses (including system inventory, CVE data, advisory details) are sent to the Google Gemini API. Sensitive infrastructure data leaves Red Hat's direct control and is processed by Google's infrastructure. | By design, all conversational data traverses the Google Gemini API boundary. Data includes: system hostnames, IP addresses, CVE exposure details, remediation status, subscription information. Google employees or systems could theoretically access this data. | Medium | High (by design) | **Medium** | Partially Mitigated | Google Cloud data processing agreements in place. Vertex AI option available for managed infrastructure with stronger data governance (`config/settings.py:21-24`). Gemini API accessed over HTTPS. | Document data handling and classification for all data sent to Gemini. Implement data residency controls. Evaluate Vertex AI deployment for stricter data governance. Consider data minimization (send only necessary context to LLM). Add data flow documentation to customer-facing security docs. |

### 6.5 Denial of Service

| Threat ID | Component | Threat Description | Attack Vector | Impact | Likelihood | Risk | Mitigation Status | Current Mitigation | Recommended Action |
|-----------|-----------|-------------------|---------------|--------|------------|------|-------------------|-------------------|-------------------|
| D-1 | Rate Limiting (`ratelimit/middleware.py:230-239`) | **Redis Failure Cascading to Service Unavailability.** When Redis is unavailable, the rate limiter returns HTTP 503 for all requests, effectively taking down the entire service even though the core agent functionality is unaffected. | Redis instance crashes, network partition, or OOM. All authenticated requests receive 503. Complete service outage until Redis recovers. | High | Medium | **High** | Partially Mitigated | Returns 503 on Redis error with descriptive message (`ratelimit/middleware.py:232-239`). Startup connection check (`api/app.py`). Configurable Redis timeout (`config/settings.py:142-145`, default 200ms). | Implement circuit breaker pattern: after N consecutive Redis failures, fall back to local in-memory rate limiting with conservative limits. Configure Redis HA (Sentinel or Cluster). Add Redis health monitoring and alerting. |
| D-2 | Database Layer (`db/base.py:48-49`) | **Database Connection Pool Exhaustion.** Default connection pool is small (5 connections + 10 overflow = 15 max). Under concurrent load, pool exhaustion causes request failures and cascading timeouts. | Send sustained concurrent requests exceeding pool capacity. Exhaust connection pool. Subsequent requests queue and timeout. Service becomes unresponsive. | High | Medium | **High** | Partially Mitigated | Pool size configurable via `DATABASE_POOL_SIZE` and `DATABASE_POOL_MAX_OVERFLOW` (`config/settings.py:186-193`). Overflow connections allowed (`db/base.py:48-49`). Async sessions with proper cleanup (`db/base.py:72-86`). | Tune pool size based on expected concurrency. Add PgBouncer connection pooler in front of PostgreSQL. Monitor connection pool usage metrics. Set connection pool wait timeout. Add health check that reports pool saturation. |
| D-3 | LLM Integration (`core/agent.py`) | **LLM Token/Quota Exhaustion (CRITICAL).** No input size limits on A2A JSON-RPC requests. A malicious client could send extremely large prompts that exhaust the Gemini API quota or incur excessive costs, impacting all users of the service. | Send A2A request with massive prompt (e.g., 1MB of text). Consume disproportionate Gemini API tokens. Exhaust shared quota. Cause billing spike. Deny service to legitimate users. | High | Medium | **High** | **UNMITIGATED** | **NONE.** No input size validation. No token counting or estimation. No per-user token budget. No request timeout specific to LLM calls. Rate limiting exists but does not account for request size. | **CRITICAL** -- Implement input size limits (max characters/tokens per request). Add per-user/per-order token quota tracking. Set request timeouts for LLM calls. Implement cost monitoring and alerting. Consider request size as a factor in rate limiting. |
| D-4 | Rate Limiting (`ratelimit/middleware.py:288-290`) | **Rate Limit Bypass via IP Spoofing.** Unauthenticated requests fall back to IP-based rate limiting using `request.client.host`. If the application sits behind a proxy, this may use `X-Forwarded-For` which can be spoofed by clients. | Rotate `X-Forwarded-For` header values across requests. Each request appears to come from a different IP. Bypass per-IP rate limits. Flood the service with unauthenticated requests. | Medium | Medium | **Medium** | Partially Mitigated | Authenticated requests use `user_id` or `client_id` for rate limiting, not IP (`ratelimit/middleware.py:271-287`). IP fallback only applies to unauthenticated requests. Only POST to `/` is rate-limited for authenticated users. | Validate `X-Forwarded-For` from trusted proxy only (Cloud Run sets this reliably). Configure trusted proxy IP ranges. Consider requiring authentication for all rate-limited endpoints (eliminating IP-based fallback). |
| D-5 | MCP Integration (`tools/insights_tools.py`) | **MCP Tool Call Amplification (CRITICAL).** A single user request could trigger an unbounded number of MCP tool calls. For example, an inventory query returning hundreds of systems could trigger a vulnerability check per system, multiplying API calls exponentially. | Submit query like "check all my systems for critical vulnerabilities." Agent iterates over inventory, calling vulnerability API per system. Hundreds or thousands of MCP tool calls from one request. Overwhelms MCP sidecar, Insights APIs, and agent resources. | High | Medium | **High** | **UNMITIGATED** | **NONE.** No limit on tool call depth or breadth per request. No circuit breaker on MCP calls. No per-tool timeout. LLM autonomously decides how many tool calls to make. | **CRITICAL** -- Implement maximum tool calls per request (e.g., 20). Add circuit breaker pattern for MCP calls. Set per-tool call timeout. Implement total request timeout. Add LLM instruction to limit iteration. Monitor tool call counts per request. |
| D-6 | Marketplace Handler (`marketplace/router.py`, `auth/middleware.py:63-69`) | **Marketplace Event Flooding.** The `/marketplace/pubsub` endpoint has no rate limiting, no authentication, and no request size limits. An attacker can flood it with fabricated events, overwhelming the handler and database. | Discover the public `/marketplace/pubsub` endpoint. Send thousands of POST requests with fabricated Pub/Sub event payloads. Overwhelm the marketplace handler. Cause database write amplification. Degrade or crash the service. | High | High | **High** | **UNMITIGATED** | **NONE.** Endpoint is explicitly public (`auth/middleware.py:63`). No rate limiting applied (rate limiting only covers `/` path, `ratelimit/middleware.py:210`). No authentication. No request size limits. Linked to T-3. | Implement Google Pub/Sub push subscription JWT verification (same as T-3). Add rate limiting to marketplace endpoints. Implement request size limits. Use async event processing with a queue. Add circuit breaker for database writes. |

### 6.6 Elevation of Privilege

| Threat ID | Component | Threat Description | Attack Vector | Impact | Likelihood | Risk | Mitigation Status | Current Mitigation | Recommended Action |
|-----------|-----------|-------------------|---------------|--------|------------|------|-------------------|-------------------|-------------------|
| E-1 | Authentication (`auth/introspection.py:68-76`) | **OAuth Scope Escalation.** A compromised Keycloak instance or a MITM attack on the introspection endpoint could modify the introspection response to add the `agent:insights` scope to an unauthorized token. | MITM on introspection HTTPS call. Modify response to set `active: true` and add `agent:insights` to scope. Gain full agent access with an unprivileged token. | High | Low | **High** | Mitigated | Server-side token introspection over HTTPS (`auth/introspection.py:84-111`). Hard-coded scope check for `agent:insights` (`auth/introspection.py:72-76`). Client credentials authentication to introspection endpoint. Token introspection is the authoritative source, not local JWT decoding. | Implement HTTPS certificate pinning to Keycloak. Add introspection response caching with short TTL to reduce exposure window. Monitor for introspection endpoint anomalies. |
| E-2 | DCR Service (`dcr/service.py:283-284`, `dcr/service.py:357-358`) | **DCR Client Over-Provisioning.** OAuth clients registered through DCR could potentially receive more capabilities than intended if the grant_types or scopes are not properly constrained. | Exploit misconfiguration in DCR flow. Register client with admin-level grant types. Use over-provisioned client to access resources beyond intended scope. | Medium | Low | **Medium** | Mitigated | Grant types are hard-coded to `["authorization_code", "refresh_token", "client_credentials"]` (`dcr/service.py:283`, `dcr/service.py:357`). Client name prefixed with configurable prefix (`config/settings.py:172-174`). Redirect URIs taken from validated JWT claims. | No immediate action required. Monitor for changes to hard-coded grant types in code reviews. Consider adding scope restrictions to DCR-created clients. |
| E-3 | Marketplace Provisioning (`marketplace/service.py`, `auth/middleware.py:63-69`) | **Marketplace Entitlement Escalation (CRITICAL).** Forged Pub/Sub events could activate premium-tier entitlements without payment. Since the Pub/Sub endpoint has no authentication (see T-3), an attacker could send `ENTITLEMENT_ACTIVE` events with arbitrary plan IDs and gain access to the service without a valid Google Cloud Marketplace purchase. | Discover `/marketplace/pubsub` endpoint. Craft `ENTITLEMENT_ACTIVE` event with desired plan/tier. POST to public endpoint. Handler creates account and activates entitlement. Attacker registers OAuth client via DCR using the fabricated order. Gains full agent access. | High | Medium | **High** | **UNMITIGATED** (linked to T-3) | **NONE.** No Pub/Sub signature verification. No authentication on marketplace endpoints. No cross-validation with Google Procurement API for state transitions (only initial validation in DCR flow). | **CRITICAL** -- Implement Pub/Sub JWT signature verification (same fix as T-3). Validate all entitlement state transitions against Google Procurement API. Add idempotency checks. Implement entitlement state machine with valid transition rules. Alert on unexpected state transitions. |
| E-4 | DCR / Authentication (`dcr/service.py`, `auth/middleware.py:112-116`) | **DCR Client to Agent Service Escalation.** A DCR-created OAuth client could potentially be used to access agent services if scope validation does not properly differentiate between token types or if the order validation can be bypassed. | Register OAuth client via DCR. Obtain token with `agent:insights` scope. Use token to access agent even if the associated entitlement has been suspended or revoked (race condition between entitlement state change and token expiry). | High | Medium | **High** | Partially Mitigated | Order validation checks entitlement state is `ACTIVE` on every request (`auth/middleware.py:112-116`, `auth/middleware.py:176-228`). DCR client lookup maps `client_id` to `order_id`. Entitlement repository checks state. | Implement token type validation (distinguish between DCR-issued tokens and other token types). Restrict DCR client scopes explicitly in Keycloak. Add short token TTL for DCR clients. Implement entitlement state change propagation (revoke active tokens when entitlement is suspended). |
| E-5 | Secrets Management (`config/settings.py`) | **Credential Staleness due to Missing Secrets Rotation.** No secrets rotation policy or mechanism exists for any of the application's critical credentials (Keycloak client secret, MCP service account, DCR encryption key, Google API key, DCR Initial Access Token). Long-lived credentials increase the window for credential-based attacks and make breach detection harder. | Obtain a leaked credential (e.g., from an old backup, log, or compromised developer machine). Use the credential indefinitely since it is never rotated. No alerts fire because the credential remains valid. | High | Medium | **High** | **UNMITIGATED** | No rotation mechanism. No rotation policy documented. No monitoring for credential age. All credentials are static environment variables or Kubernetes Secrets with no TTL. DCR encryption key, if lost, makes all stored secrets unrecoverable. | Implement secrets rotation policy: rotate all credentials every 90 days. Use GCP Secret Manager with automatic rotation where possible. Implement dual-credential support (old + new key accepted during rotation window) for zero-downtime rotation. Add monitoring for credential age. Document rotation procedures for each credential type. |

---

## 7. Risk Summary Matrix

The following matrix maps all identified threats by Impact (vertical) and Likelihood (horizontal):

```
                        L I K E L I H O O D
              +-------------+-------------+-------------+
              |    Low      |   Medium    |    High     |
  +-----------+-------------+-------------+-------------+
  |           | S-2  S-5    | S-1  S-3    | D-6         |
  |   High    | T-1  T-5   | T-3  T-2    |             |
  |           | T-7  T-8   | D-1  D-2    |             |
  |           | E-1  I-2   | D-3  D-5    |             |
  |           | I-4  R-1   | E-3  E-4    |             |
  |           | I-6         | E-5         |             |
  +-----------+-------------+-------------+-------------+
  |           | S-4  E-2   | T-4  T-6    |             |
  |  Medium   | R-3        | I-1  I-3    | I-5         |
  |           |             | D-4  R-2    |             |
  +-----------+-------------+-------------+-------------+
  |           |             |             |             |
  |   Low     |             |             |             |
  |           |             |             |             |
  +-----------+-------------+-------------+-------------+
```

**Risk Rating Legend:**

| Risk Level | Count | Criteria                                        |
|------------|-------|-------------------------------------------------|
| Critical   | 4     | High Impact + Medium/High Likelihood, UNMITIGATED|
| High       | 16    | High Impact or High Likelihood combinations      |
| Medium     | 14    | Medium Impact/Likelihood combinations            |
| Low        | 0     | Low Impact and Low Likelihood                    |

---

## 8. Mitigation Status Summary

| Metric                              | Count | Percentage |
|--------------------------------------|-------|------------|
| **Total threats identified**         | 34    | 100%       |
| Mitigated                            | 11    | 32%        |
| Partially Mitigated                  | 13    | 38%        |
| **UNMITIGATED**                      | **10**| **30%**    |

### UNMITIGATED Threats

| Threat ID | Description                              | Risk   |
|-----------|------------------------------------------|--------|
| T-3       | Pub/Sub Event Forgery                    | **High (CRITICAL)** |
| T-5       | MCP Tool Output Manipulation             | Medium |
| T-6       | CORS Misconfiguration (wildcard + creds) | Medium |
| I-3       | Sensitive Data in Application Logs       | Medium |
| I-6       | Missing Network Policies                 | Medium |
| D-3       | LLM Token/Quota Exhaustion               | **High (CRITICAL)** |
| D-5       | MCP Tool Call Amplification              | **High (CRITICAL)** |
| D-6       | Marketplace Event Flooding               | **High (CRITICAL)** |
| E-3       | Marketplace Entitlement Escalation       | **High (CRITICAL)** |
| E-5       | Missing Secrets Rotation                 | High   |

### Critical Threats Requiring Immediate Action

1. **T-3 / D-6 / E-3** -- Pub/Sub endpoint is completely unauthenticated. Allows event forgery, flooding, and entitlement escalation. Single root cause: missing Pub/Sub JWT verification.
2. **D-3** -- No input size limits on LLM requests. Enables quota exhaustion and cost attacks.
3. **D-5** -- No tool call limits. Single request can trigger unbounded MCP API calls.

---

## 9. Remediation Roadmap

### Phase 1 -- Critical (Immediate, before next release)

| Priority | Threat IDs    | Action                                                  | Effort   | Owner           |
|----------|---------------|---------------------------------------------------------|----------|-----------------|
| P0       | T-3, D-6, E-3| Implement Google Pub/Sub push subscription JWT signature verification on `/marketplace/pubsub`. Add authentication to all `/marketplace/` endpoints. Split DCR and Pub/Sub into separately authenticated endpoints. | Medium   | Backend + Security |
| P0       | D-3           | Implement input size limits (max characters per A2A request). Add per-user/per-order token quota. Set LLM call timeout. | Small    | Backend         |
| P0       | D-5           | Implement max tool calls per request (cap at 20). Add circuit breaker for MCP. Set per-tool timeout. Add total request timeout. | Small    | Backend         |
| P0       | S-1           | Add startup check: fail if `SKIP_JWT_VALIDATION=true` and environment is production. Add runtime monitoring/alerting for this flag. | Small    | Backend + DevOps|

### Phase 2 -- High Priority (Next sprint)

| Priority | Threat IDs | Action                                                     | Effort   | Owner           |
|----------|------------|------------------------------------------------------------|----------|-----------------|
| P1       | T-6        | Restrict CORS `allow_origins` to known client origins in production. Make origins configurable via env var. Set `allow_credentials=False` if not needed. | Small    | Backend         |
| P1       | T-8        | Implement branch protection for CI/CD config. Restrict Cloud Build service account permissions. Add image digest verification. | Medium   | DevOps + Security|
| P1       | E-5        | Implement secrets rotation policy (90-day rotation). Use GCP Secret Manager with automatic rotation. Add credential age monitoring. | Medium   | Security + DevOps|
| P1       | I-2        | Implement PostgreSQL RLS for tenant isolation. Create read-only DB user for agent. Enable encryption-at-rest. | Medium   | Backend + DBA   |
| P1       | I-6        | Implement Kubernetes NetworkPolicies. Enable Redis AUTH. Restrict MCP sidecar to localhost. Define egress policies. | Medium   | DevOps + SRE    |
| P1       | R-1        | Implement centralized immutable logging (Cloud Logging with org-level sinks, WORM retention). Add log signing. | Medium   | DevOps + Security|
| P1       | D-1        | Implement circuit breaker for Redis. Fall back to local in-memory rate limiting. Configure Redis HA (Sentinel). | Medium   | Backend + SRE   |
| P1       | D-2        | Tune database connection pool size. Deploy PgBouncer. Add pool usage monitoring. | Small    | Backend + DBA   |
| P1       | T-2        | Require `DCR_ENCRYPTION_KEY` when `DCR_ENABLED=true` (fail on startup). Implement key rotation. Store in Secret Manager. | Small    | Backend + Security|
| P1       | E-4        | Implement token type validation. Restrict DCR client scopes. Add short TTL. Propagate entitlement state changes to token validity. | Medium   | Backend + IAM   |

### Phase 3 -- Medium Priority (Coming releases)

| Priority | Threat IDs | Action                                                     | Effort   | Owner           |
|----------|------------|------------------------------------------------------------|----------|-----------------|
| P2       | T-4        | Add prompt injection detection. Implement content filtering. Add input classification. | Medium   | ML + Security   |
| P2       | I-3        | Implement log redaction for sensitive marketplace fields. Add sensitivity tagging. | Small    | Backend         |
| P2       | I-4        | Implement session data encryption. Add TTL-based cleanup. Audit session DB access. | Medium   | Backend         |
| P2       | I-5        | Document data handling for Gemini. Evaluate Vertex AI for data residency. Add data minimization. | Medium   | Architecture    |
| P2       | S-3        | Override `Settings.__repr__/__str__` to exclude secrets. Implement log redaction middleware. Migrate to Secret Manager. | Small    | Backend         |
| P2       | R-2, R-3   | Implement comprehensive audit trail (user, client, order, action, request_id, result). Store in immutable audit log. | Medium   | Backend + Security|
| P2       | T-5        | Implement MCP response schema validation. Add integrity checks. | Medium   | Backend         |
| P2       | T-7        | Pin all dependency versions with hashes. Use private PyPI mirror. Implement SBOM generation. Add container image signing (cosign/sigstore). | Medium   | DevOps + Security|
| P2       | S-5        | Implement certificate pinning for Keycloak and Google cert endpoints. Add CT log monitoring. Document rotation procedures. | Medium   | Security        |

---

## 10. Appendix A: Data Classification

| Data Type                    | Classification       | Storage Location         | Encryption at Rest | Notes                                              |
|------------------------------|----------------------|--------------------------|--------------------|----------------------------------------------------|
| OAuth Client Secrets         | RH-Restricted        | PostgreSQL (Marketplace) | Fernet (app-level) | Encrypted with `DCR_ENCRYPTION_KEY`                |
| Registration Access Tokens   | RH-Restricted        | PostgreSQL (Marketplace) | Fernet (app-level) | Encrypted with `DCR_ENCRYPTION_KEY`                |
| Keycloak Client Credentials  | RH-Restricted        | Environment Variables    | No                 | `RED_HAT_SSO_CLIENT_ID`, `RED_HAT_SSO_CLIENT_SECRET`|
| MCP Service Account Creds    | RH-Restricted        | Environment Variables    | No                 | `LIGHTSPEED_CLIENT_ID`, `LIGHTSPEED_CLIENT_SECRET` |
| DCR Encryption Key           | RH-Restricted        | Environment Variables    | No                 | `DCR_ENCRYPTION_KEY`                               |
| Google API Key               | RH-Restricted        | Environment Variables    | No                 | `GOOGLE_API_KEY`                                   |
| DCR Initial Access Token     | RH-Restricted        | Environment Variables    | No                 | `DCR_INITIAL_ACCESS_TOKEN`                         |
| User Bearer Tokens           | RH-Restricted        | Memory (request-scoped)  | N/A                | Held in `ContextVar`, not persisted                |
| Conversation History         | RH-Restricted        | PostgreSQL (Sessions)    | No                 | May contain system names, IPs, CVE data            |
| Customer Account IDs         | RH-Restricted+PII    | PostgreSQL (Marketplace) | No                 | Google Cloud Marketplace account identifiers       |
| Entitlement/Order IDs        | RH-Restricted        | PostgreSQL (Marketplace) | No                 | Links purchases to service access                  |
| User Email/Name              | RH-Restricted+PII    | Memory (request-scoped)  | N/A                | From token introspection, not persisted            |
| System Inventory Data        | RH-Restricted        | Transit (Gemini API)     | TLS in transit     | Hostnames, IPs, OS versions from Insights          |
| CVE/Vulnerability Data       | Internal             | Transit (Gemini API)     | TLS in transit     | Exposure details per system                        |
| Advisory/Recommendation Data | Internal             | Transit (Gemini API)     | TLS in transit     | Configuration recommendations                      |
| Subscription Data            | RH-Restricted        | Transit (Gemini API)     | TLS in transit     | Activation keys, subscription info                 |
| Rate Limit Counters          | Internal             | Redis                    | No                 | Principal keys and request counts                  |
| Usage/Metering Records       | Internal             | PostgreSQL (Marketplace) | No                 | Request counts per order for billing               |
| Agent System Instructions    | Internal             | Source Code              | N/A                | LLM behavior prompt (`core/agent.py:13-63`)        |
| Application Logs             | Internal             | stdout / Log aggregator  | No                 | May contain account IDs, order IDs                 |
| Agent Card Metadata          | Public               | HTTP endpoint            | N/A                | `/.well-known/agent.json` -- public by design      |
| Health Check Responses       | Public               | HTTP endpoint            | N/A                | `/health`, `/healthz`, `/ready`                    |

---

## 11. Appendix B: Security Controls Inventory

| Control                          | Type            | Implementation                                             | File Reference                          | Status     |
|----------------------------------|-----------------|------------------------------------------------------------|-----------------------------------------|------------|
| Bearer Token Authentication      | Preventive      | Keycloak token introspection (RFC 7662)                    | `src/lightspeed_agent/auth/introspection.py` | Active |
| Authentication Middleware         | Preventive      | FastAPI middleware enforcing auth on protected paths        | `src/lightspeed_agent/auth/middleware.py`     | Active |
| OAuth Scope Validation           | Preventive      | Required `agent:insights` scope check                      | `src/lightspeed_agent/auth/introspection.py:72-76` | Active |
| Order/Entitlement Validation     | Preventive      | Active entitlement check on every authenticated request    | `src/lightspeed_agent/auth/middleware.py:176-228` | Active |
| DCR JWT Signature Verification   | Preventive      | RS256 verification with Google X.509 certificates          | `src/lightspeed_agent/dcr/google_jwt.py`     | Active |
| DCR Issuer Validation            | Preventive      | Hard-coded Google issuer URL check                         | `src/lightspeed_agent/dcr/google_jwt.py:20-23, 247` | Active |
| DCR Audience Verification        | Preventive      | JWT audience checked against agent provider URL            | `src/lightspeed_agent/dcr/google_jwt.py:225` | Active |
| Client Secret Encryption         | Preventive      | Fernet symmetric encryption for stored secrets             | `src/lightspeed_agent/dcr/service.py:66-92`  | Active |
| Redis Rate Limiting              | Preventive      | Sliding window rate limits (per-minute, per-hour)          | `src/lightspeed_agent/ratelimit/middleware.py`| Active |
| Multi-Principal Rate Limiting    | Preventive      | Rate limits by order, user, client, or IP                  | `src/lightspeed_agent/ratelimit/middleware.py:267-290` | Active |
| Parameterized SQL Queries        | Preventive      | SQLAlchemy ORM (no raw SQL)                                | `src/lightspeed_agent/db/base.py`, `db/models.py` | Active |
| MCP Read-Only Mode               | Preventive      | Tool filter limiting to read-only operations               | `src/lightspeed_agent/config/settings.py:72-75` | Active |
| Database Separation              | Detective       | Separate PostgreSQL instances for marketplace and sessions | `src/lightspeed_agent/config/settings.py:180-200` | Active |
| Structured JSON Logging          | Detective       | JSON-formatted logs with configurable level                | `src/lightspeed_agent/config/settings.py:152-159` | Active |
| Authentication Event Logging     | Detective       | User identity logged on successful auth                    | `src/lightspeed_agent/auth/middleware.py:125` | Active |
| Marketplace Event Logging        | Detective       | Event type and IDs logged                                  | `src/lightspeed_agent/marketplace/service.py`| Active |
| Usage Tracking Plugin            | Detective       | Request metering for billing and audit                     | `src/lightspeed_agent/api/a2a/usage_plugin.py` | Active |
| Health Check Endpoints           | Detective       | Liveness and readiness probes                              | `src/lightspeed_agent/api/app.py`            | Active |
| Redis Startup Check              | Preventive      | Fail-fast if Redis is unreachable on startup               | `src/lightspeed_agent/api/app.py`            | Active |
| Database Retry on Startup        | Corrective      | Retry logic for database initialization (30 attempts)      | `src/lightspeed_agent/db/base.py:89-130`     | Active |
| OpenTelemetry Tracing            | Detective       | Configurable distributed tracing                           | `src/lightspeed_agent/telemetry/setup.py`    | Optional |
| CVE Scanning (Trivy)             | Detective       | Container image vulnerability scanning in CI               | `cloudbuild.yaml`                            | Active |
| Certificate Cache TTL            | Preventive      | Google certificates cached with 1hr TTL, auto-refresh      | `src/lightspeed_agent/dcr/google_jwt.py:29`  | Active |
| Development Mode Warning         | Detective       | Warning log emitted when `SKIP_JWT_VALIDATION=true`        | `src/lightspeed_agent/auth/introspection.py:63` | Active |
| Public Path Allowlist            | Preventive      | Explicit list of unauthenticated paths                     | `src/lightspeed_agent/auth/middleware.py:54-69` | Active |
| CORS Middleware                  | Preventive      | Cross-origin request handling (currently wildcard)         | `src/lightspeed_agent/api/app.py:149-156`       | **Needs Hardening** |
| Non-Root Container Execution     | Preventive      | Containers run as UID 1001 (non-root)                      | `Containerfile`                                  | Active |
| Multi-Stage Container Build      | Preventive      | Build dependencies excluded from production image          | `Containerfile`                                  | Active |
| Dependency Pinning               | Preventive      | Requirements files pin package versions                    | `requirements-agent.txt`, `requirements-handler.txt` | Active |
| Network Policies                 | Preventive      | Pod-to-pod network segmentation                            | Not implemented                                  | **Missing** |
| Secrets Rotation                 | Corrective      | Periodic credential rotation                               | Not implemented                                  | **Missing** |
| Redis Authentication             | Preventive      | Password protection for Redis                              | Not implemented                                  | **Missing** |

---

*End of document. This threat model should be reviewed and updated whenever significant architectural changes are made to the system, and at minimum on a quarterly basis per SEC-RA-REQ-2.*
