from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anthropic

from deep_architect.logger import get_logger

_log = get_logger(__name__)


@dataclass
class CircuitBreakerState:
    """State tracking for circuit breaker pattern per agent role."""
    consecutive_failures: int = 0
    failure_timestamps: list[datetime] = field(default_factory=list)
    last_error: Exception | None = None
    last_attempt: datetime | None = None
    agent_role: str = ""
    model: str = ""
    
    def record_failure(self, error: Exception) -> None:
        """Record a failure and update state."""
        self.consecutive_failures += 1
        self.failure_timestamps.append(datetime.now(UTC))
        self.last_error = error
        self.last_attempt = datetime.now(UTC)
    
    def reset(self) -> None:
        """Reset on success."""
        self.consecutive_failures = 0
        self.failure_timestamps.clear()
        self.last_error = None
    
    def is_open(self, threshold: int) -> bool:
        """Check if circuit should be open (too many failures)."""
        return self.consecutive_failures >= threshold


class ModelCommunicationError(Exception):
    """Raised when circuit breaker opens due to consecutive failures."""
    
    def __init__(self, message: str, failures: int, last_error: Exception,
                 agent_role: str, model: str, timestamps: list[datetime]):
        self.failures = failures
        self.last_error = last_error
        self.agent_role = agent_role
        self.model = model
        self.timestamps = timestamps
        
        detail = (
            f"Circuit breaker opened for {agent_role} ({model}): "
            f"{failures} consecutive failures between "
            f"{timestamps[0].isoformat() if timestamps else 'unknown'} and "
            f"{timestamps[-1].isoformat() if timestamps else 'unknown'}. "
            f"Last error: {last_error}"
        )
        super().__init__(detail)


def classify_error(exc: Exception) -> tuple[str, bool]:
    """Classify error as 'transient' (retryable) or 'permanent'.
    
    Returns:
        (category, is_retryable): 'transient' or 'permanent', and whether to retry
    """
    # Transient: network, rate limits, timeouts, CLI crashes
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return ("transient", True)
    
    if isinstance(exc, anthropic.RateLimitError):
        return ("transient", True)
    
    if isinstance(exc, anthropic.APIError):
        if getattr(exc, "status_code", None) in (429, 503):
            return ("transient", True)
        elif getattr(exc, "status_code", None) in (401, 403):
            return ("permanent", False)
        else:
            return ("transient", True)  # 5xx errors are retryable
    
    if isinstance(exc, anthropic.APIConnectionError):
        return ("transient", True)
    
    # CLI subprocess failures are usually transient (network, disk I/O)
    if isinstance(exc, RuntimeError) and "exit code" in str(exc).lower():
        return ("transient", True)
    
    # Default: treat as transient to be safe
    return ("transient", True)


def calculate_backoff(
    attempt: int,
    base_seconds: float = 1.0,
    max_seconds: float = 60.0,
    jitter: bool = True,
) -> float:
    """Calculate exponential backoff with optional jitter.
    
    Args:
        attempt: Current attempt number (1-indexed)
        base_seconds: Starting backoff (e.g., 1.0)
        max_seconds: Maximum cap (e.g., 60.0)
        jitter: Add up to 10% random jitter to prevent thundering herd
    
    Returns:
        Backoff delay in seconds
    """
    # Exponential: 1, 2, 4, 8, 16, 32, 60, 60...
    backoff = min(base_seconds * (2.0 ** (attempt - 1)), max_seconds)
    
    if jitter:
        # Add up to 10% jitter
        jitter_amount = backoff * 0.1 * random.random()
        backoff += jitter_amount
    
    return backoff


async def execute_with_circuit_breaker(
    coro_factory: Any,
    circuit_state: CircuitBreakerState,
    max_retries: int,
    failure_threshold: int,
    base_backoff: float,
    max_backoff: float,
    label: str,
) -> Any:
    """Execute a coroutine with circuit breaker protection.
    
    Args:
        coro_factory: Callable that returns the coroutine to execute
        circuit_state: Shared state for this agent role
        max_retries: Number of retry attempts
        failure_threshold: Failures before opening circuit
        base_backoff: Starting backoff in seconds
        max_backoff: Maximum backoff in seconds
        label: Log label (e.g., "Generator", "Critic")
    
    Returns:
        Result from coroutine
    
    Raises:
        ModelCommunicationError: When circuit opens (failures >= threshold)
        PermanentModelError: When error is non-retryable
    """
    last_exc: Exception | None = None
    
    for attempt in range(1, max_retries + 2):
        circuit_state.last_attempt = datetime.now(UTC)
         
        try:
            result = await coro_factory()
            # Success: reset circuit breaker
            circuit_state.reset()
            _log.info("[%s] Success after %d attempt(s)", label, attempt)
            return result
             
        except Exception as exc:
            category, is_retryable = classify_error(exc)
            last_exc = exc
             
            if not is_retryable:
                # Permanent error: fail immediately
                _log.error(
                    "[%s] Permanent error (no retry): %s",
                    label, exc
                )
                raise
             
            # Transient error: record and potentially backoff
            circuit_state.record_failure(exc)
             
            if circuit_state.is_open(failure_threshold):
                # Open circuit
                raise ModelCommunicationError(
                    message="Transient failures exceeded threshold",
                    failures=circuit_state.consecutive_failures,
                    last_error=exc,
                    agent_role=circuit_state.agent_role,
                    model=circuit_state.model,
                    timestamps=circuit_state.failure_timestamps,
                ) from exc
             
            # Calculate backoff
            backoff = calculate_backoff(
                attempt, base_backoff, max_backoff
            )
             
            _log.error(
                "[%s] Transient failure %d/%d (attempt %d/%d): %s\n"
                "  Backing off %.1fs before retry (next attempt in %.1fs)",
                label,
                circuit_state.consecutive_failures, failure_threshold,
                attempt, max_retries + 1,
                exc,
                backoff,
                backoff,
            )
             
            # Defense-in-depth: drain any anyio cancel-scope cancels that leaked
            # from the previous attempt so they don't detonate this sleep.
            _task = asyncio.current_task()
            if _task is not None:
                while _task.cancelling() > 0:
                    _task.uncancel()
            await asyncio.sleep(backoff)
     
    # Should be unreachable (either returns or raises), but satisfy type checker
    if last_exc:
        raise last_exc