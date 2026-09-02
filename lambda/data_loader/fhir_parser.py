"""FHIR R4 Bundle Parser for Synthea-generated patient data.

Parses FHIR R4 bundles from S3 and extracts clinical resources into
typed dataclass records for insertion into OpenEMR.

PHI / HIPAA NOTICE:
This module parses FHIR patient bundles (demographics, conditions, medications,
allergies, and clinical notes) - all protected health information (PHI) under
HIPAA. If you process real PHI, this is a HIPAA-regulated workload: execute an
AWS Business Associate Addendum (BAA), keep data within HIPAA-eligible services,
and enable encryption, access logging, and audit controls. Exception/error
handling here deliberately avoids logging record contents. The customer is
responsible for compliant handling of regulated data. This sample ships with
synthetic Synthea data only.
"""

import json
import uuid
import base64
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# --- Data Model Dataclasses ---


@dataclass
class ConditionRecord:
    """Represents a parsed FHIR Condition resource."""
    snomed_code: str    # From Condition.code.coding[system=snomed].code
    display: str        # From Condition.code.coding[system=snomed].display
    onset_date: str     # From Condition.onsetDateTime


@dataclass
class AllergyRecord:
    """Represents a parsed FHIR AllergyIntolerance resource."""
    title: str          # From AllergyIntolerance.code.coding[0].display


@dataclass
class MedicationRecord:
    """Represents a parsed FHIR MedicationRequest resource."""
    title: str          # From MedicationRequest.medicationCodeableConcept.coding[0].display
    rxnorm_code: str    # From MedicationRequest.medicationCodeableConcept.coding[system=rxnorm].code


@dataclass
class ImmunizationRecord:
    """Represents a parsed FHIR Immunization resource."""
    cvx_code: str       # From Immunization.vaccineCode.coding[system=cvx].code
    date: str           # From Immunization.occurrenceDateTime


@dataclass
class EncounterRecord:
    """Represents a parsed FHIR Encounter resource."""
    date: str           # From Encounter.period.start
    reason: str         # From Encounter.reasonCode[0].coding[0].display
    class_code: str     # From Encounter.class.code (AMB, IMP, etc.)
    full_url: str = ""  # From bundle entry fullUrl (used to link clinical notes)


@dataclass
class ClinicalNoteRecord:
    """Represents a parsed FHIR DocumentReference resource."""
    content: str        # From DocumentReference.content[0].attachment.data (base64 decoded)
    encounter_ref: str  # From DocumentReference.context.encounter[0].reference


@dataclass
class PatientRecord:
    """Represents a parsed FHIR Patient resource with associated clinical data."""
    uuid: str           # Generated UUID
    fname: str          # From Patient.name[0].given[0]
    lname: str          # From Patient.name[0].family
    dob: str            # From Patient.birthDate (YYYY-MM-DD)
    sex: str            # From Patient.gender → "Male"/"Female"
    street: str         # From Patient.address[0].line[0]
    city: str           # From Patient.address[0].city
    state: str          # From Patient.address[0].state
    postal_code: str    # From Patient.address[0].postalCode
    phone: str          # From Patient.telecom (system=phone)
    email: str          # From Patient.telecom (system=email)
    conditions: List[ConditionRecord] = field(default_factory=list)
    allergies: List[AllergyRecord] = field(default_factory=list)
    medications: List[MedicationRecord] = field(default_factory=list)
    immunizations: List[ImmunizationRecord] = field(default_factory=list)
    encounters: List[EncounterRecord] = field(default_factory=list)
    clinical_notes: List[ClinicalNoteRecord] = field(default_factory=list)


# --- FHIR Parser Class ---


class FHIRParser:
    """Parses Synthea FHIR R4 bundles into OpenEMR database records."""

    def __init__(self, s3_client, bucket: str, prefix: str):
        """Initialize with S3 client and bundle location.

        Args:
            s3_client: A boto3 S3 client instance.
            bucket: The S3 bucket name containing FHIR bundles.
            prefix: The S3 key prefix under which bundles are stored.
        """
        self.s3_client = s3_client
        self.bucket = bucket
        self.prefix = prefix

    def list_bundles(self) -> List[str]:
        """List all .json bundle files in the S3 prefix.

        Uses paginated list_objects_v2 to handle large numbers of bundles.

        Returns:
            A list of S3 keys for all .json files under the configured prefix.
        """
        keys = []
        paginator = self.s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('.json'):
                    keys.append(obj['Key'])
        return keys

    def load_bundle(self, key: str) -> dict:
        """Load and parse a single FHIR bundle from S3.

        Args:
            key: The S3 object key of the FHIR bundle to load.

        Returns:
            The parsed JSON content of the FHIR bundle as a dictionary.
        """
        response = self.s3_client.get_object(Bucket=self.bucket, Key=key)
        return json.loads(response['Body'].read().decode('utf-8'))

    def _extract_resources(self, bundle: dict) -> dict:
        """Extract resources from a FHIR bundle grouped by resource type.

        Args:
            bundle: A parsed FHIR bundle dictionary.

        Returns:
            A dictionary mapping resource type strings to lists of resource dicts.
        """
        resources = {}
        for entry in bundle.get('entry', []):
            resource = entry.get('resource', {})
            rtype = resource.get('resourceType', '')
            if rtype not in resources:
                resources[rtype] = []
            resources[rtype].append(resource)
        return resources

    def extract_patient(self, bundle: dict) -> Optional[PatientRecord]:
        """Extract Patient resource fields from a FHIR bundle.

        Args:
            bundle: A parsed FHIR bundle dictionary.

        Returns:
            A PatientRecord with demographic fields populated, or None if no
            Patient resource is found in the bundle.
        """
        resources = self._extract_resources(bundle)
        patients = resources.get('Patient', [])
        if not patients:
            logger.warning("No Patient resource found in bundle, skipping")
            return None

        patient = patients[0]

        # Extract name
        names = patient.get('name', [])
        fname = ''
        lname = ''
        if names:
            fname = names[0].get('given', [''])[0] if names[0].get('given') else ''
            lname = names[0].get('family', '')

        # Extract date of birth
        dob = patient.get('birthDate', '')

        # Extract gender with mapping
        gender = patient.get('gender', '')
        sex_map = {'male': 'Male', 'female': 'Female'}
        sex = sex_map.get(gender, gender.capitalize() if gender else '')

        # Extract address
        addresses = patient.get('address', [])
        street = ''
        city = ''
        state = ''
        postal_code = ''
        if addresses:
            addr = addresses[0]
            lines = addr.get('line', [])
            street = lines[0] if lines else ''
            city = addr.get('city', '')
            state = addr.get('state', '')
            postal_code = addr.get('postalCode', '')

        # Extract telecom (phone and email)
        phone = ''
        email = ''
        for telecom in patient.get('telecom', []):
            system = telecom.get('system', '')
            value = telecom.get('value', '')
            if system == 'phone' and not phone:
                phone = value
            elif system == 'email' and not email:
                email = value

        # Use the Synthea patient ID as the UUID — it's stable across reloads
        patient_id = patient.get('id', str(uuid.uuid4()))

        return PatientRecord(
            uuid=patient_id,
            fname=fname,
            lname=lname,
            dob=dob,
            sex=sex,
            street=street,
            city=city,
            state=state,
            postal_code=postal_code,
            phone=phone,
            email=email,
        )

    def extract_conditions(self, bundle: dict) -> List[ConditionRecord]:
        """Extract Condition resources with SNOMED codes from a FHIR bundle.

        Args:
            bundle: A parsed FHIR bundle dictionary.

        Returns:
            A list of ConditionRecord instances with SNOMED codes and display text.
        """
        resources = self._extract_resources(bundle)
        conditions = []
        for resource in resources.get('Condition', []):
            code_block = resource.get('code', {})
            codings = code_block.get('coding', [])

            snomed_code = ''
            display = ''
            for coding in codings:
                system = coding.get('system', '')
                if 'snomed' in system.lower():
                    snomed_code = coding.get('code', '')
                    display = coding.get('display', '')
                    break

            # Fall back to first coding if no SNOMED found
            if not snomed_code and codings:
                snomed_code = codings[0].get('code', '')
                display = codings[0].get('display', '')

            onset_date = resource.get('onsetDateTime', '')

            conditions.append(ConditionRecord(
                snomed_code=snomed_code,
                display=display,
                onset_date=onset_date,
            ))
        return conditions

    def extract_allergies(self, bundle: dict) -> List[AllergyRecord]:
        """Extract AllergyIntolerance resources from a FHIR bundle.

        Args:
            bundle: A parsed FHIR bundle dictionary.

        Returns:
            A list of AllergyRecord instances.
        """
        resources = self._extract_resources(bundle)
        allergies = []
        for resource in resources.get('AllergyIntolerance', []):
            code_block = resource.get('code', {})
            codings = code_block.get('coding', [])
            title = ''
            if codings:
                title = codings[0].get('display', '')
            allergies.append(AllergyRecord(title=title))
        return allergies

    def extract_medications(self, bundle: dict) -> List[MedicationRecord]:
        """Extract MedicationRequest resources with RxNorm codes from a FHIR bundle.

        Args:
            bundle: A parsed FHIR bundle dictionary.

        Returns:
            A list of MedicationRecord instances with titles and RxNorm codes.
        """
        resources = self._extract_resources(bundle)
        medications = []
        for resource in resources.get('MedicationRequest', []):
            med_concept = resource.get('medicationCodeableConcept', {})
            codings = med_concept.get('coding', [])

            title = ''
            rxnorm_code = ''

            if codings:
                title = codings[0].get('display', '')

            for coding in codings:
                system = coding.get('system', '')
                if 'rxnorm' in system.lower():
                    rxnorm_code = coding.get('code', '')
                    break

            # Fall back to first coding code if no RxNorm found
            if not rxnorm_code and codings:
                rxnorm_code = codings[0].get('code', '')

            medications.append(MedicationRecord(
                title=title,
                rxnorm_code=rxnorm_code,
            ))
        return medications

    def extract_immunizations(self, bundle: dict) -> List[ImmunizationRecord]:
        """Extract Immunization resources with CVX codes and dates from a FHIR bundle.

        Args:
            bundle: A parsed FHIR bundle dictionary.

        Returns:
            A list of ImmunizationRecord instances with CVX codes and dates.
        """
        resources = self._extract_resources(bundle)
        immunizations = []
        for resource in resources.get('Immunization', []):
            vaccine_code = resource.get('vaccineCode', {})
            codings = vaccine_code.get('coding', [])

            cvx_code = ''
            for coding in codings:
                system = coding.get('system', '')
                if 'cvx' in system.lower():
                    cvx_code = coding.get('code', '')
                    break

            # Fall back to first coding code if no CVX found
            if not cvx_code and codings:
                cvx_code = codings[0].get('code', '')

            date = resource.get('occurrenceDateTime', '')

            immunizations.append(ImmunizationRecord(
                cvx_code=cvx_code,
                date=date,
            ))
        return immunizations

    def extract_encounters(self, bundle: dict) -> List[EncounterRecord]:
        """Extract Encounter resources with dates, reasons, class codes, and fullUrls.

        Args:
            bundle: A parsed FHIR bundle dictionary.

        Returns:
            A list of EncounterRecord instances with date, reason, class_code, and full_url.
        """
        encounters = []
        for entry in bundle.get('entry', []):
            resource = entry.get('resource', {})
            if resource.get('resourceType') != 'Encounter':
                continue
            
            full_url = entry.get('fullUrl', '')

            # Extract date from period.start
            period = resource.get('period', {})
            date = period.get('start', '')

            # Extract reason from reasonCode[0].coding[0].display or type[0].text
            reason = ''
            reason_codes = resource.get('reasonCode', [])
            if reason_codes:
                reason_codings = reason_codes[0].get('coding', [])
                if reason_codings:
                    reason = reason_codings[0].get('display', '')
            if not reason:
                types = resource.get('type', [])
                if types:
                    reason = types[0].get('text', '')

            # Extract class code
            encounter_class = resource.get('class', {})
            class_code = encounter_class.get('code', '')

            encounters.append(EncounterRecord(
                date=date,
                reason=reason,
                class_code=class_code,
                full_url=full_url,
            ))
        return encounters

    def extract_clinical_notes(self, bundle: dict) -> List[ClinicalNoteRecord]:
        """Extract DocumentReference resources with base64 decoded content.

        Args:
            bundle: A parsed FHIR bundle dictionary.

        Returns:
            A list of ClinicalNoteRecord instances with decoded content and
            encounter references.
        """
        resources = self._extract_resources(bundle)
        notes = []
        for resource in resources.get('DocumentReference', []):
            # Extract content from content[0].attachment.data (base64 encoded)
            content_list = resource.get('content', [])
            content = ''
            if content_list:
                attachment = content_list[0].get('attachment', {})
                data_b64 = attachment.get('data', '')
                if data_b64:
                    try:
                        content = base64.b64decode(data_b64).decode('utf-8')
                    except (ValueError, UnicodeDecodeError) as e:
                        # Log only the exception TYPE, never the exception
                        # message/object: decode errors (e.g. UnicodeDecodeError)
                        # can embed raw byte fragments of the clinical note (PHI).
                        logger.warning(
                            "Failed to decode clinical note content: %s",
                            type(e).__name__,
                        )
                        content = ''

            # Extract encounter reference
            context = resource.get('context', {})
            encounter_refs = context.get('encounter', [])
            encounter_ref = ''
            if encounter_refs:
                encounter_ref = encounter_refs[0].get('reference', '')

            notes.append(ClinicalNoteRecord(
                content=content,
                encounter_ref=encounter_ref,
            ))
        return notes
