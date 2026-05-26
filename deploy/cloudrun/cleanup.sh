#!/bin/bash
# =============================================================================
# Google Cloud Run Deployment Cleanup Script
# =============================================================================
#
# This script removes all GCP resources created by setup.sh and deploy.sh:
# - Cloud Run services
# - Pub/Sub topic and subscription
# - Secrets in Secret Manager
# - Service accounts (runtime + Pub/Sub invoker) and IAM bindings
#
# Usage:
#   ./deploy/cloudrun/cleanup.sh [--force]
#
# Options:
#   --force    Skip confirmation prompt
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - GOOGLE_CLOUD_PROJECT environment variable set
#
# =============================================================================

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# =============================================================================
# Configuration
# =============================================================================

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-lightspeed-agent}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-${SERVICE_NAME}}"
HANDLER_SERVICE_NAME="${HANDLER_SERVICE_NAME:-marketplace-handler}"
DB_INSTANCE_NAME="${DB_INSTANCE_NAME:-lightspeed-agent-db}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Pub/Sub Invoker Service Account (must match setup.sh)
PUBSUB_INVOKER_NAME="${PUBSUB_INVOKER_NAME:-pubsub-invoker}"
PUBSUB_INVOKER_SA="${PUBSUB_INVOKER_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Pub/Sub configuration
PUBSUB_TOPIC="${PUBSUB_TOPIC:-marketplace-entitlements}"
PUBSUB_SUBSCRIPTION="${PUBSUB_SUBSCRIPTION:-${PUBSUB_TOPIC}-sub}"

# Load balancer resource name prefix (used by cleanup_service_lb)
LB_NAME="${LB_NAME:-lightspeed-lb}"

# Parse arguments
FORCE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --force)
            FORCE=true
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Usage: $0 [--force]"
            exit 1
            ;;
    esac
done

# Validate required variables
if [[ -z "$PROJECT_ID" ]]; then
    log_error "GOOGLE_CLOUD_PROJECT environment variable is required"
    echo "  export GOOGLE_CLOUD_PROJECT=your-project-id"
    exit 1
fi

log_warn "This will delete the following resources from project: $PROJECT_ID"
echo ""
echo "  - Cloud Run services: $SERVICE_NAME, $HANDLER_SERVICE_NAME"
echo "  - Load balancer resources (if any): forwarding rules, HTTPS proxies,"
echo "    URL maps, SSL certs, backend services, NEGs, static IPs, Cloud Armor policies"
echo "  - Pub/Sub topic: $PUBSUB_TOPIC"
echo "  - Pub/Sub subscription: $PUBSUB_SUBSCRIPTION"
echo "  - Secrets: redhat-sso-client-id, redhat-sso-client-secret, database-url,"
echo "             session-database-url, gma-client-id, gma-client-secret, dcr-encryption-key,"
echo "             rate-limit-redis-url"
echo "  - Service accounts: $SERVICE_ACCOUNT"
echo "                      $PUBSUB_INVOKER_SA"
echo ""

# Confirmation prompt
if [[ "$FORCE" != "true" ]]; then
    read -p "Are you sure you want to delete these resources? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Cleanup cancelled"
        exit 0
    fi
fi

echo ""
log_info "Starting cleanup..."

# =============================================================================
# Step 1: Delete Cloud Run Services
# =============================================================================
log_info "Deleting Cloud Run services..."

# Delete lightspeed-agent service
if gcloud run services describe "$SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    gcloud run services delete "$SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Cloud Run service '$SERVICE_NAME' deleted"
else
    log_info "Cloud Run service '$SERVICE_NAME' does not exist, skipping"
fi

# Delete marketplace-handler service
if gcloud run services describe "$HANDLER_SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    gcloud run services delete "$HANDLER_SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Cloud Run service '$HANDLER_SERVICE_NAME' deleted"
else
    log_info "Cloud Run service '$HANDLER_SERVICE_NAME' does not exist, skipping"
fi

# =============================================================================
# Step 2: Delete Load Balancer Resources (per-service)
# =============================================================================
# Delete all LB resources for a single service (reverse dependency order).
# Uses try-delete: nonexistent resources are silently skipped.
cleanup_service_lb() {
    local service_label="$1"
    local p="${LB_NAME}-${service_label}"

    log_info "Cleaning up ${service_label} LB resources..."

    gcloud compute forwarding-rules delete "${p}-forwarding-rule" --global --project="$PROJECT_ID" --quiet 2>/dev/null || true
    gcloud compute target-https-proxies delete "${p}-https-proxy" --global --project="$PROJECT_ID" --quiet 2>/dev/null || true
    gcloud compute url-maps delete "${p}-url-map" --global --project="$PROJECT_ID" --quiet 2>/dev/null || true
    gcloud compute ssl-certificates delete "${p}-cert" --global --project="$PROJECT_ID" --quiet 2>/dev/null || true
    # Detach Cloud Armor before deleting backend (may have been enabled without the flag)
    gcloud compute backend-services update "${p}-backend" --security-policy="" --global --project="$PROJECT_ID" 2>/dev/null || true
    gcloud compute security-policies delete "${p}-security-policy" --global --project="$PROJECT_ID" --quiet 2>/dev/null || true
    # Remove NEG from backend service before deleting either resource
    gcloud compute backend-services remove-backend "${p}-backend" \
        --global --project="$PROJECT_ID" \
        --network-endpoint-group="${p}-neg" \
        --network-endpoint-group-region="$REGION" 2>/dev/null || true
    gcloud compute backend-services delete "${p}-backend" --global --project="$PROJECT_ID" --quiet 2>/dev/null || true
    gcloud compute network-endpoint-groups delete "${p}-neg" --region="$REGION" --project="$PROJECT_ID" --quiet 2>/dev/null || true
    gcloud compute addresses delete "${p}-ip" --global --project="$PROJECT_ID" --quiet 2>/dev/null || true
}

log_info "Cleaning up load balancer resources (if any)..."
cleanup_service_lb "agent"
cleanup_service_lb "handler"

# =============================================================================
# Step 3: Delete Pub/Sub Resources
# =============================================================================
log_info "Deleting Pub/Sub resources..."

# Delete subscription first (must be deleted before topic)
if gcloud pubsub subscriptions describe "$PUBSUB_SUBSCRIPTION" --project="$PROJECT_ID" &>/dev/null; then
    gcloud pubsub subscriptions delete "$PUBSUB_SUBSCRIPTION" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Pub/Sub subscription '$PUBSUB_SUBSCRIPTION' deleted"
else
    log_info "Pub/Sub subscription '$PUBSUB_SUBSCRIPTION' does not exist, skipping"
fi

# Delete topic (skip if cross-project — the topic is managed externally)
if [[ "$PUBSUB_TOPIC" == projects/* ]]; then
    log_info "Pub/Sub topic is a cross-project reference, skipping deletion: $PUBSUB_TOPIC"
elif gcloud pubsub topics describe "$PUBSUB_TOPIC" --project="$PROJECT_ID" &>/dev/null; then
    gcloud pubsub topics delete "$PUBSUB_TOPIC" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Pub/Sub topic '$PUBSUB_TOPIC' deleted"
else
    log_info "Pub/Sub topic '$PUBSUB_TOPIC' does not exist, skipping"
fi

# =============================================================================
# Step 4: Delete Secrets
# =============================================================================
log_info "Deleting secrets from Secret Manager..."

secrets=(
    "redhat-sso-client-id"
    "redhat-sso-client-secret"
    "database-url"
    "session-database-url"
    "gma-client-id"
    "gma-client-secret"
    "dcr-encryption-key"
    "rate-limit-redis-url"
)

for secret in "${secrets[@]}"; do
    if gcloud secrets describe "$secret" --project="$PROJECT_ID" &>/dev/null; then
        gcloud secrets delete "$secret" \
            --project="$PROJECT_ID" \
            --quiet
        log_info "  Secret '$secret' deleted"
    else
        log_info "  Secret '$secret' does not exist, skipping"
    fi
done

# =============================================================================
# Step 5: Remove IAM Bindings and Delete Service Account
# =============================================================================
log_info "Removing service account IAM bindings..."

roles=(
    "roles/secretmanager.secretAccessor"
    "roles/aiplatform.user"
    "roles/pubsub.subscriber"
    "roles/pubsub.publisher"
    "roles/servicemanagement.serviceController"
    "roles/logging.logWriter"
    "roles/monitoring.metricWriter"
    "roles/cloudsql.client"
    "roles/serviceusage.serviceUsageConsumer"
)

if gcloud iam service-accounts describe "$SERVICE_ACCOUNT" --project="$PROJECT_ID" &>/dev/null; then
    for role in "${roles[@]}"; do
        log_info "  Removing $role..."
        gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
            --member="serviceAccount:$SERVICE_ACCOUNT" \
            --role="$role" \
            --quiet 2>/dev/null || true
    done

    log_info "Deleting runtime service account..."
    gcloud iam service-accounts delete "$SERVICE_ACCOUNT" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Service account '$SERVICE_ACCOUNT' deleted"
else
    log_info "Service account '$SERVICE_ACCOUNT' does not exist, skipping"
fi

# Delete Pub/Sub Invoker Service Account
log_info "Removing Pub/Sub Invoker service account..."

if gcloud iam service-accounts describe "$PUBSUB_INVOKER_SA" --project="$PROJECT_ID" &>/dev/null; then
    # Remove the service-level run.invoker binding on marketplace-handler
    log_info "  Removing roles/run.invoker from $HANDLER_SERVICE_NAME..."
    gcloud run services remove-iam-policy-binding "$HANDLER_SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --member="serviceAccount:$PUBSUB_INVOKER_SA" \
        --role="roles/run.invoker" \
        --quiet 2>/dev/null || true

    # Remove the self-referencing serviceAccountUser binding
    log_info "  Removing roles/iam.serviceAccountUser..."
    gcloud iam service-accounts remove-iam-policy-binding "$PUBSUB_INVOKER_SA" \
        --member="serviceAccount:$PUBSUB_INVOKER_SA" \
        --role="roles/iam.serviceAccountUser" \
        --project="$PROJECT_ID" \
        --quiet 2>/dev/null || true

    # Remove the project-level pubsub.editor binding
    log_info "  Removing roles/pubsub.editor..."
    gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:$PUBSUB_INVOKER_SA" \
        --role="roles/pubsub.editor" \
        --quiet 2>/dev/null || true

    log_info "Deleting Pub/Sub Invoker service account..."
    gcloud iam service-accounts delete "$PUBSUB_INVOKER_SA" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Service account '$PUBSUB_INVOKER_SA' deleted"
else
    log_info "Service account '$PUBSUB_INVOKER_SA' does not exist, skipping"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
log_info "=========================================="
log_info "Cleanup complete!"
log_info "=========================================="
echo ""
echo "The following resources have been removed:"
echo "  - Cloud Run services ($SERVICE_NAME, $HANDLER_SERVICE_NAME)"
echo "  - Load balancer resources (if any existed)"
echo "  - Pub/Sub topic and subscription"
echo "  - Secret Manager secrets"
echo "  - Service accounts (runtime + Pub/Sub invoker) and IAM bindings"
echo ""
echo "Note: The following resources were NOT deleted (delete manually if needed):"
echo "  - Cloud SQL instances"
echo "  - Cloud Memorystore Redis instances"
echo "  - Container images in GCR/Artifact Registry"
echo "  - VPC connectors"
echo "  - Cloud Build triggers"
echo ""
echo "To delete these, use the respective gcloud commands:"
echo "  gcloud sql instances delete $DB_INSTANCE_NAME --project=$PROJECT_ID"
echo "  gcloud redis instances delete lightspeed-redis --region=$REGION --project=$PROJECT_ID"
echo "  gcloud container images delete gcr.io/$PROJECT_ID/$SERVICE_NAME --force-delete-tags --quiet"
echo "  gcloud container images delete gcr.io/$PROJECT_ID/$HANDLER_SERVICE_NAME --force-delete-tags --quiet"
echo "  gcloud container images delete gcr.io/$PROJECT_ID/red-hat-lightspeed-mcp --force-delete-tags --quiet"
echo "  gcloud compute networks vpc-access connectors delete lightspeed-redis-conn --region=$REGION --project=$PROJECT_ID"
echo ""
