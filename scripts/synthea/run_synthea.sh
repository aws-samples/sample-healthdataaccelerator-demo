#!/bin/bash
# =============================================================================
# Synthea Patient Data Generator for Healthcare Demo
# =============================================================================
# This script downloads and runs Synthea to generate synthetic patient data
# with clinical notes for loading into OpenEMR.
#
# Prerequisites:
# - Java 11 or higher installed
# - curl or wget available
#
# Usage:
#   ./run_synthea.sh [num_patients] [state]
#
# Examples:
#   ./run_synthea.sh 100 Massachusetts
#   ./run_synthea.sh 50 California
# =============================================================================

set -e

# Configuration
NUM_PATIENTS=${1:-100}
STATE=${2:-Massachusetts}
SYNTHEA_VERSION="v3.3.0"
SYNTHEA_JAR="synthea-with-dependencies.jar"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/output"
SYNTHEA_DIR="${SCRIPT_DIR}/synthea"

echo "=============================================="
echo "Synthea Patient Data Generator"
echo "=============================================="
echo "Patients to generate: ${NUM_PATIENTS}"
echo "State: ${STATE}"
echo "Output directory: ${OUTPUT_DIR}"
echo ""

# Check Java installation
if ! command -v java &> /dev/null; then
    echo "ERROR: Java is not installed. Please install Java 11 or higher."
    exit 1
fi

JAVA_VERSION=$(java -version 2>&1 | head -n 1 | cut -d'"' -f2 | cut -d'.' -f1)
echo "Java version: ${JAVA_VERSION}"

# Create directories
mkdir -p "${SYNTHEA_DIR}"
mkdir -p "${OUTPUT_DIR}"

# Download Synthea if not present
if [ ! -f "${SYNTHEA_DIR}/${SYNTHEA_JAR}" ]; then
    echo ""
    echo "Downloading Synthea ${SYNTHEA_VERSION}..."
    curl -L -o "${SYNTHEA_DIR}/${SYNTHEA_JAR}" \
        "https://github.com/synthetichealth/synthea/releases/download/${SYNTHEA_VERSION}/${SYNTHEA_JAR}"
    echo "Download complete."
fi

# Copy custom configuration
if [ -f "${SCRIPT_DIR}/synthea.properties" ]; then
    cp "${SCRIPT_DIR}/synthea.properties" "${SYNTHEA_DIR}/synthea.properties"
    echo "Using custom synthea.properties configuration."
fi

# Run Synthea
echo ""
echo "Generating ${NUM_PATIENTS} synthetic patients..."
echo "This may take several minutes depending on the number of patients."
echo ""

cd "${SYNTHEA_DIR}"

java -jar "${SYNTHEA_JAR}" \
    -p "${NUM_PATIENTS}" \
    -c synthea.properties \
    --exporter.baseDirectory "${OUTPUT_DIR}" \
    "${STATE}"

echo ""
echo "=============================================="
echo "Generation Complete!"
echo "=============================================="
echo ""
echo "Output files:"
echo "  FHIR Bundles: ${OUTPUT_DIR}/fhir/"
echo "  Clinical Notes: ${OUTPUT_DIR}/notes/"
echo "  Text Records: ${OUTPUT_DIR}/text/"
echo ""

# Count generated files
FHIR_COUNT=$(find "${OUTPUT_DIR}/fhir" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
NOTES_COUNT=$(find "${OUTPUT_DIR}/notes" -name "*.txt" 2>/dev/null | wc -l | tr -d ' ')

echo "Generated files:"
echo "  FHIR bundles: ${FHIR_COUNT}"
echo "  Clinical notes: ${NOTES_COUNT}"
echo ""
echo "Next steps:"
echo "  1. Run the data loader script to import into OpenEMR"
echo "  2. Verify data in OpenEMR web interface"
