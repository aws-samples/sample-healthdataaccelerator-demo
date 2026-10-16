"""
CDK Stack for OpenEMR to HealthLake Sync

Creates:
- Lambda function for incremental FHIR sync (every 5 min)
- Lambda function for daily full DB sync (2 AM)
- DynamoDB table for tracking sync state
- EventBridge rules for scheduled execution
- Secrets Manager for OpenEMR credentials

PHI / HIPAA NOTICE:
This stack moves protected health information (PHI) - FHIR patient records
including demographics, diagnoses, medications, and clinical notes - between
OpenEMR and AWS HealthLake. If you process real PHI, this is a HIPAA-regulated
workload: execute an AWS Business Associate Addendum (BAA), keep data within
HIPAA-eligible services, and enable encryption, access logging, and audit
controls. The customer is responsible for compliant handling of regulated data.
This sample ships with synthetic data only.
"""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
    aws_kms as kms,
    aws_logs as logs,
    aws_ec2 as ec2,
    aws_lambda as lambda_,
    triggers,
)
from constructs import Construct


class HealthLakeSyncStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        openemr_base_url: str,
        healthlake_datastore_id: str,
        healthlake_endpoint: str,
        sync_interval_minutes: int = 5,
        vpc: ec2.IVpc = None,
        vpc_id: str = None,
        db_secret_arn: str = None,
        db_security_group_id: str = None,
        openemr_service_security_group_id: str = None,
        openemr_admin_secret_arn: str = None,
        run_initial_sync: bool = False,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Look up VPC if vpc_id provided but vpc is not
        if vpc_id and not vpc:
            vpc = ec2.Vpc.from_lookup(
                self,
                "OpenEmrVpc",
                vpc_id=vpc_id
            )

        # Create security group for Lambda if VPC is provided
        security_group = None
        if vpc:
            # Create security group for Lambda
            security_group = ec2.SecurityGroup(
                self,
                "LambdaSG",
                vpc=vpc,
                description="Security group for HealthLake sync Lambda",
                allow_all_outbound=True,
            )

        # Import the Aurora DB security group once (reused by the daily DB-sync
        # block and the OAuth provisioner block) to avoid importing the same SG
        # twice with duplicate logical ids.
        db_sg = None
        if vpc and security_group and db_secret_arn and db_security_group_id:
            db_sg = ec2.SecurityGroup.from_security_group_id(
                self, "DbSecurityGroup", db_security_group_id, mutable=True
            )

        # Import the OpenEMR service/task security group (not the ALB SG) so
        # in-VPC Lambdas can be granted SG-to-SG ingress on 443 to the task ENIs.
        openemr_service_sg = None
        if vpc and security_group and openemr_service_security_group_id:
            openemr_service_sg = ec2.SecurityGroup.from_security_group_id(
                self,
                "OpenEmrServiceSg",
                openemr_service_security_group_id,
                mutable=True,
            )

        # DynamoDB table for tracking sync state
        sync_state_table = dynamodb.Table(
            self,
            "SyncStateTable",
            table_name="openemr-healthlake-sync-state",
            partition_key=dynamodb.Attribute(
                name="resource_type",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Customer-managed KMS key for encrypting the OpenEMR credentials secret
        # (satisfies CDK Nag AwsSolutions-SMG4 / HIPAA SecretsManagerUsingKMSKey).
        openemr_secret_key = kms.Key(
            self,
            "OpenEMRCredentialsKey",
            description="CMK for OpenEMR HealthLake sync credentials secret",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Secret for OpenEMR credentials
        openemr_secret = secretsmanager.Secret(  # nosec B106 - not a password; creates an AWS-generated secret
            self,
            "OpenEMRCredentials",
            secret_name="openemr-healthlake-sync/credentials",
            description="OpenEMR OAuth credentials for HealthLake sync",
            encryption_key=openemr_secret_key,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                # nosec B106 - not a password: empty-valued JSON template for a
                # Secrets Manager auto-generated secret. Real credential values
                # are populated later / generated via generate_string_key.
                secret_string_template='{"client_id":"","client_secret":"","username":"","password":""}',
                generate_string_key="placeholder",
                exclude_punctuation=True,
            ),
        )

        # Lambda function for FHIR API sync
        sync_lambda_kwargs = {
            "function_name": "openemr-healthlake-sync",
            "runtime": lambda_.Runtime.PYTHON_3_11,
            "code": lambda_.Code.from_asset(
                "lambda/openemr_healthlake_sync",
                bundling={
                    "image": lambda_.Runtime.PYTHON_3_11.bundling_image,
                    "command": [
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output"
                    ],
                }
            ),
            "handler": "handler.handler",
            "timeout": Duration.minutes(14),
            "memory_size": 512,
            "environment": {
                "OPENEMR_BASE_URL": openemr_base_url,
                "HEALTHLAKE_ENDPOINT": healthlake_endpoint,
                "HEALTHLAKE_DATASTORE_ID": healthlake_datastore_id,
                "SYNC_STATE_TABLE": sync_state_table.table_name,
                "OPENEMR_CREDENTIALS_SECRET": openemr_secret.secret_arn,
                # Amazon RDS global CA bundle packaged with the Lambda, used
                # to verify the Aurora/RDS server certificate (TLS).
                "RDS_CA_BUNDLE": "/var/task/global-bundle.pem",
                # OpenEMR is reached over the internal Cloud Map hostname and
                # presents a self-signed CN=localhost cert; disable TLS
                # verification for this internal in-VPC hop only (the handler
                # never uses this opt-out for any public/internet call).
                "OPENEMR_SSL_VERIFY": "false",
            },
        }

        # Clinical-notes-from-DB path in the FHIR sync handler is skipped unless
        # DB_SECRET_ARN is present. Add it conditionally so a None is never
        # injected as a literal env value.
        if db_secret_arn:
            sync_lambda_kwargs["environment"]["DB_SECRET_ARN"] = db_secret_arn
        
        # Add VPC configuration if VPC is provided
        if vpc and security_group:
            sync_lambda_kwargs["vpc"] = vpc
            sync_lambda_kwargs["vpc_subnets"] = ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            )
            sync_lambda_kwargs["security_groups"] = [security_group]

        sync_lambda = lambda_.Function(
            self,
            "SyncFunction",
            **sync_lambda_kwargs
        )

        # Grant Lambda permissions
        sync_state_table.grant_read_write_data(sync_lambda)
        openemr_secret.grant_read(sync_lambda)

        # Allow the FHIR sync Lambda to read the DB secret for the
        # clinical-notes-from-DB path (resource-scoped, mirrors DbSyncFunction).
        # The 3306 network path already exists on the shared security_group
        # (see the daily DB-sync block below), so no extra SG rule is needed.
        if db_secret_arn:
            sync_lambda.add_to_role_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[db_secret_arn],
                )
            )

        # SG-to-SG ingress: allow the FHIR sync Lambda (shared security_group)
        # to reach the OpenEMR task/service on 443 over the Cloud Map path.
        if openemr_service_sg is not None:
            openemr_service_sg.add_ingress_rule(
                security_group,
                ec2.Port.tcp(443),
                "FHIR sync Lambda to OpenEMR task on 443",
            )

        # HealthLake permissions
        sync_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "healthlake:CreateResource",
                    "healthlake:UpdateResource",
                    "healthlake:ReadResource",
                ],
                # Scope to this account/region and the specific datastore
                # instead of a fully-wildcarded ARN.
                resources=[
                    f"arn:aws:healthlake:{self.region}:{self.account}:datastore/fhir/{healthlake_datastore_id}",
                ],
            )
        )
        
        # KMS permissions for HealthLake encryption
        sync_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "kms:GenerateDataKey",
                    "kms:Decrypt",
                    "kms:Encrypt",
                ],
                # Scope KMS access to keys used via the HealthLake service only.
                resources=[f"arn:aws:kms:{self.region}:{self.account}:key/*"],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"healthlake.{self.region}.amazonaws.com"
                    }
                },
            )
        )

        # EventBridge rule for scheduled sync
        sync_rule = events.Rule(
            self,
            "SyncScheduleRule",
            rule_name="openemr-healthlake-sync-schedule",
            schedule=events.Schedule.rate(Duration.minutes(sync_interval_minutes)),
            description=f"Trigger OpenEMR to HealthLake sync every {sync_interval_minutes} minutes",
        )
        sync_rule.add_target(targets.LambdaFunction(sync_lambda))

        # =====================================================================
        # Daily DB Sync Lambda (runs at 2 AM, syncs all data directly from DB)
        # =====================================================================
        if db_secret_arn and vpc and security_group:
            # Use the shared DB security group import (created above) to allow
            # the Lambdas sharing security_group to reach Aurora MySQL.
            if db_sg is not None:
                # Allow Lambda to connect to database
                security_group.add_egress_rule(
                    db_sg,
                    ec2.Port.tcp(3306),
                    "Allow Lambda to connect to Aurora MySQL"
                )
                # Allow DB to accept connections from Lambda
                db_sg.add_ingress_rule(
                    security_group,
                    ec2.Port.tcp(3306),
                    "Allow HealthLake sync Lambda to connect"
                )

            # DB sync Lambda
            db_sync_lambda = lambda_.Function(
                self,
                "DbSyncFunction",
                function_name="openemr-healthlake-db-sync",
                runtime=lambda_.Runtime.PYTHON_3_11,
                code=lambda_.Code.from_asset(
                    "lambda/openemr_db_sync",
                    bundling={
                        "image": lambda_.Runtime.PYTHON_3_11.bundling_image,
                        "command": [
                            "bash", "-c",
                            "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output"
                        ],
                    }
                ),
                handler="handler.handler",
                timeout=Duration.minutes(14),
                memory_size=1024,
                environment={
                    "HEALTHLAKE_ENDPOINT": healthlake_endpoint,
                    "HEALTHLAKE_DATASTORE_ID": healthlake_datastore_id,
                    "SYNC_STATE_TABLE": sync_state_table.table_name,
                    "DB_SECRET_ARN": db_secret_arn,
                    # Amazon RDS global CA bundle packaged with the Lambda, used
                    # to verify the Aurora/RDS server certificate (TLS).
                    "RDS_CA_BUNDLE": "/var/task/global-bundle.pem",
                },
                vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                security_groups=[security_group],
            )

            # Grant permissions
            sync_state_table.grant_read_write_data(db_sync_lambda)
            
            # Grant access to DB secret
            db_sync_lambda.add_to_role_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[db_secret_arn],
                )
            )

            # HealthLake permissions
            db_sync_lambda.add_to_role_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "healthlake:CreateResource",
                        "healthlake:UpdateResource",
                        "healthlake:ReadResource",
                    ],
                    # Scope to this account/region and the specific datastore
                    # instead of a fully-wildcarded ARN.
                    resources=[
                        f"arn:aws:healthlake:{self.region}:{self.account}:datastore/fhir/{healthlake_datastore_id}",
                    ],
                )
            )
            
            # KMS permissions for HealthLake encryption
            db_sync_lambda.add_to_role_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "kms:GenerateDataKey",
                        "kms:Decrypt",
                        "kms:Encrypt",
                    ],
                    # Scope KMS access to keys used via the HealthLake service only.
                    resources=[f"arn:aws:kms:{self.region}:{self.account}:key/*"],
                    conditions={
                        "StringEquals": {
                            "kms:ViaService": f"healthlake.{self.region}.amazonaws.com"
                        }
                    },
                )
            )

            # EventBridge rule for daily sync at 2 AM EST (7 AM UTC)
            db_sync_rule = events.Rule(
                self,
                "DbSyncScheduleRule",
                rule_name="openemr-healthlake-db-sync-schedule",
                schedule=events.Schedule.cron(hour="7", minute="0"),
                description="Trigger daily full DB sync at 2 AM EST",
            )
            db_sync_rule.add_target(targets.LambdaFunction(db_sync_lambda))

            # Output the DB sync Lambda name for manual invocation
            CfnOutput(
                self,
                "DbSyncLambdaName",
                value=db_sync_lambda.function_name,
                description="Lambda function for DB to HealthLake sync (invoke manually or wait for 2 AM schedule)",
            )
            
            # Store reference for initial sync trigger
            self.db_sync_lambda = db_sync_lambda

        # =====================================================================
        # OAuth credential provisioner (runs after OpenEMR is API-enabled)
        # Registers an OAuth client, enables it in the DB, confirms a ROPC user,
        # and writes real credentials into the sync credentials secret.
        # =====================================================================
        if (
            vpc
            and security_group
            and db_secret_arn
            and openemr_service_security_group_id
            and openemr_admin_secret_arn
        ):
            oauth_provisioner = triggers.TriggerFunction(
                self,
                "OpenEmrOAuthProvisioner",
                function_name="openemr-oauth-provisioner",
                runtime=lambda_.Runtime.PYTHON_3_12,
                code=lambda_.Code.from_asset(
                    "lambda/openemr_oauth_provisioner",
                    bundling={
                        "image": lambda_.Runtime.PYTHON_3_12.bundling_image,
                        "command": [
                            "bash", "-c",
                            "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output"
                        ],
                    }
                ),
                handler="handler.handler",
                timeout=Duration.minutes(10),
                memory_size=512,
                environment={
                    "OPENEMR_BASE_URL": openemr_base_url,
                    # Internal self-signed hop only (see sync Lambda env note).
                    "OPENEMR_SSL_VERIFY": "false",
                    "OPENEMR_CREDENTIALS_SECRET": openemr_secret.secret_arn,
                    "DB_SECRET_ARN": db_secret_arn,
                    "OPENEMR_ADMIN_SECRET_ARN": openemr_admin_secret_arn,
                    "RDS_CA_BUNDLE": "/var/task/global-bundle.pem",
                },
                vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                # Reuse the shared Lambda SG so the existing 3306 egress/ingress
                # (daily DB-sync block) and the 443 ingress to the OpenEMR task
                # SG (above) already apply to the provisioner.
                security_groups=[security_group],
            )

            # Read the DB secret (enable the OAuth client via SQL) and the admin
            # secret (ROPC username/password), both resource-scoped.
            oauth_provisioner.add_to_role_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[db_secret_arn, openemr_admin_secret_arn],
                )
            )

            # Read + write the credentials secret and use its CMK.
            openemr_secret.grant_read(oauth_provisioner)
            openemr_secret.grant_write(oauth_provisioner)
            openemr_secret_key.grant_encrypt_decrypt(oauth_provisioner)

        # Output the FHIR sync Lambda name
        CfnOutput(
            self,
            "FhirSyncLambdaName",
            value=sync_lambda.function_name,
            description="Lambda function for FHIR API to HealthLake sync",
        )
        
        # Run initial sync after deployment if requested (typically after data loader has populated data)
        if run_initial_sync and db_secret_arn and vpc and security_group:
            initial_sync_trigger = triggers.TriggerFunction(
                self,
                "InitialSyncTrigger",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="index.handler",
                code=lambda_.Code.from_inline('''
import boto3
import json

def handler(event, context):
    """Trigger the DB sync Lambda after deployment."""
    request_type = event.get('RequestType', 'Create')
    
    if request_type == 'Delete':
        return {'statusCode': 200, 'body': 'Delete acknowledged'}
    
    if request_type == 'Update':
        return {'statusCode': 200, 'body': 'Update skipped'}
    
    import os
    lambda_client = boto3.client('lambda')
    sync_function = os.environ.get('SYNC_FUNCTION_NAME')
    
    if sync_function:
        print(f"Triggering initial HealthLake sync: {sync_function}")
        response = lambda_client.invoke(
            FunctionName=sync_function,
            InvocationType='Event',  # Async - don't wait
            Payload=json.dumps({'source': 'initial_deployment', 'action': 'full_sync'})
        )
        print(f"Sync triggered with status: {response.get('StatusCode')}")
        return {'statusCode': 200, 'body': f'Initial sync triggered for {sync_function}'}
    
    return {'statusCode': 200, 'body': 'No sync function configured'}
'''),
                timeout=Duration.seconds(30),
                environment={
                    "SYNC_FUNCTION_NAME": self.db_sync_lambda.function_name if hasattr(self, 'db_sync_lambda') else "",
                },
            )
            
            # Grant permission to invoke the sync Lambda
            if hasattr(self, 'db_sync_lambda'):
                self.db_sync_lambda.grant_invoke(initial_sync_trigger)
            
            CfnOutput(
                self,
                "InitialSyncStatus",
                value="Initial HealthLake sync will run after deployment completes",
                description="Status of initial sync trigger",
            )
