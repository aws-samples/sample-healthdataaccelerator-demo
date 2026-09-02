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

import shutil
import subprocess  # nosec B404 - only used to invoke the local `cdk` CLI with a static argv (shell=False)
import sys

# Fully static, hardcoded argv. Every element is a string literal defined right
# here; the list is never assembled from argv, environment variables, stdin,
# network, or file contents, and no value is interpolated at runtime. CLI
# context (-c) takes precedence over cdk.json, so this is deterministic
# regardless of any local cdk.json overrides:
#   cdk_nag=true                    -> enables the AwsSolutions + HIPAA aspects
#   security_group_ip_range_ipv4    -> RFC 5737 documentation-range placeholder
#   route53_domain= (empty)         -> forces an offline synth (no DNS lookup)
#   enable_automatic_data_loading   -> includes CombinedDataLoaderStack
CDK_SYNTH_ARGS = [
    "synth", "--all",
    "--app", "python app_full_demo.py",
    "--quiet",
    "-c", "cdk_nag=true",
    "-c", "security_group_ip_range_ipv4=203.0.113.0/32",
    "-c", "route53_domain=",
    "-c", "enable_automatic_data_loading=true",
]

# Resolve the cdk CLI to an absolute path instead of relying on a bare/relative
# executable name at call time (defense in depth; avoids partial-path issues).
cdk_path = shutil.which("cdk")
if cdk_path is None:
    sys.exit("Error: the 'cdk' CLI was not found on PATH. Install aws-cdk and retry.")

# argv[0] is a resolved absolute path; the remaining elements are static
# literals from CDK_SYNTH_ARGS. Nothing here is caller- or attacker-controllable.
argv = [cdk_path, *CDK_SYNTH_ARGS]

print("Running whole-solution cdk-nag:\n  " + " ".join(argv) + "\n")
# Reviewed false positive: argv is a fully static list and the process is spawned
# with shell=False, so there is no shell interpretation and no external or
# user-controllable input reaches the command. Command injection is not possible.
rc = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    argv, shell=False, check=False
).returncode

print(
    "\n✅ cdk-nag complete across all stacks. Reports in cdk.out/:\n"
    "   - AwsSolutions-<Stack>-NagReport.csv\n"
    "   - HIPAA.Security-<Stack>-NagReport.csv"
)
sys.exit(rc)
