#!/usr/bin/env bash
set -euo pipefail

# Destroy all CDK stacks
# Note: Some resources like HealthLake datastores, S3 buckets with data,
# and Backup Vaults may need manual cleanup

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CDK_APP="python app_full_demo.py"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
fail() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; exit 1; }

cd "${PROJECT_DIR}"

log "Destroying all CDK stacks..."
cdk destroy --all --app "${CDK_APP}" --force || fail "CDK destroy failed"

log "=============================================="
log "Stack destruction complete!"
log "=============================================="
log "NOTE: The following may require manual cleanup:"
log "  - HealthLake datastore (if still active)"
log "  - S3 buckets with versioned objects"
log "  - AWS Backup vaults"
log "  - CloudWatch log groups"
log "=============================================="
