---
title: Python Secure Code Review Rules
source: OpenSSF Secure Coding Guide for Python
version: 1.0
last-updated: 2026-06-05
description: Structured, citable rules for LLM agents performing Python secure code reviews.
---

# Python Secure Code Review Rules

## Numbers

### PYSCG-0002: Guard Fixed-Width Numbers Against Overflow
**Severity:** MUST  
**Applies to:** Python applications using fixed-width numeric types from libraries like numpy, ctypes, or datetime  
**Rationale:** While Python's built-in int type protects against overflow, fixed-width numbers implemented in C (like numpy.int64, ctypes integers, or datetime calculations) can overflow or underflow, leading to unexpected behavior, security vulnerabilities, or system crashes. These primitive types share issues known from C/C++ and can cause silent wrap-around errors or exceptions.  

**Guideline:**  
Validate inputs and catch exceptions when using fixed-width numeric types to prevent overflow/underflow conditions.

**Examples:**

```python
# Bad - Unchecked numpy.int64 overflow
import numpy as np
a = np.int64(np.iinfo(np.int64).max)
result = a + 1  # Silently wraps to negative number
print(result)   # -9223372036854775808 (unexpected)

# Good - Checking for numpy overflow with warnings
import numpy as np
import warnings
warnings.filterwarnings("error")  # Convert warnings to exceptions
a = np.int64(np.iinfo(np.int64).max)
try:
    with warnings.catch_warnings():
        result = a + 1
        print(result)
except Warning:
    print("Overflow detected and handled")

# Even better - Explicit range checking
import numpy as np
def safe_add(a, b):
    max_int64 = np.iinfo(np.int64).max
    min_int64 = np.iinfo(np.int64).min
    if a > 0 and b > max_int64 - a:
        raise OverflowError("Addition would exceed int64 maximum")
    if a < 0 and b < min_int64 - a:
        raise OverflowError("Addition would go below int64 minimum")
    return np.int64(a + b)

# Bad - Unchecked datetime.timedelta() overflow
from datetime import datetime, timedelta
def add_hours(base_time, hours):
    return base_time + timedelta(hours=hours)  # Can overflow

# Good - Boundary checking for datetime operations
from datetime import datetime, timedelta
def safe_add_hours(base_time, hours):
    # Calculate boundaries
    lower_bound = base_time - datetime(1, 1, 1)
    upper_bound = datetime(9999, 12, 31) - base_time
    hours_min = lower_bound.total_seconds() // 3600 * -1
    hours_max = upper_bound.total_seconds() // 3600
    
    if hours < hours_min or hours > hours_max:
        raise ValueError(f"Hours {hours} out of safe range [{hours_min}, {hours_max}]")
    
    return base_time + timedelta(hours=hours)

# Bad - Unchecked math.exp() overflow
import math
def calc_exp(x):
    return math.exp(x)  # Can overflow

# Good - Exception handling for math operations
import math
def safe_exp(x):
    try:
        return math.exp(x)
    except OverflowError:
        # Handle overflow appropriately for your application
        return float('inf')  # Or raise custom exception, or return error code
```

**Notes for LLM Reviewer:**
- Look for usage of numpy integer types (int8, int16, int32, int64, uint8, etc.)
- Check for ctypes usage with numeric types (c_int, c_long, etc.)
- Watch for datetime.timedelta() with large time values
- Look for math module functions that can overflow (exp(), factorial(), etc.)
- Check for any direct interaction with C libraries through ctypes or C extensions
- Verify that input validation and exception handling are present for fixed-width operations
- Related rules: PYSCG-0001 (Control Numeric Precision), PYSCG-0003 (Use Arithmetic Over Bitwise Operations)