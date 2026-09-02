# Synthea Patient Data Generation

This directory contains scripts for generating synthetic patient data using Synthea and loading it into OpenEMR.

## Overview

[Synthea](https://github.com/synthetichealth/synthea) is an open-source synthetic patient generator that creates realistic (but not real) patient records in various formats including FHIR R4.

## Prerequisites

- Java 11 or higher
- Python 3.11+
- MySQL client (for database access)
- AWS CLI configured (for Secrets Manager access)

## Quick Start

### 1. Generate Synthetic Patients

```bash
# Generate 100 patients from Massachusetts
./run_synthea.sh 100 Massachusetts

# Generate 50 patients from California
./run_synthea.sh 50 California
```

This will:
- Download Synthea if not already present
- Generate FHIR R4 bundles in `output/fhir/`
- Generate clinical notes in `output/notes/`
- Generate text records in `output/text/`

### 2. Load Data into OpenEMR

```bash
# Using AWS Secrets Manager for database credentials
python load_synthea_to_openemr.py \
    --fhir-dir ./output/fhir \
    --notes-dir ./output/notes \
    --db-secret "OpenemrEcsStack-db-secret-XXXXX" \
    --region us-east-1

# Or using direct database credentials
python load_synthea_to_openemr.py \
    --fhir-dir ./output/fhir \
    --notes-dir ./output/notes \
    --db-host your-aurora-endpoint.rds.amazonaws.com \
    --db-user dbadmin \
    --db-password your-password
```

## Configuration

### synthea.properties

The `synthea.properties` file configures Synthea's output:

| Setting | Description |
|---------|-------------|
| `exporter.fhir_r4.export = true` | Enable FHIR R4 output |
| `exporter.clinical_note.export = true` | Enable clinical notes |
| `exporter.text.export = true` | Enable text records |
| `exporter.text.per_encounter_export = true` | One text file per encounter |

### Data Generated

Synthea generates comprehensive patient records including:

- **Demographics**: Name, DOB, gender, address, phone, email
- **Conditions**: Medical problems with SNOMED codes
- **Allergies**: Drug and environmental allergies
- **Medications**: Prescriptions with RxNorm codes
- **Immunizations**: Vaccines with CVX codes
- **Encounters**: Office visits, emergency, inpatient
- **Observations**: Vital signs, lab results
- **Clinical Notes**: Unstructured doctor's notes

## OpenEMR Data Mapping

| FHIR Resource | OpenEMR Table |
|---------------|---------------|
| Patient | patient_data |
| Condition | lists (type='medical_problem') |
| AllergyIntolerance | lists (type='allergy') |
| MedicationRequest | lists (type='medication') |
| Encounter | form_encounter |
| Immunization | immunizations |
| Observation (vitals) | form_vitals |
| Observation (labs) | procedure_result |
| DocumentReference | form_clinical_notes |

## Troubleshooting

### Java Not Found
```bash
# macOS
brew install openjdk@11

# Ubuntu
sudo apt install openjdk-11-jdk
```

### Database Connection Failed
1. Ensure you're connected to the VPC (use AWS Session Manager or bastion host)
2. Verify security group allows your IP
3. Check database credentials in Secrets Manager

### SSL Certificate Error
Download the RDS CA bundle:
```bash
curl -o /tmp/rds-ca-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
```

## Files

| File | Description |
|------|-------------|
| `run_synthea.sh` | Downloads and runs Synthea |
| `synthea.properties` | Synthea configuration |
| `load_synthea_to_openemr.py` | Loads FHIR data into OpenEMR |
| `output/` | Generated data (created by run_synthea.sh) |

## References

- [Synthea GitHub](https://github.com/synthetichealth/synthea)
- [Synthea Wiki](https://github.com/synthetichealth/synthea/wiki)
- [FHIR R4 Specification](https://hl7.org/fhir/R4/)
- [OpenEMR Database Schema](https://www.open-emr.org/wiki/index.php/Database_Structure)
