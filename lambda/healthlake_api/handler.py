"""
Lambda function to query HealthLake FHIR API.
Provides a secure proxy for the frontend to access patient data.
Includes Bedrock integration for AI summaries and chat.
"""

import json
import os
import boto3
from urllib.parse import urlencode, quote

# HealthLake configuration
HEALTHLAKE_ENDPOINT = os.environ.get('HEALTHLAKE_ENDPOINT')
HEALTHLAKE_DATASTORE_ID = os.environ.get('HEALTHLAKE_DATASTORE_ID')
ORTHANC_CREDENTIALS_SECRET_ARN = os.environ.get('ORTHANC_CREDENTIALS_SECRET_ARN')
# Responsible-AI Bedrock Guardrail (optional). When both are set, the guardrail
# is applied to every model invocation for the summary/chat features.
BEDROCK_GUARDRAIL_ID = os.environ.get('BEDROCK_GUARDRAIL_ID')
BEDROCK_GUARDRAIL_VERSION = os.environ.get('BEDROCK_GUARDRAIL_VERSION')

# Initialize clients
healthlake_client = boto3.client('healthlake', region_name='us-east-1')
bedrock_client = boto3.client('bedrock-runtime', region_name='us-east-1')


def _get_orthanc_password():
    """Resolve the Orthanc password without hardcoding a default.

    Prefers the Secrets Manager secret referenced by
    ORTHANC_CREDENTIALS_SECRET_ARN, then falls back to the ORTHANC_PASS
    environment variable (for local development).
    """
    if ORTHANC_CREDENTIALS_SECRET_ARN:
        try:
            sm = boto3.client('secretsmanager', region_name='us-east-1')
            secret = sm.get_secret_value(SecretId=ORTHANC_CREDENTIALS_SECRET_ARN)
            return json.loads(secret['SecretString']).get('password', '')
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: could not read Orthanc secret: {exc}")
    return os.environ.get('ORTHANC_PASS', '')


def make_healthlake_request(resource_type, params=None, resource_id=None):
    """Make a request to HealthLake FHIR API."""
    import urllib.request
    import urllib.error
    from urllib.parse import urlparse
    
    # Get credentials for signing
    session = boto3.Session()
    credentials = session.get_credentials()
    
    # Build URL
    if resource_id:
        url = f"{HEALTHLAKE_ENDPOINT}{resource_type}/{resource_id}"
    else:
        url = f"{HEALTHLAKE_ENDPOINT}{resource_type}"
        if params:
            url += "?" + urlencode(params)
    
    # Validate URL scheme to prevent file:// or other unsafe schemes
    parsed = urlparse(url)
    if parsed.scheme not in ('https', 'http'):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
    
    # Use SigV4 signing via requests-aws4auth or manual signing
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    
    request = AWSRequest(method='GET', url=url, headers={
        'Content-Type': 'application/fhir+json',
        'Accept': 'application/fhir+json',
    })
    
    SigV4Auth(credentials, 'healthlake', 'us-east-1').add_auth(request)
    
    # Make the request
    req = urllib.request.Request(
        url,
        headers=dict(request.headers),
        method='GET'
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:  # nosec B310 - trusted HealthLake endpoint, scheme validated above
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else str(e)
        print(f"HealthLake error: {e.code} - {error_body}")
        raise


def get_patients(params=None):
    """Get list of patients with pagination to fetch all."""
    import urllib.request
    import urllib.error
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from urllib.parse import urlencode, urlparse, parse_qs, quote
    
    query_params = {'_count': '100'}
    if params:
        # Map 'name' to FHIR search parameter
        if 'name' in params:
            query_params['name:contains'] = params['name']
        else:
            query_params.update(params)
    
    # Fetch first page
    result = make_healthlake_request('Patient', query_params)
    all_entries = result.get('entry', [])
    
    # Follow pagination links using POST-based search to avoid SigV4 URL signing issues
    max_pages = 10  # Safety limit
    page = 1
    while page < max_pages:
        next_link = None
        for l in result.get('link', []):
            if l.get('relation') == 'next':
                next_link = l['url']
                break
        if not next_link:
            break
        
        page += 1
        try:
            # Extract the page token from the next link
            parsed = urlparse(next_link)
            next_params = parse_qs(parsed.query)
            
            # Build a POST _search request with the page token
            # This avoids SigV4 issues with long query strings
            search_url = f"{HEALTHLAKE_ENDPOINT}Patient/_search"
            form_params = {}
            for k, v in next_params.items():
                form_params[k] = v[0] if len(v) == 1 else v
            form_body = urlencode(form_params)
            
            session_local = boto3.Session()
            credentials = session_local.get_credentials()
            
            aws_request = AWSRequest(
                method='POST', 
                url=search_url, 
                data=form_body,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': 'application/fhir+json',
                }
            )
            SigV4Auth(credentials, 'healthlake', 'us-east-1').add_auth(aws_request)
            
            req = urllib.request.Request(
                search_url, 
                data=form_body.encode('utf-8'),
                headers=dict(aws_request.headers), 
                method='POST'
            )
            # nosec B310 - URL is built from the trusted HEALTHLAKE_ENDPOINT
            # env var (https), not user input; scheme validated on the initial request.
            with urllib.request.urlopen(req, timeout=30) as response:  # nosec B310
                result = json.loads(response.read().decode('utf-8'))
                new_entries = result.get('entry', [])
                all_entries.extend(new_entries)
                print(f"Pagination page {page}: got {len(new_entries)} more patients")
        except Exception as e:
            print(f"Pagination error on page {page}: {e}")
            break
    
    print(f"Total patients fetched: {len(all_entries)}")
    
    # Return combined result
    return {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'total': len(all_entries),
        'entry': all_entries
    }


def get_patient(patient_id):
    """Get a specific patient."""
    return make_healthlake_request('Patient', resource_id=patient_id)


def get_patient_resources(patient_id, resource_type):
    """Get resources for a specific patient."""
    params = {'patient': patient_id, '_count': '100'}
    return make_healthlake_request(resource_type, params)


def get_imaging_studies(patient_id=None):
    """Get imaging studies, optionally filtered by patient."""
    params = {'_count': '100'}
    if patient_id:
        params['patient'] = patient_id
    return make_healthlake_request('ImagingStudy', params)


def get_clinical_notes(patient_id):
    """Get clinical notes (DocumentReference with category=clinical-note) for a patient, sorted by date descending."""
    import base64
    
    params = {
        'subject': f'Patient/{patient_id}',
        'category': 'clinical-note',
        '_count': '50',
        '_sort': '-date'  # Most recent first
    }
    result = make_healthlake_request('DocumentReference', params)
    
    # Decode the base64 content for each note
    if result.get('entry'):
        for entry in result['entry']:
            resource = entry.get('resource', {})
            content_list = resource.get('content', [])
            for content in content_list:
                attachment = content.get('attachment', {})
                if attachment.get('data'):
                    try:
                        decoded = base64.b64decode(attachment['data']).decode('utf-8')
                        attachment['decodedText'] = decoded
                    except Exception as e:
                        print(f"Error decoding note: {e}")
    
    return result


def get_orthanc_studies(patient_name):
    """Get imaging studies from Orthanc by patient name."""
    import urllib.request
    import urllib.error
    import base64
    
    orthanc_url = os.environ.get('ORTHANC_URL', 'http://localhost:8042')
    orthanc_user = os.environ.get('ORTHANC_USER', 'admin')
    orthanc_pass = _get_orthanc_password()
    
    # Build DICOMweb query URL
    url = f"{orthanc_url}/dicom-web/studies?PatientName={quote(patient_name, safe='')}*"
    
    # Validate URL scheme
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ('https', 'http'):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
    
    # Basic auth
    auth = base64.b64encode(f"{orthanc_user}:{orthanc_pass}".encode()).decode()
    
    req = urllib.request.Request(
        url,
        headers={
            'Authorization': f'Basic {auth}',
            'Accept': 'application/dicom+json',
        },
        method='GET'
    )
    
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 - trusted Orthanc endpoint, scheme validated above
            studies = json.loads(response.read().decode('utf-8'))
            # Transform to simpler format
            result = []
            for s in studies:
                result.append({
                    'studyUid': s.get('0020000D', {}).get('Value', [''])[0],
                    'description': s.get('00081030', {}).get('Value', [''])[0] if '00081030' in s else None,
                    'modality': s.get('00080061', {}).get('Value', [''])[0] if '00080061' in s else None,
                    'date': s.get('00080020', {}).get('Value', [''])[0] if '00080020' in s else None,
                    'accession': s.get('00080050', {}).get('Value', [''])[0] if '00080050' in s else None,
                })
            return result
    except Exception as e:
        print(f"Orthanc error: {e}")
        return []


def cors_response(status_code, body):
    """Return response with CORS headers."""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
            'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
        },
        'body': json.dumps(body) if isinstance(body, (dict, list)) else body
    }


def format_patient_context(patient_data):
    """Format patient data into a context string for the AI."""
    ctx = []
    
    p = patient_data.get('patient', {})
    name = p.get('name', [{}])[0]
    full_name = f"{' '.join(name.get('given', []))} {name.get('family', '')}"
    ctx.append(f"Patient: {full_name}")
    ctx.append(f"DOB: {p.get('birthDate', 'Unknown')}, Gender: {p.get('gender', 'Unknown')}")
    
    # Clinical Notes - most important, show first
    notes = patient_data.get('clinicalNotes', [])
    if notes:
        ctx.append(f"\n=== CLINICAL NOTES ({len(notes)}) ===")
        # Show most recent notes first (already sorted by API)
        for note in notes[:3]:  # Show up to 3 most recent notes
            note_type = note.get('type', {}).get('text', 'Clinical Note')
            note_date = note.get('date', '')[:10] if note.get('date') else 'Unknown date'
            ctx.append(f"\n--- {note_type} ({note_date}) ---")
            
            # Get the decoded text content
            content_list = note.get('content', [])
            for content in content_list:
                attachment = content.get('attachment', {})
                decoded_text = attachment.get('decodedText', '')
                if decoded_text:
                    # Truncate very long notes for context
                    if len(decoded_text) > 2000:
                        decoded_text = decoded_text[:2000] + "\n[Note truncated...]"
                    ctx.append(decoded_text)
    
    conditions = patient_data.get('conditions', [])
    if conditions:
        active = [c for c in conditions if c.get('clinicalStatus', {}).get('coding', [{}])[0].get('code') == 'active']
        resolved = [c for c in conditions if c.get('clinicalStatus', {}).get('coding', [{}])[0].get('code') != 'active']
        if active:
            ctx.append(f"\nActive Conditions ({len(active)}):")
            for c in active[:10]:
                display = c.get('code', {}).get('coding', [{}])[0].get('display', c.get('code', {}).get('text', 'Unknown'))
                ctx.append(f"  - {display}")
        if resolved:
            ctx.append(f"\nResolved Conditions ({len(resolved)}):")
            for c in resolved[:5]:
                display = c.get('code', {}).get('coding', [{}])[0].get('display', c.get('code', {}).get('text', 'Unknown'))
                ctx.append(f"  - {display}")
    
    allergies = patient_data.get('allergies', [])
    if allergies:
        ctx.append(f"\nAllergies ({len(allergies)}):")
        for a in allergies[:10]:
            display = a.get('code', {}).get('coding', [{}])[0].get('display', a.get('code', {}).get('text', 'Unknown'))
            criticality = a.get('criticality', '')
            ctx.append(f"  - {display}" + (f" (criticality: {criticality})" if criticality else ""))
    
    meds = patient_data.get('medications', [])
    if meds:
        ctx.append(f"\nMedications ({len(meds)}):")
        for m in meds[:10]:
            display = m.get('medicationCodeableConcept', {}).get('coding', [{}])[0].get('display', 
                     m.get('medicationCodeableConcept', {}).get('text', 'Unknown'))
            ctx.append(f"  - {display}")
    
    immuns = patient_data.get('immunizations', [])
    if immuns:
        ctx.append(f"\nImmunizations ({len(immuns)}):")
        for i in immuns[:10]:
            display = i.get('vaccineCode', {}).get('coding', [{}])[0].get('display', 'Unknown')
            date = i.get('occurrenceDateTime', '')[:10] if i.get('occurrenceDateTime') else ''
            ctx.append(f"  - {display}" + (f" ({date})" if date else ""))
    
    encounters = patient_data.get('encounters', [])
    if encounters:
        ctx.append(f"\nRecent Encounters ({len(encounters)}):")
        for e in encounters[:5]:
            etype = e.get('type', [{}])[0].get('coding', [{}])[0].get('display', 
                   e.get('class', {}).get('display', e.get('class', {}).get('code', 'Visit')))
            date = e.get('period', {}).get('start', '')[:10] if e.get('period', {}).get('start') else ''
            ctx.append(f"  - {etype}" + (f" ({date})" if date else ""))
    
    imaging = patient_data.get('imaging', [])
    if imaging:
        ctx.append(f"\nImaging Studies ({len(imaging)}):")
        for img in imaging[:5]:
            desc = img.get('description') or img.get('modality') or 'Study'
            date = img.get('date', '')
            if date and len(date) == 8:
                date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
            ctx.append(f"  - {desc}" + (f" ({date})" if date else ""))
    
    return '\n'.join(ctx)


def call_bedrock(system_prompt, user_message, max_tokens=400):
    """Call Bedrock Nova for AI responses.

    When a Bedrock Guardrail is configured (BEDROCK_GUARDRAIL_ID /
    BEDROCK_GUARDRAIL_VERSION), it is applied to the invocation as a
    responsible-AI control. If the guardrail blocks the request or response,
    a safe fallback message is returned instead of model output.
    """
    invoke_kwargs = {
        'modelId': 'amazon.nova-lite-v1:0',
        'contentType': 'application/json',
        'accept': 'application/json',
        'body': json.dumps({
            'inferenceConfig': {'max_new_tokens': max_tokens},
            'system': [{'text': system_prompt}],
            'messages': [{'role': 'user', 'content': [{'text': user_message}]}]
        })
    }

    # Apply the guardrail when configured (defense-in-depth over the prompts).
    if BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION:
        invoke_kwargs['guardrailIdentifier'] = BEDROCK_GUARDRAIL_ID
        invoke_kwargs['guardrailVersion'] = BEDROCK_GUARDRAIL_VERSION

    response = bedrock_client.invoke_model(**invoke_kwargs)
    result = json.loads(response['body'].read())

    # If the guardrail intervened, surface a safe, non-clinical message rather
    # than any (possibly blocked/empty) model content.
    if result.get('amazon-bedrock-guardrailAction') == 'INTERVENED':
        print("Bedrock guardrail INTERVENED on this request")
        return (
            "I can only report documented facts from the patient's record and "
            "cannot provide medical advice, diagnoses, or treatment guidance. "
            "Please consult a qualified clinician."
        )

    return result['output']['message']['content'][0]['text']


def generate_patient_summary(patient_data):
    """Generate an AI summary of the patient's health profile - facts only."""
    context = format_patient_context(patient_data)
    
    system_prompt = """You are a clinical data summarizer. Your ONLY job is to summarize the patient data provided.

STRICT RULES:
1. ONLY state facts explicitly present in the provided data
2. NEVER infer, assume, or speculate about anything not in the data
3. NEVER suggest diagnoses, treatments, or clinical interpretations
4. NEVER make connections between conditions unless explicitly stated
5. If data is missing or limited, simply state what IS available
6. Use phrases like "The record shows..." or "According to the data..."
7. Do NOT use "may indicate", "could suggest", "likely", or "possibly"

Provide a brief factual summary (3-4 sentences) listing only what is documented."""

    return call_bedrock(system_prompt, f"Summarize ONLY the facts in this patient record:\n\n{context}")


def patient_chat(patient_data, question, chat_history=None):
    """Answer questions about the patient's data - facts only."""
    context = format_patient_context(patient_data)
    
    system_prompt = f"""You answer questions using ONLY the patient data below. Nothing else.

PATIENT DATA:
{context}

STRICT RULES:
1. ONLY answer based on facts explicitly in the data above
2. If information is NOT in the data, say: "That information is not in the patient's record."
3. NEVER infer, assume, or speculate beyond what is documented
4. NEVER suggest diagnoses, treatments, or clinical interpretations
5. NEVER make connections unless explicitly stated in the data
6. Do NOT use "may", "might", "could", "likely", "possibly", or "suggests"
7. Use "The record shows...", "According to the data...", "Documented:"
8. For medical advice questions, say: "I can only report what is in the record."

Be concise. Only report documented facts."""

    if chat_history:
        history_text = "\n".join([f"Q: {h['question']}\nA: {h['answer']}" for h in chat_history[-3:]])
        user_message = f"Previous:\n{history_text}\n\nQuestion: {question}"
    else:
        user_message = question
    
    return call_bedrock(system_prompt, user_message)


def handler(event, context):
    """Lambda handler."""
    # Avoid logging the full event by default - HealthLake requests/responses
    # can contain PHI. Set DEBUG_LOG_EVENTS=true to opt in for troubleshooting.
    if os.environ.get("DEBUG_LOG_EVENTS", "false").lower() == "true":
        print(f"Event: {json.dumps(event)}")
    else:
        print(f"Event: method={event.get('httpMethod')} path={event.get('path')}")

    # Handle CORS preflight
    if event.get('httpMethod') == 'OPTIONS':
        return cors_response(200, {'message': 'OK'})
    
    path = event.get('path', '/')
    query_params = event.get('queryStringParameters') or {}
    path_params = event.get('pathParameters') or {}
    
    try:
        # Route requests
        if path == '/patients' or path == '/api/patients':
            result = get_patients(query_params)
            return cors_response(200, result)
        
        elif path.startswith('/patients/') or path.startswith('/api/patients/'):
            # Extract patient ID from path
            parts = path.split('/')
            patient_id = parts[-1] if parts[-1] else parts[-2]
            
            # Check if requesting sub-resources
            if len(parts) > 3 and parts[-2] != 'patients':
                resource_type = parts[-1]
                patient_id = parts[-2]
                
                # Map URL paths to FHIR resource types
                resource_map = {
                    'conditions': 'Condition',
                    'allergies': 'AllergyIntolerance',
                    'medications': 'MedicationRequest',
                    'encounters': 'Encounter',
                    'immunizations': 'Immunization',
                    'imaging': 'ImagingStudy',
                    'observations': 'Observation',
                    'notes': 'clinical-notes',  # Special handling for clinical notes
                }
                
                fhir_type = resource_map.get(resource_type, resource_type)
                
                # Special handling for clinical notes
                if fhir_type == 'clinical-notes':
                    result = get_clinical_notes(patient_id)
                else:
                    result = get_patient_resources(patient_id, fhir_type)
                return cors_response(200, result)
            else:
                result = get_patient(patient_id)
                return cors_response(200, result)
        
        elif path == '/imaging' or path == '/api/imaging':
            patient_id = query_params.get('patient')
            result = get_imaging_studies(patient_id)
            return cors_response(200, result)
        
        elif path == '/orthanc-imaging' or path == '/api/orthanc-imaging':
            patient_name = query_params.get('name', '')
            result = get_orthanc_studies(patient_name)
            return cors_response(200, result)
        
        elif path == '/debug/conditions' or path == '/api/debug/conditions':
            result = make_healthlake_request('Condition', {'_count': '10'})
            return cors_response(200, result)
        
        elif path == '/patient-summary' or path == '/api/patient-summary':
            if event.get('httpMethod') != 'POST':
                return cors_response(405, {'error': 'POST required'})
            body = json.loads(event.get('body', '{}'))
            patient_data = body.get('patientData', {})
            if not patient_data:
                return cors_response(400, {'error': 'patientData required'})
            summary = generate_patient_summary(patient_data)
            return cors_response(200, {'summary': summary})
        
        elif path == '/patient-chat' or path == '/api/patient-chat':
            if event.get('httpMethod') != 'POST':
                return cors_response(405, {'error': 'POST required'})
            body = json.loads(event.get('body', '{}'))
            patient_data = body.get('patientData', {})
            question = body.get('question', '')
            chat_history = body.get('chatHistory', [])
            if not patient_data or not question:
                return cors_response(400, {'error': 'patientData and question required'})
            answer = patient_chat(patient_data, question, chat_history)
            return cors_response(200, {'answer': answer})
        
        else:
            return cors_response(404, {'error': 'Not found', 'path': path})
    
    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return cors_response(500, {'error': str(e)})
