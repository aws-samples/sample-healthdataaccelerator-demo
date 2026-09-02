#!/usr/bin/env python3
"""
CDK App for Orthanc PACS Server

Deploy with:
    cdk deploy --app "python app_orthanc.py" OrthancStack

Security Options:
    - allowed_ip_ranges: Restrict access to specific IP CIDR ranges (REQUIRED)
    - certificate_arn: Enable HTTPS with an ACM certificate
    - domain_name: Custom domain for the Orthanc web interface

PHI / HIPAA NOTICE:
Orthanc is a PACS server that stores and serves DICOM medical imaging, which is
protected health information (PHI) under HIPAA. If you process real PHI, this is
a HIPAA-regulated workload: execute an AWS Business Associate Addendum (BAA),
keep data within HIPAA-eligible services, and enable encryption, access logging,
and audit controls. The customer is responsible for compliant handling of
regulated data. This sample ships with synthetic data only.
"""

import path_setup  # noqa: F401

import os
import aws_cdk as cdk

from infrastructure.orthanc_stack import OrthancStack


app = cdk.App()

# ============================================================================
# CONFIGURATION - Update these values for your environment
# ============================================================================

# HealthLake datastore ID for sync (optional - leave unset to skip sync).
# Provide via the HEALTHLAKE_DATASTORE_ID environment variable.
HEALTHLAKE_DATASTORE_ID = os.environ.get("HEALTHLAKE_DATASTORE_ID")

# Security Configuration
# Restrict to specific IP ranges (REQUIRED - no 0.0.0.0/0 allowed)
# Get your IP: curl -s https://checkip.amazonaws.com
ALLOWED_IP_RANGES = [
    "72.21.198.64/32",
    "72.21.198.66/32",
    "52.95.4.18/32",
    "52.94.133.143/32",
]

# HTTPS Configuration
CERTIFICATE_ARN = "YOUR_CERTIFICATE_ARN"
DOMAIN_NAME = "orthanc.YOUR_DOMAIN.example.com"
HOSTED_ZONE_ID = "YOUR_HOSTED_ZONE_ID"

# ============================================================================

OrthancStack(
    app,
    "OrthancStack",
    healthlake_datastore_id=HEALTHLAKE_DATASTORE_ID,
    allowed_ip_ranges=ALLOWED_IP_RANGES,
    certificate_arn=CERTIFICATE_ARN,
    domain_name=DOMAIN_NAME,
    hosted_zone_id=HOSTED_ZONE_ID,
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)

app.synth()
