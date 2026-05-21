#!/bin/bash
# =============================================================================
# Google Cloud Run Deployment Script
# =============================================================================
#
# Deploys BOTH services to Google Cloud Run:
# 1. marketplace-handler - Handles DCR and Pub/Sub events (always running)
# 2. lightspeed-agent - A2A agent with MCP sidecar (runs after provisioning)
#
# Uses the YAML service configs (service.yaml and marketplace-handler.yaml)
# with variable substitution to deploy each service.
#
# Usage:
#   ./deploy/cloudrun/deploy.sh [OPTIONS]
#
# Options:
#   --service <service>       Which service to deploy: all, handler, agent, lb
#                             (default: all)
#                             "lb" sets up load balancers only (no service redeploy)
#   --image <image>           Container image for the agent
#                             (default: gcr.io/$PROJECT_ID/lightspeed-agent:latest)
#   --handler-image <image>   Container image for the marketplace handler
#                             (default: gcr.io/$PROJECT_ID/marketplace-handler:latest)
#   --mcp-image <image>       Container image for the MCP server
#                             (default: gcr.io/$PROJECT_ID/red-hat-lightspeed-mcp:latest)
#   --allow-unauthenticated   Allow public access
#   --build                   Build images before deploying
#
# Architecture:
#   ┌─────────────────────────┐     ┌─────────────────────────┐
#   │  Marketplace Handler    │     │   Lightspeed Agent      │
#   │  (Cloud Run #1)         │     │    (Cloud Run #2)       │
#   │                         │     │                         │
#   │  - POST /dcr            │     │  - POST / (A2A)         │
#   │  - Pub/Sub push         │     │  - /.well-known/agent   │
#   │  - Account approval     │     │  - OAuth flow           │
#   │  - GMA SSO API          │     │  - MCP sidecar          │
#   └─────────────────────────┘     └─────────────────────────┘
#
# Prerequisites:
#   - Run setup.sh first to configure GCP services
#   - Update secrets in Secret Manager with actual values
#
# =============================================================================

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# =============================================================================
# Configuration
# =============================================================================

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
VERTEXAI_LOCATION="${VERTEXAI_LOCATION:-global}"
SERVICE_NAME="${SERVICE_NAME:-lightspeed-agent}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-${SERVICE_NAME}}"
HANDLER_SERVICE_NAME="${HANDLER_SERVICE_NAME:-marketplace-handler}"
DB_INSTANCE_NAME="${DB_INSTANCE_NAME:-lightspeed-agent-db}"
VPC_CONNECTOR_NAME="${VPC_CONNECTOR_NAME:-lightspeed-redis-conn}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Pub/Sub Invoker Service Account (must match setup.sh)
PUBSUB_INVOKER_NAME="${PUBSUB_INVOKER_NAME:-pubsub-invoker}"
PUBSUB_INVOKER_SA="${PUBSUB_INVOKER_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Marketplace configuration
ENABLE_MARKETPLACE="${ENABLE_MARKETPLACE:-true}"

# Per-service load balancer configuration
ENABLE_LB_AGENT="${ENABLE_LB_AGENT:-false}"
ENABLE_LB_HANDLER="${ENABLE_LB_HANDLER:-false}"
ENABLE_CLOUD_ARMOR_AGENT="${ENABLE_CLOUD_ARMOR_AGENT:-false}"
ENABLE_CLOUD_ARMOR_HANDLER="${ENABLE_CLOUD_ARMOR_HANDLER:-false}"
AGENT_DOMAIN_NAME="${AGENT_DOMAIN_NAME:-}"
HANDLER_DOMAIN_NAME="${HANDLER_DOMAIN_NAME:-}"
LB_NAME="${LB_NAME:-lightspeed-lb}"
SERVICE_CONTROL_SERVICE_NAME="${SERVICE_CONTROL_SERVICE_NAME:-}"
PUBSUB_TOPIC="${PUBSUB_TOPIC:-marketplace-entitlements}"

# When PUBSUB_TOPIC is a fully-qualified path (projects/.../topics/...),
# the default derivation "${PUBSUB_TOPIC}-sub" produces an invalid
# subscription name.  Require PUBSUB_SUBSCRIPTION to be set explicitly.
if [[ "$PUBSUB_TOPIC" == projects/* && -z "${PUBSUB_SUBSCRIPTION:-}" ]]; then
    log_error "PUBSUB_TOPIC is a fully-qualified path but PUBSUB_SUBSCRIPTION is not set."
    log_error "Set PUBSUB_SUBSCRIPTION to a valid subscription name, e.g.:"
    log_error "  export PUBSUB_SUBSCRIPTION=\"marketplace-events-sub\""
    exit 1
fi
PUBSUB_SUBSCRIPTION="${PUBSUB_SUBSCRIPTION:-${PUBSUB_TOPIC}-sub}"

# Default images
AGENT_IMAGE="${AGENT_IMAGE:-}"
HANDLER_IMAGE="${HANDLER_IMAGE:-}"
# MCP image must be in GCR since Cloud Run doesn't support Quay.io directly
# See README.md for instructions to copy the image from Quay.io to GCR
MCP_IMAGE="${MCP_IMAGE:-gcr.io/${PROJECT_ID}/red-hat-lightspeed-mcp:latest}"

# Parse arguments
DEPLOY_SERVICE="all"  # all, handler, agent
ALLOW_UNAUTH=false
BUILD_IMAGE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --service)
            DEPLOY_SERVICE="$2"
            shift 2
            ;;
        --image)
            AGENT_IMAGE="$2"
            shift 2
            ;;
        --handler-image)
            HANDLER_IMAGE="$2"
            shift 2
            ;;
        --mcp-image)
            MCP_IMAGE="$2"
            shift 2
            ;;
        --allow-unauthenticated)
            ALLOW_UNAUTH=true
            shift
            ;;
        --build)
            BUILD_IMAGE=true
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Usage: $0 [--service all|handler|agent|lb] [--image IMAGE] [--handler-image IMAGE] [--mcp-image IMAGE] [--allow-unauthenticated] [--build]"
            exit 1
            ;;
    esac
done

# Validate required variables
if [[ -z "$PROJECT_ID" ]]; then
    log_error "GOOGLE_CLOUD_PROJECT environment variable is required"
    exit 1
fi

if [[ "$ENABLE_LB_AGENT" == "true" && -z "$AGENT_DOMAIN_NAME" ]]; then
    log_error "AGENT_DOMAIN_NAME is required when ENABLE_LB_AGENT=true"
    echo "  export AGENT_DOMAIN_NAME=agent.example.com"
    exit 1
fi

if [[ "$ENABLE_LB_HANDLER" == "true" && -z "$HANDLER_DOMAIN_NAME" ]]; then
    log_error "HANDLER_DOMAIN_NAME is required when ENABLE_LB_HANDLER=true"
    echo "  export HANDLER_DOMAIN_NAME=dcr.example.com"
    exit 1
fi

if [[ "$ENABLE_CLOUD_ARMOR_AGENT" == "true" && "$ENABLE_LB_AGENT" != "true" ]]; then
    log_error "ENABLE_CLOUD_ARMOR_AGENT requires ENABLE_LB_AGENT=true"
    exit 1
fi

if [[ "$ENABLE_CLOUD_ARMOR_HANDLER" == "true" && "$ENABLE_LB_HANDLER" != "true" ]]; then
    log_error "ENABLE_CLOUD_ARMOR_HANDLER requires ENABLE_LB_HANDLER=true"
    exit 1
fi

# Set default images if not specified
if [[ -z "$AGENT_IMAGE" ]]; then
    AGENT_IMAGE="gcr.io/${PROJECT_ID}/lightspeed-agent:${IMAGE_TAG}"
fi
if [[ -z "$HANDLER_IMAGE" ]]; then
    HANDLER_IMAGE="gcr.io/${PROJECT_ID}/${HANDLER_SERVICE_NAME}:${IMAGE_TAG}"
fi

log_info "Deploying to Cloud Run"
log_info "  Project: $PROJECT_ID"
log_info "  Region: $REGION"
log_info "  Service(s): $DEPLOY_SERVICE"
log_info "  Agent Image: $AGENT_IMAGE"
log_info "  Handler Image: $HANDLER_IMAGE"
log_info "  MCP Image: $MCP_IMAGE"

# =============================================================================
# Build images if requested
# =============================================================================
build_agent_image() {
    log_info "Building agent image..."

    gcloud builds submit \
        --tag "$AGENT_IMAGE" \
        --project "$PROJECT_ID" \
        --dockerfile Containerfile \
        .

    log_info "Image built: $AGENT_IMAGE"
}

build_handler_image() {
    log_info "Building marketplace handler image..."

    gcloud builds submit \
        --tag "$HANDLER_IMAGE" \
        --project "$PROJECT_ID" \
        --dockerfile Containerfile.marketplace-handler \
        .

    log_info "Image built: $HANDLER_IMAGE"
}

# =============================================================================
# Deploy using service YAML configs
# =============================================================================
deploy_agent() {
    log_info "Deploying agent with service.yaml..."

    # Create temporary file with substituted values
    local tmp_yaml
    tmp_yaml=$(mktemp)

    # Substitute variables in service.yaml
    # Note: Image substitution must happen BEFORE PROJECT_ID substitution
    sed -e "s|gcr.io/\${PROJECT_ID}/lightspeed-agent:latest|${AGENT_IMAGE}|g" \
        -e "s|\${MCP_IMAGE}|${MCP_IMAGE}|g" \
        -e "s|\${PROJECT_ID}|${PROJECT_ID}|g" \
        -e "s|\${REGION}|${REGION}|g" \
        -e "s|\${VERTEXAI_LOCATION}|${VERTEXAI_LOCATION}|g" \
        -e "s|\${SERVICE_NAME}|${SERVICE_NAME}|g" \
        -e "s|\${SERVICE_ACCOUNT_NAME}|${SERVICE_ACCOUNT_NAME}|g" \
        -e "s|\${DB_INSTANCE_NAME}|${DB_INSTANCE_NAME}|g" \
        -e "s|\${VPC_CONNECTOR_NAME}|${VPC_CONNECTOR_NAME}|g" \
        deploy/cloudrun/service.yaml > "$tmp_yaml"

    # Deploy using the YAML
    gcloud run services replace "$tmp_yaml" \
        --region "$REGION" \
        --project "$PROJECT_ID"

    # Set IAM policy if allowing unauthenticated
    if [[ "$ALLOW_UNAUTH" == "true" ]]; then
        log_info "Allowing unauthenticated access for agent..."
        gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
            --region "$REGION" \
            --project "$PROJECT_ID" \
            --member="allUsers" \
            --role="roles/run.invoker"
    fi

    # Cleanup
    rm -f "$tmp_yaml"
}

deploy_handler() {
    log_info "Deploying marketplace handler with marketplace-handler.yaml..."

    # Create temporary file with substituted values
    local tmp_yaml
    tmp_yaml=$(mktemp)

    # Substitute variables in marketplace-handler.yaml
    # Note: Image substitution must happen BEFORE PROJECT_ID substitution
    sed -e "s|gcr.io/\${PROJECT_ID}/marketplace-handler:latest|${HANDLER_IMAGE}|g" \
        -e "s|\${PROJECT_ID}|${PROJECT_ID}|g" \
        -e "s|\${REGION}|${REGION}|g" \
        -e "s|\${SERVICE_NAME}|${SERVICE_NAME}|g" \
        -e "s|\${SERVICE_ACCOUNT_NAME}|${SERVICE_ACCOUNT_NAME}|g" \
        -e "s|\${HANDLER_SERVICE_NAME}|${HANDLER_SERVICE_NAME}|g" \
        -e "s|\${DB_INSTANCE_NAME}|${DB_INSTANCE_NAME}|g" \
        -e "s|\${SERVICE_CONTROL_SERVICE_NAME}|${SERVICE_CONTROL_SERVICE_NAME}|g" \
        -e "s|\${VPC_CONNECTOR_NAME}|${VPC_CONNECTOR_NAME}|g" \
        deploy/cloudrun/marketplace-handler.yaml > "$tmp_yaml"

    # Deploy using the YAML
    gcloud run services replace "$tmp_yaml" \
        --region "$REGION" \
        --project "$PROJECT_ID"

    # Marketplace handler needs to be publicly accessible for Pub/Sub push
    if [[ "$ALLOW_UNAUTH" == "true" ]]; then
        log_info "Allowing unauthenticated access for handler..."
        gcloud run services add-iam-policy-binding "$HANDLER_SERVICE_NAME" \
            --region "$REGION" \
            --project "$PROJECT_ID" \
            --member="allUsers" \
            --role="roles/run.invoker"
    fi

    # Cleanup
    rm -f "$tmp_yaml"
}

# =============================================================================
# Configure Pub/Sub push subscription
# =============================================================================
configure_pubsub_push() {
    if [[ "$ENABLE_MARKETPLACE" != "true" ]]; then
        log_info "Skipping Pub/Sub push configuration (ENABLE_MARKETPLACE=$ENABLE_MARKETPLACE)"
        return
    fi

    log_info "Configuring Pub/Sub push subscription..."

    # Get the marketplace-handler URL for the push endpoint
    local handler_url
    handler_url=$(gcloud run services describe "$HANDLER_SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --format='value(status.url)' 2>/dev/null || echo "")

    if [[ -z "$handler_url" ]]; then
        log_warn "Could not retrieve $HANDLER_SERVICE_NAME URL. Skipping Pub/Sub push configuration."
        log_warn "Run deploy.sh again after the handler is deployed."
        return
    fi

    local push_endpoint="${handler_url}/dcr"

    # Grant the Pub/Sub Invoker SA permission to invoke the marketplace-handler.
    # This is a service-level binding (not project-level), following least privilege.
    log_info "Granting roles/run.invoker to Pub/Sub Invoker SA on $HANDLER_SERVICE_NAME..."
    gcloud run services add-iam-policy-binding "$HANDLER_SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --member="serviceAccount:$PUBSUB_INVOKER_SA" \
        --role="roles/run.invoker" \
        --quiet || true

    # For cross-project topics (fully-qualified path), the Pub/Sub Invoker SA
    # is the account linked in the Google Cloud Marketplace Producer Portal and
    # has permission to subscribe to the external topic.  We must impersonate it
    # because the caller's personal account does not have that permission.
    local impersonate_flag=""
    if [[ "$PUBSUB_TOPIC" == projects/* ]]; then
        impersonate_flag="--impersonate-service-account=$PUBSUB_INVOKER_SA"
        log_info "Using service account impersonation for cross-project topic"
    fi

    # Create or update the push subscription
    if gcloud pubsub subscriptions describe "$PUBSUB_SUBSCRIPTION" --project="$PROJECT_ID" &>/dev/null; then
        log_info "Updating existing subscription '$PUBSUB_SUBSCRIPTION' with push endpoint..."
        gcloud pubsub subscriptions update "$PUBSUB_SUBSCRIPTION" \
            --push-endpoint="$push_endpoint" \
            --push-auth-service-account="$PUBSUB_INVOKER_SA" \
            --ack-deadline=60 \
            --project="$PROJECT_ID" \
            --quiet
    else
        log_info "Creating push subscription '$PUBSUB_SUBSCRIPTION'..."
        # shellcheck disable=SC2086
        gcloud pubsub subscriptions create "$PUBSUB_SUBSCRIPTION" \
            --topic="$PUBSUB_TOPIC" \
            --push-endpoint="$push_endpoint" \
            --push-auth-service-account="$PUBSUB_INVOKER_SA" \
            --ack-deadline=60 \
            --project="$PROJECT_ID" \
            $impersonate_flag
    fi

    log_info "Pub/Sub push subscription configured:"
    log_info "  Subscription: $PUBSUB_SUBSCRIPTION"
    log_info "  Push endpoint: $push_endpoint"
    log_info "  Auth SA: $PUBSUB_INVOKER_SA"
}

# =============================================================================
# Configure per-service Google Cloud Load Balancer
# =============================================================================
# Creates an independent GCLB for a single Cloud Run service.
# Usage: setup_service_lb <service_label> <cloud_run_service> <domain_name> <cloud_armor_enabled>
#   service_label:      "agent" or "handler" (used in resource naming)
#   cloud_run_service:  Cloud Run service name to front with the LB
#   domain_name:        Domain for the Google-managed SSL certificate
#   cloud_armor_enabled: "true" to create and attach a Cloud Armor WAF policy
setup_service_lb() {
    local service_label="$1"
    local cloud_run_service="$2"
    local domain_name="$3"
    local cloud_armor_enabled="$4"

    local neg_name="${LB_NAME}-${service_label}-neg"
    local backend_name="${LB_NAME}-${service_label}-backend"
    local policy_name="${LB_NAME}-${service_label}-security-policy"
    local url_map_name="${LB_NAME}-${service_label}-url-map"
    local cert_name="${LB_NAME}-${service_label}-cert"
    local proxy_name="${LB_NAME}-${service_label}-https-proxy"
    local rule_name="${LB_NAME}-${service_label}-forwarding-rule"
    local ip_name="${LB_NAME}-${service_label}-ip"

    log_info "Setting up load balancer for ${service_label} (${cloud_run_service})..."

    # -------------------------------------------------------------------------
    # Create serverless NEG
    # -------------------------------------------------------------------------
    if ! gcloud compute network-endpoint-groups describe "$neg_name" \
        --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute network-endpoint-groups create "$neg_name" \
            --region="$REGION" \
            --network-endpoint-type=serverless \
            --cloud-run-service="$cloud_run_service" \
            --project="$PROJECT_ID"
        log_info "NEG '$neg_name' created"
    else
        log_info "NEG '$neg_name' already exists"
    fi

    # -------------------------------------------------------------------------
    # Create backend service
    # -------------------------------------------------------------------------
    if ! gcloud compute backend-services describe "$backend_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute backend-services create "$backend_name" \
            --global \
            --project="$PROJECT_ID"
        gcloud compute backend-services add-backend "$backend_name" \
            --global \
            --network-endpoint-group="$neg_name" \
            --network-endpoint-group-region="$REGION" \
            --project="$PROJECT_ID"
        log_info "Backend service '$backend_name' created"
    else
        log_info "Backend service '$backend_name' already exists"
    fi

    # -------------------------------------------------------------------------
    # Create Cloud Armor security policy (if enabled)
    # -------------------------------------------------------------------------
    if [[ "$cloud_armor_enabled" == "true" ]]; then
        log_info "Configuring Cloud Armor security policy for ${service_label}..."

        if ! gcloud compute security-policies describe "$policy_name" \
            --global --project="$PROJECT_ID" &>/dev/null; then
            gcloud compute security-policies create "$policy_name" \
                --global \
                --project="$PROJECT_ID"
            log_info "Security policy '$policy_name' created"
        else
            log_info "Security policy '$policy_name' already exists"
        fi

        # Add preconfigured WAF rules (OWASP ModSecurity CRS)
        declare -A WAF_RULES=(
            [1000]="sqli-v33-stable"
            [1100]="xss-v33-stable"
            [1200]="lfi-v33-stable"
            [1300]="rfi-v33-stable"
            [1400]="rce-v33-stable"
            [1500]="scannerdetection-v33-stable"
            [1600]="protocolattack-v33-stable"
            [1700]="sessionfixation-v33-stable"
        )

        for priority in $(echo "${!WAF_RULES[@]}" | tr ' ' '\n' | sort -n); do
            local waf_rule_name="${WAF_RULES[$priority]}"
            if ! gcloud compute security-policies rules describe "$priority" \
                --security-policy="$policy_name" \
                --global --project="$PROJECT_ID" &>/dev/null; then
                gcloud compute security-policies rules create "$priority" \
                    --security-policy="$policy_name" \
                    --expression="evaluatePreconfiguredExpr('${waf_rule_name}')" \
                    --action=deny-403 \
                    --global \
                    --project="$PROJECT_ID"
                log_info "WAF rule '${waf_rule_name}' added at priority $priority"
            else
                log_info "WAF rule at priority $priority already exists"
            fi
        done

        log_info "Attaching security policy to backend service..."
        gcloud compute backend-services update "$backend_name" \
            --security-policy="$policy_name" \
            --global --project="$PROJECT_ID"
        log_info "Cloud Armor security policy attached to '$backend_name'"
    fi

    # -------------------------------------------------------------------------
    # Create URL map (simple default backend, no path routing needed)
    # -------------------------------------------------------------------------
    if ! gcloud compute url-maps describe "$url_map_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute url-maps create "$url_map_name" \
            --default-service="$backend_name" \
            --global \
            --project="$PROJECT_ID"
        log_info "URL map '$url_map_name' created"
    else
        log_info "URL map '$url_map_name' already exists"
    fi

    # -------------------------------------------------------------------------
    # Create HTTPS proxy with managed SSL certificate
    # -------------------------------------------------------------------------
    if ! gcloud compute ssl-certificates describe "$cert_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        log_error "SSL certificate '$cert_name' does not exist. Run setup.sh first."
        return 1
    fi

    if ! gcloud compute target-https-proxies describe "$proxy_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute target-https-proxies create "$proxy_name" \
            --ssl-certificates="$cert_name" \
            --url-map="$url_map_name" \
            --global \
            --project="$PROJECT_ID"
        log_info "HTTPS proxy '$proxy_name' created"
    else
        log_info "HTTPS proxy '$proxy_name' already exists"
    fi

    # -------------------------------------------------------------------------
    # Create global forwarding rule
    # -------------------------------------------------------------------------
    if ! gcloud compute forwarding-rules describe "$rule_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute forwarding-rules create "$rule_name" \
            --global \
            --target-https-proxy="$proxy_name" \
            --address="$ip_name" \
            --ports=443 \
            --project="$PROJECT_ID"
        log_info "Forwarding rule '$rule_name' created"
    else
        log_info "Forwarding rule '$rule_name' already exists"
    fi

    # -------------------------------------------------------------------------
    # Override Cloud Run ingress to internal-and-cloud-load-balancing
    # -------------------------------------------------------------------------
    log_info "Updating $cloud_run_service ingress to internal-and-cloud-load-balancing..."
    gcloud run services update "$cloud_run_service" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --ingress=internal-and-cloud-load-balancing \
        --quiet
    log_info "Cloud Run ingress updated for $cloud_run_service"
}

# =============================================================================
# Main deployment
# =============================================================================

# Build images if requested
if [[ "$BUILD_IMAGE" == "true" ]]; then
    case "$DEPLOY_SERVICE" in
        all)
            build_handler_image
            build_agent_image
            ;;
        handler)
            build_handler_image
            ;;
        agent)
            build_agent_image
            ;;
    esac
fi

# Deploy based on service selection
case "$DEPLOY_SERVICE" in
    all)
        deploy_handler
        configure_pubsub_push
        deploy_agent
        if [[ "$ENABLE_LB_HANDLER" == "true" ]]; then
            setup_service_lb "handler" "$HANDLER_SERVICE_NAME" "$HANDLER_DOMAIN_NAME" "$ENABLE_CLOUD_ARMOR_HANDLER"
        else
            log_info "No LB for handler — setting ingress to all..."
            gcloud run services update "$HANDLER_SERVICE_NAME" \
                --region="$REGION" --project="$PROJECT_ID" \
                --ingress=all --quiet
        fi
        if [[ "$ENABLE_LB_AGENT" == "true" ]]; then
            setup_service_lb "agent" "$SERVICE_NAME" "$AGENT_DOMAIN_NAME" "$ENABLE_CLOUD_ARMOR_AGENT"
        else
            log_info "No LB for agent — setting ingress to all..."
            gcloud run services update "$SERVICE_NAME" \
                --region="$REGION" --project="$PROJECT_ID" \
                --ingress=all --quiet
        fi
        ;;
    handler)
        deploy_handler
        configure_pubsub_push
        if [[ "$ENABLE_LB_HANDLER" == "true" ]]; then
            setup_service_lb "handler" "$HANDLER_SERVICE_NAME" "$HANDLER_DOMAIN_NAME" "$ENABLE_CLOUD_ARMOR_HANDLER"
        else
            log_info "No LB for handler — setting ingress to all..."
            gcloud run services update "$HANDLER_SERVICE_NAME" \
                --region="$REGION" --project="$PROJECT_ID" \
                --ingress=all --quiet
        fi
        ;;
    agent)
        deploy_agent
        if [[ "$ENABLE_LB_AGENT" == "true" ]]; then
            setup_service_lb "agent" "$SERVICE_NAME" "$AGENT_DOMAIN_NAME" "$ENABLE_CLOUD_ARMOR_AGENT"
        else
            log_info "No LB for agent — setting ingress to all..."
            gcloud run services update "$SERVICE_NAME" \
                --region="$REGION" --project="$PROJECT_ID" \
                --ingress=all --quiet
        fi
        ;;
    lb)
        log_info "Setting up load balancers only (no service redeploy)..."
        if [[ "$ENABLE_LB_HANDLER" == "true" ]]; then
            if ! gcloud run services describe "$HANDLER_SERVICE_NAME" \
                --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
                log_error "$HANDLER_SERVICE_NAME does not exist. Deploy it first before setting up its LB."
                exit 1
            fi
            setup_service_lb "handler" "$HANDLER_SERVICE_NAME" "$HANDLER_DOMAIN_NAME" "$ENABLE_CLOUD_ARMOR_HANDLER"
        fi
        if [[ "$ENABLE_LB_AGENT" == "true" ]]; then
            if ! gcloud run services describe "$SERVICE_NAME" \
                --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
                log_error "$SERVICE_NAME does not exist. Deploy it first before setting up its LB."
                exit 1
            fi
            setup_service_lb "agent" "$SERVICE_NAME" "$AGENT_DOMAIN_NAME" "$ENABLE_CLOUD_ARMOR_AGENT"
        fi
        if [[ "$ENABLE_LB_AGENT" != "true" && "$ENABLE_LB_HANDLER" != "true" ]]; then
            log_warn "No load balancers enabled. Set ENABLE_LB_AGENT=true and/or ENABLE_LB_HANDLER=true."
        fi
        ;;
    *)
        log_error "Unknown service: $DEPLOY_SERVICE"
        echo "Valid services: all, handler, agent, lb"
        exit 1
        ;;
esac

# =============================================================================
# Post-deployment
# =============================================================================

log_info "Deployment complete!"
echo ""

# Get and display service URLs based on what was deployed
show_service_info() {
    local service_name="$1"
    local service_url

    service_url=$(gcloud run services describe "$service_name" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --format='value(status.url)' 2>/dev/null || echo "")

    if [[ -n "$service_url" ]]; then
        log_info "$service_name URL: $service_url"
        echo "  Test: curl $service_url/health"
    else
        log_warn "Could not retrieve $service_name URL"
    fi
}

# Update AGENT_PROVIDER_URL and MARKETPLACE_HANDLER_URL on the agent service
# so the AgentCard advertises the correct externally-reachable URLs.
# When per-service LBs are enabled, uses GCLB domains instead of Cloud Run URLs.
update_agentcard_urls() {
    local service_url handler_url env_vars
    service_url=$(gcloud run services describe "$SERVICE_NAME" \
        --region="$REGION" --project="$PROJECT_ID" \
        --format='value(status.url)' 2>/dev/null || echo "")
    handler_url=$(gcloud run services describe "$HANDLER_SERVICE_NAME" \
        --region="$REGION" --project="$PROJECT_ID" \
        --format='value(status.url)' 2>/dev/null || echo "")

    [[ "$ENABLE_LB_AGENT" == "true" ]] && service_url="https://$AGENT_DOMAIN_NAME"
    [[ "$ENABLE_LB_HANDLER" == "true" ]] && handler_url="https://$HANDLER_DOMAIN_NAME"

    [[ -z "$service_url" ]] && return

    env_vars="AGENT_PROVIDER_URL=$service_url"
    if [[ -n "$handler_url" ]]; then
        env_vars="$env_vars,MARKETPLACE_HANDLER_URL=$handler_url"
    else
        log_warn "Could not retrieve $HANDLER_SERVICE_NAME URL. MARKETPLACE_HANDLER_URL not set."
    fi

    log_info "Updating agent env vars: $env_vars"
    if gcloud run services update "$SERVICE_NAME" \
        --region="$REGION" --project="$PROJECT_ID" \
        --update-env-vars="$env_vars" --quiet 2>/dev/null; then
        log_info "Agent env vars updated successfully"
    else
        log_warn "Could not update agent env vars. Set AGENT_PROVIDER_URL and MARKETPLACE_HANDLER_URL manually."
    fi
}

# Show info for deployed services
case "$DEPLOY_SERVICE" in
    all)
        echo ""
        show_service_info "$HANDLER_SERVICE_NAME"
        echo ""
        show_service_info "$SERVICE_NAME"
        update_agentcard_urls

        echo ""
        echo "Architecture:"
        echo "  1. Marketplace Handler receives Pub/Sub events and DCR requests"
        echo "  2. Agent handles A2A protocol and user interactions"
        echo ""
        echo "Test endpoints:"
        echo "  Handler health: curl \$(gcloud run services describe $HANDLER_SERVICE_NAME --region=$REGION --format='value(status.url)')/health"
        echo "  Agent card:     curl \$(gcloud run services describe $SERVICE_NAME --region=$REGION --format='value(status.url)')/.well-known/agent.json"
        ;;
    handler)
        echo ""
        show_service_info "$HANDLER_SERVICE_NAME"
        echo ""
        echo "The marketplace handler is ready to receive:"
        echo "  - Pub/Sub events from Google Cloud Marketplace"
        echo "  - DCR requests from Gemini Enterprise"
        ;;
    agent)
        echo ""
        show_service_info "$SERVICE_NAME"
        update_agentcard_urls

        echo ""
        echo "Test the agent:"
        echo "  curl \$(gcloud run services describe $SERVICE_NAME --region=$REGION --format='value(status.url)')/.well-known/agent.json"
        ;;
    lb)
        update_agentcard_urls
        ;;
esac

# Show per-service load balancer info
if [[ "$ENABLE_LB_AGENT" == "true" ]]; then
    echo ""
    log_info "=========================================="
    log_info "Agent Load Balancer"
    log_info "=========================================="

    AGENT_LB_IP=$(gcloud compute addresses describe "${LB_NAME}-agent-ip" \
        --global --project="$PROJECT_ID" \
        --format='value(address)' 2>/dev/null || echo "unknown")

    AGENT_CERT_STATUS=$(gcloud compute ssl-certificates describe "${LB_NAME}-agent-cert" \
        --global --project="$PROJECT_ID" \
        --format='value(managed.status)' 2>/dev/null || echo "unknown")

    echo ""
    echo "  URL:         https://$AGENT_DOMAIN_NAME"
    echo "  Static IP:   $AGENT_LB_IP"
    echo "  SSL status:  $AGENT_CERT_STATUS"
    echo ""
    if [[ "$AGENT_CERT_STATUS" != "ACTIVE" ]]; then
        log_warn "SSL certificate is not yet active (status: $AGENT_CERT_STATUS)"
        log_warn "Ensure DNS A record points $AGENT_DOMAIN_NAME → $AGENT_LB_IP"
        log_warn "Provisioning can take up to 60 minutes after DNS propagation."
    fi
fi

if [[ "$ENABLE_LB_HANDLER" == "true" ]]; then
    echo ""
    log_info "=========================================="
    log_info "Handler Load Balancer"
    log_info "=========================================="

    HANDLER_LB_IP=$(gcloud compute addresses describe "${LB_NAME}-handler-ip" \
        --global --project="$PROJECT_ID" \
        --format='value(address)' 2>/dev/null || echo "unknown")

    HANDLER_CERT_STATUS=$(gcloud compute ssl-certificates describe "${LB_NAME}-handler-cert" \
        --global --project="$PROJECT_ID" \
        --format='value(managed.status)' 2>/dev/null || echo "unknown")

    echo ""
    echo "  URL:         https://$HANDLER_DOMAIN_NAME"
    echo "  Static IP:   $HANDLER_LB_IP"
    echo "  SSL status:  $HANDLER_CERT_STATUS"
    echo ""
    if [[ "$HANDLER_CERT_STATUS" != "ACTIVE" ]]; then
        log_warn "SSL certificate is not yet active (status: $HANDLER_CERT_STATUS)"
        log_warn "Ensure DNS A record points $HANDLER_DOMAIN_NAME → $HANDLER_LB_IP"
        log_warn "Provisioning can take up to 60 minutes after DNS propagation."
    fi
fi

echo ""
echo "View logs:"
echo "  gcloud run services logs read $HANDLER_SERVICE_NAME --region=$REGION --project=$PROJECT_ID"
echo "  gcloud run services logs read $SERVICE_NAME --region=$REGION --project=$PROJECT_ID"
