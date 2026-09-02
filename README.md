# AWS Healthcare Data Architecture Workshop

A hands-on workshop demonstrating how to build a modern healthcare data platform on AWS using OpenEMR, Orthanc PACS, and AWS HealthLake.

## Overview

This workshop deploys a complete healthcare system that:
- Runs **OpenEMR** (open-source EHR) on ECS Fargate
- Runs **Orthanc** (open-source PACS) for medical imaging
- Syncs clinical data to **AWS HealthLake** (FHIR datastore)
- Provides a **Patient 360 Dashboard** for providers to view consolidated patient data

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Healthcare Data Platform                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│   ┌──────────────────────────┐  ┌──────────────────────────┐        │
│   │        OpenEMR           │  │        Orthanc           │        │
│   │       (EHR/EMR)          │  │        (PACS)            │        │
│   │                          │  │                          │        │
│   │  - Patient demographics  │  │  - DICOM images          │        │
│   │  - Encounters            │  │  - X-rays, CT, MRI       │        │
│   │  - Problems/Conditions   │  │  - OHIF Viewer           │        │
│   │  - Medications           │  │                          │        │
│   │  - Allergies             │  │                          │        │
│   └───────────┬──────────────┘  └───────────┬──────────────┘        │
│               │                             │                        │
│               │   FHIR Sync (Lambda)        │   Direct Query         │
│               ▼                             ▼                        │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │                    AWS HealthLake                            │   │
│   │                  (FHIR R4 Datastore)                         │   │
│   │                                                              │   │
│   │  Central repository for clinical data:                       │   │
│   │  • Patient, Condition, AllergyIntolerance                    │   │
│   │  • MedicationRequest, Encounter, Immunization                │   │
│   └──────────────────────────┬──────────────────────────────────┘   │
│                              │                                       │
│                              ▼                                       │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │                  Patient 360 Dashboard                       │   │
│   │                                                              │   │
│   │  Unified view of patient data:                               │   │
│   │  • Demographics, conditions, medications, allergies          │   │
│   │  • Imaging studies with OHIF viewer integration              │   │
│   │  • AI-powered summaries (Amazon Bedrock)                     │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

> **Responsible AI:** The AI-powered summaries and chat use Amazon Bedrock and are a documentation aid only — **not** medical advice, a diagnosis, or clinical decision support. A qualified clinician must review and verify all AI output before it informs care (human-in-the-loop). Amazon Bedrock Guardrails are enabled by default (a denied "MedicalAdvice" topic, HIGH content filters, and prompt-injection mitigation). See [RESPONSIBLE_AI.md](RESPONSIBLE_AI.md) and the [Disclaimer](#disclaimer).

## The HealthLake Value Proposition

This demo showcases **AWS HealthLake as a central FHIR repository** that aggregates clinical data:

| Source System | Data Type | FHIR Resources |
|--------------|-----------|----------------|
| **OpenEMR** (EHR) | Clinical records | Patient, Condition, AllergyIntolerance, MedicationRequest, Encounter, Immunization |
| **Orthanc** (PACS) | Medical imaging | Queried directly via DICOMweb |

**Key Benefits:**
- **Single source of truth** - All patient data accessible via standard FHIR APIs
- **Interoperability** - Data from disparate systems normalized to FHIR R4
- **Analytics-ready** - Query across all data sources for population health insights
- **AI/ML integration** - Feed unified data to Amazon Bedrock for summaries

## Components

| Component | Description | Technology |
|-----------|-------------|------------|
| OpenEMR | Electronic Health Records system | ECS Fargate, Aurora MySQL, ElastiCache |
| Orthanc | PACS server for medical imaging | ECS Fargate, EFS |
| HealthLake Sync | Syncs OpenEMR data to HealthLake | Lambda (dual-mode, see below) |
| Lake Formation | Governed analytics over FHIR data | Lake Formation, Athena |
| Patient Dashboard | Provider-facing unified patient view | CloudFront, API Gateway, Bedrock |

## HealthLake Sync Architecture

The project uses a **dual-sync strategy** to keep HealthLake in sync with OpenEMR:

| Sync Mode | Lambda | Schedule | Mechanism | Purpose |
|-----------|--------|----------|-----------|---------|
| **Continuous (FHIR API)** | `openemr-healthlake-sync` | Every 5 minutes | Queries OpenEMR's FHIR API using `_lastUpdated` cursor | Near real-time updates — when a clinician modifies a patient record, it syncs to HealthLake within minutes |
| **Daily Full (DB Sync)** | `openemr-healthlake-db-sync` | 2 AM daily | Direct MySQL queries against Aurora | Full reconciliation — catches any records missed by the incremental sync and ensures consistency |

**How it works:**

1. **FHIR API sync** — Uses OAuth2 to authenticate with OpenEMR's `/apis/default/fhir/` endpoint. Fetches resources that changed since the last cursor (stored in DynamoDB). Processes in batches of 500, paged at 100. Pushes each resource to HealthLake via SigV4-signed PUT.

2. **DB sync** — Queries `patient_data`, `form_encounter`, `lists`, `immunizations`, etc. directly. Transforms rows into FHIR R4 resources in Python. Pushes to HealthLake. Updates the cursor so the FHIR API sync picks up from the correct timestamp.

3. **HealthLake integrated analytics** — Once data is in HealthLake, it's automatically transformed into Apache Iceberg tables queryable via Athena (engine v3). Lake Formation governs access with role-based permissions.

## Repository Structure — Submodule Architecture

This project uses an **overlay pattern** with Git submodules. Upstream code lives in `submodules/` and is never modified directly. All hdademo customizations live outside of submodule directories.

### Upstream Submodule Code (do not modify)

| Directory | Upstream Repository | Purpose |
|-----------|-------------------|---------|
| `submodules/openemr-on-ecs/` | [openemr/openemr-on-ecs](https://github.com/openemr/openemr-on-ecs) | Base OpenEMR ECS deployment (stacks, configs, assets) |
| `submodules/modern-data-architecture-accelerator/` | [aws/modern-data-architecture-accelerator](https://github.com/aws/modern-data-architecture-accelerator) | AWS data architecture accelerator framework |

### hdademo Customizations (project-specific code)

| Directory / File | Purpose |
|------------------|---------|
| `infrastructure/` | Custom CDK stacks — OpenEMR, Orthanc, HealthLake, Patient Dashboard, Security |
| `lambda/` | Lambda functions — data loader, HealthLake sync, Orthanc sync |
| `sample_data/` | SQL scripts for loading sample clinical data |
| `patient-dashboard/` | React frontend for the Patient 360 Dashboard |
| `app.py`, `app_full_demo.py`, `app_orthanc.py`, `app_dashboard.py`, `app_healthlake_sync.py`, `app_healthlake_bulk_import.py` | CDK entry points (import from both submodule and local packages) |
| `path_setup.py` | Python path injection — makes submodule packages importable |
| `scripts/setup.sh` | Single-command project setup |

### Accelerator Submodule Integration

The primary integration point for the modern-data-architecture-accelerator submodule is:

```
submodules/modern-data-architecture-accelerator/sample_configs/health_data_accelerator/
```

This `health_data_accelerator` sample configuration provides the data architecture patterns and reference configurations that hdademo builds upon. The hdademo CDK stacks reference artifacts from this directory for:

- HealthLake datastore configuration patterns
- Data pipeline architecture patterns (Glue, Lambda)
- Sample FHIR resource definitions used as reference for the sync Lambda functions
- Infrastructure patterns for the patient dashboard backend

## Prerequisites

- AWS Account with appropriate permissions
- AWS CLI configured
- Python 3.11+
- Node.js 18+ and npm
- Docker (for Lambda bundling)
- AWS CDK (`npm install -g aws-cdk@2.1004.0` — pin the version for reproducible installs)
- Route53 hosted zone for custom domain names

## Quick Start

### 1. Clone the Repository

Clone with submodules initialized automatically:

```bash
git clone --recurse-submodules https://github.com/pwbamz/hdademo.git
cd hdademo
```

If you already cloned without `--recurse-submodules`, initialize submodules manually:

```bash
git submodule update --init --recursive
```

### 2. Setup Environment

**Option A: Automated setup (recommended)**

```bash
./scripts/setup.sh
source .venv/bin/activate
```

This script will:
- Initialize and update Git submodules
- Create a Python virtual environment (`.venv`)
- Install all Python dependencies from `requirements.txt`

**Option B: Manual setup**

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Build the Patient Dashboard
cd patient-dashboard
npm install
npm run build
cd ..
```

### 3. Bootstrap CDK (first time only)

```bash
cdk bootstrap
```

### 4. Configure Access

Edit `cdk.json` and set your IP address for security group access:

```json
{
  "context": {
    "security_group_ip_range_ipv4": "YOUR.IP.ADDRESS/32"
  }
}
```

**Optional: Custom Domain Configuration**

If you have a Route53 hosted zone and want custom domain names:

```json
{
  "context": {
    "route53_domain": "your-domain.com",
    "route53_hosted_zone_id": "Z0123456789ABCDEFGHIJ",
    "certificate_arn": "arn:aws:acm:us-east-1:ACCOUNT:certificate/xxx"
  }
}
```

### 5. Deploy OpenEMR Stack

```bash
cdk deploy
```

This deploys the main OpenEMR stack (~40 minutes). After deployment:
- Access OpenEMR at the URL in the output
- Username: `admin`
- Password: Found in AWS Secrets Manager

### 6. Load Sample Data (Optional)

Connect to the database and run the sample data scripts:

```bash
# Scripts are numbered 01-21 and create realistic patient data
./install_all_sample_data.sh
```

## Additional Stacks

The workshop includes additional stacks that can be deployed separately:

### Full Demo (All Components)
Deploy the complete healthcare platform with all components in one command:
```bash
cdk deploy --all --app "python app_full_demo.py"
```

This deploys:
- OpenEMR EHR system
- Orthanc PACS server
- HealthLake datastore with automatic sync
- Patient 360 Dashboard
- Combined data loader (auto-populates sample data)

### Orthanc PACS Server
```bash
# Edit app_orthanc.py with your configuration
cdk deploy --app "python app_orthanc.py"
```

### HealthLake Sync
```bash
# Edit app_healthlake_sync.py with your HealthLake datastore ID
cdk deploy --app "python app_healthlake_sync.py"
```

### Patient Dashboard
```bash
# Edit app_dashboard.py with your configuration  
cdk deploy --app "python app_dashboard.py"
```

## Updating Submodules

Each submodule can be updated independently to pull in upstream improvements. Follow this workflow:

### Update Workflow

```bash
# 1. Navigate to the submodule
cd submodules/openemr-on-ecs    # or submodules/modern-data-architecture-accelerator

# 2. Fetch latest upstream changes
git fetch origin

# 3. Checkout the desired commit or tag
git checkout <commit-sha-or-tag>

# 4. Return to the parent repository
cd ../..

# 5. Verify CDK synthesis still works
cdk synth --all

# 6. If synthesis succeeds, commit the updated reference
git add submodules/openemr-on-ecs
git commit -m "Update openemr-on-ecs submodule to <commit>"

# 7. If synthesis FAILS, revert the submodule to the previous commit
cd submodules/openemr-on-ecs
git checkout <previous-commit>
cd ../..
```

### If a Submodule Update Breaks CDK Synthesis

If `cdk synth --all` fails after updating a submodule:

1. **Revert** the submodule to its previous working commit:
   ```bash
   cd submodules/<name>
   git checkout <previous-commit>
   cd ../..
   ```
2. **Investigate** the breaking change — check the upstream changelog or diff for API changes that affect hdademo's imports or configurations.
3. **Adapt** hdademo overlay code (in `infrastructure/`, `lambda/`, etc.) to be compatible with the new upstream version.
4. **Re-attempt** the update once the overlay code is compatible.

Do not commit a submodule reference that causes `cdk synth` to fail.

## Sample Data

The `sample_data/` directory contains SQL scripts to populate OpenEMR with realistic sample data:

- **500 patients** with diverse demographics
- **Encounters, problems, allergies, medications**
- **Vital signs, lab results, clinical notes**
- **Billing codes, documents, patient messages**
- **~2,500+ total records**

See `sample_data/00_START_HERE.md` for details on the sample data.

## Workshop Modules

1. **Module 1**: Deploy OpenEMR on ECS Fargate
2. **Module 2**: Load sample patient data
3. **Module 3**: Deploy Orthanc PACS server
4. **Module 4**: Create HealthLake datastore and sync data
5. **Module 5**: Deploy Patient 360 Dashboard
6. **Module 6**: Explore the unified patient view

## Clean Up

```bash
# Destroy all stacks
cdk destroy --all

# Manual cleanup:
# - AWS Backup Vault
# - HealthLake datastore (if created)
# - S3 buckets with data
```

## Cost Estimate

Base infrastructure cost: ~$214/month

- ECS Fargate: ~$72/month
- Aurora Serverless v2: ~$40/month
- NAT Gateways: ~$66/month
- Load Balancer: ~$22/month
- Other services: ~$14/month

Costs scale with usage. See `DETAILS.md` for full breakdown.

## Security Notes

- All data encrypted at rest (KMS)
- VPC with private subnets for databases
- WAF protection on load balancers
- IP-restricted security groups
- Secrets stored in Secrets Manager

## Resources

- [OpenEMR Documentation](https://www.open-emr.org/wiki/index.php/OpenEMR_Wiki_Home_Page)
- [Orthanc Documentation](https://book.orthanc-server.com/)
- [AWS HealthLake](https://aws.amazon.com/healthlake/)
- [FHIR R4 Specification](https://hl7.org/fhir/R4/)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Disclaimer

This is a workshop/demo environment. For production healthcare deployments:
- Execute a Business Associate Agreement (BAA) with AWS
- Implement organizational security policies
- Conduct security audits and risk assessments
- Ensure HIPAA compliance requirements are met

### Responsible AI

The Patient 360 dashboard's AI-powered summaries and chat run on Amazon Bedrock and are provided **for informational and documentation support only — they are not medical advice, a diagnosis, or clinical decision support**:
- **Human-in-the-loop:** a qualified clinician must independently review and verify any AI-generated content before it is used to inform care. The feature does not make autonomous clinical decisions.
- **Guardrails:** Amazon Bedrock Guardrails are enabled by default (a denied "MedicalAdvice" topic, HIGH content filters for harmful content, and prompt-injection mitigation) as a defense-in-depth control layered over the model's fact-only prompts.
- The models summarize the patient's own record; output should always be validated against the source clinical data.

See [RESPONSIBLE_AI.md](RESPONSIBLE_AI.md) for the full responsible AI approach, limitations, and disclaimers.
