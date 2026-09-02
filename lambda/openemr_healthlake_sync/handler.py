"""
OpenEMR to HealthLake One-Way Sync Lambda

Syncs FHIR resources from OpenEMR to AWS HealthLake using batch processing.
Each invocation processes a batch of records, storing cursor in DynamoDB.
"""

import json
import os
import boto3
import requests
from datetime import datetime
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# Configuration
OPENEMR_BASE_URL = os.environ.get('OPENEMR_BASE_URL')
HEALTHLAKE_ENDPOINT = os.environ.get('HEALTHLAKE_ENDPOINT')
HEALTHLAKE_DATASTORE_ID = os.environ.get('HEALTHLAKE_DATASTORE_ID')
SYNC_STATE_TABLE = os.environ.get('SYNC_STATE_TABLE', 'openemr-healthlake-sync-state')
OPENEMR_CREDENTIALS_SECRET = os.environ.get('OPENEMR_CREDENTIALS_SECRET')

# SSL verification for internal OpenEMR connections (self-signed certs in VPC)
OPENEMR_SSL_VERIFY = os.environ.get('OPENEMR_SSL_CERT', os.environ.get('SSL_CERT_FILE', True))

# Batch settings
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '500'))  # Records per invocation
PAGE_SIZE = 100  # Records per API call

# FHIR resource types to sync (in order of priority)
# DocumentReference removed - OpenEMR doesn't support _lastUpdated filter for clinical notes
# Clinical notes are synced directly from the database instead
RESOURCE_TYPES = [
    'Patient',
    'Encounter',
    'Condition', 
    'AllergyIntolerance',
    'MedicationRequest',
    'Immunization',
    'Observation',
    'Procedure',
    'DiagnosticReport',
]

# Database config for UUID->pid mapping
# Database config for UUID->pid mapping
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')

_openemr_credentials = None
dynamodb = boto3.resource('dynamodb')
secrets_client = boto3.client('secretsmanager')
session = boto3.Session()


def get_openemr_credentials():
    global _openemr_credentials
    if _openemr_credentials is None:
        response = secrets_client.get_secret_value(SecretId=OPENEMR_CREDENTIALS_SECRET)
        _openemr_credentials = json.loads(response['SecretString'])
    return _openemr_credentials


def get_openemr_token():
    credentials = get_openemr_credentials()
    scopes = ['openid', 'api:fhir'] + [f'user/{rt}.read' for rt in RESOURCE_TYPES]
    
    response = requests.post(
        f"{OPENEMR_BASE_URL}/oauth2/default/token",
        data={
            'grant_type': 'password',
            'client_id': credentials['client_id'],
            'client_secret': credentials['client_secret'],
            'username': credentials['username'],
            'password': credentials['password'],
            'user_role': 'users',
            'scope': ' '.join(scopes)
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        verify=OPENEMR_SSL_VERIFY,
        timeout=30
    )
    
    if response.status_code != 200:
        raise Exception(f"Failed to get OpenEMR token: {response.text}")
    return response.json()['access_token']


def get_sync_state(resource_type):
    """Get sync state including cursor for batch processing."""
    try:
        table = dynamodb.Table(SYNC_STATE_TABLE)
        response = table.get_item(Key={'resource_type': resource_type})
        if 'Item' in response:
            return {
                'last_sync': response['Item'].get('last_sync', '2020-01-01T00:00:00Z'),
                'cursor': response['Item'].get('cursor'),  # Last processed timestamp
                'batch_id': response['Item'].get('batch_id'),  # Current batch run ID
                'in_progress': response['Item'].get('in_progress', False)
            }
    except Exception as e:
        print(f"Error getting sync state: {e}")
    
    return {
        'last_sync': '2020-01-01T00:00:00Z',
        'cursor': None,
        'batch_id': None,
        'in_progress': False
    }


def update_sync_state(resource_type, last_sync=None, cursor=None, batch_id=None, in_progress=False):
    """Update sync state with cursor for resumable batch processing."""
    try:
        table = dynamodb.Table(SYNC_STATE_TABLE)
        item = {
            'resource_type': resource_type,
            'updated_at': datetime.utcnow().isoformat(),
            'in_progress': in_progress
        }
        if last_sync:
            item['last_sync'] = last_sync
        if cursor:
            item['cursor'] = cursor
        if batch_id:
            item['batch_id'] = batch_id
            
        table.put_item(Item=item)
    except Exception as e:
        print(f"Error updating sync state: {e}")


def clear_cursor(resource_type, new_last_sync):
    """Clear cursor when batch completes, update last_sync."""
    try:
        table = dynamodb.Table(SYNC_STATE_TABLE)
        table.update_item(
            Key={'resource_type': resource_type},
            UpdateExpression='SET last_sync = :ls, updated_at = :ua REMOVE #cur, batch_id, in_progress',
            ExpressionAttributeNames={'#cur': 'cursor'},
            ExpressionAttributeValues={
                ':ls': new_last_sync,
                ':ua': datetime.utcnow().isoformat()
            }
        )
    except Exception as e:
        print(f"Error clearing cursor: {e}")


# State code mapping
STATE_TO_USPS = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
    'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'florida': 'FL', 'georgia': 'GA',
    'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
    'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS', 'missouri': 'MO',
    'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ',
    'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH',
    'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT',
    'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
    'district of columbia': 'DC', 'puerto rico': 'PR', 'guam': 'GU', 'virgin islands': 'VI',
}

VALID_OMB_RACE_CODES = {'1002-5', '2028-9', '2054-5', '2076-8', '2106-3'}
PROBLEMATIC_EXTENSIONS = [
    'http://hl7.org/fhir/us/core/StructureDefinition/us-core-sex',
    'http://hl7.org/fhir/us/core/StructureDefinition/us-core-interpreter-needed',
]


def sanitize_patient_resource(resource):
    if resource.get('resourceType') != 'Patient':
        return resource
    
    if 'address' in resource:
        for addr in resource['address']:
            if 'state' in addr and len(addr['state']) > 2:
                addr['state'] = STATE_TO_USPS.get(addr['state'].lower(), addr['state'])
    
    if 'extension' in resource:
        fixed = []
        for ext in resource['extension']:
            url = ext.get('url', '')
            if url in PROBLEMATIC_EXTENSIONS:
                continue
            if 'us-core-race' in url and 'extension' in ext:
                valid = [e for e in ext['extension'] 
                        if e.get('url') == 'text' or 
                        (e.get('url') == 'ombCategory' and 
                         e.get('valueCoding', {}).get('code', '').split('#')[-1] in VALID_OMB_RACE_CODES)]
                if valid:
                    ext['extension'] = valid
                    fixed.append(ext)
                continue
            fixed.append(ext)
        resource['extension'] = fixed or None
        if not resource.get('extension'):
            resource.pop('extension', None)
    
    if 'meta' in resource and 'profile' in resource['meta']:
        resource['meta']['profile'] = ['http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient']
    
    return resource


def sanitize_allergy_resource(resource):
    """Sanitize AllergyIntolerance for HealthLake compatibility."""
    if resource.get('resourceType') != 'AllergyIntolerance':
        return resource
    
    # Ensure required clinicalStatus is present
    if 'clinicalStatus' not in resource:
        resource['clinicalStatus'] = {
            'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical', 'code': 'active'}]
        }
    
    # Ensure verificationStatus is present
    if 'verificationStatus' not in resource:
        resource['verificationStatus'] = {
            'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/allergyintolerance-verification', 'code': 'confirmed'}]
        }
    
    # Ensure patient reference exists
    if 'patient' not in resource:
        return None  # Can't sync without patient
    
    # Fix empty reaction/manifestation arrays - HealthLake requires content
    if 'reaction' in resource:
        valid_reactions = []
        for reaction in resource['reaction']:
            if 'manifestation' in reaction:
                # Filter out empty manifestation objects
                valid_manifestations = []
                for m in reaction['manifestation']:
                    # Check if manifestation has actual content (coding or text)
                    if m.get('coding') or m.get('text'):
                        valid_manifestations.append(m)
                
                if valid_manifestations:
                    reaction['manifestation'] = valid_manifestations
                    valid_reactions.append(reaction)
                elif reaction.get('description') or reaction.get('severity'):
                    # Reaction has other content but empty manifestation - add placeholder
                    reaction['manifestation'] = [{
                        'coding': [{'system': 'http://snomed.info/sct', 'code': '261665006', 'display': 'Unknown'}],
                        'text': 'Unknown manifestation'
                    }]
                    valid_reactions.append(reaction)
                # else: skip reaction with only empty manifestation
            else:
                # Reaction without manifestation array - keep if has other content
                if reaction.get('description') or reaction.get('severity') or reaction.get('substance'):
                    valid_reactions.append(reaction)
        
        if valid_reactions:
            resource['reaction'] = valid_reactions
        else:
            del resource['reaction']
    
    # Remove problematic extensions
    if 'extension' in resource:
        resource['extension'] = [e for e in resource['extension'] if e.get('url') not in PROBLEMATIC_EXTENSIONS]
        if not resource['extension']:
            del resource['extension']
    
    return resource


def sanitize_immunization_resource(resource):
    """Sanitize Immunization for HealthLake compatibility."""
    if resource.get('resourceType') != 'Immunization':
        return resource
    
    # Ensure required status is present
    if 'status' not in resource:
        resource['status'] = 'completed'
    
    # Ensure patient reference exists
    if 'patient' not in resource:
        return None  # Can't sync without patient
    
    # Ensure vaccineCode is present
    if 'vaccineCode' not in resource:
        resource['vaccineCode'] = {
            'coding': [{'system': 'http://hl7.org/fhir/sid/cvx', 'code': '999', 'display': 'Unknown vaccine'}]
        }
    
    # Ensure occurrenceDateTime or occurrenceString is present
    if 'occurrenceDateTime' not in resource and 'occurrenceString' not in resource:
        resource['occurrenceString'] = 'Unknown'
    
    # Remove problematic extensions
    if 'extension' in resource:
        resource['extension'] = [e for e in resource['extension'] if e.get('url') not in PROBLEMATIC_EXTENSIONS]
        if not resource['extension']:
            del resource['extension']
    
    return resource


def sanitize_fhir_resource(resource):
    if resource is None:
        return None
    if isinstance(resource, dict):
        return {k: sanitize_fhir_resource(v) for k, v in resource.items() 
                if v is not None and v != '' and v != [] and v != {}}
    if isinstance(resource, list):
        return [sanitize_fhir_resource(i) for i in resource if i is not None and i != '' and i != {} and i != []]
    return resource


def fetch_batch(token, resource_type, cursor, batch_size):
    """Fetch a batch of resources starting from cursor."""
    fhir_url = f"{OPENEMR_BASE_URL}/apis/default/fhir/{resource_type}"
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/fhir+json'}
    
    resources = []
    last_cursor = cursor
    
    while len(resources) < batch_size:
        params = {'_count': str(PAGE_SIZE), '_sort': '_lastUpdated'}
        
        # DocumentReference in OpenEMR doesn't support _lastUpdated filtering
        # Fetch all each time and let HealthLake PUT (upsert) handle dedup
        if resource_type == 'DocumentReference':
            # No cursor filter - fetch all, paginate with _offset
            params.pop('_sort', None)
            if len(resources) > 0:
                params['_offset'] = str(len(resources))
        elif last_cursor:
            params['_lastUpdated'] = f'gt{last_cursor}'
        else:
            params['_lastUpdated'] = 'ge2020-01-01'
        
        try:
            response = requests.get(fhir_url, params=params, headers=headers, verify=OPENEMR_SSL_VERIFY, timeout=120)
            if response.status_code != 200:
                print(f"Error fetching {resource_type}: {response.status_code}")
                break
            
            bundle = response.json()
            entries = bundle.get('entry', [])
            
            if not entries:
                break
            
            for entry in entries:
                if 'resource' in entry:
                    res = entry['resource']
                    resources.append(res)
                    lu = res.get('meta', {}).get('lastUpdated')
                    if lu:
                        last_cursor = lu
            
            if len(entries) < PAGE_SIZE:
                break  # No more data
                
        except Exception as e:
            print(f"Error fetching batch: {e}")
            break
    
    return resources[:batch_size], last_cursor


def send_to_healthlake(resource):
    resource_type = resource.get('resourceType')
    
    # Apply type-specific sanitization
    if resource_type == 'Patient':
        resource = sanitize_patient_resource(resource)
    elif resource_type == 'AllergyIntolerance':
        resource = sanitize_allergy_resource(resource)
        if resource is None:
            return 400, 'Skipped: missing patient reference'
    elif resource_type == 'Immunization':
        resource = sanitize_immunization_resource(resource)
        if resource is None:
            return 400, 'Skipped: missing patient reference'
    
    # General sanitization (remove nulls, empty values)
    resource = sanitize_fhir_resource(resource)
    
    url = f"{HEALTHLAKE_ENDPOINT}{resource['resourceType']}/{resource['id']}"
    body = json.dumps(resource)
    
    request = AWSRequest(method='PUT', url=url, data=body, 
                        headers={'Content-Type': 'application/fhir+json'})
    SigV4Auth(session.get_credentials(), 'healthlake', 'us-east-1').add_auth(request)
    
    response = requests.put(url, data=body, headers=dict(request.headers), timeout=30)
    response.raise_for_status()
    return response.status_code, response.text


def sync_clinical_notes_from_db():
    """Sync clinical notes directly from OpenEMR database.
    
    OpenEMR doesn't expose form_clinical_notes via FHIR DocumentReference endpoint,
    so we query the database directly. Uses >= cursor to catch same-day updates.
    """
    import base64
    
    if not DB_SECRET_ARN:
        print("No DB_SECRET_ARN configured, skipping clinical notes")
        return 0, 0
    
    synced, failed = 0, 0
    
    try:
        import pymysql
        import ssl
        
        # Get DB credentials
        response = secrets_client.get_secret_value(SecretId=DB_SECRET_ARN)
        creds = json.loads(response['SecretString'])
        
        # Verify the Aurora/RDS server certificate instead of disabling TLS
        # verification (previously CERT_NONE, which allowed MITM). Verify
        # against the packaged Amazon RDS CA bundle if available, otherwise
        # the system trust store. Verification stays ON either way.
        ca_bundle = os.environ.get("RDS_CA_BUNDLE")
        if ca_bundle and os.path.exists(ca_bundle):
            ssl_context = ssl.create_default_context(cafile=ca_bundle)
        else:
            ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = True
        ssl_context.verify_mode = ssl.CERT_REQUIRED

        conn = pymysql.connect(
            host=creds['host'],
            user=creds['username'],
            password=creds['password'],
            database='openemr',
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            read_timeout=60,
            ssl=ssl_context
        )
        
        # Get cursor
        state = get_sync_state('ClinicalNote')
        cursor_value = state.get('cursor')
        
        with conn.cursor() as cursor:
            # Get patient UUID mapping
            cursor.execute("SELECT pid, uuid FROM patient_data WHERE uuid IS NOT NULL")
            patient_map = {}
            for r in cursor.fetchall():
                uuid_val = r['uuid']
                if isinstance(uuid_val, bytes):
                    hex_str = uuid_val.hex()
                else:
                    hex_str = str(uuid_val).replace('-', '')
                if len(hex_str) == 32:
                    patient_map[r['pid']] = f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:]}"
            
            # Get encounter UUID mapping
            cursor.execute("SELECT encounter, uuid FROM form_encounter WHERE uuid IS NOT NULL")
            encounter_map = {}
            for r in cursor.fetchall():
                uuid_val = r['uuid']
                if isinstance(uuid_val, bytes):
                    hex_str = uuid_val.hex()
                else:
                    hex_str = str(uuid_val).replace('-', '')
                if len(hex_str) == 32:
                    encounter_map[r['encounter']] = f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:]}"
            
            # Fetch notes (use >= for same-day detection)
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
            print(f"Found {len(rows)} clinical notes to sync")
            
            max_date = None
            for row in rows:
                patient_uuid = patient_map.get(row['pid'])
                if not patient_uuid:
                    continue
                
                encounter_uuid = encounter_map.get(row['encounter'])
                note_content = row.get('description') or ''
                note_base64 = base64.b64encode(note_content.encode('utf-8')).decode('utf-8')
                
                # Build DocumentReference resource
                note_date = row.get('date')
                if note_date and hasattr(note_date, 'isoformat'):
                    if hasattr(note_date, 'hour'):
                        date_str = note_date.isoformat() + '+00:00'
                    else:
                        date_str = note_date.isoformat() + 'T00:00:00+00:00'
                else:
                    date_str = datetime.utcnow().isoformat() + '+00:00'
                
                resource = {
                    'resourceType': 'DocumentReference',
                    'id': f"note-{row['id']}",
                    'meta': {'lastUpdated': date_str},
                    'status': 'current',
                    'type': {
                        'coding': [{'system': 'http://loinc.org', 'code': '11506-3', 'display': 'Progress note'}],
                        'text': 'Progress note'
                    },
                    'category': [{'coding': [{'system': 'http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category', 'code': 'clinical-note', 'display': 'Clinical Note'}]}],
                    'subject': {'reference': f'Patient/{patient_uuid}'},
                    'date': date_str,
                    'content': [{'attachment': {'contentType': 'text/plain', 'data': note_base64, 'title': 'Progress note'}}]
                }
                
                if encounter_uuid:
                    resource['context'] = {'encounter': [{'reference': f'Encounter/{encounter_uuid}'}]}
                
                status, text = send_to_healthlake(resource)
                if status in [200, 201]:
                    synced += 1
                    if note_date and (max_date is None or str(note_date) > str(max_date)):
                        max_date = note_date
                else:
                    failed += 1
                    if failed <= 3:
                        print(f"Failed ClinicalNote/note-{row['id']}: {status}")
            
            # Update cursor
            if max_date:
                cursor_str = max_date.isoformat() if hasattr(max_date, 'isoformat') else str(max_date)
                update_sync_state('ClinicalNote', last_sync=datetime.utcnow().isoformat() + 'Z', cursor=cursor_str)
        
        conn.close()
        print(f"ClinicalNote: synced={synced}, failed={failed}")
        
    except Exception as e:
        print(f"Error syncing clinical notes from DB: {e}")
        import traceback
        traceback.print_exc()
    
    return synced, failed


def handler(event, context):
    """
    Batch processing Lambda handler.
    
    Each invocation:
    1. Picks up where last batch left off (using cursor)
    2. Processes BATCH_SIZE records
    3. Saves cursor for next invocation
    4. Returns status indicating if more work remains
    """
    print(f"Batch sync started at {datetime.utcnow().isoformat()}")
    batch_id = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    
    # Get token
    try:
        token = get_openemr_token()
    except Exception as e:
        return {'statusCode': 500, 'error': str(e)}
    
    results = {'synced': 0, 'failed': 0, 'by_type': {}, 'has_more': False}
    
    for resource_type in RESOURCE_TYPES:
        # Check remaining time
        if context and context.get_remaining_time_in_millis() < 60000:
            print(f"Time running low, stopping before {resource_type}")
            results['has_more'] = True
            break
        
        state = get_sync_state(resource_type)
        cursor = state['cursor']
        
        print(f"Processing {resource_type}, cursor: {cursor or 'start'}")
        
        # Fetch batch
        resources, new_cursor = fetch_batch(token, resource_type, cursor, BATCH_SIZE)
        
        if not resources:
            print(f"No more {resource_type} to sync")
            if cursor:  # Had a cursor, batch complete
                clear_cursor(resource_type, datetime.utcnow().isoformat() + 'Z')
            continue
        
        print(f"Processing {len(resources)} {resource_type}")
        
        type_synced = 0
        type_failed = 0
        
        for i, resource in enumerate(resources):
            # Time check every 50 records
            if i % 50 == 0 and context and context.get_remaining_time_in_millis() < 30000:
                print(f"Time limit approaching, saving progress at record {i}")
                results['has_more'] = True
                break
            
            status, text = send_to_healthlake(resource)
            if status in [200, 201]:
                type_synced += 1
            else:
                type_failed += 1
                if type_failed <= 5:
                    print(f"Failed {resource_type}/{resource.get('id')}: {status}")
                    # Response body can echo back submitted FHIR resource (PHI);
                    # gate it behind DEBUG_LOG_PHI for local troubleshooting only.
                    if os.environ.get("DEBUG_LOG_PHI", "false").lower() == "true":
                        print(f"  Error: {text[:500]}")
                    else:
                        print("  Error body suppressed (set DEBUG_LOG_PHI=true to log)")
        
        results['synced'] += type_synced
        results['failed'] += type_failed
        results['by_type'][resource_type] = {'synced': type_synced, 'failed': type_failed}
        
        # Save cursor for next batch
        if new_cursor and len(resources) >= BATCH_SIZE:
            update_sync_state(resource_type, cursor=new_cursor, batch_id=batch_id, in_progress=True)
            results['has_more'] = True
            print(f"Saved cursor for {resource_type}: {new_cursor}")
        else:
            # Batch complete for this type
            clear_cursor(resource_type, datetime.utcnow().isoformat() + 'Z')
            print(f"Completed {resource_type}")
    
    # Sync clinical notes directly from database (not available via FHIR API)
    if context and context.get_remaining_time_in_millis() > 60000:
        print("Syncing ClinicalNotes from database...")
        notes_synced, notes_failed = sync_clinical_notes_from_db()
        results['synced'] += notes_synced
        results['failed'] += notes_failed
        results['by_type']['ClinicalNote'] = {'synced': notes_synced, 'failed': notes_failed}
    
    print(f"Batch complete. Synced: {results['synced']}, Failed: {results['failed']}, More: {results['has_more']}")
    return {'statusCode': 200, 'body': json.dumps(results)}
