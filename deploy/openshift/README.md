# Red Hat Lightspeed Agent — OpenShift Deployment (Helm)

Deploy the Red Hat Lightspeed Agent on OpenShift using Helm. Two deployment
modes are supported:

| Mode | `deploymentMode` | What runs on OCP | What stays on GCP |
|------|-------------------|------------------|-------------------|
| **Hybrid** (default) | `hybrid` | Agent, Redis | Marketplace handler (Cloud Run) |
| **Standalone** | `standalone` | Everything — agent, handler, UI, PostgreSQL, Redis | Only the Gemini API |

## Architecture

### Hybrid mode

```
  Google Cloud                         OpenShift Cluster
  +-----------------+                  +-------------------------------+
  |  Cloud Run      |                  |     OpenShift Route           |
  |  marketplace    |                  |     (TLS edge termination)    |
  |  handler        |                  +-------------+-----------------+
  |  (port 8001)    |                                |
  +-----------------+                  +-------------v-----------------+
                                       |   lightspeed-agent (Pod)      |
                                       |   agent (8000) + MCP (8081)   |
                                       +---+--------------------------+
                                           |
                                 +----------+--------+
                                 |  Redis            |
                                 |  (rate limiting)  |
                                 |  Port 6379        |
                                 +-------------------+
```

In hybrid mode the handler is **not deployed** on OCP and sessions use in-memory
storage by default. Order validation is skipped (`auth.skipOrderValidation: true`)
since there is no local marketplace database. JWT introspection against Red Hat
SSO is still enforced.

### Standalone mode

```
  OpenShift Cluster
  +---------------------------------------------------------------------+
  |                                                                     |
  |  +------------------+  +------------------+  +-----------+          |
  |  |  Agent Route     |  |  Handler Route   |  |  UI Route |          |
  |  +--------+---------+  +--------+---------+  +-----+-----+          |
  |           |                      |                  |                |
  |  +--------v---------+  +--------v---------+  +-----v-----+          |
  |  | lightspeed-agent |  | marketplace      |  | standalone|          |
  |  | agent + MCP      |  | handler          |  | UI        |          |
  |  | (port 8000)      |  | (port 8001)      |  | (8080)    |          |
  |  +--------+---------+  +----+--------+----+  +-----------+          |
  |           |                  |        |                              |
  |  +--------v---------+  +----v--------v----+  +-----------+          |
  |  |  Marketplace     |  |  Session         |  |   Redis   |          |
  |  |  PostgreSQL      |  |  PostgreSQL      |  |   (6379)  |          |
  |  |  (marketplace)   |  |  (sessions,      |  +-----------+          |
  |  |  Port 5432       |  |   optional)      |                         |
  |  +------------------+  +------------------+                         |
  +---------------------------------------------------------------------+
```

Everything runs on OCP. Sessions use in-memory storage by default; set
`postgresql.sessionBackend: database` to persist sessions across pod restarts
(deploys a separate session PostgreSQL instance). The standalone UI provides
a web interface to:
1. **Discover the agent** and register an OAuth client (DCR)
2. **Get an access token** from Red Hat SSO (client credentials grant)
3. **Send A2A messages** to the agent with the token

## Components

| Component | Description | Deployed in |
|---|---|---|
| **lightspeed-agent** | A2A agent (Gemini + ADK) with MCP sidecar | Both modes |
| **postgresql** | PostgreSQL 16 for session persistence | Both (only when `sessionBackend: database`) |
| **marketplace-postgresql** | PostgreSQL 16 for marketplace/entitlement data | Standalone only |
| **redis** | Redis 7 for distributed rate limiting | Both modes |
| **marketplace-handler** | DCR and marketplace event handler | Standalone only |
| **standalone-ui** | Web UI for DCR + A2A testing | Standalone only |

## Prerequisites

### CLI tools

Install `oc` and `helm` if not already available:

```bash
# OpenShift CLI — download from your cluster's web console (? > Command Line Tools)
# or from https://mirror.openshift.com/pub/openshift-v4/clients/ocp/

# Helm CLI
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

### Requirements

**Both modes:**
- OpenShift 4.x cluster with `oc` and `helm` CLIs
- Container image access: `quay.io/ecosystem-appeng/google-lightspeed-agent`,
  `quay.io/redhat-services-prod/.../red-hat-lightspeed-mcp`,
  `quay.io/fedora/redis-7`, `registry.redhat.io/rhel9/postgresql-16` (if using database session backend or standalone mode)
- Google AI Studio API key, Vertex AI project, or GCP service account key (for ADC)
- Red Hat SSO OAuth credentials (client ID and secret)

**Standalone mode (additional):**
- Container images for handler and UI:
  `quay.io/ecosystem-appeng/google-marketplace-handler`,
  `quay.io/ecosystem-appeng/google-lightspeed-agent-ui`
- A Fernet encryption key for DCR
  (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
- A GCP service account key (if Service Control is enabled)

## Deployment — Hybrid Mode

### 1. Create a project

```bash
oc new-project lightspeed-agent
```

### 2. Configure image pull secrets (if needed)

The agent and UI images on `quay.io/ecosystem-appeng/` are public — no pull
secret is required for them. However, the Red Hat registry images (PostgreSQL,
MCP sidecar) may need authentication if your cluster doesn't already have a
global pull secret for `registry.redhat.io`:

```bash
oc create secret docker-registry redhat-pull-secret \
  --docker-server=registry.redhat.io \
  --docker-username=<user> \
  --docker-password=<password> \
  -n lightspeed-agent
```

Then add the secret name to your values file:

```yaml
imagePullSecrets:
  - name: redhat-pull-secret
```

> **Tip**: Most OpenShift clusters have a global pull secret for
> `registry.redhat.io` configured at install time — check with your cluster
> admin before creating one. If using private image registries, add their
> pull secrets to the same list.

### 3. Select an image tag

The default images in `values.yaml` point to the official repositories. Browse
the available tags and choose the one matching your desired version or commit:

- **Agent**: `quay.io/ecosystem-appeng/google-lightspeed-agent`
- **MCP sidecar**: `quay.io/redhat-services-prod/.../red-hat-lightspeed-mcp`

Set the tag in your values file:

```yaml
agent:
  image:
    tag: "<commit-sha-or-version>"
```

> **Tip — local testing**: You can also build and push images to your own
> registry for development purposes:
>
> ```bash
> podman build -t quay.io/<your-org>/lightspeed-agent:latest -f Containerfile .
> podman push quay.io/<your-org>/lightspeed-agent:latest
> ```
>
> Then override the repository in your values file:
> `agent.image.repository: quay.io/<your-org>/lightspeed-agent`

### 4. Configure values

```bash
cp deploy/openshift/values.yaml deploy/openshift/my-values.yaml
cp deploy/openshift/secrets.yaml.example deploy/openshift/secrets.yaml
```

Edit `my-values.yaml` (non-secret configuration only):

```yaml
deploymentMode: hybrid   # default

auth:
  skipOrderValidation: true
```

Edit `secrets.yaml` (credentials — git-ignored to prevent accidental commits):

```yaml
secrets:
  create: true
  googleApiKey: "your-real-api-key"
  redHatSsoClientId: "your-real-client-id"
  redHatSsoClientSecret: "your-real-client-secret"
  redisPassword: "a-strong-redis-password"
```

> **Vertex AI with service account**: As an alternative to `googleApiKey`, you
> can authenticate with a GCP service account key for Vertex AI (including
> LiteLLM with `vertex_ai/*` models):
>
> ```yaml
> secrets:
>   googleCloudProject: "your-gcp-project-id"
>   gcpServiceAccountKey: |
>     { ... service account JSON ... }
> ```

No database, handler, or UI configuration is needed — sessions use in-memory
storage by default, and the handler and standalone UI are not deployed in hybrid
mode. Order validation is skipped (`skipOrderValidation: true`) since there is
no local marketplace database. JWT introspection against Red Hat SSO is still
enforced.

> **Persistent sessions**: To persist sessions across pod restarts, set
> `postgresql.sessionBackend: database` and provide `secrets.sessionDbPassword`
> and `secrets.sessionDatabaseUrl` in `secrets.yaml`. This deploys a session
> PostgreSQL instance.

### 5. Install

```bash
helm install lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  -f deploy/openshift/secrets.yaml \
  -n lightspeed-agent
```

### 6. Update the provider URL in values file

After the Route is created, get the hostname and persist it in your values file
so it survives future upgrades and pod restarts:

```bash
AGENT_HOST=$(oc get route lightspeed-agent -n lightspeed-agent -o jsonpath='{.spec.host}')
echo "Agent Route: https://${AGENT_HOST}"
```

Add the URL to `my-values.yaml`:

```yaml
agent:
  providerUrl: "https://<AGENT_HOST>"   # paste the actual hostname
```

Then apply:

```bash
helm upgrade lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  -f deploy/openshift/secrets.yaml \
  -n lightspeed-agent
```

### 7. Verify

```bash
oc get pods -n lightspeed-agent

# Health check via internal probe port
AGENT_POD=$(oc get pod -n lightspeed-agent -l app.kubernetes.io/component=agent -o jsonpath='{.items[0].metadata.name}')
oc exec -n lightspeed-agent "${AGENT_POD}" -c agent -- curl -s http://localhost:8002/health

# Agent card via route
AGENT_HOST=$(oc get route lightspeed-agent -n lightspeed-agent -o jsonpath='{.spec.host}')
curl -sk https://${AGENT_HOST}/.well-known/agent.json | python -m json.tool
```

## Deployment — Standalone Mode

### 1. Create a project

```bash
oc new-project lightspeed-agent
```

### 2. Configure image pull secrets

Follow the same pull secret setup as in [hybrid mode step 2](#2-configure-image-pull-secrets).

### 3. Select image tags

The default images in `values.yaml` point to the official repositories. Browse
the available tags and choose the ones matching your desired version or commit:

- **Agent**: `quay.io/ecosystem-appeng/google-lightspeed-agent`
- **Handler**: `quay.io/ecosystem-appeng/google-marketplace-handler`
- **Standalone UI**: `quay.io/ecosystem-appeng/google-lightspeed-agent-ui`

Set the tags in your values file:

```yaml
agent:
  image:
    tag: "<commit-sha-or-version>"
handler:
  image:
    tag: "<commit-sha-or-version>"
standaloneUI:
  image:
    tag: "<commit-sha-or-version>"
```

> **Tip — local testing**: You can also build and push images to your own
> registry for development purposes:
>
> ```bash
> # Agent
> podman build -t quay.io/<your-org>/lightspeed-agent:latest -f Containerfile .
> podman push quay.io/<your-org>/lightspeed-agent:latest
>
> # Handler
> podman build -t quay.io/<your-org>/lightspeed-agent-handler:latest \
>   -f Containerfile.marketplace-handler .
> podman push quay.io/<your-org>/lightspeed-agent-handler:latest
>
> # Standalone UI
> podman build -t quay.io/<your-org>/lightspeed-agent-ui:latest \
>   -f deploy/openshift/standalone-ui/Containerfile deploy/openshift/standalone-ui/
> podman push quay.io/<your-org>/lightspeed-agent-ui:latest
> ```
>
> Then override the repositories in your values file (e.g.,
> `agent.image.repository: quay.io/<your-org>/lightspeed-agent`).

### 4. Configure values

```bash
cp deploy/openshift/values.yaml deploy/openshift/my-values.yaml
cp deploy/openshift/secrets.yaml.example deploy/openshift/secrets.yaml
```

Edit `my-values.yaml` (non-secret configuration only):

```yaml
deploymentMode: standalone

auth:
  # Disable order validation skip — standalone has a local marketplace DB
  skipOrderValidation: false
  # Accept self-signed DCR JWTs (not signed by Google's production SA)
  skipDcrJwtValidation: true
  # Skip Pub/Sub OIDC — standalone UI sends simulated events directly
  skipPubsubOidcVerification: true
```

Edit `secrets.yaml` (credentials — git-ignored to prevent accidental commits):

```yaml
secrets:
  create: true
  googleApiKey: "your-real-api-key"
  redHatSsoClientId: "your-real-client-id"
  redHatSsoClientSecret: "your-real-client-secret"
  # Redis authentication password
  redisPassword: "a-strong-redis-password"
  # Marketplace database (separate PostgreSQL instance, deployed automatically)
  marketplaceDbPassword: "a-strong-marketplace-password"
  databaseUrl: "postgresql+asyncpg://marketplace:a-strong-marketplace-password@lightspeed-agent-marketplace-postgresql:5432/marketplace"
  # DCR encryption key (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  dcrEncryptionKey: "your-fernet-key"
  # GMA API credentials — required for the handler to create SSO clients
  gmaClientId: "your-gma-client-id"
  gmaClientSecret: "your-gma-client-secret"
```

> **Note:** Setting `deploymentMode: standalone` automatically deploys the handler,
> standalone UI, and marketplace PostgreSQL — no additional flags are needed.
> Sessions use in-memory storage by default; set `postgresql.sessionBackend: database`
> and provide the session DB credentials in `secrets.yaml` to persist them
> (see hybrid mode note above).

### 5. Install

```bash
helm install lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  -f deploy/openshift/secrets.yaml \
  -n lightspeed-agent
```

### 6. Update the provider URL in values file

After the Route is created, get the agent hostname and persist it in your values
file so it survives future upgrades and pod restarts:

```bash
AGENT_HOST=$(oc get route lightspeed-agent -n lightspeed-agent -o jsonpath='{.spec.host}')
echo "Agent Route: https://${AGENT_HOST}"
```

Add the URL to `my-values.yaml`:

```yaml
agent:
  providerUrl: "https://<AGENT_HOST>"     # paste the actual hostname

auth:
  corsAllowedOrigins: "http://localhost:8080"  # for port-forward UI access
```

Then apply:

```bash
helm upgrade lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  -f deploy/openshift/secrets.yaml \
  -n lightspeed-agent
```

> **Security note:** Only the agent has a public OpenShift Route. The handler
> and standalone UI are ClusterIP-only services — they are not exposed outside
> the cluster. Use `oc port-forward` to access the UI from your local machine.

### 7. Access the UI via port-forward

The standalone UI is not publicly exposed. Use `oc port-forward` to access it:

```bash
oc port-forward svc/lightspeed-agent-ui 8080:8080 -n lightspeed-agent
```

Then open http://localhost:8080 in your browser.

### 8. Verify

```bash
oc get pods -n lightspeed-agent
# Expect: agent, handler, UI, marketplace-postgresql, redis pods

# Health checks via internal probe ports
AGENT_POD=$(oc get pod -n lightspeed-agent -l app.kubernetes.io/component=agent -o jsonpath='{.items[0].metadata.name}')
oc exec -n lightspeed-agent "${AGENT_POD}" -c agent -- curl -s http://localhost:8002/health

HANDLER_POD=$(oc get pod -n lightspeed-agent -l app.kubernetes.io/component=handler -o jsonpath='{.items[0].metadata.name}')
oc exec -n lightspeed-agent "${HANDLER_POD}" -- curl -s http://localhost:8003/health

# Agent card via route
AGENT_HOST=$(oc get route lightspeed-agent -n lightspeed-agent -o jsonpath='{.spec.host}')
curl -sk https://${AGENT_HOST}/.well-known/agent.json | python -m json.tool
```

### Understanding `skip_dcr_jwt_validation`

The `skipDcrJwtValidation` flag **only** skips JWT signature and issuer
verification on the DCR software_statement. It does **not** bypass account or
order validation — the handler still validates that the Procurement Account ID
and Order ID exist in the local marketplace database. This means:

- You must create an order (entitlement) before registering a client.
- The standalone UI automates this via the "Create Order" step.

### Using the Standalone UI

The interface guides you through the full order lifecycle:

1. **Create Order** — Simulates a Google Cloud Marketplace Pub/Sub event to
   create an entitlement in the local marketplace database. Enter an Account ID
   and Order ID (or use the defaults), then click **Create Order**. This must
   be done before DCR registration.

2. **Register Client (DCR)** — The agent card is fetched automatically. Click
   **Register Client** to create an OAuth client via the handler's `/dcr`
   endpoint using the Account ID and Order ID from step 1. The returned
   `client_id` and `client_secret` are displayed with copy buttons.

3. **Get Access Token** — The UI uses the authorization code flow:

   **a.** Copy the authorization URL displayed in Step A and open it in your
   browser. Log in with your Red Hat account.

   **b.** After login, SSO redirects to the registered redirect URI. The
   browser will show an error page — typically **"An error occurred during
   the OAuth exchange"**. **This is expected.** Look at the browser URL
   bar — it contains a `code` parameter. For example:

   ```
   https://vertexaisearch.cloud.google.com/oauth-redirect?code=abc123&session_state=...
   ```

   Copy just the code value (the part after `code=` and before the next
   `&`).

   **c.** Paste the code into the UI and click **Generate Token Exchange
   Command**. Copy the displayed `curl` command, run it in your terminal,
   and paste the resulting `access_token` (or the full JSON response) back
   into the UI.

4. **A2A Client** — The token from step 3 is pre-filled. Type a message and
   click **Send** to send a JSON-RPC 2.0 A2A request to the agent.

Use the **Reset** button to clear all state and start over (the entitlement and
DCR client are cleaned up from the database).

## Configuration Reference

### Deployment mode

| Value | Description | Default |
|---|---|---|
| `deploymentMode` | `hybrid` or `standalone` — controls which components are deployed and how auth behaves (see [Mode Comparison](#mode-comparison)) | `hybrid` |

### Image pull secrets

| Value | Description | Default |
|---|---|---|
| `imagePullSecrets` | List of `{name: "secret-name"}` entries for private registry authentication. Applied to all pods. | `[]` |

### Agent

| Value | Description | Default |
|---|---|---|
| `agent.image.repository` | Agent container image | `quay.io/ecosystem-appeng/google-lightspeed-agent` |
| `agent.image.tag` | Image tag | `latest` |
| `agent.image.pullPolicy` | Image pull policy | `Always` |
| `agent.replicas` | Replica count | `1` |
| `agent.name` | Internal agent name | `lightspeed_agent` |
| `agent.description` | Agent description | `Red Hat Lightspeed Agent` |
| `agent.host` | Agent listen address | `0.0.0.0` |
| `agent.port` | Agent listen port | `8000` |
| `agent.probePort` | Health/readiness probe port | `8002` |
| `agent.providerUrl` | Agent URL (set to Route hostname after install) | `https://lightspeed-agent.apps.example.com` |
| `agent.displayName` | Agent display name for the agent card | `Red Hat Lightspeed Agent for Google Cloud` |
| `agent.providerOrganizationUrl` | Organization URL for the agent card | `https://www.redhat.com` |
| `agent.loggingDetail` | Agent logging verbosity (`basic` / `detailed`) | `basic` |
| `agent.toolResultMaxChars` | Max characters in MCP tool results | `204800` |
| `agent.skillsDir` | Path to mount external ADK AI Skills (empty = bundled skills only) | `""` |
| `agent.skillsConfigMap` | Name of an existing ConfigMap containing external skill files | `""` |

### ADK AI Skills

The agent uses ADK AI Skills for modular behavioral instructions. Six bundled
skills ship inside the container image and load automatically:

- `tool-invocation-rules` — correct MCP tool invocation format
- `multi-step-workflows` — multi-step investigation patterns
- `pagination-handling` — paginated API result handling
- `error-handling` — error recovery and retry logic
- `guardrails-safety` — safety and scope guardrails
- `response-formatting` — output formatting rules

No configuration is needed for the default skills — they are always loaded.

**Custom skills**: To add or override skills, create a ConfigMap with your skill
files and configure the Helm values to mount it:

```bash
# Create a ConfigMap from a local skills directory
oc create configmap my-custom-skills \
  --from-file=my-skill/SKILL.md=./skills/my-skill/SKILL.md \
  -n lightspeed-agent
```

```yaml
agent:
  skillsDir: "/skills"
  skillsConfigMap: "my-custom-skills"
```

External skills with the same name as a bundled skill override the bundled
version.

### MCP Sidecar

The MCP server runs as a sidecar container in the agent pod.

| Value | Description | Default |
|---|---|---|
| `mcp.image.repository` | MCP server container image | `quay.io/redhat-services-prod/.../red-hat-lightspeed-mcp` |
| `mcp.image.tag` | Image tag | `latest` |
| `mcp.image.pullPolicy` | Image pull policy | `Always` |
| `mcp.transport` | Transport mode (`http` / `stdio` / `sse`) | `http` |
| `mcp.port` | MCP server port | `8081` |
| `mcp.host` | MCP server listen address | `0.0.0.0` |
| `mcp.readOnly` | Enable read-only mode (safe tool subset) | `true` |
| `mcp.baseUrl` | Red Hat console base URL | `"https://console.stage.redhat.com"` |
| `mcp.ssoBaseUrl` | Red Hat SSO base URL for MCP | `"https://sso.stage.redhat.com"` |
| `mcp.proxyUrl` | HTTP proxy for MCP (may be required for staging behind Akamai) | `""` |

### Google AI / Gemini

| Value | Description | Default |
|---|---|---|
| `google.geminiModel` | Default Gemini model (used when `llm.model` is not set) | `gemini-3.5-flash` |
| `google.useVertexAI` | Use Vertex AI instead of AI Studio | `false` |
| `google.cloudLocation` | Vertex AI region (use `global` for pay-as-you-go) | `global` |
| `secrets.gcpServiceAccountKey` | GCP service account key JSON for Vertex AI authentication via ADC (alternative to `secrets.googleApiKey`) | `""` |
| `google.httpRetry.attempts` | Max retry attempts for Gemini HTTP calls | `5` |
| `google.httpRetry.initialDelay` | Initial retry delay (seconds) | `1.0` |
| `google.httpRetry.maxDelay` | Max retry delay (seconds) | `60.0` |
| `google.httpRetry.expBase` | Exponential backoff base | `2.0` |
| `google.httpRetry.jitter` | Retry jitter (seconds) | `1.0` |

### LLM Provider (Alternative Models via LiteLLM)

| Value | Description | Default |
|---|---|---|
| `llm.provider` | `"gemini"` (default) or `"litellm"` for alternative providers | `"gemini"` |
| `llm.model` | Model name (works with both providers). For `litellm`, use `provider/model` format. For `gemini`, any `provider/` prefix is stripped automatically. Falls back to `google.geminiModel` when empty. | `""` |
| `llm.apiBase` | Custom API endpoint URL for self-hosted models | `""` |
| `secrets.llmApiKey` | API key for non-Google LLM providers | `""` |

To use a self-hosted model (e.g., vLLM or text-generation-inference on OpenShift AI), set:

```yaml
llm:
  provider: "litellm"
  model: "openai/your-model-name"
  apiBase: "https://your-model.apps.ocp.example.com/v1"

secrets:
  llmApiKey: "your-api-key"
```

The `openai/` prefix tells LiteLLM to use the OpenAI-compatible chat completions protocol, which is the standard API exposed by vLLM, text-generation-inference, and most model serving frameworks.

To use Vertex AI with service account authentication, set in `my-values.yaml`:

```yaml
llm:
  model: "vertex_ai/gemini-3.5-flash"

google:
  useVertexAI: true
  cloudLocation: global
```

And in `secrets.yaml`:

```yaml
secrets:
  googleCloudProject: "your-gcp-project-id"
  gcpServiceAccountKey: |
    { ... service account JSON ... }
```

The service account key is mounted into the agent container and `GOOGLE_APPLICATION_CREDENTIALS` is set automatically. The `llm.model` value works with both providers — for the `gemini` provider (default), the `vertex_ai/` prefix is stripped automatically; for the `litellm` provider, it's passed through to LiteLLM. You can switch `llm.provider` between `gemini` and `litellm` without changing `llm.model`.

> **Notes:**
> - Gemini HTTP retry settings (`google.httpRetry.*`) do not apply to `litellm` providers.
> - MCP tools work with all model providers.
> - See [Configuration — LLM Provider](../../docs/configuration.md#llm-provider) for all available settings.
> - **LiteLLM proxy chaining:** When `apiBase` points to another LiteLLM proxy
>   (e.g., a central LiteLLM gateway that routes to model backends), the model
>   name **must** include the `openai/` prefix (e.g., `openai/my-model`). Without
>   the prefix, LiteLLM does not recognize the upstream as an OpenAI-compatible
>   endpoint and the request fails. This is documented in the LiteLLM
>   [OpenAI-Compatible Endpoints](https://docs.litellm.ai/docs/providers/openai_compatible)
>   and [LiteLLM Proxy provider](https://docs.litellm.ai/docs/providers/litellm_proxy)
>   pages.

### Authentication

| Value | Description | Default |
|---|---|---|
| `auth.skipJwtValidation` | Skip JWT validation (dev only — blocked on Cloud Run) | `false` |
| `auth.skipOrderValidation` | Skip marketplace order-id checks | `true` |
| `auth.skipDcrJwtValidation` | Skip DCR software_statement JWT signature verification. Enable for deployments using self-signed JWTs. Does not affect agent auth. | `false` |
| `auth.skipPubsubOidcVerification` | Skip Google OIDC token verification on the handler's `/pubsub` endpoint. Enable for deployments where simulated Pub/Sub events come from the UI, not Google Cloud. Blocked on Cloud Run. | `false` |
| `auth.corsAllowedOrigins` | CORS allowed origins (comma-separated) | `""` |
| `sso.issuer` | Red Hat SSO issuer URL | `https://sso.stage.redhat.com/auth/realms/redhat-external` |
| `sso.requiredScope` | Required OAuth scopes (comma-separated) | `api.console,api.ocm` |
| `sso.allowedScopes` | Allowed OAuth scopes allowlist (comma-separated) | `openid,profile,email,api.console,api.ocm,metering:admin` |

### Session Database (PostgreSQL)

The session PostgreSQL instance is **only deployed when `postgresql.sessionBackend: database`**.
With the default `memory` backend, no session database is created.

| Value | Description | Default |
|---|---|---|
| `postgresql.image.repository` | PostgreSQL container image | `registry.redhat.io/rhel9/postgresql-16` |
| `postgresql.image.tag` | Image tag | `latest` |
| `postgresql.image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `postgresql.sessionBackend` | `database` (persist to PostgreSQL) or `memory` (lost on restart) | `memory` |
| `postgresql.poolSize` | Connection pool size | `5` |
| `postgresql.poolMaxOverflow` | Max pool overflow | `10` |
| `postgresql.requireSsl` | Require SSL/TLS for PostgreSQL connections | `false` |
| `postgresql.user` | Database user | `sessions` |
| `postgresql.database` | Database name | `agent_sessions` |
| `postgresql.storage.size` | PVC size | `1Gi` |

### Marketplace Database (standalone mode only)

A separate PostgreSQL instance for marketplace/entitlement data. Deployed only
when `deploymentMode: standalone`.

| Value | Description | Default |
|---|---|---|
| `marketplacePostgresql.image.repository` | PostgreSQL container image | `registry.redhat.io/rhel9/postgresql-16` |
| `marketplacePostgresql.image.tag` | Image tag | `latest` |
| `marketplacePostgresql.image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `marketplacePostgresql.user` | Database user | `marketplace` |
| `marketplacePostgresql.database` | Database name | `marketplace` |
| `marketplacePostgresql.storage.size` | PVC size | `1Gi` |

### Rate Limiting & Redis

| Value | Description | Default |
|---|---|---|
| `rateLimit.requestsPerMinute` | Per-IP requests/min | `60` |
| `rateLimit.requestsPerHour` | Per-IP requests/hour | `1000` |
| `rateLimit.redisTimeoutMs` | Redis operation timeout (ms) | `200` |
| `rateLimit.keyPrefix` | Redis key prefix for rate-limit counters | `lightspeed:ratelimit` |
| `rateLimit.redisCaCert` | Redis TLS CA certificate path | `""` |
| `redis.image.repository` | Redis container image | `quay.io/fedora/redis-7` |
| `redis.image.tag` | Image tag | `latest` |
| `redis.image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `redis.storage.size` | Redis PVC size | `1Gi` |

### Routes

| Value | Description | Default |
|---|---|---|
| `route.enabled` | Create an OpenShift Route for the agent | `true` |
| `route.tls.termination` | TLS termination type | `edge` |
| `route.tls.insecureEdgeTerminationPolicy` | Redirect HTTP to HTTPS | `Redirect` |

### Handler (standalone mode only)

Deployed only when `deploymentMode: standalone`.

| Value | Description | Default |
|---|---|---|
| `handler.image.repository` | Handler container image | `quay.io/ecosystem-appeng/google-marketplace-handler` |
| `handler.image.tag` | Image tag | `latest` |
| `handler.image.pullPolicy` | Image pull policy | `Always` |
| `handler.replicas` | Replica count (keep at 1 to avoid duplicate event processing) | `1` |
| `handler.host` | Handler listen address | `0.0.0.0` |
| `handler.port` | Handler listen port | `8001` |
| `handler.probePort` | Handler probe port | `8003` |
| `handler.route.enabled` | Create an OpenShift Route for the handler (disabled by default — handler is ClusterIP only, reached via UI nginx proxy) | `false` |
| `handler.pubsubAudience` | Expected audience in Pub/Sub OIDC tokens (set to handler URL for strict binding) | `""` |
| `handler.dcr.clientNamePrefix` | Prefix for created OAuth clients | `gemini-order-` |
| `handler.serviceControlServiceName` | Marketplace product identifier (from GCP Producer Portal) | `""` |
| `handler.metering.staleClaimMinutes` | Stale claim timeout | `15` |
| `handler.metering.backfillMaxAgeHours` | Max backfill age | `168` |
| `handler.metering.backfillLimitPerRun` | Max records per backfill run | `20` |
| `handler.gma.apiBaseUrl` | GMA SSO API base URL | `https://sso.stage.redhat.com/...` |
| `handler.gma.apiTimeout` | GMA API timeout (seconds) | `30` |

### Service Control

| Value | Description | Default |
|---|---|---|
| `serviceControl.enabled` | Enable Google Cloud Service Control usage reporting | `false` |

### Standalone UI (standalone mode only)

Deployed only when `deploymentMode: standalone`.

| Value | Description | Default |
|---|---|---|
| `standaloneUI.image.repository` | UI container image | `quay.io/ecosystem-appeng/google-lightspeed-agent-ui` |
| `standaloneUI.image.tag` | Image tag | `latest` |
| `standaloneUI.image.pullPolicy` | Image pull policy | `Always` |
| `standaloneUI.port` | UI listen port | `8080` |
| `standaloneUI.route.enabled` | Create a Route for the UI | `true` |

### Secrets

| Value | Description | Required in |
|---|---|---|
| `secrets.create` | Have the chart create the Secret from values below (`false` = manage externally) | Both |
| `secrets.googleApiKey` | Google AI API key | Both (unless Vertex AI) |
| `secrets.googleCloudProject` | GCP project ID | Both (Vertex AI only) |
| `secrets.redHatSsoClientId` | Red Hat SSO client ID | Both |
| `secrets.redHatSsoClientSecret` | Red Hat SSO client secret | Both |
| `secrets.sessionDbPassword` | Session PostgreSQL password | Only when `sessionBackend: database` |
| `secrets.sessionDatabaseUrl` | Session database connection URL | Only when `sessionBackend: database` |
| `secrets.redisPassword` | Redis authentication password | Both |
| `secrets.marketplaceDbPassword` | Marketplace PostgreSQL password | Standalone only |
| `secrets.databaseUrl` | Marketplace database connection URL | Standalone only |
| `secrets.dcrEncryptionKey` | Fernet key for encrypting stored DCR client secrets | Standalone only |
| `secrets.gmaClientId` | GMA API client ID (for DCR tenant creation) | Standalone only |
| `secrets.gmaClientSecret` | GMA API client secret | Standalone only |
| `secrets.llmApiKey` | API key for non-Google LLM providers (`litellm` only) | When using `litellm` provider |
| `secrets.gcpServiceAccountKey` | GCP service account key JSON for ADC (Vertex AI agent auth and/or standalone handler) | When using Vertex AI auth or standalone + GCP |

### Observability

| Value | Description | Default |
|---|---|---|
| `logging.level` | Log level | `INFO` |
| `logging.format` | Log format (`json` / `text`) | `json` |
| `otel.enabled` | Enable OpenTelemetry tracing | `false` |
| `otel.serviceName` | OTEL service name | `lightspeed_agent` |
| `otel.exporterType` | Exporter type (`otlp`, `otlp-http`, `console`, `jaeger`, `zipkin`) | `otlp` |
| `otel.otlpEndpoint` | OTLP gRPC endpoint | `http://localhost:4317` |
| `otel.otlpHttpEndpoint` | OTLP HTTP endpoint | `http://localhost:4318` |
| `otel.tracesSampler` | Traces sampler strategy | `always_on` |
| `otel.tracesSamplerArg` | Traces sampler argument | `1.0` |
| `otel.metricsEnabled` | Enable OTel metrics and Prometheus scrape endpoint | `false` |
| `otel.metricsPrometheusPort` | Port for Prometheus metrics scrape endpoint | `9464` |
| `otel.metricsCollectionInterval` | DB polling interval in seconds for metrics collection (min 10) | `60` |

### Monitoring

| Value | Description | Default |
|---|---|---|
| `monitoring.serviceMonitor.enabled` | Create a ServiceMonitor CR for Prometheus Operator | `false` |
| `monitoring.serviceMonitor.interval` | Prometheus scrape interval | `60s` |
| `monitoring.serviceMonitor.namespace` | Namespace for ServiceMonitor (empty = release namespace) | `""` |
| `monitoring.grafanaDashboard.enabled` | Create a GrafanaDashboard CR for Grafana Operator | `false` |
| `monitoring.grafanaDashboard.datasource` | Grafana Prometheus datasource UID | `prometheus` |
| `monitoring.grafanaDashboard.namespace` | Namespace for GrafanaDashboard (empty = release namespace) | `""` |

## Monitoring (Prometheus + Grafana)

The agent exposes OpenTelemetry metrics via a Prometheus scrape endpoint. The
Helm chart includes templates for Prometheus Operator (ServiceMonitor)
and Grafana Operator (GrafanaDashboard) to automate discovery and visualization.

> **Important:** Before enabling monitoring in `values.yaml`, verify that all
> prerequisites (operators, Grafana instance, datasource) are in place. The
> Helm chart creates ServiceMonitor and GrafanaDashboard CRs that will fail
> to reconcile if the corresponding operators are not installed.

### Prerequisites

The following must be configured on your OpenShift cluster before enabling
monitoring in the Helm chart.

#### 1. Prometheus Operator

Included with the built-in OpenShift Monitoring stack — enabled by default on
most clusters. This provides the ServiceMonitor CRD.

Verify it is running:

```bash
oc get pods -n openshift-monitoring -l app.kubernetes.io/name=prometheus-operator
```

By default, the built-in Prometheus only monitors OpenShift system namespaces.
To scrape ServiceMonitors in application namespaces (like `lightspeed-agent`),
**user workload monitoring** must be enabled by a cluster admin. Check with your
cluster admin or see the
[OpenShift documentation](https://docs.openshift.com/container-platform/latest/observability/monitoring/enabling-monitoring-for-user-defined-projects.html).

```bash
# Check if user workload monitoring is enabled
oc get configmap cluster-monitoring-config -n openshift-monitoring -o yaml | grep enableUserWorkload
```

#### 2. Grafana Operator

Install from OperatorHub:

1. In the OpenShift web console, go to **Operators > OperatorHub**
2. Search for **"Grafana Operator"** and click **Install**
3. Set the installation namespace to the agent namespace (e.g., `lightspeed-agent`)

Verify it is running:

```bash
oc get pods -n lightspeed-agent -l app.kubernetes.io/name=grafana-operator
```

#### 3. Grafana instance

Create a `Grafana` CR in the agent namespace. The `dashboards: grafana` label
is required so the operator picks up GrafanaDashboard CRs.

```yaml
apiVersion: grafana.integreatly.org/v1beta1
kind: Grafana
metadata:
  name: grafana
  namespace: lightspeed-agent
  labels:
    dashboards: grafana
spec:
  route:
    spec:
      tls:
        termination: edge
  config:
    log:
      mode: console
    auth.anonymous:
      enabled: "true"
      org_role: "Viewer"
```

Apply it:

```bash
oc apply -f grafana.yaml
```

Verify the Grafana pod is running and the Route is created:

```bash
oc get pods -n lightspeed-agent -l app=grafana
oc get route -n lightspeed-agent grafana-route
```

#### 4. Grafana datasource

Create a `GrafanaDatasource` CR in the agent namespace that connects Grafana
to the in-cluster Prometheus. The `uid` field must match
`monitoring.grafanaDashboard.datasource` in `values.yaml` (default: `prometheus`).

The datasource connects Grafana to the built-in OpenShift Prometheus (Thanos
Querier). This requires a service account with `cluster-monitoring-view`
permissions.

**a.** Create a service account for Grafana to authenticate with Prometheus:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: grafana-prometheus
  namespace: lightspeed-agent
```

```bash
oc apply -f grafana-sa.yaml
```

**b.** Bind the `cluster-monitoring-view` cluster role to the service account.
This requires cluster admin privileges — if you don't have admin access, ask
your cluster admin to run this command on your behalf:

```bash
oc adm policy add-cluster-role-to-user cluster-monitoring-view \
  -z grafana-prometheus -n lightspeed-agent
```

**c.** Generate a long-lived token for the datasource:

```bash
oc create token grafana-prometheus -n lightspeed-agent --duration=8760h
```

**d.** Create the datasource CR, replacing `<SERVICE_ACCOUNT_TOKEN>` with the
token from the previous step:

```yaml
apiVersion: grafana.integreatly.org/v1beta1
kind: GrafanaDatasource
metadata:
  name: prometheus
  namespace: lightspeed-agent
spec:
  instanceSelector:
    matchLabels:
      dashboards: grafana
  datasource:
    name: Prometheus
    type: prometheus
    uid: prometheus
    access: proxy
    url: https://thanos-querier.openshift-monitoring.svc.cluster.local:9091
    isDefault: true
    jsonData:
      httpHeaderName1: Authorization
      tlsSkipVerify: true
    secureJsonData:
      httpHeaderValue1: "Bearer <SERVICE_ACCOUNT_TOKEN>"
```

> **Note:** Replace `<SERVICE_ACCOUNT_TOKEN>` with the token generated in
> step c above.

Apply and verify:

```bash
oc apply -f grafana-datasource.yaml
oc get grafanadatasources -n lightspeed-agent
```

#### Prerequisites checklist

Before enabling monitoring in the Helm chart, confirm all of the following:

- [ ] Prometheus Operator is running (built-in or user workload monitoring enabled)
- [ ] ServiceMonitor CRD exists: `oc get crd servicemonitors.monitoring.coreos.com`
- [ ] Grafana Operator is installed in the agent namespace
- [ ] A `Grafana` instance exists in the agent namespace with `dashboards: grafana` label
- [ ] A `GrafanaDatasource` CR connects Grafana to Prometheus
- [ ] Note the datasource `uid` — you will need it for `monitoring.grafanaDashboard.datasource`

### Enable monitoring

Add the following to your `my-values.yaml`:

```yaml
otel:
  metricsEnabled: true
  metricsPrometheusPort: 9464       # default — Prometheus scrape port
  metricsCollectionInterval: 60     # DB polling interval (seconds)

monitoring:
  serviceMonitor:
    enabled: true
    interval: 60s                   # Prometheus scrape interval
  grafanaDashboard:
    enabled: true
    datasource: prometheus          # must match your GrafanaDatasource uid
```

Then upgrade your release:

```bash
helm upgrade lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  -f deploy/openshift/secrets.yaml \
  -n lightspeed-agent
```

### What gets created

When monitoring is enabled, the chart creates:

| Resource | Purpose |
|---|---|
| **ServiceMonitor** | Tells Prometheus Operator to scrape the agent's `/metrics` endpoint on port 9464 |
| **GrafanaDashboard** | Provisions a Grafana dashboard via Grafana Operator with panels for all agent metrics |
| **NetworkPolicy rule** | Allows ingress from the `openshift-monitoring` namespace to port 9464 |

The agent Deployment and Service also gain a `metrics` port (9464) when
`otel.metricsEnabled` is true.

### Exposed metrics

See [docs/telemetry.md — Metric Instruments](../../docs/telemetry.md#metric-instruments)
for the full list of metrics, their types, labels, and descriptions.

### Verify

```bash
# Verify the metrics endpoint responds
AGENT_POD=$(oc get pod -n lightspeed-agent -l app.kubernetes.io/component=agent -o jsonpath='{.items[0].metadata.name}')
oc exec -n lightspeed-agent "${AGENT_POD}" -c lightspeed-agent -- curl -s http://localhost:9464/metrics | head -20

# Open the Grafana UI
oc get route -n lightspeed-agent grafana-route
```

### Multiple agents (umbrella chart)

When deploying multiple agents from a parent Helm chart, each agent is a
sub-chart dependency with a unique alias. Each sub-chart instance gets its own
ServiceMonitor, GrafanaDashboard, and NetworkPolicy rule — all with unique
names derived from the Helm fullname.

To keep metrics distinct, set a unique `otel.serviceName` per agent in the
parent chart's `values.yaml`:

```yaml
# Parent chart values.yaml
insights-agent:
  otel:
    serviceName: insights_agent

compliance-agent:
  otel:
    serviceName: compliance_agent
```

Each agent gets its own Grafana dashboard (titled with the agent name) and
Prometheus scrape target. The `OTEL_SERVICE_NAME` label on all metrics ensures
they are distinguishable in PromQL queries.

> **Note:** In the parent chart, most metrics values can be set globally and
> passed down to all sub-charts (`otel.metricsEnabled`,
> `otel.metricsPrometheusPort`, `otel.metricsCollectionInterval`,
> `monitoring.serviceMonitor.*`, `monitoring.grafanaDashboard.*`). Only
> `otel.serviceName` must be set per agent since it must be unique.

## Authentication

The agent authenticates requests via Red Hat SSO token introspection:

1. Clients obtain a Bearer token from Red Hat SSO
2. The agent validates the token via the SSO introspection endpoint
3. The required scopes (`api.console,api.ocm` by default) are checked

**Hybrid mode** — order validation is skipped (`skipOrderValidation: true`).
Token introspection against Red Hat SSO is still enforced.

**Standalone mode** — the handler manages DCR clients and entitlements locally
in the shared PostgreSQL. Order validation can be enabled or skipped.

## Security

### Application-Level Protections (Platform-Agnostic)

The agent enforces several security controls at the application layer, regardless
of deployment platform:

| Protection | Implementation | Details |
|---|---|---|
| **Request body size limits** | `security/body_limit.py` | 10 MB agent, 1 MB marketplace handler |
| **Security headers** | `security/middleware.py` | HSTS, CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy, Cache-Control |
| **Rate limiting** | `ratelimit/middleware.py` | 60 req/min, 1000 req/hr per IP via Redis |
| **JWT authentication** | `auth/middleware.py` | Red Hat SSO token introspection (RFC 7662) |
| **CORS** | FastAPI middleware | Disabled by default; configurable via `auth.corsAllowedOrigins` |

These protections run inside the application process and apply identically on
Cloud Run and OpenShift.

### Platform-Level Protections

OpenShift and Cloud Run provide different infrastructure-level security:

| Capability | Cloud Run | OpenShift |
|---|---|---|
| **TLS termination** | GCLB with managed SSL certificates | OpenShift Routes with edge TLS termination |
| **WAF** | Cloud Armor (OWASP CRS, CVE canary rules, method enforcement) | No built-in equivalent — use a third-party WAF or API gateway if needed |
| **DDoS protection** | Cloud Armor + GCLB | Platform-level protections vary by cluster configuration |
| **Network isolation** | VPC + Cloud Run ingress restrictions | NetworkPolicies restrict pod-to-pod traffic (database, Redis) |
| **Service exposure** | Cloud Run ingress set to `internal-and-cloud-load-balancing` when GCLB is enabled | Only the agent has a public Route; handler and UI are ClusterIP-only |

> **Note:** If your OpenShift deployment requires WAF-level protection (e.g.,
> OWASP rule sets, bot mitigation), consider placing an external WAF or API
> gateway in front of the OpenShift Route. The application-level rate limiting
> and body size limits provide baseline protection without additional
> infrastructure.

## Using Red Hat Production Environment

The default configuration points at the Red Hat **staging** environment
(`sso.stage.redhat.com`, `console.stage.redhat.com`). To switch to production,
add these overrides to your `my-values.yaml`:

```yaml
# SSO issuer — production realm
sso:
  issuer: "https://sso.redhat.com/auth/realms/redhat-external"

# MCP sidecar — production console and SSO
mcp:
  baseUrl: "https://console.redhat.com"
  ssoBaseUrl: "https://sso.redhat.com"
  proxyUrl: ""

# Handler GMA API — production SSO (standalone mode only)
handler:
  gma:
    apiBaseUrl: "https://sso.redhat.com/auth/realms/redhat-external/apis/beta/acs/v1/"
```

## Scaling

```bash
oc scale deployment/lightspeed-agent --replicas=3 -n lightspeed-agent
```

Rate limiting state is shared across replicas through Redis. For automatic
scaling:

```bash
oc autoscale deployment/lightspeed-agent --min=1 --max=5 --cpu-percent=80 -n lightspeed-agent
```

> The marketplace handler should typically run with a single replica to avoid
> processing duplicate events.

## Upgrading

```bash
helm upgrade lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  -f deploy/openshift/secrets.yaml \
  -n lightspeed-agent
```

## Troubleshooting

### View logs

```bash
# Agent
oc logs deployment/lightspeed-agent -c lightspeed-agent -n lightspeed-agent
# MCP sidecar
oc logs deployment/lightspeed-agent -c lightspeed-mcp -n lightspeed-agent
# Handler (standalone mode)
oc logs deployment/lightspeed-agent-handler -n lightspeed-agent
# Standalone UI
oc logs deployment/lightspeed-agent-ui -n lightspeed-agent
# Session PostgreSQL (only when sessionBackend=database)
oc logs deployment/lightspeed-agent-postgresql -n lightspeed-agent
# Marketplace PostgreSQL (standalone mode)
oc logs deployment/lightspeed-agent-marketplace-postgresql -n lightspeed-agent
# Redis
oc logs deployment/lightspeed-agent-redis -n lightspeed-agent
```

### Common issues

**Pod stuck in `ImagePullBackOff`**: Verify image registry access. Ensure the
pull secrets exist in the namespace and are listed in `imagePullSecrets` in your
values file (see [step 2](#2-configure-image-pull-secrets)).

**Agent cannot connect to PostgreSQL**: Check the relevant PostgreSQL pod is
running and that `SESSION_DATABASE_URL` / `DATABASE_URL` in the secret matches
the correct service name (`lightspeed-agent-postgresql` for sessions,
`lightspeed-agent-marketplace-postgresql` for marketplace).

**Agent cannot connect to Redis**: Verify the Redis pod is running and
`RATE_LIMIT_REDIS_URL` in the ConfigMap points to the Redis service.

**Health check failing**: Check agent logs for startup errors (missing secrets,
unreachable database/Redis).

**Standalone UI shows CORS error on token exchange**: Red Hat SSO may not allow
CORS from the UI origin. Use the displayed `curl` command to exchange the
authorization code for a token manually and paste it into the UI.

## Cleanup

```bash
helm uninstall lightspeed-agent -n lightspeed-agent
```

PVCs are not deleted by `helm uninstall`:

```bash
oc delete pvc -l app.kubernetes.io/part-of=lightspeed-agent -n lightspeed-agent
```

Or delete the entire project:

```bash
oc delete project lightspeed-agent
```
