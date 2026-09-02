"""
OpenEMR Direct Database Sync Lambda

Performs daily full sync of all FHIR resources by querying the OpenEMR database
directly and transforming to FHIR format. Stores the last synced timestamp for
each resource type so the incremental FHIR sync can pick up from there.

Runs once daily (e.g., 2 AM) via EventBridge schedule.
"""

import json
import os
import boto3
import pymysql
from datetime import datetime
from decimal import Decimal
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
HEALTHLAKE_ENDPOINT = os.environ.get('HEALTHLAKE_ENDPOINT')
HEALTHLAKE_DATASTORE_ID = os.environ.get('HEALTHLAKE_DATASTORE_ID')
SYNC_STATE_TABLE = os.environ.get('SYNC_STATE_TABLE', 'openemr-healthlake-sync-state')
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
BATCH_SIZE = 500
CONCURRENT_REQUESTS = 20  # Number of parallel requests to HealthLake

dynamodb = boto3.resource('dynamodb')
secrets_client = boto3.client('secretsmanager')
session = boto3.Session()

_db_credentials = None


def format_uuid(uuid_value):
    """Format UUID with hyphens to match OpenEMR FHIR API format.
    
    MySQL stores UUIDs as binary(16), and .hex() returns them without hyphens.
    This function ensures consistent UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    """
    if uuid_value is None:
        return None
    
    # Convert bytes to hex string if needed
    if isinstance(uuid_value, bytes):
        hex_str = uuid_value.hex()
    else:
        hex_str = str(uuid_value).replace('-', '')
    
    # Ensure it's 32 hex characters
    if len(hex_str) != 32:
        return str(uuid_value)  # Return as-is if not a valid UUID
    
    # Insert hyphens at standard positions: 8-4-4-4-12
    return f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:]}"


def safe_isoformat(value):
    """Safely convert a datetime or string to ISO format string.
    
    Handles cases where the database returns either a datetime object or a string.
    Also handles invalid dates like '0000-00-00' which MySQL uses for NULL dates.
    """
    if value is None:
        return None
    
    # Handle string values
    if isinstance(value, str):
        # Check for invalid MySQL dates
        if value.startswith('0000-00-00') or value == '':
            return None
        # Already a string, return as-is (possibly add timezone if missing)
        if '+' not in value and 'Z' not in value:
            return value + '+00:00'
        return value
    
    # Handle datetime objects
    if hasattr(value, 'isoformat'):
        # Check for invalid dates (year 0 or very old dates)
        if hasattr(value, 'year') and value.year < 1900:
            return None
        return value.isoformat() + '+00:00'
    
    return str(value)


def ts_greater(a, b):
    """Compare two timestamps that may be datetime objects or strings.
    
    Converts both to strings for safe comparison, handling mixed types from MySQL.
    """
    if a is None:
        return False
    if b is None:
        return True
    a_str = a.isoformat() if hasattr(a, 'isoformat') else str(a)
    b_str = b.isoformat() if hasattr(b, 'isoformat') else str(b)
    return a_str > b_str


def get_db_credentials():
    """Get database credentials from Secrets Manager."""
    global _db_credentials
    if _db_credentials is None:
        response = secrets_client.get_secret_value(SecretId=DB_SECRET_ARN)
        _db_credentials = json.loads(response['SecretString'])
    return _db_credentials


def get_db_connection():
    """Create database connection with TLS server-certificate verification."""
    creds = get_db_credentials()

    # Verify the Aurora/RDS server certificate instead of disabling TLS
    # verification (previously CERT_NONE, which allowed MITM). If the Amazon
    # RDS CA bundle is packaged (RDS_CA_BUNDLE), verify against it; otherwise
    # fall back to the system trust store. Either way verification stays ON.
    import ssl
    ca_bundle = os.environ.get("RDS_CA_BUNDLE")
    if ca_bundle and os.path.exists(ca_bundle):
        ssl_context = ssl.create_default_context(cafile=ca_bundle)
    else:
        ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = True
    ssl_context.verify_mode = ssl.CERT_REQUIRED

    return pymysql.connect(
        host=creds['host'],
        user=creds['username'],
        password=creds['password'],
        database='openemr',
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=30,
        read_timeout=300,
        ssl=ssl_context
    )


def get_sync_cursor(resource_type):
    """Get the last sync cursor for incremental sync."""
    try:
        table = dynamodb.Table(SYNC_STATE_TABLE)
        response = table.get_item(Key={'resource_type': resource_type})
        if 'Item' in response:
            return response['Item'].get('cursor')
    except Exception as e:
        print(f"Error getting sync cursor: {e}")
    return None


def update_sync_cursor(resource_type, cursor_value):
    """Update the sync cursor for FHIR incremental sync to use."""
    try:
        table = dynamodb.Table(SYNC_STATE_TABLE)
        # Use update_item with expression attribute names for reserved keyword
        table.update_item(
            Key={'resource_type': resource_type},
            UpdateExpression='SET #c = :c, last_sync = :ls, updated_at = :ua, sync_source = :ss',
            ExpressionAttributeNames={'#c': 'cursor'},
            ExpressionAttributeValues={
                ':c': cursor_value,
                ':ls': datetime.utcnow().isoformat() + 'Z',
                ':ua': datetime.utcnow().isoformat(),
                ':ss': 'db_sync'
            }
        )
        print(f"Updated cursor for {resource_type}: {cursor_value}")
    except Exception as e:
        print(f"Error updating sync cursor: {e}")


def send_to_healthlake(resource):
    """Send a FHIR resource to HealthLake."""
    resource = sanitize_resource(resource)
    
    url = f"{HEALTHLAKE_ENDPOINT}{resource['resourceType']}/{resource['id']}"
    body = json.dumps(resource, default=str)
    
    # Get fresh credentials for each request
    credentials = session.get_credentials()
    if credentials is None:
        print("ERROR: No credentials available")
        return 500, "No credentials"
    
    request = AWSRequest(method='PUT', url=url, data=body,
                        headers={'Content-Type': 'application/fhir+json'})
    SigV4Auth(credentials, 'healthlake', 'us-east-1').add_auth(request)
    
    response = requests.put(url, data=body, headers=dict(request.headers), timeout=30)
    
    # Log first few failures for debugging
    if response.status_code not in [200, 201]:
        if not hasattr(send_to_healthlake, '_error_count'):
            send_to_healthlake._error_count = 0
        send_to_healthlake._error_count += 1
        if send_to_healthlake._error_count <= 3:
            # Do not log the response body: it can echo back the submitted FHIR
            # resource (PHI). Log only the status code. Set DEBUG_LOG_PHI=true
            # to include the body for local troubleshooting.
            if os.environ.get("DEBUG_LOG_PHI", "false").lower() == "true":
                print(f"HealthLake error {response.status_code}: {response.text[:500]}")
            else:
                print(f"HealthLake error {response.status_code} (body suppressed; set DEBUG_LOG_PHI=true to log)")
    
    return response.status_code, response.text


def send_batch_to_healthlake(resources):
    """Send multiple FHIR resources to HealthLake in parallel."""
    synced = 0
    failed = 0
    
    def send_one(resource):
        status, _ = send_to_healthlake(resource)
        return status in [200, 201], resource
    
    with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as executor:
        futures = {executor.submit(send_one, r): r for r in resources}
        for future in as_completed(futures):
            try:
                success, resource = future.result()
                if success:
                    synced += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
    
    return synced, failed


def sanitize_resource(resource):
    """Remove empty/null values from resource."""
    if resource is None:
        return None
    if isinstance(resource, Decimal):
        return float(resource)
    if isinstance(resource, dict):
        return {k: sanitize_resource(v) for k, v in resource.items()
                if v is not None and v != '' and v != [] and v != {}}
    if isinstance(resource, list):
        cleaned = [sanitize_resource(i) for i in resource 
                   if i is not None and i != '' and i != {} and i != []]
        return cleaned if cleaned else None
    return resource


# ============================================================================
# Database to FHIR Transformers
# ============================================================================

def transform_patient(row):
    """Transform patient_data row to FHIR Patient."""
    resource = {
        'resourceType': 'Patient',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': row.get('date_modified', row.get('date_created', datetime.now())).isoformat() + '+00:00'
        },
        'identifier': [{
            'system': 'urn:oid:openemr',
            'value': str(row['pid'])
        }],
        'name': [{
            'family': row.get('lname', ''),
            'given': [g for g in [row.get('fname'), row.get('mname')] if g]
        }],
        'gender': {'Male': 'male', 'Female': 'female'}.get(row.get('sex'), 'unknown'),
        'birthDate': row.get('DOB').isoformat() if row.get('DOB') else None,
    }
    
    # Address
    if any([row.get('street'), row.get('city'), row.get('state'), row.get('postal_code')]):
        resource['address'] = [{
            'line': [row['street']] if row.get('street') else None,
            'city': row.get('city'),
            'state': row.get('state'),
            'postalCode': row.get('postal_code'),
            'country': row.get('country_code', 'US')
        }]
    
    # Phone
    if row.get('phone_home'):
        resource['telecom'] = [{'system': 'phone', 'value': row['phone_home'], 'use': 'home'}]
    
    return resource


def transform_encounter(row, patient_uuid):
    """Transform form_encounter row to FHIR Encounter."""
    last_updated = row.get('date_modified') or row.get('date')
    last_updated_str = safe_isoformat(last_updated) if last_updated else datetime.now().isoformat() + '+00:00'
    if not last_updated_str:
        last_updated_str = datetime.now().isoformat() + '+00:00'
    period_start = safe_isoformat(row.get('date')) if row.get('date') else None
    
    encounter = {
        'resourceType': 'Encounter',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': last_updated_str
        },
        'status': 'finished',
        'class': {
            'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode',
            'code': 'AMB',
            'display': 'ambulatory'
        },
        'subject': {'reference': f'Patient/{patient_uuid}'},
    }
    
    if period_start:
        encounter['period'] = {'start': period_start}
    if row.get('reason'):
        encounter['reasonCode'] = [{'text': row.get('reason')}]
    
    return encounter


def transform_condition(row, patient_uuid):
    """Transform lists (medical_problem) row to FHIR Condition."""
    last_updated = row.get('modifydate') or row.get('date')
    last_updated_str = safe_isoformat(last_updated) if last_updated else datetime.now().isoformat() + '+00:00'
    if not last_updated_str:
        last_updated_str = datetime.now().isoformat() + '+00:00'
    onset_str = safe_isoformat(row.get('begdate')) if row.get('begdate') else None
    
    condition = {
        'resourceType': 'Condition',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': last_updated_str
        },
        'clinicalStatus': {
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/condition-clinical',
                'code': 'active' if row.get('enddate') is None else 'resolved'
            }]
        },
        'code': {
            'coding': [{
                'system': 'http://hl7.org/fhir/sid/icd-10-cm',
                'code': row.get('diagnosis'),
                'display': row.get('title')
            }] if row.get('diagnosis') else None,
            'text': row.get('title')
        },
        'subject': {'reference': f'Patient/{patient_uuid}'},
    }
    if onset_str:
        condition['onsetDateTime'] = onset_str
    return condition


def transform_allergy(row, patient_uuid):
    """Transform lists (allergy) row to FHIR AllergyIntolerance."""
    return {
        'resourceType': 'AllergyIntolerance',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': row.get('modifydate', row.get('date')).isoformat() + '+00:00' if row.get('modifydate') or row.get('date') else datetime.now().isoformat() + '+00:00'
        },
        'clinicalStatus': {
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical',
                'code': 'active' if row.get('enddate') is None else 'resolved'
            }]
        },
        'code': {
            'text': row.get('title')
        },
        'patient': {'reference': f'Patient/{patient_uuid}'},
        'recordedDate': row['begdate'].isoformat() + '+00:00' if row.get('begdate') else None,
        'reaction': [{
            'manifestation': [{'text': row.get('reaction')}]
        }] if row.get('reaction') else None
    }


def transform_medication_request(row, patient_uuid):
    """Transform prescriptions row to FHIR MedicationRequest."""
    # Handle lastUpdated safely
    last_updated = row.get('date_modified') or row.get('date_added')
    last_updated_str = last_updated.isoformat() + '+00:00' if last_updated else datetime.now().isoformat() + '+00:00'
    
    return {
        'resourceType': 'MedicationRequest',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': last_updated_str
        },
        'status': 'active' if row.get('active') == 1 else 'stopped',
        'intent': 'order',
        'medicationCodeableConcept': {
            'coding': [{
                'system': 'http://www.nlm.nih.gov/research/umls/rxnorm',
                'code': str(row.get('rxnorm_drugcode')) if row.get('rxnorm_drugcode') else None,
                'display': row.get('drug')
            }] if row.get('rxnorm_drugcode') else None,
            'text': row.get('drug')
        },
        'subject': {'reference': f'Patient/{patient_uuid}'},
        'authoredOn': row['date_added'].isoformat() + '+00:00' if row.get('date_added') else None,
        'dosageInstruction': [{
            'text': row.get('dosage'),
            'timing': {
                'code': {'text': str(row.get('frequency'))}
            } if row.get('frequency') else None,
            'route': {'text': row.get('route')} if row.get('route') else None
        }] if row.get('dosage') else None,
        'dispenseRequest': {
            'quantity': {
                'value': float(row['quantity']) if row.get('quantity') else None,
                'unit': str(row.get('unit')) if row.get('unit') else None
            },
            'numberOfRepeatsAllowed': int(row['refills']) if row.get('refills') else None
        } if row.get('quantity') or row.get('refills') else None
    }


def transform_immunization(row, patient_uuid):
    """Transform immunizations row to FHIR Immunization."""
    # Handle lastUpdated safely - use current time if no valid date available
    last_updated = row.get('update_date') or row.get('administered_date') or row.get('create_date')
    last_updated_str = safe_isoformat(last_updated)
    if not last_updated_str:
        last_updated_str = datetime.now().isoformat() + '+00:00'
    
    # CVX code to display name mapping for common vaccines
    CVX_DISPLAY = {
        '03': 'MMR', '05': 'Measles', '06': 'Rubella', '08': 'Hepatitis B',
        '10': 'IPV', '20': 'DTaP', '21': 'Varicella', '33': 'Pneumococcal',
        '43': 'Hepatitis B', '44': 'Hepatitis B', '48': 'Hib',
        '49': 'Hib', '51': 'Hib-Hep B', '62': 'HPV', '83': 'Hepatitis A',
        '85': 'Hepatitis A', '88': 'Influenza', '94': 'MMRV',
        '104': 'Hepatitis A-B', '110': 'DTaP-Hep B-IPV', '113': 'Td',
        '114': 'Meningococcal MCV4', '115': 'Tdap', '116': 'Rotavirus',
        '118': 'HPV Bivalent', '119': 'Rotavirus', '120': 'DTaP-IPV-Hib',
        '121': 'Zoster (Shingrix)', '127': 'Novel Influenza H1N1',
        '133': 'PCV13 (Pneumococcal)', '135': 'Influenza High-Dose',
        '136': 'Meningococcal MenB', '140': 'Influenza (IIV)',
        '141': 'Influenza (IIV)', '150': 'Influenza (IIV4)',
        '158': 'Influenza (IIV4)', '171': 'Influenza (IIV4)',
        '185': 'Recombinant Zoster', '187': 'Recombinant Zoster',
        '197': 'Influenza High-Dose (IIV4)', '205': 'Influenza (IIV4)',
        '207': 'COVID-19 mRNA (Moderna)', '208': 'COVID-19 mRNA (Pfizer)',
        '210': 'COVID-19 (AstraZeneca)', '211': 'COVID-19 (Novavax)',
        '212': 'COVID-19 (J&J/Janssen)', '213': 'COVID-19 Unspecified',
        '217': 'COVID-19 Bivalent (Pfizer)', '218': 'COVID-19 Bivalent (Moderna)',
        '219': 'COVID-19 Updated (2023-2024)',
        '229': 'RSV (Abrysvo)', '230': 'RSV (Arexvy)',
    }
    
    cvx_code = str(row.get('cvx_code')) if row.get('cvx_code') else None
    display_name = row.get('title') or CVX_DISPLAY.get(cvx_code, f'Vaccine (CVX {cvx_code})')
    
    return {
        'resourceType': 'Immunization',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': last_updated_str
        },
        'status': 'completed',
        'vaccineCode': {
            'coding': [{
                'system': 'http://hl7.org/fhir/sid/cvx',
                'code': cvx_code,
                'display': display_name
            }] if cvx_code else None,
            'text': display_name
        },
        'patient': {'reference': f'Patient/{patient_uuid}'},
        'occurrenceDateTime': safe_isoformat(row.get('administered_date')),
        'lotNumber': row.get('lot_number'),
        'manufacturer': {'display': row.get('manufacturer')} if row.get('manufacturer') else None
    }


def transform_observation(row, patient_uuid):
    """Transform procedure_result row to FHIR Observation."""
    # Handle date - skip invalid dates like 0000-00-00
    effective_dt = None
    if row.get('date') and str(row.get('date')) != '0000-00-00 00:00:00':
        try:
            effective_dt = row['date'].isoformat() + '+00:00'
        except:
            pass
    
    return {
        'resourceType': 'Observation',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': datetime.now().isoformat() + '+00:00'
        },
        'status': row.get('result_status', 'final') or 'final',
        'code': {
            'coding': [{
                'system': 'http://loinc.org',
                'code': row.get('result_code'),
                'display': row.get('result_text')
            }] if row.get('result_code') else None,
            'text': row.get('result_text')
        },
        'subject': {'reference': f'Patient/{patient_uuid}'},
        'effectiveDateTime': effective_dt,
        'valueQuantity': {
            'value': float(row['result']) if row.get('result') and row['result'].replace('.', '').replace('-', '').isdigit() else None,
            'unit': row.get('units')
        } if row.get('result') and row.get('units') else None,
        'valueString': row.get('result') if row.get('result') and not row.get('units') else None
    }


def transform_procedure(row, patient_uuid):
    """Transform procedure_order row to FHIR Procedure."""
    performed_dt = None
    if row.get('date_ordered'):
        try:
            performed_dt = row['date_ordered'].isoformat() + '+00:00'
        except:
            pass
    
    return {
        'resourceType': 'Procedure',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': datetime.now().isoformat() + '+00:00'
        },
        'status': 'completed' if row.get('order_status') == 'complete' else 'in-progress',
        'code': {
            'text': row.get('order_type_name') or 'Procedure'
        },
        'subject': {'reference': f'Patient/{patient_uuid}'},
        'performedDateTime': performed_dt
    }


def transform_diagnostic_report(row, patient_uuid):
    """Transform procedure_report row to FHIR DiagnosticReport."""
    effective_dt = None
    if row.get('date_report'):
        try:
            effective_dt = row['date_report'].isoformat() + '+00:00'
        except:
            pass
    
    return {
        'resourceType': 'DiagnosticReport',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': datetime.now().isoformat() + '+00:00'
        },
        'status': 'final' if row.get('report_status') == 'final' else 'preliminary',
        'code': {
            'text': 'Laboratory Report'
        },
        'subject': {'reference': f'Patient/{patient_uuid}'},
        'effectiveDateTime': effective_dt,
        'conclusion': row.get('report_notes')
    }


def transform_document_reference(row, patient_uuid):
    """Transform documents row to FHIR DocumentReference."""
    return {
        'resourceType': 'DocumentReference',
        'id': format_uuid(row['uuid']),
        'meta': {
            'lastUpdated': row.get('revision').isoformat() + '+00:00' if row.get('revision') else datetime.now().isoformat() + '+00:00'
        },
        'status': 'current',
        'type': {
            'text': row.get('mimetype', 'application/octet-stream')
        },
        'subject': {'reference': f'Patient/{patient_uuid}'},
        'date': row.get('date').isoformat() + '+00:00' if row.get('date') else None,
        'description': row.get('name'),
        'content': [{
            'attachment': {
                'contentType': row.get('mimetype'),
                'url': row.get('url'),
                'size': row.get('size')
            }
        }]
    }


def transform_clinical_note(row, patient_uuid, encounter_uuid=None):
    """Transform form_clinical_notes row to FHIR DocumentReference with embedded text content."""
    import base64
    
    # Get the note content
    note_content = row.get('description') or row.get('clinical_notes_text') or ''
    
    # Base64 encode the note content for FHIR attachment
    note_bytes = note_content.encode('utf-8') if note_content else b''
    note_base64 = base64.b64encode(note_bytes).decode('utf-8')
    
    # Map clinical_notes_type to LOINC codes
    note_type_map = {
        'progress_note': ('11506-3', 'Progress note'),
        'soap_note': ('34109-9', 'Note'),
        'consultation_note': ('11488-4', 'Consultation note'),
        'discharge_summary': ('18842-5', 'Discharge summary'),
        'history_physical': ('34117-2', 'History and physical note'),
        'procedure_note': ('28570-0', 'Procedure note'),
    }
    
    note_type = row.get('clinical_notes_type', 'progress_note')
    loinc_code, loinc_display = note_type_map.get(note_type, ('11506-3', 'Progress note'))
    
    # Handle lastUpdated - ensure it's a full datetime with time component
    last_updated = row.get('date')
    if last_updated:
        if hasattr(last_updated, 'isoformat'):
            # Check if it has time component
            if hasattr(last_updated, 'hour'):
                last_updated_str = last_updated.isoformat() + '+00:00'
            else:
                # Date only - add midnight time
                last_updated_str = last_updated.isoformat() + 'T00:00:00+00:00'
        else:
            last_updated_str = datetime.now().isoformat() + '+00:00'
    else:
        last_updated_str = datetime.now().isoformat() + '+00:00'
    
    # Handle document date
    doc_date = row.get('date')
    if doc_date:
        if hasattr(doc_date, 'isoformat'):
            if hasattr(doc_date, 'hour'):
                doc_date_str = doc_date.isoformat() + '+00:00'
            else:
                doc_date_str = doc_date.isoformat() + 'T00:00:00+00:00'
        else:
            doc_date_str = None
    else:
        doc_date_str = None
    
    resource = {
        'resourceType': 'DocumentReference',
        'id': f"note-{row['id']}",  # Use form_clinical_notes.id as unique identifier
        'meta': {
            'lastUpdated': last_updated_str
        },
        'status': 'current',
        'type': {
            'coding': [{
                'system': 'http://loinc.org',
                'code': loinc_code,
                'display': loinc_display
            }],
            'text': loinc_display
        },
        'category': [{
            'coding': [{
                'system': 'http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category',
                'code': 'clinical-note',
                'display': 'Clinical Note'
            }]
        }],
        'subject': {'reference': f'Patient/{patient_uuid}'},
        'date': doc_date_str,
        'description': f"{loinc_display} - {row.get('date').strftime('%Y-%m-%d') if row.get('date') and hasattr(row.get('date'), 'strftime') else 'Unknown date'}",
        'content': [{
            'attachment': {
                'contentType': 'text/plain',
                'data': note_base64,
                'title': loinc_display
            }
        }]
    }
    
    # Add encounter context if available
    if encounter_uuid:
        resource['context'] = {
            'encounter': [{'reference': f'Encounter/{encounter_uuid}'}]
        }
    
    return resource


# ============================================================================
# Sync Functions
# ============================================================================

def sync_patients(conn, context, incremental=False):
    """Sync patients from database. If incremental, only sync records after cursor."""
    synced, failed = 0, 0
    max_timestamp = None
    
    # Get cursor for incremental sync
    cursor_value = get_sync_cursor('Patient') if incremental else None
    
    with conn.cursor() as cursor:
        if cursor_value:
            cursor.execute("""
                SELECT uuid, pid, fname, mname, lname, sex, DOB, street, city, state, 
                       postal_code, country_code, phone_home, date as date_created,
                       COALESCE(date, NOW()) as date_modified
                FROM patient_data 
                WHERE uuid IS NOT NULL AND COALESCE(date, NOW()) > %s
                ORDER BY COALESCE(date, NOW())
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT uuid, pid, fname, mname, lname, sex, DOB, street, city, state, 
                       postal_code, country_code, phone_home, date as date_created,
                       COALESCE(date, NOW()) as date_modified
                FROM patient_data 
                WHERE uuid IS NOT NULL
                ORDER BY COALESCE(date, NOW())
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} patients" + (f" (after {cursor_value})" if cursor_value else ""))
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                print("Time limit approaching, stopping")
                break
                
            resource = transform_patient(row)
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('date_modified')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
                if failed <= 3:
                    print(f"Failed Patient/{resource['id']}: {status}")
    
    # Update cursor: use max_timestamp if we synced records, otherwise preserve existing cursor
    if max_timestamp:
        update_sync_cursor('Patient', safe_isoformat(max_timestamp))
    elif cursor_value and synced == 0 and len(rows) == 0:
        # No new records found, preserve existing cursor
        update_sync_cursor('Patient', cursor_value)
    
    return synced, failed


def sync_encounters(conn, context, incremental=False):
    """Sync encounters from database using batch processing."""
    synced, failed = 0, 0
    max_timestamp = None
    
    cursor_value = get_sync_cursor('Encounter') if incremental else None
    
    with conn.cursor() as cursor:
        # Get patient UUID mapping
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) 
                       for r in cursor.fetchall()}
        
        if cursor_value:
            cursor.execute("""
                SELECT e.uuid, e.pid, e.date, e.reason, e.date as date_modified
                FROM form_encounter e
                WHERE e.uuid IS NOT NULL AND e.date > %s
                ORDER BY e.date
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT e.uuid, e.pid, e.date, e.reason, e.date as date_modified
                FROM form_encounter e
                WHERE e.uuid IS NOT NULL
                ORDER BY e.date
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} encounters" + (f" (after {cursor_value})" if cursor_value else ""))
        
        # Process in batches
        batch = []
        batch_timestamps = []
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 60000:
                # Process remaining batch before stopping
                if batch:
                    batch_synced, batch_failed = send_batch_to_healthlake(batch)
                    synced += batch_synced
                    failed += batch_failed
                    if batch_timestamps:
                        max_timestamp = max(batch_timestamps, key=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x))
                print("Time limit approaching, stopping")
                break
                
            patient_uuid = patient_map.get(row['pid'])
            if not patient_uuid:
                continue
                
            resource = transform_encounter(row, patient_uuid)
            batch.append(resource)
            ts = row.get('date_modified') or row.get('date')
            if ts:
                batch_timestamps.append(ts)
            
            # Send batch when full
            if len(batch) >= BATCH_SIZE:
                batch_synced, batch_failed = send_batch_to_healthlake(batch)
                synced += batch_synced
                failed += batch_failed
                if batch_timestamps:
                    max_timestamp = max(batch_timestamps, key=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x))
                    update_sync_cursor('Encounter', safe_isoformat(max_timestamp))
                batch = []
                batch_timestamps = []
                print(f"  Progress: {synced} synced, {failed} failed")
        
        # Process final batch
        if batch:
            batch_synced, batch_failed = send_batch_to_healthlake(batch)
            synced += batch_synced
            failed += batch_failed
            if batch_timestamps:
                max_timestamp = max(batch_timestamps, key=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x))
    
    # Update cursor: use max_timestamp if we synced records, otherwise preserve existing cursor
    if max_timestamp:
        update_sync_cursor('Encounter', safe_isoformat(max_timestamp))
    elif cursor_value and synced == 0 and len(rows) == 0:
        # No new records found, preserve existing cursor
        update_sync_cursor('Encounter', cursor_value)
    
    return synced, failed


def sync_conditions(conn, context, incremental=False):
    """Sync conditions (medical problems) from database."""
    synced, failed = 0, 0
    max_timestamp = None
    cursor_value = get_sync_cursor('Condition') if incremental else None
    
    with conn.cursor() as cursor:
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) 
                       for r in cursor.fetchall()}
        
        if cursor_value:
            cursor.execute("""
                SELECT uuid, pid, title, diagnosis, begdate, enddate, date, modifydate
                FROM lists 
                WHERE type = 'medical_problem' AND uuid IS NOT NULL 
                  AND COALESCE(modifydate, date) > %s
                ORDER BY COALESCE(modifydate, date)
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT uuid, pid, title, diagnosis, begdate, enddate, date, modifydate
                FROM lists 
                WHERE type = 'medical_problem' AND uuid IS NOT NULL
                ORDER BY COALESCE(modifydate, date)
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} conditions" + (f" (after {cursor_value})" if cursor_value else ""))
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                break
                
            patient_uuid = patient_map.get(row['pid'])
            if not patient_uuid:
                continue
                
            resource = transform_condition(row, patient_uuid)
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('modifydate') or row.get('date')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
    
    # Update cursor: use max_timestamp if we synced records, otherwise preserve existing cursor
    if max_timestamp:
        update_sync_cursor('Condition', safe_isoformat(max_timestamp))
    elif cursor_value and synced == 0 and len(rows) == 0:
        # No new records found, preserve existing cursor
        update_sync_cursor('Condition', cursor_value)
    
    return synced, failed


def sync_allergies(conn, context, incremental=False):
    """Sync allergies from database."""
    synced, failed = 0, 0
    max_timestamp = None
    cursor_value = get_sync_cursor('AllergyIntolerance') if incremental else None
    
    with conn.cursor() as cursor:
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) 
                       for r in cursor.fetchall()}
        
        if cursor_value:
            cursor.execute("""
                SELECT uuid, pid, title, reaction, begdate, enddate, date, modifydate
                FROM lists 
                WHERE type = 'allergy' AND uuid IS NOT NULL
                  AND COALESCE(modifydate, date) > %s
                ORDER BY COALESCE(modifydate, date)
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT uuid, pid, title, reaction, begdate, enddate, date, modifydate
                FROM lists 
                WHERE type = 'allergy' AND uuid IS NOT NULL
                ORDER BY COALESCE(modifydate, date)
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} allergies" + (f" (after {cursor_value})" if cursor_value else ""))
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                break
                
            patient_uuid = patient_map.get(row['pid'])
            if not patient_uuid:
                continue
                
            resource = transform_allergy(row, patient_uuid)
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('modifydate') or row.get('date')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
    
    # Update cursor: use max_timestamp if we synced records, otherwise preserve existing cursor
    if max_timestamp:
        update_sync_cursor('AllergyIntolerance', safe_isoformat(max_timestamp))
    elif cursor_value and synced == 0 and len(rows) == 0:
        # No new records found, preserve existing cursor
        update_sync_cursor('AllergyIntolerance', cursor_value)
    
    return synced, failed


def sync_medications(conn, context, incremental=False):
    """Sync medication requests from database (from both prescriptions and lists tables)."""
    synced, failed = 0, 0
    max_timestamp = None
    cursor_value = get_sync_cursor('MedicationRequest') if incremental else None
    
    with conn.cursor() as cursor:
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) 
                       for r in cursor.fetchall()}
        
        # First try prescriptions table
        if cursor_value:
            cursor.execute("""
                SELECT uuid, patient_id, drug, rxnorm_drugcode, dosage, `interval` as frequency, route,
                       quantity, unit, refills, active, date_added, date_modified
                FROM prescriptions 
                WHERE uuid IS NOT NULL AND COALESCE(date_modified, date_added) > %s
                ORDER BY COALESCE(date_modified, date_added)
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT uuid, patient_id, drug, rxnorm_drugcode, dosage, `interval` as frequency, route,
                       quantity, unit, refills, active, date_added, date_modified
                FROM prescriptions 
                WHERE uuid IS NOT NULL
                ORDER BY COALESCE(date_modified, date_added)
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} medications from prescriptions table")
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                break
                
            patient_uuid = patient_map.get(row['patient_id'])
            if not patient_uuid:
                continue
                
            resource = transform_medication_request(row, patient_uuid)
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('date_modified') or row.get('date_added')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
                if failed <= 3:
                    print(f"Failed MedicationRequest/{resource['id']}: {status}")
        
        # Also check lists table for medications (data loader stores them here)
        if cursor_value:
            cursor.execute("""
                SELECT uuid, pid as patient_id, title as drug, date as date_added, date as date_modified
                FROM lists 
                WHERE type = 'medication' AND uuid IS NOT NULL AND date > %s
                ORDER BY date
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT uuid, pid as patient_id, title as drug, date as date_added, date as date_modified
                FROM lists 
                WHERE type = 'medication' AND uuid IS NOT NULL
                ORDER BY date
            """)
        
        list_rows = cursor.fetchall()
        print(f"Found {len(list_rows)} medications from lists table")
        
        for row in list_rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                break
                
            patient_uuid = patient_map.get(row['patient_id'])
            if not patient_uuid:
                continue
            
            # Transform lists-style medication to FHIR MedicationRequest
            resource = {
                'resourceType': 'MedicationRequest',
                'id': format_uuid(row['uuid']),
                'meta': {
                    'lastUpdated': row['date_added'].isoformat() + '+00:00' if row.get('date_added') else datetime.now().isoformat() + '+00:00'
                },
                'status': 'active',
                'intent': 'order',
                'medicationCodeableConcept': {
                    'text': row.get('drug')
                },
                'subject': {'reference': f'Patient/{patient_uuid}'},
                'authoredOn': row['date_added'].isoformat() + '+00:00' if row.get('date_added') else None,
            }
            
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('date_modified') or row.get('date_added')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
                if failed <= 3:
                    print(f"Failed MedicationRequest/{resource['id']}: {status}")
    
    # Update cursor: use max_timestamp if we synced records, otherwise preserve existing cursor
    if max_timestamp:
        update_sync_cursor('MedicationRequest', safe_isoformat(max_timestamp))
    elif cursor_value and synced == 0 and len(rows) == 0 and len(list_rows) == 0:
        # No new records found, preserve existing cursor
        update_sync_cursor('MedicationRequest', cursor_value)
    
    return synced, failed


def sync_immunizations(conn, context, incremental=False):
    """Sync immunizations from database."""
    synced, failed = 0, 0
    max_timestamp = None
    cursor_value = get_sync_cursor('Immunization') if incremental else None
    
    with conn.cursor() as cursor:
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) 
                       for r in cursor.fetchall()}
        
        if cursor_value:
            cursor.execute("""
                SELECT uuid, patient_id, cvx_code, administered_date, lot_number, 
                       manufacturer, create_date, update_date,
                       (SELECT title FROM list_options WHERE list_id = 'immunizations' 
                        AND option_id = immunizations.cvx_code LIMIT 1) as title
                FROM immunizations 
                WHERE uuid IS NOT NULL 
                  AND COALESCE(update_date, administered_date, create_date) > %s
                ORDER BY COALESCE(update_date, administered_date, create_date)
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT uuid, patient_id, cvx_code, administered_date, lot_number, 
                       manufacturer, create_date, update_date,
                       (SELECT title FROM list_options WHERE list_id = 'immunizations' 
                        AND option_id = immunizations.cvx_code LIMIT 1) as title
                FROM immunizations 
                WHERE uuid IS NOT NULL
                ORDER BY COALESCE(update_date, administered_date, create_date)
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} immunizations" + (f" (after {cursor_value})" if cursor_value else ""))
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                break
                
            patient_uuid = patient_map.get(row['patient_id'])
            if not patient_uuid:
                continue
                
            resource = transform_immunization(row, patient_uuid)
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('update_date') or row.get('administered_date') or row.get('create_date')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
                if failed <= 3:
                    print(f"Failed Immunization/{resource['id']}: {status}")
    
    if max_timestamp:
        update_sync_cursor('Immunization', safe_isoformat(max_timestamp))
    
    return synced, failed


def sync_observations(conn, context, incremental=False):
    """Sync observations (procedure results) from database using batch processing."""
    synced, failed = 0, 0
    max_id = None
    cursor_value = get_sync_cursor('Observation') if incremental else None
    
    with conn.cursor() as cursor:
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) 
                       for r in cursor.fetchall()}
        
        # Observations don't have good timestamps, use procedure_result_id for cursor
        if cursor_value and cursor_value.isdigit():
            cursor.execute("""
                SELECT pr.uuid, pr.procedure_result_id, pr.result_code, pr.result_text, pr.date, pr.units, 
                       pr.result, pr.result_status, po.patient_id
                FROM procedure_result pr
                JOIN procedure_report prp ON pr.procedure_report_id = prp.procedure_report_id
                JOIN procedure_order po ON prp.procedure_order_id = po.procedure_order_id
                WHERE pr.uuid IS NOT NULL AND pr.procedure_result_id > %s
                ORDER BY pr.procedure_result_id
            """, (int(cursor_value),))
        else:
            cursor.execute("""
                SELECT pr.uuid, pr.procedure_result_id, pr.result_code, pr.result_text, pr.date, pr.units, 
                       pr.result, pr.result_status, po.patient_id
                FROM procedure_result pr
                JOIN procedure_report prp ON pr.procedure_report_id = prp.procedure_report_id
                JOIN procedure_order po ON prp.procedure_order_id = po.procedure_order_id
                WHERE pr.uuid IS NOT NULL
                ORDER BY pr.procedure_result_id
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} observations" + (f" (after id {cursor_value})" if cursor_value else ""))
        
        # Process in batches
        batch = []
        batch_ids = []
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 60000:
                if batch:
                    batch_synced, batch_failed = send_batch_to_healthlake(batch)
                    synced += batch_synced
                    failed += batch_failed
                    if batch_ids:
                        max_id = max(batch_ids)
                print("Time limit approaching, stopping")
                break
                
            patient_uuid = patient_map.get(row['patient_id'])
            if not patient_uuid:
                continue
                
            resource = transform_observation(row, patient_uuid)
            batch.append(resource)
            if row.get('procedure_result_id'):
                batch_ids.append(row['procedure_result_id'])
            
            if len(batch) >= BATCH_SIZE:
                batch_synced, batch_failed = send_batch_to_healthlake(batch)
                synced += batch_synced
                failed += batch_failed
                if batch_ids:
                    max_id = max(batch_ids)
                    update_sync_cursor('Observation', str(max_id))
                batch = []
                batch_ids = []
                print(f"  Progress: {synced} synced, {failed} failed")
        
        # Process final batch
        if batch:
            batch_synced, batch_failed = send_batch_to_healthlake(batch)
            synced += batch_synced
            failed += batch_failed
            if batch_ids:
                max_id = max(batch_ids)
    
    # Update cursor: use max_id if we synced records, otherwise preserve existing cursor
    if max_id:
        update_sync_cursor('Observation', str(max_id))
    elif cursor_value and synced == 0 and len(rows) == 0:
        # No new records found, preserve existing cursor
        update_sync_cursor('Observation', cursor_value)
    return synced, failed


def sync_procedures(conn, context, incremental=False):
    """Sync procedures from database."""
    synced, failed = 0, 0
    max_timestamp = None
    cursor_value = get_sync_cursor('Procedure') if incremental else None
    
    with conn.cursor() as cursor:
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) 
                       for r in cursor.fetchall()}
        
        if cursor_value:
            cursor.execute("""
                SELECT uuid, patient_id, date_ordered, order_status, procedure_order_type as order_type_name
                FROM procedure_order
                WHERE uuid IS NOT NULL AND date_ordered > %s
                ORDER BY date_ordered
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT uuid, patient_id, date_ordered, order_status, procedure_order_type as order_type_name
                FROM procedure_order
                WHERE uuid IS NOT NULL
                ORDER BY date_ordered
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} procedures" + (f" (after {cursor_value})" if cursor_value else ""))
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                break
                
            patient_uuid = patient_map.get(row['patient_id'])
            if not patient_uuid:
                continue
                
            resource = transform_procedure(row, patient_uuid)
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('date_ordered')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
                if failed <= 3:
                    print(f"Failed Procedure/{resource['id']}: {status}")
    
    # Update cursor: use max_timestamp if we synced records, otherwise preserve existing cursor
    if max_timestamp:
        update_sync_cursor('Procedure', safe_isoformat(max_timestamp))
    elif cursor_value and synced == 0 and len(rows) == 0:
        # No new records found, preserve existing cursor
        update_sync_cursor('Procedure', cursor_value)
    
    return synced, failed


def sync_diagnostic_reports(conn, context, incremental=False):
    """Sync diagnostic reports from database."""
    synced, failed = 0, 0
    max_timestamp = None
    cursor_value = get_sync_cursor('DiagnosticReport') if incremental else None
    
    with conn.cursor() as cursor:
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) 
                       for r in cursor.fetchall()}
        
        if cursor_value:
            cursor.execute("""
                SELECT prp.uuid, prp.date_report, prp.report_status, prp.report_notes, po.patient_id
                FROM procedure_report prp
                JOIN procedure_order po ON prp.procedure_order_id = po.procedure_order_id
                WHERE prp.uuid IS NOT NULL AND prp.date_report > %s
                ORDER BY prp.date_report
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT prp.uuid, prp.date_report, prp.report_status, prp.report_notes, po.patient_id
                FROM procedure_report prp
                JOIN procedure_order po ON prp.procedure_order_id = po.procedure_order_id
                WHERE prp.uuid IS NOT NULL
                ORDER BY prp.date_report
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} diagnostic reports" + (f" (after {cursor_value})" if cursor_value else ""))
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                break
                
            patient_uuid = patient_map.get(row['patient_id'])
            if not patient_uuid:
                continue
                
            resource = transform_diagnostic_report(row, patient_uuid)
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('date_report')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
                if failed <= 3:
                    print(f"Failed DiagnosticReport/{resource['id']}: {status}")
    
    # Update cursor: use max_timestamp if we synced records, otherwise preserve existing cursor
    if max_timestamp:
        update_sync_cursor('DiagnosticReport', safe_isoformat(max_timestamp))
    elif cursor_value and synced == 0 and len(rows) == 0:
        # No new records found, preserve existing cursor
        update_sync_cursor('DiagnosticReport', cursor_value)
    
    return synced, failed


def sync_document_references(conn, context, incremental=False):
    """Sync document references from database."""
    synced, failed = 0, 0
    max_timestamp = None
    cursor_value = get_sync_cursor('DocumentReference') if incremental else None
    
    with conn.cursor() as cursor:
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) 
                       for r in cursor.fetchall()}
        
        if cursor_value:
            cursor.execute("""
                SELECT uuid, foreign_id as patient_id, date, revision, mimetype, url, size, name
                FROM documents
                WHERE uuid IS NOT NULL AND deleted = 0 AND revision > %s
                ORDER BY revision
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT uuid, foreign_id as patient_id, date, revision, mimetype, url, size, name
                FROM documents
                WHERE uuid IS NOT NULL AND deleted = 0
                ORDER BY revision
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} document references" + (f" (after {cursor_value})" if cursor_value else ""))
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                break
                
            patient_uuid = patient_map.get(row['patient_id'])
            if not patient_uuid:
                continue
                
            resource = transform_document_reference(row, patient_uuid)
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('revision')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
                if failed <= 3:
                    print(f"Failed DocumentReference/{resource['id']}: {status}")
    
    # Update cursor: use max_timestamp if we synced records, otherwise preserve existing cursor
    if max_timestamp:
        update_sync_cursor('DocumentReference', safe_isoformat(max_timestamp))
    elif cursor_value and synced == 0 and len(rows) == 0:
        # No new records found, preserve existing cursor
        update_sync_cursor('DocumentReference', cursor_value)
    
    return synced, failed


def sync_clinical_notes(conn, context, incremental=False):
    """Sync clinical notes from form_clinical_notes table to HealthLake as DocumentReference."""
    synced, failed = 0, 0
    max_timestamp = None
    cursor_value = get_sync_cursor('ClinicalNote') if incremental else None
    
    with conn.cursor() as cursor:
        # Get patient UUID mapping
        cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
        patient_map = {r['pid']: format_uuid(r['uuid']) for r in cursor.fetchall()}
        
        # Get encounter UUID mapping
        cursor.execute("SELECT encounter, uuid FROM form_encounter WHERE uuid IS NOT NULL")
        encounter_map = {r['encounter']: format_uuid(r['uuid']) for r in cursor.fetchall()}
        
        if cursor_value:
            cursor.execute("""
                SELECT id, date, pid, encounter, clinical_notes_type, description
                FROM form_clinical_notes
                WHERE date >= %s
                ORDER BY date
            """, (cursor_value,))
        else:
            cursor.execute("""
                SELECT id, date, pid, encounter, clinical_notes_type, description
                FROM form_clinical_notes
                ORDER BY date
            """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} clinical notes" + (f" (after {cursor_value})" if cursor_value else ""))
        
        for row in rows:
            if context and context.get_remaining_time_in_millis() < 30000:
                break
            
            patient_uuid = patient_map.get(row['pid'])
            if not patient_uuid:
                continue
            
            encounter_uuid = encounter_map.get(row['encounter'])
            
            resource = transform_clinical_note(row, patient_uuid, encounter_uuid)
            status, _ = send_to_healthlake(resource)
            
            if status in [200, 201]:
                synced += 1
                ts = row.get('date')
                if ts and (max_timestamp is None or ts_greater(ts, max_timestamp)):
                    max_timestamp = ts
            else:
                failed += 1
                if failed <= 3:
                    print(f"Failed ClinicalNote/{resource['id']}: {status}")
    
    # Update cursor
    if max_timestamp:
        update_sync_cursor('ClinicalNote', safe_isoformat(max_timestamp))
    elif cursor_value and synced == 0 and len(rows) == 0:
        update_sync_cursor('ClinicalNote', cursor_value)
    
    return synced, failed


def handler(event, context):
    """
    Daily database sync handler.
    
    Syncs all resource types directly from the OpenEMR database to HealthLake.
    Updates cursors so incremental FHIR sync can pick up from there.
    
    Event parameters:
    - incremental: If true, only sync records after the last cursor (default: true for nightly runs)
    - full_sync: If true, force full sync ignoring cursors (for initial sync)
    - resource_types: Optional list of specific resource types to sync (e.g., ["ClinicalNote"])
    """
    # Default to incremental sync, but allow full_sync override
    incremental = not event.get('full_sync', False)
    resource_type_filter = event.get('resource_types', None)
    
    print(f"DB sync started at {datetime.utcnow().isoformat()} (incremental={incremental}, filter={resource_type_filter})")
    
    results = {}
    
    try:
        conn = get_db_connection()
        
        # Sync each resource type
        sync_functions = [
            ('Patient', sync_patients),
            ('Encounter', sync_encounters),
            ('Condition', sync_conditions),
            ('AllergyIntolerance', sync_allergies),
            ('MedicationRequest', sync_medications),
            ('Immunization', sync_immunizations),
            ('Observation', sync_observations),
            ('Procedure', sync_procedures),
            ('DiagnosticReport', sync_diagnostic_reports),
            ('DocumentReference', sync_document_references),
            ('ClinicalNote', sync_clinical_notes),
        ]
        
        for resource_type, sync_func in sync_functions:
            # Skip if filter is specified and this type is not in it
            if resource_type_filter and resource_type not in resource_type_filter:
                continue
            if context and context.get_remaining_time_in_millis() < 60000:
                print(f"Time running low, stopping before {resource_type}")
                break
                
            print(f"\nSyncing {resource_type}...")
            synced, failed = sync_func(conn, context, incremental=incremental)
            results[resource_type] = {'synced': synced, 'failed': failed}
            print(f"{resource_type}: synced={synced}, failed={failed}")
        
        conn.close()
        
    except Exception as e:
        print(f"Error in DB sync: {e}")
        return {'statusCode': 500, 'error': str(e)}
    
    total_synced = sum(r['synced'] for r in results.values())
    total_failed = sum(r['failed'] for r in results.values())
    
    print(f"\nDB sync complete. Total synced: {total_synced}, failed: {total_failed}")
    return {'statusCode': 200, 'body': json.dumps(results)}
