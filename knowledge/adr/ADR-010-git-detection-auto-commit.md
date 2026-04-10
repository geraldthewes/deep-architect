# ADR-010: Auto-Commit After Each Generator Pass via Git Status Detection

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

After the generator writes architecture files, the harness needs to know what was written and preserve it. The generator could report what it wrote, or the harness could detect changes independently.

## Decision

After each generator pass, the harness:
1. Calls `get_modified_files(repo)` which runs `git status` to detect new/modified files
2. Stages and commits those files with message: `"Generator pass {round} - sprint {N} ({Sprint Name})"`

The output directory must be inside a git repository (validated at startup by `validate_git_repo()`).

## Rationale

- **Git is authoritative:** `git status` reliably reports exactly what changed. The generator does not need to track or report its own writes — that would be error-prone.
- **Audit trail:** Each round creates a separate commit. Reviewers can inspect how the architecture evolved: `git log --oneline knowledge/architecture/`
- **Recovery:** If a sprint goes wrong, users can `git revert` the last N commits to roll back to a good state.
- **No extra bookkeeping:** Using the git repo that already contains the PRD input avoids creating a separate artifact store.

## Consequences

- The target output directory must be a git repo. `validate_git_repo()` raises `ValueError` if it is not.
- The harness calls `git_commit()` after every successful generator pass, even in early rounds that the critic might reject.
- Commit history faithfully records all iterations including failed ones (critic may score round 1 poorly; that commit still exists).
- Users can see the full architectural evolution via `git log`.

**Files:** `deep_researcher/git_ops.py`, `deep_researcher/harness.py:285-299`
