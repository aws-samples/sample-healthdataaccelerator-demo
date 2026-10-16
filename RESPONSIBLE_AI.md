# Responsible AI

This project includes an optional AI feature in the Patient 360 Dashboard that
uses Amazon Bedrock (Amazon Nova Lite) to generate a plain-language **summary**
of a patient record and to answer **questions** about that record. Because this
is a healthcare context, the feature is built with explicit responsible-AI
controls. This document describes those controls, their intent, and their
limitations.

## Intended use

- **What it does:** Summarizes and answers questions about the documented facts
  already present in a single patient's record (conditions, medications,
  allergies, encounters, immunizations, notes, imaging metadata).
- **What it is NOT:** It is not clinical decision support. It does not diagnose,
  recommend treatment, interpret results, or predict outcomes. It is a
  documentation/readability aid for authenticated users viewing records they are
  already authorized to see.

## Controls in place

### 1. Human oversight
The AI output is presented to an authenticated clinician/staff user alongside
the underlying record. The human remains the decision-maker; the model never
takes an action and never writes back to any system.

### 2. "Facts only" prompting
The system prompts (see `lambda/healthlake_api/handler.py`) constrain the model
to report only what is explicitly documented, to avoid speculative language
("may", "might", "could", "suggests"), and to refuse medical-advice questions
with a fixed response. Prompts are a first line of defense, not the only one.

### 3. Amazon Bedrock Guardrails (defense-in-depth)
A Bedrock Guardrail is provisioned in `infrastructure/patient_dashboard_stack.py`
(`CfnGuardrail` + `CfnGuardrailVersion`) and applied on every model invocation
via `guardrailIdentifier` / `guardrailVersion`. It provides:

- **Denied topic — Medical Advice:** blocks diagnoses, prognoses, treatment or
  medication guidance, and clinical interpretation/recommendation.
- **Content filters (HIGH):** hate, insults, sexual, violence, misconduct.
- **Prompt-attack filter (HIGH, input):** mitigates jailbreak / prompt injection
  that could try to override the "facts only" instructions.
- **Safe blocked messaging:** if the guardrail intervenes, the user receives a
  neutral message directing them to a qualified clinician rather than model
  output.

The guardrail is enabled by default and can be toggled with the
`enable_bedrock_guardrails` context flag in `cdk.json` (default `"true"`).

### 3a. Live guardrail toggle (demo)
The `enable_bedrock_guardrails` context flag above is a *deploy-time* switch. In
addition, when guardrails are enabled at deploy there is a separate *live*
on/off switch that takes effect in seconds with **no redeploy**. It is backed by
an SSM Parameter Store string parameter named
`/hda/<stackname-lowercased>/guardrails_enabled` (for the default deploy this is
exactly `/hda/patientdashboardstack/guardrails_enabled`). The exact name is also
emitted as the `GuardrailToggleParameter` CloudFormation output on
`PatientDashboardStack`. Valid values are `on` or `off`, and the deploy default
is `on`.

The parameter is only created when guardrails are enabled at deploy time (the
default). If guardrails were disabled at deploy, there is no toggle and the
dashboard runs unguarded. The dashboard API Lambda
(`lambda/healthlake_api/handler.py`) reads this parameter on every request with
a short in-process cache (a few seconds), so a change takes effect within
seconds. It **fails safe**: if the parameter cannot be read, it defaults to the
guardrail being **ON**.

The toggle changes two things together, which is what makes the contrast visible
in a live demo:

- **ON** — the Bedrock guardrail (denied `MedicalAdvice` topic, HIGH content
  filters, prompt-injection/prompt-attack filter) is attached to every model
  invocation, **and** the system prompt is the strict "facts only" prompt (no
  interpretation, no recommendations). If the guardrail intervenes, the user
  gets a safe fallback message directing them to a qualified clinician instead
  of model output.
- **OFF** — the guardrail is **not** attached, **and** the system prompt
  switches to a permissive clinical-assistant prompt that will offer
  interpretation and recommendations. This makes the unguarded behavior obvious.

So asking the dashboard AI the same clinical question (e.g. a treatment
recommendation) returns a blocked/neutral "cannot provide medical advice"
response when ON, and an actual recommendation when OFF.

```bash
# Turn the guardrail OFF (show raw, unguarded model output)
aws ssm put-parameter --overwrite --name /hda/patientdashboardstack/guardrails_enabled --value off

# Turn it back ON
aws ssm put-parameter --overwrite --name /hda/patientdashboardstack/guardrails_enabled --value on
```

If you deployed under a non-default stack name/id, substitute the lowercased
stack id in the parameter path, or read the `GuardrailToggleParameter`
CloudFormation output to get the exact name. This toggle is primarily a
**demonstration** aid to show the guardrail's effect; the safe default is ON,
and production use should keep it ON.

### 4. Data protection
- Access to the dashboard and its API is authenticated via Amazon Cognito.
- The API Lambda's IAM permissions are least-privilege and scoped
  (HealthLake read, KMS via HealthLake only, a single Bedrock model, and
  `bedrock:ApplyGuardrail` scoped to this guardrail).
- We intentionally do **not** enable Guardrail PII anonymization on output: the
  dashboard's purpose is to show an authorized user the patient's own record, so
  redacting names/dates would defeat the legitimate use. PHI is protected by
  authentication and IAM, not by stripping it from the response.
- Full request/response event logging is off by default to avoid writing PHI to
  logs (`DEBUG_LOG_EVENTS` / `DEBUG_LOG_PHI` opt-in flags).

## Limitations and disclaimers

- This is a **demonstration/sample**. It is not a certified medical device and
  must not be used for real clinical decision-making.
- LLM output can be incomplete or wrong even when constrained to documented
  facts; always verify against the source record.
- Guardrails and prompts reduce but do not eliminate the risk of inappropriate
  output. Human review is required.
- Before any production or regulated use, conduct your own risk assessment,
  validation, and compliance review (e.g., HIPAA, applicable local regulations).
