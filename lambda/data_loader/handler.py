"""
Automatic Data Loader Lambda

Loads Synthea-generated FHIR patient data into OpenEMR and generates
corresponding DICOM images for upload to Orthanc after CDK deployment completes.

PHI / HIPAA NOTICE:
This Lambda processes FHIR patient records and DICOM medical imaging - both
protected health information (PHI) under HIPAA. If you process real PHI, this is
a HIPAA-regulated workload: execute an AWS Business Associate Addendum (BAA),
keep data within HIPAA-eligible services, and enable encryption, access logging,
and audit controls. Set DEBUG_LOG_PHI / DEBUG_LOG_EVENTS only in non-regulated
environments. The customer is responsible for compliant handling of regulated
data. This sample ships with synthetic Synthea data only.

This Lambda is triggered as a CDK custom resource to provide a "batteries included"
demo experience where a single `cdk deploy` results in a fully populated system.

IDEMPOTENCY:
    This Lambda includes idempotency protection to prevent duplicate patient data
    on re-deployment. Before loading data, it checks if patients already exist in
    the database. If patients are found, the load is skipped.

    To force a reload (e.g., after clearing the database), you can:
    1. Set the FORCE_RELOAD=true environment variable on the Lambda
    2. Pass ForceReload=true in the CDK custom resource properties

    This ensures that running `cdk deploy` multiple times won't create duplicate
    patients (which previously resulted in 4000 patients instead of 500 after
    8 deployments during troubleshooting).
"""

import json
import os
import boto3
import mysql.connector
from mysql.connector import Error as MySQLError
from datetime import datetime, timedelta
import random
import uuid
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

# DICOM imports
import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import generate_uid, ExplicitVRLittleEndian
import numpy as np
import requests

# FHIR parser imports
from fhir_parser import (
    FHIRParser,
    PatientRecord,
    ConditionRecord,
    AllergyRecord,
    MedicationRecord,
    ImmunizationRecord,
    EncounterRecord,
    ClinicalNoteRecord,
)

# Configuration from environment
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
ORTHANC_URL = os.environ.get('ORTHANC_URL')  # Can be empty, will try SSM
ORTHANC_USER = os.environ.get('ORTHANC_USER', 'admin')
ORTHANC_CREDENTIALS_SECRET_ARN = os.environ.get('ORTHANC_CREDENTIALS_SECRET_ARN')


def _resolve_orthanc_password():
    """Resolve the Orthanc password without a hardcoded default.

    Prefers the Secrets Manager secret referenced by
    ORTHANC_CREDENTIALS_SECRET_ARN, then falls back to the ORTHANC_PASS
    environment variable (for local development).
    """
    if ORTHANC_CREDENTIALS_SECRET_ARN:
        try:
            import json as _json
            _sm = boto3.client('secretsmanager')
            _secret = _sm.get_secret_value(SecretId=ORTHANC_CREDENTIALS_SECRET_ARN)
            return _json.loads(_secret['SecretString']).get('password', '')
        except Exception as _exc:  # noqa: BLE001
            print(f"WARN: could not read Orthanc secret: {_exc}")
    return os.environ.get('ORTHANC_PASS', '')


ORTHANC_PASS = _resolve_orthanc_password()
HEALTHLAKE_SYNC_FUNCTION = os.environ.get('HEALTHLAKE_SYNC_FUNCTION')  # Lambda to trigger after data load
SYNTHEA_BUCKET = os.environ.get('SYNTHEA_BUCKET', '')
SYNTHEA_PREFIX = os.environ.get('SYNTHEA_PREFIX', 'synthea-bundles/')
# AWS_REGION is automatically set by Lambda runtime
REGION = os.environ.get('AWS_REGION', 'us-east-1')

# DICOM upload is parallelized with ThreadPoolExecutor for performance
# Number of imaging studies per patient
STUDIES_PER_PATIENT = 1

secrets_client = boto3.client('secretsmanager', region_name=REGION)
ssm_client = boto3.client('ssm', region_name=REGION)
lambda_client = boto3.client('lambda', region_name=REGION)
s3_client = boto3.client('s3', region_name=REGION)


def get_orthanc_url():
    """Get Orthanc URL from environment or SSM Parameter Store."""
    # First check environment variable
    if ORTHANC_URL:
        return ORTHANC_URL

    # Try SSM Parameter Store (for combined deployment)
    try:
        response = ssm_client.get_parameter(Name='/openemr/orthanc-url')
        return response['Parameter']['Value']
    except Exception as e:
        print(f"Could not get Orthanc URL from SSM: {e}")
        return None


def get_db_credentials():
    """Retrieve database credentials from Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=DB_SECRET_ARN)
    return json.loads(response['SecretString'])


def get_db_connection(credentials):
    """Create database connection."""
    return mysql.connector.connect(
        host=credentials['host'],
        port=int(credentials.get('port', 3306)),
        user=credentials['username'],
        password=credentials['password'],
        database='openemr'
    )


# DICOM modality configurations
MODALITIES = [
    ("CT", "Computed Tomography", "CT Scanner"),
    ("MR", "Magnetic Resonance", "MRI Scanner"),
    ("CR", "Computed Radiography", "X-Ray"),
    ("US", "Ultrasound", "Ultrasound Machine"),
    ("DX", "Digital Radiography", "Digital X-Ray"),
]

BODY_PARTS = {
    "CT": ["HEAD", "CHEST", "ABDOMEN", "PELVIS", "SPINE"],
    "MR": ["BRAIN", "SPINE", "KNEE", "SHOULDER", "ABDOMEN"],
    "CR": ["CHEST", "HAND", "FOOT", "KNEE", "SPINE"],
    "US": ["ABDOMEN", "PELVIS", "THYROID", "HEART"],
    "DX": ["CHEST", "HAND", "FOOT", "KNEE", "ELBOW"],
}

STUDY_DESCRIPTIONS = {
    "CT": ["CT Head without contrast", "CT Chest with contrast", "CT Abdomen/Pelvis", "CT Spine"],
    "MR": ["MRI Brain with contrast", "MRI Lumbar Spine", "MRI Knee", "MRI Shoulder"],
    "CR": ["Chest X-Ray PA/Lateral", "Hand X-Ray", "Foot X-Ray", "Knee X-Ray"],
    "US": ["Abdominal Ultrasound", "Pelvic Ultrasound", "Thyroid Ultrasound", "Echocardiogram"],
    "DX": ["Chest X-Ray", "Hand X-Ray 2 views", "Foot X-Ray", "Knee X-Ray"],
}


def get_next_patient_id(cursor):
    """Get the next available patient ID from OpenEMR database."""
    cursor.execute("SELECT COALESCE(MAX(pid), 0) + 1 FROM patient_data")
    result = cursor.fetchone()
    return result[0] if result else 1


def load_patient_to_openemr(cursor, patient: PatientRecord):
    """Insert a patient and related data into OpenEMR database.

    Args:
        cursor: MySQL cursor instance.
        patient: A PatientRecord dataclass with all clinical data populated.

    Returns:
        The database patient ID (pid) assigned to the inserted patient.
    """
    # Use the Synthea patient ID as OpenEMR's uuid - single ID across all systems
    patient_uuid = uuid.UUID(patient.uuid).bytes

    # Get next available pid
    cursor.execute("SELECT COALESCE(MAX(pid), 0) + 1 FROM patient_data")
    patient_id = cursor.fetchone()[0] or 1

    # Insert patient with explicit pid
    sql = """
        INSERT INTO patient_data (
            pid, uuid, fname, lname, DOB, sex, street, city, state, postal_code,
            phone_home, email, pubpid, date
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    """
    cursor.execute(sql, (
        patient_id,
        patient_uuid,
        patient.fname, patient.lname, patient.dob, patient.sex,
        patient.street, patient.city, patient.state, patient.postal_code,
        patient.phone, patient.email, patient.uuid
    ))

    # Insert conditions
    for idx, condition in enumerate(patient.conditions):
        condition_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"openemr-condition-{patient.fname}.{patient.lname}.{patient.dob}.{idx}.{condition.snomed_code}").bytes
        cursor.execute(
            """INSERT INTO lists (uuid, date, type, title, diagnosis, pid, begdate, activity)
               VALUES (%s, NOW(), 'medical_problem', %s, %s, %s, %s, 1)""",
            (condition_uuid, condition.display[:255], condition.snomed_code[:255], patient_id, condition.onset_date[:19] if condition.onset_date else patient.dob)
        )

    # Insert allergies
    for idx, allergy in enumerate(patient.allergies):
        allergy_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"openemr-allergy-{patient.fname}.{patient.lname}.{patient.dob}.{idx}.{allergy.title}").bytes
        cursor.execute(
            """INSERT INTO lists (uuid, date, type, title, pid, begdate, activity, outcome)
               VALUES (%s, NOW(), 'allergy', %s, %s, NOW(), 1, 1)""",
            (allergy_uuid, allergy.title[:255], patient_id)
        )

    # Insert medications
    for idx, med in enumerate(patient.medications):
        med_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"openemr-medication-{patient.fname}.{patient.lname}.{patient.dob}.{idx}.{med.rxnorm_code}").bytes
        cursor.execute(
            """INSERT INTO lists (uuid, date, type, title, pid, begdate, activity)
               VALUES (%s, NOW(), 'medication', %s, %s, NOW(), 1)""",
            (med_uuid, med.title[:255], patient_id)
        )

    # Insert immunizations
    for idx, imm in enumerate(patient.immunizations):
        imm_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"openemr-immunization-{patient.fname}.{patient.lname}.{patient.dob}.{idx}.{imm.cvx_code}").bytes
        imm_id = (patient_id * 100) + idx + 1
        cursor.execute(
            """INSERT INTO immunizations (uuid, patient_id, administered_date, cvx_code, immunization_id, create_date)
               VALUES (%s, %s, %s, %s, %s, NOW())""",
            (imm_uuid, patient_id, imm.date or datetime.now().strftime('%Y-%m-%d'), imm.cvx_code, imm_id)
        )

    # Insert encounters from FHIR bundle
    encounter_nums = []
    encounter_ref_to_num = {}  # Map Synthea encounter reference to our encounter number
    for idx, encounter in enumerate(patient.encounters):
        # Deterministic UUID based on patient + encounter index + date
        encounter_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"openemr-encounter-{patient.fname}.{patient.lname}.{patient.dob}.{idx}.{encounter.date}").bytes
        # Deterministic encounter number based on patient_id and index
        encounter_num = (patient_id * 1000) + idx + 1
        encounter_nums.append(encounter_num)

        cursor.execute(
            """INSERT INTO form_encounter (uuid, date, reason, pid, encounter, facility_id, pc_catid, class_code, sensitivity)
               VALUES (%s, %s, %s, %s, %s, 3, 5, %s, 'normal')""",
            (encounter_uuid, encounter.date or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             encounter.reason, patient_id, encounter_num, encounter.class_code or 'AMB')
        )
        encounter_form_id = cursor.lastrowid

        # Register the encounter in the forms table (REQUIRED for OpenEMR to display it)
        cursor.execute(
            """INSERT INTO forms (date, encounter, form_name, form_id, pid, user, groupname, formdir, authorized, deleted)
               VALUES (%s, %s, 'New Patient Encounter', %s, %s, 'admin', 'Default', 'newpatient', 1, 0)""",
            (encounter.date or datetime.now().strftime('%Y-%m-%d %H:%M:%S'), encounter_num, encounter_form_id, patient_id)
        )

        # Track the encounter reference for clinical notes linking
        # Synthea encounter refs look like "urn:uuid:xxx" — they match the entry's fullUrl
        if encounter.full_url:
            encounter_ref_to_num[encounter.full_url] = encounter_num
        # Also store by index so we can fall back to positional matching
        encounter_ref_to_num[f"__index_{len(encounter_nums)-1}"] = encounter_num

    # If no encounters from FHIR, create a default encounter for clinical notes
    if not encounter_nums:
        encounter_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"openemr-encounter-{patient.fname}.{patient.lname}.{patient.dob}.default").bytes
        encounter_num = (patient_id * 1000) + 999
        encounter_nums.append(encounter_num)

        cursor.execute(
            """INSERT INTO form_encounter (uuid, date, reason, pid, encounter, facility_id, pc_catid, class_code, sensitivity)
               VALUES (%s, NOW(), 'Annual checkup', %s, %s, 3, 5, 'AMB', 'normal')""",
            (encounter_uuid, patient_id, encounter_num)
        )
        encounter_form_id = cursor.lastrowid

        cursor.execute(
            """INSERT INTO forms (date, encounter, form_name, form_id, pid, user, groupname, formdir, authorized, deleted)
               VALUES (NOW(), %s, 'New Patient Encounter', %s, %s, 'admin', 'Default', 'newpatient', 1, 0)""",
            (encounter_num, encounter_form_id, patient_id)
        )

    # Insert clinical notes from FHIR bundle
    # Distribute notes across encounters (Synthea typically generates one note per encounter)
    for idx, note in enumerate(patient.clinical_notes):
        if not note.content:
            continue

        # Try to match note to an encounter:
        # 1. Use encounter_ref if available and mappable (matches encounter fullUrl)
        # 2. Fall back to distributing notes across encounters by index
        note_encounter_num = encounter_nums[0] if encounter_nums else 0
        
        if note.encounter_ref and note.encounter_ref in encounter_ref_to_num:
            note_encounter_num = encounter_ref_to_num[note.encounter_ref]
        elif encounter_nums:
            # Distribute notes across encounters by index (round-robin)
            note_encounter_num = encounter_nums[idx % len(encounter_nums)]

        # Get next form_id (the join key for form_clinical_notes)
        cursor.execute("SELECT COALESCE(MAX(form_id), 0) + 1 FROM form_clinical_notes")
        next_form_id = cursor.fetchone()[0]

        # Determine the authored date. ClinicalNoteRecord has no date of its
        # own (a FHIR DocumentReference carries no reliable authored date here),
        # so derive it from the linked encounter, falling back to NOW() below.
        note_date = None
        if not note_date and note.encounter_ref and note.encounter_ref in encounter_ref_to_num:
            # Look up encounter date from the encounters list
            enc_num = encounter_ref_to_num[note.encounter_ref]
            for enc in patient.encounters:
                if encounter_ref_to_num.get(enc.full_url) == enc_num:
                    note_date = enc.date
                    break

        # Insert clinical note with all required fields
        if note_date:
            cursor.execute(
                """INSERT INTO form_clinical_notes
                   (form_id, date, pid, encounter, user, groupname, authorized, activity, code, codetext, description, clinical_notes_type, note_related_to)
                   VALUES (%s, %s, %s, %s, 'admin', 'Default', 1, 1, 'LOINC:34109-9', 'General Note', %s, 'progress_note', '[]')""",
                (next_form_id, note_date, patient_id, str(note_encounter_num), note.content)
            )
        else:
            cursor.execute(
                """INSERT INTO form_clinical_notes
                   (form_id, date, pid, encounter, user, groupname, authorized, activity, code, codetext, description, clinical_notes_type, note_related_to)
                   VALUES (%s, NOW(), %s, %s, 'admin', 'Default', 1, 1, 'LOINC:34109-9', 'General Note', %s, 'progress_note', '[]')""",
                (next_form_id, patient_id, str(note_encounter_num), note.content)
            )

        # Link clinical note in forms table (form_id references form_clinical_notes.form_id)
        if note_date:
            cursor.execute(
                """INSERT INTO forms (date, encounter, form_name, form_id, pid, user, groupname, formdir, authorized, deleted)
                   VALUES (%s, %s, 'Clinical Notes Form', %s, %s, 'admin', 'Default', 'clinical_notes', 1, 0)""",
                (note_date, note_encounter_num, next_form_id, patient_id)
            )
        else:
            cursor.execute(
                """INSERT INTO forms (date, encounter, form_name, form_id, pid, user, groupname, formdir, authorized, deleted)
                   VALUES (NOW(), %s, 'Clinical Notes Form', %s, %s, 'admin', 'Default', 'clinical_notes', 1, 0)""",
                (note_encounter_num, next_form_id, patient_id)
            )

    return patient_id


def load_data_to_openemr(patients):
    """Load all patient data into OpenEMR database.

    Args:
        patients: List of PatientRecord instances with clinical data populated.

    Returns:
        Tuple of (loaded_count, errors, patient_db_ids mapping uuid->pid).
    """
    credentials = get_db_credentials()
    connection = get_db_connection(credentials)
    cursor = connection.cursor()

    loaded_count = 0
    errors = []
    patient_db_ids = {}  # Map patient uuid to database patient_id

    try:
        for patient in patients:
            try:
                db_id = load_patient_to_openemr(cursor, patient)
                connection.commit()
                patient_db_ids[patient.uuid] = db_id
                loaded_count += 1
            except MySQLError as e:
                # Do not include patient name (PHI) in the error; use the UUID.
                errors.append(f"Patient uuid={patient.uuid}: {str(e)}")
                connection.rollback()
    finally:
        cursor.close()
        connection.close()

    return loaded_count, errors, patient_db_ids


# ============== DICOM Generation Functions ==============

def create_synthetic_image(width=512, height=512, modality="CT"):
    """Create a synthetic medical image with noise pattern."""
    np.random.seed(random.randint(0, 10000))

    if modality in ["CT", "MR"]:
        # Create circular pattern for CT/MR (like a body cross-section)
        y, x = np.ogrid[:height, :width]
        center_x, center_y = width // 2, height // 2
        r = min(width, height) // 2 - 20
        mask = ((x - center_x) ** 2 + (y - center_y) ** 2) <= r ** 2

        image = np.random.randint(20, 60, (height, width), dtype=np.uint8)
        image[mask] = np.random.randint(100, 200, mask.sum(), dtype=np.uint8)

        # Add some internal structure
        inner_r = r // 2
        inner_mask = ((x - center_x) ** 2 + (y - center_y) ** 2) <= inner_r ** 2
        image[inner_mask] = np.random.randint(60, 120, inner_mask.sum(), dtype=np.uint8)

    elif modality in ["CR", "DX"]:
        # Create X-ray like image
        image = np.random.randint(180, 220, (height, width), dtype=np.uint8)
        # Add some darker regions
        for _ in range(5):
            cx, cy = random.randint(100, width-100), random.randint(100, height-100)
            rr = random.randint(30, 80)
            y, x = np.ogrid[:height, :width]
            mask = ((x - cx) ** 2 + (y - cy) ** 2) <= rr ** 2
            image[mask] = np.clip(image[mask].astype(int) - random.randint(40, 80), 0, 255).astype(np.uint8)

    else:  # US
        # Ultrasound-like image
        image = np.random.randint(10, 40, (height, width), dtype=np.uint8)
        # Add fan shape
        for i in range(height):
            spread = int((i / height) * (width // 3))
            left = max(0, width // 2 - spread)
            right = min(width, width // 2 + spread)
            if right > left:
                image[i, left:right] = np.random.randint(40, 150, right - left, dtype=np.uint8)

    return image


def create_dicom_bytes(patient: PatientRecord, modality_info, study_date):
    """Create a DICOM file in memory and return bytes.

    Args:
        patient: A PatientRecord dataclass instance.
        modality_info: Tuple of (modality, description, equipment).
        study_date: datetime for the study.

    Returns:
        Tuple of (dicom_bytes, study_info_dict).
    """
    modality, modality_desc, equipment = modality_info
    body_part = random.choice(BODY_PARTS[modality])
    study_desc = random.choice(STUDY_DESCRIPTIONS[modality])

    # Generate UIDs
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop_instance_uid = generate_uid()

    # Create the image
    pixel_array = create_synthetic_image(512, 512, modality)

    # Create DICOM dataset
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = pydicom.uid.CTImageStorage if modality == "CT" else pydicom.uid.SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    # Create file dataset
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)

    # Patient info - use DICOM format: LastName^FirstName
    ds.PatientName = f"{patient.lname}^{patient.fname}"
    ds.PatientID = patient.uuid
    ds.PatientBirthDate = patient.dob.replace("-", "")
    ds.PatientSex = "M" if patient.sex == "Male" else "F"

    # Study info
    ds.StudyInstanceUID = study_uid
    ds.StudyDate = study_date.strftime("%Y%m%d")
    ds.StudyTime = study_date.strftime("%H%M%S")
    ds.StudyDescription = study_desc
    ds.AccessionNumber = f"ACC{random.randint(100000, 999999)}"
    ds.ReferringPhysicianName = "Dr^Demo^Provider"

    # Series info
    ds.SeriesInstanceUID = series_uid
    ds.SeriesNumber = 1
    ds.SeriesDescription = f"{modality} {body_part}"
    ds.Modality = modality
    ds.BodyPartExamined = body_part

    # Instance info
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = sop_instance_uid
    ds.InstanceNumber = 1

    # Equipment info
    ds.Manufacturer = "Synthetic Medical Imaging"
    ds.InstitutionName = "OpenEMR Demo Hospital"
    ds.StationName = equipment

    # Image info
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = 512
    ds.Columns = 512
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = pixel_array.tobytes()

    # Set creation date
    ds.ContentDate = study_date.strftime("%Y%m%d")
    ds.ContentTime = study_date.strftime("%H%M%S")

    # Write to bytes buffer as a compliant DICOM File Format stream.
    # This dataset is built from scratch, so its VR encoding defaults to
    # IMPLICIT VR — but the File Meta above declares Explicit VR Little Endian.
    # Orthanc parses the instance strictly per the declared transfer syntax and
    # rejects that mismatch with HTTP 400. Force explicit VR little-endian to
    # match the meta, and enforce the full file format (128-byte preamble +
    # "DICM" marker + File Meta group) on write.
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    buffer = io.BytesIO()
    ds.save_as(buffer, write_like_original=False)
    buffer.seek(0)

    return buffer.getvalue(), {
        "study_uid": study_uid,
        "modality": modality,
        "body_part": body_part,
        "study_description": study_desc,
        "patient_id": patient.uuid,
    }


def upload_to_orthanc(dicom_bytes, orthanc_url, username, password):
    """Upload DICOM bytes to Orthanc via REST API."""
    try:
        from urllib.parse import urlparse, urlunparse

        # Determine whether this is an internal VPC endpoint (Cloud Map .local
        # or localhost). Internal endpoints reach Orthanc's plain-HTTP listener
        # inside the private VPC and are never exposed to the internet.
        parsed = urlparse(orthanc_url)
        host = parsed.netloc or parsed.path
        is_internal = host.endswith('.local') or 'localhost' in host or host.startswith('orthanc.')

        # For any endpoint reachable outside the VPC, require TLS.
        if not is_internal and parsed.scheme != 'https':
            parsed = parsed._replace(scheme='https')
            orthanc_url = urlunparse(parsed)

        # Reject plaintext transport for external endpoints outright.
        if not is_internal and urlparse(orthanc_url).scheme != 'https':
            return False, "Refusing to send DICOM to a non-HTTPS external endpoint"

        url = f"{orthanc_url.rstrip('/')}/instances"
        response = requests.post(
            url,
            data=dicom_bytes,
            headers={'Content-Type': 'application/dicom'},
            auth=(username, password),
            timeout=30,
            verify=os.environ.get('SSL_CERT_FILE', True)  # Use custom CA bundle if provided
        )

        if response.status_code in [200, 201]:
            return True, None
        else:
            # Do not include the Orthanc response body: it can echo back DICOM
            # patient metadata (PHI). Return only the status code. Set
            # DEBUG_LOG_PHI=true to include the body for local troubleshooting.
            if os.environ.get("DEBUG_LOG_PHI", "false").lower() == "true":
                return False, f"HTTP {response.status_code}: {response.text[:200]}"
            return False, f"HTTP {response.status_code} (body suppressed)"
    except Exception as e:
        return False, str(e)


def trigger_healthlake_sync():
    """Trigger the HealthLake sync Lambda to sync data after loading."""
    if not HEALTHLAKE_SYNC_FUNCTION:
        print("HEALTHLAKE_SYNC_FUNCTION not configured, skipping sync trigger")
        return False, "Not configured"

    try:
        print(f"Triggering HealthLake sync Lambda: {HEALTHLAKE_SYNC_FUNCTION}")
        response = lambda_client.invoke(
            FunctionName=HEALTHLAKE_SYNC_FUNCTION,
            InvocationType='Event',  # Async invocation - don't wait for completion
            Payload=json.dumps({'source': 'data_loader', 'action': 'full_sync'})
        )
        status_code = response.get('StatusCode', 0)
        if status_code in [200, 202]:
            print(f"HealthLake sync triggered successfully (status: {status_code})")
            return True, None
        else:
            return False, f"Unexpected status code: {status_code}"
    except Exception as e:
        print(f"Failed to trigger HealthLake sync: {e}")
        return False, str(e)


def _upload_single_dicom(args):
    """Helper function to generate and upload a single DICOM image (for parallel execution)."""
    patient, study_num, orthanc_url, orthanc_user, orthanc_pass = args

    # Random study date in the past year
    days_ago = random.randint(1, 365)
    study_date = datetime.now() - timedelta(days=days_ago)

    # Random modality
    modality_info = random.choice(MODALITIES)

    try:
        # Create DICOM in memory
        dicom_bytes, study_info = create_dicom_bytes(patient, modality_info, study_date)

        # Upload to Orthanc
        success, error = upload_to_orthanc(dicom_bytes, orthanc_url, orthanc_user, orthanc_pass)

        if success:
            return (True, None, patient.uuid)
        else:
            return (False, f"Patient {patient.uuid}, {modality_info[0]}: {error}", patient.uuid)

    except Exception as e:
        return (False, f"Patient {patient.uuid}: {str(e)}", patient.uuid)


def generate_and_upload_dicom_for_patients(patients, orthanc_url, orthanc_user, orthanc_pass):
    """Generate DICOM images for all patients and upload to Orthanc using parallel uploads.

    Args:
        patients: List of PatientRecord instances.
        orthanc_url: Orthanc server URL.
        orthanc_user: Orthanc username.
        orthanc_pass: Orthanc password.

    Returns:
        Tuple of (uploaded_count, failed_count, errors).
    """
    if not orthanc_url:
        print("ORTHANC_URL not configured, skipping DICOM generation")
        return 0, 0, []

    uploaded_count = 0
    failed_count = 0
    errors = []
    total_patients = len(patients)
    total_studies = total_patients * STUDIES_PER_PATIENT

    print(f"Starting PARALLEL DICOM generation for {total_patients} patients ({total_studies} total studies)")

    # Build list of upload tasks
    upload_tasks = []
    for patient in patients:
        for study_num in range(STUDIES_PER_PATIENT):
            upload_tasks.append((patient, study_num, orthanc_url, orthanc_user, orthanc_pass))

    # Use ThreadPoolExecutor for parallel uploads (20 concurrent workers)
    max_workers = 20
    print(f"Using {max_workers} parallel workers for DICOM uploads")

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_upload_single_dicom, task): task for task in upload_tasks}

        for future in as_completed(futures):
            success, error_msg, patient_id = future.result()
            completed += 1

            if success:
                uploaded_count += 1
            else:
                failed_count += 1
                if len(errors) < 10:  # Limit error collection
                    errors.append(error_msg)

            # Progress logging every 50 uploads
            if completed % 50 == 0 or completed == total_studies:
                print(f"DICOM upload progress: {completed}/{total_studies} ({uploaded_count} success, {failed_count} failed)")

    print(f"DICOM upload complete: {uploaded_count} uploaded, {failed_count} failed")
    return uploaded_count, failed_count, errors


def check_existing_patients():
    """
    Check if patients already exist in the database.
    Returns the count of existing patients.
    """
    try:
        credentials = get_db_credentials()
        connection = get_db_connection(credentials)
        cursor = connection.cursor()

        cursor.execute("SELECT COUNT(*) FROM patient_data")
        result = cursor.fetchone()
        count = result[0] if result else 0

        cursor.close()
        connection.close()

        return count
    except Exception as e:
        print(f"Error checking existing patients: {e}")
        return 0


def handler(event, context):
    """
    Lambda handler for automatic data loading.

    Triggered as a CDK custom resource after deployment.

    IDEMPOTENCY: This handler checks if patients already exist in the database
    before loading new data. If patients are found, it skips the load to prevent
    duplicates on re-deployment. Set FORCE_RELOAD=true environment variable or
    pass force_reload=true in the event to override this behavior.
    """
    print(f"Data loader started at {datetime.utcnow().isoformat()}")
    # Never log the full event (it may carry sensitive data). Log only a small
    # set of safe, non-PHI CloudFormation control keys. DEBUG_LOG_EVENTS adds
    # the ResourceProperties keys (names only, not values) for troubleshooting.
    _safe_keys = {"RequestType", "RequestId", "StackId", "LogicalResourceId"}
    _event_summary = {k: event.get(k) for k in _safe_keys if k in event}
    if os.environ.get("DEBUG_LOG_EVENTS", "false").lower() == "true":
        _rp = event.get("ResourceProperties")
        if isinstance(_rp, dict):
            _event_summary["ResourcePropertyKeys"] = sorted(_rp.keys())
    print(f"Event summary: {json.dumps(_event_summary)}")

    # Handle CloudFormation custom resource events
    request_type = event.get('RequestType', 'Create')

    if request_type == 'Delete':
        # Nothing to clean up on delete
        return send_cfn_response(event, context, 'SUCCESS', {'Message': 'Delete acknowledged'})

    if request_type == 'Update':
        # Skip on update to avoid duplicate data
        return send_cfn_response(event, context, 'SUCCESS', {'Message': 'Update skipped'})

    # Validate SYNTHEA_BUCKET is configured
    if not SYNTHEA_BUCKET:
        error_msg = "SYNTHEA_BUCKET environment variable is not set or empty. Cannot load patient data."
        print(f"ERROR: {error_msg}")
        return send_cfn_response(event, context, 'FAILED', {'Error': error_msg})

    # Create: Check for idempotency before loading data
    # Check if force reload is requested via environment variable or event
    force_reload = os.environ.get('FORCE_RELOAD', 'false').lower() == 'true'
    if not force_reload:
        # Also check event properties (for CFN custom resource, check ResourceProperties)
        resource_props = event.get('ResourceProperties', {})
        force_reload = resource_props.get('ForceReload', 'false').lower() == 'true'

    if not force_reload:
        existing_count = check_existing_patients()
        if existing_count > 0:
            print(f"IDEMPOTENCY CHECK: Found {existing_count} existing patients in database")
            print("Skipping data load to prevent duplicates. Set FORCE_RELOAD=true to override.")
            result = {
                'Message': f'Skipped - {existing_count} patients already exist in database',
                'ExistingPatients': existing_count,
                'PatientsLoaded': 0,
                'DicomUploaded': 0,
                'Skipped': True,
                'Reason': 'Idempotency check - data already loaded'
            }
            return send_cfn_response(event, context, 'SUCCESS', result)
        else:
            print("IDEMPOTENCY CHECK: No existing patients found, proceeding with data load")
    else:
        print("FORCE_RELOAD enabled - clearing existing data before reload")
        # Clear existing data (matching ambient demo pattern)
        try:
            credentials = get_db_credentials()
            connection = get_db_connection(credentials)
            cursor = connection.cursor()
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            cursor.execute("TRUNCATE TABLE forms")
            cursor.execute("TRUNCATE TABLE form_clinical_notes")
            cursor.execute("TRUNCATE TABLE form_encounter")
            cursor.execute("TRUNCATE TABLE immunizations")
            cursor.execute("DELETE FROM lists WHERE pid > 0")
            cursor.execute("TRUNCATE TABLE patient_data")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
            connection.commit()
            cursor.close()
            connection.close()
            print("Existing data cleared successfully")
        except Exception as e:
            print(f"Warning: Error clearing existing data: {e}")

    # Create: Load the data from FHIR bundles
    try:
        # Parse FHIR bundles from S3
        print(f"Loading FHIR bundles from s3://{SYNTHEA_BUCKET}/{SYNTHEA_PREFIX}...")
        parser = FHIRParser(s3_client, SYNTHEA_BUCKET, SYNTHEA_PREFIX)
        bundle_keys = parser.list_bundles()
        print(f"Found {len(bundle_keys)} FHIR bundle files")

        if not bundle_keys:
            print("WARNING: No FHIR bundles found in S3. Returning success with 0 patients.")
            result = {
                'Message': 'No FHIR bundles found in S3 prefix',
                'PatientsLoaded': 0,
                'DicomUploaded': 0,
                'BundlesFound': 0,
            }
            return send_cfn_response(event, context, 'SUCCESS', result)

        # Parse each bundle into PatientRecord with clinical data
        patients = []
        skipped_bundles = 0
        for bundle_key in bundle_keys:
            try:
                bundle = parser.load_bundle(bundle_key)
                patient = parser.extract_patient(bundle)
                if patient is None:
                    print(f"WARNING: No Patient resource in {bundle_key}, skipping")
                    skipped_bundles += 1
                    continue
                # Populate clinical data on the patient record
                patient.conditions = parser.extract_conditions(bundle)
                patient.allergies = parser.extract_allergies(bundle)
                patient.medications = parser.extract_medications(bundle)
                patient.immunizations = parser.extract_immunizations(bundle)
                patient.encounters = parser.extract_encounters(bundle)
                patient.clinical_notes = parser.extract_clinical_notes(bundle)
                patients.append(patient)
            except Exception as e:
                print(f"WARNING: Error parsing bundle {bundle_key}: {e}, skipping")
                skipped_bundles += 1

        print(f"Parsed {len(patients)} patients from FHIR bundles ({skipped_bundles} bundles skipped)")

        # Load into OpenEMR
        print("Loading patients into OpenEMR database...")
        loaded_count, db_errors, patient_db_ids = load_data_to_openemr(patients)
        print(f"Loaded {loaded_count} patients into OpenEMR")

        if db_errors:
            print(f"Database errors: {db_errors[:5]}")

        # Generate and upload DICOM images to Orthanc
        dicom_uploaded = 0
        dicom_failed = 0
        dicom_errors = []

        # Get Orthanc URL (from env var or SSM)
        orthanc_url = get_orthanc_url()

        if orthanc_url:
            print(f"Generating DICOM images and uploading to Orthanc at {orthanc_url}...")
            dicom_uploaded, dicom_failed, dicom_errors = generate_and_upload_dicom_for_patients(
                patients, orthanc_url, ORTHANC_USER, ORTHANC_PASS
            )
            print(f"DICOM upload complete: {dicom_uploaded} uploaded, {dicom_failed} failed")

            if dicom_errors:
                print(f"DICOM errors: {dicom_errors[:5]}")
        else:
            print("Orthanc URL not configured, skipping DICOM generation")

        result = {
            'Message': f'Loaded {loaded_count} patients, uploaded {dicom_uploaded} DICOM studies',
            'PatientsLoaded': loaded_count,
            'DatabaseErrors': len(db_errors),
            'DicomUploaded': dicom_uploaded,
            'DicomFailed': dicom_failed,
            'BundlesFound': len(bundle_keys),
            'BundlesSkipped': skipped_bundles,
        }

        print(f"Data loading complete: {result}")

        # Trigger HealthLake sync after data is loaded
        sync_triggered, sync_error = trigger_healthlake_sync()
        if sync_triggered:
            result['HealthLakeSyncTriggered'] = True
            print("HealthLake sync triggered successfully")
        else:
            result['HealthLakeSyncTriggered'] = False
            result['HealthLakeSyncError'] = sync_error
            print(f"HealthLake sync not triggered: {sync_error}")

        return send_cfn_response(event, context, 'SUCCESS', result)

    except Exception as e:
        print(f"Error loading data: {str(e)}")
        import traceback
        traceback.print_exc()
        return send_cfn_response(event, context, 'FAILED', {'Error': str(e)})


def send_cfn_response(event, context, status, data):
    """Send response to CloudFormation for custom resource."""
    import urllib.request

    response_url = event.get('ResponseURL')
    if not response_url:
        # Not a CFN custom resource call, just return
        return {'statusCode': 200, 'body': json.dumps(data)}

    response_body = {
        'Status': status,
        'Reason': f"See CloudWatch Log Stream: {context.log_stream_name if context else 'N/A'}",
        'PhysicalResourceId': event.get('PhysicalResourceId', context.log_stream_name if context else 'data-loader'),
        'StackId': event.get('StackId', ''),
        'RequestId': event.get('RequestId', ''),
        'LogicalResourceId': event.get('LogicalResourceId', ''),
        'Data': data
    }

    json_body = json.dumps(response_body).encode('utf-8')

    # The response URL is the CloudFormation-provided pre-signed S3 URL (https).
    # Validate the scheme defensively before opening it.
    from urllib.parse import urlparse
    _parsed_url = urlparse(response_url)
    if _parsed_url.scheme != 'https':
        # Do not log the full URL: CFN pre-signed URLs embed temporary
        # credentials (X-Amz-Signature / X-Amz-Security-Token) in the query
        # string. Log only the scheme and host.
        print(f"Refusing to send CFN response to non-https URL (scheme={_parsed_url.scheme}, host={_parsed_url.netloc})")
        return {'statusCode': 400, 'body': 'invalid response url'}

    req = urllib.request.Request(
        response_url,
        data=json_body,
        headers={'Content-Type': 'application/json', 'Content-Length': len(json_body)},
        method='PUT'
    )

    try:
        # nosec B310 - response_url is the CFN-provided https pre-signed URL, validated above.
        urllib.request.urlopen(req)  # nosec B310
        print(f"CFN response sent: {status}")
    except Exception as e:
        print(f"Failed to send CFN response: {e}")

    return {'statusCode': 200, 'body': json.dumps(data)}
