# Sprint Documentation Generation Enhancement Plan

## Overview
This plan addresses the missing mechanism to generate and save sprint-specific documentation during harness runs. While PROJ-0008 established the permanent sprint documentation templates in `knowledge/architecture/sprints/`, there's currently no code to:
1. Read these templates during sprint execution
2. Fill them with sprint-specific data (Agreements, Strengths, Concerns, etc.)
3. Save the completed documentation to the output directory for each sprint

This enhancement will ensure that each sprint run produces usable documentation regardless of exit path, fulfilling the original ticket's requirement for clear documentation of output produced when exiting via different mechanisms.

## Current State Analysis
- Permanent sprint documentation templates exist in `knowledge/architecture/sprints/`
- Each template contains sections for: Agreements, Strengths, Concerns, Unresolved Critic Concerns, Exit Status, and Notes on Exit Mechanism
- However, no code exists to populate these templates with actual sprint data during runs
- Sprint data currently appears in generator-history.md and critic-history.md, but not in structured sprint documentation format

## Desired End State
After each sprint completes (whether via quality criteria, max rounds fallback, or other exit paths):
1. A completed sprint document is generated in the sprint's output directory
2. The document follows the template structure with actual data filled in:
   - Agreements: Consensus points between Generator and Critic
   - Strengths: What worked well in the design
   - Concerns: Issues identified during the sprint
   - Unresolved Critic Concerns: Specific criteria where Critic remained concerned
   - Exit Status: Whether sprint passed via quality criteria, max rounds, or failed
   - Notes on Exit Mechanism: Documentation of what output was produced and why
3. The documentation serves as a clear record of the adversarial process for human evaluation

## What We're NOT Doing
- Modifying the existing sprint template structure in `knowledge/architecture/sprints/`
- Changing the fundamental harness execution flow
- Altering how generator/critic history is currently recorded
- Modifying exit criteria logic or decision-making processes

## Implementation Approach
Add sprint documentation generation functionality to:
1. Create a new function in `deep_architect/io/files.py` to generate sprint documentation
2. Call this function at the end of each sprint in `harness.py` after sprint completion
3. The function will:
   - Read the appropriate sprint template from `knowledge/architecture/sprints/`
   - Extract relevant data from sprint runs (history files, progress tracking)
   - Fill in the template sections with actual sprint data
   - Save the completed document to the sprint's output directory

## Phase 1: Sprint Documentation Generation Function

### Overview
Create the core function to generate sprint documentation by reading templates and filling them with sprint data.

### Changes Required:

#### 1. deep_architect/io/files.py
**File**: `deep_architect/io/files.py`
**Changes**: Add new function `generate_sprint_documentation`

### Success Criteria:

#### Automated Verification:
- [x] Function compiles without errors: `python -m py_compile deep_architect/io/files.py`
- [x] Unit tests pass: `python -m pytest tests/test_sprint_documentation.py -v`
- [x] Type checking passes: `mypy deep_architect/io/files.py`
- [x] Linting passes: `ruff check deep_architect/io/files.py`

#### Manual Verification:
- [x] Generated sprint documentation contains filled-in sections for Agreements, Strengths, Concerns, etc.
- [x] Exit status correctly reflects how the sprint actually terminated
- [x] Notes on exit mechanism accurately describe the termination reason
- [x] Documentation is saved to the correct output directory with proper naming

---

## Phase 2: Integration with Harness

### Overview
Integrate the sprint documentation generation function into the harness execution flow.

### Changes Required:

#### 1. deep_architect/harness.py
**File**: `deep_architect/harness.py`
**Changes**: 
1. Import the new function from `deep_architect.io.files`
2. Call `generate_sprint_documentation` at the end of each sprint after completion

### Success Criteria:

#### Automated Verification:
- [x] Harness compiles without errors: `python -m py_compile deep_architect/harness.py`
- [x] No syntax errors in modified sections
- [x] Import statements are correct

#### Manual Verification:
- [x] After a sprint completes, sprint documentation appears in the output directory
- [x] Documentation file is named correctly (e.g., `sprint-01-documentation.md`)
- [x] Content reflects actual sprint execution data
- [x] No errors in logs related to documentation generation
- [x] Existing functionality remains intact (commits, progress tracking, etc.)

---

## Phase 3: Testing and Validation

### Overview
Verify the complete implementation works correctly across different sprint exit scenarios.

### Changes Required:
- No direct code changes, but validation through testing

### Success Criteria:

#### Automated Verification:
- [x] All existing tests still pass
- [x] New unit tests for sprint documentation generation pass
- [x] Integration tests verify documentation generation in different scenarios

#### Manual Verification:
**Test Scenario 1: Sprint passes via quality criteria**
- [x] Run harness with settings that allow sprint to pass via quality criteria
- [x] Verify sprint documentation shows exit via quality criteria
- [x] Check that Agreements, Strengths, etc. sections are populated

**Test Scenario 2: Sprint exits via max rounds fallback (accepted)**
- [x] Run harness with low max rounds or stubborn critic settings
- [x] Verify sprint documentation shows exit via max rounds fallback
- [x] Check that best score and round count are documented

**Test Scenario 3: Sprint fails completely**
- [x] Run harness with settings that prevent any progress
- [x] Verify sprint documentation shows failed status
- [x] Check that appropriate error information is captured

**Test Scenario 4: Resume functionality**
- [x] Run harness, interrupt it, then resume
- [x] Verify sprint documentation generation works correctly for resumed sprints
- [x] Check that documentation reflects the complete sprint execution

### References
- Original ticket: `knowledge/tickets/PROJ-0008.md`
- Sprint templates: `knowledge/architecture/sprints/`
- Related ADR: `knowledge/adr/ADR-024-soft-fail-sprint.md`
- Core harness logic: `deep_architect/harness.py`
- IO functions: `deep_architect/io/files.py`