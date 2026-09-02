#!/usr/bin/env python3
"""
CDK App for deploying OpenEMR to HealthLake Sync

Deploy with:
    cdk deploy --app "python app_healthlake_sync.py" HealthLakeSyncStack

This stack creates:
- Lambda for incremental FHIR sync (every 5 minutes)
- Lambda for daily full DB sync (2 AM)
- DynamoDB table for tracking sync state
- EventBridge rules for scheduled execution
"""

import path_setup  # noqa: F401

import os
import aws_cdk as cdk
from infrastructure.healthlake_sync_stack import HealthLakeSyncStack

app = cdk.App()

# ============================================================================
# CONFIGURATION - Values from your OpenEMR and HealthLake deployments
# Override these via CDK context (-c key=value) or environment variables.
# ============================================================================

# OpenEMR URL (from OpenemrEcsStack output)
OPENEMR_BASE_URL = os.getenv("OPENEMR_BASE_URL", app.node.try_get_context("openemr_base_url") or "")

# HealthLake Configuration (from HealthLakeDatastoreStack output)
HEALTHLAKE_DATASTORE_ID = os.getenv("HEALTHLAKE_DATASTORE_ID", app.node.try_get_context("healthlake_datastore_id") or "")
HEALTHLAKE_ENDPOINT = f"https://healthlake.us-east-1.amazonaws.com/datastore/{HEALTHLAKE_DATASTORE_ID}/r4/"

# VPC Configuration (from OpenemrEcsStack output)
VPC_ID = os.getenv("VPC_ID", app.node.try_get_context("vpc_id") or "")

# Database Configuration (for daily DB sync)
DB_SECRET_ARN = os.getenv("DB_SECRET_ARN", app.node.try_get_context("db_secret_arn") or "")
DB_SECURITY_GROUP_ID = os.getenv("DB_SECURITY_GROUP_ID", app.node.try_get_context("db_security_group_id") or "")

# ============================================================================

HealthLakeSyncStack(
    app,
    "HealthLakeSyncStack",
    openemr_base_url=OPENEMR_BASE_URL,
    healthlake_datastore_id=HEALTHLAKE_DATASTORE_ID,
    healthlake_endpoint=HEALTHLAKE_ENDPOINT,
    sync_interval_minutes=5,
    vpc_id=VPC_ID,
    db_secret_arn=DB_SECRET_ARN,
    db_security_group_id=DB_SECURITY_GROUP_ID,
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)

app.synth()
