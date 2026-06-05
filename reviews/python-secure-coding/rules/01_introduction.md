---
title: Python Secure Code Review Rules
source: OpenSSF Secure Coding Guide for Python
version: 1.0
last-updated: 2026-06-05
description: Structured, citable rules for LLM agents performing Python secure code reviews.
---

# Python Secure Code Review Rules

## How to Use These Rules (for the LLM Agent)
When reviewing Python code or diffs:
1. Identify relevant categories (Introduction, Encoding and Strings, Numbers, etc.).
2. Check the code against **every applicable rule**.
3. For each issue, **cite the exact Rule ID** (e.g. `PYSCG-0040`) and quote the key phrase.
4. Prioritize by **Severity** (MUST > SHOULD > MAY > NIT).
5. Always provide a concrete fix suggestion + Bad/Good example when possible.
6. Also note positive adherence to rules.
7. Use the standard review output format defined in the parent `SKILL.md`.

## Introduction

### PYSCG-0040: Use Process Isolation for Trust Zones
**Severity:** MUST  
**Applies to:** All Python applications requiring trust boundaries  
**Rationale:** Unlike Java, where we have in-process mechanisms like Oracle Access Management that can enforce access boundaries inside the same runtime, standard Python does not provide a built-in in-process access manager. In Python we need to implement different trust zones by starting python runtimes with individual POSIX/Machine users. The POSIX/Machine user access rights must be set in accordance to level of trust per zone.  

**Guideline:**  
Create isolated trust zones on the operating system level by using different POSIX/Machine users for different trust levels.

**Examples:**

```python
# Bad - All processes running under same user (violation of trust isolation)
# Imagine this as pseudocode representing system architecture
def process_user_signup():
    # All components run under same OS user
    validate_user_input()      # Same privilege level
    create_account_record()    # Same privilege level
    send_welcome_email()       # Same privilege level
    update_audit_log()         # Same privilege level

# Good - Separate processes with different OS users for different trust zones
# Process isolation approach (conceptual representation)
# In practice, this would be implemented via:
# - Different system users for different services
# - Containerization with different user contexts
# - Microservices with separate trust boundaries

def untrusted_zone_process():
    # Runs as limited user
    validate_user_input()      # Low privilege
    
def trusted_zone_process():
    # Runs as higher privilege user (only when needed)
    create_account_record()    # Medium privilege
    update_audit_log()         # Medium privilege
    
def sensitive_zone_process():
    # Runs as highest privilege user (minimal exposure)
    send_welcome_email()       # High privilege (if needed)
```

**Notes for LLM Reviewer:**
- Look for architectures where all components run under the same OS/user context
- Check for missing separation between public-facing components and sensitive data handling
- Verify if privilege separation is implemented via different system users or containers
- Related rules: PYSCG-0041 (Externalize Configuration and Secrets), PYSCG-0055 (Determine Access on Server Side)

### PYSCG-0041: Externalize Configuration and Secrets
**Severity:** MUST  
**Applies to:** All Python applications handling configuration or secrets  
**Rationale:** Hardcoded secrets and configuration values in source code create significant security risks. When source code is exposed (through version control leaks, improper access controls, or other means), these secrets can be immediately exploited. Externalizing configuration prevents accidental exposure and allows for environment-specific configurations without code changes.  

**Guideline:**  
Store configuration data and secrets outside of source code, using environment variables, configuration files with appropriate access controls, or secret management systems.

**Examples:**

```python
# Bad - Hardcoded secrets
API_KEY = "sk_live_1234567890abcdef"  # Secret exposed in source code
DATABASE_PASSWORD = "supersecret123"
DEBUG = True  # Configuration that should vary by environment

# Good - Externalized configuration
import os
from dotenv import load_dotenv

load_dotenv()  # Load from .env file (which should be .gitignored)
API_KEY = os.getenv("STRIPE_API_KEY")
DATABASE_PASSWORD = os.getenv("DB_PASSWORD")
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# Even better - using secret management systems
# import boto3
# secrets_client = boto3.client('secretsmanager')
# secret_value = secrets_client.get_secret_value(SecretId='prod/db/credentials')
# DATABASE_PASSWORD = json.loads(secret_value['SecretString'])['password']
```

**Notes for LLM Reviewer:**
- Scan for literal strings that look like passwords, API keys, tokens, or certificates
- Check for configuration values that should vary between environments (dev/staging/prod)
- Look for debug flags or verbose logging settings that are hardcoded to True
- Related rules: PYSCG-0040 (Process Isolation), PYSCG-0055 (Determine Access on Server Side)