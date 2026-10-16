#!/usr/bin/env bash
set -euo pipefail

# Destroy all CDK stacks
# Note: Some resources like HealthLake datastores, S3 buckets with data,
# and Backup Vaults may need manual cleanup

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CDK_APP="python app_full_demo.py"

# ---- TLS / DNS variant selection --------------------------------------------
# Pass the SAME context the stacks were deployed with so `cdk destroy` targets
# the correct variant. For the DNS variant, set ROUTE53_DOMAIN (and optionally
# ROUTE53_HOSTED_ZONE_ID); leave empty for the domain-less / self-signed variant.
ROUTE53_DOMAIN="${ROUTE53_DOMAIN:-}"
ROUTE53_HOSTED_ZONE_ID="${ROUTE53_HOSTED_ZONE_ID:-}"

CDK_CONTEXT_ARGS=()
CDK_CONTEXT_ARGS+=(-c "route53_domain=${ROUTE53_DOMAIN}")
if [[ -n "${ROUTE53_HOSTED_ZONE_ID}" ]]; then
    CDK_CONTEXT_ARGS+=(-c "route53_hosted_zone_id=${ROUTE53_HOSTED_ZONE_ID}")
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
fail() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; exit 1; }

cd "${PROJECT_DIR}"

if [[ -n "${ROUTE53_DOMAIN}" ]]; then
    log "Destroy variant: DNS (route53_domain=${ROUTE53_DOMAIN})"
else
    log "Destroy variant: domain-less / self-signed (no route53_domain)"
fi

log "Destroying all CDK stacks..."
cdk destroy --all --app "${CDK_APP}" --force "${CDK_CONTEXT_ARGS[@]}" || fail "CDK destroy failed"

log "=============================================="
log "Stack destruction complete!"
log "=============================================="
log "NOTE: The following may require manual cleanup:"
log "  - HealthLake datastore (if still active)"
log "  - S3 buckets with versioned objects"
log "  - AWS Backup vaults"
log "  - CloudWatch log groups"
log "=============================================="
