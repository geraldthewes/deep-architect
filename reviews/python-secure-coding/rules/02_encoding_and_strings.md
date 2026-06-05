---
title: Python Secure Code Review Rules
source: OpenSSF Secure Coding Guide for Python
version: 1.0
last-updated: 2026-06-05
description: Structured, citable rules for LLM agents performing Python secure code reviews.
---

# Python Secure Code Review Rules

## Encoding and Strings

### PYSCG-0043: Specify Locale Explicitly
**Severity:** SHOULD  
**Applies to:** Python applications that perform locale-dependent operations (case conversion, string comparison, number/date parsing)  
**Rationale:** Locale-dependent programs may produce unexpected behavior or security bypasses in an environment whose locale is unset, or not set to an appropriate value. Python follows Unicode conventions for text, which may not match user expectations in their native language. Some characters have particular rules in Unicode that may not match native usage (e.g., German ß, Greek Σ).  

**Guideline:**  
Explicitly set the locale to a known value before performing locale-dependent operations, or avoid locale-dependent operations when possible by using locale-independent alternatives.

**Examples:**

```python
# Bad - Relying on system locale without setting it explicitly
import locale
WORD = "Title"
print(WORD.upper())  # May produce unexpected results in Turkish locale

# Good - Explicitly setting locale for known behavior
import locale
WORD = "Title"
locale.setlocale(locale.LC_ALL, "en_US.utf8")  # Set to known locale
print(WORD.upper())  # Consistent behavior

# Better - Avoiding locale-dependent operations when possible
import datetime
dt = datetime.datetime(2022, 3, 9, 12, 55, 35, 000000)
# Use locale-independent attributes instead of strftime("%B")
month_number = dt.month  # Always 3 for March, regardless of locale

# Good - Setting locale once and checking for consistency
import locale
ORIGINAL_NUMBER = 12.345

def compare_number(number):
    # Set explicit locale for consistent parsing
    locale.setlocale(locale.LC_ALL, 'en_US.utf8')
    input_number = locale.atof(input(f"Enter a number {ORIGINAL_NUMBER}: "))
    return number == input_number

# Set and verify locale at start of program
locale.setlocale(locale.LC_ALL, 'en_US.utf8')
print(f"Locale is {locale.getlocale()}")
```

**Notes for LLM Reviewer:**
- Look for uses of `strftime()` with `%B` (month name) or `%A` (weekday name) for locale-dependent formatting
- Check for string case conversions (`.upper()`, `.lower()`) that might be locale-sensitive
- Watch for `locale.atof()` or `locale.atoi()` used for parsing user input without setting locale first
- Look for missing `locale.setlocale()` calls before locale-dependent operations
- Related rules: PYSCG-0044 (Canonicalize Input Before Validating), PYSCG-0045 (Enforce Consistent Encoding)

### PYSCG-0044: Canonicalize Input Before Validating
**Severity:** MUST  
**Applies to:** All Python applications that validate or filter user input  
**Rationale:** Validation checks can be bypassed if attackers provide input in alternate encodings or representations that get normalized after validation but before use. Canonicalizing input to a standard form before validation ensures that validation checks work on the actual data that will be used.  

**Guideline:**  
Convert input to a canonical form (standard representation) before applying validation or filtering rules.

**Examples:**

```python
# Bad - Validating then canonicalizing (TOCTOU vulnerability)
import unicodedata
username = input("Enter username: ")
if not is_valid_username(username):  # Validation on raw input
    raise ValueError("Invalid username")
username_norm = unicodedata.normalize('NFKC', username)  # Canonicalization after validation
# Attacker could bypass validation using special unicode characters

# Good - Canonicalizing then validating
import unicodedata
username = input("Enter username: ")
username_norm = unicodedata.normalize('NFKC', username)  # Canonicalize first
if not is_valid_username(username_norm):  # Validation on canonicalized input
    raise ValueError("Invalid username")
# Now validation checks work on the actual normalized data that will be used

# Bad - Directory traversal validation before canonicalization
filename = input("Enter filename: ")
if ".." in filename or "/" in filename:  # Simple validation
    raise ValueError("Invalid filename")
# Attacker might bypass with %2E%2E%2F (URL-encoded ../) or other encodings

# Good - Canonicalize path before validation
import os
filename = input("Enter filename: ")
filename = os.path.normpath(filename)  # Canonicalize path
if ".." in filename or filename.startswith("/"):
    raise ValueError("Invalid filename")
```

**Notes for LLM Reviewer:**
- Identify all points where user input is validated or filtered
- Check if validation happens before or after canonicalization/normalization
- Look for validation of filenames, URLs, usernames, or other structured input
- Watch for missing Unicode normalization (NFC, NFD, NFKC, NFKD)
- Related rules: PYSCG-0043 (Specify Locale Explicitly), PYSCG-0045 (Enforce Consistent Encoding)

### PYSCG-0045: Enforce Consistent Encoding
**Severity:** MUST  
**Applies to:** All Python applications that handle text data  
**Rationale:** Inconsistent handling of text encoding can lead to data corruption, security vulnerabilities, or unexpected behavior. When different parts of an application assume different encodings, data can be misinterpreted or corrupted during processing.  

**Guideline:**  
Establish and enforce a consistent encoding policy throughout the application, typically UTF-8, and explicitly handle encoding/decoding at boundaries.

**Examples:**

```python
# Bad - Implicit encoding assumptions
def process_file(filename):
    with open(filename, 'r') as f:  # Uses system default encoding
        content = f.read()
    # Process content...
    return content

# Good - Explicit UTF-8 encoding
def process_file(filename):
    with open(filename, 'r', encoding='utf-8') as f:  # Explicit UTF-8
        content = f.read()
    # Process content...
    return content

# Bad - Mixed encoding handling
def handle_user_input(data):
    # Some code assumes ASCII
    if isinstance(data, bytes):
        data = data.decode('ascii')  # May fail on non-ASCII
    # Other code assumes UTF-8
    return data.upper()

# Good - Consistent UTF-8 handling
def handle_user_input(data):
    if isinstance(data, bytes):
        data = data.decode('utf-8')  # Consistent UTF-8
    return data.upper()

# Good - Explicit encoding at boundaries
def read_config_file(filename):
    with open(filename, 'rb') as f:  # Read as bytes
        content = f.read()
    # Explicitly decode at boundary
    return content.decode('utf-8')

def write_config_file(filename, content):
    # Explicitly encode at boundary
    with open(filename, 'wb') as f:
        f.write(content.encode('utf-8'))
```

**Notes for LLM Reviewer:**
- Check all file open operations for explicit encoding parameters
- Look for places where bytes/string conversions happen without explicit encoding
- Watch for mixed encoding assumptions in different parts of the code
- Verify that encoding declarations are present in Python files (# -*- coding: utf-8 -*-)
- Related rules: PYSCG-0043 (Specify Locale Explicitly), PYSCG-0044 (Canonicalize Input Before Validating)