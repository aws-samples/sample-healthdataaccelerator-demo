"""
CDK Stack for the Synthea staging S3 bucket.

This lightweight stack provisions the S3 bucket that holds pre-generated
Synthea FHIR bundles. It is deployed first so that data can be uploaded to the
bucket before the full demo (OpenEMR + Orthanc + data loader) is deployed. The
combined data loader then reads the pre-staged bundles from this bucket.
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    aws_s3 as s3,
)
from constructs import Construct


class SyntheaStagingStack(Stack):
    """Stack that provisions the Synthea FHIR bundle staging bucket."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 bucket for pre-staged Synthea FHIR bundles.
        # Secure defaults: block all public access and require TLS in transit.
        self.bucket = s3.Bucket(
            self,
            "SyntheaStagingBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,            # retain prior object versions
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        CfnOutput(
            self,
            "SyntheaBucketName",
            value=self.bucket.bucket_name,
            description="S3 bucket for Synthea FHIR bundles (pre-staged before full deploy)",
            export_name="SyntheaStagingBucketName",
        )

        CfnOutput(
            self,
            "SyntheaBucketArn",
            value=self.bucket.bucket_arn,
            description="ARN of the Synthea staging bucket",
            export_name="SyntheaStagingBucketArn",
        )
