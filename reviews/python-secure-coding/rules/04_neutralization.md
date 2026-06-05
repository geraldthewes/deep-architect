---
title: Python Secure Code Review Rules
source: OpenSSF Secure Coding Guide for Python
version: 1.0
last-updated: 2026-06-05
description: Structured, citable rules for LLM agents performing Python secure code reviews.
---

# Python Secure Code Review Rules

## Neutralization

### PYSCG-0008: Prevent Format String Injection
**Severity:** MUST  
**Applies to:** All Python applications that use string formatting with user input  
**Rationale:** In Python, the use of string formatting combined with the ability to access a function's `__globals__` attribute can expose internal variables and methods unless properly guarded. Attackers can craft malicious format strings to read or write memory, leading to information disclosure or remote code execution.  

**Guideline:**  
Ensure that all format string functions are passed a static string which cannot be controlled by the user. Never concatenate user input with format string templates.

**Examples:**

```python
# Bad - User input concatenated with format string (vulnerable to injection)
import sys

ENCRYPTION_KEY = "FL4G1"  # Sensitive data
MESSAGE = "Contract '{0.instance_name}' created for "

class MicroService:
    def __init__(self, instance_name):
        self.instance_name = instance_name

def front_end(customer):
    # VULNERABLE: customer controls part of the format string
    message_format = MESSAGE + customer  
    mc = MicroService("big time microservice")
    print(message_format.format(mc))

# Attacker can pass: "{0.__init__.__globals__[ENCRYPTION_KEY]}"
# To leak the encryption key

# Good - Using string.Template for safe substitution
import sys
from string import Template

ENCRYPTION_KEY = "FL4G1"
MESSAGE = Template("Contract '$instance_name' created for '$customer'")

class MicroService:
    def __init__(self, instance_name):
        self.instance_name = instance_name

    def get_instance_name(self):
        return self.instance_name

def front_end(customer):
    # SAFE: Template substitution prevents format injection
    mc = MicroService("big time microservice")
    print(MESSAGE.substitute(
        instance_name=mc.get_instance_name(),
        customer=customer
    ))

# Also good - Using f-strings with careful variable placement
def front_end_safe(customer):
    # SAFE: f-string with variables, not user-controlled format
    mc = MicroService("big time microservice")
    print(f"Contract '{mc.instance_name}' created for {customer}")

# Bad - Direct use of user input in format()
def vulnerable_log(message):
    print(message.format())  # If message comes from user, dangerous

# Good - Explicit format strings
def safe_log(template, *args):
    print(template.format(*args))  # template is static, args are data
```

**Notes for LLM Reviewer:**
- Look for string concatenation where user input becomes part of a format string
- Check for direct use of user input in `.format()` or f-string expressions
- Watch for logging functions that might inadvertently use user input as format strings
- Verify that format strings are static/literal and never constructed from user data
- Related rules: PYSCG-0009 (Prevent OS Command Injection), PYSCG-0010 (Prevent SQL Injection)

### PYSCG-0009: Prevent OS Command Injection
**Severity:** MUST  
**Applies to:** All Python applications that execute system commands  
**Rationale:** OS command injection occurs when an application executes operating system commands using unsanitized user input. Attackers can inject malicious commands to gain unauthorized access, steal data, or compromise the system.  

**Guideline:**  
Avoid executing system commands when possible. When necessary, use subprocess with proper argument validation and avoid shell=True.

**Examples:**

```python
# Bad - Using os.system with user input (high risk)
import os
def backup_file(filename):
    # VULNERABLE: filename is not sanitized
    os.system(f"cp {filename} /backup/")  # Command injection possible

# Bad - Using subprocess with shell=True and user input
import subprocess
def backup_file(filename):
    # VULNERABLE: shell=True interprets shell metacharacters
    subprocess.run(f"cp {filename} /backup/", shell=True)  # Dangerous

# Good - Using subprocess without shell, with arguments as list
import subprocess
def backup_file(filename):
    # SAFE: Arguments passed as list, no shell interpretation
    subprocess.run(["cp", filename, "/backup/"])

# Even better - Using shlex.quote for shell commands when unavoidable
import subprocess
import shlex
def backup_file(filename):
    # SAFE: Properly quoted for shell
    quoted_filename = shlex.quote(filename)
    subprocess.run(f"cp {quoted_filename} /backup/", shell=True)

# Best - Using Python's built-in file operations
import shutil
def backup_file(filename):
    # SAFEST: Pure Python, no subprocess needed
    shutil.copy(filename, "/backup/")
```

**Notes for LLM Reviewer:**
- Look for `os.system()`, `subprocess.call()`, `subprocess.Popen()`, etc.
- Check for `shell=True` parameter usage
- Watch for string concatenation to build shell commands
- Verify that user input is properly validated or escaped when used in commands
- Prefer Python-native libraries over subprocess when possible
- Related rules: PYSCG-0008 (Prevent Format String Injection), PYSCG-0010 (Prevent SQL Injection)

### PYSCG-0010: Prevent SQL Injection
**Severity:** MUST  
**Applies to:** All Python applications that construct SQL queries  
**Rationale:** SQL injection occurs when user input is incorrectly embedded in SQL queries, allowing attackers to modify query logic, access unauthorized data, or execute arbitrary SQL commands.  

**Guideline:**  
Use parameterized queries (prepared statements) or ORM query builders instead of string concatenation to build SQL queries.

**Examples:**

```python
# Bad - String concatenation (vulnerable to SQL injection)
import sqlite3
def get_user(username, password):
    conn = sqlite3.connect('app.db')
    cursor = conn.cursor()
    # VULNERABLE: Direct string concatenation
    query = f"SELECT * FROM users WHERE username='{username}' AND password='{password}'"
    cursor.execute(query)
    return cursor.fetchone()

# Attacker can input: ' OR '1'='1' -- 
# Resulting query: SELECT * FROM users WHERE username='' OR '1'='1' -- ' AND password=''

# Good - Parameterized queries
import sqlite3
def get_user_safe(username, password):
    conn = sqlite3.connect('app.db')
    cursor = conn.cursor()
    # SAFE: Parameters passed separately
    query = "SELECT * FROM users WHERE username=? AND password=?"
    cursor.execute(query, (username, password))
    return cursor.fetchone()

# Good - Named parameters (also safe)
import sqlite3
def get_user_named(username, password):
    conn = sqlite3.connect('app.db')
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE username=:username AND password=:password"
    cursor.execute(query, {"username": username, "password": password})
    return cursor.fetchone()

# Good - Using ORM (SQLAlchemy example)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
def get_user_orm(username, password):
    Session = sessionmaker(bind=engine)
    session = Session()
    # SAFE: ORM handles parameterization
    result = session.execute(
        text("SELECT * FROM users WHERE username=:username AND password=:password"),
        {"username": username, "password": password}
    )
    return result.fetchone()
```

**Notes for LLM Reviewer:**
- Look for string concatenation or f-strings used to build SQL queries
- Check for `.execute()` calls with string arguments that contain user input
- Watch for LIKE clauses, ORDER BY, or other SQL parts that might be dynamically built
- Verify that parameterized queries or ORM methods are used consistently
- Related rules: PYSCG-0008 (Prevent Format String Injection), PYSCG-0009 (Prevent OS Command Injection)