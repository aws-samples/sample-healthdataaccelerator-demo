#!/usr/bin/env bash
# =============================================================================
# Healthcare Demo - Full Deployment Script
# =============================================================================
# Orchestrates:
#   1. Synthea patient data generation (FHIR R4 bundles)
#   2. CDK infrastructure deployment
#   3. FHIR bundle upload to S3
#
# Prerequisites:
#   - Java 11+ (for Synthea)
#   - AWS CLI configured with appropriate credentials
#   - CDK CLI installed
#   - Python virtual environment with dependencies installed
#
# Environment Variables (all optional):
#   PATIENT_COUNT  - Number of patients to generate (default: 100)
#   SYNTHEA_SEED   - Random seed for reproducibility (default: 12345)
#   SYNTHEA_STATE  - US state for patient generation (default: Massachusetts)
#
# Usage:
#   ./scripts/deploy.sh
#   PATIENT_COUNT=50 SYNTHEA_STATE=California ./scripts/deploy.sh
# =============================================================================

set -euo pipefail

# ---- Configuration ----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SYNTHEA_DIR="${PROJECT_DIR}/submodules/synthea"
SYNTHEA_JAR="${SYNTHEA_DIR}/build/libs/synthea-with-dependencies.jar"
SYNTHEA_OUTPUT="${SYNTHEA_DIR}/output/fhir"
SYNTHEA_PROPERTIES="${SCRIPT_DIR}/synthea/synthea.properties"

PATIENT_COUNT="${PATIENT_COUNT:-100}"
SYNTHEA_SEED="${SYNTHEA_SEED:-12345}"
SYNTHEA_STATE="${SYNTHEA_STATE:-Massachusetts}"

CDK_APP="python app_full_demo.py"
CDK_STACK_NAME="CombinedDataLoaderStack"

# ---- Helper Functions -------------------------------------------------------
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

fail() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2
    exit 1
}

# ---- Step 1: Verify Java >= 11 ---------------------------------------------
log "Checking Java installation..."

if ! command -v java &> /dev/null; then
    fail "Java is not installed. Synthea requires Java 11 or higher. Install with:
  macOS:   brew install openjdk@11
  Ubuntu:  sudo apt install openjdk-11-jre
  Amazon Linux: sudo yum install java-11-amazon-corretto"
fi

JAVA_VERSION=$(java -version 2>&1 | head -n 1 | sed -E 's/.*"([0-9]+).*/\1/')
if [[ -z "${JAVA_VERSION}" ]] || [[ "${JAVA_VERSION}" -lt 11 ]]; then
    fail "Java 11 or higher is required (found version ${JAVA_VERSION:-unknown}). Install with:
  macOS:   brew install openjdk@11
  Ubuntu:  sudo apt install openjdk-11-jre
  Amazon Linux: sudo yum install java-11-amazon-corretto"
fi

log "Java ${JAVA_VERSION} detected (>= 11 required) ✓"

# ---- Step 2: Initialize Synthea submodule if needed -------------------------
if [[ ! -f "${SYNTHEA_DIR}/run_synthea" ]]; then
    log "Initializing Synthea submodule..."
    git -C "${PROJECT_DIR}" submodule update --init submodules/synthea \
        || fail "Failed to initialize Synthea submodule. Run: git submodule update --init --recursive"
fi

# ---- Step 3: Build Synthea if JAR does not exist ----------------------------
if [[ ! -f "${SYNTHEA_JAR}" ]]; then
    log "Building Synthea (this may take a few minutes on first run)..."
    cd "${SYNTHEA_DIR}"
    ./gradlew build -x test || fail "Synthea build failed. Try running: cd ${SYNTHEA_DIR} && ./gradlew clean build -x test"
    cd "${PROJECT_DIR}"
    
    if [[ ! -f "${SYNTHEA_JAR}" ]]; then
        fail "Synthea JAR not found at ${SYNTHEA_JAR} after build. Check Gradle output for errors."
    fi
    log "Synthea built successfully ✓"
else
    log "Synthea JAR already exists, skipping build ✓"
fi

# ---- Step 4: Enable clinical note export via properties ---------------------
if [[ -f "${SYNTHEA_PROPERTIES}" ]]; then
    log "Copying Synthea properties for clinical note export..."
    cp "${SYNTHEA_PROPERTIES}" "${SYNTHEA_DIR}/synthea.properties"
else
    log "Creating Synthea properties for clinical note export..."
    cat > "${SYNTHEA_DIR}/synthea.properties" << 'EOF'
exporter.fhir.export = true
exporter.fhir_r4.export = true
exporter.clinical_note.export = true
exporter.text.export = true
exporter.text.per_encounter_export = true
exporter.ccda.export = false
exporter.csv.export = false
exporter.hospital.fhir.export = false
exporter.practitioner.fhir.export = false
EOF
fi

# ---- Step 5: Generate Synthea patients --------------------------------------
log "Generating ${PATIENT_COUNT} synthetic patients in ${SYNTHEA_STATE} (seed: ${SYNTHEA_SEED})..."

cd "${SYNTHEA_DIR}"
./run_synthea \
    -p "${PATIENT_COUNT}" \
    -s "${SYNTHEA_SEED}" \
    --exporter.fhir.export=true \
    "${SYNTHEA_STATE}" 2>&1 | tail -20

cd "${PROJECT_DIR}"

BUNDLE_COUNT=$(find "${SYNTHEA_OUTPUT}" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
if [[ "${BUNDLE_COUNT}" -eq 0 ]]; then
    fail "Synthea generated 0 FHIR bundles. Check Synthea output above for errors."
fi

log "Synthea generated ${BUNDLE_COUNT} FHIR bundles ✓"

# ---- Step 6: Deploy CDK stacks ---------------------------------------------
# NOTE: The data loader Lambda is triggered as a CDK custom resource during deploy.
# However, it needs Synthea bundles in S3 first. We deploy in two phases:
#   Phase 1: Deploy all stacks (data loader will find 0 bundles, that's OK)
#   Phase 2: Upload bundles then invoke the data loader manually

log "Deploying CDK stacks (Phase 1 - infrastructure)..."

cd "${PROJECT_DIR}"
cdk deploy --all --app "${CDK_APP}" --require-approval never \
    || fail "CDK deployment failed. Check the CloudFormation console for details."

log "CDK deployment complete ✓"

# ---- Step 7: Retrieve bucket name from CloudFormation outputs ---------------
log "Retrieving Synthea bucket name from CloudFormation outputs..."

BUCKET_NAME=$(aws cloudformation describe-stacks \
    --stack-name "${CDK_STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='SyntheaBucketName'].OutputValue" \
    --output text 2>/dev/null)

if [[ -z "${BUCKET_NAME}" ]] || [[ "${BUCKET_NAME}" == "None" ]]; then
    fail "Could not retrieve SyntheaBucketName from CloudFormation stack '${CDK_STACK_NAME}'.
  Ensure the stack deployed successfully: aws cloudformation describe-stacks --stack-name ${CDK_STACK_NAME}"
fi

log "Synthea bucket: ${BUCKET_NAME} ✓"

# ---- Step 8: Upload FHIR bundles to S3 --------------------------------------
log "Uploading ${BUNDLE_COUNT} FHIR bundles to s3://${BUCKET_NAME}/synthea-bundles/..."

aws s3 sync "${SYNTHEA_OUTPUT}" "s3://${BUCKET_NAME}/synthea-bundles/" --quiet \
    || fail "S3 upload failed. Verify AWS credentials and bucket '${BUCKET_NAME}' exists.
  Bucket: ${BUCKET_NAME}
  Prefix: synthea-bundles/
  Source: ${SYNTHEA_OUTPUT}"

UPLOADED_COUNT=$(aws s3 ls "s3://${BUCKET_NAME}/synthea-bundles/" --recursive | wc -l | tr -d ' ')
log "Uploaded ${UPLOADED_COUNT} files to S3 ✓"

# ---- Step 8.5: Invoke data loader with Synthea bundles now in S3 ------------
log "Invoking data loader to parse FHIR bundles and load into OpenEMR..."

# NOTE: list-functions auto-paginates. Applying `| [0]` inside --query emits
# one result per page (pages without a match yield `None`), so --output text
# can return "name\nNone" and corrupt the function name. Instead, return all
# matches, split to lines, drop empty/None, and take the first real match.
DATA_LOADER_FN=$(aws lambda list-functions \
    --query "Functions[?contains(FunctionName,'CombinedDataLoader3C0660C7')].FunctionName" \
    --output text 2>/dev/null | tr '\t' '\n' | grep -v -e '^$' -e '^None$' | head -n1)

if [[ -n "${DATA_LOADER_FN}" ]] && [[ "${DATA_LOADER_FN}" != "None" ]]; then
    echo '{"RequestType":"Create","ResourceProperties":{"ForceReload":"true"}}' > /tmp/deploy-payload.json
    # This is a long-running synchronous invoke. Disable client-side retries
    # (AWS_MAX_ATTEMPTS=1) and the read timeout (--cli-read-timeout 0) so the CLI
    # waits for this single execution instead of timing out and retrying, which
    # would spawn duplicate (concurrent) loader runs. The invocation is bounded
    # by the Lambda's own function timeout.
    AWS_MAX_ATTEMPTS=1 aws lambda invoke \
        --function-name "${DATA_LOADER_FN}" \
        --payload fileb:///tmp/deploy-payload.json \
        --cli-read-timeout 0 \
        /tmp/data-loader-output.json 2>&1 | tail -3

    LOADER_RESULT=$(cat /tmp/data-loader-output.json 2>/dev/null)
    log "Data loader result: ${LOADER_RESULT}"
else
    log "WARNING: Data loader Lambda not found. Data will load on next scheduled sync."
fi

# ---- Step 9: Configure Lake Formation Governance ----------------------------
# Grant Lake Formation permissions on the HealthLake resource link database.
# HealthLake uses a cross-account shared catalog (resource link).

log "Configuring Lake Formation governance..."

DATASTORE_ID=$(aws cloudformation describe-stacks \
    --stack-name "HealthLakeDatastoreStack" \
    --query "Stacks[0].Outputs[?OutputKey=='DatastoreId'].OutputValue" \
    --output text 2>/dev/null)

if [[ -n "${DATASTORE_ID}" ]] && [[ "${DATASTORE_ID}" != "None" ]]; then
    # Discover the actual HealthLake database name from Glue
    HEALTHLAKE_DB=$(aws glue get-databases \
        --query "DatabaseList[?contains(Name,'${DATASTORE_ID}')].Name | [0]" \
        --output text 2>/dev/null)

    if [[ -z "${HEALTHLAKE_DB}" ]] || [[ "${HEALTHLAKE_DB}" == "None" ]]; then
        log "WARNING: HealthLake resource link database not found yet."
        log "  Tables may still be generating. Re-run deploy.sh later."
    else
        log "HealthLake database: ${HEALTHLAKE_DB}"

        # Get the shared catalog ID from the resource link target
        SHARED_CATALOG=$(aws glue get-database --name "${HEALTHLAKE_DB}" \
            --query 'Database.TargetDatabase.CatalogId' --output text 2>/dev/null)

        log "Shared catalog: ${SHARED_CATALOG}"

        # Ensure current role is Lake Formation admin
        CURRENT_ROLE_ARN=$(aws sts get-caller-identity --query 'Arn' --output text | sed 's|assumed-role/\(.*\)/.*|role/\1|; s|sts|iam|')
        aws lakeformation put-data-lake-settings \
            --data-lake-settings "{\"DataLakeAdmins\":[{\"DataLakePrincipalIdentifier\":\"${CURRENT_ROLE_ARN}\"}]}" 2>/dev/null

        # Grant DESCRIBE on the resource link database
        aws lakeformation grant-permissions \
            --principal "{\"DataLakePrincipalIdentifier\":\"${CURRENT_ROLE_ARN}\"}" \
            --resource "{\"Database\":{\"Name\":\"${HEALTHLAKE_DB}\"}}" \
            --permissions DESCRIBE 2>/dev/null \
            && log "Granted DESCRIBE on resource link ✓" \
            || log "DESCRIBE grant already exists"

        # Grant SELECT + DESCRIBE on all tables in the shared catalog
        if [[ -n "${SHARED_CATALOG}" ]] && [[ "${SHARED_CATALOG}" != "None" ]]; then
            aws lakeformation grant-permissions \
                --principal "{\"DataLakePrincipalIdentifier\":\"${CURRENT_ROLE_ARN}\"}" \
                --resource "{\"Table\":{\"DatabaseName\":\"${HEALTHLAKE_DB}\",\"TableWildcard\":{},\"CatalogId\":\"${SHARED_CATALOG}\"}}" \
                --permissions SELECT DESCRIBE 2>/dev/null \
                && log "Granted SELECT/DESCRIBE on all HealthLake tables ✓" \
                || log "Table grants already exist"
        fi

        log "Lake Formation governance configured ✓"
    fi
else
    log "WARNING: HealthLake datastore not found. Skipping Lake Formation."
fi

# ---- Step 10: Trigger HealthLake Sync ----------------------------------------
# Sync OpenEMR data to HealthLake. Split into two invocations to avoid timeout:
# 1) All resources except ClinicalNote
# 2) ClinicalNote only (can be large and slow)

log "Triggering HealthLake sync (OpenEMR → HealthLake)..."

# Same pagination-safe pattern as the data loader lookup above.
SYNC_FN=$(aws lambda list-functions \
    --query "Functions[?contains(FunctionName,'healthlake-db-sync')].FunctionName" \
    --output text 2>/dev/null | tr '\t' '\n' | grep -v -e '^$' -e '^None$' | head -n1)

if [[ -n "${SYNC_FN}" ]] && [[ "${SYNC_FN}" != "None" ]]; then
    log "Sync Lambda: ${SYNC_FN}"

    # First pass: all resource types except ClinicalNote.
    # The sync can run ~13 min. Disable client retries (AWS_MAX_ATTEMPTS=1) and
    # the read timeout (--cli-read-timeout 0) so the CLI waits for this single
    # execution rather than timing out at 900s and retrying, which would launch
    # duplicate concurrent sync runs. Bounded by the Lambda's own timeout.
    log "  Pass 1: Syncing patients, encounters, conditions, allergies, meds, immunizations..."
    AWS_MAX_ATTEMPTS=1 aws lambda invoke \
        --function-name "${SYNC_FN}" \
        --cli-binary-format raw-in-base64-out \
        --payload '{"full_sync": true, "resource_types": ["Patient", "Encounter", "Condition", "AllergyIntolerance", "MedicationRequest", "Immunization"]}' \
        --cli-read-timeout 0 \
        /tmp/sync-pass1-output.json 2>&1 | tail -3
    log "  Pass 1 complete: $(python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(v.get("synced",0) for v in json.loads(d.get("body","{}")).values()))' < /tmp/sync-pass1-output.json 2>/dev/null || echo 'check logs') resources synced"

    # Second pass: ClinicalNote only (separate to avoid timeout).
    # Same retry/timeout handling as Pass 1 to avoid duplicate sync runs.
    log "  Pass 2: Syncing clinical notes..."
    AWS_MAX_ATTEMPTS=1 aws lambda invoke \
        --function-name "${SYNC_FN}" \
        --cli-binary-format raw-in-base64-out \
        --payload '{"full_sync": true, "resource_types": ["ClinicalNote"]}' \
        --cli-read-timeout 0 \
        /tmp/sync-pass2-output.json 2>&1 | tail -3
    log "  Pass 2 complete: $(python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(v.get("synced",0) for v in json.loads(d.get("body","{}")).values()))' < /tmp/sync-pass2-output.json 2>/dev/null || echo 'check logs') notes synced"

    log "HealthLake sync complete ✓"
else
    log "WARNING: DB sync Lambda not found. Data will sync on next scheduled run."
fi

# ---- Done -------------------------------------------------------------------
log "=============================================="
log "Deployment complete!"
log "=============================================="
log "  Patients generated: ${BUNDLE_COUNT}"
log "  S3 bucket: ${BUCKET_NAME}"
log "  S3 prefix: synthea-bundles/"
log "  CDK stacks: deployed"
log "  Lake Formation: ${HEALTHLAKE_DB:-skipped}"
log "  HealthLake sync: ${SYNC_FN:-skipped}"
log "=============================================="
log "=============================================="
