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
