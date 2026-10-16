"""
Custom-resource handler (CDK custom_resources.Provider framework) that generates
a self-signed X.509 certificate and imports it into AWS Certificate Manager
(ACM), returning the resulting certificate ARN.

Why this exists:
  Public ACM cannot *issue* a certificate without domain (DNS/email) validation,
  and an ALB HTTPS listener can only reference a certificate by ACM/IAM ARN (it
  cannot read a raw PEM). To offer HTTPS on the OpenEMR and Orthanc ALBs when no
  Route 53 domain is available, we generate a self-signed certificate here and
  *import* it into ACM (import requires no validation). The certificate is not
  publicly trusted, so clients will see a trust warning - acceptable for a
  demo / sandbox that already ships with synthetic data only.

Provider framework contract (onEvent):
  Create : generate key+cert, import into ACM, return PhysicalResourceId=ARN.
  Update : generate a fresh key+cert, re-import into the SAME ACM cert (stable ARN).
  Delete : delete the ACM certificate.

Imported certificates are NOT auto-renewed by ACM; re-deploy before expiry.
"""

import datetime
import boto3
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

acm = boto3.client("acm")

# 825 days is the CA/Browser Forum maximum many TLS stacks accept; fine for a
# self-signed demo cert.
VALIDITY_DAYS = 825


def _generate_self_signed(common_name):
    """Return (cert_pem_bytes, key_pem_bytes) for a fresh self-signed RSA-2048 cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HealthDataAccelerator Sample"),
    ])

    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def handler(event, context):
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {}) or {}
    common_name = props.get("CommonName", "healthdata-accelerator.local")

    if request_type == "Delete":
        arn = event.get("PhysicalResourceId", "")
        if arn.startswith("arn:aws:acm:"):
            try:
                acm.delete_certificate(CertificateArn=arn)
            except (acm.exceptions.ResourceNotFoundException,
                    acm.exceptions.ResourceInUseException):
                # Not found -> already gone. In use -> the ALB listener in the
                # same stack is torn down alongside this; report success so the
                # delete can proceed.
                pass
        return {"PhysicalResourceId": arn or "no-cert"}

    # Create or Update: generate a fresh cert and (re)import.
    cert_pem, key_pem = _generate_self_signed(common_name)

    import_args = {"Certificate": cert_pem, "PrivateKey": key_pem}
    if request_type == "Update":
        existing = event.get("PhysicalResourceId", "")
        if existing.startswith("arn:aws:acm:"):
            import_args["CertificateArn"] = existing

    resp = acm.import_certificate(**import_args)
    arn = resp["CertificateArn"]
    return {
        "PhysicalResourceId": arn,
        "Data": {"CertificateArn": arn},
    }
