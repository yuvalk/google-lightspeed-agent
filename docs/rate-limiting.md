# Rate Limiting

This document describes the Redis-backed rate limiting system for controlling API usage.

## Overview

The rate limiting system enforces global usage limits using a Redis-backed sliding window algorithm:
- Requests per minute
- Requests per hour

## Architecture

```
┌─────────────────┐      ┌────────────────────┐      ┌──────────────────┐
│  API Request    │────▶│RateLimitMiddleware │────▶│ RedisRateLimiter │
│                 │      │                    │      │  (Redis + Lua)   │
└─────────────────┘      └────────────────────┘      └──────────────────┘
```

### Components

| Component | File | Description |
|-----------|------|-------------|
| `RateLimitMiddleware` | `ratelimit/middleware.py` | FastAPI middleware for enforcement |
| `RedisRateLimiter` | `ratelimit/middleware.py` | Redis sliding window limiter using Lua scripts |

## Configuration

### Environment Variables

```bash
# Redis backend (required)
RATE_LIMIT_REDIS_URL=redis://localhost:6379/0
RATE_LIMIT_REDIS_TIMEOUT_MS=200
RATE_LIMIT_KEY_PREFIX=lightspeed:ratelimit

# Global rate limits
RATE_LIMIT_REQUESTS_PER_MINUTE=60
RATE_LIMIT_REQUESTS_PER_HOUR=1000
```

## Rate-Limited Paths

Only specific paths are rate-limited:

| Path | Description |
|------|-------------|
| `/` | A2A JSON-RPC endpoint (supports both send and streaming) |

## Principal Dimensions

Rate limits are evaluated across multiple principal dimensions:

1. `order_id` (tenant/subscription boundary)
2. `user_id` (or `client_id` if `user_id` is unavailable)
3. IP fallback only when no authenticated principal is available

If both `order_id` and `user_id` are present, the request must pass both checks.
If either dimension exceeds the configured limit, the request is rejected with `429`.

### Rate-Limited Paths

**Agent service (port 8000):**

- `/` - A2A JSON-RPC endpoint (authenticated — keyed by order, user, or client)
- `/.well-known/agent.json` - AgentCard discovery (unauthenticated — keyed by IP)
- `/.well-known/agent-card.json` - AgentCard alias (unauthenticated — keyed by IP)

**Marketplace handler (port 8001):**

- `/dcr` - DCR and Pub/Sub endpoint (unauthenticated — keyed by IP)

Both services share the same Redis backend and the same per-IP limits.

### Skipped Paths

These paths are never rate-limited:

- `/health`, `/healthz`, `/ready` - Health checks (served on a separate probe port, never reach the rate limiting middleware)
- `/docs`, `/openapi.json`, `/redoc` - Documentation (disabled in production via `DEBUG=false`)

## Response Headers

When a request is rate-limited (429 response):

| Header | Description |
|--------|-------------|
| `Retry-After` | Seconds until the limit resets |
| `X-RateLimit-Limit` | The limit per minute |
| `X-RateLimit-Remaining` | Remaining requests |

Example response:
```http
HTTP/1.1 429 Too Many Requests
Retry-After: 60
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
Content-Type: application/json

{
  "error": "rate_limit_exceeded",
  "message": "Rate limit exceeded (per_minute)",
  "retry_after": 60
}
```

## How It Works

### Sliding Window Algorithm

The rate limiter uses an atomic Redis + Lua sliding window algorithm:

1. For each principal dimension (for example `order_id` and `user_id`), Redis keeps two sorted sets:
   - a minute window key (`:m`)
   - an hour window key (`:h`)
2. Before checking limits, old entries are removed from each set with `ZREMRANGEBYSCORE` so only in-window requests remain.
3. Redis counts current in-window requests with `ZCARD` and compares them to configured limits.
4. If any dimension is already at/over the limit, the script returns `429` metadata (including `Retry-After`) and does not record the new request.
5. If all dimensions are under limits, the script records the new request with `ZADD` and updates key expiry with `PEXPIRE`.

### Request Flow

```
1. Request arrives
2. Middleware checks if path should be rate-limited
3. RedisRateLimiter executes an atomic Lua script in Redis
4. If within limits:
   - Record timestamp
   - Allow request
5. If exceeded:
   - Return 429 Too Many Requests
   - Include Retry-After header
```

## Testing Rate Limiting

```bash
# Make 70 requests quickly (default limit is 60/min)
for i in {1..70}; do
  echo -n "Request $i: "
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST http://localhost:8000/ \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"message/send","id":'$i',"params":{"message":{"role":"user","parts":[{"type":"text","text":"test"}]}}}'
done
```

You should see 429 responses after 60 requests.

### Cloud Run

When the agent is deployed on Cloud Run, use your service URL and include a Bearer token (production typically requires authentication):

```bash
SERVICE_URL="https://your-service-xxxx-uc.a.run.app"  # Your Cloud Run URL
TOKEN="your-oauth-token"  # From DCR client_credentials or SSO

for i in {1..70}; do
  echo -n "Request $i: "
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST "$SERVICE_URL/" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","method":"message/send","id":'$i',"params":{"message":{"role":"user","parts":[{"type":"text","text":"test"}]}}}'
done
```

With authentication, rate limits apply per `order_id` and `user_id` (from the token) instead of per IP. Redis (Cloud Memorystore) is internal to the VPC and cannot be inspected with `redis-cli` from outside.

## Rate Limiting vs Usage Tracking

The agent has two separate systems for managing API usage:

| System | Purpose | Mechanism |
|--------|---------|-----------|
| **Rate Limiting** | Prevent abuse | FastAPI middleware, rejects excess requests |
| **Usage Tracking** | Monitor consumption | ADK plugin, counts tokens and tool calls |

Rate limiting happens **before** the request is processed (at the middleware layer), while usage tracking happens **during** request processing (via ADK plugin callbacks).

## Notes

- Rate limits are enforced across replicas as long as they share the same Redis instance.
- The service verifies Redis connectivity at startup and fails fast when Redis is unavailable.
- **Fail-open behaviour**: If Redis becomes unreachable at runtime, requests are allowed through without rate limiting (with a warning log). This prevents a Redis outage from causing a self-inflicted denial of service.
- **In-transit encryption (TLS)**: Cloud Memorystore instances are created with `--transit-encryption-mode=SERVER_AUTHENTICATION`. Use the `rediss://` URL scheme and set `RATE_LIMIT_REDIS_CA_CERT` to the path of the mounted server CA certificate. See [Cloud Run Deployment — Redis Setup](../deploy/cloudrun/README.md#4-redis-setup-for-rate-limiting) for setup instructions.
