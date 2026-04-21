"""Async-safe three-state circuit breaker for external provider calls.

States
------
CLOSED    Normal operation; failures increment the counter.
OPEN      Fast-fail for `reset_timeout` seconds; raises CircuitOpen.
HALF_OPEN One trial call is allowed; success → CLOSED, failure → OPEN.

Usage
-----
    breaker = ProviderCircuitBreaker(fail_max=5, reset_timeout=60.0)

    try:
        breaker.before_call()
    except CircuitOpen as e:
        raise ProviderError(f"circuit open: {e}") from e
    try:
        result = await do_something()
        breaker.on_success()
        return result
    except SomeError:
        breaker.on_failure()
        raise
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum


class CircuitOpen(Exception):
    """Raised by ProviderCircuitBreaker.before_call() when the circuit is open."""

    def __init__(self, resets_at: datetime) -> None:
        self.resets_at = resets_at
        secs = max(0, int((resets_at - datetime.now(tz=timezone.utc)).total_seconds()))
        super().__init__(f"circuit open — resets in ~{secs}s (at {resets_at.isoformat()})")


class _State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ProviderCircuitBreaker:
    """Three-state circuit breaker safe for use in async code.

    Not thread-safe by design — asyncio is single-threaded and we don't
    need locks. Do not share an instance across multiple event loops.
    """

    def __init__(self, fail_max: int = 5, reset_timeout: float = 60.0) -> None:
        self._fail_max = fail_max
        self._reset_timeout = timedelta(seconds=reset_timeout)
        self._state = _State.CLOSED
        self._failures = 0
        self._opened_at: datetime | None = None

    @property
    def state(self) -> str:
        return self._state.value

    @property
    def failure_count(self) -> int:
        return self._failures

    def reset(self) -> None:
        """Return to CLOSED state with zero failure count.  Used in tests."""
        self._state = _State.CLOSED
        self._failures = 0
        self._opened_at = None

    # ------------------------------------------------------------------
    # Call lifecycle
    # ------------------------------------------------------------------

    def before_call(self) -> None:
        """Call before each attempt.

        Raises CircuitOpen if the circuit is OPEN and the reset window
        has not elapsed.  Transitions OPEN → HALF_OPEN when the window
        has elapsed, allowing one trial request through.
        """
        self._maybe_half_open()
        if self._state == _State.OPEN:
            assert self._opened_at is not None
            raise CircuitOpen(self._opened_at + self._reset_timeout)

    def on_success(self) -> None:
        """Call after a successful attempt."""
        self._failures = 0
        self._state = _State.CLOSED

    def on_failure(self) -> None:
        """Call after a failed attempt."""
        self._failures += 1
        if self._failures >= self._fail_max or self._state == _State.HALF_OPEN:
            self._state = _State.OPEN
            self._opened_at = datetime.now(tz=timezone.utc)

    # ------------------------------------------------------------------

    def _maybe_half_open(self) -> None:
        if (
            self._state == _State.OPEN
            and self._opened_at is not None
            and datetime.now(tz=timezone.utc) - self._opened_at >= self._reset_timeout
        ):
            self._state = _State.HALF_OPEN
