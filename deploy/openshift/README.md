# Red Hat Lightspeed Agent - OpenShift Deployment (Helm)

This guide covers deploying the Red Hat Lightspeed Agent on OpenShift using Helm.

By default, the OpenShift deployment does **not** include the Google Cloud
Marketplace handler. Order-id validation is skipped
(`SKIP_ORDER_VALIDATION=true`), while JWT token introspection against Red Hat SSO
is still enforced.

For OpenShift clusters running **inside Google Cloud**, you can optionally enable
the marketplace handler to support the full Gemini Enterprise integration flow
(Pub/Sub events, Procurement API approvals, and DCR). See
[Marketplace Handler (optional)](#marketplace-handler-optional) below.

## Architecture

### Default (without marketplace handler)

```
                        +----------------------------------+
                        |         OpenShift Route           |
                        |    (TLS edge termination)         |
                        +---------------+------------------+
                                        |
                        +---------------v------------------+
                        |      lightspeed-agent (Pod)       |
                        |                                   |
                        |  +---------------------------+    |
                        |  |   lightspeed-agent        |    |
                        |  |   (port 8000)             |----------> console.redhat.com
                        |  |   A2A / JSON-RPC 2.0      |    |       (via MCP)
                        |  |   OAuth 2.0 (Red Hat SSO) |    |
                        |  +-----------+---------------+    |
                        |              | localhost:8081      |
                        |  +-----------v---------------+    |
                        |  |   lightspeed-mcp           |    |
                        |  |   (sidecar)                |    |
                        |  |   Red Hat Lightspeed MCP   |    |
                        |  +---------------------------+    |
                        +-----------------------------------+
                             |                    |
              +--------------+--+    +------------+----------+
              |  PostgreSQL     |    |  Redis                 |
              |  (sessions)     |    |  (rate limiting)       |
              |  Port 5432      |    |  Port 6379             |
              +-----------------+    +------------------------+
```

### With marketplace handler (`handler.enabled=true`)

```
  Google Cloud                                 OpenShift Cluster
  Marketplace                                  +---------------------------------+
  (Pub/Sub)                                    |                                 |
      |                                        |  +---------------------------+  |
      +------ push ----------------------------+->|  handler Route            |  |
                                               |  +------------+--------------+  |
                                               |               |                 |
  Gemini                                       |  +------------v--------------+  |
  Enterprise                                   |  |  marketplace-handler      |  |
      |                                        |  |  (port 8001)              |  |
      +------ DCR -----------------------------+->|  Pub/Sub + DCR endpoint   |  |
                                               |  +---+-----------+-----------+  |
                                               |      |           |              |
                                               |      v           v              |
                                               |  Procurement   Keycloak         |
                                               |  API (GCP)     (Red Hat SSO)    |
                                               |                                 |
                                               |  +---------------------------+  |
                                               |  |  agent Route              |  |
                                               |  +------------+--------------+  |
                                               |               |                 |
                                               |  +------------v--------------+  |
                                               |  |  lightspeed-agent (Pod)   |  |
                                               |  |  agent + MCP sidecar      |  |
                                               |  +---+-----------+-----------+  |
                                               |      |           |              |
                                               |      v           v              |
                                               |  PostgreSQL    Redis            |
                                               |  (shared)      (rate limiting)  |
                                               +---------------------------------+
```

## Components

| Component | Description |
|---|---|
| **lightspeed-agent** | Main A2A agent (Gemini + Google ADK) |
| **lightspeed-mcp** | Red Hat Lightspeed MCP server (sidecar in agent pod) providing tools for console.redhat.com APIs |
| **postgresql** | PostgreSQL 16 for ADK session persistence (and marketplace data when handler is enabled) |
| **redis** | Redis 7 for distributed rate limiting |
| **marketplace-handler** | *(optional)* Marketplace handler for Pub/Sub events and DCR from Gemini Enterprise |

## Prerequisites

- OpenShift 4.x cluster with `oc` and `helm` CLIs configured
- Access to pull container images from:
  - `quay.io/ecosystem-appeng/lightspeed-agent` (or your own registry)
  - `quay.io/redhat-services-prod/insights-management-tenant/insights-mcp/red-hat-lightspeed-mcp`
  - `registry.redhat.io/rhel9/postgresql-16`
  - `quay.io/fedora/redis-7`
- A Google AI Studio API key or Vertex AI project
- Red Hat SSO OAuth credentials (client ID and secret)

**Additional prerequisites when enabling the marketplace handler:**
- OpenShift cluster running inside Google Cloud (for Pub/Sub push delivery)
- A GCP service account with the following roles:
  - `roles/cloudcommerceprocurement.admin` (Procurement API access)
  - `roles/servicecontrol.reporter` (if Service Control is enabled)
- A Keycloak Initial Access Token for DCR
- A Fernet encryption key (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)

## Deployment Steps

### 1. Create a project (namespace)

```bash
oc new-project lightspeed-agent
```

### 2. Build and push the agent image

Build the container image from the repository root and push it to an accessible
registry:

```bash
podman build -t quay.io/<your-org>/lightspeed-agent:latest -f Containerfile .
podman push quay.io/<your-org>/lightspeed-agent:latest
```

If enabling the marketplace handler, also build its image:

```bash
podman build -t quay.io/<your-org>/lightspeed-agent-handler:latest -f Containerfile.marketplace-handler .
podman push quay.io/<your-org>/lightspeed-agent-handler:latest
```

If using a different registry or tag, set the image in `values.yaml` or pass it
as an override (see step 4).

### 3. Configure values

Copy `values.yaml` and edit it with your settings:

```bash
cp deploy/openshift/values.yaml deploy/openshift/my-values.yaml
```

At minimum, update the `secrets` section with real credentials:

```yaml
secrets:
  create: true
  googleApiKey: "your-real-api-key"
  redHatSsoClientId: "your-real-client-id"
  redHatSsoClientSecret: "your-real-client-secret"
  sessionDbPassword: "a-strong-password"
  sessionDatabaseUrl: "postgresql+asyncpg://sessions:a-strong-password@lightspeed-agent-postgresql:5432/agent_sessions"
```

> **Note**: If you prefer to manage the Secret externally (e.g., via Vault or
> sealed-secrets), set `secrets.create: false` and create a Secret named
> `<release>-lightspeed-agent-secrets` with the same keys.

### Key configurable values

| Value | Description | Default |
|---|---|---|
| `agent.image.repository` | Agent container image | `quay.io/ecosystem-appeng/lightspeed-agent` |
| `agent.image.tag` | Agent image tag | `latest` |
| `agent.replicas` | Number of agent replicas | `1` |
| `mcp.image.repository` | Lightspeed MCP server image | `quay.io/.../red-hat-lightspeed-mcp` |
| `google.geminiModel` | Gemini model name | `gemini-2.5-flash` |
| `google.useVertexAI` | Use Vertex AI instead of AI Studio | `false` |
| `postgresql.storage.size` | PostgreSQL PVC size | `1Gi` |
| `redis.storage.size` | Redis PVC size | `1Gi` |
| `route.enabled` | Create an OpenShift Route | `true` |
| `auth.skipOrderValidation` | Skip marketplace order checks | `true` |
| `handler.enabled` | Deploy the marketplace handler | `false` |
| `handler.serviceControlServiceName` | Marketplace product identifier | `""` |
| `serviceControl.enabled` | Enable Service Control usage reporting | `false` |

See `values.yaml` for the full list of configurable options.

### 4. Install the chart

```bash
helm install lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  -n lightspeed-agent
```

Or override individual values directly:

```bash
helm install lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  --set agent.image.repository=my-registry.example.com/lightspeed-agent \
  --set agent.image.tag=v1.0.0 \
  --set google.geminiModel=gemini-2.5-pro \
  --set postgresql.storage.size=5Gi \
  -n lightspeed-agent
```

### 5. Update the agent provider URL

After the Route is created, update `AGENT_PROVIDER_URL` to match the route
hostname:

```bash
ROUTE_HOST=$(oc get route lightspeed-agent -n lightspeed-agent -o jsonpath='{.spec.host}')
helm upgrade lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  --set agent.providerUrl=https://${ROUTE_HOST} \
  -n lightspeed-agent
```

### 6. Verify the deployment

```bash
# Check all pods are running
oc get pods -n lightspeed-agent

# Check the agent health endpoint
ROUTE_HOST=$(oc get route lightspeed-agent -n lightspeed-agent -o jsonpath='{.spec.host}')
curl -s https://${ROUTE_HOST}/health

# Check the agent card
curl -s https://${ROUTE_HOST}/.well-known/agent.json | python -m json.tool
```

## Authentication

The agent authenticates requests via Red Hat SSO token introspection:

1. Clients obtain a Bearer token from Red Hat SSO
2. The agent validates the token via the SSO introspection endpoint
3. The required scope (`agent:insights` by default) is checked

When there is no marketplace handler (`handler.enabled=false`), order-id
validation is disabled (`SKIP_ORDER_VALIDATION=true`). The agent does not need a
marketplace database or DCR client registrations.

When the handler is enabled, order-id validation should be turned on
(`auth.skipOrderValidation=false`) so the agent verifies that each request is
associated with an active marketplace entitlement.

## Marketplace Handler (optional)

For OpenShift clusters running **inside Google Cloud**, you can enable the
marketplace handler to support the full Google Cloud Marketplace integration:

- Receives Pub/Sub push events from Google Cloud Marketplace
- Approves accounts and entitlements via the Procurement API
- Handles Dynamic Client Registration (DCR) from Gemini Enterprise
- Stores entitlements in the shared PostgreSQL database

### 1. Create a GCP service account

Create a service account in your GCP project and grant it the required roles:

```bash
PROJECT_ID="your-gcp-project"
SA_NAME="lightspeed-handler"

gcloud iam service-accounts create $SA_NAME \
  --project=$PROJECT_ID \
  --display-name="Lightspeed Marketplace Handler"

# Grant Procurement API access
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/cloudcommerceprocurement.admin"

# Grant Service Control access (if enabling usage reporting)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/servicemanagement.serviceController"
```

### 2. Download and encode the service account key

```bash
gcloud iam service-accounts keys create sa-key.json \
  --iam-account="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Base64-encode for the Helm values
GCP_SA_KEY_B64=$(base64 -w0 sa-key.json)
```

### 3. Configure handler values

Add the following to your `my-values.yaml`:

```yaml
handler:
  enabled: true
  image:
    repository: quay.io/<your-org>/lightspeed-agent-handler
  serviceControlServiceName: "your-product.gcpmarketplace.example.com"

auth:
  skipOrderValidation: false

secrets:
  databaseUrl: "postgresql+asyncpg://sessions:a-strong-password@lightspeed-agent-postgresql:5432/agent_sessions"
  dcrInitialAccessToken: "your-keycloak-initial-access-token"
  dcrEncryptionKey: "your-fernet-encryption-key"
  gcpServiceAccountKey: "<base64-encoded-sa-key.json>"
```

### 4. Deploy or upgrade

```bash
helm upgrade --install lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  -n lightspeed-agent
```

### 5. Configure Pub/Sub push subscription

After the handler Route is created, configure a Pub/Sub push subscription to
send events to the handler:

```bash
HANDLER_HOST=$(oc get route lightspeed-agent-handler -n lightspeed-agent -o jsonpath='{.spec.host}')

gcloud pubsub subscriptions create marketplace-events-sub \
  --topic="$PUBSUB_TOPIC" \
  --push-endpoint="https://${HANDLER_HOST}/dcr" \
  --push-auth-service-account="$PUBSUB_INVOKER_SA" \
  --ack-deadline=60 \
  --project="$PROJECT_ID"
```

> **Note**: The handler Route must be reachable from Google Cloud Pub/Sub. For
> OpenShift clusters on GCP, this is typically the case as long as the Route has
> a public hostname. The Pub/Sub push subscription authenticates itself with an
> OIDC token signed by the push auth service account.

### Handler configuration values

| Value | Description | Default |
|---|---|---|
| `handler.enabled` | Enable the marketplace handler | `false` |
| `handler.image.repository` | Handler container image | `quay.io/ecosystem-appeng/lightspeed-agent-handler` |
| `handler.image.tag` | Handler image tag | `latest` |
| `handler.replicas` | Number of handler replicas | `1` |
| `handler.port` | Handler listen port | `8001` |
| `handler.serviceControlServiceName` | Marketplace product identifier | `""` |
| `handler.dcr.enabled` | Enable DCR with Keycloak | `true` |
| `handler.dcr.clientNamePrefix` | Prefix for created OAuth client names | `gemini-order-` |
| `handler.route.enabled` | Create a Route for the handler | `true` |
| `serviceControl.enabled` | Enable Service Control usage reporting | `false` |
| `secrets.dcrInitialAccessToken` | Keycloak Initial Access Token | `""` |
| `secrets.dcrEncryptionKey` | Fernet key for encrypting client secrets | `""` |
| `secrets.databaseUrl` | Marketplace database URL (shared PostgreSQL) | *(see values.yaml)* |
| `secrets.gcpServiceAccountKey` | Base64-encoded GCP SA key JSON | `""` |

## Scaling

To scale the agent horizontally:

```bash
oc scale deployment/lightspeed-agent --replicas=3 -n lightspeed-agent
```

Rate limiting state is shared across replicas through Redis.

For automatic scaling, create a HorizontalPodAutoscaler:

```bash
oc autoscale deployment/lightspeed-agent --min=1 --max=5 --cpu-percent=80 -n lightspeed-agent
```

> **Note**: The marketplace handler should typically run with a single replica
> to avoid processing duplicate Pub/Sub events.

## Upgrading

```bash
helm upgrade lightspeed-agent deploy/openshift/ \
  -f deploy/openshift/my-values.yaml \
  -n lightspeed-agent
```

## Troubleshooting

### View logs

```bash
# Agent logs
oc logs deployment/lightspeed-agent -c lightspeed-agent -n lightspeed-agent

# Lightspeed MCP server logs
oc logs deployment/lightspeed-agent -c lightspeed-mcp -n lightspeed-agent

# Marketplace handler logs (if enabled)
oc logs deployment/lightspeed-agent-handler -n lightspeed-agent

# PostgreSQL logs
oc logs deployment/lightspeed-agent-postgresql -n lightspeed-agent

# Redis logs
oc logs deployment/lightspeed-agent-redis -n lightspeed-agent
```

### Common issues

**Pod stuck in `ImagePullBackOff`**: Verify the image registry is accessible and
credentials are configured if pulling from a private registry:

```bash
oc create secret docker-registry my-registry-secret \
  --docker-server=quay.io \
  --docker-username=<user> \
  --docker-password=<password> \
  -n lightspeed-agent
oc secrets link default my-registry-secret --for=pull -n lightspeed-agent
```

**Agent cannot connect to PostgreSQL**: Verify the PostgreSQL pod is running and
the `SESSION_DATABASE_URL` in the secret matches the service name and port.

**Agent cannot connect to Redis**: Verify the Redis pod is running and the
`RATE_LIMIT_REDIS_URL` in the ConfigMap points to the correct Redis service.

**Health check failing**: Check agent logs for startup errors. Common causes
include missing secrets or unreachable database/Redis services.

**Handler cannot reach Procurement API**: Verify the GCP service account key is
correctly base64-encoded in `secrets.gcpServiceAccountKey` and that the service
account has the required IAM roles.

**Pub/Sub events not arriving**: Verify the handler Route is externally
accessible and the Pub/Sub push subscription is configured with the correct
endpoint URL (`https://<handler-route-host>/dcr`).

## Cleanup

Uninstall the Helm release to remove all deployed resources:

```bash
helm uninstall lightspeed-agent -n lightspeed-agent
```

PersistentVolumeClaims are not deleted by `helm uninstall`. Remove them manually
if needed:

```bash
oc delete pvc -l app.kubernetes.io/part-of=lightspeed-agent -n lightspeed-agent
```

Or delete the entire project:

```bash
oc delete project lightspeed-agent
```
