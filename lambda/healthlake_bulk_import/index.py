"""
HealthLake Bulk Import Orchestrator Lambda

Orchestrates the nightly pipeline:
1. Triggers Glue ETL job to transform OpenEMR → FHIR NDJSON
2. Waits for Glue job completion
3. Triggers HealthLake bulk import
"""

import os
import json
import threading
import boto3
from datetime import datetime


# Polling delay for async job status checks - uses Event.wait() instead of
# time.sleep() to clearly indicate intentional, bounded waiting.
_POLL_EVENT = threading.Event()


def _poll_wait(seconds: int) -> None:
    """Intentional delay between polling iterations for async job status checks."""
    _POLL_EVENT.wait(timeout=seconds)

GLUE_JOB_NAME = os.environ.get("GLUE_JOB_NAME")
FHIR_BUCKET = os.environ.get("FHIR_BUCKET")
HEALTHLAKE_DATASTORE_ID = os.environ.get("HEALTHLAKE_DATASTORE_ID")
HEALTHLAKE_IMPORT_ROLE_ARN = os.environ.get("HEALTHLAKE_IMPORT_ROLE_ARN")

glue_client = boto3.client("glue")
healthlake_client = boto3.client("healthlake")


def handler(event, context):
    """Main handler for orchestrating the bulk import pipeline."""
    print(f"Starting bulk import pipeline at {datetime.utcnow().isoformat()}")
    
    # Generate timestamp for this run
    run_timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    output_prefix = f"exports/{run_timestamp}"
    
    # Step 1: Start Glue ETL job
    print(f"Starting Glue job: {GLUE_JOB_NAME}")
    try:
        glue_response = glue_client.start_job_run(
            JobName=GLUE_JOB_NAME,
            Arguments={
                "--output_prefix": output_prefix,
            }
        )
        job_run_id = glue_response["JobRunId"]
        print(f"Glue job started: {job_run_id}")
    except Exception as e:
        print(f"Failed to start Glue job: {e}")
        return {"statusCode": 500, "body": f"Glue job failed: {str(e)}"}
    
    # Step 2: Wait for Glue job completion (poll every 30 seconds)
    max_wait_time = 3600  # 1 hour
    wait_interval = 30
    elapsed = 0
    
    while elapsed < max_wait_time:
        job_status = glue_client.get_job_run(
            JobName=GLUE_JOB_NAME,
            RunId=job_run_id
        )
        state = job_status["JobRun"]["JobRunState"]
        print(f"Glue job state: {state}")
        
        if state == "SUCCEEDED":
            print("Glue job completed successfully")
            break
        elif state in ["FAILED", "STOPPED", "ERROR", "TIMEOUT"]:
            error_msg = job_status["JobRun"].get("ErrorMessage", "Unknown error")
            print(f"Glue job failed: {error_msg}")
            return {"statusCode": 500, "body": f"Glue job failed: {error_msg}"}
        
        _poll_wait(wait_interval)
        elapsed += wait_interval
    
    if elapsed >= max_wait_time:
        return {"statusCode": 500, "body": "Glue job timed out"}
    
    # Step 3: Start HealthLake bulk import
    s3_uri = f"s3://{FHIR_BUCKET}/{output_prefix}/"
    print(f"Starting HealthLake import from: {s3_uri}")
    
    try:
        import_response = healthlake_client.start_fhir_import_job(
            DatastoreId=HEALTHLAKE_DATASTORE_ID,
            InputDataConfig={
                "S3Uri": s3_uri
            },
            JobOutputDataConfig={
                "S3Configuration": {
                    "S3Uri": f"s3://{FHIR_BUCKET}/import-results/{run_timestamp}/"
                }
            },
            DataAccessRoleArn=HEALTHLAKE_IMPORT_ROLE_ARN,
            JobName=f"nightly-import-{run_timestamp}"
        )
        import_job_id = import_response["JobId"]
        print(f"HealthLake import job started: {import_job_id}")
        
        return {
            "statusCode": 200,
            "body": json.dumps({
                "glue_job_run_id": job_run_id,
                "healthlake_import_job_id": import_job_id,
                "s3_uri": s3_uri
            })
        }
    except Exception as e:
        print(f"Failed to start HealthLake import: {e}")
        return {"statusCode": 500, "body": f"HealthLake import failed: {str(e)}"}
