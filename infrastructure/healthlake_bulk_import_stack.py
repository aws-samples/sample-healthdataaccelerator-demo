"""
CDK Stack for HealthLake Bulk Import Pipeline

Creates:
- S3 bucket for FHIR NDJSON files
- Glue ETL job to transform OpenEMR data to FHIR format
- IAM role for HealthLake import
- Lambda to trigger bulk import
- EventBridge rule for nightly schedule

PHI / HIPAA NOTICE:
This stack creates infrastructure that handles protected health information
(PHI) - FHIR patient records exported to S3 and bulk-imported into AWS
HealthLake. If you process real PHI, this is a HIPAA-regulated workload:
execute an AWS Business Associate Addendum (BAA), keep data within
HIPAA-eligible services, and enable encryption, access logging, and audit
controls. The customer is responsible for compliant handling of regulated data.
This sample ships with synthetic data only.
"""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_iam as iam,
    aws_glue as glue,
    aws_events as events,
    aws_events_targets as targets,
    aws_logs as logs,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from aws_cdk import aws_lambda as lambda_
from constructs import Construct


class HealthLakeBulkImportStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        healthlake_datastore_id: str,
        source_database: str = "openemr_landing",
        source_staging_bucket: str = None,
        source_staging_kms_key_arn: str = None,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Name of the shared data-lake staging bucket that Glue reads from.
        # Not hardcoded: comes from the constructor arg or the
        # 'source_staging_bucket' CDK context value. Leave unset to skip
        # granting cross-bucket access.
        source_staging_bucket = (
            source_staging_bucket
            or self.node.try_get_context("source_staging_bucket")
        )
        # KMS key ARN protecting the source staging bucket (env-specific).
        # Comes from the constructor arg or 'source_staging_kms_key_arn'
        # context; not hardcoded. Leave unset to skip granting KMS access.
        source_staging_kms_key_arn = (
            source_staging_kms_key_arn
            or self.node.try_get_context("source_staging_kms_key_arn")
        )

        # S3 bucket for FHIR NDJSON files
        fhir_bucket = s3.Bucket(
            self,
            "FhirExportBucket",
            bucket_name=f"healthlake-fhir-import-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            # Security hardening: this bucket holds FHIR/PHI export data and
            # must never be public. Block all public access, encrypt at rest,
            # and require TLS in transit.
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(7),  # Clean up old exports
                    prefix="exports/"
                )
            ]
        )

        # IAM role for HealthLake import
        healthlake_import_role = iam.Role(
            self,
            "HealthLakeImportRole",
            role_name="HealthLakeImportRole",
            assumed_by=iam.ServicePrincipal("healthlake.amazonaws.com"),
        )

        # Grant HealthLake access to read from S3
        fhir_bucket.grant_read(healthlake_import_role)
        
        # Add KMS permissions if needed
        healthlake_import_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                # Scope KMS access to keys used via the HealthLake service only,
                # instead of an unconstrained wildcard resource.
                resources=[f"arn:aws:kms:{self.region}:{self.account}:key/*"],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"healthlake.{self.region}.amazonaws.com"
                    }
                },
            )
        )

        # IAM role for Glue ETL job
        glue_role = iam.Role(
            self,
            "GlueETLRole",
            role_name="HealthLakeFhirTransformRole",
            assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole"),
            ]
        )

        # Grant Glue access to the (optional) shared source staging bucket.
        if source_staging_bucket:
            glue_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["s3:GetObject", "s3:ListBucket"],
                    resources=[
                        f"arn:aws:s3:::{source_staging_bucket}",
                        f"arn:aws:s3:::{source_staging_bucket}/*",
                    ],
                )
            )
        
        # Grant KMS permissions for the source bucket's key (if configured).
        # The key ARN is provided via config, not hardcoded.
        if source_staging_kms_key_arn:
            glue_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["kms:Decrypt", "kms:GenerateDataKey"],
                    resources=[source_staging_kms_key_arn],
                )
            )
        
        fhir_bucket.grant_read_write(glue_role)

        # Grant Glue access to Data Catalog
        glue_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "glue:GetDatabase",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetPartitions",
                ],
                resources=["*"],
            )
        )

        # Upload Glue script to S3
        s3deploy.BucketDeployment(
            self,
            "GlueScriptDeployment",
            sources=[s3deploy.Source.asset("glue_scripts")],
            destination_bucket=fhir_bucket,
            destination_key_prefix="scripts",
        )

        # Glue ETL job for transforming OpenEMR to FHIR
        glue_job = glue.CfnJob(
            self,
            "FhirTransformJob",
            name="openemr-to-fhir-transform",
            role=glue_role.role_arn,
            command=glue.CfnJob.JobCommandProperty(
                name="glueetl",
                python_version="3",
                script_location=f"s3://{fhir_bucket.bucket_name}/scripts/transform_to_fhir.py",
            ),
            default_arguments={
                "--job-language": "python",
                "--enable-metrics": "true",
                "--enable-continuous-cloudwatch-log": "true",
                "--source_database": source_database,
                "--output_bucket": fhir_bucket.bucket_name,
                "--TempDir": f"s3://{fhir_bucket.bucket_name}/temp/",
            },
            glue_version="4.0",
            worker_type="G.1X",
            number_of_workers=2,
            timeout=60,  # 1 hour timeout
        )

        # Lambda to orchestrate the pipeline
        orchestrator_lambda = PythonFunction(
            self,
            "OrchestratorLambda",
            function_name="healthlake-bulk-import-orchestrator",
            runtime=lambda_.Runtime.PYTHON_3_11,
            entry="lambda/healthlake_bulk_import",
            index="index.py",
            handler="handler",
            timeout=Duration.minutes(15),
            memory_size=256,
            environment={
                "GLUE_JOB_NAME": "openemr-to-fhir-transform",
                "FHIR_BUCKET": fhir_bucket.bucket_name,
                "HEALTHLAKE_DATASTORE_ID": healthlake_datastore_id,
                "HEALTHLAKE_IMPORT_ROLE_ARN": healthlake_import_role.role_arn,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # Grant Lambda permissions
        orchestrator_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["glue:StartJobRun", "glue:GetJobRun"],
                resources=[f"arn:aws:glue:{self.region}:{self.account}:job/openemr-to-fhir-transform"],
            )
        )

        orchestrator_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "healthlake:StartFHIRImportJob",
                    "healthlake:DescribeFHIRImportJob",
                ],
                resources=[
                    f"arn:aws:healthlake:{self.region}:{self.account}:datastore/fhir/{healthlake_datastore_id}",
                ],
            )
        )

        # Grant Lambda permission to pass the import role to HealthLake
        orchestrator_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[healthlake_import_role.role_arn],
            )
        )

        fhir_bucket.grant_read(orchestrator_lambda)

        # EventBridge rule for nightly execution (2 AM UTC)
        nightly_rule = events.Rule(
            self,
            "NightlyImportRule",
            rule_name="healthlake-nightly-bulk-import",
            schedule=events.Schedule.cron(hour="2", minute="0"),
            description="Trigger nightly HealthLake bulk import from OpenEMR",
        )
        nightly_rule.add_target(targets.LambdaFunction(orchestrator_lambda))
