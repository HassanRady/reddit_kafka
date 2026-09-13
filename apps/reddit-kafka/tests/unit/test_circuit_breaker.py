from datetime import UTC, datetime, timedelta

import pytest

from src.stream.circuit_breaker import CircuitBreaker, CircuitState


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_open_circuit_does_not_invoke_callable(self):
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
        breaker.state = CircuitState.OPEN
        breaker.last_failure_time = datetime.now(UTC)

        called = False

        async def coro_factory():
            nonlocal called
            called = True
            return "ok"

        with pytest.raises(RuntimeError, match="Circuit breaker OPEN"):
            await breaker.call(coro_factory)

        assert not called

    @pytest.mark.asyncio
    async def test_failure_opens_circuit_after_threshold(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60)

        async def failing_call():
            raise ConnectionError("boom")

        with pytest.raises(ConnectionError):
            await breaker.call(failing_call)

        assert breaker.state == CircuitState.CLOSED

        with pytest.raises(ConnectionError):
            await breaker.call(failing_call)

        assert breaker.state == CircuitState.OPEN
        assert breaker.fail_count == 2

    @pytest.mark.asyncio
    async def test_half_open_failure_increases_backoff(self):
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10)
        breaker.state = CircuitState.OPEN
        breaker.last_failure_time = datetime.now(UTC) - timedelta(seconds=11)

        async def failing_call():
            raise ConnectionError("boom")

        with pytest.raises(ConnectionError):
            await breaker.call(failing_call)

        assert breaker.state == CircuitState.OPEN
        assert breaker.backoff_seconds == 20

    @pytest.mark.asyncio
    async def test_rate_limit_error_sets_suggested_backoff(self):
        class TooManyRequestsError(Exception):
            pass

        TooManyRequestsError.__name__ = "TooManyRequests"

        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10)

        async def failing_call():
            raise TooManyRequestsError("rate limited")

        with pytest.raises(TooManyRequestsError):
            await breaker.call(failing_call)

        assert breaker.state == CircuitState.OPEN
        # ErrorHandler.get_backoff_duration returns 60 for TooManyRequests,
        # and the breaker picks the max(suggested, backoff*multiplier)
        assert breaker.backoff_seconds == 60
