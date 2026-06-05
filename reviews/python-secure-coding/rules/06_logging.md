---
title: Python Secure Code Review Rules
source: OpenSSF Secure Coding Guide for Python
version: 1.0
last-updated: 2026-06-05
description: Structured, citable rules for LLM agents performing Python secure code reviews.
---

# Python Secure Code Review Rules

## Logging

### PYSCG-0019: Exclude Sensitive Data From Logs
**Severity:** MUST  
**Applies to:** All Python applications that use logging  
**Rationale:** Logging is crucial for debugging and incident management, but logging sensitive information such as passwords, credit card numbers, or personal data creates security and compliance risks. National regulations like GDPR and CCPA impose fines for data protection violations, and sensitive data in logs can be exposed through log aggregation systems, debugging interfaces, or improper access controls.  

**Guideline:**  
Never log sensitive information such as passwords, credit card numbers, or personal identifiers. Instead, log only non-sensitive identifiers or use techniques like data masking, tokenization, or encryption when logging is absolutely necessary.

**Examples:**

```python
# Bad - Logging sensitive data directly
import logging
def login_user(username, password, security_answer):
    # VULNERABLE: Logging password and security answer in plain text
    logging.info(
        "User %s login attempt: password=%s, security answer=%s",
        username, password, security_answer
    )

# Good - Logging only non-sensitive information
import logging
def login_user_safe(username, password, security_answer):
    # SAFE: Only logging username, not sensitive data
    logging.info("User %s login attempt", username)
    # Process login without logging credentials

# Good - Using custom filters to redact sensitive data
import logging
import re

class RedactingFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        # Redact passwords, security answers, etc.
        message = re.sub(r"password=\S+", "password=REDACTED", message)
        message = re.sub(r"security_answer=\S+", "security_answer=REDACTED", message)
        message = re.sub(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b", "CARD_NUMBER_REDACTED", message)  # Credit cards
        record.msg = message
        record.args = ()
        return True

def setup_secure_logging():
    logger = logging.getLogger()
    logger.addFilter(RedactingFilter())
    return logger

# Good - Using structured logging with safe fields
import logging
import json
def login_user_structured(username, password, security_answer):
    # SAFE: Only log non-sensitive fields in structured format
    log_entry = {
        "event": "login_attempt",
        "username": username,
        "timestamp": "2023-01-01T12:00:00Z",
        "success": True  # or False based on outcome
    }
    logging.info(json.dumps(log_entry))
```

**Notes for LLM Reviewer:**
- Look for logging calls that include variables likely to contain sensitive data (password, secret, key, token, ssn, credit_card, etc.)
- Check for logging of user input that might contain sensitive information
- Verify that logging configuration doesn't inadvertently log sensitive data through format strings or f-strings
- Look for missing redaction filters when logging might contain sensitive data
- Related rules: PYSCG-0020 (Implement Informative Event Logging), PYSCG-0021 (Exclude Developer Tools), PYSCG-0022 (Neutralize Untrusted Data in Logs)

### PYSCG-0020: Implement Informative Event Logging
**Severity:** SHOULD  
**Applies to:** All Python applications that require monitoring or debugging  
**Rationale:** While avoiding sensitive data in logs is critical, logs should still contain sufficient information for debugging, forensic analysis, and incident response. Informative logging helps developers understand system behavior, troubleshoot issues, and detect security incidents.  

**Guideline:**  
Implement logging that captures important operational events while excluding sensitive data. Use appropriate log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL) and structured logging formats when beneficial.

**Examples:**

```python
# Bad - Insufficient logging for debugging
import logging
def process_order(item_id, quantity):
    # No logging makes debugging difficult
    total = calculate_price(item_id, quantity)
    return apply_discount(total)

# Good - Informative but safe logging
import logging
def process_order_safe(item_id, quantity):
    logging.info("Processing order for item_id: %s, quantity: %s", item_id, quantity)
    try:
        total = calculate_price(item_id, quantity)
        logging.debug("Calculated price for item_id %s: %s", item_id, total)
        discounted_total = apply_discount(total)
        logging.info("Order processed successfully. Final amount: %s", discounted_total)
        return discounted_total
    except Exception as e:
        logging.error("Failed to process order for item_id %s: %s", item_id, str(e))
        raise

# Good - Structured logging for better analysis
import logging
import json
from datetime import datetime
def process_order_structured(item_id, quantity):
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "event": "order_processing_start",
        "item_id": item_id,
        "quantity": quantity
    }
    logging.info(json.dumps(log_entry))
    
    try:
        total = calculate_price(item_id, quantity)
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "event": "price_calculated",
            "item_id": item_id,
            "quantity": quantity,
            "price": total
        }
        logging.debug(json.dumps(log_entry))
        
        discounted_total = apply_discount(total)
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "event": "order_processed_success",
            "item_id": item_id,
            "quantity": quantity,
            "final_amount": discounted_total
        }
        logging.info(json.dumps(log_entry))
        return discounted_total
    except Exception as e:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "event": "order_processing_failed",
            "item_id": item_id,
            "quantity": quantity,
            "error": str(e)
        }
        logging.error(json.dumps(log_entry))
        raise
```

**Notes for LLM Reviewer:**
- Look for functions with no logging that would benefit from diagnostic information
- Check for appropriate use of log levels (DEBUG for detailed info, INFO for general events, WARNING for recoverable issues, etc.)
- Verify that logging doesn't contain sensitive data while still being useful for debugging
- Look for opportunities to use structured logging (JSON) for better log analysis
- Related rules: PYSCG-0019 (Exclude Sensitive Data From Logs), PYSCG-0021 (Exclude Developer Tools)

### PYSCG-0021: Exclude Developer Tools From the Final Product
**Severity:** MUST  
**Applies to:** All Python applications intended for production deployment  
**Rationale:** Developer tools such as debuggers, interactive consoles, or verbose debugging outputs can create security vulnerabilities if left in production code. These tools might expose internal state, allow arbitrary code execution, or provide attackers with insights into the application's internal workings.  

**Guideline:**  
Remove or disable developer tools, debuggers, and verbose debugging outputs before deploying to production environments.

**Examples:**

```python
# Bad - Leaving debugger in production code
import pdb
def process_data(data):
    # VULNERABLE: Debugger left in production code
    pdb.set_trace()  # Allows arbitrary code execution if reached
    return transform_data(data)

# Bad - Verbose debugging in production
import logging
def process_data_verbose(data):
    logging.basicConfig(level=logging.DEBUG)  # Too verbose for production
    logging.debug("Processing data: %s", data)
    result = transform_data(data)
    logging.debug("Transformed data: %s", result)
    return result

# Good - Conditional debugging based on environment
import logging
import os
def process_data_safe(data):
    # SAFE: Only enable debug logging in development
    if os.getenv("ENVIRONMENT") == "development":
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)  # Appropriate for production
    
    logging.info("Processing data batch")
    result = transform_data(data)
    logging.debug("Data transformation completed")  # Only visible in dev
    return result

# Good - Using proper logging configuration
import logging.config
def setup_production_logging():
    # SAFE: Production-appropriate logging configuration
    logging_config = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'standard': {
                'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
            },
        },
        'handlers': {
            'default': {
                'level': 'INFO',
                'formatter': 'standard',
                'class': 'logging.StreamHandler',
            },
        },
        'loggers': {
            '': {
                'handlers': ['default'],
                'level': 'INFO',
                'propagate': False
            }
        }
    }
    logging.config.dictConfig(logging_config)
```

**Notes for LLM Reviewer:**
- Look for `pdb.set_trace()`, `breakpoint()`, or other debugger calls
- Check for logging level set to DEBUG in production-like configurations
- Watch for print statements or console output that might expose internal details
- Verify that development tools are guarded by environment checks or removed for production
- Related rules: PYSCG-0019 (Exclude Sensitive Data From Logs), PYSCG-0020 (Implement Informative Event Logging)

### PYSCG-0022: Neutralize Untrusted Data in Logs
**Severity:** SHOULD  
**Applies to:** All Python applications that log user-provided or external data  
**Rationale:** Logging untrusted data without neutralization can lead to log injection attacks, where attackers inject malicious content into logs to confuse administrators, execute code in log viewers, or compromise log aggregation systems. This is particularly dangerous when logs are viewed in terminals (escape sequences) or processed by automated systems.  

**Guideline:**  
Sanitize or neutralize untrusted data before including it in log output to prevent injection attacks.

**Examples:**

```python
# Bad - Logging untrusted data without neutralization
import logging
def search_products(user_query):
    # VULNERABLE: User query might contain escape sequences or malicious content
    logging.info("User searched for: %s", user_query)
    # If user_query contains "\x1b[31mERROR\x1b[0m", it might appear as red text in terminal
    # If user_query contains script tags, it might execute in web log viewers

# Good - Neutralizing untrusted data for logs
import logging
import re
def search_products_safe(user_query):
    # SAFE: Neutralize potential control characters
    safe_query = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', user_query)  # Remove control chars
    safe_query = safe_query.replace('%', '%%')  # Escape percent signs for logging
    logging.info("User searched for: %s", safe_query)
    # Process the actual query (without neutralization) for functionality
    return perform_search(user_query)

# Even better - Using structured logging to avoid injection entirely
import logging
import json
def search_products_structured(user_query):
    # SAFE: Structured logging treats data as data, not executable content
    log_entry = {
        "timestamp": "2023-01-01T12:00:00Z",
        "event": "search_performed",
        "query": user_query,  # JSON encoding handles special characters safely
        "results_count": 0  # Will be filled in
    }
    logging.info(json.dumps(log_entry))
    results = perform_search(user_query)
    log_entry["results_count"] = len(results)
    logging.info(json.dumps(log_entry))
    return results

# Good - Length limiting to prevent log flooding
import logging
def search_products_limited(user_query):
    # SAFE: Limit length to prevent excessive log entries
    max_query_length = 100
    if len(user_query) > max_query_length:
        logged_query = user_query[:max_query_length] + "...[TRUNCATED]"
    else:
        logged_query = user_query
    logging.info("User searched for: %s", logged_query)
    return perform_search(user_query)
```

**Notes for LLM Reviewer:**
- Look for logging of user input, URLs, filenames, or other external data
- Check for lack of sanitization when logging data that comes from users or external systems
- Watch for potential terminal escape sequences, HTML/script injection, or log flooding
- Verify use of structured logging or proper sanitization for untrusted data in logs
- Related rules: PYSCG-0019 (Exclude Sensitive Data From Logs), PYSCG-0020 (Implement Informative Event Logging)