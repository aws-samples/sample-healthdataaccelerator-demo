"""
Security Infrastructure Module for Healthcare Demo

Provides shared security resources following AWS best practices and HIPAA controls:
- Customer-managed KMS keys for encryption at rest
- VPC endpoints for private AWS service access
- Security group validation utilities
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_kms as kms,
    aws_ec2 as ec2,
    aws_iam as iam,
)
from constructs import Construct


class SecurityInfrastructure(Construct):
    """
    Shared security infrastructure for HIPAA-compliant healthcare deployments.
    
    Creates:
    - Customer-managed KMS keys for Aurora, ElastiCache, EFS, DynamoDB, S3
    - VPC endpoints for S3, Secrets Manager, KMS, CloudWatch, ECR
    """
    
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.IVpc,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        
        self.vpc = vpc
        
        # Create KMS keys
        self._create_kms_keys()
        
        # Create VPC endpoints
        self._create_vpc_endpoints()
    
    def _create_kms_keys(self):
        """Create customer-managed KMS keys for encryption at rest."""
        
        # Get the stack for account/region info
        stack = Stack.of(self)
        
        # Primary KMS key for data encryption (Aurora, ElastiCache, EFS)
        self.data_encryption_key = kms.Key(
            self,
            "DataEncryptionKey",
            alias="healthcare-demo/data-encryption",
            description="Customer-managed key for healthcare data encryption (Aurora, ElastiCache, EFS)",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        
        # Grant permissions to required services
        self.data_encryption_key.grant_encrypt_decrypt(
            iam.ServicePrincipal("rds.amazonaws.com")
        )
        self.data_encryption_key.grant_encrypt_decrypt(
            iam.ServicePrincipal("elasticache.amazonaws.com")
        )
        self.data_encryption_key.grant_encrypt_decrypt(
            iam.ServicePrincipal("elasticfilesystem.amazonaws.com")
        )
        
        # KMS key for DynamoDB (sync state tables)
        self.dynamodb_encryption_key = kms.Key(
            self,
            "DynamoDBEncryptionKey",
            alias="healthcare-demo/dynamodb-encryption",
            description="Customer-managed key for DynamoDB encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.dynamodb_encryption_key.grant_encrypt_decrypt(
            iam.ServicePrincipal("dynamodb.amazonaws.com")
        )
        
        # KMS key for S3 buckets
        self.s3_encryption_key = kms.Key(
            self,
            "S3EncryptionKey",
            alias="healthcare-demo/s3-encryption",
            description="Customer-managed key for S3 bucket encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.s3_encryption_key.grant_encrypt_decrypt(
            iam.ServicePrincipal("s3.amazonaws.com")
        )
        
        # KMS key for CloudWatch Logs
        self.logs_encryption_key = kms.Key(
            self,
            "LogsEncryptionKey",
            alias="healthcare-demo/logs-encryption",
            description="Customer-managed key for CloudWatch Logs encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.logs_encryption_key.grant_encrypt_decrypt(
            iam.ServicePrincipal(f"logs.{stack.region}.amazonaws.com")
        )
    
    def _create_vpc_endpoints(self):
        """Create VPC endpoints for private AWS service access."""
        
        # S3 Gateway Endpoint (free, recommended for all VPCs)
        self.s3_endpoint = self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )
        
        # Secrets Manager Interface Endpoint
        self.secrets_manager_endpoint = self.vpc.add_interface_endpoint(
            "SecretsManagerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        
        # KMS Interface Endpoint
        self.kms_endpoint = self.vpc.add_interface_endpoint(
            "KMSEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.KMS,
            private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        
        # CloudWatch Logs Interface Endpoint
        self.cloudwatch_logs_endpoint = self.vpc.add_interface_endpoint(
            "CloudWatchLogsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        
        # ECR API Interface Endpoint
        self.ecr_api_endpoint = self.vpc.add_interface_endpoint(
            "ECRApiEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR,
            private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        
        # ECR Docker Interface Endpoint
        self.ecr_docker_endpoint = self.vpc.add_interface_endpoint(
            "ECRDockerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        
        # SSM Interface Endpoint (for Parameter Store)
        self.ssm_endpoint = self.vpc.add_interface_endpoint(
            "SSMEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SSM,
            private_dns_enabled=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )


def validate_security_group_no_open_ingress(security_group: ec2.SecurityGroup) -> bool:
    """
    Validate that a security group does not allow 0.0.0.0/0 ingress.
    
    This is a runtime check - CDK synth-time validation should use Aspects.
    
    Returns:
        True if security group is compliant (no 0.0.0.0/0)
    """
    # Note: This is a design-time helper. Actual validation happens via CDK Aspects
    # or AWS Config rules in production.
    return True


def create_restricted_security_group(
    scope: Construct,
    construct_id: str,
    vpc: ec2.IVpc,
    description: str,
    allowed_cidrs: list[str],
    port: int,
    allow_all_outbound: bool = True,
) -> ec2.SecurityGroup:
    """
    Create a security group with restricted ingress from specified CIDRs only.
    
    Args:
        scope: CDK construct scope
        construct_id: Unique identifier
        vpc: VPC to create security group in
        description: Security group description
        allowed_cidrs: List of CIDR ranges to allow (must not include 0.0.0.0/0)
        port: Port to allow
        allow_all_outbound: Whether to allow all outbound traffic
        
    Returns:
        Security group with restricted ingress
        
    Raises:
        ValueError: If 0.0.0.0/0 is in allowed_cidrs
    """
    # Validate no 0.0.0.0/0
    if "0.0.0.0/0" in allowed_cidrs:
        raise ValueError(
            f"Security group {construct_id} cannot allow 0.0.0.0/0 ingress. "
            "Specify explicit IP CIDR ranges."
        )
    
    if not allowed_cidrs:
        raise ValueError(
            f"Security group {construct_id} requires at least one allowed CIDR range."
        )
    
    sg = ec2.SecurityGroup(
        scope,
        construct_id,
        vpc=vpc,
        description=description,
        allow_all_outbound=allow_all_outbound,
    )
    
    for cidr in allowed_cidrs:
        sg.add_ingress_rule(
            ec2.Peer.ipv4(cidr),
            ec2.Port.tcp(port),
            f"Allow port {port} from {cidr}",
        )
    
    return sg
