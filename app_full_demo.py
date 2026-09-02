#!/usr/bin/env python3
"""
Combined CDK App for OpenEMR + Orthanc with Automatic Data Loading

This app deploys both OpenEMR and Orthanc together, with the data loader
automatically receiving the Orthanc URL from the Orthanc stack.

Deploy with:
    cdk deploy --app "python app_full_demo.py" --all

The deployment creates:
1. OpenemrEcsStack - OpenEMR EHR system  
2. OrthancStack - Orthanc PACS server (shares VPC with OpenEMR)
3. DataLoaderTrigger - Runs after both stacks to load data

The data loader runs as a CloudFormation custom resource that depends on
both stacks, ensuring Orthanc is available when data loading begins.
"""

import path_setup  # noqa: F401

import os
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_lambda as _lambda,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_s3 as s3,
    triggers,
    CfnOutput,
)

from infrastructure.openemr_ecs_stack import OpenemrEcsStack
from infrastructure.orthanc_stack import OrthancStack
from infrastructure.healthlake_sync_stack import HealthLakeSyncStack
from infrastructure.healthlake_datastore_stack import HealthLakeDatastoreStack
from infrastructure.patient_dashboard_stack import PatientDashboardStack
from infrastructure.synthea_staging_stack import SyntheaStagingStack


class CombinedDataLoaderStack(cdk.Stack):
    """Stack that runs data loader after both OpenEMR and Orthanc are deployed."""
    
    def __init__(
        self,
        scope,
        construct_id,
        vpc: ec2.IVpc,
        db_secret_arn: str,
        db_security_group_id: str,  # Pass ID instead of object to avoid cyclic ref
        orthanc_url: str,
        synthea_bucket: s3.IBucket,  # Pre-staged bucket with FHIR bundles already uploaded
        orthanc_ecs_security_group_id: str = None,  # For internal access to Orthanc ECS tasks
        healthlake_sync_function_name: str = None,  # Lambda to trigger after data load
        orthanc_credentials_secret=None,  # Secrets Manager secret with Orthanc admin creds
        **kwargs
    ):
        super().__init__(scope, construct_id, **kwargs)
        
        # Import the DB security group by ID (avoids cyclic dependency)
        db_security_group = ec2.SecurityGroup.from_security_group_id(
            self, "ImportedDbSecurityGroup", db_security_group_id
        )
        
        # Create security group for the data loader Lambda
        data_loader_sg = ec2.SecurityGroup(
            self,
            "DataLoaderSecurityGroup",
            vpc=vpc,
            description="Security group for combined data loader Lambda",
            allow_all_outbound=True,  # Allows access to Orthanc via internal URL
        )
        
        # Allow Lambda to connect to the database (port 3306)
        # Using the imported security group avoids cyclic dependency
        db_security_group.add_ingress_rule(
            data_loader_sg,
            ec2.Port.tcp(3306),
            "Allow combined data loader to connect to database"
        )
        
        # Allow Lambda to access Orthanc ECS tasks directly on port 8042 (internal URL via Cloud Map)
        # This is more reliable than going through the internet-facing ALB
        if orthanc_ecs_security_group_id:
            orthanc_ecs_sg = ec2.SecurityGroup.from_security_group_id(
                self, "ImportedOrthancEcsSg", orthanc_ecs_security_group_id,
                mutable=True  # Allow adding ingress rules to imported security group
            )
            
            # Add direct security group rule: Lambda SG -> Orthanc ECS SG on port 8042
            orthanc_ecs_sg.add_ingress_rule(
                data_loader_sg,
                ec2.Port.tcp(8042),
                "Allow data loader Lambda to access Orthanc ECS tasks directly"
            )
        
        # Use the pre-staged S3 bucket (data already uploaded before this stack deploys)
        self.synthea_bucket = synthea_bucket

        # Create the data loader trigger
        self.data_loader = triggers.TriggerFunction(
            self,
            "CombinedDataLoader",
            runtime=_lambda.Runtime.PYTHON_3_12,
            code=_lambda.Code.from_asset(
                'lambda/data_loader',
                bundling={
                    "image": _lambda.Runtime.PYTHON_3_12.bundling_image,
                    "command": [
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output"
                    ]
                }
            ),
            handler='handler.handler',
            architecture=_lambda.Architecture.ARM_64,
            timeout=Duration.minutes(15),
            memory_size=1024,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[data_loader_sg],
            environment={
                'DB_SECRET_ARN': db_secret_arn,
                'ORTHANC_URL': orthanc_url,
                'HEALTHLAKE_SYNC_FUNCTION': healthlake_sync_function_name or '',
                'SYNTHEA_BUCKET': self.synthea_bucket.bucket_name,
                'SYNTHEA_PREFIX': 'synthea-bundles/',
                # Orthanc admin credentials come from Secrets Manager, not a
                # hardcoded default. Empty when no secret is provided.
                'ORTHANC_CREDENTIALS_SECRET_ARN': (
                    orthanc_credentials_secret.secret_arn
                    if orthanc_credentials_secret else ''
                ),
            }
        )
        
        # Grant Lambda permission to read database credentials
        self.data_loader.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue"],
                resources=[db_secret_arn]
            )
        )

        # Grant Lambda read access to the Orthanc admin credentials secret
        if orthanc_credentials_secret:
            orthanc_credentials_secret.grant_read(self.data_loader)
        
        # Grant Lambda permission to invoke the HealthLake sync function
        if healthlake_sync_function_name:
            self.data_loader.add_to_role_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["lambda:InvokeFunction"],
                    resources=[f"arn:aws:lambda:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:function:{healthlake_sync_function_name}"]
                )
            )
        
        # Grant Lambda read access to the Synthea bucket (s3:GetObject and s3:ListBucket)
        self.synthea_bucket.grant_read(self.data_loader)
        
        CfnOutput(
            self,
            "SyntheaBucketName",
            value=self.synthea_bucket.bucket_name,
            description="S3 bucket name for Synthea FHIR bundles (pre-staged)",
        )
        
        CfnOutput(
            self,
            "DataLoaderStatus",
            value=f"Data loader will populate OpenEMR and Orthanc at {orthanc_url}",
            description="Combined data loader status",
        )


app = cdk.App()

# Optional cdk-nag pass over the ENTIRE solution (all stacks). Enabled with
# `-c cdk_nag=true`; off by default so normal deploys are unaffected. Aspects
# added here traverse every stack created below at synth time.
if app.node.try_get_context("cdk_nag") == "true":
    from cdk_nag import AwsSolutionsChecks, HIPAASecurityChecks
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    cdk.Aspects.of(app).add(HIPAASecurityChecks(verbose=True))

# Get configuration from context
security_group_ip_range = app.node.try_get_context("security_group_ip_range_ipv4")
route53_domain = app.node.try_get_context("route53_domain")
enable_data_loading = app.node.try_get_context("enable_automatic_data_loading") == "true"
# Responsible-AI guardrails for the dashboard's Bedrock features. Default ON;
# set enable_bedrock_guardrails="false" in context to disable.
enable_bedrock_guardrails = (
    app.node.try_get_context("enable_bedrock_guardrails") != "false"
)

# Option to use existing HealthLake datastore or create new one
# Set to "create_new" or leave empty to create a new datastore
EXISTING_HEALTHLAKE_DATASTORE_ID = app.node.try_get_context("healthlake_datastore_id")

# Build allowed IP ranges list
allowed_ip_ranges = []
if security_group_ip_range:
    allowed_ip_ranges.append(security_group_ip_range)

if not allowed_ip_ranges:
    raise ValueError(
        "security_group_ip_range_ipv4 must be configured in cdk.json. "
        "Get your IP with: curl -s https://checkip.amazonaws.com"
    )

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION"),
)

# Deploy Synthea staging bucket first (data is uploaded here before full deploy)
synthea_staging_stack = SyntheaStagingStack(
    app,
    "SyntheaStagingStack",
    env=env,
)

# Deploy OpenEMR first (creates VPC) - disable built-in data loader
# We'll use the combined data loader instead
openemr_stack = OpenemrEcsStack(
    app,
    "OpenemrEcsStack",
    env=env,
)

# Deploy HealthLake datastore (or use existing one)
if EXISTING_HEALTHLAKE_DATASTORE_ID and EXISTING_HEALTHLAKE_DATASTORE_ID != "create_new":
    # Use existing datastore
    HEALTHLAKE_DATASTORE_ID = EXISTING_HEALTHLAKE_DATASTORE_ID
    healthlake_datastore_stack = None
else:
    # Create new HealthLake datastore
    healthlake_datastore_stack = HealthLakeDatastoreStack(
        app,
        "HealthLakeDatastoreStack",
        datastore_name="healthcare-demo-datastore",
        env=env,
    )
    HEALTHLAKE_DATASTORE_ID = healthlake_datastore_stack.datastore_id

# HealthLake CMK ARN so downstream stacks can scope KMS policies to the specific
# key. Only available when we create the datastore here; when reusing an existing
# datastore (bring-your-own), the key ARN is unknown and downstream stacks fall
# back to a ViaService-scoped policy.
healthlake_kms_key_arn = (
    healthlake_datastore_stack.kms_key_arn if healthlake_datastore_stack else None
)

# Deploy Orthanc in the same VPC with HealthLake sync
# Use the certificate created by OpenEMR stack (wildcard cert for the domain)
orthanc_certificate_arn = None
if route53_domain and hasattr(openemr_stack, 'certificate'):
    orthanc_certificate_arn = openemr_stack.certificate.certificate_arn

orthanc_stack = OrthancStack(
    app,
    "OrthancStack",
    vpc=openemr_stack.vpc,
    healthlake_datastore_id=HEALTHLAKE_DATASTORE_ID,
    healthlake_kms_key_arn=healthlake_kms_key_arn,
    allowed_ip_ranges=allowed_ip_ranges,
    certificate_arn=orthanc_certificate_arn,
    domain_name=f"orthanc.{route53_domain}" if route53_domain else None,
    hosted_zone_id=app.node.try_get_context("route53_hosted_zone_id"),
    env=env,
)

# Orthanc depends on OpenEMR for VPC
orthanc_stack.add_dependency(openemr_stack)
if healthlake_datastore_stack:
    orthanc_stack.add_dependency(healthlake_datastore_stack)

# Deploy HealthLake sync for OpenEMR FIRST (creates the sync Lambda)
# This must deploy before data loader so the Lambda exists when data loader tries to invoke it
healthlake_sync_stack = HealthLakeSyncStack(
    app,
    "HealthLakeSyncStack",
    openemr_base_url=f"https://{openemr_stack.alb.load_balancer_dns_name}",
    healthlake_datastore_id=HEALTHLAKE_DATASTORE_ID,
    healthlake_endpoint=f"https://healthlake.us-east-1.amazonaws.com/datastore/{HEALTHLAKE_DATASTORE_ID}/r4/",
    vpc=openemr_stack.vpc,
    db_secret_arn=openemr_stack.db_instance.secret.secret_arn,
    db_security_group_id=openemr_stack.db_sec_group.security_group_id,
    run_initial_sync=False,  # Data loader will trigger sync, not the stack itself
    env=env,
)
healthlake_sync_stack.add_dependency(openemr_stack)
if healthlake_datastore_stack:
    healthlake_sync_stack.add_dependency(healthlake_datastore_stack)

# Deploy combined data loader AFTER sync stack (so sync Lambda exists)
# Data loader will trigger HealthLake sync after loading data
if enable_data_loading:
    data_loader_stack = CombinedDataLoaderStack(
        app,
        "CombinedDataLoaderStack",
        vpc=openemr_stack.vpc,
        db_secret_arn=openemr_stack.db_instance.secret.secret_arn,
        db_security_group_id=openemr_stack.db_sec_group.security_group_id,
        # Use internal URL via Cloud Map (stays within VPC, avoids NAT/internet routing)
        # This allows SG-to-SG rules to work properly
        orthanc_url=orthanc_stack.internal_orthanc_url,
        orthanc_ecs_security_group_id=orthanc_stack.ecs_security_group.security_group_id,
        synthea_bucket=synthea_staging_stack.bucket,  # Pre-staged bucket with data already uploaded
        healthlake_sync_function_name="openemr-healthlake-db-sync",  # Trigger sync after data load completes
        orthanc_credentials_secret=orthanc_stack.orthanc_admin_secret,  # Orthanc admin creds from Secrets Manager
        env=env,
    )
    
    # Data loader runs after OpenEMR, Orthanc, AND HealthLakeSyncStack are ready
    data_loader_stack.add_dependency(openemr_stack)
    data_loader_stack.add_dependency(orthanc_stack)
    data_loader_stack.add_dependency(healthlake_sync_stack)  # Ensures sync Lambda exists
    data_loader_stack.add_dependency(synthea_staging_stack)  # Ensures bucket exists

# Lake Formation governance is deployed via MDAA (see mdaa/ directory)
# Run: cd mdaa && mdaa deploy (after CDK stacks are deployed)

# Deploy Patient 360 Dashboard
# Use the same certificate from OpenEMR stack
patient_dashboard_stack = PatientDashboardStack(
    app,
    "PatientDashboardStack",
    healthlake_datastore_id=HEALTHLAKE_DATASTORE_ID,
    healthlake_kms_key_arn=healthlake_kms_key_arn,
    orthanc_url=orthanc_stack.orthanc_url,
    vpc=openemr_stack.vpc,
    domain_name=f"dashboard.{route53_domain}" if route53_domain else None,
    certificate_arn=orthanc_certificate_arn,
    hosted_zone_id=app.node.try_get_context("route53_hosted_zone_id"),
    orthanc_credentials_secret=orthanc_stack.orthanc_admin_secret,  # Orthanc admin creds from Secrets Manager
    enable_bedrock_guardrails=enable_bedrock_guardrails,  # Responsible-AI guardrail (default on)
    env=env,
)
patient_dashboard_stack.add_dependency(openemr_stack)
patient_dashboard_stack.add_dependency(orthanc_stack)
if healthlake_datastore_stack:
    patient_dashboard_stack.add_dependency(healthlake_datastore_stack)


# ---------------------------------------------------------------------------
# Documented CDK Nag suppressions
# ---------------------------------------------------------------------------
# This is a SAMPLE that ships with synthetic data only (see the PHI / HIPAA
# notices in each stack). The findings suppressed below are intentional
# trade-offs that keep the sample cheap and easy to deploy / tear down. A
# production PHI workload MUST revisit every one of these: enable backups /
# PITR, dead-letter queues, reserved concurrency, secret rotation, WAF,
# access logging, deletion protection, VPC isolation, and customer-managed
# KMS keys where appropriate. Suppressions are only applied when the cdk-nag
# aspects run (`-c cdk_nag=true`).
if app.node.try_get_context("cdk_nag") == "true":
    from cdk_nag import NagSuppressions

    # Applied to every stack: findings driven by CDK-generated constructs
    # (custom-resource providers, BucketDeployment, grant_* helpers).
    _COMMON = [
        {"id": "AwsSolutions-IAM4",
         "reason": "CDK-generated roles for custom-resource/provider Lambdas use AWS managed policies (e.g. AWSLambdaBasicExecutionRole) in this sample."},
        {"id": "AwsSolutions-IAM5",
         "reason": "Wildcard permissions originate from CDK grant_* helpers and the custom-resource / BucketDeployment providers, scoped to the resources they manage."},
        {"id": "AwsSolutions-L1",
         "reason": "Non-latest runtime belongs to the CDK custom-resource provider framework; first-party functions target Python 3.11."},
        {"id": "HIPAA.Security-IAMNoInlinePolicy",
         "reason": "CDK emits inline policies for grant_* helpers and custom resources."},
        {"id": "HIPAA.Security-LambdaDLQ",
         "reason": "Demo Lambdas (sync + custom resources) do not require a dead-letter queue."},
        {"id": "HIPAA.Security-LambdaConcurrency",
         "reason": "Reserved concurrency is intentionally unset for this low-volume sample."},
    ]

    # Shared justifications reused across stacks.
    _R_ROTATION = "Static credentials for the sample; automatic rotation would break the demo flow. Enable rotation for production PHI."
    _R_KMSSECRET = "Secret uses the default Secrets Manager encryption; a customer-managed KMS key is out of scope for this sample."
    _R_LOGKMS = "CloudWatch Logs use default encryption; customer-managed KMS keys are out of scope for this sample."
    _R_S3LOG = "Server access logging is omitted for sample buckets holding synthetic data."
    _R_S3REPL = "Cross-region replication is not configured for disposable sample buckets."
    _R_S3KMS = "Buckets use SSE-S3 (AES-256); customer-managed KMS is out of scope for this sample."
    _R_LAMBDA_VPC = "Lambda runs outside the VPC where VPC-only access is not required by the sample."
    _R_EFS_BACKUP = "EFS is not enrolled in a backup plan for this disposable sample."
    _R_DDB = "The table is reconstructable state; PITR / backup plan are omitted for the sample."
    _R_ELB = "Deletion protection, access logs and HTTPS redirect / ACM cert are intentionally relaxed for the sample (no custom domain in the default deploy)."
    _R_RDS = "Demo database with synthetic data: enhanced monitoring, backup plan, deletion protection and multi-AZ are intentionally relaxed for easy teardown."

    _per_stack = {
        synthea_staging_stack: [
            {"id": "AwsSolutions-S1", "reason": _R_S3LOG},
            {"id": "HIPAA.Security-S3BucketLoggingEnabled", "reason": _R_S3LOG},
            {"id": "HIPAA.Security-S3BucketReplicationEnabled", "reason": _R_S3REPL},
            {"id": "HIPAA.Security-S3DefaultEncryptionKMS", "reason": _R_S3KMS},
        ],
        healthlake_sync_stack: [
            {"id": "AwsSolutions-SMG4", "reason": _R_ROTATION},
            {"id": "AwsSolutions-DDB3", "reason": _R_DDB},
            {"id": "HIPAA.Security-DynamoDBInBackupPlan", "reason": _R_DDB},
            {"id": "HIPAA.Security-DynamoDBPITREnabled", "reason": _R_DDB},
            {"id": "HIPAA.Security-SecretsManagerRotationEnabled", "reason": _R_ROTATION},
        ],
        orthanc_stack: [
            {"id": "AwsSolutions-ECS2", "reason": "Container environment variables are non-sensitive sample configuration; secrets are injected from Secrets Manager."},
            {"id": "AwsSolutions-ECS4", "reason": "Container Insights is not enabled for the sample to reduce cost."},
            {"id": "AwsSolutions-ELB2", "reason": _R_ELB},
            {"id": "AwsSolutions-SMG4", "reason": _R_ROTATION},
            {"id": "AwsSolutions-DDB3", "reason": _R_DDB},
            {"id": "HIPAA.Security-DynamoDBInBackupPlan", "reason": _R_DDB},
            {"id": "HIPAA.Security-DynamoDBPITREnabled", "reason": _R_DDB},
            {"id": "HIPAA.Security-EFSInBackupPlan", "reason": _R_EFS_BACKUP},
            {"id": "HIPAA.Security-SecretsManagerRotationEnabled", "reason": _R_ROTATION},
            {"id": "HIPAA.Security-SecretsManagerUsingKMSKey", "reason": "Orthanc admin secret is consumed cross-stack and injected into ECS; default encryption is retained for the sample."},
            {"id": "HIPAA.Security-CloudWatchLogGroupEncrypted", "reason": _R_LOGKMS},
            {"id": "HIPAA.Security-ALBHttpDropInvalidHeaderEnabled", "reason": _R_ELB},
            {"id": "HIPAA.Security-ELBDeletionProtectionEnabled", "reason": _R_ELB},
            {"id": "HIPAA.Security-ELBLoggingEnabled", "reason": _R_ELB},
            {"id": "HIPAA.Security-ALBHttpToHttpsRedirection", "reason": _R_ELB},
            {"id": "HIPAA.Security-ELBv2ACMCertificateRequired", "reason": _R_ELB},
        ],
        openemr_stack: [
            {"id": "AwsSolutions-S1", "reason": _R_S3LOG},
            {"id": "AwsSolutions-SMG4", "reason": "OpenEMR DB credentials are RDS-managed secrets; rotation is handled by RDS in production configurations."},
            {"id": "AwsSolutions-RDS6", "reason": _R_RDS},
            {"id": "AwsSolutions-RDS10", "reason": _R_RDS},
            {"id": "AwsSolutions-RDS11", "reason": _R_RDS},
            {"id": "AwsSolutions-RDS14", "reason": _R_RDS},
            {"id": "AwsSolutions-ECS4", "reason": "Container Insights is not enabled for the sample to reduce cost."},
            {"id": "HIPAA.Security-CloudWatchLogGroupEncrypted", "reason": _R_LOGKMS},
            {"id": "HIPAA.Security-S3BucketLoggingEnabled", "reason": _R_S3LOG},
            {"id": "HIPAA.Security-S3BucketReplicationEnabled", "reason": _R_S3REPL},
            {"id": "HIPAA.Security-S3DefaultEncryptionKMS", "reason": _R_S3KMS},
            {"id": "HIPAA.Security-VPCNoUnrestrictedRouteToIGW", "reason": "Public subnets require a route to the internet gateway for the ALB in this sample topology."},
            {"id": "HIPAA.Security-VPCDefaultSecurityGroupClosed", "reason": "Default security group is left at CDK defaults for the sample."},
            {"id": "HIPAA.Security-SecretsManagerRotationEnabled", "reason": _R_ROTATION},
            {"id": "HIPAA.Security-SecretsManagerUsingKMSKey", "reason": "OpenEMR DB credentials are RDS-managed secrets; default encryption is retained for the sample."},
            {"id": "HIPAA.Security-RDSEnhancedMonitoringEnabled", "reason": _R_RDS},
            {"id": "HIPAA.Security-RDSInBackupPlan", "reason": _R_RDS},
            {"id": "HIPAA.Security-RDSInstanceDeletionProtectionEnabled", "reason": _R_RDS},
            {"id": "HIPAA.Security-EFSInBackupPlan", "reason": _R_EFS_BACKUP},
            {"id": "HIPAA.Security-LambdaInsideVPC", "reason": _R_LAMBDA_VPC},
            {"id": "HIPAA.Security-CloudTrailCloudWatchLogsEnabled", "reason": "CloudTrail delivers to S3 for the sample; CloudWatch Logs integration is out of scope."},
            {"id": "HIPAA.Security-ELBDeletionProtectionEnabled", "reason": _R_ELB},
            {"id": "HIPAA.Security-ALBHttpToHttpsRedirection", "reason": _R_ELB},
            {"id": "HIPAA.Security-ELBv2ACMCertificateRequired", "reason": _R_ELB},
        ],
        patient_dashboard_stack: [
            {"id": "AwsSolutions-SMG4", "reason": _R_ROTATION},
            {"id": "AwsSolutions-S1", "reason": _R_S3LOG},
            {"id": "AwsSolutions-COG1", "reason": "Cognito password policy uses defaults for the sample dashboard."},
            {"id": "AwsSolutions-COG2", "reason": "MFA is not enforced on the sample dashboard user pool."},
            {"id": "AwsSolutions-COG3", "reason": "Advanced security mode is not enabled for the sample."},
            {"id": "AwsSolutions-COG4", "reason": "API methods are protected by a Cognito user pool authorizer at the application layer for the sample."},
            {"id": "AwsSolutions-APIG1", "reason": "API access logging is not enabled for the sample."},
            {"id": "AwsSolutions-APIG2", "reason": "Request validation is handled in the proxy Lambda for the sample."},
            {"id": "AwsSolutions-APIG3", "reason": "WAF is not attached to the sample API."},
            {"id": "AwsSolutions-APIG4", "reason": "Authorization is enforced via Cognito for the sample dashboard API."},
            {"id": "AwsSolutions-APIG6", "reason": "Method-level CloudWatch logging is not enabled for the sample."},
            {"id": "AwsSolutions-CFR1", "reason": "CloudFront geo restriction is not configured for the sample."},
            {"id": "AwsSolutions-CFR2", "reason": "CloudFront is not integrated with WAF for the sample."},
            {"id": "AwsSolutions-CFR3", "reason": "CloudFront access logging is not enabled for the sample."},
            {"id": "AwsSolutions-CFR4", "reason": "Default CloudFront viewer certificate is used when no custom domain is configured."},
            {"id": "HIPAA.Security-LambdaInsideVPC", "reason": _R_LAMBDA_VPC},
            {"id": "HIPAA.Security-APIGWCacheEnabledAndEncrypted", "reason": "API caching is not enabled for the sample."},
            {"id": "HIPAA.Security-APIGWExecutionLoggingEnabled", "reason": "Execution logging is not enabled for the sample API."},
            {"id": "HIPAA.Security-APIGWSSLEnabled", "reason": "Backend SSL client certificates are not configured for the sample."},
            {"id": "HIPAA.Security-APIGWXrayEnabled", "reason": "X-Ray tracing is not enabled for the sample API."},
            {"id": "HIPAA.Security-S3BucketLoggingEnabled", "reason": _R_S3LOG},
            {"id": "HIPAA.Security-S3BucketReplicationEnabled", "reason": _R_S3REPL},
            {"id": "HIPAA.Security-S3DefaultEncryptionKMS", "reason": _R_S3KMS},
            {"id": "HIPAA.Security-SecretsManagerRotationEnabled", "reason": _R_ROTATION},
        ],
    }

    _stacks_to_suppress = [
        synthea_staging_stack,
        openemr_stack,
        orthanc_stack,
        healthlake_sync_stack,
        patient_dashboard_stack,
    ]
    if healthlake_datastore_stack:
        _stacks_to_suppress.append(healthlake_datastore_stack)
    if enable_data_loading:
        _stacks_to_suppress.append(data_loader_stack)

    for _stack in _stacks_to_suppress:
        _rules = list(_COMMON) + _per_stack.get(_stack, [])
        NagSuppressions.add_stack_suppressions(_stack, _rules, apply_to_nested_stacks=True)


app.synth()
