#!/bin/bash
# Mock gcloud CLI for testing deploy.sh
# Place this directory first in PATH to intercept gcloud calls.
#
# Behavior is controlled via environment variables:
#   MOCK_GCLOUD_SERVICE_URL  — URL returned by 'run services describe --format=status.url'
#   MOCK_GCLOUD_DESCRIBE_EXIT — exit code for describe/list commands (default: 1 = not found)
#   MOCK_GCLOUD_LOG          — file to append mutation commands to (default: /dev/null)

SUBARGS="$*"

# Service URL query (used by update_agentcard_urls, show_service_info)
if [[ "$SUBARGS" == *"services describe"*"--format"*"status.url"* ]]; then
    if [[ -n "${MOCK_GCLOUD_SERVICE_URL:-}" ]]; then
        echo "$MOCK_GCLOUD_SERVICE_URL"
        exit 0
    fi
    exit 1
fi

# Address describe returning an IP
if [[ "$SUBARGS" == *"addresses describe"*"--format"*"address"* ]]; then
    echo "${MOCK_GCLOUD_STATIC_IP:-203.0.113.1}"
    exit 0
fi

# SSL cert status
if [[ "$SUBARGS" == *"ssl-certificates describe"*"--format"*"managed.status"* ]]; then
    echo "${MOCK_GCLOUD_CERT_STATUS:-ACTIVE}"
    exit 0
fi

# Read-only commands: configurable exit code
if [[ "$SUBARGS" == *" describe "* || "$SUBARGS" == *" list "* ]]; then
    exit "${MOCK_GCLOUD_DESCRIBE_EXIT:-1}"
fi

# Mutations: log and succeed
echo "MOCK_GCLOUD: $SUBARGS" >> "${MOCK_GCLOUD_LOG:-/dev/null}"
exit 0
