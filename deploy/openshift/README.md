# Red Hat Lightspeed Agent - OpenShift Deployment (Helm)

This guide covers deploying the Red Hat Lightspeed Agent on OpenShift using Helm.

Unlike the Cloud Run and Podman deployments, the OpenShift deployment does **not**
include the Google Cloud Marketplace handler. Order-id validation is skipped
(`SKIP_ORDER_VALIDATION=true`), while JWT token introspection against Red Hat SSO
is still enforced.

## Architecture

```
                        ┌─────────────────────────────────┐
                        │         OpenShift Route          │
                        │    (TLS edge termination)        │
                        └──────────────┬──────────────────┘
                                       │
                        ┌──────────────▼──────────────────┐
                        │      lightspeed-agent (Pod)      │
                        │                                  │
                        │  ┌─────────────────────────┐    │
                        │  │   lightspeed-agent       │    │
                        │  │   (port 8000)            │──────────▶ console.redhat.com
                        │  │   A2A / JSON-RPC 2.0     │    │       (via MCP)
                        │  │   OAuth 2.0 (Red Hat SSO)│    │
                        │  └────────┬────────────────┘    │
                        │           │ localhost:8081       │
                        │  ┌────────▼────────────────┐    │
                        │  │   lightspeed-mcp         │    │
                        │  │   (sidecar)              │    │
                        │  │   Red Hat Lightspeed MCP  │    │
                        │  └──────────────────────────┘    │
                        └──────────────────────────────────┘
                             │                    │
              ┌──────────────▼───┐    ┌──────────▼──────────┐
              │  PostgreSQL      │    │  Redis               │
              │  (sessions)      │    │  (rate limiting)     │
              │  Port 5432       │    │  Port 6379           │
              └──────────────────┘    └─────────────────────┘
```

## Components

| Component | Description |
|---|---|
| **lightspeed-agent** | Main A2A agent (Gemini + Google ADK) |
| **lightspeed-mcp** | Red Hat Lightspeed MCP server (sidecar in agent pod) providing tools for console.redhat.com APIs |
| **postgresql** | PostgreSQL 16 for ADK session persistence |
| **redis** | Redis 7 for distributed rate limiting |

## Prerequisites

- OpenShift 4.x cluster with `oc` and `helm` CLIs configured
- Access to pull container images from:
  - `quay.io/ecosystem-appeng/lightspeed-agent` (or your own registry)
  - `quay.io/redhat-services-prod/insights-management-tenant/insights-mcp/red-hat-lightspeed-mcp`
  - `registry.redhat.io/rhel9/postgresql-16`
  - `quay.io/fedora/redis-7`
- A Google AI Studio API key or Vertex AI project
- Red Hat SSO OAuth credentials (client ID and secret)

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

Since there is no marketplace handler in this deployment, order-id validation
is disabled (`SKIP_ORDER_VALIDATION=true`). The agent does not need a marketplace
database or DCR client registrations.

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
