"""
CDK Stack that provisions a self-signed TLS certificate in AWS Certificate
Manager (ACM) for use by the OpenEMR and Orthanc Application Load Balancers when
no Route 53 domain / publicly-validated ACM certificate is available.

Public ACM cannot *issue* a certificate without domain validation, and an ALB
HTTPS listener can only reference a certificate by ARN. This stack runs a
Lambda-backed custom resource that generates a self-signed certificate and
*imports* it into ACM (import needs no validation), exposing the resulting ARN
as `certificate_arn` for downstream stacks.

The certificate is NOT publicly trusted - clients will see a trust warning.
This is intended only for demo / sandbox deployments (synthetic data only).
Enable via `-c self_signed_tls=true` when deploying without a domain.
"""

from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_iam as iam,
    aws_logs as logs,
    custom_resources as cr,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from aws_cdk import aws_lambda as lambda_
from constructs import Construct


class SelfSignedCertStack(Stack):
    """Generate a self-signed certificate and import it into ACM."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        common_name: str = "healthdata-accelerator.local",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Lambda that generates the cert and imports/deletes it in ACM.
        cert_lambda = PythonFunction(
            self,
            "SelfSignedCertFn",
            entry="lambda/self_signed_cert",
            runtime=lambda_.Runtime.PYTHON_3_12,
            index="index.py",
            handler="handler",
            timeout=Duration.minutes(2),
            memory_size=256,
        )

        # Least-privilege: import/delete/describe ACM certs. ACM import does not
        # support resource-level scoping on ImportCertificate for *new* certs,
        # so the import action uses "*"; delete/describe are scoped to certs in
        # this account/region.
        cert_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["acm:ImportCertificate"],
                resources=["*"],
            )
        )
        cert_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "acm:DeleteCertificate",
                    "acm:DescribeCertificate",
                    "acm:AddTagsToCertificate",
                ],
                resources=[f"arn:aws:acm:{self.region}:{self.account}:certificate/*"],
            )
        )

        provider = cr.Provider(
            self,
            "SelfSignedCertProvider",
            on_event_handler=cert_lambda,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        from aws_cdk import CustomResource

        cert_resource = CustomResource(
            self,
            "SelfSignedCert",
            service_token=provider.service_token,
            properties={"CommonName": common_name},
        )

        # ARN downstream stacks consume for their ALB HTTPS listeners.
        self.certificate_arn = cert_resource.get_att_string("CertificateArn")

        CfnOutput(
            self,
            "SelfSignedCertificateArn",
            value=self.certificate_arn,
            description="ACM ARN of the imported self-signed certificate (untrusted; demo only)",
        )
