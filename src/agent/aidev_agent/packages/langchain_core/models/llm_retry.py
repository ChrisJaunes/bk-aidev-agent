"""Opt-in, run-scoped retries at the OpenAI HTTP send boundary."""

from __future__ import annotations

import asyncio
import math
import random
import threading
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, Iterator

import httpx
from openai import AsyncOpenAI, OpenAI, OpenAIError

_current_state: ContextVar[RetryState | None] = ContextVar("llm_retry_state", default=None)


class LLMRateLimitRetryError(OpenAIError):
    """A handled 429 must not enter OpenAI's or the SDK's rate-limit retry loop."""


class RetryState:
    def __init__(
        self,
        max_retries: int = 6,
        wait_budget: float = 1800,
        cancel_checker: Callable[[], bool] | None = None,
        on_event: Callable[[dict], None] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] | None = None,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0, 1),
    ) -> None:
        if not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        if not math.isfinite(wait_budget) or wait_budget < 0:
            raise ValueError("wait_budget must be finite and nonnegative")
        self.max_retries = max_retries
        self.wait_budget = float(wait_budget)
        self._cancel_checker = cancel_checker
        self._on_event = on_event
        self._clock = clock
        self._wall_clock = wall_clock
        self._cancel = threading.Event()
        self._sleep = sleep if sleep is not None else self._cancel.wait
        self._async_sleep = async_sleep
        self._jitter = jitter
        self._lock = threading.Lock()
        self._retry_count = 0
        # Reservations remain charged after completion: overlapping waits never
        # create extra budget just because their wall-clock intervals overlap.
        self._reserved_seconds = 0.0
        self._waited_seconds = 0.0
        self._active_waits = 0
        self._last_clock: float | None = None
        self._exhausted = False
        self._failure: OpenAIError | None = None
        self._recovered = False
        self._sequence = 0
        self._cancelled_emitted = False

    @contextmanager
    def bind(self) -> Iterator[RetryState]:
        token = _current_state.set(self)
        try:
            yield self
        finally:
            _current_state.reset(token)

    def cancel(self) -> None:
        self._cancel.set()

    def _account_locked(self) -> float:
        now = self._clock()
        if self._active_waits and self._last_clock is not None:
            self._waited_seconds += max(0.0, now - self._last_clock)
        self._last_clock = now
        return now

    def _snapshot_locked(self) -> dict:
        self._account_locked()
        return {
            "retry_count": self._retry_count,
            "waited_seconds": self._waited_seconds,
            "active_waits": self._active_waits,
            "exhausted": self._exhausted,
            "recovered": self._recovered,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _event_locked(self, phase: str) -> dict:
        self._sequence += 1
        return {
            "type": "CUSTOM",
            "name": "llm_rate_limit_retry",
            "value": {
                "sequence": self._sequence,
                "phase": phase,
                **self._snapshot_locked(),
            },
        }

    def _deliver(self, event: dict) -> None:
        if self._on_event is not None:
            # Telemetry failure must not cause OpenAI to replay a request.
            with suppress(Exception):
                self._on_event(event)

    def _emit(self, phase: str) -> None:
        with self._lock:
            if phase == "cancelled":
                if self._cancelled_emitted:
                    return
                self._cancelled_emitted = True
            elif phase == "exhausted":
                self._exhausted = True
            elif phase == "recovered":
                self._recovered = True
            event = self._event_locked(phase)
        self._deliver(event)

    def _check_cancelled(self) -> None:
        if self._cancel.is_set() or (
            self._cancel_checker is not None and self._cancel_checker()
        ):
            self.cancel()
            self._emit("cancelled")
            # BaseException bypasses OpenAI's connection-error retry handler,
            # including for synchronous callers.
            raise asyncio.CancelledError()

    def _check_exhausted(self) -> None:
        with self._lock:
            error = self._failure
            exhausted = self._exhausted
        if exhausted:
            raise LLMRateLimitRetryError("LLM rate-limit retry stopped") from error

    def _stop(self, error: OpenAIError) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error
        self._check_exhausted()

    def _reserve_wait(self, headers: httpx.Headers) -> float | None:
        self._check_cancelled()
        header_delay = _retry_after(headers, self._wall_clock())
        jitter = float(self._jitter()) if header_delay is None else 0.0
        if not math.isfinite(jitter) or jitter < 0:
            raise ValueError("jitter must be finite and nonnegative")
        with self._lock:
            self._account_locked()
            delay = header_delay
            if delay is None:
                delay = min(30.0 * 2 ** min(self._retry_count, 4), 300.0) + jitter
            if (
                self._exhausted
                or headers.get("x-should-retry", "").lower() == "false"
                or self._retry_count >= self.max_retries
                or delay > self.wait_budget - max(self._reserved_seconds, self._waited_seconds)
            ):
                self._exhausted = True
                event = self._event_locked("exhausted")
                delay = None
            else:
                started = self._account_locked()
                self._retry_count += 1
                self._reserved_seconds += delay
                self._active_waits += 1
                # The deadline starts with the reservation, not after a callback
                # or scheduling delay; those must not extend the reserved wait.
                deadline = started + delay
                event = self._event_locked("waiting")
        # No callback runs under the state lock, even when it cancels the run or
        # asks for another snapshot. Sequence numbers handle cross-thread delivery.
        try:
            self._deliver(event)
        except BaseException:
            if delay is not None:
                self._finish_wait()
            raise
        return deadline if delay is not None else None

    def _finish_wait(self) -> None:
        with self._lock:
            self._account_locked()
            self._active_waits -= 1
            event = self._event_locked("cancelled" if self._cancel.is_set() else "resumed")
        self._deliver(event)

    def _wait_sync(self, deadline: float) -> None:
        try:
            heartbeat = self._clock() + 0.5
            while True:
                self._check_cancelled()
                now = self._clock()
                if now >= deadline:
                    break
                if now >= heartbeat:
                    heartbeat = now + 0.5
                    self._emit("heartbeat")
                    continue
                self._sleep(min(0.25, deadline - now, max(0.0, heartbeat - now)))
        except asyncio.CancelledError:
            self.cancel()
            self._emit("cancelled")
            raise
        finally:
            self._finish_wait()
        self._check_cancelled()

    async def _wait_async(self, deadline: float) -> None:
        try:
            heartbeat = self._clock() + 0.5
            while True:
                self._check_cancelled()
                now = self._clock()
                if now >= deadline:
                    break
                if now >= heartbeat:
                    heartbeat = now + 0.5
                    self._emit("heartbeat")
                    continue
                await self._async_sleep(
                    min(0.25, deadline - now, max(0.0, heartbeat - now))
                )
        except asyncio.CancelledError:
            self.cancel()
            self._emit("cancelled")
            raise
        finally:
            self._finish_wait()
        self._check_cancelled()


def _retry_after(headers: httpx.Headers, wall_time: float) -> float | None:
    for name, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value) / divisor
        except ValueError:
            if name != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    continue
                seconds = max(0.0, date.timestamp() - wall_time)
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def _status_error(client: OpenAI | AsyncOpenAI, response: httpx.Response):
    try:
        return client._make_status_error_from_response(response)
    except httpx.ResponseNotRead:
        # A failed streamed read can mark the stream consumed without retaining
        # content. Preserve the known status/headers with an unavailable body,
        # still delegating the exception type to the original OpenAI builder.
        unreadable = httpx.Response(
            response.status_code, headers=response.headers,
            request=response.request, content=b"",
        )
        return client._make_status_error_from_response(unreadable)


class RunScopedOpenAI(OpenAI):
    """SDK-owned client; run-scoped 429 handling without global OpenAI patches."""

    def _send_request(self, request: httpx.Request, *, stream: bool, **kwargs) -> httpx.Response:
        state = _current_state.get()
        if state is None or not request.url.path.endswith("/chat/completions"):
            return super()._send_request(request, stream=stream, **kwargs)
        retried = False
        while True:
            state._check_cancelled()
            state._check_exhausted()
            response = super()._send_request(request, stream=stream, **kwargs)
            if response.status_code != 429:
                if retried and 200 <= response.status_code < 300:
                    state._emit("recovered")
                return response
            try:
                with suppress(httpx.HTTPError):
                    response.read()
            finally:
                response.close()
            deadline = state._reserve_wait(response.headers)
            if deadline is None:
                state._stop(_status_error(self, response))
            state._wait_sync(deadline)
            retried = True


class RunScopedAsyncOpenAI(AsyncOpenAI):
    """Asynchronous counterpart sharing the same run budget and cancellation."""

    async def _send_request(self, request: httpx.Request, *, stream: bool, **kwargs) -> httpx.Response:
        state = _current_state.get()
        if state is None or not request.url.path.endswith("/chat/completions"):
            return await super()._send_request(request, stream=stream, **kwargs)
        retried = False
        while True:
            state._check_cancelled()
            state._check_exhausted()
            response = await super()._send_request(request, stream=stream, **kwargs)
            if response.status_code != 429:
                if retried and 200 <= response.status_code < 300:
                    state._emit("recovered")
                return response
            try:
                with suppress(httpx.HTTPError):
                    await response.aread()
            finally:
                await response.aclose()
            deadline = state._reserve_wait(response.headers)
            if deadline is None:
                state._stop(_status_error(self, response))
            await state._wait_async(deadline)
            retried = True
