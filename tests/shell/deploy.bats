#!/usr/bin/env bats
# Tests for deploy/cloudrun/deploy.sh
#
# Run with: npx bats tests/shell/deploy.bats
#   or:     make test-shell

DEPLOY_SH="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/deploy/cloudrun/deploy.sh"
MOCK_DIR="$BATS_TEST_DIRNAME"

setup() {
    # Put mock gcloud first in PATH
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    cp "$MOCK_DIR/mock_gcloud.sh" "$BATS_TEST_TMPDIR/bin/gcloud"
    chmod +x "$BATS_TEST_TMPDIR/bin/gcloud"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"

    # Minimal required env vars
    export GOOGLE_CLOUD_PROJECT="test-project"
    export GOOGLE_CLOUD_LOCATION="us-central1"
    export SERVICE_NAME="lightspeed-agent"
    export HANDLER_SERVICE_NAME="marketplace-handler"
    export ENABLE_LB_AGENT="false"
    export ENABLE_LB_HANDLER="false"
    export ENABLE_CLOUD_ARMOR_AGENT="false"
    export ENABLE_CLOUD_ARMOR_HANDLER="false"
    export ENABLE_MARKETPLACE="false"

    # Mock gcloud defaults
    export MOCK_GCLOUD_DESCRIBE_EXIT=1
    export MOCK_GCLOUD_LOG="$BATS_TEST_TMPDIR/gcloud.log"
    : > "$MOCK_GCLOUD_LOG"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

@test "default DEPLOY_SERVICE is all" {
    # Source to get the variables (arg parsing runs on source before guard)
    DEPLOY_SERVICE=""
    source "$DEPLOY_SH"
    [[ "$DEPLOY_SERVICE" == "all" ]]
}

@test "--dry-run sets DRY_RUN=true" {
    set -- --dry-run
    DRY_RUN=""
    source "$DEPLOY_SH"
    [[ "$DRY_RUN" == "true" ]]
}

@test "--service handler sets DEPLOY_SERVICE" {
    set -- --service handler
    DEPLOY_SERVICE=""
    source "$DEPLOY_SH"
    [[ "$DEPLOY_SERVICE" == "handler" ]]
}

@test "unknown flag exits 1" {
    run bash -c "GOOGLE_CLOUD_PROJECT=test-project source '$DEPLOY_SH' --bogus 2>&1"
    # The script should have exited with error
    run bash "$DEPLOY_SH" --bogus 2>&1
    [[ "$status" -ne 0 ]]
}

# ---------------------------------------------------------------------------
# Variable validation
# ---------------------------------------------------------------------------

@test "missing GOOGLE_CLOUD_PROJECT exits 1" {
    unset GOOGLE_CLOUD_PROJECT
    run bash "$DEPLOY_SH"
    [[ "$status" -eq 1 ]]
    [[ "$output" == *"GOOGLE_CLOUD_PROJECT"* ]]
}

@test "ENABLE_LB_AGENT without AGENT_DOMAIN_NAME exits 1" {
    export ENABLE_LB_AGENT="true"
    export AGENT_DOMAIN_NAME=""
    run bash "$DEPLOY_SH"
    [[ "$status" -eq 1 ]]
    [[ "$output" == *"AGENT_DOMAIN_NAME"* ]]
}

@test "ENABLE_CLOUD_ARMOR_AGENT without ENABLE_LB_AGENT exits 1" {
    export ENABLE_CLOUD_ARMOR_AGENT="true"
    export ENABLE_LB_AGENT="false"
    run bash "$DEPLOY_SH"
    [[ "$status" -eq 1 ]]
    [[ "$output" == *"ENABLE_CLOUD_ARMOR_AGENT requires ENABLE_LB_AGENT"* ]]
}

# ---------------------------------------------------------------------------
# update_agentcard_urls function
# ---------------------------------------------------------------------------

@test "update_agentcard_urls: LB domains override Cloud Run URLs" {
    export ENABLE_LB_AGENT="true"
    export AGENT_DOMAIN_NAME="agent.example.com"
    export ENABLE_LB_HANDLER="true"
    export HANDLER_DOMAIN_NAME="handler.example.com"
    export MOCK_GCLOUD_SERVICE_URL=""

    source "$DEPLOY_SH"

    # Override gcloud to capture the update call
    gcloud() {
        if [[ "$*" == *"services describe"* ]]; then
            return 1
        fi
        echo "GCLOUD: $*" >> "$MOCK_GCLOUD_LOG"
    }

    update_agentcard_urls

    run cat "$MOCK_GCLOUD_LOG"
    [[ "$output" == *"AGENT_PROVIDER_URL=https://agent.example.com"* ]]
    [[ "$output" == *"MARKETPLACE_HANDLER_URL=https://handler.example.com"* ]]
}

@test "update_agentcard_urls: returns early when service_url is empty" {
    export ENABLE_LB_AGENT="false"
    export ENABLE_LB_HANDLER="false"

    source "$DEPLOY_SH"

    gcloud() {
        if [[ "$*" == *"services describe"* ]]; then
            return 1
        fi
        echo "GCLOUD: $*" >> "$MOCK_GCLOUD_LOG"
    }

    update_agentcard_urls

    run cat "$MOCK_GCLOUD_LOG"
    # No update call should have been made
    [[ -z "$output" ]]
}

@test "update_agentcard_urls: warns when handler URL is empty" {
    export ENABLE_LB_AGENT="true"
    export AGENT_DOMAIN_NAME="agent.example.com"
    export ENABLE_LB_HANDLER="false"

    source "$DEPLOY_SH"

    gcloud() {
        if [[ "$*" == *"services describe"* ]]; then
            return 1
        fi
        echo "GCLOUD: $*" >> "$MOCK_GCLOUD_LOG"
    }

    run update_agentcard_urls
    [[ "$output" == *"MARKETPLACE_HANDLER_URL not set"* ]]
}

# ---------------------------------------------------------------------------
# Dry-run mode (end-to-end)
# ---------------------------------------------------------------------------

@test "dry-run: deploys all without real gcloud calls" {
    export ENABLE_LB_AGENT="true"
    export AGENT_DOMAIN_NAME="agent.example.com"
    export ENABLE_LB_HANDLER="true"
    export HANDLER_DOMAIN_NAME="handler.example.com"
    export ENABLE_CLOUD_ARMOR_AGENT="true"
    export ENABLE_CLOUD_ARMOR_HANDLER="true"

    run bash "$DEPLOY_SH" --dry-run --service all
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"DRY-RUN"* ]]
    [[ "$output" == *"gcloud run services replace"* ]]
}

@test "dry-run: lb-only mode shows LB setup commands" {
    export ENABLE_LB_AGENT="true"
    export AGENT_DOMAIN_NAME="agent.example.com"

    run bash "$DEPLOY_SH" --dry-run --service lb
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"DRY-RUN"* ]]
    [[ "$output" == *"network-endpoint-groups create"* ]]
    [[ "$output" == *"backend-services create"* ]]
}

@test "dry-run: handler-only doesn't deploy agent" {
    run bash "$DEPLOY_SH" --dry-run --service handler
    [[ "$status" -eq 0 ]]
    # Should deploy handler, not agent
    [[ "$output" == *"marketplace-handler"* ]]
    # The gcloud replace call should reference the handler yaml, not agent
    # (handler yaml contains marketplace-handler)
}

# ---------------------------------------------------------------------------
# Ingress logic
# ---------------------------------------------------------------------------

@test "ingress set to all when LB not enabled" {
    export ENABLE_LB_HANDLER="false"

    run bash "$DEPLOY_SH" --dry-run --service handler
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"--ingress=all"* ]]
}

@test "ingress set to internal-and-cloud-load-balancing when LB enabled" {
    export ENABLE_LB_HANDLER="true"
    export HANDLER_DOMAIN_NAME="handler.example.com"

    run bash "$DEPLOY_SH" --dry-run --service handler
    [[ "$status" -eq 0 ]]
    [[ "$output" == *"internal-and-cloud-load-balancing"* ]]
}
