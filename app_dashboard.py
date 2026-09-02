#!/usr/bin/env python3
"""
CDK App for Patient 360 Dashboard

Deploy with:
    cdk deploy --app "python app_dashboard.py" PatientDashboardStack

This stack creates:
- Cognito User Pool for authentication
- S3 bucket for static website hosting
- CloudFront distribution
- API Gateway + Lambda for HealthLake proxy
"""

import path_setup  # noqa: F401

import os
import aws_cdk as cdk

from infrastructure.patient_dashboard_stack import PatientDashboardStack


app = cdk.App()

# ============================================================================
# CONFIGURATION - Update these values for your environment
# ============================================================================

# HealthLake datastore ID (from your HealthLake deployment)
HEALTHLAKE_DATASTORE_ID = "your-datastore-id"

# Orthanc URL (from your Orthanc deployment output)
ORTHANC_URL = "https://orthanc.example.com"

# VPC Configuration (from your OpenEMR deployment - needed for Orthanc access)
VPC_ID = None  # e.g., "vpc-0123456789abcdef0"

# Custom domain configuration (optional)
DOMAIN_NAME = None  # e.g., "dashboard.example.com"
CERTIFICATE_ARN = None  # e.g., "arn:aws:acm:us-east-1:ACCOUNT:certificate/xxx"
HOSTED_ZONE_ID = None  # e.g., "Z0123456789ABCDEFGHIJ"

# ============================================================================

PatientDashboardStack(
    app,
    "PatientDashboardStack",
    healthlake_datastore_id=HEALTHLAKE_DATASTORE_ID,
    orthanc_url=ORTHANC_URL,
    vpc_id=VPC_ID,
    domain_name=DOMAIN_NAME,
    certificate_arn=CERTIFICATE_ARN,
    hosted_zone_id=HOSTED_ZONE_ID,
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)

app.synth()
