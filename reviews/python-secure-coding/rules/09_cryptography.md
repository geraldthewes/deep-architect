---
title: Python Secure Code Review Rules
source: OpenSSF Secure Coding Guide for Python
version: 1.0
last-updated: 2026-06-05
description: Structured, citable rules for LLM agents performing Python secure code reviews.
---

# Python Secure Code Review Rules

## Cryptography

### PYSCG-0038: Use Sufficiently Random Values
**Severity:** MUST  
**Applies to:** All Python applications that generate random values for security purposes (tokens, keys, nonces, etc.)  
**Rationale:** Python's `random` module uses the Mersenne Twister algorithm, which is deterministic and predictable. If an attacker knows or can guess the seed value, they can predict the entire sequence of random numbers. This makes it unsuitable for security/cryptographic purposes where unpredictability is essential.  

**Guideline:**  
For security/cryptographic purposes, use cryptographically secure random number generators like Python's `secrets` module or `os.urandom()` instead of the `random` module.

**Examples:**

```python
# Bad - Using random module for security purposes
import random
def generate_insecure_token():
    # VULNERABLE: Predictable if seed is known
    return random.randrange(int("1" + "0" * 31), int("9" * 32), 1)

# Bad - Using random with timestamp as seed (still predictable)
import random
import time
def generate_insecure_token_time_seed():
    random.seed(time.time())  # Still guessable/predictable
    return random.random()

# Good - Using secrets module for tokens
import secrets
def generate_secure_token():
    # SAFE: Cryptographically secure
    return secrets.token_urlsafe()

# Good - Using secrets for random integers
import secrets
def generate_secure_random_int(upper_bound):
    # SAFE: Cryptographically secure random integer
    return secrets.randbelow(upper_bound)

# Good - Using os.urandom for cryptographic randomness
import os
def generate_secure_random_bytes(length):
    # SAFE: Cryptographically secure random bytes
    return os.urandom(length)

# Good - Using secrets for password generation
import secrets
import string
def generate_secure_password(length=16):
    alphabet = string.ascii_letters + string.digits + string.punctuation
    # SAFE: Using secrets.choice instead of random.choice
    return ''.join(secrets.choice(alphabet) for _ in range(length))
```

**Notes for LLM Reviewer:**
- Look for imports of `random` module and its usage in security contexts
- Check for `random.random()`, `random.randint()`, `random.choice()`, etc. used for tokens, passwords, or security values
- Watch for seeding `random` with timestamps or other predictable values
- Verify use of `secrets` module or `os.urandom()` for security-related randomness
- Related rules: PYSCG-0001 (Control Numeric Precision), PYSCG-0002 (Guard Fixed-Width Numbers Against Overflow)