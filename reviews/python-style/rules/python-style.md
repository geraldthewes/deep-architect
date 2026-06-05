---
title: Python Code Review Rules
source: Adapted from Google Python Style Guide
version: 1.0
last-updated: 2026-06-04
description: Structured, citable rules for LLM agents performing Python code reviews.
---

# Python Code Review Rules

## How to Use These Rules (for the LLM Agent)
When reviewing Python code or diffs:
1. Identify relevant categories (Language Rules, Style Rules, etc.).
2. Check the code against **every applicable rule**.
3. For each issue, **cite the exact Rule ID** (e.g. `PY-LANG-001`) and quote the key phrase.
4. Prioritize by **Severity** (MUST > SHOULD > MAY > NIT).
5. Always provide a concrete fix suggestion + Bad/Good example when possible.
6. Also note positive adherence to rules.
7. Use the standard review output format defined in the parent `SKILL.md`.

## Python Language Rules

### PY-LANG-001: Run pylint over your code
**Severity:** MUST  
**Applies to:** All Python source files  
**Rationale:** pylint catches bugs and style problems that are typically caught by a compiler for less dynamic languages like C and C++.

**Guideline:**  
Run `pylint` over your code using the project's pylintrc configuration.

**Examples:**

```python
# Bad - Missing pylint suppression comment when needed
def do_PUT(self):
    # WSGI name, so pylint: disable=invalid-name
    ...
```

**Notes for LLM Reviewer:**
- Check that pylint is being run (via CI or local development setup)
- Flag inappropriate pylint suppressions that hide real issues
- Related: PY-LANG-002 (suppressing pylint warnings appropriately)

### PY-LANG-002: Suppress pylint warnings appropriately
**Severity:** SHOULD  
**Applies to:** All Python source files  
**Rationale:** Suppressing pylint warnings inappropriately can hide real issues that should be fixed.

**Guideline:**  
When suppressing pylint warnings, always explain why the suppression is appropriate.

**Examples:**

```python
# Bad - No explanation for suppression
def do_PUT(self):  # pylint: disable=invalid-name
    ...

# Good - Explains why suppression is needed
def do_PUT(self):  # WSGI name, so pylint: disable=invalid-name
    ...
```

**Notes for LLM Reviewer:**
- Check that suppressions have explanatory comments
- Flag suppressions without explanations
- Related: PY-LANG-001 (running pylint)

### PY-LANG-003: Use import statements for packages and modules only
**Severity:** MUST  
**Applies to:** All Python source files  
**Rationale:** Importing individual types, classes, or functions makes it unclear where identifiers come from and breaks namespace management conventions.

**Guideline:**  
Use `import` statements for packages and modules only, not for individual types, classes, or functions.

**Examples:**

```python
# Bad - Importing individual functions/classes
from mymodule import MyClass, my_function

# Good - Importing the module
import mymodule

# Then use: mymodule.MyClass() or mymodule.my_function()
```

**Notes for LLM Reviewer:**
- Flag `from x import y` patterns where y is a class, function, or constant
- Exception: Symbols from typing, collections.abc, typing_extensions, and six.moves modules
- Related: PY-LANG-004 (alternative import forms)

### PY-LANG-004: Use appropriate import forms
**Severity:** SHOULD  
**Applies to:** All Python source files  
**Rationale:** Using the correct import form improves code clarity and avoids naming conflicts.

**Guideline:**  
- Use `from x import y` where x is the package prefix and y is the module name with no prefix
- Use `from x import y as z` when there are naming conflicts or inconvenient names
- Use `import y as z` only when z is a standard abbreviation

**Examples:**

```python
# Good - Standard import forms
from sound.effects import echo
import numpy as np

# Bad - Non-standard abbreviations
import numpy as num  # np is standard, num is not
```

**Notes for LLM Reviewer:**
- Check for non-standard aliases that reduce readability
- Standard abbreviations like np for numpy, pd for pandas are acceptable
- Related: PY-LANG-003 (import statements for packages/modules)

### PY-LANG-005: Avoid relative names in imports
**Severity:** MUST  
**Applies to:** All Python source files  
**Rationale:** Relative imports can cause unintentional double imports and make it unclear what module is being imported.

**Guideline:**  
Do not use relative names in imports. Even if the module is in the same package, use the full package name.

**Examples:**

```python
# Bad - Relative import (unclear what will be imported)
import jodie

# Good - Full package name
from doctor.who import jodie
```

**Notes for LLM Reviewer:**
- Flag any import that doesn't use the full package path
- Related: PY-LANG-006 (importing modules by full pathname)

### PY-LANG-006: Import each module using full pathname
**Severity:** MUST  
**Applies to:** All Python source files  
**Rationale:** Importing by full pathname avoids conflicts and makes it easier to find modules.

**Guideline:**  
Import each module using the full pathname location of the module.

**Examples:**

```python
# Good - Full pathname imports
import absl.flags
from doctor.who import jodie

# Bad - Unclear import source
import jodie  # Could be local jodie.py or third-party package
```

**Notes for LLM Reviewer:**
- Check that imports use full package paths
- The directory containing the main binary should not be assumed to be in sys.path
- Related: PY-LANG-005 (avoiding relative imports)

### PY-LANG-007: Handle exceptions carefully
**Severity:** MUST  
**Applies to:** All Python source files  
**Rationale:** Improper exception handling can make code confusing and hide error cases.

**Guideline:**  
Exceptions must follow certain conditions:
- Use built-in exception classes when appropriate
- Do not use assert statements in place of conditionals
- Libraries must inherit from existing exception classes
- Never use catch-all except: statements
- Minimize try/except block size
- Use finally clause for cleanup

**Examples:**

```python
# Bad - Using assert for validation
def connect_to_next_port(self, minimum: int) -> int:
    assert minimum >= 1024, 'Minimum port must be at least 1024.'
    port = self._find_next_open_port(minimum)
    assert port is not None
    return port

# Good - Proper exception handling
def connect_to_next_port(self, minimum: int) -> int:
    if minimum < 1024:
        raise ValueError(f'Min. port must be at least 1024, not {minimum}.')
    port = self._find_next_open_port(minimum)
    if port is None:
        raise ConnectionError(
            f'Could not connect to service on port {minimum} or higher.')
    return port
```

**Notes for LLM Reviewer:**
- Flag assert statements used for argument validation
- Check for bare except: statements
- Look for overly large try/except blocks
- Related: PY-LANG-008 (avoiding mutable global state)

### PY-LANG-008: Avoid mutable global state
**Severity:** MUST  
**Applies to:** All Python source files  
**Rationale:** Mutable global state breaks encapsulation and can change module behavior during import.

**Guideline:**  
Avoid mutable global state. When necessary, declare mutable global entities at module level or as class attributes with a leading underscore.

**Examples:**

```python
# Bad - Mutable global state
GLOBAL_LIST = []

def add_to_list(item):
    GLOBAL_LIST.append(item)

# Good - Internal mutable global with proper access
_internal_list = []

def add_to_list(item):
    _internal_list.append(item)

def get_list():
    return _internal_list.copy()
```

**Notes for LLM Reviewer:**
- Flag module-level variables that are mutated after initialization
- Check for proper encapsulation of mutable state
- Module-level constants (ALL_CAPS) are permitted
- Related: PY-LANG-009 (nested functions/classes)

### PY-LANG-009: Use nested functions/classes appropriately
**Severity:** SHOULD  
**Applies to:** All Python source files  
**Rationale:** Improper use of nested functions/classes can lead to confusing bugs and make code harder to test.

**Guideline:**  
Nested local functions or classes are fine when used to close over a local variable. Avoid nesting just to hide functions from users.

**Examples:**

```python
# Bad - Nested just to hide from users (use _prefix instead)
def public_function():
    def _helper():
        return 42
    return _helper()

# Good - Helper function at module level with _ prefix
def _helper():
    return 42

def public_function():
    return _helper()

# Good - Nested to close over local value
def make_adder(n):
    def adder(x):
        return x + n
    return adder
```

**Notes for LLM Reviewer:**
- Flag nested functions that don't close over local values
- Check that nested functions aren't used just for hiding
- Related: PY-LANG-010 (comprehensions & generator expressions)

### PY-LANG-010: Use comprehensions appropriately
**Severity:** SHOULD  
**Applies to:** All Python source files  
**Rationale:** Complicated comprehensions can be hard to read and understand.

**Guideline:**  
Comprehensions are allowed, but multiple for clauses or filter expressions are not permitted. Optimize for readability, not conciseness.

**Examples:**

```python
# Bad - Multiple for clauses
result = [(x, y) for x in range(10) for y in range(5) if x * y > 10]

# Good - Clear loop structure
result = []
for x in range(10):
    for y in range(5):
        if x * y > 10:
            result.append((x, y))

# Good - Simple comprehension
result = [mapping_expr for value in iterable if filter_expr]
```

**Notes for LLM Reviewer:**
- Flag comprehensions with multiple for clauses
- Flag comprehensions with complex filter expressions
- Generator expressions follow same rules
- Related: PY-LANG-011 (default iterators and operators)

### PY-LANG-011: Use default iterators and operators
**Severity:** SHOULD  
**Applies to:** All Python source files  
**Rationale:** Default iterators and operators are simple, efficient, and express operations directly.

**Guideline:**  
Use default iterators and operators for types that support them, like lists, dictionaries, and files.

**Examples:**

```python
# Bad - Using non-default methods
for key in adict.keys():
    ...
for line in afile.readlines():
    ...

# Good - Using default iterators/operators
for key in adict:
    ...
for line in afile:
    ...
```

**Notes for LLM Reviewer:**
- Flag .keys(), .readlines(), and similar non-default method usage
- Prefer direct iteration over explicit method calls
- Related: PY-LANG-012 (using generators)

### PY-LANG-012: Use generators as needed
**Severity:** MAY  
**Applies to:** All Python source files  
**Rationale:** Generators use less memory than functions that create entire lists at once.

**Guideline:**  
Use generators as needed. Use "Yields:" rather than "Returns:" in docstrings for generator functions.

**Examples:**

```python
# Bad - Returns list (memory inefficient for large ranges)
def get_numbers(n):
    result = []
    for i in range(n):
        result.append(i)
    return result

# Good - Generator function
def get_numbers(n):
    for i in range(n):
        yield i
```

**Notes for LLM Reviewer:**
- Check generator docstrings use "Yields:" not "Returns:"
- Flag generators managing expensive resources without cleanup
- Related: PY-LANG-013 (lambda functions)

### PY-LANG-013: Use lambda functions appropriately
**Severity:** MAY  
**Applies to:** All Python source files  
**Rationale:** Lambda functions are harder to read and debug than local functions.

**Guideline:**  
Lambdas are allowed for one-liners. Prefer generator expressions over map() or filter() with lambda. If lambda spans multiple lines or is longer than 60-80 chars, define as regular nested function.

**Examples:**

```python
# Bad - Multi-line lambda
result = list(map(lambda x: 
                  complex_operation(x) 
                  if condition(x) 
                  else fallback_operation(x),
                  items))

# Good - Generator expression
result = [complex_operation(x) if condition(x) else fallback_operation(x) 
          for x in items]

# Good - Single line lambda
result = list(map(lambda x: x * 2, items))
```

**Notes for LLM Reviewer:**
- Flag lambda functions longer than 60-80 characters
- Flag multi-line lambda functions
- Prefer operator module functions for common operations (e.g., operator.mul)
- Related: PY-LANG-014 (conditional expressions)

### PY-LANG-014: Use conditional expressions appropriately
**Severity:** MAY  
**Applies to:** All Python source files  
**Rationale:** Conditional expressions can be harder to read than if statements when conditions are long.

**Guideline:**  
Okay to use for simple cases. Each portion must fit on one line: true-expression, if-expression, else-expression. Use a complete if statement when things get more complicated.

**Examples:**

```python
# Bad - Line breaking in conditional expression
bad_line_breaking = ('yes' if predicate(value) else
                     'no')

# Good - Simple conditional expression
one_line = 'yes' if predicate(value) else 'no'

# Good - If statement for complex condition
if some_long_condition():
    result = 'yes_value'
else:
    result = 'no_value'
```

**Notes for LLM Reviewer:**
- Flag conditional expressions that break across lines
- Check that each portion (true/false/condition) fits on one line
- Related: PY-LANG-015 (default argument values)

### PY-LANG-015: Avoid mutable default argument values
**Severity:** MUST  
**Applies to:** All Python source files  
**Rationale:** Default arguments are evaluated once at module load time, causing issues with mutable objects.

**Guideline:**  
Do not use mutable objects as default values in function or method definitions.

**Examples:**

```python
# Bad - Mutable default arguments
def foo(a, b=[]):
    b.append(a)
    return b

# Good - Using None and checking inside function
def foo(a, b=None):
    if b is None:
        b = []
    b.append(a)
    return b

# Good - Immutable default (empty tuple)
def foo(a, b=()):
    return a + b
```

**Notes for LLM Reviewer:**
- Flag [] , {} , or other mutable objects as default values
- Flag time.time() or similar mutable defaults
- Empty tuples () are acceptable as they're immutable
- Related: PY-LANG-016 (properties)

### PY-LANG-016: Use properties appropriately
**Severity:** SHOULD  
**Applies to:** All Python source files  
**Rationale:** Properties should match expectations of regular attribute access; unnecessary properties add complexity.

**Guideline:**  
Properties are allowed but should only be used when necessary and match expectations of typical attribute access.

**Examples:**

```python
# Bad - Unnecessary property (no computation)
class BadExample:
    def __init__(self):
        self._value = 0
    
    @property
    def value(self):
        return self._value  # Just returns attribute, no computation
    
    @value.setter
    def value(self, val):
        self._value = val

# Good - Property with trivial computation
class GoodExample:
    def __init__(self):
        self._items = []
    
    @property
    def item_count(self):
        return len(self._items)  # Trivial derivation

# Good - Make attribute public instead of unnecessary property
class BetterExample:
    def __init__(self):
        self.value = 0  # Public attribute is fine
```

**Notes for LLM Reviewer:**
- Flag properties that just get/set internal attributes without computation
- Check that property logic is simple and unsurprising
- Properties should be created with @property decorator
- Related: PY-LANG-017 (True/False evaluations)

### PY-LANG-017: Use implicit False evaluations
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Using implicit False evaluations makes code more readable and less error-prone.

**Guideline:**  
Use the "implicit" false if possible, with specific caveats for None, booleans, sequences, integers, and strings.

**Examples:**

```python
# Bad - Explicit comparisons
if len(users) == 0:
    print('no users')

if not i % 10:
    self.handle_multiple_of_ten()

def f(x=None):
    x = x or []

# Good - Implicit False evaluations
if not users:
    print('no users')

if i % 10 == 0:
    self.handle_multiple_of_ten()

def f(x=None):
    if x is None:
        x = []
```

**Notes for LLM Reviewer:**
- Check for proper None checks (is None vs == None)
- Flag boolean comparisons to False (use not x instead)
- Check sequence emptiness checks (if seq: vs if len(seq):)
- Note that '0' string evaluates to True
- Related: PY-LANG-018 (lexical scoping)

### PY-LANG-018: Use lexical scoping appropriately
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Improper use of lexical scoping can lead to confusing bugs.

**Guideline:**  
Okay to use lexical scoping, but be aware of variable binding resolution rules.

**Examples:**

```python
# Bad - Confusing variable scoping
i = 4
def foo(x):
    def bar():
        print(i, end='')  # Prints 4
    for i in x:  # This i is local to foo
        print(i, end='')
    bar()

# Good - Clear variable scoping
i = 4
def foo(x):
    def bar():
        print(i, end='')  # Prints 4 (global i)
    for item in x:  # Use different variable name
        print(item, end='')
    bar()
```

**Notes for LLM Reviewer:**
- Flag nested functions that modify variables from outer scopes
- Check for variable name collisions in nested scopes
- Related: PY-LANG-019 (function and method decorators)

### PY-LANG-019: Use decorators judiciously
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Decorators can perform arbitrary operations and execute at import time, making failures hard to recover from.

**Guideline:**  
Use decorators judiciously when there is a clear advantage. Avoid staticmethod and limit use of classmethod.

**Examples:**

```python
# Bad - Unnecessary staticmethod
class MathUtils:
    @staticmethod
    def add(x, y):
        return x + y

# Good - Module-level function
def add(x, y):
    return x + y

# Good - Appropriate classmethod for alternative constructor
class Date:
    def __init__(self, year, month, day):
        self.year = year
        self.month = month
        self.day = day
    
    @classmethod
    def from_timestamp(cls, timestamp):
        # Implementation...
        return cls(year, month, day)
```

**Notes for LLM Reviewer:**
- Flag staticmethod usage unless required for API integration
- Check classmethod usage is for named constructors or class-specific routines
- Decorator docstrings should state the function is a decorator
- Avoid external dependencies in decorators (files, sockets, etc.)
- Related: PY-LANG-020 (threading)

### PY-LANG-020: Handle threading appropriately
**Severity:** MUST  
**Applies:** All Python source files  
**Rationale:** Relying on atomicity of built-in types can lead to race conditions and unpredictable behavior.

**Guideline:**  
Do not rely on atomicity of built-in types. Use queue module's Queue for thread communication, otherwise use threading module with locking primitives.

**Examples:**

```python
# Bad - Relying on dictionary atomicity
shared_dict = {}

def worker():
    # Not actually atomic!
    shared_dict['key'] = shared_dict.get('key', 0) + 1

# Good - Using Queue for communication
from queue import Queue
q = Queue()

def worker():
    result = compute_something()
    q.put(result)

# Good - Using threading locks
import threading
lock = threading.Lock()
shared_counter = 0

def worker():
    global shared_counter
    with lock:
        shared_counter += 1
```

**Notes for LLM Reviewer:**
- Flag direct mutation of shared built-in types (dict, list) between threads
- Check for proper use of Queue or threading.Lock/Condition
- Related: PY-LANG-021 (avoiding power features)

### PY-LANG-021: Avoid power features
**Severity:** MUST  
**Applies:** All Python source files  
**Rationale:** Power features make code harder to read, understand, and debug.

**Guideline:**  
Avoid power features like custom metaclasses, bytecode access, dynamic inheritance, etc.

**Examples:**

```python
# Bad - Custom metaclass (power feature)
class Meta(type):
    def __new__(cls, name, bases, dct):
        # Complex metaclass logic
        return super().__new__(cls, name, bases, dct)

class MyClass(metaclass=Meta):
    pass

# Good - Standard class without power features
class MyClass:
    def __init__(self):
        self.value = 42
```

**Notes for LLM Reviewer:**
- Flag custom metaclasses (__metaclass__ or metaclass=)
- Flag direct bytecode manipulation
- Flag dynamic inheritance or object reparenting
- Standard library modules using these features are OK (abc.ABCMeta, dataclasses, enum)
- Related: PY-LANG-022 (future imports)

### PY-LANG-022: Use future imports appropriately
**Severity:** MAY  
**Applies:** All Python source files  
**Rationale:** Future imports enable modern Python syntax in older runtimes.

**Guideline:**  
Use of `from __future__ import` statements is encouraged. Remove them when no longer needed for older version compatibility.

**Examples:**

```python
# Good - Future import for generator stop (needed for Python 3.5 compatibility)
from __future__ import generator_stop

# Good - Other future imports as needed
from __future__ import annotations
from __future__ import print_function
```

**Notes for LLM Reviewer:**
- Check that future imports are removed when no longer needed
- For code targeting >= 3.7, generator_stop import may be unnecessary
- Related: PY-LANG-023 (type annotated code)

### PY-LANG-023: Use type annotations
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Type annotations improve readability and maintainability, converting runtime errors to build-time errors.

**Guideline:**  
Strongly encouraged to enable Python type analysis. Include type annotations for public APIs and enable checking via pytype.

**Examples:**

```python
# Good - Type annotated function
def func(a: int) -> list[int]:
    return [a] * a

# Good - Type annotated variable
count: int = 0
items: list[str] = []

# Good - Complex type annotations
def process_data(data: Mapping[str, List[int]]) -> Dict[str, float]:
    # Implementation...
    return results
```

**Notes for LLM Reviewer:**
- Check for type annotations on function arguments and return values
- Look for variable type annotations
- Flag missing annotations on public APIs
- Related: Style Rules (following sections)

## Python Style Rules

### PY-STY-001: Do not use semicolons
**Severity:** MUST  
**Applies:** All Python source files  
**Rationale:** Semicolons are unnecessary in Python and reduce readability.

**Guideline:**  
Do not terminate lines with semicolons, and do not use semicolons to put two statements on the same line.

**Examples:**

```python
# Bad - Unnecessary semicolons
x = 1; y = 2;
if x > y: print(x);

# Good - No semicolons
x = 1
y = 2
if x > y:
    print(x)
```

**Notes for LLM Reviewer:**
- Flag any semicolon usage in Python code
- Related: PY-STY-002 (line length)

### PY-STY-002: Limit line length to 80 characters
**Severity:** MUST  
**Applies:** All Python source files  
**Rationale:** Long lines are harder to read and require horizontal scrolling.

**Guideline:**  
Maximum line length is 80 characters.

**Exceptions:**
- Long import statements
- URLs, pathnames, or long flags in comments
- Long string module-level constants not containing whitespace
- Pylint disable comments

**Examples:**

```python
# Good - Within 80 character limit
def foo_bar(self, width, height, color='black', design=None, x='foo',
            emphasis=None, highlight=0):
    pass

# Good - Using implicit line joining
if (width == 0 and height == 0 and
    color == 'red' and emphasis == 'strong'):
    pass

# Bad - Exceeds 80 characters (without justification)
very_long_line_that_exceeds_the_eighty_character_limit_by_far = "this line is way too long"
```

**Notes for LLM Reviewer:**
- Check line lengths (excluding allowed exceptions)
- Flag lines exceeding 80 chars without valid exception
- Prefer implicit line joining inside parentheses over backslash continuation
- Related: PY-STY-003 (parentheses usage)

### PY-STY-003: Use parentheses sparingly
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Unnecessary parentheses reduce code clarity.

**Guideline:**  
Use parentheses sparingly. Do not use them in return statements or conditional statements unless for line continuation or tuples.

**Examples:**

```python
# Bad - Unnecessary parentheses
if (x):
    bar()
if not(x):
    bar()
return (foo)

# Good - Necessary parentheses only
if x:
    bar()
if not x:
    bar()
return foo
return spam, beans
return (spam, beans)  # Explicit tuple
for (x, y) in dict.items(): ...  # Tuple unpacking
```

**Notes for LLM Reviewer:**
- Flag parentheses around conditionals in if/while statements
- Flag parentheses around return values (unless tuple)
- Check for appropriate use in line continuation and tuple creation
- Related: PY-STY-004 (indentation)

### PY-STY-004: Indent code blocks with 4 spaces
**Severity:** MUST  
**Applies:** All Python source files  
**Rationale:** Consistent indentation improves readability and prevents mixing spaces/tabs.

**Guideline:**  
Indent code blocks with 4 spaces. Never use tabs.

**Examples:**

```python
# Good - 4-space indentation
def long_function_name(var_one, var_two,
                       var_three, var_four):
    if var_one > var_two:
        print(var_one)
    else:
        print(var_two)

# Bad - Mixed indentation
def long_function_name(var_one, var_two,
                       var_three, var_four):
	if var_one > var_two:  # Tab indentation
	    print(var_one)     # Mixed tabs/spaces
	else:
	        print(var_two)  # Inconsistent spacing
```

**Notes for LLM Reviewer:**
- Check for consistent 4-space indentation
- Flag any tab characters in indentation
- Check hanging indents for alignment
- Related: PY-STY-005 (trailing commas)

### PY-STY-005: Use trailing commas appropriately
**Severity:** MAY  
**Applies:** All Python source files  
**Rationale:** Trailing commas improve git diffs and help auto-formatters.

**Guideline:**  
Trailing commas in sequences are recommended only when the closing token doesn't appear on the same line as the final element, or for single-element tuples.

**Examples:**

```python
# Good - Trailing comma when closing token on next line
golomb4 = [
    0,
    1,
    4,
    6,
]

# Good - Single element tuple needs trailing comma
onesie = (foo,)

# Bad - Trailing comma when closing token on same line
golomb4 = [
    0,
    1,
    4,
    6,  # Comma here causes issues
]

# Good - No trailing comma when all on one line
golomb3 = [0, 1, 3]
```

**Notes for LLM Reviewer:**
- Check trailing commas in lists, tuples, dicts
- Flag commas before closing tokens on same line
- Check that single-element tuples have trailing commas
- Related: PY-STY-006 (blank lines)

### PY-STY-006: Use blank lines appropriately
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Blank lines improve code organization and readability.

**Guideline:**  
Two blank lines between top-level definitions. One blank line between method definitions and between class docstring and first method. No blank line following def line.

**Examples:**

```python
# Good - Proper blank line usage
class MyClass:
    """Class docstring."""
    
    def __init__(self):
        pass
    
    def my_method(self):
        pass


def top_level_function():
    pass


def another_top_level_function():
    pass

# Bad - Incorrect blank line usage
class MyClass:
    """Class docstring."""
    def __init__(self):  # Missing blank line after docstring
        pass
        
    def my_method(self):  # Missing blank line between methods
        pass

def top_level_function():  # Missing blank line after function
    pass
def another_top_level_function():  # Missing blank line before function
    pass
```

**Notes for LLM Reviewer:**
- Check for 2 blank lines between top-level definitions (functions/classes)
- Check for 1 blank line between method definitions
- Check for 1 blank line after class docstring before first method
- Check for NO blank line after def line
- Related: PY-STY-007 (whitespace)

### PY-STY-007: Follow whitespace conventions
**Severity:** MUST  
**Applies:** All Python source files  
**Rationale:** Proper whitespace usage improves readability and follows typographic standards.

**Guideline:**  
Follow standard typographic rules for spaces around punctuation.

**Examples:**

```python
# Bad - Incorrect whitespace
if x == 4 :                    # Space before colon
    print(x , y)               # Spaces around comma
x , y = y , x                  # Spaces around equals

spam( ham[ 1 ], { 'eggs': 2 }, [ ] )  # Spaces inside parentheses
dict ['key'] = list [index]     # Spaces around brackets
x<1                            # No space around operator
x = 1                          # Missing spaces around equals

# Good - Correct whitespace
if x == 4:
    print(x, y)
x, y = y, x

spam(ham[1], {'eggs': 2}, [])
dict['key'] = list[index]
x == 1
x = 1
```

**Notes for LLM Reviewer:**
- Check for no whitespace inside parentheses/brackets/braces
- Check for no whitespace before commas, semicolons, colons
- Check for whitespace after commas, semicolons, colons (except end of line)
- Check for no whitespace before opening paren/bracket for argument lists
- Check for single space around binary operators
- Check for no spaces around = for keyword args (except with type annotations)
- Related: PY-STY-008 (alignment)

### PY-STY-008: Avoid using spaces for vertical alignment
**Severity:** MAY  
**Applies:** All Python source files  
**Rationale:** Using spaces for alignment creates maintenance burdens.

**Guideline:**  
Don't use spaces to vertically align tokens on consecutive lines.

**Examples:**

```python
# Bad - Vertical alignment with spaces
foo       = 1000  # comment
long_name = 2     # comment that should not be aligned

dictionary = {
    'foo'      : 1,
    'long_name': 2,
}

# Good - No vertical alignment
foo = 1000  # comment
long_name = 2  # comment that should not be aligned

dictionary = {
    'foo': 1,
    'long_name': 2,
}
```

**Notes for LLM Reviewer:**
- Flag spaces used for alignment of =, :, #, etc.
- Check that similar tokens aren't aligned with extra spaces
- Related: PY-STY-009 (shebang line)

### PY-STY-009: Use appropriate shebang line
**Severity:** MAY  
**Applies:** Executable Python files  
**Rationale:** Proper shebang lines ensure correct interpreter usage.

**Guideline:**  
Start the main file of a program with `#!/usr/bin/env python3` or `#!/usr/bin/python3`.

**Examples:**

```python
# Good - Portable shebang
#!/usr/bin/env python3

"""Module docstring."""
def main():
    pass

if __name__ == '__main__':
    main()

# Good - Specific shebang
#!/usr/bin/python3

"""Module docstring."""
def main():
    pass

if __name__ == '__main__':
    main()

# Bad - Missing or incorrect shebang
"""Module docstring."""
def main():
    pass

if __name__ == '__main__':
    main()
```

**Notes for LLM Reviewer:**
- Check executable Python files for shebang lines
- Prefer #!/usr/bin/env python3 for portability
- Shebang only needed for files intended to be executed directly
- Related: PY-STY-010 (comments and docstrings)

### PY-STY-010: Use proper comments and docstrings
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Proper documentation improves code maintainability and understanding.

**Guideline:**  
Use the right style for module, function, method docstrings and inline comments.

**Examples:**

```python
# Bad - Incorrect docstring format
'''
Incorrect format docstring
'''

def bad_function():
    """Bad docstring without period."""
    return 42

# Good - Proper docstring format
"""A one-line summary of the module.

Leave one blank line. The rest of this docstring should contain an
overall description of the module or program.
"""

def good_function():
    """Good docstring with period and proper structure.
    
    This function does something useful.
    
    Returns:
        The answer to life, the universe, and everything.
    """
    return 42

# Bad - Poor comment quality
# Fix the bug with the thing
x = 1

# Good - Clear, helpful comments
# Handle edge case where user provides empty input
if not user_input:
    return default_value
```

**Notes for LLM Reviewer:**
- Check for proper triple-quote docstring format (""")
- Check docstring organization: summary line, blank line, detailed description
- Check for proper punctuation and grammar in comments
- Check for helpful, explanatory comments (not just stating the obvious)
- Related: Specific comment/docstring subsections (following)

### PY-STY-011: Write proper module docstrings
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Module docstrings provide essential information about file contents and usage.

**Guideline:**  
Every file should contain a docstring describing the contents and usage of the module.

**Examples:**

```python
# Bad - Missing module docstring
import os
import sys

def main():
    pass

# Good - Proper module docstring
"""A module for processing customer orders.

This module provides functions to validate, process, and track
customer orders through the fulfillment system.

Typical usage example:

  processor = OrderProcessor()
  order = processor.validate_order(raw_order)
  processor.process_order(order)
"""

import os
import sys

def main():
    pass
```

**Notes for LLM Reviewer:**
- Check that every Python file has a module-level docstring
- Check for proper structure: summary line, blank line, description
- Look for usage examples when appropriate
- Test modules may omit module-level docstrings when no additional info
- Related: PY-STY-012 (function/method docstrings)

### PY-STY-012: Write proper function and method docstrings
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Function and method docstrings explain purpose, parameters, and return values.

**Guideline:**  
Use proper docstring format for functions and methods with clear parameter and return value descriptions.

**Examples:**

```python
# Bad - Incomplete function docstring
def process_order(order):
    """Process an order."""
    # Implementation...
    return result

# Good - Complete function docstring
def process_order(order):
    """Process a customer order through the fulfillment system.
    
    Validates the order, processes payment, and initiates shipping.
    
    Args:
        order: A dictionary containing order details with keys:
              'customer_id', 'items', 'total_amount', 'shipping_address'
              
    Returns:
        An OrderConfirmation object containing:
        - order_id: Unique identifier for the processed order
        - tracking_number: Shipping tracking number
        - estimated_delivery: Estimated delivery date
        
    Raises:
        ValidationError: If order data is invalid
        PaymentError: If payment processing fails
    """
    # Implementation...
    return result
```

**Notes for LLM Reviewer:**
- Check for proper docstring format on all functions/methods
- Look for Args:, Returns:, Raises: sections when appropriate
- Check parameter descriptions are clear and complete
- Check return value descriptions explain what is returned
- Overridden methods should mention they override parent method
- Related: PY-STY-013 (class docstrings)

### PY-STY-013: Write proper class docstrings
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Class docstrings explain purpose, attributes, and usage of classes.

**Guideline:**  
Use proper docstring format for classes with clear description of purpose and usage.

**Examples:**

```python
# Bad - Incomplete class docstring
class OrderProcessor:
    """Processes orders."""
    def __init__(self):
        pass

# Good - Complete class docstring
class OrderProcessor:
    """Processes customer orders through the fulfillment system.
    
    This class handles order validation, payment processing,
    and shipment initiation for customer orders.
    
    Example:
        processor = OrderProcessor()
        order = {'customer_id': '123', 'items': [{'sku': 'ABC', 'qty': 2}]}
        confirmation = processor.process_order(order)
    """
    
    def __init__(self):
        """Initialize the order processor.
        
        Sets up connections to payment and inventory systems.
        """
        pass
```

**Notes for LLM Reviewer:**
- Check for proper docstring format on all classes
- Look for clear description of class purpose and usage
- Check for examples when appropriate
- Check __init__ docstrings explain initialization process
- Related: PY-STY-014 (block and inline comments)

### PY-STY-014: Write helpful block and inline comments
**Severity:** MAY  
**Applies:** All Python source files  
**Rationale:** Helpful comments explain why code does something, not just what it does.

**Guideline:**  
Write comments that explain the reasoning behind code decisions.

**Examples:**

```python
# Bad - Comment stating the obvious
# Increment counter by 1
counter += 1

# Good - Explaining why
# We add 1 to account for the header row in CSV processing
counter += 1

# Bad - Commented out code (should be removed)
# def old_function():
#     return process_data_legacy way

# Good - Clear explanation of complex logic
# Use binary search instead of linear search for O(log n) performance
# instead of O(n) since we expect large datasets
def find_item(items, target):
    left, right = 0, len(items) - 1
    while left <= right:
        mid = (left + right) // 2
        if items[mid] == target:
            return mid
        elif items[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1
```

**Notes for LLM Reviewer:**
- Flag comments that just repeat what the code does
- Look for comments that explain why or reasoning
- Flag large blocks of commented-out code (should be removed or in version control)
- Check that comments clarify complex or non-obvious logic
- Related: PY-STY-015 (punctuation, spelling, grammar)

### PY-STY-015: Maintain proper punctuation, spelling, and grammar
**Severity:** MAY  
**Applies:** All Python source files  
**Rationale:** Proper punctuation, spelling, and grammar improve readability and professionalism.

**Guideline:**  
Follow standard writing conventions for punctuation, spelling, and grammar in comments and docstrings.

**Examples:**

```python
# Bad - Poor punctuation, spelling, grammar
# this funktion doesnt work rite
# fix it asap plz thanx

# Good - Proper punctuation, spelling, grammar
# This function doesn't work correctly.
# Please fix it as soon as possible. Thanks!

# Bad - Inconsistent punctuation
"""This is a docstring without proper punctuation
It has inconsistent style"""

# Good - Consistent punctuation
"""This is a proper docstring.
It ends with a period and has consistent style."""
```

**Notes for LLM Reviewer:**
- Check for proper sentence punctuation in comments/docstrings
- Look for spelling errors in comments/documentation
- Check for grammatical correctness in explanations
- Flag excessive use of internet slang or abbreviations
- Related: PY-STY-016 (strings)

### PY-STY-016: Handle strings appropriately
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Proper string handling improves readability and prevents errors.

**Guideline:**  
Follow conventions for string literals, logging, and error messages.

**Examples:**

```python
# Bad - Inconsistent string quotes
message = "He said \"hello\" to me"
path = 'C:\\Users\\Name\\Documents\\file.txt'

# Good - Consistent string quoting
message = 'He said "hello" to me'
path = r'C:\Users\Name\Documents\file.txt'  # Raw string for Windows paths
# OR
path = 'C:\\Users\\Name\\Documents\\file.txt'  # Escaped backslashes

# Bad - Poor logging practice
logging.error("Something went wrong: " + str(error))

# Good - Proper logging
logging.error("Something went wrong: %s", error)

# Bad - Unclear error message
raise ValueError("Invalid input")

# Good - Clear error message
raise ValueError(f"Input value {value} must be between 1 and 100")
```

**Notes for LLM Reviewer:**
- Check for consistent string quote usage (prefer single quotes unless contains single quote)
- Check for raw strings (r'...') for paths or regex to avoid excessive escaping
- Check logging uses % formatting rather than string concatenation
- Check error messages are clear and actionable
- Related: PY-STY-017 (files and sockets)

### PY-STY-017: Close files, sockets, and similar resources
**Severity:** MUST  
**Applies:** All Python source files  
**Rationale:** Failing to close resources can lead to leaks and system instability.

**Guideline:**  
Prefer using context managers (with statement) for files, sockets, and similar stateful resources.

**Examples:**

```python
# Bad - Manual resource management (error-prone)
f = open('file.txt', 'r')
try:
    data = f.read()
finally:
    f.close()

# Good - Context manager (automatic cleanup)
with open('file.txt', 'r') as f:
    data = f.read()

# Bad - Socket not properly closed
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.connect(('example.com', 80))
# ... use sock ...
# Forgot to close sock!

# Good - Socket with context manager
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.connect(('example.com', 80))
    # ... use sock ...
    # Automatically closed when exiting with block
```

**Notes for LLM Reviewer:**
- Flag open() calls not using with statement
- Check for proper resource cleanup in try/finally blocks when context managers aren't used
- Flag socket, file, or similar resource usage without clear cleanup strategy
- Prefer context managers for automatic resource management
- Related: PY-STY-018 (TODO comments)

### PY-STY-018: Use TODO comments appropriately
**Severity:** MAY  
**Applies:** All Python source files  
**Rationale:** TODO comments help track work that needs to be done.

**Guideline:**  
Use TODO comments for temporary code, short-term solutions, or imperfect implementations.

**Examples:**

```python
# Good - Proper TODO comment
# TODO: Replace this with proper authentication once auth service is ready
token = get_temp_token()

# Good - TODO with owner and date
# TODO(john.doe): Optimize this algorithm for better performance - 2026-06-04

# Bad - Vague TODO without action items
# TODO: fix this later

# Bad - TODO in production code without plan
# TODO: rewrite this entire module (no plan or timeline)
```

**Notes for LLM Reviewer:**
- Check for TODO comments with clear action items
- Look for TODOs with owners and dates when appropriate
- Flag vague TODOs without clear next steps
- Flag TODOs in production code without implementation plan
- Related: PY-STY-019 (imports formatting)

### PY-STY-019: Format imports properly
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Properly formatted imports improve readability and maintainability.

**Guideline:**  
Group imports in this order: standard library, third-party, local application/library.

**Examples:**

```python
# Bad - Poorly ordered imports
import os  # Standard library
from myproject import utils  # Local application
import numpy as np  # Third-party
import sys  # Standard library (out of order)

# Good - Properly ordered imports
# Standard library imports
import os
import sys

# Third-party imports
import numpy as np
import pandas as pd

# Local application imports
from myproject import utils
from myproject.config import settings

# Good - With blank lines between groups
import os
import sys

import numpy as np
import pandas as pd

from myproject import utils
from myproject.config import settings
```

**Notes for LLM Reviewer:**
- Check import grouping: stdlib → third-party → local
- Look for blank lines between import groups
- Flag imports that are out of order
- Check within groups, imports should be alphabetically ordered
- Related: PY-STY-020 (statements)

### PY-STY-020: Format statements properly
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Properly formatted statements improve code readability.

**Guideline:**  
Each line should contain at most one statement.

**Examples:**

```python
# Bad - Multiple statements on one line
if foo: bar(); baz = 1; qux()

# Good - One statement per line
if foo:
    bar()
baz = 1
qux()

# Bad - Compound statements without proper formatting
if foo: bar() if baz: qux()

# Good - Properly formatted compound statements
if foo:
    bar()
if baz:
    qux()
```

**Notes for LLM Reviewer:**
- Flag lines with multiple statements separated by semicolons
- Check that control flow statements (if, for, while, etc.) are followed by indented blocks
- Flag attempts to put multiple logical operations on one line
- Related: PY-STY-021 (accessors)

### PY-STY-021: Use accessors appropriately
**Severity:** MAY  
**Applies:** All Python source files  
**Rationale:** Unnecessary accessor methods add complexity without benefit.

**Guideline:**  
If you would use a simple public attribute, use a simple public attribute rather than accessor methods.

**Examples:**

```python
# Bad - Unnecessary getter/setter methods
class Particle:
    def __init__(self):
        self._mass = 0
    
    def get_mass(self):
        return self._mass
    
    def set_mass(self, mass):
        self._mass = mass

# Good - Public attribute (when appropriate)
class Particle:
    def __init__(self):
        self.mass = 0  # Direct access is fine

# Good - Property with computation (when needed)
class Particle:
    def __init__(self, velocity):
        self.velocity = velocity
    
    @property
    def kinetic_energy(self):
        return 0.5 * self.mass * self.velocity ** 2
```

**Notes for LLM Reviewer:**
- Flag getter/setter methods that just access private attributes
- Check that properties are used when computation is needed
- Public attributes are fine when no encapsulation is needed
- Related: PY-STY-022 (main function)

### PY-STY-022: Use main function appropriately
**Severity:** SHOULD  
**Applies:** All Python source files  
**Rationale:** Proper main function structure improves testability and reusability.

**Guideline:**  
The main functionality of a program should be in a main() function.

**Examples:**

```python
# Bad - Code at module level
#!/usr/bin/env python3
"""Module docstring."""

import sys

def process_data(data):
    return data * 2

# Processing happens at module level - harder to test/reuse
data = int(sys.argv[1])
result = process_data(data)
print(result)

# Good - Main function structure
#!/usr/bin/env python3
"""Module docstring."""

import sys

def process_data(data):
    return data * 2

def main():
    """Main program entry point."""
    data = int(sys.argv[1])
    result = process_data(data)
    print(result)

if __name__ == '__main__':
    main()
```

**Notes for LLM Reviewer:**
- Check that executable scripts have main() function
- Look for if __name__ == '__main__': guard calling main()
- Flag significant logic happening at module level in executable files
- Library files (not meant to be executed directly) may not need main()
- Related: PY-STY-023 (function length)

### PY-STY-023: Keep functions reasonably sized
**Severity:** MAY  
**Applies:** All Python source files  
**Rationale:** Overly large functions are harder to understand, test, and maintain.

**Guideline:**  
Break down functions that exceed 40 lines into smaller, more focused functions.

**Examples:**

```python
# Bad - Overly large function
def process_user_data(user_id):
    """Process all data for a user."""
    # 50+ lines of mixed validation, processing, and storage logic
    # Hard to follow, test, and maintain
    validation_result = validate_user_input(user_id)
    if not validation_result.is_valid:
        return validation_result
    
    # ... many more lines of processing ...
    
    # ... storage logic ...
    
    return processing_result

# Good - Broken down into smaller functions
def process_user_data(user_id):
    """Process all data for a user."""
    validation_result = validate_user_input(user_id)
    if not validation_result.is_valid:
        return validation_result
    
    processed_data = prepare_user_data(user_id)
    storage_result = store_user_data(processed_data)
    
    return storage_result

def validate_user_input(user_id):
    """Validate user input data."""
    # Focused validation logic
    return validation_result

def prepare_user_data(user_id):
    """Prepare user data for storage."""
    # Focused preparation logic
    return processed_data

def store_user_data(data):
    """Store user data."""
    # Focused storage logic
    return storage_result
```

**Notes for LLM Reviewer:**
- Check function lengths (aim for < 40 lines when possible)
- Look for functions doing multiple distinct things (validation, processing, storage)
- Flag functions with high cyclomatic complexity
- Suggest breaking large functions into smaller, focused ones
- Related: PY-STY-024 (type annotations)

### PY-STY-024: Use type annotations in style context
**Severity:** MAY  
**Applies:** All Python source files  
**Rationale:** Type annotations in function signatures improve readability and IDE support.

**Guideline:**  
Follow conventions for type annotation formatting and placement.

**Examples:**

```python
# Bad - Poor type annotation formatting
def func(a: int,b: str)-> list[int]:
    return [a]*len(b)

# Good - Proper type annotation spacing
def func(a: int, b: str) -> list[int]:
    return [a] * len(b)

# Bad - Line breaking that hurts readability
def very_long_function_name(
    param1: TypeWithAVeryLongNameThatExceedsLineLength,
    param2: AnotherVeryLongTypeName
) -> ReturnTypeAlsoVeryLong:
    # Implementation...
    return result

# Good - Proper line breaking for type annotations
def very_long_function_name(
    param1: TypeWithAVeryLongName,
    param2: AnotherVeryLongTypeName,
) -> ReturnTypeAlsoVeryLong:
    # Implementation...
    return result
```

**Notes for LLM Reviewer:**
- Check for proper spacing around colons and arrows in type annotations
- Look for reasonable line breaking when type annotations are long
- Flag missing spaces: int->str should be int: str
- Check that line breaks align with opening delimiter
- Related to PY-LANG-023 (type annotated code) but focuses on style aspects

## Rule ID Prefixes

All rule IDs follow the pattern `PY-{CATEGORY}-{NUMBER}` where:
- `LANG` = Language Rules (sections 2.1-2.21)
- `STY` = Style Rules (sections 3.1-3.19)

## Severity Levels

- **MUST**: Critical issues that must be fixed before merge (correctness, security)
- **SHOULD**: Important issues that should be fixed unless justified
- **MAY**: Suggestions worth considering
- **NIT**: Minor style preferences (not used in this guide as Google style is prescriptive)

## How to Cite Rules

When issuing feedback, cite the exact Rule ID and quote the relevant guideline:
```
- `file.py:42` — **PY-LANG-003** (Use import statements for packages and modules only)
  - Why: Importing individual functions breaks namespace management
  - Suggested fix: Import the module instead of individual components
    ```python
    # Good
    import mymodule
    # Use: mymodule.MyClass() or mymodule.my_function()
    ```
```