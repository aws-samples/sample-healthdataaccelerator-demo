"""Unit tests for FHIRParser class - S3 listing and bundle loading."""

import json
import io
import pytest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'data_loader'))

from fhir_parser import FHIRParser


class TestFHIRParserInit:
    """Tests for FHIRParser constructor."""

    def test_init_stores_s3_client(self):
        client = MagicMock()
        parser = FHIRParser(client, "my-bucket", "my-prefix/")
        assert parser.s3_client is client

    def test_init_stores_bucket(self):
        client = MagicMock()
        parser = FHIRParser(client, "my-bucket", "my-prefix/")
        assert parser.bucket == "my-bucket"

    def test_init_stores_prefix(self):
        client = MagicMock()
        parser = FHIRParser(client, "my-bucket", "synthea-bundles/")
        assert parser.prefix == "synthea-bundles/"


class TestFHIRParserListBundles:
    """Tests for FHIRParser.list_bundles() method."""

    def test_list_bundles_returns_json_files(self):
        """Should return only .json files from S3 listing."""
        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                'Contents': [
                    {'Key': 'synthea-bundles/patient1.json'},
                    {'Key': 'synthea-bundles/patient2.json'},
                    {'Key': 'synthea-bundles/README.txt'},
                ]
            }
        ]

        parser = FHIRParser(client, "test-bucket", "synthea-bundles/")
        result = parser.list_bundles()

        assert result == [
            'synthea-bundles/patient1.json',
            'synthea-bundles/patient2.json',
        ]

    def test_list_bundles_filters_non_json(self):
        """Should exclude non-.json files."""
        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                'Contents': [
                    {'Key': 'prefix/file.csv'},
                    {'Key': 'prefix/data.json'},
                    {'Key': 'prefix/image.png'},
                ]
            }
        ]

        parser = FHIRParser(client, "bucket", "prefix/")
        result = parser.list_bundles()

        assert result == ['prefix/data.json']

    def test_list_bundles_handles_pagination(self):
        """Should aggregate results across multiple pages."""
        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                'Contents': [
                    {'Key': 'prefix/page1_file1.json'},
                    {'Key': 'prefix/page1_file2.json'},
                ]
            },
            {
                'Contents': [
                    {'Key': 'prefix/page2_file1.json'},
                ]
            },
        ]

        parser = FHIRParser(client, "bucket", "prefix/")
        result = parser.list_bundles()

        assert len(result) == 3
        assert 'prefix/page1_file1.json' in result
        assert 'prefix/page2_file1.json' in result

    def test_list_bundles_handles_empty_bucket(self):
        """Should return empty list when no objects found."""
        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {}  # No 'Contents' key
        ]

        parser = FHIRParser(client, "bucket", "prefix/")
        result = parser.list_bundles()

        assert result == []

    def test_list_bundles_uses_correct_paginator(self):
        """Should use list_objects_v2 paginator."""
        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]

        parser = FHIRParser(client, "my-bucket", "my-prefix/")
        parser.list_bundles()

        client.get_paginator.assert_called_once_with('list_objects_v2')
        paginator.paginate.assert_called_once_with(
            Bucket="my-bucket", Prefix="my-prefix/"
        )


class TestFHIRParserLoadBundle:
    """Tests for FHIRParser.load_bundle() method."""

    def test_load_bundle_parses_json(self):
        """Should download and parse JSON from S3."""
        bundle_data = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {"resource": {"resourceType": "Patient", "name": [{"family": "Smith"}]}}
            ]
        }

        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(bundle_data).encode('utf-8')

        client = MagicMock()
        client.get_object.return_value = {'Body': body_mock}

        parser = FHIRParser(client, "test-bucket", "prefix/")
        result = parser.load_bundle("prefix/patient1.json")

        assert result == bundle_data
        client.get_object.assert_called_once_with(
            Bucket="test-bucket", Key="prefix/patient1.json"
        )

    def test_load_bundle_handles_unicode(self):
        """Should handle UTF-8 encoded content correctly."""
        bundle_data = {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Patient", "name": [{"family": "García"}]}}
            ]
        }

        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(bundle_data).encode('utf-8')

        client = MagicMock()
        client.get_object.return_value = {'Body': body_mock}

        parser = FHIRParser(client, "bucket", "prefix/")
        result = parser.load_bundle("prefix/unicode_patient.json")

        assert result['entry'][0]['resource']['name'][0]['family'] == 'García'

    def test_load_bundle_uses_correct_bucket_and_key(self):
        """Should call get_object with correct bucket and key parameters."""
        body_mock = MagicMock()
        body_mock.read.return_value = b'{"resourceType": "Bundle"}'

        client = MagicMock()
        client.get_object.return_value = {'Body': body_mock}

        parser = FHIRParser(client, "specific-bucket", "data/bundles/")
        parser.load_bundle("data/bundles/test.json")

        client.get_object.assert_called_once_with(
            Bucket="specific-bucket", Key="data/bundles/test.json"
        )


# --- Tests for Extraction Methods (Task 2.3) ---


def _make_parser():
    """Helper to create a FHIRParser instance with a mock S3 client."""
    return FHIRParser(MagicMock(), "test-bucket", "prefix/")


class TestExtractResources:
    """Tests for FHIRParser._extract_resources() helper."""

    def test_extracts_resources_by_type(self):
        bundle = {
            "entry": [
                {"resource": {"resourceType": "Patient", "name": []}},
                {"resource": {"resourceType": "Condition", "code": {}}},
                {"resource": {"resourceType": "Condition", "code": {}}},
            ]
        }
        parser = _make_parser()
        result = parser._extract_resources(bundle)
        assert len(result["Patient"]) == 1
        assert len(result["Condition"]) == 2

    def test_handles_empty_bundle(self):
        bundle = {"entry": []}
        parser = _make_parser()
        result = parser._extract_resources(bundle)
        assert result == {}

    def test_handles_missing_entry_key(self):
        bundle = {}
        parser = _make_parser()
        result = parser._extract_resources(bundle)
        assert result == {}


class TestExtractPatient:
    """Tests for FHIRParser.extract_patient() method."""

    def test_extracts_full_patient(self):
        bundle = {
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "name": [{"given": ["John"], "family": "Doe"}],
                        "birthDate": "1990-01-15",
                        "gender": "male",
                        "address": [{
                            "line": ["123 Main St"],
                            "city": "Boston",
                            "state": "MA",
                            "postalCode": "02101"
                        }],
                        "telecom": [
                            {"system": "phone", "value": "555-1234"},
                            {"system": "email", "value": "john@example.com"},
                        ]
                    }
                }
            ]
        }
        parser = _make_parser()
        patient = parser.extract_patient(bundle)

        assert patient is not None
        assert patient.fname == "John"
        assert patient.lname == "Doe"
        assert patient.dob == "1990-01-15"
        assert patient.sex == "Male"
        assert patient.street == "123 Main St"
        assert patient.city == "Boston"
        assert patient.state == "MA"
        assert patient.postal_code == "02101"
        assert patient.phone == "555-1234"
        assert patient.email == "john@example.com"
        assert patient.uuid  # UUID should be generated

    def test_maps_female_gender(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "Patient",
                    "name": [{"given": ["Jane"], "family": "Smith"}],
                    "birthDate": "1985-06-20",
                    "gender": "female",
                    "address": [{"line": ["456 Elm St"], "city": "Salem", "state": "MA", "postalCode": "01970"}],
                    "telecom": []
                }
            }]
        }
        parser = _make_parser()
        patient = parser.extract_patient(bundle)
        assert patient.sex == "Female"

    def test_returns_none_for_missing_patient(self):
        bundle = {
            "entry": [
                {"resource": {"resourceType": "Condition", "code": {}}}
            ]
        }
        parser = _make_parser()
        result = parser.extract_patient(bundle)
        assert result is None

    def test_handles_missing_telecom(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "Patient",
                    "name": [{"given": ["Test"], "family": "User"}],
                    "birthDate": "2000-01-01",
                    "gender": "male",
                    "address": [{"line": ["1 St"], "city": "C", "state": "S", "postalCode": "00000"}],
                }
            }]
        }
        parser = _make_parser()
        patient = parser.extract_patient(bundle)
        assert patient.phone == ""
        assert patient.email == ""

    def test_handles_missing_address(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "Patient",
                    "name": [{"given": ["Test"], "family": "User"}],
                    "birthDate": "2000-01-01",
                    "gender": "male",
                    "telecom": []
                }
            }]
        }
        parser = _make_parser()
        patient = parser.extract_patient(bundle)
        assert patient.street == ""
        assert patient.city == ""
        assert patient.state == ""
        assert patient.postal_code == ""


class TestExtractConditions:
    """Tests for FHIRParser.extract_conditions() method."""

    def test_extracts_conditions_with_snomed(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "Condition",
                    "code": {
                        "coding": [{
                            "system": "http://snomed.info/sct",
                            "code": "44054006",
                            "display": "Type 2 diabetes mellitus"
                        }]
                    },
                    "onsetDateTime": "2020-03-15T10:00:00Z"
                }
            }]
        }
        parser = _make_parser()
        conditions = parser.extract_conditions(bundle)

        assert len(conditions) == 1
        assert conditions[0].snomed_code == "44054006"
        assert conditions[0].display == "Type 2 diabetes mellitus"
        assert conditions[0].onset_date == "2020-03-15T10:00:00Z"

    def test_handles_no_conditions(self):
        bundle = {
            "entry": [{
                "resource": {"resourceType": "Patient", "name": []}
            }]
        }
        parser = _make_parser()
        conditions = parser.extract_conditions(bundle)
        assert conditions == []

    def test_handles_condition_without_snomed(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "Condition",
                    "code": {
                        "coding": [{
                            "system": "http://hl7.org/fhir/sid/icd-10",
                            "code": "E11",
                            "display": "Diabetes"
                        }]
                    },
                    "onsetDateTime": "2021-01-01"
                }
            }]
        }
        parser = _make_parser()
        conditions = parser.extract_conditions(bundle)
        # Falls back to first coding
        assert len(conditions) == 1
        assert conditions[0].snomed_code == "E11"
        assert conditions[0].display == "Diabetes"


class TestExtractAllergies:
    """Tests for FHIRParser.extract_allergies() method."""

    def test_extracts_allergies(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "AllergyIntolerance",
                    "code": {
                        "coding": [{"display": "Penicillin", "code": "12345"}]
                    }
                }
            }]
        }
        parser = _make_parser()
        allergies = parser.extract_allergies(bundle)

        assert len(allergies) == 1
        assert allergies[0].title == "Penicillin"

    def test_handles_no_allergies(self):
        bundle = {"entry": [{"resource": {"resourceType": "Patient"}}]}
        parser = _make_parser()
        allergies = parser.extract_allergies(bundle)
        assert allergies == []


class TestExtractMedications:
    """Tests for FHIRParser.extract_medications() method."""

    def test_extracts_medications_with_rxnorm(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "MedicationRequest",
                    "medicationCodeableConcept": {
                        "coding": [{
                            "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                            "code": "860975",
                            "display": "Metformin 500 MG"
                        }]
                    }
                }
            }]
        }
        parser = _make_parser()
        medications = parser.extract_medications(bundle)

        assert len(medications) == 1
        assert medications[0].title == "Metformin 500 MG"
        assert medications[0].rxnorm_code == "860975"

    def test_handles_no_medications(self):
        bundle = {"entry": [{"resource": {"resourceType": "Patient"}}]}
        parser = _make_parser()
        medications = parser.extract_medications(bundle)
        assert medications == []


class TestExtractImmunizations:
    """Tests for FHIRParser.extract_immunizations() method."""

    def test_extracts_immunizations_with_cvx(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "Immunization",
                    "vaccineCode": {
                        "coding": [{
                            "system": "http://hl7.org/fhir/sid/cvx",
                            "code": "140",
                            "display": "Influenza"
                        }]
                    },
                    "occurrenceDateTime": "2023-10-01T09:00:00Z"
                }
            }]
        }
        parser = _make_parser()
        immunizations = parser.extract_immunizations(bundle)

        assert len(immunizations) == 1
        assert immunizations[0].cvx_code == "140"
        assert immunizations[0].date == "2023-10-01T09:00:00Z"

    def test_handles_no_immunizations(self):
        bundle = {"entry": [{"resource": {"resourceType": "Patient"}}]}
        parser = _make_parser()
        immunizations = parser.extract_immunizations(bundle)
        assert immunizations == []


class TestExtractEncounters:
    """Tests for FHIRParser.extract_encounters() method."""

    def test_extracts_encounters_with_reason_code(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "Encounter",
                    "class": {"code": "AMB"},
                    "period": {"start": "2023-06-15T14:30:00Z"},
                    "reasonCode": [{
                        "coding": [{"display": "Annual physical"}]
                    }]
                }
            }]
        }
        parser = _make_parser()
        encounters = parser.extract_encounters(bundle)

        assert len(encounters) == 1
        assert encounters[0].date == "2023-06-15T14:30:00Z"
        assert encounters[0].reason == "Annual physical"
        assert encounters[0].class_code == "AMB"

    def test_falls_back_to_type_text_for_reason(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "Encounter",
                    "class": {"code": "IMP"},
                    "period": {"start": "2023-01-01"},
                    "type": [{"text": "Emergency visit"}]
                }
            }]
        }
        parser = _make_parser()
        encounters = parser.extract_encounters(bundle)

        assert len(encounters) == 1
        assert encounters[0].reason == "Emergency visit"
        assert encounters[0].class_code == "IMP"

    def test_handles_no_encounters(self):
        bundle = {"entry": [{"resource": {"resourceType": "Patient"}}]}
        parser = _make_parser()
        encounters = parser.extract_encounters(bundle)
        assert encounters == []


class TestExtractClinicalNotes:
    """Tests for FHIRParser.extract_clinical_notes() method."""

    def test_extracts_clinical_notes_with_base64_content(self):
        import base64
        note_text = "Patient presents with mild cough and fever."
        encoded = base64.b64encode(note_text.encode('utf-8')).decode('utf-8')

        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "DocumentReference",
                    "content": [{
                        "attachment": {"data": encoded}
                    }],
                    "context": {
                        "encounter": [{"reference": "Encounter/12345"}]
                    }
                }
            }]
        }
        parser = _make_parser()
        notes = parser.extract_clinical_notes(bundle)

        assert len(notes) == 1
        assert notes[0].content == note_text
        assert notes[0].encounter_ref == "Encounter/12345"

    def test_handles_no_document_references(self):
        bundle = {"entry": [{"resource": {"resourceType": "Patient"}}]}
        parser = _make_parser()
        notes = parser.extract_clinical_notes(bundle)
        assert notes == []

    def test_handles_missing_attachment_data(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "DocumentReference",
                    "content": [{"attachment": {}}],
                    "context": {
                        "encounter": [{"reference": "Encounter/999"}]
                    }
                }
            }]
        }
        parser = _make_parser()
        notes = parser.extract_clinical_notes(bundle)

        assert len(notes) == 1
        assert notes[0].content == ""
        assert notes[0].encounter_ref == "Encounter/999"

    def test_handles_invalid_base64(self):
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "DocumentReference",
                    "content": [{
                        "attachment": {"data": "not-valid-base64!!!"}
                    }],
                    "context": {"encounter": []}
                }
            }]
        }
        parser = _make_parser()
        notes = parser.extract_clinical_notes(bundle)

        assert len(notes) == 1
        assert notes[0].content == ""
        assert notes[0].encounter_ref == ""
