"""Network stack for OpenEMR infrastructure."""

from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_logs as logs,
    aws_iam as iam,
    CfnOutput
)
from constructs import Construct


class NetworkStack(Stack):
    """Network infrastructure stack - VPC, subnets, security groups."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        """Initialize the network stack."""
        super().__init__(scope, construct_id, **kwargs)

        # VPC CIDR
        self.cidr = "10.0.0.0/16"

        # Create VPC
        self._create_vpc()
        
        # Create security groups
        self._create_security_groups()

        # Outputs
        self._create_outputs()

    def _create_vpc(self):
        """Create the VPC and networking foundation."""
        vpc_flow_role = iam.Role(
            self, 'Flow-Log-Role',
            assumed_by=iam.ServicePrincipal('vpc-flow-logs.amazonaws.com')
        )

        vpc_log_group = logs.LogGroup(
            self,
            'VPC-Log-Group',
        )

        self.vpc = ec2.Vpc(
            self,
            "OpenEmr-Vpc",
            ip_addresses=ec2.IpAddresses.cidr(self.cidr),
            max_azs=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="private-subnet",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                ec2.SubnetConfiguration(
                    name="public-subnet",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    map_public_ip_on_launch=False
                )
            ]
        )

        ec2.CfnFlowLog(
            self, 'FlowLogs',
            resource_id=self.vpc.vpc_id,
            resource_type='VPC',
            traffic_type='ALL',
            deliver_logs_permission_arn=vpc_flow_role.role_arn,
            log_destination_type='cloud-watch-logs',
            log_group_name=vpc_log_group.log_group_name
        )

    def _create_security_groups(self):
        """Create security groups for different components."""
        # Database security group
        self.db_sec_group = ec2.SecurityGroup(
            self,
            "db-sec-group",
            vpc=self.vpc,
            allow_all_outbound=False,
            description="Security group for RDS database"
        )

        # Cache security group
        self.cache_sec_group = ec2.SecurityGroup(
            self,
            "cache-sec-group",
            vpc=self.vpc,
            allow_all_outbound=False,
            description="Security group for Redis/Valkey cache"
        )

        # Load balancer security group
        self.lb_sec_group = ec2.SecurityGroup(
            self,
            "lb-sec-group",
            vpc=self.vpc,
            allow_all_outbound=False,
            description="Security group for Application Load Balancer"
        )

        # Configure load balancer security group based on context
        if self.node.try_get_context("certificate_arn") or self.node.try_get_context("route53_domain"):
            # HTTPS configuration
            cidr_ipv4 = self.node.try_get_context("security_group_ip_range_ipv4")
            if cidr_ipv4:
                self.lb_sec_group.add_ingress_rule(
                    ec2.Peer.ipv4(cidr_ipv4),
                    ec2.Port.tcp(443),
                )
                self.lb_sec_group.add_egress_rule(
                    ec2.Peer.ipv4(cidr_ipv4),
                    ec2.Port.tcp(443),
                )
        else:
            # HTTP configuration
            cidr_ipv4 = self.node.try_get_context("security_group_ip_range_ipv4")
            if cidr_ipv4:
                self.lb_sec_group.add_ingress_rule(
                    ec2.Peer.ipv4(cidr_ipv4),
                    ec2.Port.tcp(80),
                )
                self.lb_sec_group.add_egress_rule(
                    ec2.Peer.ipv4(cidr_ipv4),
                    ec2.Port.tcp(80),
                )

        # EFS security group
        self.efs_sec_group = ec2.SecurityGroup(
            self,
            "efs-sec-group",
            vpc=self.vpc,
            allow_all_outbound=False,
            description="Security group for EFS file systems"
        )

    def _create_outputs(self):
        """Create stack outputs for other stacks to reference."""
        CfnOutput(
            self, "VpcId",
            value=self.vpc.vpc_id,
            export_name="OpenEMR-VpcId"
        )

        CfnOutput(
            self, "PrivateSubnetIds",
            value=",".join([subnet.subnet_id for subnet in self.vpc.private_subnets]),
            export_name="OpenEMR-PrivateSubnetIds"
        )

        CfnOutput(
            self, "PublicSubnetIds",
            value=",".join([subnet.subnet_id for subnet in self.vpc.public_subnets]),
            export_name="OpenEMR-PublicSubnetIds"
        )

        CfnOutput(
            self, "DbSecurityGroupId",
            value=self.db_sec_group.security_group_id,
            export_name="OpenEMR-DbSecurityGroupId"
        )

        CfnOutput(
            self, "CacheSecurityGroupId",
            value=self.cache_sec_group.security_group_id,
            export_name="OpenEMR-CacheSecurityGroupId"
        )

        CfnOutput(
            self, "LbSecurityGroupId",
            value=self.lb_sec_group.security_group_id,
            export_name="OpenEMR-LbSecurityGroupId"
        )

        CfnOutput(
            self, "EfsSecurityGroupId",
            value=self.efs_sec_group.security_group_id,
            export_name="OpenEMR-EfsSecurityGroupId"
        )