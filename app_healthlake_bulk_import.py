#!/usr/bin/env python3
"""
CDK App for deploying HealthLake Bulk Import Pipeline

Deploy with:
    cdk deploy --app "python app_healthlake_bulk_import.py" HealthLakeBulkImportStack

This stack creates:
- S3 bucket for FHIR NDJSON files
- Glue ETL job to transform OpenEMR data to FHIR format
- Lambda to orchestrate the pipeline
- EventBridge rule for nightly execution

PHI / HIPAA NOTICE:
This pipeline processes FHIR data, which is protected health information (PHI)
under HIPAA. If you process real PHI, this is a HIPAA-regulated workload:
execute an AWS Business Associate Addendum (BAA), keep data within
HIPAA-eligible services, and enable encryption, access logging, and audit
controls. The customer is responsible for compliant handling of regulated
data. This sample ships with synthetic data only.
"""

import path_setup  # noqa: F401

import os
import aws_cdk as cdk
from infrastructure.healthlake_bulk_import_stack import HealthLakeBulkImportStack

app = cdk.App()

# ============================================================================
# CONFIGURATION - Update these values for your environment
# ============================================================================

# HealthLake datastore ID (create in AWS Console first)
HEALTHLAKE_DATASTORE_ID = "your-datastore-id"

# Source database name in Glue Data Catalog
SOURCE_DATABASE = "openemr_landing"

# ============================================================================

HealthLakeBulkImportStack(
    app,
    "HealthLakeBulkImportStack",
    healthlake_datastore_id=HEALTHLAKE_DATASTORE_ID,
    source_database=SOURCE_DATABASE,
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)

app.synth()
