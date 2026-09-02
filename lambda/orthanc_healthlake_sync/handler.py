"""
Orthanc to HealthLake Sync Lambda

Syncs DICOM imaging studies from Orthanc to AWS HealthLake as FHIR ImagingStudy resources.

HIPAA / PHI notice:
    This function processes FHIR ImagingStudy resources that include patient
    identifiers and DICOM study metadata. When used with real data this is a
    HIPAA-regulated workload. You are responsible for executing an AWS Business
    Associate Addendum (BAA), enabling encryption in transit and at rest, and
    applying least-privilege access controls. This sample ships with synthetic
    data only and is not intended for production use as-is.
"""

import json
import os
import boto3
import requests
from datetime import datetime
from urllib.parse import quote
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from requests.auth import HTTPBasicAuth

# Configuration
ORTHANC_URL = os.environ.get('ORTHANC_URL')
HEALTHLAKE_ENDPOINT = os.environ.get('HEALTHLAKE_ENDPOINT')
HEALTHLAKE_DATASTORE_ID = os.environ.get('HEALTHLAKE_DATASTORE_ID')
SYNC_STATE_TABLE = os.environ.get('SYNC_STATE_TABLE', 'orthanc-healthlake-sync-state')
ORTHANC_CREDENTIALS_SECRET_ARN = os.environ.get('ORTHANC_CREDENTIALS_SECRET_ARN')

dynamodb = boto3.resource('dynamodb')
session = boto3.Session()


def _load_orthanc_credentials():
    """Load Orthanc credentials.

    Prefers the Secrets Manager secret referenced by
    ORTHANC_CREDENTIALS_SECRET_ARN. Falls back to ORTHANC_USERNAME /
    ORTHANC_PASSWORD environment variables (useful for local development).
    No credentials are hardcoded.
    """
    username = os.environ.get('ORTHANC_USERNAME', 'admin')
    password = os.environ.get('ORTHANC_PASSWORD')

    if ORTHANC_CREDENTIALS_SECRET_ARN:
        try:
            sm = boto3.client('secretsmanager')
            secret_value = sm.get_secret_value(SecretId=ORTHANC_CREDENTIALS_SECRET_ARN)
            creds = json.loads(secret_value['SecretString'])
            username = creds.get('username', username)
            password = creds.get('password', password)
        except Exception as exc:  # noqa: BLE001 - log and surface config error
            print(f"ERROR: could not load Orthanc credentials from Secrets Manager: {exc}")
            raise

    if not password:
        raise RuntimeError(
            "Orthanc password not configured: set ORTHANC_CREDENTIALS_SECRET_ARN "
            "or ORTHANC_PASSWORD"
        )
    return username, password


ORTHANC_USERNAME, ORTHANC_PASSWORD = _load_orthanc_credentials()


def get_last_sync_time():
    """Get the last sync timestamp from DynamoDB."""
    try:
        table = dynamodb.Table(SYNC_STATE_TABLE)
        response = table.get_item(Key={'resource_type': 'ImagingStudy'})
        if 'Item' in response:
            return response['Item']['last_sync']
    except Exception as e:
        print(f"Error getting last sync time: {e}")
    return '2020-01-01T00:00:00Z'


def update_last_sync_time(sync_time):
    """Update the last sync timestamp in DynamoDB."""
    try:
        table = dynamodb.Table(SYNC_STATE_TABLE)
        table.put_item(Item={
            'resource_type': 'ImagingStudy',
            'last_sync': sync_time,
            'updated_at': datetime.utcnow().isoformat()
        })
    except Exception as e:
        print(f"Error updating last sync time: {e}")


def query_orthanc_studies():
    """Query Orthanc for all imaging studies."""
    studies = []
    auth = HTTPBasicAuth(ORTHANC_USERNAME, ORTHANC_PASSWORD)
    
    try:
        # Get all study IDs from Orthanc
        studies_url = f"{ORTHANC_URL}/studies"
        response = requests.get(studies_url, auth=auth, timeout=30)
        
        if response.status_code == 200:
            study_ids = response.json()
            
            for study_id in study_ids:
                # Get detailed study info
                study_url = f"{ORTHANC_URL}/studies/{study_id}"
                study_response = requests.get(study_url, auth=auth, timeout=30)
                
                if study_response.status_code == 200:
                    study_data = study_response.json()
                    main_tags = study_data.get("MainDicomTags", {})
                    patient_tags = study_data.get("PatientMainDicomTags", {})
                    
                    # Get series info for modality
                    modality = "OT"  # Default
                    series_ids = study_data.get("Series", [])
                    if series_ids:
                        series_url = f"{ORTHANC_URL}/series/{series_ids[0]}"
                        series_response = requests.get(series_url, auth=auth, timeout=30)
                        if series_response.status_code == 200:
                            series_data = series_response.json()
                            series_tags = series_data.get("MainDicomTags", {})
                            modality = series_tags.get("Modality", "OT")
                    
                    study = {
                        "study_uid": main_tags.get("StudyInstanceUID"),
                        "orthanc_id": study_id,
                        "patient_id": patient_tags.get("PatientID"),
                        "patient_name": patient_tags.get("PatientName"),
                        "study_date": main_tags.get("StudyDate"),
                        "study_time": main_tags.get("StudyTime"),
                        "study_description": main_tags.get("StudyDescription"),
                        "accession_number": main_tags.get("AccessionNumber"),
                        "modality": modality,
                        "series_count": len(series_ids),
                        "instance_count": study_data.get("Statistics", {}).get("CountInstances", 1),
                    }
                    studies.append(study)
                    
        else:
            # Log status only. Orthanc query responses can contain PHI
            # (patient names/IDs, study metadata); do not log the body.
            print(f"Orthanc query failed: {response.status_code}")
            
    except Exception as e:
        print(f"Error querying Orthanc: {e}")
    
    return studies


def resolve_patient_reference(study):
    """Resolve the DICOM patient to a HealthLake Patient FHIR ID by matching name."""
    patient_name = study.get("patient_name", "")
    
    if not patient_name:
        return None
    
    # DICOM name format is LastName^FirstName
    name_parts = patient_name.split("^")
    family = name_parts[0] if name_parts else ""
    given = name_parts[1] if len(name_parts) > 1 else ""
    
    if not family:
        return None
    
    # Search HealthLake for patient by name using SigV4 (same approach as send_to_healthlake)
    try:
        # Sanitize patient name parameters to prevent SSRF
        safe_family = quote(family, safe='')
        safe_given = quote(given, safe='')
        if family and given:
            search_url = f"{HEALTHLAKE_ENDPOINT}Patient?family={safe_family}&given={safe_given}&_count=1"
        else:
            search_url = f"{HEALTHLAKE_ENDPOINT}Patient?name={safe_family}&_count=1"
        
        headers = {
            'Accept': 'application/fhir+json'
        }
        
        request = AWSRequest(method='GET', url=search_url, headers=headers)
        credentials = session.get_credentials()
        SigV4Auth(credentials, 'healthlake', 'us-east-1').add_auth(request)
        
        response = requests.get(
            search_url,
            headers=dict(request.headers),
            timeout=30
        )
        response.raise_for_status()
        
        bundle = response.json()
        if bundle.get('entry'):
            patient_id = bundle['entry'][0]['resource']['id']
            return patient_id
    except Exception as e:
        # Do not log patient_name (PHI). Log only the error.
        print(f"Error resolving patient: {e}")
    
    return None


# Cache for patient ID lookups to avoid repeated API calls
_patient_cache = {}


def convert_to_fhir_imaging_study(study):
    """Convert Orthanc study to FHIR ImagingStudy resource."""
    
    # Parse study date
    study_date = study.get("study_date", "")
    study_time = study.get("study_time", "")
    
    if study_date:
        # Convert YYYYMMDD to YYYY-MM-DD
        if len(study_date) == 8:
            study_date = f"{study_date[:4]}-{study_date[4:6]}-{study_date[6:8]}"
        if study_time and len(study_time) >= 6:
            study_date = f"{study_date}T{study_time[:2]}:{study_time[2:4]}:{study_time[4:6]}Z"
    
    # Resolve patient reference - look up actual HealthLake Patient by name
    patient_ref = study['patient_id']  # Default to Orthanc ID
    cache_key = study.get('patient_name', study['patient_id'])
    
    if cache_key in _patient_cache:
        patient_ref = _patient_cache[cache_key]
    else:
        resolved_id = resolve_patient_reference(study)
        if resolved_id:
            patient_ref = resolved_id
            _patient_cache[cache_key] = resolved_id
            # Avoid logging patient names (PHI). Log only the resolved reference.
            print(f"Resolved patient study_uid={study.get('study_uid')} -> {resolved_id}")
        else:
            _patient_cache[cache_key] = patient_ref
            if len(_patient_cache) <= 3:  # Only log first few failures
                # Do not log patient name/id (PHI); log the study UID instead.
                print(f"Could not resolve patient for study_uid={study.get('study_uid')}")
    
    # Build FHIR ImagingStudy resource
    imaging_study = {
        "resourceType": "ImagingStudy",
        "id": study["study_uid"].replace(".", "-"),  # FHIR IDs can't have dots
        "identifier": [
            {
                "system": "urn:dicom:uid",
                "value": f"urn:oid:{study['study_uid']}"
            }
        ],
        "status": "available",
        "subject": {
            "reference": f"Patient/{patient_ref}"
        },
        "started": study_date if study_date else None,
        "numberOfSeries": study.get("series_count", 1),
        "numberOfInstances": study.get("instance_count", 1),
        "description": study.get("study_description"),
        "series": [
            {
                "uid": study["study_uid"],
                "modality": {
                    "system": "http://dicom.nema.org/resources/ontology/DCM",
                    "code": study.get("modality", "OT")
                },
                "bodySite": {
                    "display": study.get("body_part")
                } if study.get("body_part") else None,
                "numberOfInstances": study.get("instance_count", 1)
            }
        ]
    }
    
    # Add accession number if present
    if study.get("accession_number"):
        imaging_study["identifier"].append({
            "type": {
                "coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                    "code": "ACSN"
                }]
            },
            "value": study["accession_number"]
        })
    
    # Add location/institution
    if study.get("institution"):
        imaging_study["location"] = {
            "display": study["institution"]
        }
    
    # Remove None values
    imaging_study = {k: v for k, v in imaging_study.items() if v is not None}
    if imaging_study.get("series"):
        imaging_study["series"] = [
            {k: v for k, v in s.items() if v is not None}
            for s in imaging_study["series"]
        ]
    
    return imaging_study


def send_to_healthlake(resource):
    """Send a FHIR resource to HealthLake using SigV4 authentication."""
    resource_type = resource.get('resourceType')
    resource_id = resource.get('id')
    
    url = f"{HEALTHLAKE_ENDPOINT}{resource_type}/{resource_id}"
    
    headers = {
        'Content-Type': 'application/fhir+json',
        'Accept': 'application/fhir+json'
    }
    
    body = json.dumps(resource)
    
    # Sign the request with SigV4
    request = AWSRequest(method='PUT', url=url, data=body, headers=headers)
    credentials = session.get_credentials()
    SigV4Auth(credentials, 'healthlake', 'us-east-1').add_auth(request)
    
    response = requests.put(
        url,
        data=body,
        headers=dict(request.headers),
        timeout=30
    )
    response.raise_for_status()
    
    return response.status_code, response.text


def handler(event, context):
    """Lambda handler for Orthanc to HealthLake sync."""
    global _patient_cache
    _patient_cache = {}  # Clear cache on each invocation
    print(f"Starting Orthanc to HealthLake sync at {datetime.utcnow().isoformat()}")
    
    sync_start_time = datetime.utcnow().isoformat() + 'Z'
    
    # Query Orthanc for studies
    print("Querying Orthanc for imaging studies...")
    studies = query_orthanc_studies()
    print(f"Found {len(studies)} studies in Orthanc")
    
    results = {
        'synced': 0,
        'failed': 0,
        'studies': []
    }
    
    for study in studies:
        try:
            # Convert to FHIR
            fhir_resource = convert_to_fhir_imaging_study(study)
            
            # Send to HealthLake
            status_code, response_text = send_to_healthlake(fhir_resource)
            
            if status_code in [200, 201]:
                results['synced'] += 1
                # Do not include patient_id (PHI) in the returned summary.
                results['studies'].append({
                    'study_uid': study['study_uid'],
                    'status': 'success'
                })
            else:
                results['failed'] += 1
                # Log the status code only, not the response body (may echo PHI).
                print(f"Failed to sync study {study['study_uid']}: {status_code}")
                # Do not include patient_id (PHI) or raw response text in the summary.
                results['studies'].append({
                    'study_uid': study['study_uid'],
                    'status': 'failed'
                })
                
        except Exception as e:
            results['failed'] += 1
            print(f"Error processing study {study.get('study_uid')}: {e}")
    
    # Update last sync time
    if results['synced'] > 0:
        update_last_sync_time(sync_start_time)
    
    print(f"Sync complete. Synced: {results['synced']}, Failed: {results['failed']}")
    
    return {
        'statusCode': 200,
        'body': json.dumps(results)
    }
