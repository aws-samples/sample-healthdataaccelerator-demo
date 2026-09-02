"""
CDK Stack for Orthanc PACS Server

Deploys Orthanc on ECS Fargate with:
- EFS for persistent DICOM storage (encrypted)
- ALB for web interface with HTTPS support
- Security groups with IP restrictions (required)
- Lambda for syncing to HealthLake

PHI / HIPAA NOTICE:
Orthanc is a PACS server that stores and serves DICOM medical imaging, which is
protected health information (PHI) under HIPAA. If you process real PHI, this is
a HIPAA-regulated workload: execute an AWS Business Associate Addendum (BAA),
keep data within HIPAA-eligible services, and enable encryption, access logging,
and audit controls. The customer is responsible for compliant handling of
regulated data. This sample ships with synthetic data only.
"""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_iam as iam,
    aws_logs as logs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_servicediscovery as servicediscovery,
    aws_secretsmanager as secretsmanager,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from aws_cdk import aws_lambda as lambda_
from constructs import Construct
from typing import List, Optional


class OrthancStack(Stack):
    """Orthanc PACS Stack."""
    
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.IVpc = None,
        healthlake_datastore_id: str = None,
        healthlake_kms_key_arn: Optional[str] = None,
        allowed_ip_ranges: List[str] = None,
        certificate_arn: Optional[str] = None,
        domain_name: Optional[str] = None,
        hosted_zone_id: Optional[str] = None,
        **kwargs
    ) -> None:
        """
        Initialize Orthanc PACS Stack.
        
        Args:
            vpc: VPC to deploy into (will lookup default if not provided)
            healthlake_datastore_id: HealthLake datastore ID for sync
            allowed_ip_ranges: List of CIDR ranges allowed to access (REQUIRED for security)
                              e.g., ["72.21.196.66/32", "10.0.0.0/8"]
            certificate_arn: ACM certificate ARN for HTTPS (recommended for production)
            domain_name: Custom domain name (e.g., orthanc.example.com)
            hosted_zone_id: Route53 hosted zone ID for the domain
        """
        super().__init__(scope, construct_id, **kwargs)

        # Specific HealthLake CMK ARN (when known) so the sync Lambda's KMS
        # policy can be scoped to it instead of a wildcard.
        self.healthlake_kms_key_arn = healthlake_kms_key_arn

        # SECURITY: Require explicit IP ranges - no default to 0.0.0.0/0
        if not allowed_ip_ranges or len(allowed_ip_ranges) == 0:
            raise ValueError(
                "allowed_ip_ranges is required for security. "
                "Provide a list of CIDR ranges, e.g., ['72.21.196.66/32']"
            )
        
        # Validate no 0.0.0.0/0 in allowed ranges
        if "0.0.0.0/0" in allowed_ip_ranges:
            raise ValueError(
                "0.0.0.0/0 is not allowed for security reasons. "
                "Please specify specific IP ranges."
            )
        
        # Lookup VPC if not provided
        if vpc is None:
            vpc = ec2.Vpc.from_lookup(
                self,
                "ExistingVpc",
                vpc_id="vpc-072c924484c2114d5",
            )

        # ALB Security Group - restricts inbound traffic from allowed IPs only
        alb_sg = ec2.SecurityGroup(
            self,
            "ALBSecurityGroup",
            vpc=vpc,
            description="Security group for Orthanc ALB - IP restricted",
            allow_all_outbound=True,
        )
        
        # Add ingress rules for ALB from allowed IP ranges
        for cidr in allowed_ip_ranges:
            # Only add HTTP (port 80) if no certificate (dev/testing only)
            # When certificate is provided, HTTPS only - no port 80 in security group
            if not certificate_arn:
                alb_sg.add_ingress_rule(
                    ec2.Peer.ipv4(cidr),
                    ec2.Port.tcp(80),
                    f"HTTP from {cidr}"
                )
            else:
                # HTTPS only - port 443 only, no port 80
                alb_sg.add_ingress_rule(
                    ec2.Peer.ipv4(cidr),
                    ec2.Port.tcp(443),
                    f"HTTPS from {cidr}"
                )

        # ECS Task Security Group - allows traffic from ALB only
        ecs_sg = ec2.SecurityGroup(
            self,
            "ECSSecurityGroup",
            vpc=vpc,
            description="Security group for Orthanc ECS tasks",
            allow_all_outbound=True,
        )
        
        # Allow ALB to reach ECS tasks on port 8042
        ecs_sg.add_ingress_rule(
            alb_sg,
            ec2.Port.tcp(8042),
            "Orthanc web interface from ALB"
        )
        
        # Allow DICOM protocol from allowed IPs (NLB passes through client IP)
        for cidr in allowed_ip_ranges:
            ecs_sg.add_ingress_rule(
                ec2.Peer.ipv4(cidr),
                ec2.Port.tcp(4242),
                f"DICOM protocol from {cidr}"
            )
        
        # Store certificate ARN for later use
        self.certificate_arn = certificate_arn

        # EFS for persistent storage
        file_system = efs.FileSystem(
            self,
            "OrthancStorage",
            vpc=vpc,
            removal_policy=RemovalPolicy.DESTROY,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.BURSTING,
            encrypted=True,
        )

        # Allow ECS to access EFS
        file_system.connections.allow_default_port_from(ecs_sg)

        # EFS Access Point - Orthanc runs as root
        access_point = file_system.add_access_point(
            "OrthancAccessPoint",
            path="/orthanc",
            create_acl=efs.Acl(owner_uid="0", owner_gid="0", permissions="755"),
            posix_user=efs.PosixUser(uid="0", gid="0"),
        )

        # ECS Cluster - use stack name to avoid conflicts
        cluster = ecs.Cluster(
            self,
            "OrthancCluster",
            vpc=vpc,
            cluster_name=f"{construct_id.lower()}-cluster",
        )

        # Create secret for Orthanc admin credentials with an auto-generated
        # random password. The username is fixed to "admin" and the password
        # is generated by Secrets Manager (never hardcoded). The generated
        # password is injected into the container as an ECS secret below.
        self.orthanc_admin_secret = secretsmanager.Secret(
            self,
            "OrthancAdminSecret",
            secret_name=f"{construct_id}/admin-credentials",
            description="Orthanc PACS admin credentials",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                # nosec B106 - not a password: this is a JSON template for a
                # Secrets Manager auto-generated secret. The password value is
                # produced by AWS via generate_string_key, never hardcoded.
                secret_string_template='{"username": "admin"}',
                generate_string_key="password",
                # Orthanc registered-users JSON is passed via env; keep the
                # password free of characters that are awkward in that context.
                exclude_punctuation=True,
                password_length=20,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Task Definition
        task_definition = ecs.FargateTaskDefinition(
            self,
            "OrthancTaskDef",
            memory_limit_mib=2048,
            cpu=1024,
        )

        # Add EFS volume
        task_definition.add_volume(
            name="orthanc-storage",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=file_system.file_system_id,
                transit_encryption="ENABLED",
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=access_point.access_point_id,
                    iam="ENABLED",
                ),
            ),
        )

        # Grant EFS access to task role
        file_system.grant_read_write(task_definition.task_role)

        # Orthanc container - using orthancteam/orthanc with DICOMweb enabled
        # Both index and storage on EFS for persistence across scale down/up
        # SQLite locking on EFS is fine with single task (desired_count=1)
        container = task_definition.add_container(
            "OrthancContainer",
            image=ecs.ContainerImage.from_registry("orthancteam/orthanc:latest-full"),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="orthanc",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
            secrets={
                # Inject the generated admin password from Secrets Manager.
                # Orthanc's env-var config supports nested keys via "__", so
                # ORTHANC__REGISTERED_USERS__admin sets the admin password
                # without embedding any credential in the task definition.
                "ORTHANC__REGISTERED_USERS__admin": ecs.Secret.from_secrets_manager(
                    self.orthanc_admin_secret, "password"
                ),
            },
            environment={
                "ORTHANC__AUTHENTICATION_ENABLED": "true",
                "ORTHANC__DICOM_AET": "ORTHANC",
                "ORTHANC__DICOM_PORT": "4242",
                # Both index and storage on EFS for persistence
                "ORTHANC__INDEX_DIRECTORY": "/var/lib/orthanc/db",
                "ORTHANC__STORAGE_DIRECTORY": "/var/lib/orthanc/storage",
                # Enable DICOMweb plugin for OHIF viewer
                "DICOM_WEB_PLUGIN_ENABLED": "true",
                "ORTHANC__DICOM_WEB__ENABLE": "true",
                "ORTHANC__DICOM_WEB__ROOT": "/dicom-web/",
                "ORTHANC__DICOM_WEB__ENABLEWADO": "true",
                "ORTHANC__DICOM_WEB__WADOROOT": "/wado",
                "ORTHANC__DICOM_WEB__SSL": "false",
                "ORTHANC__DICOM_WEB__STUDIESMETADATA": "MainDicomTags",
                "ORTHANC__DICOM_WEB__SERIESMETADATA": "Full",
                # Enable OHIF viewer plugin
                "OHIF_PLUGIN_ENABLED": "true",
                # Enable Stone Web Viewer plugin
                "STONE_WEB_VIEWER_PLUGIN_ENABLED": "true",
            },
            port_mappings=[
                ecs.PortMapping(container_port=8042, protocol=ecs.Protocol.TCP),
                ecs.PortMapping(container_port=4242, protocol=ecs.Protocol.TCP),
            ],
        )

        # Mount entire /var/lib/orthanc on EFS (includes both db index and storage)
        container.add_mount_points(
            ecs.MountPoint(
                container_path="/var/lib/orthanc",
                source_volume="orthanc-storage",
                read_only=False,
            )
        )

        # Application Load Balancer
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "OrthancALB",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
        )

        # Target group for web interface (Orthanc uses 8042)
        web_target_group = elbv2.ApplicationTargetGroup(
            self,
            "OrthancWebTargetGroup",
            vpc=vpc,
            port=8042,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/app/explorer.html",
                healthy_http_codes="200-401",  # 401 is OK (auth required)
                interval=Duration.seconds(30),
            ),
        )

        # Add HTTPS listener if certificate provided
        if certificate_arn:
            certificate = acm.Certificate.from_certificate_arn(
                self, "Certificate", certificate_arn
            )
            
            # HTTPS listener only - no HTTP listener since port 80 is blocked
            # Note: open=False prevents CDK from adding 0.0.0.0/0 rule
            alb.add_listener(
                "HttpsListener",
                port=443,
                open=False,
                certificates=[certificate],
                default_target_groups=[web_target_group],
            )
            
            web_url = f"https://{alb.load_balancer_dns_name}"
        else:
            # HTTP only listener (for development/testing)
            # Note: open=False prevents CDK from adding 0.0.0.0/0 rule
            alb.add_listener(
                "HttpListener",
                port=80,
                open=False,
                default_target_groups=[web_target_group],
            )
            web_url = f"http://{alb.load_balancer_dns_name}"

        # Create Route53 record if domain provided
        if domain_name and hosted_zone_id:
            # Extract zone name from domain (e.g., orthanc.hda.example.com -> hda.example.com)
            zone_name = ".".join(domain_name.split(".")[1:])
            
            hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
                self,
                "HostedZone",
                hosted_zone_id=hosted_zone_id,
                zone_name=zone_name,
            )
            
            route53.ARecord(
                self,
                "OrthancDnsRecord",
                zone=hosted_zone,
                record_name=domain_name,
                target=route53.RecordTarget.from_alias(
                    route53_targets.LoadBalancerTarget(alb)
                ),
            )
            
            # Use custom domain in URL
            if certificate_arn:
                web_url = f"https://{domain_name}"
            else:
                web_url = f"http://{domain_name}"

        # Fargate Service with Cloud Map for internal service discovery
        namespace = servicediscovery.PrivateDnsNamespace(
            self,
            "OrthancNamespace",
            name=f"{construct_id.lower()}.local",
            vpc=vpc,
        )
        
        service = ecs.FargateService(
            self,
            "OrthancService",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=1,
            security_groups=[ecs_sg],
            # Run tasks in private subnets (no public IP). Outbound access for
            # pulling the Docker Hub image goes through the VPC's NAT gateway.
            # The internet-facing ALB continues to front the service publicly.
            assign_public_ip=False,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            health_check_grace_period=Duration.seconds(120),  # Give container time to start
            cloud_map_options=ecs.CloudMapOptions(
                name="orthanc",
                cloud_map_namespace=namespace,
                dns_record_type=servicediscovery.DnsRecordType.A,
            ),
        )
        
        # Internal URL for Lambda to use (via Cloud Map)
        internal_orthanc_url = f"http://orthanc.{construct_id.lower()}.local:8042"
        
        # Expose internal URL and ECS security group for other stacks (e.g., data loader)
        self.internal_orthanc_url = internal_orthanc_url
        self.ecs_security_group = ecs_sg

        # Register with target group
        service.attach_to_application_target_group(web_target_group)

        # Outputs
        CfnOutput(
            self,
            "OrthancWebURL",
            value=web_url,
            description="Orthanc Web Interface URL",
        )

        CfnOutput(
            self,
            "OrthancCredentials",
            value=(
                "Username: admin. Retrieve the generated password from "
                f"Secrets Manager secret: {self.orthanc_admin_secret.secret_name}"
            ),
            description="How to retrieve Orthanc admin credentials",
        )

        CfnOutput(
            self,
            "OrthancCredentialsSecretArn",
            value=self.orthanc_admin_secret.secret_arn,
            description="ARN of the Secrets Manager secret holding Orthanc admin credentials",
        )
        
        CfnOutput(
            self,
            "AllowedIPRanges",
            value=", ".join(allowed_ip_ranges),
            description="IP ranges allowed to access Orthanc",
        )

        # Store ALB DNS for Lambda
        self.orthanc_url = web_url
        self.orthanc_alb = alb
        self.orthanc_alb_security_group = alb_sg
        self.ecs_security_group = ecs_sg

        # Add HealthLake sync Lambda if datastore ID provided
        if healthlake_datastore_id:
            self._create_healthlake_sync(
                vpc=vpc,
                orthanc_url=internal_orthanc_url,  # Use internal URL for Lambda
                healthlake_datastore_id=healthlake_datastore_id,
                construct_id=construct_id,
                ecs_sg=ecs_sg,  # Pass ECS security group for Lambda access
            )

    def _create_healthlake_sync(self, vpc, orthanc_url, healthlake_datastore_id, construct_id, ecs_sg):
        """Create Lambda for syncing Orthanc to HealthLake."""
        
        # Lambda security group
        lambda_sg = ec2.SecurityGroup(
            self,
            "LambdaSecurityGroup",
            vpc=vpc,
            description="Security group for Orthanc sync Lambda",
            allow_all_outbound=True,
        )
        
        # Allow Lambda to reach ECS tasks on port 8042
        ecs_sg.add_ingress_rule(
            lambda_sg,
            ec2.Port.tcp(8042),
            "Orthanc access from sync Lambda"
        )
        
        # DynamoDB table for sync state - use stack name to avoid conflicts
        sync_state_table = dynamodb.Table(
            self,
            "OrthancSyncStateTable",
            table_name=f"{construct_id.lower()}-healthlake-sync-state",
            partition_key=dynamodb.Attribute(
                name="resource_type",
                type=dynamodb.AttributeType.STRING,
            ),
            removal_policy=RemovalPolicy.DESTROY,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        )

        # Lambda function
        sync_lambda = PythonFunction(
            self,
            "OrthancHealthLakeSyncLambda",
            function_name=f"{construct_id.lower()}-healthlake-sync",
            runtime=lambda_.Runtime.PYTHON_3_11,
            entry="lambda/orthanc_healthlake_sync",
            index="handler.py",
            handler="handler",
            timeout=Duration.minutes(5),
            memory_size=512,
            vpc=vpc,
            security_groups=[lambda_sg],
            environment={
                "ORTHANC_URL": orthanc_url,
                "HEALTHLAKE_ENDPOINT": f"https://healthlake.us-east-1.amazonaws.com/datastore/{healthlake_datastore_id}/r4/",
                "HEALTHLAKE_DATASTORE_ID": healthlake_datastore_id,
                "SYNC_STATE_TABLE": sync_state_table.table_name,
                # Provide the secret ARN so the handler fetches credentials at
                # runtime from Secrets Manager instead of using a default.
                "ORTHANC_CREDENTIALS_SECRET_ARN": self.orthanc_admin_secret.secret_arn,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # Grant DynamoDB access
        sync_state_table.grant_read_write_data(sync_lambda)

        # Allow the sync Lambda to read the Orthanc admin credentials secret
        self.orthanc_admin_secret.grant_read(sync_lambda)

        # Grant HealthLake access
        sync_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "healthlake:CreateResource",
                    "healthlake:UpdateResource",
                    "healthlake:ReadResource",
                    "healthlake:SearchWithGet",
                    "healthlake:SearchWithPost",
                ],
                resources=[
                    f"arn:aws:healthlake:us-east-1:{self.account}:datastore/fhir/{healthlake_datastore_id}",
                ],
            )
        )

        # Grant KMS access for HealthLake encryption
        # HealthLake uses KMS for data encryption, Lambda needs these permissions to write data
        sync_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "kms:GenerateDataKey",
                    "kms:Decrypt",
                    "kms:Encrypt",
                ],
                # Scope to the specific HealthLake CMK when its ARN is known
                # (the default create-new-datastore path always passes it, so the
                # resource is a single key ARN). The wildcard is ONLY a fallback
                # for the bring-your-own-datastore path where the key ARN is not
                # available at synth time, and it remains constrained to
                # HealthLake-initiated calls via the kms:ViaService condition
                # below.
                # SECURITY (least privilege): this key/* wildcard fallback MUST be
                # replaced with the specific HealthLake CMK ARN before production
                # use. Pass healthlake_kms_key_arn (e.g. from the datastore's
                # KmsKeyId) so this policy scopes to that single key ARN.
                resources=[
                    self.healthlake_kms_key_arn if self.healthlake_kms_key_arn
                    else f"arn:aws:kms:us-east-1:{self.account}:key/*",
                ],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"healthlake.us-east-1.amazonaws.com"
                    }
                }
            )
        )

        # EventBridge rule for periodic sync (every 15 minutes)
        sync_rule = events.Rule(
            self,
            "OrthancSyncRule",
            rule_name=f"{construct_id.lower()}-healthlake-sync-schedule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            description="Sync Orthanc imaging studies to HealthLake",
        )
        sync_rule.add_target(targets.LambdaFunction(sync_lambda))

        CfnOutput(
            self,
            "OrthancSyncLambda",
            value=sync_lambda.function_name,
            description="Lambda function for Orthanc to HealthLake sync",
        )
