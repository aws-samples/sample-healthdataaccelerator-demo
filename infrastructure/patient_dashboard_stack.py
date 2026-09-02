"""
CDK Stack for Patient 360 Dashboard

Deploys:
- Cognito User Pool for authentication
- S3 bucket for static website hosting
- CloudFront distribution
- API Gateway + Lambda for HealthLake proxy (in VPC for Orthanc access)

PHI / HIPAA NOTICE:
This stack exposes protected health information (PHI) - patient records read
from AWS HealthLake - through an API and web dashboard. If you process real PHI,
this is a HIPAA-regulated workload: execute an AWS Business Associate Addendum
(BAA), keep data within HIPAA-eligible services, and enable encryption, access
logging, and audit controls. The customer is responsible for compliant handling
of regulated data. This sample ships with synthetic data only.
"""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    CustomResource,
    aws_s3 as s3,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_cognito as cognito,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_ec2 as ec2,
    aws_s3_deployment as s3deploy,
    aws_secretsmanager as secretsmanager,
    aws_kms as kms,
    aws_bedrock as bedrock,
    custom_resources as cr,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from aws_cdk import aws_lambda as lambda_
from constructs import Construct
from typing import Optional


class PatientDashboardStack(Stack):
    """Patient 360 Dashboard Stack."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        healthlake_datastore_id: str,
        orthanc_url: str,
        vpc: Optional[ec2.IVpc] = None,
        domain_name: Optional[str] = None,
        certificate_arn: Optional[str] = None,
        hosted_zone_id: Optional[str] = None,
        orthanc_credentials_secret: Optional[secretsmanager.ISecret] = None,
        enable_bedrock_guardrails: bool = True,
        healthlake_kms_key_arn: Optional[str] = None,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # HealthLake endpoint
        healthlake_endpoint = f"https://healthlake.us-east-1.amazonaws.com/datastore/{healthlake_datastore_id}/r4/"

        # ------------------------------------------------------------------
        # Responsible AI: Amazon Bedrock Guardrail for the patient summary /
        # chat features. This is a defense-in-depth control layered on top of
        # the strict "facts only" system prompts in the Lambda:
        #  - Denied topics block the model from producing medical advice,
        #    diagnoses, or treatment recommendations (the app is documentation,
        #    not clinical decision support).
        #  - Content filters block hate/insults/sexual/violence/misconduct.
        #  - A prompt-attack filter mitigates jailbreak / prompt-injection.
        # Note: we intentionally do NOT enable PII anonymization here. The
        # dashboard's purpose is to show an authenticated clinician the
        # patient's own record, so names/DOB in a summary are expected; the
        # data is protected by Cognito auth + IAM, not by stripping it.
        self.bedrock_guardrail = None
        self.bedrock_guardrail_version = None
        if enable_bedrock_guardrails:
            self.bedrock_guardrail = bedrock.CfnGuardrail(
                self,
                "PatientAiGuardrail",
                name=f"{construct_id.lower()}-patient-ai-guardrail",
                description=(
                    "Responsible-AI guardrail for the Patient 360 dashboard "
                    "summary/chat features. Blocks medical advice and harmful "
                    "content; mitigates prompt injection."
                ),
                blocked_input_messaging=(
                    "This request can't be processed. The assistant only "
                    "reports documented facts from the patient record and "
                    "cannot provide medical advice."
                ),
                blocked_outputs_messaging=(
                    "The response was blocked. The assistant only reports "
                    "documented facts from the patient record and cannot "
                    "provide medical advice, diagnoses, or treatment guidance. "
                    "Please consult a qualified clinician."
                ),
                content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                    filters_config=[
                        bedrock.CfnGuardrail.ContentFilterConfigProperty(
                            type=t, input_strength="HIGH", output_strength="HIGH"
                        )
                        for t in ["HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"]
                    ] + [
                        # Prompt-attack filtering only applies to input.
                        bedrock.CfnGuardrail.ContentFilterConfigProperty(
                            type="PROMPT_ATTACK",
                            input_strength="HIGH",
                            output_strength="NONE",
                        )
                    ]
                ),
                topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                    topics_config=[
                        bedrock.CfnGuardrail.TopicConfigProperty(
                            name="MedicalAdvice",
                            type="DENY",
                            definition=(
                                "Providing medical advice, diagnoses, prognoses, "
                                "treatment plans, medication guidance, or any "
                                "clinical interpretation or recommendation."
                            ),
                            examples=[
                                "What should this patient be treated with?",
                                "Does this patient have cancer?",
                                "What medication should they take?",
                                "Is this test result dangerous?",
                                "What's your diagnosis?",
                            ],
                        )
                    ]
                ),
            )

            # An immutable, deployable version of the guardrail.
            self.bedrock_guardrail_version = bedrock.CfnGuardrailVersion(
                self,
                "PatientAiGuardrailVersion",
                guardrail_identifier=self.bedrock_guardrail.attr_guardrail_id,
                description="Initial deployed version",
            )

        # Create security group for Lambda if VPC provided
        lambda_security_group = None
        if vpc:
            # Create security group for Lambda
            lambda_security_group = ec2.SecurityGroup(
                self,
                "LambdaSG",
                vpc=vpc,
                description="Security group for Dashboard API Lambda",
                allow_all_outbound=True,
            )

        # Cognito User Pool for authentication
        user_pool = cognito.UserPool(
            self,
            "DashboardUserPool",
            user_pool_name=f"{construct_id}-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Cognito App Client (for frontend auth)
        user_pool_client = user_pool.add_client(
            "DashboardAppClient",
            user_pool_client_name=f"{construct_id}-client",
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            generate_secret=False,
        )

        # Lambda for HealthLake API proxy - in VPC if provided
        lambda_props = {
            "function_name": f"{construct_id.lower()}-healthlake-api",
            "runtime": lambda_.Runtime.PYTHON_3_11,
            "entry": "lambda/healthlake_api",
            "index": "handler.py",
            "handler": "handler",
            "timeout": Duration.seconds(30),
            "memory_size": 256,
            "environment": {
                "HEALTHLAKE_ENDPOINT": healthlake_endpoint,
                "HEALTHLAKE_DATASTORE_ID": healthlake_datastore_id,
                "ORTHANC_URL": orthanc_url,
                # Orthanc admin credentials come from Secrets Manager, not a
                # hardcoded default. Empty when no secret is provided.
                "ORTHANC_CREDENTIALS_SECRET_ARN": (
                    orthanc_credentials_secret.secret_arn
                    if orthanc_credentials_secret else ""
                ),
                # Bedrock Guardrail applied to the AI summary/chat features.
                # Empty when guardrails are disabled (handler skips them).
                "BEDROCK_GUARDRAIL_ID": (
                    self.bedrock_guardrail.attr_guardrail_id
                    if self.bedrock_guardrail else ""
                ),
                "BEDROCK_GUARDRAIL_VERSION": (
                    self.bedrock_guardrail_version.attr_version
                    if self.bedrock_guardrail_version else ""
                ),
            },
        }
        
        if vpc and lambda_security_group:
            lambda_props["vpc"] = vpc
            lambda_props["vpc_subnets"] = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
            lambda_props["security_groups"] = [lambda_security_group]

        api_lambda = PythonFunction(self, "HealthLakeApiLambda", **lambda_props)

        # Grant the API Lambda read access to the Orthanc admin credentials secret
        if orthanc_credentials_secret:
            orthanc_credentials_secret.grant_read(api_lambda)

        # Grant HealthLake read access
        api_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "healthlake:ReadResource",
                    "healthlake:SearchWithGet",
                    "healthlake:SearchWithPost",
                ],
                resources=[
                    f"arn:aws:healthlake:us-east-1:{self.account}:datastore/fhir/{healthlake_datastore_id}",
                ],
            )
        )

        # Grant KMS access for HealthLake datastore encryption key
        api_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                ],
                # Scope to the specific HealthLake CMK when known. The wildcard
                # fallback only applies when reusing an existing datastore whose
                # key ARN isn't available here, and it stays constrained to
                # HealthLake usage via the kms:ViaService condition below.
                resources=[
                    healthlake_kms_key_arn if healthlake_kms_key_arn
                    else f"arn:aws:kms:us-east-1:{self.account}:key/*",
                ],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"healthlake.us-east-1.amazonaws.com"
                    }
                }
            )
        )

        # Grant Bedrock access for AI summaries and chat (Amazon Nova)
        api_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock:InvokeModel",
                ],
                resources=[
                    "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0",
                ],
            )
        )

        # Grant permission to apply the Responsible-AI guardrail at inference
        # time, scoped to this stack's guardrail resource only.
        if self.bedrock_guardrail:
            api_lambda.add_to_role_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[self.bedrock_guardrail.attr_guardrail_arn],
                )
            )

        # Cognito Authorizer for API Gateway
        cognito_authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "CognitoAuthorizer",
            cognito_user_pools=[user_pool],
            authorizer_name=f"{construct_id}-authorizer",
        )

        # API Gateway
        # Restrict CORS to the dashboard's own origin when a custom domain is
        # configured. Falls back to allowing all origins only when no domain is
        # provided (e.g. quick local demos). Overridable via
        # 'dashboard_allowed_origins' CDK context (comma-separated).
        context_origins = self.node.try_get_context("dashboard_allowed_origins")
        if context_origins:
            allowed_origins = [o.strip() for o in context_origins.split(",") if o.strip()]
        elif domain_name:
            allowed_origins = [f"https://{domain_name}"]
        else:
            allowed_origins = apigw.Cors.ALL_ORIGINS

        api = apigw.RestApi(
            self,
            "PatientDashboardApi",
            rest_api_name=f"{construct_id}-api",
            description="API for Patient Dashboard",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=allowed_origins,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "Authorization"],
            ),
        )

        # Lambda integration
        lambda_integration = apigw.LambdaIntegration(api_lambda)

        # API routes (all protected by Cognito)
        patients = api.root.add_resource("patients")
        patients.add_method(
            "GET", lambda_integration,
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorizer=cognito_authorizer,
        )

        patient = patients.add_resource("{patientId}")
        patient.add_method(
            "GET", lambda_integration,
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorizer=cognito_authorizer,
        )

        # Patient sub-resources
        for resource_name in ["conditions", "allergies", "medications", "encounters", "immunizations", "notes"]:
            resource = patient.add_resource(resource_name)
            resource.add_method(
                "GET", lambda_integration,
                authorization_type=apigw.AuthorizationType.COGNITO,
                authorizer=cognito_authorizer,
            )

        # Imaging endpoint
        imaging = api.root.add_resource("imaging")
        imaging.add_method(
            "GET", lambda_integration,
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorizer=cognito_authorizer,
        )

        # Orthanc imaging proxy endpoint
        orthanc_imaging = api.root.add_resource("orthanc-imaging")
        orthanc_imaging.add_method(
            "GET", lambda_integration,
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorizer=cognito_authorizer,
        )

        # AI Summary endpoint
        patient_summary = api.root.add_resource("patient-summary")
        patient_summary.add_method(
            "POST", lambda_integration,
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorizer=cognito_authorizer,
        )

        # AI Chat endpoint
        patient_chat = api.root.add_resource("patient-chat")
        patient_chat.add_method(
            "POST", lambda_integration,
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorizer=cognito_authorizer,
        )

        # S3 bucket for static website
        website_bucket = s3.Bucket(
            self,
            "WebsiteBucket",
            bucket_name=f"{construct_id.lower()}-website-{self.account}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,          # deny non-TLS requests (bucket policy)
            versioned=True,            # retain prior object versions
        )

        # Origin Access Identity for CloudFront
        oai = cloudfront.OriginAccessIdentity(
            self,
            "OAI",
            comment=f"OAI for {construct_id}",
        )
        website_bucket.grant_read(oai)

        # CloudFront distribution
        if certificate_arn and domain_name:
            certificate = acm.Certificate.from_certificate_arn(
                self, "Certificate", certificate_arn
            )
            distribution = cloudfront.Distribution(
                self,
                "Distribution",
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.S3Origin(website_bucket, origin_access_identity=oai),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                ),
                default_root_object="index.html",
                error_responses=[
                    cloudfront.ErrorResponse(
                        http_status=404,
                        response_http_status=200,
                        response_page_path="/index.html",
                    ),
                ],
                domain_names=[domain_name],
                certificate=certificate,
            )
        else:
            distribution = cloudfront.Distribution(
                self,
                "Distribution",
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.S3Origin(website_bucket, origin_access_identity=oai),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                ),
                default_root_object="index.html",
                error_responses=[
                    cloudfront.ErrorResponse(
                        http_status=404,
                        response_http_status=200,
                        response_page_path="/index.html",
                    ),
                ],
            )

        # Route53 record if domain provided
        if domain_name and hosted_zone_id:
            zone_name = ".".join(domain_name.split(".")[1:])
            hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
                self,
                "HostedZone",
                hosted_zone_id=hosted_zone_id,
                zone_name=zone_name,
            )
            route53.ARecord(
                self,
                "DashboardDnsRecord",
                zone=hosted_zone,
                record_name=domain_name,
                target=route53.RecordTarget.from_alias(
                    route53_targets.CloudFrontTarget(distribution)
                ),
            )

        # Deploy static website files from patient-dashboard/dist
        # This deploys the built React app
        website_deployment = s3deploy.BucketDeployment(
            self,
            "WebsiteDeployment",
            sources=[s3deploy.Source.asset("patient-dashboard/dist")],
            destination_bucket=website_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            # Exclude config.js - we'll generate it separately with correct values
            exclude=["config.js"],
        )

        # Generate and upload config.js with correct Cognito values
        # Using AwsCustomResource to put the config file with dynamic values
        config_content = f"""// Configuration - Auto-generated by CDK
window.API_BASE_URL = '{api.url}';
window.ORTHANC_URL = '{orthanc_url}';
window.COGNITO_CONFIG = {{
  region: 'us-east-1',
  userPoolId: '{user_pool.user_pool_id}',
  clientId: '{user_pool_client.user_pool_client_id}'
}};
"""
        
        config_upload = cr.AwsCustomResource(
            self,
            "ConfigUpload",
            on_create=cr.AwsSdkCall(
                service="S3",
                action="putObject",
                parameters={
                    "Bucket": website_bucket.bucket_name,
                    "Key": "config.js",
                    "Body": config_content,
                    "ContentType": "application/javascript",
                },
                physical_resource_id=cr.PhysicalResourceId.of("config-js-upload"),
            ),
            on_update=cr.AwsSdkCall(
                service="S3",
                action="putObject",
                parameters={
                    "Bucket": website_bucket.bucket_name,
                    "Key": "config.js",
                    "Body": config_content,
                    "ContentType": "application/javascript",
                },
                physical_resource_id=cr.PhysicalResourceId.of("config-js-upload"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=["s3:PutObject"],
                    resources=[f"{website_bucket.bucket_arn}/config.js"],
                )
            ]),
        )
        config_upload.node.add_dependency(website_deployment)

        # Customer-managed KMS key for encrypting the admin credentials secret
        # (satisfies CDK Nag AwsSolutions-SMG4 / HIPAA SecretsManagerUsingKMSKey).
        dashboard_secret_key = kms.Key(
            self,
            "DashboardAdminSecretKey",
            description="CMK for Patient 360 Dashboard admin credentials secret",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create secret for dashboard admin credentials
        dashboard_admin_secret = secretsmanager.Secret(
            self,
            "DashboardAdminSecret",
            secret_name=f"{construct_id}/admin-credentials",
            description="Patient 360 Dashboard admin credentials",
            encryption_key=dashboard_secret_key,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                # nosec B106 - not a password: this is a JSON template for a
                # Secrets Manager auto-generated secret. The password value is
                # produced by AWS via generate_string_key, never hardcoded.
                secret_string_template='{"username": "admin"}',
                generate_string_key="password",
                exclude_punctuation=True,
                password_length=16,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Lambda to create Cognito user with password from Secrets Manager
        create_user_lambda = lambda_.Function(
            self,
            "CreateUserLambda",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_inline('''
import boto3
import cfnresponse
import json

def handler(event, context):
    if event["RequestType"] == "Delete":
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
        return
    
    try:
        secrets = boto3.client("secretsmanager")
        cognito = boto3.client("cognito-idp")
        
        secret_arn = event["ResourceProperties"]["SecretArn"]
        user_pool_id = event["ResourceProperties"]["UserPoolId"]
        
        # Get password from Secrets Manager
        secret_value = secrets.get_secret_value(SecretId=secret_arn)
        secret_data = json.loads(secret_value["SecretString"])
        username = secret_data["username"]
        password = secret_data["password"]
        
        # Create user
        try:
            cognito.admin_create_user(
                UserPoolId=user_pool_id,
                Username=username,
                TemporaryPassword=password,
                UserAttributes=[
                    {"Name": "email", "Value": "admin@example.com"},
                    {"Name": "email_verified", "Value": "true"},
                ],
                MessageAction="SUPPRESS",
            )
        except cognito.exceptions.UsernameExistsException:
            pass  # User already exists, continue to set password
        
        # Set permanent password
        cognito.admin_set_user_password(
            UserPoolId=user_pool_id,
            Username=username,
            Password=password,
            Permanent=True,
        )
        
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Username": username})
    except Exception as e:
        # Log/return only the exception type, not the full message: Cognito
        # errors can echo back the submitted password or user attributes.
        err_type = type(e).__name__
        print(f"Error creating dashboard admin user: {err_type}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Error": err_type})
'''),
            timeout=Duration.seconds(60),
        )

        # Grant permissions
        dashboard_admin_secret.grant_read(create_user_lambda)
        create_user_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                ],
                resources=[user_pool.user_pool_arn],
            )
        )

        # Custom resource to trigger user creation
        create_user_provider = cr.Provider(
            self,
            "CreateUserProvider",
            on_event_handler=create_user_lambda,
        )

        CustomResource(
            self,
            "CreateDefaultUser",
            service_token=create_user_provider.service_token,
            properties={
                "SecretArn": dashboard_admin_secret.secret_arn,
                "UserPoolId": user_pool.user_pool_id,
            },
        )

        # Outputs
        CfnOutput(
            self,
            "DashboardURL",
            value=f"https://{domain_name}" if domain_name else f"https://{distribution.distribution_domain_name}",
            description="Patient Dashboard URL",
        )

        CfnOutput(
            self,
            "ApiURL",
            value=api.url,
            description="API Gateway URL",
        )

        CfnOutput(
            self,
            "UserPoolId",
            value=user_pool.user_pool_id,
            description="Cognito User Pool ID",
        )

        CfnOutput(
            self,
            "UserPoolClientId",
            value=user_pool_client.user_pool_client_id,
            description="Cognito App Client ID",
        )

        CfnOutput(
            self,
            "WebsiteBucketName",
            value=website_bucket.bucket_name,
            description="S3 bucket for website deployment",
        )

        CfnOutput(
            self,
            "DistributionId",
            value=distribution.distribution_id,
            description="CloudFront distribution ID",
        )

        CfnOutput(
            self,
            "AdminCredentialsSecret",
            value=dashboard_admin_secret.secret_arn,
            description="Secrets Manager ARN for dashboard admin credentials",
        )
        
        if lambda_security_group:
            CfnOutput(
                self,
                "LambdaSecurityGroupId",
                value=lambda_security_group.security_group_id,
                description="Lambda Security Group ID - add to Orthanc ALB SG",
            )

        # Store for deployment script
        self.website_bucket = website_bucket
        self.distribution = distribution
        self.api_url = api.url
        self.orthanc_url = orthanc_url
        self.user_pool = user_pool
        self.user_pool_client = user_pool_client
        self.lambda_security_group = lambda_security_group
