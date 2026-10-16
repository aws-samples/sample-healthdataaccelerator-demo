"""
OpenEMR OAuth credential provisioner (CloudFormation custom resource / TriggerFunction).

Runs after OpenEMR is healthy and API-enabled and, idempotently:
  (a) ensures the OpenEMR API globals are enabled in the MySQL DB
      (rest_api, rest_fhir_api, oauth_password_grant all set to '1'), so the
      FHIR API serves and the ROPC/password grant works even on an
      already-initialized instance that ignored its first-boot env vars,
  (b) registers an OAuth client (application_type=private, client_secret_post),
  (c) enables that client in the OpenEMR MySQL DB (oauth_clients.is_enabled=1),
  (d) confirms an API-capable user for the ROPC/password grant (reuses the
      OpenEMR admin user), and
  (e) writes {client_id, client_secret, username, password} into the
      openemr-healthlake-sync/credentials secret.

Design: .agents/tasks/openemr-sync-design.md section 3.4. Never logs secret
or password values; logs posture/status only.
"""

import json
import os
import ssl
import time

import boto3
import pymysql
import requests

# ----------------------------------------------------------------------------
# Configuration (from env)
# ----------------------------------------------------------------------------
OPENEMR_BASE_URL = os.environ.get("OPENEMR_BASE_URL")
OPENEMR_CREDENTIALS_SECRET = os.environ.get("OPENEMR_CREDENTIALS_SECRET")
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN")
OPENEMR_ADMIN_SECRET_ARN = os.environ.get("OPENEMR_ADMIN_SECRET_ARN")
RDS_CA_BUNDLE = os.environ.get("RDS_CA_BUNDLE", "/var/task/global-bundle.pem")

# INTERNAL IN-VPC HOP ONLY: OpenEMR presents a self-signed CN=localhost cert on
# its container port, reached via a Cloud Map private DNS name. This opt-out is
# never used for any public/internet call. Mirrors the sync handler resolver.
_ssl_env = os.environ.get("OPENEMR_SSL_VERIFY", "true")
if isinstance(_ssl_env, str) and _ssl_env.strip().lower() in {"false", "0", "no", "off"}:
    OPENEMR_SSL_VERIFY = False
elif isinstance(_ssl_env, str) and os.path.exists(_ssl_env):
    OPENEMR_SSL_VERIFY = _ssl_env
else:
    OPENEMR_SSL_VERIFY = True

# FHIR resource types the sync handler reads. Keep in sync with
# lambda/openemr_healthlake_sync/handler.py RESOURCE_TYPES so the registered
# scope stays a strict superset of the handler's token-request scopes.
RESOURCE_TYPES = [
    "Patient",
    "Encounter",
    "Condition",
    "AllergyIntolerance",
    "MedicationRequest",
    "Immunization",
    "Observation",
    "Procedure",
    "DiagnosticReport",
]

# Registered scope: openid offline_access api:fhir + user/<Resource>.read for
# each resource type. A superset of the handler's token scopes (handler omits
# offline_access), which OpenEMR requires at registration time.
REGISTRATION_SCOPE = " ".join(
    ["openid", "offline_access", "api:fhir"]
    + [f"user/{rt}.read" for rt in RESOURCE_TYPES]
)
# Token self-test scope matches what the sync handler requests at token time.
TOKEN_SCOPE = " ".join(
    ["openid", "api:fhir"] + [f"user/{rt}.read" for rt in RESOURCE_TYPES]
)

# Readiness probe limits (design 3.4).
READINESS_TIMEOUT_SECONDS = 8 * 60
READINESS_BACKOFF_SECONDS = 15

secrets_client = boto3.client("secretsmanager")


# ----------------------------------------------------------------------------
# Secrets helpers
# ----------------------------------------------------------------------------
def _get_secret_json(secret_arn):
    response = secrets_client.get_secret_value(SecretId=secret_arn)
    return json.loads(response["SecretString"])


def _put_secret_json(secret_arn, payload):
    secrets_client.put_secret_value(
        SecretId=secret_arn, SecretString=json.dumps(payload)
    )


# ----------------------------------------------------------------------------
# OpenEMR HTTP helpers
# ----------------------------------------------------------------------------
def _wait_for_fhir_ready():
    """Poll GET {BASE}/apis/default/fhir/metadata until the FHIR API serves.

    HTTP 200 -> ready. Connection error / timeout / 5xx, or a body containing
    'API is disabled', or any other 4xx -> not ready; sleep and retry. Capped at
    READINESS_TIMEOUT_SECONDS, then raise with a precise non-secret error.
    """
    url = f"{OPENEMR_BASE_URL}/apis/default/fhir/metadata"
    deadline = time.time() + READINESS_TIMEOUT_SECONDS
    last_status = None
    last_snippet = ""
    while time.time() < deadline:
        try:
            resp = requests.get(url, verify=OPENEMR_SSL_VERIFY, timeout=30)
            last_status = resp.status_code
            body = resp.text or ""
            last_snippet = body[:200]
            if resp.status_code == 200 and "API is disabled" not in body:
                print("OpenEMR FHIR API is ready (metadata returned 200).")
                return
            print(
                f"OpenEMR not ready yet (status={resp.status_code}); "
                f"retrying in {READINESS_BACKOFF_SECONDS}s"
            )
        except requests.RequestException as e:
            last_status = "connection-error"
            last_snippet = str(e)[:200]
            print(
                f"OpenEMR not reachable yet ({e.__class__.__name__}); "
                f"retrying in {READINESS_BACKOFF_SECONDS}s"
            )
        time.sleep(READINESS_BACKOFF_SECONDS)
    raise RuntimeError(
        f"OpenEMR FHIR API not ready after 8m: last status={last_status}, "
        f"last body-snippet={last_snippet!r}"
    )


def _register_client():
    """Register a new OAuth client; return (client_id, client_secret)."""
    url = f"{OPENEMR_BASE_URL}/oauth2/default/registration"
    body = {
        "application_type": "private",
        "token_endpoint_auth_method": "client_secret_post",
        "client_name": "healthlake-sync",
        "contacts": ["noreply@example.com"],
        # Required-but-unused placeholder: ROPC never performs a browser
        # redirect, so this value is never exercised.
        "redirect_uris": [f"{OPENEMR_BASE_URL}/swagger/oauth2-redirect.html"],
        "scope": REGISTRATION_SCOPE,
    }
    resp = requests.post(
        url,
        json=body,
        headers={"Content-Type": "application/json"},
        verify=OPENEMR_SSL_VERIFY,
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"OAuth client registration failed: status={resp.status_code}, "
            f"body-snippet={resp.text[:200]!r}"
        )
    data = resp.json()
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    if not client_id or not client_secret:
        raise RuntimeError(
            "OAuth client registration response missing client_id/client_secret"
        )
    print(f"Registered OAuth client: client_id={client_id}")
    return client_id, client_secret


def _self_test_token(client_id, client_secret, username, password):
    """Attempt a ROPC/password-grant token. Return True on HTTP 200."""
    url = f"{OPENEMR_BASE_URL}/oauth2/default/token"
    try:
        resp = requests.post(
            url,
            data={
                "grant_type": "password",
                "client_id": client_id,
                "client_secret": client_secret,
                "username": username,
                "password": password,
                "user_role": "users",
                "scope": TOKEN_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            verify=OPENEMR_SSL_VERIFY,
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"Self-test token request error: {e.__class__.__name__}")
        return False
    if resp.status_code == 200 and resp.json().get("access_token"):
        print("Self-test token request succeeded (200, access_token present).")
        return True
    print(f"Self-test token request failed: status={resp.status_code}")
    return False


# ----------------------------------------------------------------------------
# Database helper (enable the client)
# ----------------------------------------------------------------------------
def _db_connect():
    creds = _get_secret_json(DB_SECRET_ARN)
    if RDS_CA_BUNDLE and os.path.exists(RDS_CA_BUNDLE):
        ssl_context = ssl.create_default_context(cafile=RDS_CA_BUNDLE)
    else:
        ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = True
    ssl_context.verify_mode = ssl.CERT_REQUIRED
    return pymysql.connect(
        host=creds["host"],
        user=creds["username"],
        password=creds["password"],
        database="openemr",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
        ssl=ssl_context,
    )


def _enable_api_globals():
    """Ensure OpenEMR API globals are enabled in the DB. Idempotent.

    OpenEMR only applies its OPENEMR_SETTING_* env vars at first boot on an
    empty EFS volume; an already-initialized instance ignores them. This
    upserts the three globals the FHIR-API sync depends on so provisioning
    works on both fresh and already-initialized instances without manual DB
    edits. OpenEMR reads globals per-request (short cache), so no container
    restart is required. Logs posture only (never any value).
    """
    conn = _db_connect()
    try:
        with conn.cursor() as cursor:
            for gl_name in ("rest_api", "rest_fhir_api", "oauth_password_grant"):
                cursor.execute(
                    "INSERT INTO globals (gl_name, gl_index, gl_value) "
                    "VALUES (%s, 0, '1') "
                    "ON DUPLICATE KEY UPDATE gl_value = '1'",
                    (gl_name,),
                )
        conn.commit()
        print(
            "Ensured OpenEMR API globals enabled: "
            "rest_api, rest_fhir_api, oauth_password_grant"
        )
    finally:
        conn.close()


def _enable_client_in_db(client_id):
    """Introspect oauth_clients and enable the client. Idempotent."""
    conn = _db_connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='openemr' AND table_name='oauth_clients'"
            )
            columns = {
                (row.get("column_name") or row.get("COLUMN_NAME"))
                for row in cursor.fetchall()
            }
            if "client_id" not in columns or "is_enabled" not in columns:
                raise RuntimeError(
                    "oauth_clients schema unexpected: could not find both "
                    f"'client_id' and 'is_enabled' columns. Columns found: "
                    f"{sorted(c for c in columns if c)}"
                )
            cursor.execute(
                "UPDATE oauth_clients SET is_enabled = 1 WHERE client_id = %s",
                (client_id,),
            )
        conn.commit()
        print(f"Enabled OAuth client in DB: client_id={client_id}")
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Provisioning flow
# ----------------------------------------------------------------------------
def _full_provision():
    """Register -> enable -> re-read admin creds -> self-test -> write secret."""
    client_id, client_secret = _register_client()
    _enable_client_in_db(client_id)

    # Re-read the current admin creds (never a stale copy from the sync secret).
    admin = _get_secret_json(OPENEMR_ADMIN_SECRET_ARN)
    username = admin.get("username") or admin.get("user")
    password = admin.get("password") or admin.get("pass")
    if not username or not password:
        raise RuntimeError(
            "Admin secret missing username/password; cannot provision ROPC creds."
        )

    if not _self_test_token(client_id, client_secret, username, password):
        raise RuntimeError(
            "Self-test token request failed with the freshly registered client "
            "and admin credentials. Verify the OpenEMR admin user is API-enabled "
            "for the ROPC/password grant, or designate a dedicated API user."
        )

    _put_secret_json(
        OPENEMR_CREDENTIALS_SECRET,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
        },
    )
    print("Credentials secret written with an enabled, verified OAuth client.")


def _provision():
    """Idempotent provisioning per the design decision table (3.4)."""
    # Enable the API globals first so an already-initialized instance (which
    # ignored its first-boot env vars) passes the readiness probe and any
    # token self-test, instead of timing out.
    _enable_api_globals()
    _wait_for_fhir_ready()

    # 1. Read the credentials secret.
    try:
        current = _get_secret_json(OPENEMR_CREDENTIALS_SECRET)
    except Exception as e:  # noqa: BLE001 - treat unreadable/empty as not provisioned
        print(f"Could not read existing credentials secret ({e.__class__.__name__}); "
              "treating as not provisioned.")
        current = {}

    # 2. Completeness gate.
    required = ("client_id", "client_secret", "username", "password")
    if all(current.get(k) for k in required):
        # 3. Reuse self-test with the secret's own stored creds.
        if _self_test_token(
            current["client_id"],
            current["client_secret"],
            current["username"],
            current["password"],
        ):
            print("Existing credentials still authenticate; reusing (no changes).")
            return
        print("Existing credentials failed self-test; re-provisioning.")
    else:
        print("Credentials secret incomplete/empty; provisioning from scratch.")

    # 4. Full provision.
    _full_provision()


def handler(event, context):
    """Custom-resource / TriggerFunction entrypoint.

    Delete -> no-op success. Create AND Update -> run full provisioning logic
    (no 'Update skipped' short-circuit).
    """
    request_type = (event or {}).get("RequestType", "Create")
    print(f"OAuth provisioner invoked: RequestType={request_type}")

    if request_type == "Delete":
        return {"statusCode": 200, "body": "Delete acknowledged (no-op)"}

    _provision()
    return {"statusCode": 200, "body": "OpenEMR OAuth credentials provisioned"}
