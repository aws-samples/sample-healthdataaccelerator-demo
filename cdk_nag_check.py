#!/usr/bin/env python3
"""Run cdk-nag (AwsSolutions + HIPAA Security) across the ENTIRE solution.

Previously this only synthesized the OpenEMR stack. It now synthesizes the full
app (app_full_demo.py) with the `cdk_nag` context flag, which attaches the
AwsSolutions and HIPAASecurity aspects to *every* stack: SyntheaStaging,
OpenemrEcs, HealthLakeDatastore, Orthanc, HealthLakeSync, CombinedDataLoader,
and PatientDashboard.

It runs synth-only (no deploy) with placeholder context, so it needs Docker
(for Lambda asset bundling) but NOT real AWS credentials, IP, or domain. The
route53 domain is forced empty so no hosted-zone lookup is attempted (fully
offline); DNS/ACM/CloudFront-alias resources are therefore not exercised here.

Per-stack reports are written to:
    cdk.out/AwsSolutions-<Stack>-NagReport.csv
    cdk.out/HIPAA.Security-<Stack>-NagReport.csv

Usage:  python cdk_nag_check.py
"""

import subprocess
import sys

# CLI context (-c) takes precedence over cdk.json, so this is deterministic
# regardless of any local cdk.json overrides.
CONTEXT = {
    "cdk_nag": "true",                              # enables the nag aspects
    "security_group_ip_range_ipv4": "203.0.113.0/32",  # valid placeholder CIDR
    "route53_domain": "",                            # empty -> skip DNS lookups (offline)
    "enable_automatic_data_loading": "true",         # include CombinedDataLoaderStack
}

cmd = ["cdk", "synth", "--all", "--app", "python app_full_demo.py", "--quiet"]
for key, value in CONTEXT.items():
    cmd += ["-c", f"{key}={value}"]

print("Running whole-solution cdk-nag:\n  " + " ".join(cmd) + "\n")
rc = subprocess.call(cmd)

print(
    "\n✅ cdk-nag complete across all stacks. Reports in cdk.out/:\n"
    "   - AwsSolutions-<Stack>-NagReport.csv\n"
    "   - HIPAA.Security-<Stack>-NagReport.csv"
)
sys.exit(rc)
