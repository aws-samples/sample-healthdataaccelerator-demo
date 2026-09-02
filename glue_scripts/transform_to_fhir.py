"""
Glue ETL Script: Transform OpenEMR data to FHIR NDJSON format

Reads from OpenEMR tables in Glue Data Catalog and writes FHIR resources
as NDJSON files to S3 for HealthLake bulk import.

PHI / HIPAA NOTICE:
This script transforms and writes protected health information (PHI) - patient
names, dates of birth, addresses, phone numbers, conditions, medications,
allergies, and immunizations - to S3 as FHIR NDJSON. If you process real PHI,
this is a HIPAA-regulated workload: execute an AWS Business Associate Addendum
(BAA), keep data within HIPAA-eligible services, and enable encryption, access
logging, and audit controls. The customer is responsible for compliant handling
of regulated data. This sample ships with synthetic data only.
"""

import sys
import json
from datetime import datetime
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import col, lit, concat, when, coalesce, struct, array, to_json, udf
from pyspark.sql.types import StringType


def format_uuid_with_hyphens(uuid_str):
    """Convert UUID without hyphens to standard format with hyphens.
    
    Example: a0d21f938be645acad07af4b85f257e0 -> a0d21f93-8be6-45ac-ad07-af4b85f257e0
    """
    if uuid_str is None:
        return None
    # Remove any existing hyphens first
    clean = uuid_str.replace("-", "")
    if len(clean) != 32:
        return uuid_str  # Return as-is if not a valid UUID length
    # Insert hyphens at standard positions: 8-4-4-4-12
    return f"{clean[:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:]}"


format_uuid_udf = udf(format_uuid_with_hyphens, StringType())

# Get job parameters
args = getResolvedOptions(sys.argv, [
    'JOB_NAME',
    'source_database',
    'output_bucket',
    'output_prefix'
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

SOURCE_DB = args['source_database']
OUTPUT_BUCKET = args['output_bucket']
OUTPUT_PREFIX = args.get('output_prefix', f"exports/{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}")


def write_ndjson(df, resource_type):
    """Write DataFrame as NDJSON file to S3."""
    output_path = f"s3://{OUTPUT_BUCKET}/{OUTPUT_PREFIX}/{resource_type}.ndjson"
    
    # Convert to JSON strings and write as text
    df.select(to_json(struct("*")).alias("json_str")) \
      .write \
      .mode("overwrite") \
      .text(output_path)
    
    print(f"Wrote {df.count()} {resource_type} resources to {output_path}")


def transform_patients():
    """Transform patient_data to FHIR Patient resources."""
    print("Transforming Patient resources...")
    
    # Read patient data
    patient_df = glueContext.create_dynamic_frame.from_catalog(
        database=SOURCE_DB,
        table_name="patient_data"
    ).toDF()
    
    # Map to FHIR Patient structure
    # Use format_uuid_udf to ensure UUIDs have hyphens (matches OpenEMR FHIR API format)
    fhir_patients = patient_df.select(
        lit("Patient").alias("resourceType"),
        format_uuid_udf(col("uuid")).alias("id"),
        struct(
            lit(datetime.utcnow().isoformat() + "Z").alias("lastUpdated")
        ).alias("meta"),
        array(
            struct(
                lit("official").alias("use"),
                col("fname").alias("given"),
                col("lname").alias("family")
            )
        ).alias("name"),
        when(col("sex") == "Male", "male")
            .when(col("sex") == "Female", "female")
            .otherwise("unknown").alias("gender"),
        col("DOB").cast("string").alias("birthDate"),
        array(
            struct(
                array(col("street")).alias("line"),
                col("city").alias("city"),
                col("state").alias("state"),
                col("postal_code").alias("postalCode"),
                col("country_code").alias("country")
            )
        ).alias("address"),
        array(
            struct(
                lit("phone").alias("system"),
                col("phone_home").alias("value"),
                lit("home").alias("use")
            )
        ).alias("telecom"),
    ).filter(col("id").isNotNull())
    
    write_ndjson(fhir_patients, "Patient")
    return fhir_patients.count()


def transform_encounters():
    """Transform form_encounter to FHIR Encounter resources."""
    print("Transforming Encounter resources...")
    
    encounter_df = glueContext.create_dynamic_frame.from_catalog(
        database=SOURCE_DB,
        table_name="form_encounter"
    ).toDF()
    
    fhir_encounters = encounter_df.select(
        lit("Encounter").alias("resourceType"),
        format_uuid_udf(col("uuid")).alias("id"),
        struct(
            lit(datetime.utcnow().isoformat() + "Z").alias("lastUpdated")
        ).alias("meta"),
        lit("finished").alias("status"),
        struct(
            struct(
                lit("http://terminology.hl7.org/CodeSystem/v3-ActCode").alias("system"),
                lit("AMB").alias("code"),
                lit("ambulatory").alias("display")
            ).alias("coding")
        ).alias("class"),
        struct(
            concat(lit("Patient/"), format_uuid_udf(col("pid"))).alias("reference")
        ).alias("subject"),
        struct(
            col("date").cast("string").alias("start")
        ).alias("period"),
        col("reason").alias("reasonCode"),
    ).filter(col("id").isNotNull())
    
    write_ndjson(fhir_encounters, "Encounter")
    return fhir_encounters.count()


def transform_conditions():
    """Transform lists (medical problems) to FHIR Condition resources."""
    print("Transforming Condition resources...")
    
    # Lists table contains medical problems where type='medical_problem'
    lists_df = glueContext.create_dynamic_frame.from_catalog(
        database=SOURCE_DB,
        table_name="lists"
    ).toDF()
    
    conditions_df = lists_df.filter(col("type") == "medical_problem")
    
    fhir_conditions = conditions_df.select(
        lit("Condition").alias("resourceType"),
        format_uuid_udf(col("uuid")).alias("id"),
        struct(
            lit(datetime.utcnow().isoformat() + "Z").alias("lastUpdated")
        ).alias("meta"),
        struct(
            struct(
                lit("http://terminology.hl7.org/CodeSystem/condition-clinical").alias("system"),
                when(col("enddate").isNull(), "active").otherwise("resolved").alias("code")
            ).alias("coding")
        ).alias("clinicalStatus"),
        struct(
            array(
                struct(
                    col("diagnosis").alias("system"),
                    col("diagnosis").alias("code"),
                    col("title").alias("display")
                )
            ).alias("coding"),
            col("title").alias("text")
        ).alias("code"),
        struct(
            concat(lit("Patient/"), format_uuid_udf(col("pid"))).alias("reference")
        ).alias("subject"),
        col("begdate").cast("string").alias("onsetDateTime"),
        col("enddate").cast("string").alias("abatementDateTime"),
    ).filter(col("id").isNotNull())
    
    write_ndjson(fhir_conditions, "Condition")
    return fhir_conditions.count()


def transform_allergies():
    """Transform lists (allergies) to FHIR AllergyIntolerance resources."""
    print("Transforming AllergyIntolerance resources...")
    
    lists_df = glueContext.create_dynamic_frame.from_catalog(
        database=SOURCE_DB,
        table_name="lists"
    ).toDF()
    
    allergies_df = lists_df.filter(col("type") == "allergy")
    
    fhir_allergies = allergies_df.select(
        lit("AllergyIntolerance").alias("resourceType"),
        format_uuid_udf(col("uuid")).alias("id"),
        struct(
            lit(datetime.utcnow().isoformat() + "Z").alias("lastUpdated")
        ).alias("meta"),
        struct(
            struct(
                lit("http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical").alias("system"),
                when(col("enddate").isNull(), "active").otherwise("resolved").alias("code")
            ).alias("coding")
        ).alias("clinicalStatus"),
        struct(
            col("title").alias("text")
        ).alias("code"),
        struct(
            concat(lit("Patient/"), format_uuid_udf(col("pid"))).alias("reference")
        ).alias("patient"),
        col("begdate").cast("string").alias("onsetDateTime"),
    ).filter(col("id").isNotNull())
    
    write_ndjson(fhir_allergies, "AllergyIntolerance")
    return fhir_allergies.count()


def transform_medications():
    """Transform prescriptions to FHIR MedicationRequest resources."""
    print("Transforming MedicationRequest resources...")
    
    rx_df = glueContext.create_dynamic_frame.from_catalog(
        database=SOURCE_DB,
        table_name="prescriptions"
    ).toDF()
    
    fhir_medications = rx_df.select(
        lit("MedicationRequest").alias("resourceType"),
        format_uuid_udf(col("uuid")).alias("id"),
        struct(
            lit(datetime.utcnow().isoformat() + "Z").alias("lastUpdated")
        ).alias("meta"),
        when(col("active") == 1, "active").otherwise("stopped").alias("status"),
        lit("order").alias("intent"),
        struct(
            col("drug").alias("text")
        ).alias("medicationCodeableConcept"),
        struct(
            concat(lit("Patient/"), format_uuid_udf(col("patient_id"))).alias("reference")
        ).alias("subject"),
        col("date_added").cast("string").alias("authoredOn"),
        array(
            struct(
                col("dosage").alias("text")
            )
        ).alias("dosageInstruction"),
    ).filter(col("id").isNotNull())
    
    write_ndjson(fhir_medications, "MedicationRequest")
    return fhir_medications.count()


def transform_immunizations():
    """Transform immunizations to FHIR Immunization resources."""
    print("Transforming Immunization resources...")
    
    imm_df = glueContext.create_dynamic_frame.from_catalog(
        database=SOURCE_DB,
        table_name="immunizations"
    ).toDF()
    
    fhir_immunizations = imm_df.select(
        lit("Immunization").alias("resourceType"),
        format_uuid_udf(col("uuid")).alias("id"),
        struct(
            lit(datetime.utcnow().isoformat() + "Z").alias("lastUpdated")
        ).alias("meta"),
        lit("completed").alias("status"),
        struct(
            col("cvx_code").alias("text")
        ).alias("vaccineCode"),
        struct(
            concat(lit("Patient/"), format_uuid_udf(col("patient_id"))).alias("reference")
        ).alias("patient"),
        col("administered_date").cast("string").alias("occurrenceDateTime"),
    ).filter(col("id").isNotNull())
    
    write_ndjson(fhir_immunizations, "Immunization")
    return fhir_immunizations.count()


def transform_observations():
    """Transform form_vitals to FHIR Observation resources."""
    print("Transforming Observation resources...")
    
    vitals_df = glueContext.create_dynamic_frame.from_catalog(
        database=SOURCE_DB,
        table_name="form_vitals"
    ).toDF()
    
    # Create separate observations for each vital sign
    # Blood Pressure
    bp_obs = vitals_df.filter(col("bps").isNotNull()).select(
        lit("Observation").alias("resourceType"),
        concat(format_uuid_udf(col("uuid")), lit("-bp")).alias("id"),
        struct(
            lit(datetime.utcnow().isoformat() + "Z").alias("lastUpdated")
        ).alias("meta"),
        lit("final").alias("status"),
        struct(
            array(
                struct(
                    lit("http://loinc.org").alias("system"),
                    lit("85354-9").alias("code"),
                    lit("Blood pressure panel").alias("display")
                )
            ).alias("coding")
        ).alias("code"),
        struct(
            concat(lit("Patient/"), format_uuid_udf(col("pid"))).alias("reference")
        ).alias("subject"),
        col("date").cast("string").alias("effectiveDateTime"),
        array(
            struct(
                struct(
                    array(struct(
                        lit("http://loinc.org").alias("system"),
                        lit("8480-6").alias("code"),
                        lit("Systolic blood pressure").alias("display")
                    )).alias("coding")
                ).alias("code"),
                struct(
                    col("bps").cast("float").alias("value"),
                    lit("mmHg").alias("unit"),
                    lit("http://unitsofmeasure.org").alias("system"),
                    lit("mm[Hg]").alias("code")
                ).alias("valueQuantity")
            ),
            struct(
                struct(
                    array(struct(
                        lit("http://loinc.org").alias("system"),
                        lit("8462-4").alias("code"),
                        lit("Diastolic blood pressure").alias("display")
                    )).alias("coding")
                ).alias("code"),
                struct(
                    col("bpd").cast("float").alias("value"),
                    lit("mmHg").alias("unit"),
                    lit("http://unitsofmeasure.org").alias("system"),
                    lit("mm[Hg]").alias("code")
                ).alias("valueQuantity")
            )
        ).alias("component")
    )
    
    write_ndjson(bp_obs, "Observation")
    return bp_obs.count()


# Main execution
print(f"Starting FHIR transformation from {SOURCE_DB}")
print(f"Output: s3://{OUTPUT_BUCKET}/{OUTPUT_PREFIX}/")

results = {}
results["Patient"] = transform_patients()
results["Encounter"] = transform_encounters()
results["Condition"] = transform_conditions()
results["AllergyIntolerance"] = transform_allergies()
results["MedicationRequest"] = transform_medications()
results["Immunization"] = transform_immunizations()
results["Observation"] = transform_observations()

print(f"Transformation complete: {json.dumps(results)}")

job.commit()
