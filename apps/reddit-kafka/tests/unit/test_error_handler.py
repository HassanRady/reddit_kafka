"""Unit tests for ErrorHandler."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientConnectorError
from asyncprawcore.exceptions import (
    NotFound,
    RequestException,
    ResponseException,
    TooManyRequests,
)

from src.stream.error_handler import ErrorHandler, RecoveryStrategy
from src.stream.exceptions import CircuitOpenError, KafkaDeliveryError


class TestErrorHandlerRecordError:
    """Test error recording."""

    @pytest.mark.asyncio
    async def test_record_error_redis(self, redis_mock):
        handler = ErrorHandler(redis_client=redis_mock)
        await handler.record_error(
            stream_id="stream-1",
            error_type="ConnectionError",
            error_message="Connection timeout",
            is_recoverable=True,
        )
        redis_mock.lpush.assert_called_once()
        redis_mock.ltrim.assert_called_once()
        redis_mock.expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_error_with_db_persistence(
        self, redis_mock, session_mock, session_maker_mock
    ):
        handler = ErrorHandler(
            redis_client=redis_mock, session_maker=session_maker_mock
        )
        await handler.record_error(
            stream_id="stream-1",
            error_type="TooManyRequests",
            error_message="Rate limited",
            is_recoverable=True,
        )
        redis_mock.lpush.assert_called_once()
        session_mock.execute.assert_called()
        session_mock.commit.assert_called()
        parameters = session_mock.execute.call_args.args[1]
        assert isinstance(parameters["timestamp"], datetime)
        assert parameters["timestamp"].tzinfo is None

    @pytest.mark.asyncio
    async def test_get_error_count(self, redis_mock):
        redis_mock.llen = AsyncMock(return_value=3)
        handler = ErrorHandler(redis_client=redis_mock)
        count = await handler.get_error_count("stream-1")
        assert count == 3
        redis_mock.llen.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_errors(self, redis_mock):
        redis_mock.delete = AsyncMock()
        handler = ErrorHandler(redis_client=redis_mock)
        await handler.clear_errors("stream-1")
        redis_mock.delete.assert_called_once()


class TestRecoveryStrategy:
    """Test recovery strategy determination."""

    def test_should_retry_transient_for_transient_error(self):
        class ServiceConnectionError(ConnectionError):
            pass

        error = ServiceConnectionError("Network error")
        assert RecoveryStrategy.should_retry_transient(error)

    def test_should_retry_transient_for_connector_error(self):
        error = ClientConnectorError(None, OSError("Connection refused"))
        assert RecoveryStrategy.should_retry_transient(error)

    def test_should_retry_transient_for_wrapped_request_error(self):
        error = RequestException(OSError("Connection refused"), (), {})
        assert RecoveryStrategy.should_retry_transient(error)

    def test_should_retry_transient_for_response_exception_subclass(self):
        class TemporaryResponseError(ResponseException):
            pass

        error = TemporaryResponseError(MagicMock())
        assert RecoveryStrategy.should_retry_transient(error)

    def test_should_abandon_stream_for_kafka_delivery_error(self):
        error = KafkaDeliveryError("Broker rejected message")
        assert RecoveryStrategy.should_abandon_stream(error)

    def test_should_retry_with_backoff_for_rate_limit(self):
        class CustomTooManyRequests(TooManyRequests):
            pass

        error = CustomTooManyRequests(MagicMock())
        assert RecoveryStrategy.should_retry_with_backoff(error)
        assert ErrorHandler.get_backoff_duration(error) == 60

    def test_should_retry_with_backoff_for_open_circuit(self):
        error = CircuitOpenError(retry_after=17)

        assert RecoveryStrategy.should_retry_with_backoff(error)
        assert ErrorHandler.is_retryable(error)
        assert ErrorHandler.get_backoff_duration(error) == 17

    def test_should_abandon_stream_for_fatal_error(self):
        class CustomNotFound(NotFound):
            pass

        error = CustomNotFound(MagicMock())
        assert RecoveryStrategy.should_abandon_stream(error)
        assert not ErrorHandler.is_retryable(error)
