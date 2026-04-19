# ADR-026: Circuit Breaker Pattern for LLM Provider Communication Failures

**Status:** Accepted  
**Date:** 2026-04-19  
**Deciders:** Project design  

---

## Context

The adversarial-architect system makes frequent LLM provider calls through the Claude Code CLI agent. These calls can fail due to transient issues like network blips, rate limiting, or brief service outages. Without proper error handling, these transient failures could:
- Cause infinite retry loops consuming excessive resources
- Fail the entire architecture generation process unnecessarily
- Provide poor error diagnostics for troubleshooting

The system previously had basic retry logic but lacked:
- Configurable failure thresholds to prevent endless retries
- Exponential backoff to avoid overwhelming the provider during recovery
- Structured error classification (transient vs permanent)
- Detailed logging with timestamps for post-mortem analysis
- Graceful degradation when circuits are opened

## Decision

Implement a circuit breaker pattern with exponential backoff for handling transient LLM provider communication failures, following the specification in PROJ-0007.

### Implementation Details

1. **New Components:**
   - `deep_architect/agents/circuit_breaker.py` module containing:
     - `CircuitBreakerState`: Tracks consecutive failures per agent role
     - `ModelCommunicationError`: Exception raised when circuit opens
     - `classify_error()`: Distinguishes transient vs permanent errors
     - `calculate_backoff()`: Implements exponential backoff with jitter
     - `execute_with_circuit_breaker()`: Async context manager wrapping agent calls

2. **Error Classification:**
   - **Transient (retryable with backoff):** TimeoutError, RateLimitError, APIError with status 429/503, APIConnectionError, CLI subprocess crashes
   - **Permanent (fail immediately):** AuthenticationError, APIError with status 401/403, FileNotFoundError (missing CLI), TurnLimitError

3. **Configuration:**
   - Added to `ThresholdConfig` in `config.py`:
     - `model_comm_failure_threshold`: Consecutive failures before opening circuit (default: 3)
     - `model_comm_base_backoff`: Initial backoff delay in seconds (default: 1.0)
     - `model_comm_max_backoff`: Maximum backoff delay in seconds (default: 60.0)
   - Updated `.deep-architect.toml.template` with these defaults

4. **Integration:**
   - `deep_architect/agents/client.py`: Enhanced `run_agent()` and `run_simple_structured()` to use circuit breaker pattern
   - `deep_architect/harness.py`: Creates circuit breaker states for Generator/Critic and passes them to agent calls
   - `deep_architect/agents/generator.py` and `critic.py`: Updated to accept circuit breaker parameters

5. **Behavior:**
   - Tracks consecutive failures separately for Generator and Critic
   - Implements exponential backoff with jitter: 1s, 2s, 4s, 8s... capped at max_backoff
   - Opens circuit after threshold consecutive failures, preventing further attempts
   - Automatically resets circuit breaker state on successful requests
   - Logs detailed information when circuit opens: failure counts, timestamps, error details
   - Maintains backward compatibility when circuit breaker state is not provided

## Consequences

### Positive
- System survives temporary network outages without hanging or resource exhaustion
- No infinite retry loops (configurable limit, default 3 consecutive failures)
- Clear error logging shows failure timeline for debugging
- Graceful failure with actionable recommendations when circuit opens
- Backward compatibility preserved
- All existing tests continue to pass (no regressions)

### Neutral
- Slight increase in code complexity due to new circuit breaker module
- Minor performance overhead from state tracking and error classification

### Negative
- None identified

## Implementation Plan Status

✅ Phase 1: Core Infrastructure - COMPLETED
- Created circuit_breaker.py module
- Updated config.py, client.py, harness.py, generator.py, critic.py
- Updated .deep-architect.toml.template

✅ Phase 2: Testing - COMPLETED
- Created tests/test_circuit_breaker.py with 15 comprehensive unit tests
- Fixed indentation issue in harness.py
- Verified all existing tests pass (184/184)

✅ Phase 3: Documentation - COMPLETED
- Updated AGENTS.md with circuit breaker behavior
- Updated PROJ-0007 ticket status to "done"

## Verification

- All unit tests pass: `python -m pytest tests/test_circuit_breaker.py -v`
- All existing tests pass: `python -m pytest tests/ -x`
- Manual verification confirms circuit breaker opens after threshold failures
- Backoff timing verified with jitter prevents thundering herd problems
- Permanent errors correctly bypass retry logic
- Legacy mode works when circuit breaker state is None

## References

- PROJ-0007: Circuit Breaker Pattern for Model Communication Failures
- ADR-012: Structured Output JSON Schema (related to agent communication)
- ADR-015: Dual Retry Layers (builds upon existing retry mechanisms)