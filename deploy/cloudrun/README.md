# Google Cloud Run Deployment

Deploy the Red Hat Lightspeed Agent for Google Cloud to Google Cloud Run for production use.

## Table of Contents

- [Architecture](#architecture)
- [Service Accounts](#service-accounts)
- [Load Balancer (Optional)](#load-balancer-optional)
  - [Cloud Armor (WAF)](#cloud-armor-waf)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
  - [1. Set Environment Variables](#1-set-environment-variables)
  - [2. Run Setup Script](#2-run-setup-script)
  - [3. Set Up Cloud SQL Database](#3-set-up-cloud-sql-database)
  - [4. Redis Setup for Rate Limiting](#4-redis-setup-for-rate-limiting)
  - [5. Configure Load Balancer (Optional)](#5-configure-load-balancer-optional)
  - [6. Configure Secrets](#6-configure-secrets)
  - [7. Copy MCP Image to GCR](#7-copy-mcp-image-to-gcr)
  - [8. Deploy](#8-deploy)
- [Service Configuration](#service-configuration)
  - [Agent Container](#agent-container)
  - [Using a Different LLM](#using-a-different-llm)
  - [Rate Limiting (Redis)](#rate-limiting-redis)
  - [MCP Output Size Guard](#mcp-output-size-guard)
  - [MCP Server Sidecar](#mcp-server-sidecar)
  - [Copying the MCP Image to GCR](#copying-the-mcp-image-to-gcr)
  - [Customizing MCP Server Configuration](#customizing-mcp-server-configuration)
  - [Custom ADK AI Skills](#custom-adk-ai-skills)
  - [Alternative: Use Docker Hub](#alternative-use-docker-hub)
  - [Scaling](#scaling)
- [How the MCP Server Works](#how-the-mcp-server-works)
- [Authentication](#authentication)
- [Endpoints](#endpoints)
- [Testing the Deployment](#testing-the-deployment)
- [Database Architecture](#database-architecture)
- [Custom Domain](#custom-domain)
- [Testing the Agent](#testing-the-agent)
  - [Authentication Setup for Testing](#authentication-setup-for-testing)
  - [Test Agent Card](#test-agent-card)
  - [Test A2A Requests with Local Proxy](#test-a2a-requests-with-local-proxy)
  - [Test with A2A Inspector](#test-with-a2a-inspector)
  - [Cleanup After Testing](#cleanup-after-testing)
  - [Testing Without Proxy (Direct Cloud Run Access)](#testing-without-proxy-direct-cloud-run-access)
  - [Troubleshooting Testing Issues](#troubleshooting-testing-issues)
- [Testing DCR on Cloud Run](#testing-dcr-on-cloud-run)
- [GMA SSO API Configuration (Staging vs Production)](#gma-sso-api-configuration-staging-vs-production)
- [Audit Logging](#audit-logging)
- [Monitoring](#monitoring)
  - [Built-in Cloud Run Metrics](#built-in-cloud-run-metrics)
  - [OpenTelemetry Business Metrics](#opentelemetry-business-metrics)
  - [Alerts](#alerts)
- [Troubleshooting](#troubleshooting)
  - [DCR Requests Not Reaching Marketplace Handler](#dcr-requests-not-reaching-marketplace-handler)
- [Cleanup / Teardown](#cleanup--teardown)

## Architecture

The deployment consists of **two separate Cloud Run services** plus **Cloud Memorystore for Redis** (for rate limiting), with optional **per-service Google Cloud Load Balancers (GCLB)** for SSL termination, DDoS protection, and WAF:

```
                              Google Cloud Marketplace
                                       │
                 ┌─────────────────────┴─────────────────────┐
                 │                                           │
                 ▼                                           ▼
      ┌──────────────────────┐                ┌──────────────────────────────────┐
      │  Pub/Sub (Events)    │                │  Gemini Enterprise (DCR)         │
      └──────────┬───────────┘                └──────────────────┬───────────────┘
                 │ (internal)                                    │ (external)
                 │                                               │
                 │  ┌──────────────────────────┐  ┌──────────────┴─────────────┐
                 │  │  Handler LB (Optional)   │  │  Agent LB (Optional)       │
                 │  │  ──────────────────────  │  │  ────────────────────      │
                 │  │  - SSL (handler cert)    │  │  - SSL (agent cert)        │
                 │  │  - Cloud Armor (handler) │  │  - Cloud Armor (agent)     │
                 │  │  - Default backend only  │  │  - Default backend only    │
                 │  └────────────┬─────────────┘  └────────────┬───────────────┘
                 │               │                             │
                 ▼               ▼                             ▼
      ┌──────────────────────────────────┐  ┌──────────────────────────────────┐
      │  Marketplace Handler (Port 8001) │  │  Lightspeed Agent (Port 8000)    │
      │  ──────────────────────────────  │  │  ──────────────────────────────  │
      │  - Always running (minScale=1)   │  │  ┌──────────────┐ ┌───────────┐ │
      │  - Pub/Sub events (internal)     │  │  │ Lightspeed   │ │ MCP       │ │
      │  - DCR requests (via LB)         │  │  │ Agent        │ │ Server    │ │
      │  - Entitlement approval          │  │  │ LLM (config.)│◄►│ (8081)    │ │
      │                                  │  │  │ A2A + OAuth  │ │ Advisor   │ │
      └───────┬──────────────────┬───────┘  │  └──────────────┘ └─────┬─────┘ │
              │                  │          └──────────────────────────┼───────┘
     Shared DB│                  │ DCR                                 │
              ▼                  ▼                                     ▼
      ┌────────────┐     ┌──────────────┐                 ┌──────────────────┐
      │ PostgreSQL │     │  Red Hat SSO  │                 │console.redhat.com│
      └────────────┘     │ (GMA SSO API)│                 │ (Insights APIs)  │
                         └──────────────┘                 └──────────────────┘
```

Each service can have its own independent load balancer. When a service's LB is enabled (`ENABLE_LB_AGENT=true` or `ENABLE_LB_HANDLER=true`), Cloud Run ingress for that service is restricted to `internal-and-cloud-load-balancing`, meaning external traffic **must** go through its GCLB. Without a LB, external traffic reaches the Cloud Run service directly via its Cloud Run URL. Pub/Sub traffic is always internal Google Cloud traffic and reaches the handler directly, bypassing any load balancer.

### Service Responsibilities

| Service | Port | Purpose | Scaling |
|---------|------|---------|---------|
| **Marketplace Handler** | 8001 | Pub/Sub events, DCR | Always on (minScale=1) |
| **Lightspeed Agent** | 8000 | A2A queries, user interactions | Always on (minScale=1) |

### Deployment Order

1. **Set up Cloud Memorystore Redis and VPC connector** - Required for agent rate limiting (see [Redis Setup](#redis-setup-for-rate-limiting))
2. **Deploy Marketplace Handler first** - Must be running to receive provisioning events
3. **Deploy Agent after provisioning** - Can be deployed when customers are ready to use the agent

The MCP server runs as a sidecar in the Agent service. The agent forwards the caller's JWT token to the MCP server, which uses it to authenticate with console.redhat.com on behalf of the user (see [MCP Authentication](#mcp-authentication)).

## Service Accounts

The deployment uses **two separate service accounts** following the principle of least privilege:

| Service Account | Name | Purpose | Permissions |
|-----------------|------|---------|-------------|
| **Runtime SA** | `lightspeed-agent` | Cloud Run service identity for both services | Secret Manager access, Vertex AI, Pub/Sub, Cloud SQL, Service Usage, logging, monitoring |
| **Pub/Sub Invoker SA** | `pubsub-invoker` | Authenticates Pub/Sub push subscriptions to invoke Cloud Run | `roles/run.invoker` on marketplace-handler service only |

**Why two service accounts?**

- The **Runtime SA** runs as the identity of both Cloud Run services and needs access to secrets, AI models, databases, etc. It does **not** need `roles/run.invoker`.
- The **Pub/Sub Invoker SA** is used exclusively by the Pub/Sub push subscription to authenticate when delivering marketplace events to the handler. It only has `roles/run.invoker` on the marketplace-handler service (not project-wide).
- This separation ensures that if one SA is compromised, the blast radius is limited.

Both are created automatically by `setup.sh`. The Pub/Sub Invoker SA is only created when `ENABLE_MARKETPLACE=true` (the default).

## Load Balancer (Optional)

The deployment scripts can create **independent per-service Google Cloud Load Balancers (GCLB)** — one for the agent and one for the marketplace handler. Each LB is fully self-contained with its own static IP, SSL certificate, Cloud Armor policy, and domain. This is optional — without LBs, services are accessed directly via their Cloud Run URLs.

### What GCLB Provides

- **SSL termination** with Google-managed certificates for your custom domains
- **DDoS protection** via Cloud Armor
- **WAF capabilities** (Web Application Firewall)
- **Per-service isolation** — each service has its own independent LB, blast radius is contained
- **Independent WAF policies** — tailor Cloud Armor rules per service (e.g., stricter rules for the agent)

### Per-Service Architecture

Each service that has an LB enabled gets its own independent set of resources — there is no shared state between the agent and handler LBs. Each LB has a simple default backend (no path-based routing needed since each fronts a single service).

Pub/Sub events are internal Google Cloud traffic and reach the marketplace handler directly, bypassing the load balancer.

### Ingress Restriction

`deploy.sh` manages Cloud Run ingress for each service based on its LB configuration:

- **LB enabled** → ingress is set to `internal-and-cloud-load-balancing`. External traffic **must** go through the service's GCLB (direct Cloud Run URLs are blocked from the internet).
- **LB not enabled** → ingress is set to `all`. External traffic reaches the service directly via its Cloud Run URL.

In both cases:

- Internal Google Cloud traffic (e.g., Pub/Sub to handler) always reaches services directly
- Health checks from the load balancer are allowed
- Each service's ingress is managed independently — enabling the agent LB does not affect the handler's ingress, and vice versa

> **Note:** The YAML configs (`service.yaml`, `marketplace-handler.yaml`) default to `internal-and-cloud-load-balancing`. `deploy.sh` overrides this to `all` for any service without an LB. If you deploy using `gcloud run services replace` directly (bypassing `deploy.sh`), set `run.googleapis.com/ingress: all` in the YAML manually when not using a GCLB.

### Resources Created

Each enabled LB creates the following resources (all prefixed with `LB_NAME` and the service label, default prefix: `lightspeed-lb`):

**Agent LB resources** (when `ENABLE_LB_AGENT=true`):

| Resource | Name | Description |
|----------|------|-------------|
| Global static IP | `{LB_NAME}-agent-ip` | External IP address for agent DNS |
| Serverless NEG | `{LB_NAME}-agent-neg` | Network endpoint group for agent service |
| Backend service | `{LB_NAME}-agent-backend` | Backend for agent NEG |
| URL map | `{LB_NAME}-agent-url-map` | Default backend (agent) |
| SSL certificate | `{LB_NAME}-agent-cert` | Google-managed SSL certificate for agent domain |
| HTTPS target proxy | `{LB_NAME}-agent-https-proxy` | Terminates SSL and forwards to URL map |
| Global forwarding rule | `{LB_NAME}-agent-forwarding-rule` | Maps static IP:443 to HTTPS proxy |

**Handler LB resources** (when `ENABLE_LB_HANDLER=true`):

| Resource | Name | Description |
|----------|------|-------------|
| Global static IP | `{LB_NAME}-handler-ip` | External IP address for handler DNS |
| Serverless NEG | `{LB_NAME}-handler-neg` | Network endpoint group for handler service |
| Backend service | `{LB_NAME}-handler-backend` | Backend for handler NEG |
| URL map | `{LB_NAME}-handler-url-map` | Default backend (handler) |
| SSL certificate | `{LB_NAME}-handler-cert` | Google-managed SSL certificate for handler domain |
| HTTPS target proxy | `{LB_NAME}-handler-https-proxy` | Terminates SSL and forwards to URL map |
| Global forwarding rule | `{LB_NAME}-handler-forwarding-rule` | Maps static IP:443 to HTTPS proxy |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_LB_AGENT` | `false` | Enable GCLB for the agent service |
| `AGENT_DOMAIN_NAME` | (required when agent LB enabled) | Domain for the agent's Google-managed SSL certificate (e.g., `agent.example.com`) |
| `ENABLE_LB_HANDLER` | `false` | Enable GCLB for the marketplace handler service |
| `HANDLER_DOMAIN_NAME` | (required when handler LB enabled) | Domain for the handler's Google-managed SSL certificate (e.g., `dcr.example.com`) |
| `ENABLE_CLOUD_ARMOR_AGENT` | `false` | Enable Cloud Armor WAF for the agent LB (requires `ENABLE_LB_AGENT=true`) |
| `ENABLE_CLOUD_ARMOR_HANDLER` | `false` | Enable Cloud Armor WAF for the handler LB (requires `ENABLE_LB_HANDLER=true`) |
| `CLOUD_ARMOR_SENSITIVITY_AGENT` | `1` | OWASP CRS sensitivity level for the agent WAF policy (1-4). Level 1 is recommended for the agent because its A2A payloads contain free-form user text that can trigger false positives at higher sensitivities. |
| `CLOUD_ARMOR_SENSITIVITY_HANDLER` | `2` | OWASP CRS sensitivity level for the handler WAF policy (1-4). Level 2 is suitable because the handler only receives structured DCR JSON from Gemini Enterprise. |
| `LB_NAME` | `lightspeed-lb` | Prefix for all load balancer resource names |

### DNS Setup

After `setup.sh` reserves the static IPs, create DNS A records for each enabled LB:

1. Get the static IP addresses:
   ```bash
   # Agent LB IP (if ENABLE_LB_AGENT=true)
   gcloud compute addresses describe ${LB_NAME:-lightspeed-lb}-agent-ip \
     --global \
     --project=$GOOGLE_CLOUD_PROJECT \
     --format='value(address)'

   # Handler LB IP (if ENABLE_LB_HANDLER=true)
   gcloud compute addresses describe ${LB_NAME:-lightspeed-lb}-handler-ip \
     --global \
     --project=$GOOGLE_CLOUD_PROJECT \
     --format='value(address)'
   ```

2. Create A records in your DNS provider:
   ```
   agent.example.com.  A  <agent-static-ip>
   dcr.example.com.    A  <handler-static-ip>
   ```

3. Verify DNS propagation:
   ```bash
   dig +short $AGENT_DOMAIN_NAME
   dig +short $HANDLER_DOMAIN_NAME
   ```

### SSL Certificate Provisioning

Google-managed SSL certificates require each domain to resolve to its respective static IP before provisioning begins. Certificate provisioning typically takes **15 to 60 minutes** after DNS is correctly configured.

Check certificate status:

```bash
# Agent certificate
gcloud compute ssl-certificates describe ${LB_NAME:-lightspeed-lb}-agent-cert \
  --global \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(managed.status)'

# Handler certificate
gcloud compute ssl-certificates describe ${LB_NAME:-lightspeed-lb}-handler-cert \
  --global \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(managed.status)'
```

Each certificate goes through these states: `PROVISIONING` → `ACTIVE`. HTTPS traffic will not work for a service until its certificate reaches `ACTIVE` status.

### Cloud Armor (WAF)

Each service can have its own independent Cloud Armor security policy. Set `ENABLE_CLOUD_ARMOR_AGENT=true` (requires `ENABLE_LB_AGENT=true`) and/or `ENABLE_CLOUD_ARMOR_HANDLER=true` (requires `ENABLE_LB_HANDLER=true`) to create **Google Cloud Armor** security policies. Cloud Armor provides:

- **Web Application Firewall (WAF)** with preconfigured OWASP ModSecurity Core Rule Set (CRS) rules
- **DDoS mitigation** at the edge via Google's global infrastructure
- **Layer 7 filtering** to block common web attacks before they reach your services
- **Independent policies per service** — tailor WAF rules for each service's traffic patterns

#### Why Enable WAF

Without a GCLB in front of a Cloud Run service, there is no WAF layer — all HTTP traffic reaches your application directly. Cloud Armor WAF is only available through GCLB, so enabling a per-service load balancer is the only way to get WAF protection on Cloud Run.

WAF enforcement applies the [OWASP ModSecurity Core Rule Set (CRS)](https://owasp.org/www-project-modsecurity-core-rule-set/) at the edge, blocking malicious requests **before they reach your application**. This provides defense-in-depth: even if the application has an undiscovered vulnerability, the WAF catches common exploit patterns at the network edge.

Per-service policies allow independent tuning for each service's traffic profile. For example, the agent processes free-form A2A JSON-RPC payloads where users may ask about SQL queries or include HTML-like text — aggressive SQL injection rules could cause false positives. The marketplace handler receives structured DCR JSON from Gemini Enterprise — stricter rules are appropriate. Independent policies let you tune each service without compromise.

#### Enabling Cloud Armor

```bash
# Enable LBs with Cloud Armor for both services
export ENABLE_LB_AGENT=true
export AGENT_DOMAIN_NAME="agent.example.com"
export ENABLE_CLOUD_ARMOR_AGENT=true

export ENABLE_LB_HANDLER=true
export HANDLER_DOMAIN_NAME="dcr.example.com"
export ENABLE_CLOUD_ARMOR_HANDLER=true

./deploy/cloudrun/deploy.sh
```

#### Preconfigured WAF Rules

Each security policy includes the following OWASP ModSecurity CRS rules, each configured to deny matching requests with HTTP 403. The rules map to [OWASP Top 10 (2021)](https://owasp.org/Top10/) categories:

| Priority | Rule | OWASP Category | What It Blocks |
|----------|------|----------------|----------------|
| 900 | `methodenforcement-v422-stable` | A05:2021 Security Misconfiguration | Blocks unexpected HTTP methods (DELETE, PUT, TRACE, OPTIONS, etc.). Both services only use POST (JSON-RPC, DCR) and GET (health, agent card). |
| 1000 | `sqli-v422-stable` | A03:2021 Injection | SQL injection attempts in query parameters, headers, and request body. Detects patterns like `' OR 1=1`, `UNION SELECT`, and encoded variants. |
| 1100 | `xss-v422-stable` | A03:2021 Injection | Cross-site scripting via `<script>` tags, event handlers (`onerror`, `onload`), and JavaScript URIs in request parameters and headers. |
| 1200 | `lfi-v422-stable` | A01:2021 Broken Access Control | Local file inclusion via path traversal (`../../etc/passwd`, `..%2f..%2f`). Prevents attackers from reading files on the server filesystem. |
| 1300 | `rfi-v422-stable` | A01:2021 Broken Access Control | Remote file inclusion via URL parameters pointing to external resources (`http://evil.com/shell.php`). Blocks attempts to load and execute remote code. |
| 1400 | `rce-v422-stable` | A03:2021 Injection | OS command injection attempts (`; rm -rf /`, backtick execution, pipe chains). Prevents arbitrary command execution on the server. |
| 1500 | `scannerdetection-v422-stable` | Reconnaissance | Detects and blocks automated vulnerability scanners, web crawlers, and attack tools by matching known scanner signatures in User-Agent headers and request patterns. |
| 1600 | `protocolattack-v422-stable` | A05:2021 Security Misconfiguration | HTTP request smuggling, response splitting (`\r\n` injection), and protocol-level attacks that exploit differences between proxy and backend HTTP parsing. |
| 1700 | `sessionfixation-v422-stable` | A07:2021 Identification and Authentication Failures | Session fixation attacks where an attacker sets a known session ID via URL parameters or cookies, then waits for the victim to authenticate with that session. |
| 1800 | `cve-canary` | Emerging Threats | Rapidly-updated signatures for critical CVEs (e.g., Log4Shell, Spring4Shell). Google updates these rules as new CVEs are disclosed, providing early protection before application patches are deployed. |

All rules use `evaluatePreconfiguredWaf()` with configurable sensitivity levels (1-4). The agent defaults to sensitivity 1 (`CLOUD_ARMOR_SENSITIVITY_AGENT`) because its A2A payloads contain free-form user text that can trigger false positives at higher sensitivities. The handler defaults to sensitivity 2 (`CLOUD_ARMOR_SENSITIVITY_HANDLER`) because it only receives structured DCR JSON from Gemini Enterprise. JSON parsing is enabled on each security policy (`--json-parsing=STANDARD`) for accurate inspection of JSON-RPC and DCR request bodies. Request body inspection is set to 64kB (`--request-body-inspection-size=64kB`), the maximum, to cover large A2A conversation payloads. Verbose logging is enabled (`--log-level=VERBOSE`) for detailed visibility into WAF decisions, useful for debugging false positives and tuning rules.

#### Preview Mode (Recommended for Initial Deployment)

Cloud Armor rules are deployed in **enforce mode** (`deny-403`) by default — matching requests are blocked immediately. [Google recommends](https://docs.cloud.google.com/armor/docs/best-practices) deploying WAF rules in **preview mode** first to observe what they would block without actually blocking traffic, then switching to enforce mode after tuning.

This is especially important for the **agent service**, where free-form A2A JSON-RPC payloads may contain SQL-like text, HTML fragments, or code snippets that trigger false positives on SQLi (`sqli`) or XSS (`xss`) rules. False positives from Cloud Armor are silent from the application's perspective — blocked requests never reach the app and produce no application-level logs, only Cloud Armor logs in Cloud Logging.

To switch individual rules to preview mode after deployment:

```bash
# Put the SQLi rule into preview mode on the agent policy (log only, do not block)
gcloud compute security-policies rules update 1000 \
  --security-policy="${LB_NAME:-lightspeed-lb}-agent-security-policy" \
  --action=deny-403 \
  --preview \
  --global --project=$GOOGLE_CLOUD_PROJECT
```

To re-enable enforcement after tuning:

```bash
gcloud compute security-policies rules update 1000 \
  --security-policy="${LB_NAME:-lightspeed-lb}-agent-security-policy" \
  --action=deny-403 \
  --no-preview \
  --global --project=$GOOGLE_CLOUD_PROJECT
```

**Recommended rollout procedure:**

1. Deploy with rules in enforce mode (the default)
2. Immediately switch agent SQLi and XSS rules (priorities 1000, 1100) to preview mode
3. Monitor Cloud Armor logs for false positives (see [Viewing Blocked Requests](#viewing-blocked-requests))
4. After 24–48 hours with no false positives, re-enable enforcement with `--no-preview`
5. Repeat for any other rules that show false positive activity

#### Checking Policy Status

```bash
# Agent security policy
gcloud compute security-policies describe ${LB_NAME:-lightspeed-lb}-agent-security-policy \
  --global --project=$GOOGLE_CLOUD_PROJECT

# Handler security policy
gcloud compute security-policies describe ${LB_NAME:-lightspeed-lb}-handler-security-policy \
  --global --project=$GOOGLE_CLOUD_PROJECT
```

#### Viewing Blocked Requests

Cloud Armor logs blocked requests to Cloud Logging. To view recent blocks:

```bash
gcloud logging read 'resource.type="http_load_balancer" AND jsonPayload.enforcedSecurityPolicy.outcome="DENY"' \
  --project=$GOOGLE_CLOUD_PROJECT --limit=25 \
  --format='table(timestamp, jsonPayload.enforcedSecurityPolicy.name, jsonPayload.enforcedSecurityPolicy.matchedRule, httpRequest.requestUrl)'
```

#### Customizing Rules

You can add, remove, or modify rules after deployment. Specify the per-service policy name.

> **Note:** Priorities 900–1800 are used by the preconfigured WAF rules. Custom rules should use priorities below 900 or above 1800.

```bash
# Remove a rule from the agent policy (e.g., scanner detection)
gcloud compute security-policies rules delete 1500 \
  --security-policy="${LB_NAME:-lightspeed-lb}-agent-security-policy" \
  --global --project=$GOOGLE_CLOUD_PROJECT

# Switch a rule to preview mode on the handler policy (log only, do not block)
gcloud compute security-policies rules update 1000 \
  --security-policy="${LB_NAME:-lightspeed-lb}-handler-security-policy" \
  --action=deny-403 \
  --preview \
  --global --project=$GOOGLE_CLOUD_PROJECT

# Add a custom IP deny rule to the agent policy
gcloud compute security-policies rules create 800 \
  --security-policy="${LB_NAME:-lightspeed-lb}-agent-security-policy" \
  --src-ip-ranges="203.0.113.0/24" \
  --action=deny-403 \
  --global --project=$GOOGLE_CLOUD_PROJECT
```

#### Field-Level Exclusions (Reducing False Positives)

The agent service processes free-form A2A JSON-RPC payloads where users may ask about SQL queries, paste HTML/code, or discuss file paths. These can trigger SQLi, XSS, or LFI rules as false positives. Use [field-level exclusions](https://docs.cloud.google.com/armor/docs/rule-tuning) to exclude specific request fields from WAF inspection while keeping protection for other fields.

Exclusions are configured per rule per policy. To identify which fields trigger false positives, enable verbose logging (already configured) and check Cloud Armor logs:

```bash
# View recent blocks with request details
gcloud logging read 'resource.type="http_load_balancer" AND jsonPayload.enforcedSecurityPolicy.outcome="DENY"' \
  --project=$GOOGLE_CLOUD_PROJECT --limit=10 \
  --format='json(timestamp, jsonPayload.enforcedSecurityPolicy.matchedRule, jsonPayload.enforcedSecurityPolicy.matchedFieldType, jsonPayload.enforcedSecurityPolicy.matchedFieldValue)'

# Exclude a specific request header from SQLi inspection on the agent policy
gcloud compute security-policies rules update 1000 \
  --security-policy="${LB_NAME:-lightspeed-lb}-agent-security-policy" \
  --request-header-to-exclude="Authorization" \
  --global --project=$GOOGLE_CLOUD_PROJECT
```

> **Tip:** Start with verbose logging and preview mode on the agent's SQLi and XSS rules (priorities 1000, 1100) to identify false positives before adding exclusions. See [Preview Mode](#preview-mode-recommended-for-initial-deployment).

#### Pricing

Cloud Armor **Standard** tier is included with GCLB at no extra cost for preconfigured WAF rules and basic security policies. **Cloud Armor Enterprise** (formerly Managed Protection Plus) provides additional features such as adaptive protection, named IP lists, and threat intelligence — see [Cloud Armor pricing](https://cloud.google.com/armor/pricing) for details.

## Prerequisites

- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) installed and authenticated
- GCP project with billing enabled
- Required permissions:
  - Cloud Run Admin
  - Cloud Build Editor
  - Secret Manager Admin
  - Service Account Admin

## Quick Start

### 1. Set Environment Variables

```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
export GOOGLE_CLOUD_LOCATION="us-central1"  # Cloud Run deployment region
export SERVICE_NAME="lightspeed-agent"

# Vertex AI model location (use "global" for pay-as-you-go access).
# This is separate from GOOGLE_CLOUD_LOCATION, which sets the Cloud Run
# deployment region. Defaults to "global" if not set.
# export VERTEXAI_LOCATION="global"

# Optional: use a different name for the GCP service account
# export SERVICE_ACCOUNT_NAME="my-custom-sa"

# Optional: disable Pub/Sub marketplace integration
export ENABLE_MARKETPLACE="false"

# Optional: enable per-service Google Cloud Load Balancers (GCLB)
# export ENABLE_LB_AGENT="true"
# export AGENT_DOMAIN_NAME="agent.example.com"    # Required when agent LB is enabled
# export ENABLE_LB_HANDLER="true"
# export HANDLER_DOMAIN_NAME="dcr.example.com"    # Required when handler LB is enabled
# export LB_NAME="lightspeed-lb"                  # Default prefix for LB resources

# Optional: enable Cloud Armor WAF per service (requires respective LB)
# export ENABLE_CLOUD_ARMOR_AGENT="true"
# export ENABLE_CLOUD_ARMOR_HANDLER="true"
```

> **Note:** `deploy.sh` manages Cloud Run ingress automatically — services with an LB get restricted ingress (`internal-and-cloud-load-balancing`), services without an LB get open ingress (`all`). No manual YAML edits are needed.

**Google Cloud Marketplace deployments:** If you are deploying with marketplace
integration (`ENABLE_MARKETPLACE=true`, the default), you **must** set the
following variables **before** running `setup.sh` or `deploy.sh`:

```bash
# Required: managed service name from the Producer Portal (the product-level
# identifier). Used by the handler to approve entitlements via the Procurement
# API and to filter Pub/Sub events by product. You can find it under
# APIs & Services > Endpoints, or by running:
#   gcloud endpoints services list --project=$GOOGLE_CLOUD_PROJECT
export SERVICE_CONTROL_SERVICE_NAME="<service-name>.endpoints.<project-id>.cloud.goog"

# Required: fully-qualified Pub/Sub topic provided by Google Cloud Marketplace
export PUBSUB_TOPIC="projects/<marketplace-project>/topics/<your-marketplace-topic>"

# Required when using a fully-qualified topic: the subscription name is derived
# from the topic by default (appending "-sub"), which produces an invalid name
# when the topic is a fully-qualified path.
export PUBSUB_SUBSCRIPTION="marketplace-events-sub"
```

If `SERVICE_CONTROL_SERVICE_NAME` is not set, the handler will skip
entitlement approval and subscriptions will remain pending in the Google
Cloud console.

If `PUBSUB_TOPIC` is not set, the scripts default to creating a local topic
named `marketplace-entitlements`, which does **not** receive events from the
marketplace. Orders will remain stuck in `pending` status because
entitlement approval events never reach the handler.

### 2. Run Setup Script

The setup script enables required APIs, creates service accounts (runtime + Pub/Sub invoker), and sets up secrets:

```bash
./deploy/cloudrun/setup.sh
```

**Environment variables:**
| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CLOUD_PROJECT` | (required) | GCP project ID |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Cloud Run deployment region |
| `VERTEXAI_LOCATION` | `global` | Vertex AI model location (use `global` for pay-as-you-go) |
| `SERVICE_NAME` | `lightspeed-agent` | Cloud Run service name |
| `SERVICE_ACCOUNT_NAME` | `${SERVICE_NAME}` | GCP service account name (allows a different name than the Cloud Run service) |
| `HANDLER_SERVICE_NAME` | `marketplace-handler` | Marketplace handler Cloud Run service name |
| `DB_INSTANCE_NAME` | `lightspeed-agent-db` | Cloud SQL instance name |
| `VPC_CONNECTOR_NAME` | `lightspeed-redis-conn` | Serverless VPC Access connector for Redis |
| `PUBSUB_INVOKER_NAME` | `pubsub-invoker` | Pub/Sub invoker SA name |
| `PUBSUB_TOPIC` | `marketplace-entitlements` | Pub/Sub topic for marketplace events. **Must** be set to the fully-qualified topic from Google Cloud Marketplace for production deployments. See [Set Environment Variables](#1-set-environment-variables). |
| `PUBSUB_SUBSCRIPTION` | `${PUBSUB_TOPIC}-sub` | Pub/Sub subscription name. **Must** be set explicitly when `PUBSUB_TOPIC` is a fully-qualified path, since the default derivation produces an invalid name. |
| `SERVICE_CONTROL_SERVICE_NAME` | - | Managed service name from the Producer Portal. **Required** for marketplace deployments — used for entitlement approval and product-level event filtering. |
| `ENABLE_MARKETPLACE` | `true` | Create Pub/Sub invoker SA and topic for marketplace integration |
| `ENABLE_LB_AGENT` | `false` | Enable GCLB for the agent service (see [Load Balancer](#load-balancer-optional)) |
| `AGENT_DOMAIN_NAME` | - | Domain for the agent SSL certificate. **Required** when `ENABLE_LB_AGENT=true`. |
| `ENABLE_LB_HANDLER` | `false` | Enable GCLB for the marketplace handler service |
| `HANDLER_DOMAIN_NAME` | - | Domain for the handler SSL certificate. **Required** when `ENABLE_LB_HANDLER=true`. |
| `ENABLE_CLOUD_ARMOR_AGENT` | `false` | Enable Cloud Armor WAF for agent LB (requires `ENABLE_LB_AGENT=true`). See [Cloud Armor (WAF)](#cloud-armor-waf). |
| `ENABLE_CLOUD_ARMOR_HANDLER` | `false` | Enable Cloud Armor WAF for handler LB (requires `ENABLE_LB_HANDLER=true`). |
| `LB_NAME` | `lightspeed-lb` | Prefix for all load balancer resource names |

### 3. Set Up Cloud SQL Database

Cloud Run requires PostgreSQL for production. Create a Cloud SQL instance with two databases:

```bash
# Create Cloud SQL instance (using smallest Enterprise tier)
gcloud sql instances create $DB_INSTANCE_NAME \
  --database-version=POSTGRES_16 \
  --edition=ENTERPRISE \
  --tier=db-g1-small \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --ssl-mode=ENCRYPTED_ONLY

# Generate random passwords for database users
MARKETPLACE_DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
SESSION_DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
echo "Marketplace DB password: $MARKETPLACE_DB_PASSWORD"
echo "Session DB password: $SESSION_DB_PASSWORD"
# Save these — you'll need them for the database-url secrets below

# Create marketplace database and user
gcloud sql databases create lightspeed_agent \
  --instance=$DB_INSTANCE_NAME \
  --project=$GOOGLE_CLOUD_PROJECT

gcloud sql users create insights \
  --instance=$DB_INSTANCE_NAME \
  --password=$MARKETPLACE_DB_PASSWORD \
  --project=$GOOGLE_CLOUD_PROJECT

# Create session database and user
gcloud sql databases create agent_sessions \
  --instance=$DB_INSTANCE_NAME \
  --project=$GOOGLE_CLOUD_PROJECT

gcloud sql users create sessions \
  --instance=$DB_INSTANCE_NAME \
  --password=$SESSION_DB_PASSWORD \
  --project=$GOOGLE_CLOUD_PROJECT

# Get the connection name for later use
CONNECTION_NAME=$(gcloud sql instances describe $DB_INSTANCE_NAME \
  --project=$GOOGLE_CLOUD_PROJECT --format='value(connectionName)')
echo "Connection name: $CONNECTION_NAME"
```

### 4. Redis Setup for Rate Limiting

The agent uses Redis for distributed rate limiting. On Cloud Run, use **Cloud Memorystore for Redis** with a **Serverless VPC Access connector** so the agent can reach the Redis instance.

**Step 1: Create a VPC connector** (if you don't have one):

```bash
# Create a Serverless VPC Access connector in the same region as Cloud Run
# Use the default network or your custom VPC. The subnet range must not overlap with existing subnets.
# Check available ranges: gcloud compute networks subnets list --network=default --filter="region:$GOOGLE_CLOUD_LOCATION"
gcloud compute networks vpc-access connectors create lightspeed-redis-conn \
  --region=$GOOGLE_CLOUD_LOCATION \
  --network=default \
  --range=10.8.0.0/28 \
  --project=$GOOGLE_CLOUD_PROJECT
```

**Step 2: Create a Redis instance** in the same VPC network with in-transit encryption (TLS):

```bash
# Create a Basic tier Redis instance with TLS enabled
gcloud redis instances create lightspeed-redis \
  --size=1 \
  --region=$GOOGLE_CLOUD_LOCATION \
  --redis-version=redis_7_0 \
  --network=default \
  --transit-encryption-mode=SERVER_AUTHENTICATION \
  --project=$GOOGLE_CLOUD_PROJECT

# Get the Redis host IP and port (TLS uses port 6378, not 6379)
REDIS_HOST=$(gcloud redis instances describe lightspeed-redis \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(host)')
REDIS_PORT=$(gcloud redis instances describe lightspeed-redis \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(port)')
echo "Redis host: $REDIS_HOST, port: $REDIS_PORT"
```

**Step 3: Download the Redis CA certificate and store it in Secret Manager**:

```bash
# Download the server CA certificate (required for TLS verification)
gcloud redis instances describe lightspeed-redis \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(serverCaCerts[0].cert)' > /tmp/redis-ca.pem

# Store the CA certificate in Secret Manager
gcloud secrets create redis-ca-cert \
  --data-file=/tmp/redis-ca.pem \
  --project=$GOOGLE_CLOUD_PROJECT

rm /tmp/redis-ca.pem
```

**Step 4: Store the Redis URL in Secret Manager** (using `rediss://` scheme for TLS):

```bash
# Note: "rediss://" (double s) enables TLS on the connection.
# TLS-enabled instances use port 6378 (not the default 6379).
echo -n "rediss://${REDIS_HOST}:${REDIS_PORT}/0" | \
  gcloud secrets versions add rate-limit-redis-url --data-file=- --project=$GOOGLE_CLOUD_PROJECT
```

**Step 5: Set the VPC connector name** (if different from default):

```bash
# Default is lightspeed-redis-conn; override if you used a different name
export VPC_CONNECTOR_NAME="lightspeed-redis-conn"
```

See [Connect to Redis from Cloud Run](https://cloud.google.com/run/docs/integrate/redis-memorystore) for more details.

#### Migrating an Existing Redis Instance to TLS

Cloud Memorystore does not support enabling in-transit encryption on an existing instance — the `--transit-encryption-mode` flag is immutable after creation. To enable TLS you must create a new instance and cut over. The Redis data is entirely ephemeral (rate limiting sliding window counters), so there is nothing to migrate.

**1. Create a new Redis instance with TLS:**

```bash
gcloud redis instances create lightspeed-redis-tls \
  --size=1 \
  --region=$GOOGLE_CLOUD_LOCATION \
  --redis-version=redis_7_0 \
  --network=default \
  --transit-encryption-mode=SERVER_AUTHENTICATION \
  --project=$GOOGLE_CLOUD_PROJECT

REDIS_HOST=$(gcloud redis instances describe lightspeed-redis-tls \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(host)')
REDIS_PORT=$(gcloud redis instances describe lightspeed-redis-tls \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(port)')
```

**2. Download the CA certificate and store it in Secret Manager:**

```bash
gcloud redis instances describe lightspeed-redis-tls \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(serverCaCerts[0].cert)' > /tmp/redis-ca.pem

gcloud secrets create redis-ca-cert \
  --data-file=/tmp/redis-ca.pem \
  --project=$GOOGLE_CLOUD_PROJECT

rm /tmp/redis-ca.pem
```

**3. Update the Redis URL secret to use `rediss://`** (TLS uses port 6378):

```bash
echo -n "rediss://${REDIS_HOST}:${REDIS_PORT}/0" | \
  gcloud secrets versions add rate-limit-redis-url \
    --data-file=- --project=$GOOGLE_CLOUD_PROJECT
```

**4. Redeploy the agent service** (picks up the new secret version, CA cert volume mount, and `RATE_LIMIT_REDIS_CA_CERT` env var from the updated `service.yaml`):

```bash
./deploy/cloudrun/deploy.sh --service agent
```

**5. Verify the agent is healthy:**

```bash
curl $(gcloud run services describe ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')/health
```

**6. Delete the old Redis instance:**

```bash
gcloud redis instances delete lightspeed-redis \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT
```

**Notes:**

- No downtime: Cloud Run rolls out the new revision alongside the old one. The old revision keeps using the previous `redis://` URL (pinned at deploy time) until it drains.
- Rate limiting counters reset after the cutover (all sliding windows start fresh). This is harmless — users simply get a full quota again.

### 5. Configure Load Balancer (Optional)

If you want SSL termination, DDoS protection, and independent WAF policies per service, enable per-service Google Cloud Load Balancers. See [Load Balancer (Optional)](#load-balancer-optional) for details on what GCLB provides.

**Step 1: Enable the load balancers and set your domains:**

```bash
# Agent LB (A2A traffic)
export ENABLE_LB_AGENT=true
export AGENT_DOMAIN_NAME="agent.example.com"

# Handler LB (DCR traffic)
export ENABLE_LB_HANDLER=true
export HANDLER_DOMAIN_NAME="dcr.example.com"

# Optional: change the resource name prefix
# export LB_NAME="lightspeed-lb"
```

You can enable LBs for one or both services independently.

**Step 2: Run setup.sh** (if not already done — it creates the static IPs and SSL certificates for each enabled LB):

```bash
./deploy/cloudrun/setup.sh
```

**Step 3: Get the static IPs for DNS configuration:**

```bash
# Agent LB IP
gcloud compute addresses describe ${LB_NAME:-lightspeed-lb}-agent-ip \
  --global \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(address)'

# Handler LB IP
gcloud compute addresses describe ${LB_NAME:-lightspeed-lb}-handler-ip \
  --global \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(address)'
```

**Step 4: Configure your DNS A records** to point each domain to its respective static IP address before deploying. Google-managed SSL certificates require domains to resolve to their static IPs before provisioning (takes 15–60 minutes after DNS propagates).

### 6. Configure Secrets

Update the placeholder secrets with actual values:

```bash
# Red Hat SSO credentials
echo -n 'your-sso-client-id' | \
  gcloud secrets versions add redhat-sso-client-id --data-file=- --project=$GOOGLE_CLOUD_PROJECT

echo -n 'your-sso-client-secret' | \
  gcloud secrets versions add redhat-sso-client-secret --data-file=- --project=$GOOGLE_CLOUD_PROJECT

# DCR (Dynamic Client Registration) - Required for Gemini Enterprise integration
# GMA SSO API credentials for tenant creation
echo -n 'your-gma-client-id' | \
  gcloud secrets versions add gma-client-id --data-file=- --project=$GOOGLE_CLOUD_PROJECT
echo -n 'your-gma-client-secret' | \
  gcloud secrets versions add gma-client-secret --data-file=- --project=$GOOGLE_CLOUD_PROJECT

# Fernet encryption key for DCR client secrets
# Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
echo -n 'your-fernet-key' | \
  gcloud secrets versions add dcr-encryption-key --data-file=- --project=$GOOGLE_CLOUD_PROJECT

# Database URLs (use CONNECTION_NAME and passwords from step 3)
# Marketplace database: stores orders, entitlements, DCR clients
echo -n "postgresql+asyncpg://insights:$MARKETPLACE_DB_PASSWORD@/lightspeed_agent?host=/cloudsql/$CONNECTION_NAME" | \
  gcloud secrets versions add database-url --data-file=- --project=$GOOGLE_CLOUD_PROJECT

# Session database: stores agent sessions (required for persistence)
echo -n "postgresql+asyncpg://sessions:$SESSION_DB_PASSWORD@/agent_sessions?host=/cloudsql/$CONNECTION_NAME" | \
  gcloud secrets versions add session-database-url --data-file=- --project=$GOOGLE_CLOUD_PROJECT

# Rate limit Redis URL (required). As instructed in Redis Setup steps 3-4 after creating the Redis instance.
# TLS-enabled instances use port 6378 (not 6379). Read $REDIS_PORT from step 2.
# REDIS_HOST=$(gcloud redis instances describe lightspeed-redis --region=$GOOGLE_CLOUD_LOCATION --project=$GOOGLE_CLOUD_PROJECT --format='value(host)')
# REDIS_PORT=$(gcloud redis instances describe lightspeed-redis --region=$GOOGLE_CLOUD_LOCATION --project=$GOOGLE_CLOUD_PROJECT --format='value(port)')
# echo -n "rediss://${REDIS_HOST}:${REDIS_PORT}/0" | gcloud secrets versions add rate-limit-redis-url --data-file=- --project=$GOOGLE_CLOUD_PROJECT
# The CA certificate is stored separately (see Redis Setup step 3).
```

### 7. Copy MCP Image to GCR

Cloud Run doesn't support Quay.io directly. Copy the MCP server image to GCR.

**Authenticate to GCR first:**

```bash
# Authenticate your container runtime to gcr.io using gcloud
gcloud auth print-access-token | docker login -u oauth2accesstoken --password-stdin gcr.io
```

If you're using **Podman** instead of Docker:

```bash
gcloud auth print-access-token | podman login -u oauth2accesstoken --password-stdin gcr.io
```

Podman stores the resulting credentials in `${XDG_RUNTIME_DIR}/containers/auth.json` (typically `/run/user/$UID/containers/auth.json`). You can verify the login succeeded with:

```bash
cat ${XDG_RUNTIME_DIR}/containers/auth.json
```

**Pull, tag, and push:**

```bash
# Pull from Quay.io
docker pull quay.io/redhat-services-prod/insights-management-tenant/insights-mcp/red-hat-lightspeed-mcp:latest

# Tag and push to GCR
docker tag quay.io/redhat-services-prod/insights-management-tenant/insights-mcp/red-hat-lightspeed-mcp:latest \
  gcr.io/$GOOGLE_CLOUD_PROJECT/red-hat-lightspeed-mcp:latest
docker push gcr.io/$GOOGLE_CLOUD_PROJECT/red-hat-lightspeed-mcp:latest
```

### 8. Deploy

The agent's AgentCard advertises the DCR endpoints served by the
marketplace-handler service. Because of this, the **handler must be
deployed first** so its URL is known when the agent is configured.

**Step 1: Deploy the marketplace handler**

```bash
./deploy/cloudrun/deploy.sh --service handler --allow-unauthenticated
```

**Step 2: Get the handler URL and set `MARKETPLACE_HANDLER_URL`**

```bash
# Get the marketplace handler URL
HANDLER_URL=$(gcloud run services describe ${HANDLER_SERVICE_NAME:-marketplace-handler} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')
echo "Handler URL: $HANDLER_URL"

# Export it so deploy.sh can set it on the agent service
export MARKETPLACE_HANDLER_URL="$HANDLER_URL"
```

**Step 3: Deploy the agent**

The deploy script automatically sets `AGENT_PROVIDER_URL` (agent base URL)
and `MARKETPLACE_HANDLER_URL` on the agent service using the actual
Cloud Run URLs after deployment. `AGENT_PROVIDER_ORGANIZATION_URL`
(the provider's website, used as the JWT audience for DCR) is set in the
YAML configs and does not change per deployment.

```bash
./deploy/cloudrun/deploy.sh --service agent --allow-unauthenticated
```

After deployment, verify the AgentCard DCR endpoints point to the handler:

```bash
AGENT_URL=$(gcloud run services describe ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')
curl -s $AGENT_URL/.well-known/agent.json | jq '.capabilities.extensions'
```

**Load balancer:** When `ENABLE_LB_AGENT=true` and/or `ENABLE_LB_HANDLER=true`, `deploy.sh` automatically creates independent GCLB resources (NEG, backend service, URL map, HTTPS proxy, forwarding rule) for each enabled service and restricts its Cloud Run ingress to `internal-and-cloud-load-balancing`. When `ENABLE_CLOUD_ARMOR_AGENT=true` or `ENABLE_CLOUD_ARMOR_HANDLER=true`, the script also creates a per-service Cloud Armor security policy with preconfigured WAF rules. After deployment, check the SSL certificate status:

```bash
# Agent certificate
gcloud compute ssl-certificates describe ${LB_NAME:-lightspeed-lb}-agent-cert \
  --global \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(managed.status)'

# Handler certificate
gcloud compute ssl-certificates describe ${LB_NAME:-lightspeed-lb}-handler-cert \
  --global \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(managed.status)'
```

If the status is `PROVISIONING`, HTTPS traffic will not work yet — Google-managed certificates take 15–60 minutes to provision after DNS is correctly configured. HTTP traffic through the load balancer IP will work immediately.

**Other examples:**

```bash
# Deploy only the agent with a custom image
./deploy/cloudrun/deploy.sh --service agent --image gcr.io/my-project/lightspeed-agent:v1.0

# Deploy the handler with a custom image
./deploy/cloudrun/deploy.sh --service handler --handler-image gcr.io/my-project/marketplace-handler:v1.0
```

**Deploy script options:**

| Flag | Description |
|------|-------------|
| `--service <service>` | Which service to deploy: `all` (default), `handler`, `agent`, `lb` (LB-only, no service redeploy) |
| `--image <image>` | Container image for the agent (default: `gcr.io/$PROJECT_ID/lightspeed-agent:latest`) |
| `--handler-image <image>` | Container image for the marketplace handler (default: `gcr.io/$PROJECT_ID/marketplace-handler:latest`) |
| `--mcp-image <image>` | Container image for the MCP server (default: `gcr.io/$PROJECT_ID/red-hat-lightspeed-mcp:latest`) |
| `--build` | Build the image(s) before deploying |
| `--allow-unauthenticated` | Allow public access (required for A2A and Pub/Sub) |
| `--dry-run` | Preview gcloud commands without executing them |

**Service deployment:**

| Service | YAML Config | Description |
|---------|-------------|-------------|
| `handler` | `marketplace-handler.yaml` | Pub/Sub events, DCR requests |
| `agent` | `service.yaml` | A2A queries with MCP sidecar |
| `all` | Both | Deploy both services |
| `lb` | — | Set up load balancers only for enabled services (no service redeploy) |

The deploy script performs variable substitution on the YAML configs
(`${PROJECT_ID}`, `${REGION}`, image references, etc.) and deploys using
`gcloud run services replace`. For manual
deployment without the script, substitute all `${...}` variables in the YAML before running
`gcloud run services replace`:

```bash
sed -e "s|\${PROJECT_ID}|$GOOGLE_CLOUD_PROJECT|g" \
    -e "s|\${REGION}|$GOOGLE_CLOUD_LOCATION|g" \
    -e "s|\${VERTEXAI_LOCATION}|${VERTEXAI_LOCATION:-global}|g" \
    -e "s|\${DB_INSTANCE_NAME}|${DB_INSTANCE_NAME:-lightspeed-agent-db}|g" \
    -e "s|\${VPC_CONNECTOR_NAME}|${VPC_CONNECTOR_NAME:-lightspeed-redis-conn}|g" \
    -e "s|\${SERVICE_NAME}|${SERVICE_NAME:-lightspeed-agent}|g" \
    -e "s|\${SERVICE_ACCOUNT_NAME}|${SERVICE_ACCOUNT_NAME:-lightspeed-agent}|g" \
    -e "s|\${MCP_IMAGE}|${MCP_IMAGE:-gcr.io/$GOOGLE_CLOUD_PROJECT/insights-mcp:latest}|g" \
    deploy/cloudrun/service.yaml | \
    gcloud run services replace - --region=$GOOGLE_CLOUD_LOCATION --project=$GOOGLE_CLOUD_PROJECT
```

### Alternative: Deploy with Cloud Build

Instead of running `deploy.sh` manually (step 7), you can use Google Cloud Build
to pull pre-built images from Quay.io (built by Konflux), push them to GCR, and
deploy both services in a single command. Cloud Build also handles step 6 (Copy
MCP Image to GCR) automatically. By default, the pipeline creates per-service
Google Cloud Load Balancers (GCLB) with Cloud Armor WAF protection.

#### One-Command Deployment

The `deploy-cloudbuild.sh` wrapper runs the full deployment in a single command —
it calls `setup.sh` (idempotent), grants the Cloud Build service account IAM roles,
and submits the Cloud Build pipeline:

```bash
# Deploy with GCLB + Cloud Armor (setup + build + deploy in one command)
./deploy/cloudrun/deploy-cloudbuild.sh \
  --agent-domain agent.example.com --handler-domain dcr.example.com

# Deploy without GCLB
./deploy/cloudrun/deploy-cloudbuild.sh --no-lb

# Deploy with public access
./deploy/cloudrun/deploy-cloudbuild.sh --no-lb --allow-unauthenticated

# Preview the gcloud command without executing
./deploy/cloudrun/deploy-cloudbuild.sh --no-lb --dry-run
```

| Flag | Description |
|------|-------------|
| `--agent-domain DOMAIN` | Domain name for the agent GCLB (enables LB for agent) |
| `--handler-domain DOMAIN` | Domain name for the handler GCLB (enables LB for handler) |
| `--no-lb` | Disable GCLB for both services |
| `--allow-unauthenticated` | Allow public access to both services |
| `--region REGION` | GCP region (default: `us-central1`) |
| `--image-tag TAG` | Container image tag (default: `latest`) |
| `--dry-run` | Print the `gcloud builds submit` command without executing |

When deploying with GCLB enabled, you must configure DNS **before** running
the script. After the first run creates static IPs, create DNS A records pointing
your domain names to those IPs. Google-managed SSL certificates require DNS
resolution to provision (15-60 minutes).

#### Manual Cloud Build Submission

If you prefer to run `setup.sh` and grant IAM roles separately, you can submit
the Cloud Build pipeline directly:

```bash
# Grant Cloud Build SA the required roles (one-time)
PROJECT_NUMBER=$(gcloud projects describe $GOOGLE_CLOUD_PROJECT --format='value(projectNumber)')
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
  --member="serviceAccount:$CB_SA" --role="roles/run.admin"
gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
  --member="serviceAccount:$CB_SA" --role="roles/iam.serviceAccountUser"
gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
  --member="serviceAccount:$CB_SA" --role="roles/pubsub.editor"
gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
  --member="serviceAccount:$CB_SA" --role="roles/compute.admin"

# Note: roles/pubsub.editor and roles/compute.admin are broader than strictly
# needed. For production, consider custom roles scoped to the specific resources
# (see cloudbuild.yaml header comments for recommended permissions).

# Deploy with GCLB + Cloud Armor (requires domain names)
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_AGENT_DOMAIN_NAME=agent.example.com,_HANDLER_DOMAIN_NAME=dcr.example.com

# Deploy without GCLB (services accessible via Cloud Run URLs directly)
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_ENABLE_LB_AGENT=false,_ENABLE_LB_HANDLER=false

# Override region or image tag
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_REGION=europe-west1,_IMAGE_TAG=v1.0,_AGENT_DOMAIN_NAME=agent.example.com,_HANDLER_DOMAIN_NAME=dcr.example.com
```

**What the pipeline does:**

| Phase | Steps | Description |
|-------|-------|-------------|
| Validate | `validate-lb-config` | Validates GCLB configuration (domain names, flag consistency) |
| Pull | `pull-agent`, `pull-handler`, `pull-mcp` | Pulls pre-built images from Quay.io (parallel) |
| Scan | `scan-agent`, `scan-handler`, `scan-mcp` | Trivy vulnerability scanning — blocks push on CRITICAL/HIGH CVEs (parallel) |
| Push | `push-agent`, `push-handler`, `push-mcp` | Tags and pushes scanned images to GCR (parallel) |
| Deploy | `deploy-handler`, `deploy-agent` | Deploys handler first, then agent (using YAML configs with sed substitution, same as `deploy.sh`) |
| GCLB | `setup-lb-agent`, `setup-lb-handler`, `configure-ingress` | Creates per-service GCLB with Cloud Armor WAF (parallel), then configures ingress |
| Post-deploy | `allow-unauthenticated`, `configure-pubsub`, `update-agent-urls` | Configures IAM, Pub/Sub push subscription, and sets `AGENT_PROVIDER_URL`/`MARKETPLACE_HANDLER_URL` |

**Substitution variables:**

All variables have defaults matching `deploy.sh`. Override any with `--substitutions`:

| Variable | Default | Description |
|----------|---------|-------------|
| `_SERVICE_NAME` | `lightspeed-agent` | Cloud Run agent service name |
| `_HANDLER_SERVICE_NAME` | `marketplace-handler` | Cloud Run handler service name |
| `_REGION` | `us-central1` | GCP region |
| `_VERTEXAI_LOCATION` | `global` | Vertex AI location |
| `_IMAGE_TAG` | `latest` | Container image tag |
| `_SERVICE_ACCOUNT_NAME` | `lightspeed-agent` | GCP service account name |
| `_DB_INSTANCE_NAME` | `lightspeed-agent-db` | Cloud SQL instance name |
| `_VPC_CONNECTOR_NAME` | `lightspeed-redis-conn` | VPC connector for Redis |
| `_MCP_SOURCE_IMAGE` | `quay.io/.../red-hat-lightspeed-mcp:latest` | MCP image to mirror to GCR |
| `_ALLOW_UNAUTHENTICATED` | `false` | Allow public access to both services |
| `_PUBSUB_INVOKER_NAME` | `pubsub-invoker` | Pub/Sub invoker SA name |
| `_PUBSUB_TOPIC` | `marketplace-entitlements` | Pub/Sub topic for marketplace events |
| `_PUBSUB_SUBSCRIPTION` | *(derived from topic)* | Pub/Sub subscription name (defaults to `{topic}-sub`) |
| `_AGENT_SOURCE_IMAGE` | `quay.io/ecosystem-appeng/google-lightspeed-agent:latest` | Agent image to pull from Quay.io |
| `_HANDLER_SOURCE_IMAGE` | `quay.io/ecosystem-appeng/google-marketplace-handler:latest` | Handler image to pull from Quay.io |
| `_SERVICE_CONTROL_SERVICE_NAME` | *(empty)* | Google Cloud Service Control service name for usage metering |
| `_ENABLE_LB_AGENT` | `true` | Enable GCLB for the agent service |
| `_ENABLE_LB_HANDLER` | `true` | Enable GCLB for the marketplace handler service |
| `_ENABLE_CLOUD_ARMOR_AGENT` | `true` | Enable Cloud Armor WAF for the agent LB |
| `_ENABLE_CLOUD_ARMOR_HANDLER` | `true` | Enable Cloud Armor WAF for the handler LB |
| `_CLOUD_ARMOR_SENSITIVITY_AGENT` | `1` | OWASP CRS sensitivity level for agent WAF (1-4) |
| `_CLOUD_ARMOR_SENSITIVITY_HANDLER` | `2` | OWASP CRS sensitivity level for handler WAF (1-4) |
| `_AGENT_DOMAIN_NAME` | *(empty)* | Domain name for agent SSL certificate (required when LB enabled) |
| `_HANDLER_DOMAIN_NAME` | *(empty)* | Domain name for handler SSL certificate (required when LB enabled) |
| `_LB_NAME` | `lightspeed-lb` | Prefix for all GCLB resource names |

**Cloud Build vs deploy.sh:**

| | `deploy.sh` | `cloudbuild.yaml` |
|-|-------------|-------------------|
| Image source | Optional local build (`--build` flag) | Pulls pre-built images from Quay.io (built by Konflux) |
| MCP image copy | Manual (step 6) | Automatic |
| Deployment | Manual, one service at a time | Full pipeline, both services |
| Load balancer | Per-service GCLB via `--service lb` | Per-service GCLB (enabled by default) |
| Cloud Armor WAF | Per-service policies | Per-service policies (enabled by default with LB) |
| Pub/Sub setup | Automatic | Automatic |
| URL update | Automatic | Automatic |
| Use case | Manual/iterative deployment | CI/CD pipeline |

## Service Configuration

### Agent Container

| Setting | Value | Description |
|---------|-------|-------------|
| CPU | 2 | vCPUs allocated |
| Memory | 2Gi | Memory limit |
| Port | 8000 | Container port |

### Using a Different LLM

By default the agent uses **Gemini 2.5 Flash** via Vertex AI. You can switch to a different model on Vertex AI or point the agent at an external model endpoint (e.g., a model served on OpenShift).

#### Different model on Vertex AI

To use a different model available through Vertex AI (e.g., a partner model like GTP-OSS 120B), set `LLM_PROVIDER=litellm` and use the `vertex_ai/` model prefix:

```bash
gcloud run services update ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="\
LLM_PROVIDER=litellm,\
LLM_MODEL=vertex_ai/gtp-oss-120b"
```

The agent uses the existing `GOOGLE_CLOUD_PROJECT` and Vertex AI credentials — no additional API keys are needed. For Claude models on Vertex AI:

```bash
gcloud run services update ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="\
LLM_PROVIDER=litellm,\
LLM_MODEL=vertex_ai/claude-3-5-sonnet-v2@20241022"
```

To switch back to the default Gemini model:

```bash
gcloud run services update ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --remove-env-vars="LLM_PROVIDER,LLM_MODEL"
```

#### External model endpoint (e.g., OpenShift)

To use a model hosted outside Google Cloud — for example, a model served via vLLM or text-generation-inference on OpenShift AI — point the agent at the model's OpenAI-compatible endpoint:

**1. Store the API key in Secret Manager:**

```bash
echo -n 'your-model-api-key' | \
  gcloud secrets versions add llm-api-key --data-file=- --project=$GOOGLE_CLOUD_PROJECT

# Grant the runtime SA access to the new secret
gcloud secrets add-iam-policy-binding llm-api-key \
  --member="serviceAccount:${SERVICE_ACCOUNT_NAME:-lightspeed-agent}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$GOOGLE_CLOUD_PROJECT
```

**2. Add the secret to `service.yaml`:**

In the agent container's `env` section, add:

```yaml
- name: LLM_API_KEY
  valueFrom:
    secretKeyRef:
      key: latest
      name: llm-api-key
```

**3. Deploy with the external model settings:**

```bash
gcloud run services update ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="\
LLM_PROVIDER=litellm,\
LLM_MODEL=openai/your-model-name,\
LLM_API_BASE=https://your-model.apps.ocp.example.com/v1"
```

The `openai/` prefix tells LiteLLM to use the OpenAI-compatible chat completions protocol, which is the standard API exposed by vLLM, text-generation-inference, and most model serving frameworks.

> **Notes:**
> - The external endpoint must be reachable from Cloud Run. For private endpoints, configure a [VPC connector](https://cloud.google.com/run/docs/configuring/vpc-connectors) or [Private Google Access](https://cloud.google.com/run/docs/configuring/private-networking).
> - Gemini HTTP retry settings (`GEMINI_HTTP_RETRY_*`) do not apply to `litellm` providers. LiteLLM has its own retry logic.
> - MCP tools work with all model providers. Google's built-in ADK tools (e.g., `SearchTool`) only work with Gemini.
> - See [Configuration — LLM Provider](../../docs/configuration.md#llm-provider) for all available settings.

### Rate Limiting (Redis)

Both the agent and the marketplace handler use Cloud Memorystore for Redis for distributed rate limiting. The same Redis instance and configuration are shared by both services. Required configuration:

| Variable | Source | Description |
|----------|--------|-------------|
| `RATE_LIMIT_REDIS_URL` | Secret `rate-limit-redis-url` | Redis connection URL (e.g. `rediss://10.x.x.x:6378/0`). Use `rediss://` (double s) for TLS. Note: TLS instances use port 6378, not 6379. |
| `RATE_LIMIT_REDIS_CA_CERT` | Env (file path) | Path to the Redis server CA certificate for TLS verification (e.g. `/secrets/redis-ca-cert/latest`) |
| `RATE_LIMIT_REDIS_TIMEOUT_MS` | Env | Redis operation timeout (default: 200) |
| `RATE_LIMIT_KEY_PREFIX` | Env | Key prefix for rate limit keys |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | Env | Max requests per minute per principal |
| `RATE_LIMIT_REQUESTS_PER_HOUR` | Env | Max requests per hour per principal |

Both services use a VPC connector to reach the Redis instance. Set `VPC_CONNECTOR_NAME` (default: `lightspeed-redis-conn`) when deploying. In-transit encryption (TLS) is enabled on the Memorystore instance; the CA certificate is mounted from Secret Manager as a volume (see `service.yaml` and `marketplace-handler.yaml`). See [Rate Limiting — Testing](../../docs/rate-limiting.md#testing-rate-limiting) for how to validate rate limiting.

### MCP Output Size Guard

MCP tools can return very large responses (e.g., listing all advisories or inventory systems),
which inflate the LLM input context and may trigger Vertex AI token-per-minute (TPM) rate limits
(HTTP 429 `RESOURCE_EXHAUSTED`).

The agent includes an **MCP output size guard** that detects oversized tool results and replaces
them with an actionable message. Instead of sending the full payload to the LLM, the agent tells
the model the result was too large and asks it to guide the user toward narrowing down their query
or using pagination.

**Configuration:**

| Variable | Default | Description |
|----------|---------|-------------|
| `TOOL_RESULT_MAX_CHARS` | `51200` | Maximum character length for MCP tool results. Results exceeding this are replaced with guidance. Set to `0` to disable. |

**To adjust the limit on Cloud Run:**

```bash
# Allow larger results (e.g., 100K characters)
gcloud run services update lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --set-env-vars TOOL_RESULT_MAX_CHARS=100000

# Disable the guard entirely (not recommended — may cause 429 errors)
gcloud run services update lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --set-env-vars TOOL_RESULT_MAX_CHARS=0
```

**How it works:**

1. An MCP tool executes and returns a result
2. The `MCPOutputSizeGuardPlugin` serializes the result and checks its character length
3. If the result exceeds `TOOL_RESULT_MAX_CHARS`, it is replaced with:
   ```json
   {
     "error": "tool_result_too_large",
     "message": "The tool 'get_advisories' returned a result that is too large to process (270,000 characters, limit is 51,200). Please ask the user to narrow down their query or use pagination/filtering parameters to reduce the result size.",
     "original_size_chars": 270000,
     "limit_chars": 51200
   }
   ```
4. The LLM receives this message and can inform the user to refine their request

**Tuning tips:**

- **51,200 characters** (default, 50 KiB) is a conservative starting point meant to keep input
  tokens well within your Vertex AI TPM quota — verify against the quota for whichever model
  you configure via `GEMINI_MODEL`, since quotas and context windows vary by model
- If you have higher TPM quotas, increase the limit to allow richer responses
- The optimal limit depends on the model's context window and expected session length — longer
  multi-turn sessions accumulate more context, leaving less room for individual tool results.
  Short single-turn sessions can tolerate a higher limit.
- Monitor the `Tool result too large` warning logs to see which tools trigger the guard
  and how often

See [Configuration — MCP Output Size Guard](../../docs/configuration.md#mcp-output-size-guard) for more details.

### MCP Server Sidecar

| Setting | Value | Description |
|---------|-------|-------------|
| CPU | 1 | vCPUs allocated |
| Memory | 512Mi | Memory limit |
| Port | 8080 | Internal MCP port |
| Image | `gcr.io/$PROJECT_ID/red-hat-lightspeed-mcp:latest` | MCP server image (copied from Quay.io) |

### Copying the MCP Image to GCR

Cloud Run doesn't support pulling images directly from Quay.io. You must copy the MCP server image to Google Container Registry (GCR) before deploying:

```bash
# Pull from Quay.io locally
docker pull quay.io/redhat-services-prod/insights-management-tenant/insights-mcp/red-hat-lightspeed-mcp:latest

# Tag for GCR
docker tag quay.io/redhat-services-prod/insights-management-tenant/insights-mcp/red-hat-lightspeed-mcp:latest \
  gcr.io/$GOOGLE_CLOUD_PROJECT/red-hat-lightspeed-mcp:latest

# Push to GCR
docker push gcr.io/$GOOGLE_CLOUD_PROJECT/red-hat-lightspeed-mcp:latest
```

This step is required before running `deploy.sh`. The deploy script defaults to `gcr.io/$PROJECT_ID/red-hat-lightspeed-mcp:latest`.

**To update the MCP server**, repeat the above steps with a new tag or `:latest`.

**Costs (GCR):**
| Cost Type | Rate | Notes |
|-----------|------|-------|
| Storage | $0.026/GB/month | ~$0.005/month for a 200MB image |
| Network egress | Standard GCP rates | Free within same region |
| Requests | No charge | Pull requests are free |

### Customizing MCP Server Configuration

The MCP server configuration is hardcoded in `deploy/cloudrun/service.yaml` because Cloud Run does not support environment variable expansion in the `args` field (unlike Kubernetes/Podman).

**Current MCP server settings:**
```yaml
args:
  - "--readonly"      # Run in read-only mode
  - "--toolset"       # Restrict to specific toolsets
  - "advisor,inventory,vulnerability,planning,rhsm,content_sources,rbac"
  - "http"            # Use HTTP transport
  - "--port"
  - "8080"            # Listen on port 8080
  - "--host"
  - "0.0.0.0"         # Bind to all interfaces
```

The `--toolset` flag controls which MCP tool categories the server loads. Only the listed toolsets are available to the agent. This is enforced at the MCP server level, independently of the agent-side `MCP_READ_ONLY` tool filtering.

**Available toolsets:** `advisor`, `inventory`, `vulnerability`, `remediations`, `planning`, `image_builder`, `rhsm`, `content_sources`, `rbac`

**To change MCP server settings:**

1. Edit `deploy/cloudrun/service.yaml` directly:
   ```bash
   vim deploy/cloudrun/service.yaml
   # Find the "insights-mcp" container section
   # Modify the args array as needed
   ```

2. Common customizations:
   - **Change port**: Modify `"8080"` to your desired port (also update `MCP_SERVER_URL` in the agent container env)
   - **Enable write operations**: Remove `"--readonly"` flag (not recommended for production)
   - **Change transport**: Modify `"http"` to `"sse"` or `"stdio"` (requires corresponding agent changes)
   - **Change available toolsets**: Modify the comma-separated list after `"--toolset"`

3. Redeploy after making changes:
   ```bash
   ./deploy/cloudrun/deploy.sh --service agent
   ```

**Note**: If you change the MCP server port, you must also update the `MCP_SERVER_URL` environment variable in the agent container to match.

### Custom ADK AI Skills

The agent uses [ADK AI Skills](https://adk.dev/skills/) for modular behavioral instructions. Six bundled skills ship with the container image (tool invocation rules, multi-step workflows, pagination handling, error handling, guardrails/safety, response formatting). These always load at startup.

You can add **custom skills** to change or extend the agent's behavior without rebuilding the image by setting the `SKILLS_DIR` environment variable to a directory containing your skill definitions. External skills with the same name as a bundled skill override the bundled version.

#### Skill file format

Each skill lives in its own directory with a `SKILL.md` file:

```
my-custom-skill/
├── SKILL.md              # Required: YAML frontmatter + markdown instructions
└── references/           # Optional: additional docs loaded on-demand
    └── examples.md
```

The `SKILL.md` has YAML frontmatter followed by markdown instructions:

```markdown
---
name: my-custom-skill
description: |
  What this skill does and when the LLM should use it.
  Must be under 1024 characters.
metadata:
  author: my-team
  version: "1.0"
---

# Detailed instructions

Step-by-step instructions the agent follows when this skill is activated.
```

**Naming rules:** The directory name must match the `name` field in frontmatter exactly. Names must be lowercase kebab-case (a-z, 0-9, hyphens), max 64 characters.

#### Deploying custom skills on Cloud Run

**Option A: Build a custom image** with skills baked in:

```dockerfile
FROM gcr.io/$PROJECT_ID/lightspeed-agent:latest
COPY my-skills/ /opt/agent-skills/
ENV SKILLS_DIR=/opt/agent-skills
```

**Option B: Use a Cloud Storage volume mount** (no image rebuild):

1. Upload skills to a GCS bucket:
   ```bash
   gsutil -m cp -r my-skills/* gs://$PROJECT_ID-agent-skills/
   ```

2. Add a volume mount to `service.yaml` on the agent container:
   ```yaml
   volumeMounts:
     - name: agent-skills
       mountPath: /skills
       readOnly: true
   ```

3. Add the volume definition:
   ```yaml
   volumes:
     - name: agent-skills
       csi:
         driver: gcsfuse.run.googleapis.com
         volumeAttributes:
           bucketName: ${PROJECT_ID}-agent-skills
   ```

4. Set the environment variable:
   ```yaml
   - name: SKILLS_DIR
     value: "/skills"
   ```

5. Redeploy:
   ```bash
   ./deploy/cloudrun/deploy.sh --service agent
   ```

#### Overriding bundled skills

To override a bundled skill (e.g., customize `response-formatting` for your deployment), create a skill directory with the same name in your external skills directory:

```
my-skills/
├── response-formatting/        # Overrides the bundled response-formatting skill
│   └── SKILL.md
└── domain-specific-rules/      # New skill added alongside bundled ones
    └── SKILL.md
```

The agent logs which skills were loaded and which were overridden:
```
Loaded 6 bundled skills from /app/core/skills
External skills overriding bundled: response-formatting
Loaded 2 external skills from /skills (7 total)
```

### Staging Environment (MCP Sidecar)

When deploying against the Red Hat **staging** environment, the MCP sidecar needs environment variable overrides so it connects to the stage Insights APIs and SSO instead of production.

Uncomment the `env` block on the `insights-mcp` container in `service.yaml`:

```yaml
env:
  - name: LIGHTSPEED_BASE_URL
    value: "https://console.stage.redhat.com"
  - name: LIGHTSPEED_SSO_BASE_URL
    value: "https://sso.stage.redhat.com"
```

These overrides are already present in `service.yaml` as commented-out lines. To enable them:

1. Edit `deploy/cloudrun/service.yaml` and uncomment the `env` section under the `insights-mcp` container.
2. Update the **agent container** SSO issuer to match the staging environment:
   ```yaml
   - name: RED_HAT_SSO_ISSUER
     value: "https://sso.stage.redhat.com/auth/realms/redhat-external"
   ```
3. If using DCR, also update the marketplace handler for staging — see [GMA SSO API Configuration (Staging vs Production)](#gma-sso-api-configuration-staging-vs-production).
4. Redeploy:
   ```bash
   ./deploy/cloudrun/deploy.sh --service agent
   ```

### Alternative: Use Docker Hub

Instead of GCR, you can copy the image to Docker Hub (free storage, but has rate limits):

```bash
# Pull from Quay.io
docker pull quay.io/redhat-services-prod/insights-management-tenant/insights-mcp/red-hat-lightspeed-mcp:latest

# Tag for Docker Hub (replace YOUR_USERNAME with your Docker Hub username)
docker tag quay.io/redhat-services-prod/insights-management-tenant/insights-mcp/red-hat-lightspeed-mcp:latest \
  docker.io/YOUR_USERNAME/red-hat-lightspeed-mcp:latest

# Login and push to Docker Hub
docker login
docker push docker.io/YOUR_USERNAME/red-hat-lightspeed-mcp:latest

# Deploy with Docker Hub image
./deploy/cloudrun/deploy.sh --mcp-image docker.io/YOUR_USERNAME/red-hat-lightspeed-mcp:latest
```

**Docker Hub Rate Limits:**
| Account Type | Pull Limit | Cost |
|--------------|------------|------|
| Anonymous | 100 pulls / 6 hours | Free |
| Free (authenticated) | 200 pulls / 6 hours | Free |
| Pro | 5,000 pulls / day | $5/month |
| Team | Unlimited | $9/user/month |

**When to use Docker Hub:**
- Development or low-traffic deployments
- You already have a Docker Hub account

**When to use GCR (recommended for production):**
- Auto-scaling deployments (rate limits could cause failures)
- High availability requirements
- Cost is negligible (~$0.005/month)

### Scaling

| Setting | Agent | Handler | Description |
|---------|-------|---------|-------------|
| Min Instances | 1 | 1 | Always keep at least one instance running |
| Max Instances | 10 | 2 | Maximum concurrent instances |
| Concurrency | 80 | 80 | Requests per instance |
| Timeout | 300s | 60s | Request timeout |

## How the MCP Server Works

The MCP server runs as a sidecar container alongside the agent:

1. **Agent Container** (port 8000): Handles A2A requests, uses configurable LLM (Gemini by default)
2. **MCP Server Container** (port 8080): Provides tools for Red Hat Insights APIs

When the agent needs to access Insights data (e.g., system vulnerabilities, recommendations):
1. Agent calls MCP tools via HTTP to `localhost:8080`
2. Agent forwards credentials to the MCP server via HTTP headers (see below)
3. MCP server authenticates with console.redhat.com
4. MCP server calls the appropriate Insights API
5. Results are returned to the agent for processing

### MCP Authentication

The agent forwards the caller's JWT token to the MCP server via the
`Authorization: Bearer` header. The MCP server uses this token to call
console.redhat.com APIs on behalf of the user.

### Credential Flow

```
Client                     Agent                   MCP Server        console.redhat.com
  │                          │                         │                     │
  │  POST / (A2A)            │                         │                     │
  │  Authorization: Bearer T │                         │                     │
  ├─────────────────────────►│                         │                     │
  │                          │  MCP tool call          │                     │
  │                          │  Authorization: Bearer T│                     │
  │                          ├────────────────────────►│                     │
  │                          │                         │  API Request + T    │
  │                          │                         ├────────────────────►│
  │                          │                         │  API Response       │
  │                          │                         │◄────────────────────┤
  │                          │  Tool result            │                     │
  │                          │◄────────────────────────┤                     │
  │  A2A Response            │                         │                     │
  │◄─────────────────────────┤                         │                     │
```

## Authentication

The agent uses **Red Hat SSO** for authentication via **token
introspection** (RFC 7662).  Requests to the A2A endpoint (POST /) require a
Bearer token that is active and carries the `api.console` and `api.ocm` scopes.

### Authentication Flow

```
┌──────────┐    ┌───────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐
│  Client  │    │Lightspeed Agt │    │ Red Hat SSO  │    │  MCP Server  │    │console.redhat.com│
│(Gemini)  │    │  (port 8000)  │    │ (Red Hat SSO)│    │  (port 8080) │    │ (Insights APIs)  │
└────┬─────┘    └──────┬────────┘    └──────┬───────┘    └──────┬───────┘    └────────┬─────────┘
     │                 │                    │                   │                     │
     │  ── Obtain Token (directly from SSO) ──                 │                     │
     │                 │                    │                   │                     │
     │ 1. client_credentials grant         │                   │                     │
     ├─────────────────────────────────────►│                   │                     │
     │ 2. Access token                     │                   │                     │
     │◄────────────────────────────────────-┤                   │                     │
     │                 │                    │                   │                     │
     │  ── A2A Request with Tool Call ──    │                   │                     │
     │                 │                    │                   │                     │
     │ 3. POST / (A2A) │                    │                   │                     │
     │    Bearer token │                    │                   │                     │
     ├────────────────►│ 4. Introspect      │                   │                     │
     │                 │    token + check   │                   │                     │
     │                 │    required scopes │                   │                     │
     │                 ├───────────────────►│                   │                     │
     │                 │                    │                   │                     │
     │                 │ 5. MCP tool call   │                   │                     │
     │                 │  + Bearer token    │                   │                     │
     │                 ├───────────────────────────────────────►│                     │
     │                 │                    │                   │ 6. Insights API     │
     │                 │                    │                   │    (using token)    │
     │                 │                    │                   ├────────────────────►│
     │                 │                    │                   │ 7. API response     │
     │                 │                    │                   │◄────────────────────┤
     │                 │ 8. Tool result     │                   │                     │
     │                 │◄──────────────────────────────────────-┤                     │
     │ 9. A2A Response │                    │                   │                     │
     │◄────────────────┤                    │                   │                     │
```

**Credential sets:**
- **Red Hat SSO credentials** (`RED_HAT_SSO_CLIENT_ID/SECRET`): Used by the agent as Resource Server credentials for token introspection (step 4)
- **MCP authentication** (step 5): The caller's Bearer token is forwarded to the MCP server (see [MCP Authentication](#mcp-authentication))

### Configuration

| Secret / Env Var | Description |
|------------------|-------------|
| `redhat-sso-client-id` | Resource Server client ID (used for token introspection) |
| `redhat-sso-client-secret` | Resource Server client secret |
| `MARKETPLACE_HANDLER_URL` | URL of the marketplace-handler service. Used to build the DCR endpoints in the AgentCard. If empty, falls back to `AGENT_PROVIDER_URL`. Set automatically by `deploy.sh`. |
| `AGENT_PROVIDER_ORGANIZATION_URL` | Provider's organization website URL (default: `https://www.redhat.com`). Used in AgentCard `provider.url` and as the expected JWT audience for Google DCR `software_statement` validation. Set in YAML configs, not changed by `deploy.sh`. |
| `AGENT_REQUIRED_SCOPE` | Comma-separated OAuth scopes required in tokens (default: `api.console,api.ocm`) |
| `AGENT_ALLOWED_SCOPES` | Comma-separated allowlist of permitted scopes (default: `openid,profile,email,api.console,api.ocm,metering:admin`). Tokens with scopes outside this list are rejected (403). |

### Development Mode

Set `SKIP_JWT_VALIDATION=true` to disable token introspection for local
development.  The agent still extracts the Bearer token from the request and
forwards it to the MCP server (JWT pass-through continues to work).  Requests
without a Bearer token are also allowed.

## Endpoints

After deployment, the following endpoints are available:

### Marketplace Handler Service

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /ready` | Readiness check |
| `POST /dcr` | DCR requests (OAuth client registration) |
| `POST /pubsub` | Pub/Sub events (Google OIDC authenticated) |

### Lightspeed Agent Service

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /ready` | Readiness check |
| `GET /.well-known/agent.json` | A2A AgentCard (public) |
| `POST /` | A2A JSON-RPC endpoint (message/send, message/stream) |

## Testing the Deployment

```bash
# Get service URLs
HANDLER_URL=$(gcloud run services describe marketplace-handler \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')

AGENT_URL=$(gcloud run services describe lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')

# Test marketplace handler health
curl $HANDLER_URL/health

# Test agent health
curl $AGENT_URL/health

# Get AgentCard (public endpoint)
curl $AGENT_URL/.well-known/agent.json

# View logs for each service
gcloud run services logs read marketplace-handler \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT

gcloud run services logs read lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT
```

## Database Architecture

Cloud Run deployments **require PostgreSQL** (Cloud SQL) for production. The system uses **two databases** for security isolation:

| Database | Purpose | Service |
|----------|---------|---------|
| Marketplace DB | Orders, entitlements, DCR clients | Both handler and agent |
| Session DB | ADK agent sessions | Agent only |

This separation ensures:
- Agent sessions cannot access marketplace/auth data
- Compromised agents cannot access DCR credentials
- Different retention policies can be applied

> **Setup:** See [Step 3. Set Up Cloud SQL Database](#3-set-up-cloud-sql-database) in Quick Start.

### Adding Cloud SQL to Existing Services

If you deployed services before setting up Cloud SQL, add the connection:

```bash
CONNECTION_NAME=$(gcloud sql instances describe $DB_INSTANCE_NAME \
  --project=$GOOGLE_CLOUD_PROJECT --format='value(connectionName)')

# Add to marketplace handler
gcloud run services update marketplace-handler \
  --add-cloudsql-instances=$CONNECTION_NAME \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT

# Add to insights agent
gcloud run services update lightspeed-agent \
  --add-cloudsql-instances=$CONNECTION_NAME \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT
```

### Session Database Behavior

- If `SESSION_DATABASE_URL` is set: Uses PostgreSQL for session persistence
- If `SESSION_DATABASE_URL` is not set: Uses in-memory storage (sessions lost on restart)

For production, always configure `SESSION_DATABASE_URL` to ensure session persistence across container restarts and scaling events.

## Custom Domain

Map a custom domain to your Cloud Run service:

```bash
gcloud run domain-mappings create \
  --service=lightspeed-agent \
  --domain=agent.yourdomain.com \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT
```

Follow the instructions to verify domain ownership and configure DNS.

## Testing the Agent

Once deployed, you can test the agent using a local proxy that handles Google Cloud Run authentication.

> **Important:** When full authentication is enabled (the default on Cloud Run),
> every A2A request must pass three validation steps:
>
> 1. **Token introspection** — The Bearer token must be active at Red Hat SSO
> 2. **DCR client lookup** — The token's `client_id` (`azp` claim) must exist in the `dcr_clients` table
> 3. **Entitlement check** — The DCR client's `order_id` must have an `active` entitlement in `marketplace_entitlements`
>
> Without steps 2 and 3, you will get `"No active order found for this client"` (403 Forbidden).
> See [Authentication Setup for Testing](#authentication-setup-for-testing) below to configure
> the required database records before sending A2A requests.

### Authentication Setup for Testing

#### Prerequisites

Install the [Cloud SQL Auth Proxy](https://cloud.google.com/sql/docs/postgres/connect-auth-proxy)
if you don't have it already, then start it and fetch database credentials:

```bash
# Install Cloud SQL Auth Proxy (one-time setup)
# Linux (amd64):
curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.15.2/cloud-sql-proxy.linux.amd64
chmod +x cloud-sql-proxy

# macOS (Apple Silicon):
# curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.15.2/cloud-sql-proxy.darwin.arm64
# chmod +x cloud-sql-proxy
```

```bash
# Terminal 1: Start Cloud SQL Auth Proxy
./cloud-sql-proxy --port 5432 \
  ${GOOGLE_CLOUD_PROJECT}:${GOOGLE_CLOUD_LOCATION}:${DB_INSTANCE_NAME:-lightspeed-agent-db}
```

```bash
# Terminal 2: Fetch credentials from Secret Manager
CLOUD_DB_URL=$(gcloud secrets versions access latest \
  --secret=database-url --project=$GOOGLE_CLOUD_PROJECT)
DB_PASSWORD=$(echo "$CLOUD_DB_URL" | sed -n 's|.*://insights:\([^@]*\)@.*|\1|p')
export DATABASE_URL="postgresql+asyncpg://insights:${DB_PASSWORD}@localhost:5432/lightspeed_agent"
export DCR_ENCRYPTION_KEY=$(gcloud secrets versions access latest \
  --secret=dcr-encryption-key --project=$GOOGLE_CLOUD_PROJECT)
```

#### Seed database records and get a token

This seeds the database with a DCR client and entitlement that match your
`ocm token`'s `azp` claim, allowing you to use `ocm token` directly for A2A
requests.

> **Why `ocm token`?** DCR clients created via the GMA API only support
> `authorization_code` and `refresh_token` grants (the flows Gemini Enterprise
> uses) — `client_credentials` is not enabled. The `ocm token` approach works
> because it produces a valid Red Hat SSO token whose `azp` claim we map to a
> seeded DCR client in the database.

**1. Set scope requirements to match `ocm token`:**

The `ocm token` carries `openid`, `roles`, and `web-origins` scopes — not the
`api.console` / `api.ocm` scopes the agent requires by default. Temporarily set
both required and allowed scopes to match the ocm token on Cloud Run:

```bash
gcloud run services update lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="AGENT_REQUIRED_SCOPE=openid,roles,web-origins" \
  --update-env-vars="AGENT_ALLOWED_SCOPES=openid,roles,web-origins"
```

> **Remember to restore these after testing** — see
> [Cleanup test records](#cleanup-test-records) below.

**2. Login to OCM and get the token's client_id:**

```bash
ocm login --use-auth-code

# Decode the token's azp (authorized party) claim — this is the client_id
# the auth middleware will look up in the dcr_clients table
OCM_CLIENT_ID=$(ocm token | cut -d. -f2 | base64 -d 2>/dev/null | jq -r '.azp')
echo "OCM client_id (azp): $OCM_CLIENT_ID"
```

**3. Choose an order ID and seed the DCR client:**

```bash
export TEST_ORDER_ID="test-order-$(date +%s)"
export TEST_ACCOUNT_ID="test-account-001"

python scripts/seed_dcr_clients.py seed \
  --client-id "$OCM_CLIENT_ID" \
  --client-secret "placeholder-not-used-for-ocm" \
  --order-id "$TEST_ORDER_ID" \
  --account-id "$TEST_ACCOUNT_ID"
```

**4. Seed the matching entitlement record:**

```bash
python3 -c "
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.begin() as conn:
        await conn.execute(text('''
            INSERT INTO marketplace_entitlements (id, account_id, provider_id, state, metadata)
            VALUES (:id, :account_id, 'test-provider', 'active', '{}')
            ON CONFLICT (id) DO UPDATE SET state = 'active'
        '''), {'id': os.environ['TEST_ORDER_ID'], 'account_id': os.environ['TEST_ACCOUNT_ID']})
    print(f'Entitlement seeded: order_id={os.environ[\"TEST_ORDER_ID\"]}')
    await engine.dispose()

asyncio.run(main())
"
```

**5. Verify the records:**

```bash
# Check DCR client
python scripts/seed_dcr_clients.py list

# Check entitlement
python3 -c "
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.connect() as conn:
        result = await conn.execute(text('''
            SELECT id, account_id, state FROM marketplace_entitlements
            WHERE id = :id
        '''), {'id': os.environ['TEST_ORDER_ID']})
        row = result.first()
        if row:
            print(f'Entitlement: id={row.id}, account_id={row.account_id}, state={row.state}')
        else:
            print('ERROR: Entitlement not found')
    await engine.dispose()

asyncio.run(main())
"
```

**6. Get your token:**

```bash
export RED_HAT_TOKEN=$(ocm token)
```

Your `RED_HAT_TOKEN` is now ready. Quick smoke test against the deployed agent:

```bash
AGENT_URL=$(gcloud run services describe lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')

curl -X POST $AGENT_URL/ \
  -H "Authorization: Bearer $RED_HAT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "1",
        "role": "user",
        "parts": [{"type": "text", "text": "Can you give me the systems affected by CVE-2023-49569"}]
      }
    },
    "id": "1"
  }' | jq .
```

For more testing options, see [Test A2A Requests with Local Proxy](#test-a2a-requests-with-local-proxy)
or [Test with A2A Inspector](#test-with-a2a-inspector) below.

#### Cleanup test records

When done testing, restore scope settings and remove the seeded database records.
The database cleanup requires the Cloud SQL Auth Proxy and environment variables
from [Prerequisites](#prerequisites) above.

```bash
# 1. Restore scope requirements
gcloud run services update lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="AGENT_REQUIRED_SCOPE=api.console,api.ocm" \
  --update-env-vars="AGENT_ALLOWED_SCOPES=openid,profile,email,api.console,api.ocm,metering:admin"

# 2. Remove DCR client
python scripts/seed_dcr_clients.py delete --order-id "$TEST_ORDER_ID" --confirm

# 3. Remove entitlement
python3 -c "
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.begin() as conn:
        result = await conn.execute(text('''
            DELETE FROM marketplace_entitlements WHERE id = :id
        '''), {'id': os.environ['TEST_ORDER_ID']})
        print(f'Deleted {result.rowcount} entitlement(s)')
    await engine.dispose()

asyncio.run(main())
"
```

### Test Agent Card

Verify the agent is running and accessible:

```bash
# Get the agent URL
AGENT_URL=$(gcloud run services describe lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')

# Test agent card endpoint (requires authentication)
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  $AGENT_URL/.well-known/agent-card.json | jq .
```

### Test A2A Requests with Local Proxy

The local proxy handles Google Cloud Run authentication, allowing you to test with just your Red Hat SSO token.

**Important:** The MCP sidecar inside Cloud Run uses port 8080. To avoid port conflicts, run the proxy on a different port (e.g., 8099).

**1. Start the local proxy:**

```bash
# Start proxy on localhost:8099 (NOT 8080 - that's used by MCP sidecar)
gcloud run services proxy lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --port=8099
```

This command will keep running in your terminal. The proxy authenticates all requests to Cloud Run using your current `gcloud` credentials.

**2. Configure AGENT_PROVIDER_URL for local testing:**

The agent card needs to advertise the proxy URL so tools like A2A Inspector connect to it:

```bash
# In a new terminal, set the agent URL to point to your local proxy
gcloud run services update lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="AGENT_PROVIDER_URL=http://localhost:8099"

# Wait for the update to complete (takes ~30 seconds)
# The proxy automatically handles the connection to Cloud Run
```

**Important:** This makes the agent advertise itself as `http://localhost:8099/` to ALL clients. This is fine for local testing, but remember to restore the real URL when done (see cleanup section below).

**3. Get a Red Hat SSO access token:**

Follow the [Authentication Setup for Testing](#authentication-setup-for-testing)
section above to configure database records and obtain a token (`RED_HAT_TOKEN`).
You must complete that setup first — without it, the agent will reject requests
with `"No active order found for this client"` (403).

```bash
# Verify token is set and valid (should show decoded JWT payload)
echo $RED_HAT_TOKEN | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```

**4. Test the A2A endpoint:**

The agent uses the A2A (Agent-to-Agent) protocol, which is based on JSON-RPC 2.0. All requests must include:
- `jsonrpc`: "2.0"
- `method`: "message/send" (for non-streaming) or "message/stream" (for streaming)
- `params`: Contains the message object with `messageId`
- `id`: Unique request identifier

```bash
# Send a test message to the agent (note: using port 8099, not 8080)
curl -X POST http://localhost:8099/ \
  -H "Authorization: Bearer $RED_HAT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "1",
        "role": "user",
        "parts": [{"type": "text", "text": "What are the latest CVEs affecting my systems?"}]
      }
    },
    "id": "1"
  }' | jq .
```

**Expected response:**

```json
{
  "jsonrpc": "2.0",
  "result": {
    "id": "task-abc123",
    "status": {
      "state": "completed"
    },
    "artifacts": [
      {
        "parts": [
          {
            "type": "text",
            "text": "Based on your systems, here are the latest CVEs..."
          }
        ]
      }
    ]
  },
  "id": "1"
}
```

**5. Test other endpoints:**

```bash
# Check health endpoint (no auth required)
curl http://localhost:8099/health | jq .

# Get agent card (should show http://localhost:8099/)
curl http://localhost:8099/.well-known/agent-card.json | jq -r '.url'
```

### Test with A2A Inspector

The [A2A Inspector](https://github.com/a2aproject/a2a-inspector) provides a web-based UI for testing A2A agents.

**1. Prerequisites:**

```bash
# Make sure the proxy is running (from step 1 above)
# Make sure AGENT_PROVIDER_URL is set to http://localhost:8099 (from step 2 above)
# Make sure you have completed the Authentication Setup for Testing (from step 3 above)
# Make sure RED_HAT_TOKEN is set (via ocm token or client_credentials grant)
```

**2. Start A2A Inspector:**

```bash
# Clone and run A2A Inspector (if not already installed)
git clone https://github.com/a2aproject/a2a-inspector.git /tmp/a2a-inspector
cd /tmp/a2a-inspector
uv sync
npm install -C frontend
./scripts/run.sh  # Usually runs on port 5001
```

**3. Configure A2A Inspector:**

In the A2A Inspector web UI (usually at `http://localhost:5001`):

1. **Agent URL**: Enter `http://localhost:8099/`
2. **Authentication**:
   - Select "Bearer Token" or "OAuth"
   - Paste your `RED_HAT_TOKEN` (obtained via [Authentication Setup for Testing](#authentication-setup-for-testing))
3. Click "Connect" - it will fetch the agent card from `http://localhost:8099/.well-known/agent-card.json`

The A2A Inspector will read the agent card and see `"url": "http://localhost:8099/"`, which points back to your local proxy. All messages will flow through the proxy to Cloud Run.

**4. Send test messages:**

In the A2A Inspector UI:
- Type: "What are my RHEL systems?"
- Type: "Show CVEs affecting my infrastructure"
- Type: "What is the lifecycle for RHEL 8?"

The inspector will send properly formatted JSON-RPC requests with `messageId` fields automatically.

### Cleanup After Testing

When you're done testing, clean up the local proxy, restore the production
configuration, and remove any test database records.

**1. Remove test database records:**

Follow the [Cleanup test records](#cleanup-test-records) steps in the
Authentication Setup section to remove any DCR clients and entitlements you
seeded. This requires the Cloud SQL Auth Proxy to still be running.

```bash
# Remove seeded DCR client and entitlement
python scripts/seed_dcr_clients.py delete --order-id "$TEST_ORDER_ID" --confirm

python3 -c "
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.begin() as conn:
        result = await conn.execute(text('''
            DELETE FROM marketplace_entitlements WHERE id = :id
        '''), {'id': os.environ['TEST_ORDER_ID']})
        print(f'Deleted {result.rowcount} entitlement(s)')
    await engine.dispose()

asyncio.run(main())
"
```

**2. Restore AGENT_PROVIDER_URL to the real Cloud Run URL:**

```bash
# Get the actual Cloud Run service URL
SERVICE_URL=$(gcloud run services describe lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')

# Restore the agent card to advertise the real Cloud Run URL
gcloud run services update lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="AGENT_PROVIDER_URL=$SERVICE_URL"

# Verify the agent card now shows the correct URL
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  $SERVICE_URL/.well-known/agent-card.json | jq -r '.url'
# Should show: https://lightspeed-agent-xxxxx.run.app/
```

**3. Stop the proxy:**

Press `Ctrl+C` in the terminal where the proxy is running.

**4. Clean up port (if needed):**

If the port is still in use:

```bash
# Find and kill process using port 8099
lsof -ti:8099 | xargs kill -9

# Or on systems without lsof
fuser -k 8099/tcp
```

**Note:** The proxy doesn't create any cloud resources - it only runs locally on your machine. Stopping the proxy (Ctrl+C) is sufficient to clean up.

**Why port 8099 instead of 8080?**

The MCP sidecar inside Cloud Run uses port 8080 internally. If you run the proxy on port 8080, the agent will try to connect to the proxy instead of the MCP sidecar, causing "Failed to create MCP session" errors. Using port 8099 (or any other port except 8080) avoids this conflict.

### Testing Without Proxy (Direct Cloud Run Access)

If you prefer to test without the proxy, you'll need to:

1. **Allow unauthenticated access** (requires admin permissions):
   ```bash
   gcloud run services add-iam-policy-binding lightspeed-agent \
     --region=$GOOGLE_CLOUD_LOCATION \
     --project=$GOOGLE_CLOUD_PROJECT \
     --member="allUsers" \
     --role="roles/run.invoker"
   ```

2. **Test directly** with the Cloud Run URL (requires [auth setup](#authentication-setup-for-testing)):
   ```bash
   # RED_HAT_TOKEN must be set via the Authentication Setup for Testing section
   curl -X POST $AGENT_URL/ \
     -H "Authorization: Bearer $RED_HAT_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "jsonrpc": "2.0",
       "method": "message/send",
       "params": {
         "message": {
           "messageId": "1",
           "role": "user",
           "parts": [{"type": "text", "text": "Hello"}]
         }
       },
       "id": "1"
     }'
   ```

**Security Note:** Allowing unauthenticated access makes the service publicly accessible. Only use this for development/testing environments, not production.

### Troubleshooting Testing Issues

**"Method Not Allowed" or "detail": "Method Not Allowed"**

This usually means you're testing the endpoint without proper authentication or the request format is incorrect:

```bash
# Make sure you have a valid token (see Authentication Setup for Testing)
# Verify token is valid (should show decoded JWT payload)
echo $RED_HAT_TOKEN | cut -d. -f2 | base64 -d 2>/dev/null | jq .

# Make sure proxy is running
# You should see: "Listening on http://localhost:8080"
gcloud run services proxy lightspeed-agent --region=us-central1 --port=8080
```

**"No active order found for this client"** (403 Forbidden)

The token is valid but the auth middleware cannot find a matching DCR client or
active entitlement in the database. This is the most common issue when testing.

The middleware performs these lookups:
1. Looks up the token's `azp` (client_id) in the `dcr_clients` table
2. Uses the `order_id` from that record to check `marketplace_entitlements`
3. Verifies the entitlement `state` is `active`

Fix: Complete the [Authentication Setup for Testing](#authentication-setup-for-testing)
to seed the required database records.

```bash
# Decode your token to see the azp claim being looked up
echo $RED_HAT_TOKEN | cut -d. -f2 | base64 -d 2>/dev/null | jq -r '.azp'

# Check if a DCR client exists for that azp
python scripts/seed_dcr_clients.py list

# Check if the entitlement exists and is active (replace <order-id>)
python3 -c "
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.connect() as conn:
        result = await conn.execute(text('''
            SELECT id, state FROM marketplace_entitlements WHERE id = :id
        '''), {'id': '<order-id>'})
        row = result.first()
        if row:
            print(f'Entitlement: id={row.id}, state={row.state}')
        else:
            print('ERROR: No entitlement found for this order_id')
    await engine.dispose()

asyncio.run(main())
"
```

**"Invalid Authorization header format"**

The agent expects a Red Hat SSO Bearer token, not a Google Cloud identity token. Make sure:
- You're using a token from the [Authentication Setup](#authentication-setup-for-testing)
- The token is a valid JWT from Red Hat SSO
- You're including it as: `-H "Authorization: Bearer $RED_HAT_TOKEN"`

**"Field required" error (e.g. "messageId", "method")**

The A2A protocol requires specific fields. A common mistake is omitting
`messageId` from the message object. Make sure your request includes:

```json
{
  "jsonrpc": "2.0",
  "method": "message/send",
  "params": {
    "message": {
      "messageId": "1",
      "role": "user",
      "parts": [{"type": "text", "text": "Hello"}]
    }
  },
  "id": "1"
}
```

**"Token is missing required scope(s): api.console, api.ocm"**

The agent requires the `api.console` and `api.ocm` scopes in the access token
by default. If your Red Hat SSO client is not configured to issue these scopes,
you will see:

```json
{"jsonrpc":"2.0","error":{"code":-32003,"message":"Forbidden","data":{"detail":"Token is missing required scope(s): api.console, api.ocm"}},"id":null}
```

To temporarily adjust the required scopes for testing (e.g. when using
`ocm token` which carries `openid,roles,web-origins`), set both
`AGENT_REQUIRED_SCOPE` and `AGENT_ALLOWED_SCOPES` to match the token's scopes:

```bash
gcloud run services update ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="AGENT_REQUIRED_SCOPE=openid,roles,web-origins" \
  --update-env-vars="AGENT_ALLOWED_SCOPES=openid,roles,web-origins"
```

To restore the default scope requirements:

```bash
gcloud run services update ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="AGENT_REQUIRED_SCOPE=api.console,api.ocm" \
  --update-env-vars="AGENT_ALLOWED_SCOPES=openid,profile,email,api.console,api.ocm,metering:admin"
```

> **Note:** Both `AGENT_REQUIRED_SCOPE` and `AGENT_ALLOWED_SCOPES` must not be
> empty in production environments. The agent validates at startup that required
> scopes are a subset of allowed scopes.

These settings are also configurable in `service.yaml`.

**"Token carries disallowed scope(s): ..."**

The agent enforces a scope allowlist (`AGENT_ALLOWED_SCOPES`) to prevent tokens
with elevated privileges from being forwarded to downstream services.  If the
token carries scopes not in the allowlist, you will see a 403 error.

Add the missing scope(s) to the allowlist:

```bash
gcloud run services update ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="AGENT_ALLOWED_SCOPES=openid,profile,email,api.console,api.ocm,metering:admin,your.extra.scope"
```

This setting is also configurable in `service.yaml` via the
`AGENT_ALLOWED_SCOPES` environment variable.

**Empty response or connection refused**

- Ensure the proxy is running in a separate terminal
- Verify the agent is deployed and healthy:
  ```bash
  gcloud run services describe lightspeed-agent \
    --region=us-central1 \
    --format='value(status.conditions.status)'
  # Should show: True;True;True
  ```

## Testing DCR on Cloud Run

This section explains how to test the DCR (Dynamic Client Registration) flow
against a deployed marketplace handler.

```
┌──────────────┐
│  Test Script │
│  (local)     │
└──────┬───────┘
       │ POST /dcr
       │ (software_statement JWT)
       │ + Cloud Run ID token
       ▼
┌──────────────────────┐         ┌──────────────────┐
│  Marketplace Handler │────────>│  Red Hat SSO      │
│  (Cloud Run)         │         │  (GMA SSO API)    │
│                      │<────────│  Creates tenant   │
│  Validates JWT,      │         └──────────────────┘
│  creates tenant,     │
│  returns creds       │
└──────────────────────┘
```

Testing requires `SKIP_JWT_VALIDATION=true` on the handler to accept test
JWTs not signed by Google's production `cloud-agentspace` service account.

Testing also requires a signing service account. Create one if you don't
have one already:

```bash
gcloud iam service-accounts create dcr-test \
  --display-name "DCR test signer" \
  --project=$GOOGLE_CLOUD_PROJECT

# NOTE: GCP may need a few seconds to propagate the new service account.
# If the next command fails with NOT_FOUND, wait ~10 seconds and retry.
sleep 10

gcloud iam service-accounts keys create dcr-test-key.json \
  --iam-account=dcr-test@$GOOGLE_CLOUD_PROJECT.iam.gserviceaccount.com \
  --project=$GOOGLE_CLOUD_PROJECT
```

### Running the DCR Test

**1. Configure the handler for testing:**

```bash
# Generate and store Fernet encryption key (if not already set)
FERNET_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
echo -n "$FERNET_KEY" | \
  gcloud secrets versions add dcr-encryption-key \
    --data-file=- --project=$GOOGLE_CLOUD_PROJECT

# Update handler env vars (deploys a new revision)
gcloud run services update marketplace-handler \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="\
SKIP_JWT_VALIDATION=true"
```

**2. Run the test script:**

```bash
HANDLER_URL=$(gcloud run services describe marketplace-handler \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')

export MARKETPLACE_HANDLER_URL=$HANDLER_URL
export TEST_SA_KEY_FILE=dcr-test-key.json
# Generate a fresh order ID for each test run
export TEST_ORDER_ID="order-$(uuidgen || python3 -c 'import uuid; print(uuid.uuid4())')"
# Don't set SKIP_CLOUD_RUN_AUTH -- script fetches an ID token automatically

python scripts/test_deployed_dcr.py
```

The handler creates OAuth tenant credentials via the GMA SSO API and returns
them. The second request verifies idempotency (same credentials returned for
the same order).

> **Note:** If the handler was deployed with `--allow-unauthenticated`, you can
> set `export SKIP_CLOUD_RUN_AUTH=true` to skip ID token authentication.

**3. Restore production configuration:**

```bash
gcloud run services update marketplace-handler \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="\
SKIP_JWT_VALIDATION=false"
```

#### Admin Tool: seed_dcr_clients.py

The `seed_dcr_clients.py` script is available as an admin tool for
managing DCR client records in the database (listing, deleting, or bulk
inserting credentials).

```bash
# Connect to Cloud SQL first (requires Cloud SQL Auth Proxy)
./cloud-sql-proxy --port 5432 ${GOOGLE_CLOUD_PROJECT}:${GOOGLE_CLOUD_LOCATION}:${DB_INSTANCE_NAME:-lightspeed-agent-db}

# Fetch DATABASE_URL and DCR_ENCRYPTION_KEY from Secret Manager
CLOUD_DB_URL=$(gcloud secrets versions access latest \
  --secret=database-url --project=$GOOGLE_CLOUD_PROJECT)
DB_PASSWORD=$(echo "$CLOUD_DB_URL" | sed -n 's|.*://insights:\([^@]*\)@.*|\1|p')
export DATABASE_URL="postgresql+asyncpg://insights:${DB_PASSWORD}@localhost:5432/lightspeed_agent"
export DCR_ENCRYPTION_KEY=$(gcloud secrets versions access latest \
  --secret=dcr-encryption-key --project=$GOOGLE_CLOUD_PROJECT)

# List existing entries
python scripts/seed_dcr_clients.py list

# Delete an entry
python scripts/seed_dcr_clients.py delete --order-id order-12345 --confirm
```

### Test Script Reference

The test script at `scripts/test_deployed_dcr.py` is configurable via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MARKETPLACE_HANDLER_URL` | (required) | Cloud Run handler URL |
| `TEST_SA_KEY_FILE` | - | Path to SA key file (Method A) |
| `TEST_SERVICE_ACCOUNT` | - | SA email for IAM API signing (Method B) |
| `PROVIDER_URL` | `https://www.redhat.com` | JWT audience claim (must match handler's `AGENT_PROVIDER_ORGANIZATION_URL`) |
| `SKIP_CLOUD_RUN_AUTH` | `false` | Skip Cloud Run ID token auth |
| `TEST_ORDER_ID` | random UUID | Fixed order ID |
| `TEST_ACCOUNT_ID` | `test-procurement-account-001` | Procurement account ID |
| `TEST_REDIRECT_URIS` | `https://gemini.google.com/callback` | Comma-separated redirect URIs |

## GMA SSO API Configuration (Staging vs Production)

The marketplace handler creates OAuth tenant clients in Red Hat SSO via the GMA API. Two environment variables control which SSO environment is used:

| Variable | Description |
|----------|-------------|
| `RED_HAT_SSO_ISSUER` | SSO issuer URL. The token endpoint (`/protocol/openid-connect/token`) is derived from this. |
| `GMA_API_BASE_URL` | GMA tenant creation API endpoint. |

### Environment Values

| Environment | `RED_HAT_SSO_ISSUER` | `GMA_API_BASE_URL` |
|-------------|----------------------|--------------------|
| **Production** | `https://sso.redhat.com/auth/realms/redhat-external` | `https://sso.redhat.com/auth/realms/redhat-external/apis/beta/acs/v1/` |
| **Staging** | `https://sso.stage.redhat.com/auth/realms/redhat-external` | `https://sso.stage.redhat.com/auth/realms/redhat-external/apis/beta/acs/v1/` |

Both values are set in `marketplace-handler.yaml`. To switch to staging, update both variables and use staging-specific `GMA_CLIENT_ID` / `GMA_CLIENT_SECRET` credentials:

```bash
gcloud run services update ${HANDLER_SERVICE_NAME:-marketplace-handler} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="\
RED_HAT_SSO_ISSUER=https://sso.stage.redhat.com/auth/realms/redhat-external,\
GMA_API_BASE_URL=https://sso.stage.redhat.com/auth/realms/redhat-external/apis/beta/acs/v1/"

# Update GMA credentials in Secret Manager with staging values
echo -n 'your-staging-gma-client-id' | \
  gcloud secrets versions add gma-client-id --data-file=- --project=$GOOGLE_CLOUD_PROJECT
echo -n 'your-staging-gma-client-secret' | \
  gcloud secrets versions add gma-client-secret --data-file=- --project=$GOOGLE_CLOUD_PROJECT
```

**Important:** `RED_HAT_SSO_ISSUER` and `GMA_API_BASE_URL` must point to the same SSO environment. The GMA client credentials (`GMA_CLIENT_ID` / `GMA_CLIENT_SECRET`) are environment-specific and cannot be shared between staging and production.

## Audit Logging

The agent automatically produces structured audit logs that correlate each user session with Red Hat API requests. When `LOG_FORMAT=json` (the default in Cloud Run), every log record includes:

- **`user_id`** — authenticated user (JWT `sub` claim)
- **`org_id`** — Red Hat organization (JWT `org_id` claim)
- **`order_id`** — Google Cloud Marketplace order
- **`request_id`** — UUID4 correlation ID (unique per request)

Each agent lifecycle event carries an `event_type` tag (`request_authenticated`, `agent_run_started`, `tool_call_completed`, `mcp_jwt_forwarded`, etc.) and tool calls include a `data_source` field identifying which Red Hat Insights MCP tool retrieved the data.

This provides a full data lineage audit trail: every piece of information disclosed by the agent can be traced back to a specific authenticated user prompt and a verified Red Hat Insights data source. These persistent logs are independent of the ephemeral ADK session storage.

### Querying Audit Logs

Cloud Logging automatically parses JSON log fields. To filter logs from the Lightspeed Agent service specifically, add a `resource.labels.service_name` filter:

```bash
# All Lightspeed Agent logs (filter by Cloud Run service name)
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lightspeed-agent"' \
  --project=$GOOGLE_CLOUD_PROJECT --limit=50

# All actions by a specific user (scoped to the agent service)
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lightspeed-agent" AND jsonPayload.user_id="<user-id>"' \
  --project=$GOOGLE_CLOUD_PROJECT --limit=50

# All events in a single request (correlation)
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lightspeed-agent" AND jsonPayload.request_id="<request-id>"' \
  --project=$GOOGLE_CLOUD_PROJECT

# All MCP data access for an organization
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lightspeed-agent" AND jsonPayload.org_id="<org-id>" AND jsonPayload.message=~"mcp_jwt_forwarded"' \
  --project=$GOOGLE_CLOUD_PROJECT

# All tool calls with data source tracking
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lightspeed-agent" AND jsonPayload.message=~"tool_call_completed"' \
  --project=$GOOGLE_CLOUD_PROJECT --limit=20
```

No additional configuration is required — audit logging is automatically active when `LOG_FORMAT=json`.

## Monitoring

### Built-in Cloud Run Metrics

View standard Cloud Run metrics in the Google Cloud Console:
- **Cloud Run** → **Services** → **lightspeed-agent** → **Metrics**

### OpenTelemetry Business Metrics

The agent collects business-level metrics via OpenTelemetry and exports them to Cloud Monitoring using the [Google-Built OpenTelemetry Collector](https://cloud.google.com/stackdriver/docs/instrumentation/opentelemetry-collector-cloud-run) as a sidecar. This is enabled by default in `service.yaml` — both `deploy.sh` and Cloud Build deployments include the required configuration.

#### How It Works

```
┌──────────────────────────────────┐
│  Agent Container (port 8000)     │
│                                  │
│  MetricsCollector polls DB       │
│  every 60s, exports via OTLP     │
│       │                          │
│       ▼                          │
│  localhost:4317 (gRPC)           │
└──────────┬───────────────────────┘
           │
           ▼
┌──────────────────────────────────┐
│  OTel Collector Sidecar          │
│  (otelcol-google:0.151.0)        │
│                                  │
│  Receives OTLP metrics,          │
│  exports to Cloud Monitoring     │
└──────────┬───────────────────────┘
           │
           ▼
┌──────────────────────────────────┐
│  Google Cloud Monitoring         │
│                                  │
│  Metrics available under:        │
│  prometheus_target resource type │
└──────────────────────────────────┘
```

The setup in `service.yaml` consists of:

1. **OTel Collector sidecar** — the `collector` container (`otelcol-google:0.151.0`) receives OTLP metrics on `localhost:4317` and exports them to Cloud Monitoring via the `googlemanagedprometheus` exporter.

2. **Metrics enabled** — `OTEL_METRICS_ENABLED=true` starts the background metrics collector in the agent.

3. **Collector config** — the `otel-collector-config` Secret Manager secret (mounted at `/etc/otelcol-google/config.yaml`) configures the collector's receivers, processors, and exporters.

**Prerequisites:** The `otel-collector-config` secret must be created before deploying. Run `setup.sh` to create it automatically from `deploy/cloudrun/otel-collector-config.yaml`, or create it manually:

```bash
gcloud secrets create otel-collector-config \
  --data-file=deploy/cloudrun/otel-collector-config.yaml \
  --project=$GOOGLE_CLOUD_PROJECT
```

#### Available Metrics

| Name | Type | Attributes | Description |
|------|------|------------|-------------|
| `subscriptions_count` | Gauge | `account_id`, `state` | Entitlement count by account and state |
| `dcr_clients_active` | Gauge | `account_id` | Active DCR client count by account |
| `input_tokens` | Gauge | `account_id` | Total input tokens consumed by account |
| `output_tokens` | Gauge | `account_id` | Total output tokens consumed by account |
| `request_count` | Gauge | `account_id` | Total requests by account |
| `tool_calls_by_name` | Counter | `tool_name` | Tool invocations by MCP tool name |

Gauge metrics are polled from the database on each collection cycle. The `tool_calls_by_name` counter is incremented in real time but is in-process only — it resets to zero on instance restart.

#### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_METRICS_ENABLED` | `true` (in `service.yaml`) | Enable OTel metrics collection |
| `OTEL_METRICS_COLLECTION_INTERVAL` | `60` | DB polling interval in seconds (minimum 10) |

To change the collection interval on a running deployment:

```bash
gcloud run services update ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="OTEL_METRICS_COLLECTION_INTERVAL=30"
```

To update the OTel Collector configuration, add a new secret version:

```bash
gcloud secrets versions add otel-collector-config \
  --data-file=deploy/cloudrun/otel-collector-config.yaml \
  --project=$GOOGLE_CLOUD_PROJECT
```

Then redeploy the service to pick up the new config version.

#### Viewing Metrics in Cloud Monitoring

Metrics appear in Cloud Monitoring under the `prometheus_target` resource type with the prefix `prometheus.googleapis.com/`:

```bash
# List metrics from the agent
gcloud monitoring metrics-descriptors list \
  --project=$GOOGLE_CLOUD_PROJECT \
  --filter='metric.type=starts_with("prometheus.googleapis.com/")'
```

To explore in the console:
1. Go to **Monitoring** → **Metrics Explorer**
2. Select resource type **Prometheus Target**
3. Search for metric names like `subscriptions_count`, `input_tokens`, etc.
4. Metrics are named `prometheus.googleapis.com/{metric_name}/{type}` (e.g., `prometheus.googleapis.com/subscriptions_count/gauge`)

#### Minimum Instance Requirement

The metrics collector runs as a background task inside the agent process. If the agent scales to zero, no metrics are collected until an instance starts again. The default `service.yaml` sets `minScale: "1"`, so this is not a concern. If you change `minScale` to `0`, metrics will have gaps during idle periods. Subscription and usage data is not lost — it remains in the database and is reported on the next collection cycle. However, the `tool_calls_by_name` counter resets to zero on restart.

### Alerts

Set up alerts for error rates:
```bash
gcloud monitoring policies create \
  --display-name="Lightspeed Agent Error Rate" \
  --condition-display-name="Error rate > 5%" \
  --condition-filter='resource.type="cloud_run_revision" AND metric.type="run.googleapis.com/request_count" AND metric.labels.response_code_class="5xx"' \
  --project=$GOOGLE_CLOUD_PROJECT
```

## Troubleshooting

### View Logs

```bash
gcloud run services logs read lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --limit=100
```

### Check Service Status

```bash
gcloud run services describe lightspeed-agent \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT
```

### Common Issues

1. **Secret access denied**: Ensure service account has `secretmanager.secretAccessor` role
2. **Container fails to start**: Check logs for missing environment variables
3. **Database connection timeout**: Ensure Cloud SQL connection is configured

### SSL Certificate Stuck in PROVISIONING

Google-managed SSL certificates require each domain's DNS A record to point to its load balancer's static IP before provisioning can complete. If a certificate remains in `PROVISIONING` state:

1. Verify the DNS A record is correctly configured:
   ```bash
   # Get the expected static IPs
   gcloud compute addresses describe ${LB_NAME:-lightspeed-lb}-agent-ip \
     --global --project=$GOOGLE_CLOUD_PROJECT --format='value(address)'
   gcloud compute addresses describe ${LB_NAME:-lightspeed-lb}-handler-ip \
     --global --project=$GOOGLE_CLOUD_PROJECT --format='value(address)'

   # Check what the domains resolve to
   dig +short $AGENT_DOMAIN_NAME
   dig +short $HANDLER_DOMAIN_NAME
   ```

2. Ensure each domain resolves to its respective static IP. If not, update your DNS provider.

3. Check the certificate status and domain status:
   ```bash
   # Agent certificate
   gcloud compute ssl-certificates describe ${LB_NAME:-lightspeed-lb}-agent-cert \
     --global --project=$GOOGLE_CLOUD_PROJECT \
     --format='yaml(managed.status,managed.domainStatus)'

   # Handler certificate
   gcloud compute ssl-certificates describe ${LB_NAME:-lightspeed-lb}-handler-cert \
     --global --project=$GOOGLE_CLOUD_PROJECT \
     --format='yaml(managed.status,managed.domainStatus)'
   ```

4. Wait up to 60 minutes after DNS is correctly configured. Certificate provisioning is handled by Google and cannot be expedited.

### 502 Errors After Enabling GCLB

Backend services may take a few minutes to become healthy after a GCLB is first created. If you see 502 errors:

1. Verify the Cloud Run service is healthy:
   ```bash
   gcloud run services describe ${SERVICE_NAME:-lightspeed-agent} \
     --region=$GOOGLE_CLOUD_LOCATION --project=$GOOGLE_CLOUD_PROJECT \
     --format='value(status.conditions.status)'
   ```

2. Check that the serverless NEG is correctly configured:
   ```bash
   # Agent NEG
   gcloud compute network-endpoint-groups describe ${LB_NAME:-lightspeed-lb}-agent-neg \
     --region=$GOOGLE_CLOUD_LOCATION --project=$GOOGLE_CLOUD_PROJECT

   # Handler NEG
   gcloud compute network-endpoint-groups describe ${LB_NAME:-lightspeed-lb}-handler-neg \
     --region=$GOOGLE_CLOUD_LOCATION --project=$GOOGLE_CLOUD_PROJECT
   ```

3. Wait 2–3 minutes for the backend services to register as healthy, then retry.

### DCR Requests Not Reaching Marketplace Handler

If DCR requests from Gemini Enterprise fail with no logs on the marketplace
handler (but Pub/Sub events work fine), the most likely cause is a Cloud Run
ingress restriction without a corresponding GCLB.

**Background:** When Cloud Run ingress is set to `internal-and-cloud-load-balancing`,
only internal Google Cloud traffic (e.g., Pub/Sub push) and traffic through a
Google Cloud Load Balancer (GCLB) are allowed. Direct external traffic — including
DCR requests from Gemini Enterprise — is blocked at the Cloud Run ingress level
before it reaches the application. This is why no logs appear.

**Diagnosis:**

```bash
# Check the handler's ingress setting
gcloud run services describe ${HANDLER_SERVICE_NAME:-marketplace-handler} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(metadata.annotations."run.googleapis.com/ingress")'
# If this shows "internal-and-cloud-load-balancing", external DCR traffic is blocked
# unless a GCLB is in front of the handler.

# Check if a GCLB exists for the handler
gcloud compute backend-services list \
  --project=$GOOGLE_CLOUD_PROJECT \
  --filter="name~handler" \
  --format='table(name, backends.group)'
# If empty, no GCLB is configured.

# Check what URL the AgentCard advertises for DCR
AGENT_URL=$(gcloud run services describe ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')
curl -s $AGENT_URL/.well-known/agent.json | \
  jq '.capabilities.extensions[] | select(.uri | contains("dcr"))'
# The target_url must point to the GCLB domain, NOT the Cloud Run URL.
```

**Fix — Add a GCLB to an existing deployment:**

This adds a Google Cloud Load Balancer in front of the marketplace handler
so external DCR traffic from Gemini Enterprise is routed through the GCLB
and accepted by Cloud Run.

```bash
# 1. Set GCLB environment variables
export ENABLE_LB_HANDLER=true
export HANDLER_DOMAIN_NAME="dcr.example.com"  # Replace with your handler domain

# Optional: also enable agent LB
# export ENABLE_LB_AGENT=true
# export AGENT_DOMAIN_NAME="agent.example.com"

# 2. Run setup.sh to create the static IP and SSL certificate
./deploy/cloudrun/setup.sh

# 3. Get the static IP for DNS configuration
gcloud compute addresses describe ${LB_NAME:-lightspeed-lb}-handler-ip \
  --global \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(address)'

# 4. Create a DNS A record in your DNS provider:
#    dcr.example.com.  A  <handler-static-ip>

# 5. Verify DNS propagation
dig +short $HANDLER_DOMAIN_NAME

# 6. Deploy with LB enabled
#    This creates the GCLB, updates the handler's ingress, and sets
#    MARKETPLACE_HANDLER_URL on the agent to point to the GCLB domain.
./deploy/cloudrun/deploy.sh --service all

# 7. Wait for SSL certificate provisioning (typically 15-60 minutes)
gcloud compute ssl-certificates describe ${LB_NAME:-lightspeed-lb}-handler-cert \
  --global \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(managed.status)'
# Must show ACTIVE before HTTPS traffic works through the GCLB.
```

**Verify the fix:**

```bash
# AgentCard should show the GCLB domain in the DCR target_url
curl -s $AGENT_URL/.well-known/agent.json | \
  jq '.capabilities.extensions[] | select(.uri | contains("dcr"))'
# Expected: "target_url": "https://dcr.example.com/dcr"

# Health check through the GCLB (once SSL cert is ACTIVE)
curl -s https://$HANDLER_DOMAIN_NAME/health
```

**Alternative — Update MARKETPLACE_HANDLER_URL without full redeployment:**

If the GCLB is already set up but the AgentCard still points to the Cloud Run
URL, update just the agent's environment variable:

```bash
gcloud run services update ${SERVICE_NAME:-lightspeed-agent} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="MARKETPLACE_HANDLER_URL=https://${HANDLER_DOMAIN_NAME}"
```

### DCR Requests Failing with GCLB

If DCR requests fail after enabling the handler load balancer, verify the handler LB resources are correctly created:

```bash
# Check the handler backend service
gcloud compute backend-services describe ${LB_NAME:-lightspeed-lb}-handler-backend \
  --global --project=$GOOGLE_CLOUD_PROJECT

# Check the handler NEG
gcloud compute network-endpoint-groups describe ${LB_NAME:-lightspeed-lb}-handler-neg \
  --region=$GOOGLE_CLOUD_LOCATION --project=$GOOGLE_CLOUD_PROJECT
```

If resources are missing, redeploy with `ENABLE_LB_HANDLER=true`:

```bash
ENABLE_LB_HANDLER=true HANDLER_DOMAIN_NAME=dcr.example.com ./deploy/cloudrun/deploy.sh --service handler
```

### Legitimate Requests Blocked by Cloud Armor

If Cloud Armor is blocking legitimate traffic, check which rule is triggering:

```bash
gcloud logging read 'resource.type="http_load_balancer" AND jsonPayload.enforcedSecurityPolicy.outcome="DENY"' \
  --project=$GOOGLE_CLOUD_PROJECT --limit=10 \
  --format='table(timestamp, jsonPayload.enforcedSecurityPolicy.matchedRule, httpRequest.requestUrl)'
```

To stop blocking while you investigate, switch the offending rule to **preview mode** (logs only, no enforcement). Use the per-service policy name:

```bash
# Example: put the SQL injection rule into preview mode on the agent policy
gcloud compute security-policies rules update 1000 \
  --security-policy="${LB_NAME:-lightspeed-lb}-agent-security-policy" \
  --action=deny-403 \
  --preview \
  --global --project=$GOOGLE_CLOUD_PROJECT
```

To re-enable enforcement, run the same command without `--preview`:

```bash
gcloud compute security-policies rules update 1000 \
  --security-policy="${LB_NAME:-lightspeed-lb}-agent-security-policy" \
  --action=deny-403 \
  --no-preview \
  --global --project=$GOOGLE_CLOUD_PROJECT
```

### Orders Stuck in Pending Status

If marketplace subscriptions remain in `pending` status in the Google Cloud
console, check the handler logs for one of these messages:

**"SERVICE_CONTROL_SERVICE_NAME not set, skipping approval"** — The handler
is not configured with the managed service name. Set it on the handler:

```bash
gcloud run services update ${HANDLER_SERVICE_NAME:-marketplace-handler} \
  --region=$GOOGLE_CLOUD_LOCATION \
  --project=$GOOGLE_CLOUD_PROJECT \
  --update-env-vars="SERVICE_CONTROL_SERVICE_NAME=<your-service-name>.endpoints.<project-id>.cloud.goog"
```

You can find your managed service name via:

```bash
gcloud endpoints services list --project=$GOOGLE_CLOUD_PROJECT
```

**No events arriving at all** — The Pub/Sub
subscription is likely pointing to the wrong topic. This happens when
`PUBSUB_TOPIC` was not set to the fully-qualified marketplace topic before
deploying. See [Set Environment Variables](#1-set-environment-variables).

To verify and fix:

```bash
# Check which topic the subscription points to
gcloud pubsub subscriptions describe ${PUBSUB_SUBSCRIPTION:-marketplace-entitlements-sub} \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='yaml(topic, pushConfig.pushEndpoint)'
```

If the topic is wrong, the subscription must be deleted and recreated (the
topic cannot be changed on an existing subscription). The Pub/Sub Invoker SA
(linked in the Marketplace Producer Portal) must be used to create the
subscription because it holds the cross-project permissions on the
marketplace topic:

```bash
export PUBSUB_TOPIC="projects/<marketplace-project>/topics/<your-marketplace-topic>"
export PUBSUB_SUBSCRIPTION="marketplace-events-sub"
export PUBSUB_INVOKER_SA="${PUBSUB_INVOKER_NAME:-pubsub-invoker}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"

# Delete the old subscription
gcloud pubsub subscriptions delete marketplace-entitlements-sub \
  --project=$GOOGLE_CLOUD_PROJECT --quiet

# Ensure the invoker SA has roles/pubsub.editor in your project
# (setup.sh grants this automatically for new deployments)
gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
  --member="serviceAccount:$PUBSUB_INVOKER_SA" \
  --role="roles/pubsub.editor" --quiet

# Ensure you can impersonate the invoker SA
gcloud iam service-accounts add-iam-policy-binding "$PUBSUB_INVOKER_SA" \
  --member="user:$(gcloud config get account)" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project=$GOOGLE_CLOUD_PROJECT --quiet

# Wait a couple of minutes for IAM propagation, then create the subscription
HANDLER_URL=$(gcloud run services describe ${HANDLER_SERVICE_NAME:-marketplace-handler} \
  --region=${GOOGLE_CLOUD_LOCATION:-us-central1} \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='value(status.url)')

gcloud pubsub subscriptions create "$PUBSUB_SUBSCRIPTION" \
  --topic="$PUBSUB_TOPIC" \
  --push-endpoint="${HANDLER_URL}/pubsub" \
  --push-auth-service-account="$PUBSUB_INVOKER_SA" \
  --push-auth-token-audience="${HANDLER_URL}/pubsub" \
  --ack-deadline=60 \
  --project=$GOOGLE_CLOUD_PROJECT \
  --impersonate-service-account="$PUBSUB_INVOKER_SA"
```

Verify the fix:

```bash
gcloud pubsub subscriptions describe "$PUBSUB_SUBSCRIPTION" \
  --project=$GOOGLE_CLOUD_PROJECT \
  --format='yaml(topic, pushConfig.pushEndpoint)'
```

## Cleanup / Teardown

To remove all resources created by the setup and deploy scripts:

```bash
./deploy/cloudrun/cleanup.sh
```

This will delete:
- Cloud Run services (lightspeed-agent, marketplace-handler)
- Pub/Sub topic and subscription
- Secret Manager secrets
- Service accounts (runtime + Pub/Sub invoker) and IAM bindings
- Agent LB resources (when `ENABLE_LB_AGENT=true`): forwarding rule, HTTPS proxy, SSL certificate, URL map, backend service, NEG, and static IP
- Handler LB resources (when `ENABLE_LB_HANDLER=true`): forwarding rule, HTTPS proxy, SSL certificate, URL map, backend service, NEG, and static IP
- Cloud Armor security policies and WAF rules (defensively removed regardless of the `ENABLE_CLOUD_ARMOR_*` flag, ensuring no orphaned policies remain)

Use `--force` to skip the confirmation prompt:

```bash
./deploy/cloudrun/cleanup.sh --force
```

**Note**: The cleanup script does NOT delete container images in GCR or Cloud SQL instances. Delete these separately if needed:

```bash
# Delete container images
gcloud container images delete gcr.io/$GOOGLE_CLOUD_PROJECT/lightspeed-agent --force-delete-tags --quiet
gcloud container images delete gcr.io/$GOOGLE_CLOUD_PROJECT/red-hat-lightspeed-mcp --force-delete-tags --quiet

# Delete Cloud SQL instance (if created)
gcloud sql instances delete INSTANCE_NAME --project=$GOOGLE_CLOUD_PROJECT
```
