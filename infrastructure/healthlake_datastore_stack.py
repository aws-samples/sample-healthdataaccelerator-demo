"""
CDK Stack for AWS HealthLake FHIR Datastore

Creates a HealthLake FHIR R4 datastore for storing healthcare data.
The datastore can receive data from OpenEMR and Orthanc via sync Lambdas.

PHI / HIPAA NOTICE:
A HealthLake FHIR datastore holds protected health information (PHI). If you
store real PHI, this is a HIPAA-regulated workload: execute an AWS Business
Associate Addendum (BAA), keep data in HIPAA-eligible services, and enable
KMS encryption (configured here), access logging, and audit controls. The
customer is responsible for compliant handling of regulated data. This sample
ships with synthetic data only.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_healthlake as healthlake,
    aws_kms as kms,
    aws_iam as iam,
)
from constructs import Construct


class HealthLakeDatastoreStack(Stack):
    """Creates an AWS HealthLake FHIR R4 datastore."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        datastore_name: str = "healthcare-demo-datastore",
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create KMS key for HealthLake encryption
        self.kms_key = kms.Key(
            self,
            "HealthLakeKey",
            description="KMS key for HealthLake FHIR datastore encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Expose the key ARN so downstream stacks can scope their KMS policies
        # to this specific key instead of a wildcard.
        self.kms_key_arn = self.kms_key.key_arn

        # Grant HealthLake service access to the KMS key
        self.kms_key.grant_encrypt_decrypt(
            iam.ServicePrincipal("healthlake.amazonaws.com")
        )

        # Create the HealthLake FHIR datastore
        # Note: No preload_data_config - we load our own data via the data loader Lambda
        # Integrated analytics enabled for Athena querying via Lake Formation
        self.datastore = healthlake.CfnFHIRDatastore(
            self,
            "FHIRDatastore",
            datastore_type_version="R4",
            datastore_name=datastore_name,
            sse_configuration=healthlake.CfnFHIRDatastore.SseConfigurationProperty(
                kms_encryption_config=healthlake.CfnFHIRDatastore.KmsEncryptionConfigProperty(
                    cmk_type="CUSTOMER_MANAGED_KMS_KEY",
                    kms_key_id=self.kms_key.key_id,
                )
            ),
        )

        # Store datastore ID for use by other stacks
        self.datastore_id = self.datastore.attr_datastore_id
        self.datastore_endpoint = self.datastore.attr_datastore_endpoint

        # Outputs
        CfnOutput(
            self,
            "DatastoreId",
            value=self.datastore_id,
            description="HealthLake FHIR Datastore ID",
            export_name="HealthLakeDatastoreId",
        )

        CfnOutput(
            self,
            "DatastoreEndpoint",
            value=self.datastore_endpoint,
            description="HealthLake FHIR Datastore Endpoint",
            export_name="HealthLakeDatastoreEndpoint",
        )

        CfnOutput(
            self,
            "DatastoreArn",
            value=self.datastore.attr_datastore_arn,
            description="HealthLake FHIR Datastore ARN",
            export_name="HealthLakeDatastoreArn",
        )
