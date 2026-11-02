from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI, OpenAIError, RateLimitError, _base_client

from aidev_agent.packages.langchain_core.models import llm_retry

MODEL = "mock-model"
SECRET = "private-prompt-and-token"
OPTIONS = {
    "model": MODEL,
    "messages": [{"role": "user", "content": SECRET}],
    "tools": [{"type": "function", "function": {
        "name": "local_tool", "parameters": {"type": "object"},
    }}],
}
_IMPORTED_SYNC = _base_client.SyncAPIClient._send_request
_IMPORTED_ASYNC = _base_client.AsyncAPIClient._send_request


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.wall = 1_700_000_000.0
        self.sleeps = []
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, amount):
        with self.lock:
            self.now += amount

    def sleep(self, seconds):
        assert 0 < seconds <= 0.25
        self.sleeps.append(seconds)
        self.advance(seconds)

    async def async_sleep(self, seconds):
        self.sleep(seconds)
        await asyncio.sleep(0)

    def state(self, **kwargs):
        return llm_retry.RetryState(
            clock=self, wall_clock=lambda: self.wall,
            sleep=self.sleep, async_sleep=self.async_sleep,
            jitter=lambda: 0, **kwargs,
        )


@pytest.fixture
def clients():
    opened = []

    def make(mode, handler, max_retries=4):
        transport = httpx.MockTransport(handler)
        if mode == "sync":
            http_client = httpx.Client(transport=transport)
            client = llm_retry.RunScopedOpenAI(api_key=SECRET, http_client=http_client, max_retries=max_retries)
        else:
            http_client = httpx.AsyncClient(transport=transport)
            client = llm_retry.RunScopedAsyncOpenAI(api_key=SECRET, http_client=http_client, max_retries=max_retries)
        # Retain real clients/request loops, only remove unrelated OpenAI backoff.
        if mode == "sync":
            client._sleep_for_retry = lambda **kwargs: None
        else:
            async def no_sleep(**kwargs):
                pass
            client._sleep_for_retry = no_sleep
        opened.append(client)
        return client

    yield make
    for client in opened:
        if isinstance(client, AsyncOpenAI):
            asyncio.run(client.close())
        else:
            client.close()


async def invoke(client, **kwargs):
    result = client.chat.completions.create(**(OPTIONS | kwargs))
    if isinstance(client, AsyncOpenAI):
        return await result
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_exhaustion_cannot_send_a_fallback_model_request(mode, clients):
    models = []

    def handler(request):
        models.append(json.loads(request.content)["model"])
        return rejected()

    client = clients(mode, handler)
    retry = FakeClock().state(max_retries=0)
    with retry.bind():
        with pytest.raises(llm_retry.LLMRateLimitRetryError) as first:
            await invoke(client)
        with pytest.raises(llm_retry.LLMRateLimitRetryError) as fallback:
            await invoke(client, model="fallback-model")
    assert models == [MODEL]
    assert first.value.__cause__ is fallback.value.__cause__
    assert isinstance(first.value.__cause__, RateLimitError)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_langchain_fallback_cannot_bypass_the_exhausted_run_budget(mode, clients):
    from langchain_core.runnables import RunnableLambda

    requests = []

    def handler(request):
        requests.append(json.loads(request.content)["model"])
        return rejected()

    client = clients(mode, handler)
    retry = FakeClock().state(max_retries=0)

    def call_sync(value):
        return client.chat.completions.create(**(OPTIONS | {"model": value}))

    async def call_async(value):
        return await client.chat.completions.create(**(OPTIONS | {"model": value}))

    fallback = RunnableLambda(lambda value: call_sync("fallback"), afunc=lambda value: call_async("fallback"))
    runnable = RunnableLambda(call_sync, afunc=call_async).with_fallbacks([fallback])
    with retry.bind(), pytest.raises(llm_retry.LLMRateLimitRetryError):
        if mode == "sync":
            runnable.invoke(MODEL)
        else:
            await runnable.ainvoke(MODEL)
    assert requests == [MODEL]


def test_actual_wait_overrun_prevents_reserving_more_wait():
    clock = FakeClock()
    state = clock.state(wait_budget=1800)
    deadline = state._reserve_wait(httpx.Headers({"retry-after": "1000"}))
    clock.advance(1801)
    state._wait_sync(deadline)
    assert state.snapshot()["waited_seconds"] == 1801
    assert state._reserve_wait(httpx.Headers({"retry-after": "100"})) is None
    assert state.snapshot()["exhausted"]


def test_late_inflight_limit_response_cannot_start_another_wait():
    state = FakeClock().state(wait_budget=1800)
    assert state._reserve_wait(httpx.Headers({"retry-after": "1801"})) is None
    assert state._reserve_wait(httpx.Headers({"retry-after": "1"})) is None
    assert state.snapshot()["active_waits"] == 0
    assert state.snapshot()["retry_count"] == 0


def success():
    return httpx.Response(200, json={
        "id": "mock-completion", "object": "chat.completion", "created": 0,
        "model": MODEL, "choices": [{"index": 0, "finish_reason": "stop",
                                   "message": {"role": "assistant", "content": "ok"}}],
    })


class RejectedStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closes = 0

    def __iter__(self):
        self.reads += 1
        yield json.dumps({"error": {"message": SECRET, "type": "rate_limit"}}).encode()

    async def __aiter__(self):
        for chunk in self:
            yield chunk

    def close(self):
        self.closes += 1

    async def aclose(self):
        self.close()


def rejected(headers=None, streams=None):
    stream = RejectedStream()
    if streams is not None:
        streams.append(stream)
    return httpx.Response(429, headers=headers or {}, stream=stream)


def phases(events):
    return [event["value"]["phase"] for event in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_plain_openai_clients_are_not_modified_or_subject_to_run_budget(mode):
    assert _base_client.SyncAPIClient._send_request is _IMPORTED_SYNC
    assert _base_client.AsyncAPIClient._send_request is _IMPORTED_ASYNC
    requests = []

    def handler(request):
        requests.append(request)
        return rejected()

    clock = FakeClock()
    state = clock.state(max_retries=0)
    if mode == "sync":
        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as http_client,
            OpenAI(api_key=SECRET, http_client=http_client, max_retries=1) as client,
        ):
            client._sleep_for_retry = lambda **kwargs: None
            with state.bind(), pytest.raises(RateLimitError):
                await invoke(client)
    else:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client,
            AsyncOpenAI(api_key=SECRET, http_client=http_client, max_retries=1) as client,
        ):
            async def no_sleep(**kwargs):
                pass
            client._sleep_for_retry = no_sleep
            with state.bind(), pytest.raises(RateLimitError):
                await invoke(client)
    assert len(requests) == 2
    assert state.snapshot()["retry_count"] == 0
    assert not state.snapshot()["exhausted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_two_429_then_success_reuses_request_and_keeps_credentials(mode, clients):
    clock = FakeClock()
    events, requests, streams = [], [], []
    state = clock.state(on_event=events.append)

    def handler(request):
        requests.append(request)
        return rejected(streams=streams) if len(requests) <= 2 else success()

    client = clients(mode, handler)
    with state.bind():
        result = await invoke(client, stream=False)
    assert result.choices[0].message.content == "ok"
    assert len(requests) == 3
    assert all(request is requests[0] for request in requests)
    assert all(request.content == requests[0].content for request in requests)
    assert json.loads(requests[0].content)["tools"] == OPTIONS["tools"]
    assert requests[0].headers["authorization"] == f"Bearer {SECRET}"
    assert json.loads(requests[0].content)["model"] == MODEL
    assert client.max_retries == 4
    assert all(stream.reads == 1 and stream.closes == 1 for stream in streams)
    assert state.snapshot() == {
        "retry_count": 2, "waited_seconds": 90.0, "active_waits": 0,
        "exhausted": False, "recovered": True,
    }
    assert phases(events).count("waiting") == 2
    assert phases(events).count("resumed") == 2
    assert phases(events)[-1] == "recovered"
    assert max(clock.sleeps) <= 0.25


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_exhaustion_has_seven_sends_and_original_status_cause(mode, clients):
    clock = FakeClock()
    events, requests, streams = [], [], []
    state = clock.state(on_event=events.append)

    def handler(request):
        requests.append(request)
        return rejected(streams=streams)

    client = clients(mode, handler, max_retries=20)
    with state.bind(), pytest.raises(llm_retry.LLMRateLimitRetryError) as caught:
        await invoke(client, stream=True)
    assert isinstance(caught.value, OpenAIError)
    assert not isinstance(caught.value, RateLimitError)
    assert isinstance(caught.value.__cause__, RateLimitError)
    assert caught.value.__cause__.status_code == 429
    assert caught.value.__cause__.body["message"] == SECRET
    assert len(requests) == 7
    assert all(request is requests[0] for request in requests)
    assert all(stream.reads == 1 and stream.closes == 1 for stream in streams)
    assert state.snapshot() == {
        "retry_count": 6, "waited_seconds": 1050.0, "active_waits": 0,
        "exhausted": True, "recovered": False,
    }
    waiting = [event["value"]["waited_seconds"] for event in events
               if event["value"]["phase"] == "waiting"]
    assert waiting == [0, 30, 90, 210, 450, 750]
    assert phases(events)[-1] == "exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_default_no_state_and_path_filter_preserve_original_retries(mode, clients):
    requests = []

    def handler(request):
        requests.append(request)
        return rejected()

    client = clients(mode, handler, max_retries=2)
    with pytest.raises(RateLimitError):
        await invoke(client)
    assert len(requests) == 3
    clock = FakeClock()
    state = clock.state()
    requests.clear()
    with state.bind(), pytest.raises(RateLimitError):
        result = client.embeddings.create(model=MODEL, input=SECRET)
        if mode == "async":
            await result
    assert len(requests) == 3
    assert state.snapshot()["retry_count"] == 0
    assert clock.sleeps == []
    assert llm_retry._current_state.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("failure", [500, "connection"])
async def test_non_429_retries_are_left_to_openai(mode, failure, clients):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            if failure == "connection":
                raise httpx.ConnectError("mock connection failure", request=request)
            return httpx.Response(failure, json={"error": "mock"})
        return success()

    clock = FakeClock()
    state = clock.state()
    with state.bind():
        assert (await invoke(clients(mode, handler))).choices[0].message.content == "ok"
    assert count == 2
    assert state.snapshot()["retry_count"] == 0
    assert clock.sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("headers,expected", [
    ({"retry-after-ms": "1250", "retry-after": "10"}, 1.25),
    ({"retry-after-ms": "invalid", "retry-after": "1.5"}, 1.5),
    ({"retry-after-ms": "nan", "retry-after": "2"}, 2),
    ({"retry-after-ms": "-1", "retry-after": "1"}, 1),
    ({"retry-after": "date"}, 2),
    ({"retry-after": "past-date"}, 0),
    ({"retry-after": "0"}, 0),
    ({"retry-after": "nan"}, 30),
    ({"retry-after": "inf"}, 30),
    ({"retry-after": "-2"}, 30),
    ({"retry-after": "nonsense"}, 30),
    ({}, 30),
])
async def test_header_formats_and_precedence(mode, headers, expected, clients):
    clock = FakeClock()
    headers = dict(headers)
    if headers.get("retry-after") in ("date", "past-date"):
        offset = 2 if headers["retry-after"] == "date" else -2
        headers["retry-after"] = format_datetime(
            datetime.fromtimestamp(clock.wall + offset, timezone.utc), usegmt=True,
        )
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return rejected(headers) if count == 1 else success()

    state = clock.state()
    with state.bind():
        await invoke(clients(mode, handler))
    assert count == 2
    assert state.snapshot()["waited_seconds"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("headers,budget", [
    ({"x-should-retry": "false", "retry-after": "1"}, 1800),
    ({"retry-after": "1801"}, 1800),
    ({"retry-after-ms": "1800001"}, 1800),
    ({"retry-after": "31"}, 30),
    ({}, 29),
])
async def test_no_retry_and_budget_never_shorten_server_delay(mode, headers, budget, clients):
    clock = FakeClock()
    streams, requests = [], []

    def handler(request):
        requests.append(request)
        return rejected(headers, streams)

    state = clock.state(wait_budget=budget)
    with state.bind(), pytest.raises(llm_retry.LLMRateLimitRetryError):
        await invoke(clients(mode, handler), stream=True)
    assert len(requests) == 1
    assert state.snapshot()["retry_count"] == 0
    assert state.snapshot()["exhausted"] is True
    assert all(stream.reads == 1 and stream.closes == 1 for stream in streams)
    assert clock.sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_header_above_fallback_cap_is_not_truncated(mode, clients):
    clock = FakeClock()
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return rejected({"retry-after": "301"}) if count == 1 else success()

    state = clock.state(wait_budget=301)
    with state.bind():
        await invoke(clients(mode, handler))
    assert state.snapshot()["waited_seconds"] == 301


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_cumulative_budget_and_nonnegative_jitter(mode, clients):
    clock = FakeClock()
    requests = []

    def handler(request):
        requests.append(request)
        return rejected()

    state = llm_retry.RetryState(
        wait_budget=91, clock=clock, sleep=clock.sleep, async_sleep=clock.async_sleep,
        jitter=lambda: 1,
    )
    with state.bind(), pytest.raises(llm_retry.LLMRateLimitRetryError):
        await invoke(clients(mode, handler))
    assert len(requests) == 2
    assert state.snapshot()["waited_seconds"] == 31
    assert state.snapshot()["retry_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("source", ["flag", "checker", "pre-send"])
async def test_cancellation_bypasses_connection_retries(mode, source, clients):
    clock = FakeClock()
    events, requests = [], []
    state = clock.state(
        on_event=events.append,
        cancel_checker=(lambda: clock() >= 0.25) if source == "checker" else None,
    )
    if source == "flag":
        def sleep(seconds):
            clock.sleep(seconds)
            state.cancel()

        async def async_sleep(seconds):
            sleep(seconds)
            await asyncio.sleep(0)

        state._sleep = sleep
        state._async_sleep = async_sleep
    elif source == "pre-send":
        state.cancel()

    def handler(request):
        requests.append(request)
        return rejected()

    with state.bind(), pytest.raises(asyncio.CancelledError):
        await invoke(clients(mode, handler, max_retries=20))
    assert len(requests) == (0 if source == "pre-send" else 1)
    assert state.snapshot()["active_waits"] == 0
    assert state.snapshot()["waited_seconds"] <= 0.25
    assert "cancelled" in phases(events)
    assert events[-1]["value"]["phase"] == "cancelled"
    assert events[-1]["value"]["active_waits"] == 0
    assert "resumed" not in phases(events)


@pytest.mark.asyncio
async def test_native_task_cancel_cleans_shared_wait(clients):
    entered = asyncio.Event()

    async def suspended(seconds):
        assert seconds <= 0.25
        entered.set()
        await asyncio.Future()

    state = llm_retry.RetryState(async_sleep=suspended)
    requests = []

    def handler(request):
        requests.append(request)
        return rejected()

    client = clients("async", handler)
    with state.bind():
        task = asyncio.create_task(invoke(client))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.snapshot()["active_waits"] == 0
    with state.bind(), pytest.raises(asyncio.CancelledError):
        await invoke(client)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_cancel_from_resumed_callback_checks_before_send(mode, clients):
    clock = FakeClock()
    requests = []

    def callback(event):
        if event["value"]["phase"] == "resumed":
            state.cancel()

    state = clock.state(on_event=callback)

    def handler(request):
        requests.append(request)
        return rejected({"retry-after": "0"})

    with state.bind(), pytest.raises(asyncio.CancelledError):
        await invoke(clients(mode, handler))
    assert len(requests) == 1
    assert state.snapshot()["active_waits"] == 0


class PartialStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self):
        self.started = False
        self.closed = False

    def __iter__(self):
        self.started = True
        chunk = {"id": "mock", "object": "chat.completion.chunk", "created": 0,
                 "model": MODEL, "choices": [{"index": 0, "delta": {"content": "part"},
                                               "finish_reason": None}]}
        yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
        raise httpx.ReadError("mock partial stream failure")

    async def __aiter__(self):
        for chunk in self:
            yield chunk

    def close(self):
        self.closed = True

    async def aclose(self):
        self.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_partial_200_stream_is_unwrapped_and_never_replayed(mode, clients):
    clock = FakeClock()
    stream_body = PartialStream()
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return rejected({"retry-after": "0"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream_body)

    state = clock.state()
    with state.bind():
        stream = await invoke(clients(mode, handler, max_retries=20), stream=True)
    assert not stream_body.started
    assert state.snapshot()["recovered"] is True
    # Cancellation after headers must not wrap or interfere with the 200 body.
    state.cancel()
    try:
        if mode == "sync":
            iterator = iter(stream)
            assert next(iterator).choices[0].delta.content == "part"
            with pytest.raises(httpx.ReadError):
                next(iterator)
        else:
            iterator = stream.__aiter__()
            assert (await anext(iterator)).choices[0].delta.content == "part"
            with pytest.raises(httpx.ReadError):
                await anext(iterator)
    finally:
        if mode == "sync":
            stream.close()
        else:
            await stream.close()
    assert len(requests) == 2
    assert requests[0] is requests[1]
    assert stream_body.closed
    assert state.snapshot()["retry_count"] == 1


@pytest.mark.asyncio
async def test_nested_bind_and_async_copy_context_isolation(clients):
    outer_clock, inner_clock = FakeClock(), FakeClock()
    outer, inner = outer_clock.state(), inner_clock.state()
    counts = {}

    def handler(request):
        content = json.loads(request.content)["messages"][0]["content"]
        counts[content] = counts.get(content, 0) + 1
        if content == "unbound":
            return rejected({"x-should-retry": "false"})
        return rejected({"retry-after": "0"}) if counts[content] == 1 else success()

    client = clients("async", handler)
    with outer.bind():
        context = copy_context()
        with inner.bind():
            assert llm_retry._current_state.get() is inner
            task = context.run(asyncio.create_task, invoke(
                client, messages=[{"role": "user", "content": "outer"}],
            ))
            await invoke(client, messages=[{"role": "user", "content": "inner"}])
        assert llm_retry._current_state.get() is outer
    assert llm_retry._current_state.get() is None
    await task
    assert outer.snapshot()["retry_count"] == 1
    assert inner.snapshot()["retry_count"] == 1
    with pytest.raises(RateLimitError):
        await invoke(client, messages=[{"role": "user", "content": "unbound"}])


class ScheduledClock(FakeClock):
    def __init__(self):
        super().__init__()
        self.pending = []

    async def async_sleep(self, seconds):
        assert 0 < seconds <= 0.25
        future = asyncio.get_running_loop().create_future()
        self.pending.append((self() + seconds, future))
        await future

    async def drive(self, tasks):
        for _ in range(200):
            # Drain runnable tasks before advancing time, so simultaneous waits
            # observe the same instant instead of each donating simulated time.
            for _ in range(100):
                await asyncio.sleep(0)
            if all(task.done() for task in tasks):
                return
            pending = [(deadline, future) for deadline, future in self.pending if not future.done()]
            assert pending
            next_time = min(deadline for deadline, _ in pending)
            self.advance(next_time - self())
            for deadline, future in pending:
                if deadline <= self():
                    future.set_result(None)
        pytest.fail("mock scheduler did not finish")


@pytest.mark.asyncio
async def test_parallel_union_wait_and_conservative_sum_budget(clients):
    clock = ScheduledClock()
    events, counts = [], {}
    state = clock.state(max_retries=6, wait_budget=3, on_event=events.append)

    def handler(request):
        label = json.loads(request.content)["messages"][0]["content"]
        counts[label] = counts.get(label, 0) + 1
        if label == "third" or counts[label] == 1:
            return rejected({"retry-after": "2" if label == "long" else "1"})
        return success()

    client = clients("async", handler)
    with state.bind():
        tasks = [asyncio.create_task(invoke(client, messages=[{"role": "user", "content": label}]))
                 for label in ("long", "short")]
    await clock.drive(tasks)
    await asyncio.gather(*tasks)
    assert any(event["value"]["active_waits"] == 2 for event in events)
    assert state.snapshot()["waited_seconds"] == 2  # Not 1 + 2.
    assert state.snapshot()["retry_count"] == 2
    with state.bind(), pytest.raises(llm_retry.LLMRateLimitRetryError):
        await invoke(client, messages=[{"role": "user", "content": "third"}])
    assert counts["third"] == 1  # The overlap did not refund a second.
    assert state.snapshot()["waited_seconds"] == 2
    assert state.snapshot()["active_waits"] == 0
    sequences = [event["value"]["sequence"] for event in events]
    assert sorted(sequences) == list(range(1, len(sequences) + 1))


@pytest.mark.asyncio
async def test_parallel_run_aggregate_retry_limit(clients):
    clock = ScheduledClock()
    counts = {}
    state = clock.state(max_retries=6)

    def handler(request):
        label = json.loads(request.content)["messages"][0]["content"]
        counts[label] = counts.get(label, 0) + 1
        return rejected({"retry-after": "1"})

    client = clients("async", handler, max_retries=20)
    with state.bind():
        tasks = [asyncio.create_task(invoke(client, messages=[{"role": "user", "content": label}]))
                 for label in ("a", "b")]
    await clock.drive(tasks)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, llm_retry.LLMRateLimitRetryError) for result in results)
    # A concurrent exhaustion can cancel a reserved retry before its send.
    assert set(counts) == {"a", "b"}
    assert 2 <= sum(counts.values()) <= 8  # Two initial sends, at most six retries.
    assert state.snapshot()["retry_count"] == 6
    assert state.snapshot()["waited_seconds"] == 3
    assert state.snapshot()["active_waits"] == 0


def test_sync_threads_copy_context_share_atomic_retry_limit(clients):
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    requests, events = [], []
    state = llm_retry.RetryState(max_retries=6, on_event=events.append)

    def handler(request):
        with lock:
            requests.append(request)
            first = len(requests) <= 2
        if first:
            barrier.wait(timeout=5)
        return rejected({"retry-after": "0"})

    client = clients("sync", handler, max_retries=20)

    def run():
        with pytest.raises(llm_retry.LLMRateLimitRetryError):
            client.chat.completions.create(**OPTIONS)

    with ThreadPoolExecutor(max_workers=2) as pool:
        with state.bind():
            futures = [pool.submit(copy_context().run, run) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)
    assert len(requests) == 8
    assert state.snapshot()["retry_count"] == 6
    assert state.snapshot()["active_waits"] == 0
    sequences = [event["value"]["sequence"] for event in events]
    assert sorted(sequences) == list(range(1, len(sequences) + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_callback_privacy_reentrancy_and_heartbeat_frequency(mode, clients):
    clock = FakeClock()
    events, times = [], []
    calls = 0

    def callback(event):
        # Would deadlock if the wrapper held its state lock during delivery.
        state.snapshot()
        events.append(event)
        times.append(clock())

    state = clock.state(on_event=callback)

    def handler(request):
        nonlocal calls
        calls += 1
        return rejected({"retry-after": "2"}) if calls == 1 else success()

    with state.bind():
        await invoke(clients(mode, handler))
    allowed = {"sequence", "phase", "retry_count", "waited_seconds", "active_waits",
               "exhausted", "recovered"}
    for event in events:
        assert set(event) == {"type", "name", "value"}
        assert event["type"] == "CUSTOM"
        assert event["name"] == "llm_rate_limit_retry"
        assert set(event["value"]) == allowed
    serialized = json.dumps(events)
    for forbidden in (SECRET, MODEL, "authorization", "messages", "tools", "https", "/chat/completions"):
        assert forbidden not in serialized
    assert phases(events) == ["waiting", "heartbeat", "heartbeat", "heartbeat", "resumed", "recovered"]
    assert max(b - a for a, b in zip(times, times[1:])) <= 0.5
    assert [event["value"]["sequence"] for event in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_callback_failure_cannot_trigger_connection_replay(mode, clients):
    clock = FakeClock()
    calls = 0

    def callback(event):
        raise RuntimeError("mock telemetry failure")

    def handler(request):
        nonlocal calls
        calls += 1
        return rejected({"retry-after": "0"}) if calls == 1 else success()

    state = clock.state(on_event=callback)
    with state.bind():
        await invoke(clients(mode, handler))
    assert calls == 2
    assert state.snapshot()["recovered"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_retry_limit_is_shared_across_sequential_sdk_calls(mode, clients):
    clock = FakeClock()
    counts = {}
    state = clock.state(max_retries=2)

    def handler(request):
        label = json.loads(request.content)["messages"][0]["content"]
        counts[label] = counts.get(label, 0) + 1
        return rejected({"retry-after": "0"}) if counts[label] == 1 else success()

    client = clients(mode, handler)
    with state.bind():
        for label in ("first", "second"):
            await invoke(client, messages=[{"role": "user", "content": label}])
        with pytest.raises(llm_retry.LLMRateLimitRetryError):
            await invoke(client, messages=[{"role": "user", "content": "third"}])
    assert counts == {"first": 2, "second": 2, "third": 1}
    assert state.snapshot()["retry_count"] == 2
    # Both facts survive: a prior request recovered, a later one exhausted.
    assert state.snapshot()["recovered"] and state.snapshot()["exhausted"]


def test_actual_sync_event_wait_is_interruptible(clients):
    entered = threading.Event()
    state = llm_retry.RetryState()
    slices = []
    event_wait = state._sleep

    def tracked_wait(seconds):
        slices.append(seconds)
        entered.set()
        return event_wait(seconds)

    state._sleep = tracked_wait
    requests = []

    def handler(request):
        requests.append(request)
        return rejected()

    client = clients("sync", handler, max_retries=20)

    def run():
        client.chat.completions.create(**OPTIONS)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with state.bind():
            future = pool.submit(copy_context().run, run)
        try:
            assert entered.wait(timeout=5)
        finally:
            state.cancel()
        with pytest.raises(asyncio.CancelledError):
            future.result(timeout=5)
    assert len(requests) == 1
    assert slices and max(slices) <= 0.25
    assert state.snapshot()["active_waits"] == 0


class ThreadClock(FakeClock):
    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()
        self.sleeping = {}
        self.finished = 0

    def sleep(self, seconds):
        assert 0 < seconds <= 0.25
        ident = threading.get_ident()
        with self.condition:
            deadline = self() + seconds
            self.sleeping[ident] = deadline
            self.condition.notify_all()
            assert self.condition.wait_for(lambda: self() >= deadline, timeout=5)
            del self.sleeping[ident]
            self.condition.notify_all()

    def drive(self, workers):
        with self.condition:
            while self.finished < workers:
                assert self.condition.wait_for(
                    lambda: self.finished == workers or (
                        len(self.sleeping) == workers - self.finished
                        and all(deadline > self() for deadline in self.sleeping.values())
                    ), timeout=5,
                )
                if self.finished == workers:
                    break
                self.advance(min(self.sleeping.values()) - self())
                self.condition.notify_all()

    def finish(self):
        with self.condition:
            self.finished += 1
            self.condition.notify_all()


def test_sync_thread_union_uses_shared_clock_and_no_budget_refund(clients):
    clock = ThreadClock()
    events, counts = [], {}
    lock = threading.Lock()
    state = clock.state(wait_budget=3, on_event=events.append)

    def handler(request):
        label = json.loads(request.content)["messages"][0]["content"]
        with lock:
            counts[label] = counts.get(label, 0) + 1
            count = counts[label]
        if count == 1:
            return rejected({"retry-after": "2" if label == "long" else "1"})
        return success()

    client = clients("sync", handler)

    def run(label):
        try:
            return client.chat.completions.create(**(OPTIONS | {
                "messages": [{"role": "user", "content": label}],
            }))
        finally:
            clock.finish()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with state.bind():
            futures = [pool.submit(copy_context().run, run, label) for label in ("long", "short")]
        clock.drive(2)
        for future in futures:
            assert future.result(timeout=5).choices[0].message.content == "ok"
    assert state.snapshot()["waited_seconds"] == 2
    assert state.snapshot()["retry_count"] == 2
    assert any(event["value"]["active_waits"] == 2 for event in events)
    with state.bind(), pytest.raises(llm_retry.LLMRateLimitRetryError):
        client.chat.completions.create(**(OPTIONS | {
            "messages": [{"role": "user", "content": "third"}],
        }))
    assert counts["third"] == 1
    assert state.snapshot()["waited_seconds"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_read_and_close_429_even_when_read_fails(mode, clients):
    class BrokenRejectedStream(RejectedStream):
        def __iter__(self):
            self.reads += 1
            raise httpx.ReadError("mock refused body read failure")
            yield b""  # Keep this a byte-stream iterator.

    body = BrokenRejectedStream()
    clock = FakeClock()
    state = clock.state(max_retries=0)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, stream=body)

    client = clients(mode, handler, max_retries=20)
    with state.bind(), pytest.raises(llm_retry.LLMRateLimitRetryError) as caught:
        await invoke(client, stream=True)
    assert isinstance(caught.value.__cause__, RateLimitError)
    assert caught.value.__cause__.status_code == 429
    assert len(requests) == 1
    assert body.reads == 1 and body.closes == 1
    assert state.snapshot()["active_waits"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_wait_deadline_includes_callback_elapsed_time(mode, clients):
    clock = FakeClock()
    calls = 0

    def callback(event):
        if event["value"]["phase"] == "waiting":
            clock.advance(0.75)

    def handler(request):
        nonlocal calls
        calls += 1
        return rejected({"retry-after": "1"}) if calls == 1 else success()

    state = clock.state(wait_budget=1, on_event=callback)
    with state.bind():
        await invoke(clients(mode, handler))
    assert clock.sleeps == [0.25]
    assert state.snapshot()["waited_seconds"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_heartbeat_callback_cancel_does_not_sleep_again(mode, clients):
    clock = FakeClock()
    requests = []

    def callback(event):
        if event["value"]["phase"] == "heartbeat":
            state.cancel()

    def handler(request):
        requests.append(request)
        return rejected({"retry-after": "1"})

    state = clock.state(on_event=callback)
    with state.bind(), pytest.raises(asyncio.CancelledError):
        await invoke(clients(mode, handler))
    assert clock.sleeps == [0.25, 0.25]
    assert len(requests) == 1
    assert state.snapshot()["waited_seconds"] == 0.5
    assert state.snapshot()["active_waits"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("factory", [False, True])
async def test_chat_model_uses_native_clients_and_preserves_http_configuration(mode, factory):
    from aidev_agent.packages.langchain_core.models.llm_gateway import ChatModel

    requests = []

    def handler(request):
        requests.append(request)
        return rejected({"retry-after": "0"}) if len(requests) == 1 else success()

    sync_http = httpx.Client(transport=httpx.MockTransport(handler))
    async_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs = dict(model=MODEL, api_key=SECRET, base_url="https://gateway.invalid/v1",
                  http_client=sync_http, http_async_client=async_http, max_retries=20,
                  default_headers={"X-Session-ID": "retry-session"})
    model = ChatModel.get_setup_instance(**kwargs) if factory else ChatModel(**kwargs)
    try:
        assert isinstance(model.root_client, llm_retry.RunScopedOpenAI)
        assert isinstance(model.root_async_client, llm_retry.RunScopedAsyncOpenAI)
        assert model.http_client is sync_http
        assert model.http_async_client is async_http
        assert model._owns_http_async_client is False
        clock = FakeClock()
        state = clock.state(max_retries=1)
        with state.bind():
            result = model.invoke("hello") if mode == "sync" else await model.ainvoke("hello")
        assert result.content == "ok"
        assert len(requests) == 2
        assert requests[0] is requests[1]
        assert requests[0].headers["authorization"] == f"Bearer {SECRET}"
        assert requests[0].headers["X-Session-ID"] == "retry-session"
        if factory:
            assert "X-Bkapi-Authorization" in requests[0].headers
        assert model.root_client.max_retries == 20
        assert state.snapshot()["retry_count"] == 1
    finally:
        sync_http.close()
        await async_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_chat_model_fallback_shares_exhausted_budget(mode):
    from aidev_agent.packages.langchain_core.models.llm_gateway import ChatModel

    requested_models = []

    def handler(request):
        requested_models.append(json.loads(request.content)["model"])
        return rejected()

    sync_http = httpx.Client(transport=httpx.MockTransport(handler))
    async_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = ChatModel.get_setup_instance(
        model=MODEL, fallback_model="fallback-model", api_key=SECRET,
        base_url="https://gateway.invalid/v1",
        http_client=sync_http, http_async_client=async_http, max_retries=20,
    )
    try:
        state = FakeClock().state(max_retries=0)
        with state.bind(), pytest.raises(llm_retry.LLMRateLimitRetryError):
            if mode == "sync":
                model.invoke("hello")
            else:
                await model.ainvoke("hello")
        assert requested_models == [MODEL]
    finally:
        sync_http.close()
        await async_http.aclose()


def test_chat_model_preserves_explicit_completion_clients():
    from aidev_agent.packages.langchain_core.models.llm_gateway import ChatModel

    sync = object()
    async_ = object()
    model = ChatModel(model=MODEL, client=sync, async_client=async_)
    assert model.client is sync
    assert model.async_client is async_
    assert model.root_client is None
    assert model.root_async_client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_chat_model_partial_stream_is_not_replayed(mode):
    from aidev_agent.packages.langchain_core.models.llm_gateway import ChatModel

    requests = []
    stream = PartialStream()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    sync_http = httpx.Client(transport=httpx.MockTransport(handler))
    async_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = ChatModel(
        model=MODEL, api_key=SECRET, base_url="https://gateway.invalid/v1",
        http_client=sync_http, http_async_client=async_http, max_retries=20,
    )
    state = FakeClock().state()
    chunks = []
    try:
        with state.bind(), pytest.raises(httpx.ReadError):
            if mode == "sync":
                for chunk in model.stream("hello"):
                    chunks.append(chunk.content)
            else:
                async for chunk in model.astream("hello"):
                    chunks.append(chunk.content)
        assert chunks == ["part"]
        assert len(requests) == 1
        assert state.snapshot()["retry_count"] == 0
        assert stream.closed
    finally:
        sync_http.close()
        await async_http.aclose()
