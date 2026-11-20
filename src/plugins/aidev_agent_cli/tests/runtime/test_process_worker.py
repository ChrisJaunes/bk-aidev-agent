import asyncio
import multiprocessing
import os

import pytest

from aidev_agent_cli.runtime.worker import ProcessWorker


def worker(**snapshot):
    return ProcessWorker(snapshot, entry="tests.runtime.worker_fixtures:build", cancel_grace=0.05, join_grace=0.05)


async def discard(event):
    pass


async def reply(name, payload):
    assert name == "before_execute"
    return payload["value"] + 1


def assert_reaped(instance):
    assert instance.phase == "exited"
    assert instance.exitcode is not None
    assert instance.pid not in {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(ProcessLookupError):
        os.kill(instance.pid, 0)


async def test_spawn_events_and_hook_reply():
    events = []

    async def sink(event):
        events.append(event)

    instance = worker(hook=True)
    result = await instance.run(event_sink=sink, request_handler=reply)
    assert result.status == "finished"
    assert result.value == 4
    assert events[0]["pid"] != os.getpid()
    assert_reaped(instance)


@pytest.mark.parametrize("stage", ["hang_build", "hang_execute"])
@pytest.mark.parametrize("ignore_term", [False, True])
async def test_hung_worker_killed_and_next_run_succeeds(stage, ignore_term):
    instance = worker(**{stage: True, "ignore_term": ignore_term})
    started = asyncio.Event()

    async def sink(event):
        started.set()

    task = asyncio.create_task(instance.run(event_sink=sink, request_handler=reply))
    await asyncio.wait_for(started.wait(), 5)
    instance.cancel()
    result = await asyncio.wait_for(task, 5)
    assert result.status == "cancelled"
    assert result.forced
    assert result.side_effects_warning
    assert_reaped(instance)
    next_worker = worker(value="next")
    next_result = await next_worker.run(event_sink=discard, request_handler=reply)
    assert next_result.status == "finished"
    assert next_result.value["value"] == "next"
    assert_reaped(next_worker)


async def test_sdk_error_evidence_cannot_be_overwritten_by_success():
    instance = worker(error_event=True)
    result = await instance.run(event_sink=discard, request_handler=reply)
    assert result.status == "failed"
    assert_reaped(instance)


async def test_unexpected_process_exit():
    instance = worker(crash=True)
    result = await instance.run(event_sink=discard, request_handler=reply)
    assert result.status == "failed"
    assert instance.exitcode == 17
    assert_reaped(instance)


async def test_parent_task_cancel_reaps_child():
    instance = worker(hang_execute=True, ignore_term=True)
    started = asyncio.Event()

    async def sink(event):
        started.set()

    task = asyncio.create_task(instance.run(event_sink=sink, request_handler=reply))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_reaped(instance)


async def test_slow_hook_cannot_prevent_worker_kill():
    instance = worker(hook=True, ignore_term=True)
    started = asyncio.Event()

    async def blocked_hook(name, value):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(instance.run(event_sink=discard, request_handler=blocked_hook))
    await asyncio.wait_for(started.wait(), 5)
    instance.cancel()
    result = await asyncio.wait_for(task, 5)
    assert result.status == "cancelled"
    assert_reaped(instance)


@pytest.mark.parametrize("entry,snapshot", [("missing", {}), ("module:factory", {"object": object()})])
async def test_invalid_spec_does_not_spawn(entry, snapshot):
    instance = ProcessWorker(snapshot, entry=entry)
    with pytest.raises((ValueError, TypeError)):
        await instance.run(event_sink=discard, request_handler=reply)
    assert instance.pid is None


async def test_result_wait_timeout_preserves_shared_future():
    from aidev_agent_cli.runtime.handles import CancelToken, RunHandle
    from aidev_agent_cli.utils.runs import RunOutcome

    handle = RunHandle(run_id="run-test", session_code="session-test", token=CancelToken())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(handle.result(), 0.01)
    outcome = RunOutcome(status="finished", run_id=handle.run_id, session_code=handle.session_code)
    handle.settle(outcome)
    assert await handle.result() is outcome
    assert await handle.result() is outcome


async def test_repeated_parent_cancel_still_reaps():
    instance = worker(hang_build=True, ignore_term=True)
    started = asyncio.Event()

    async def sink(event):
        started.set()

    task = asyncio.create_task(instance.run(event_sink=sink, request_handler=reply))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_reaped(instance)


async def test_cooperative_cancel_does_not_force_kill():
    instance = worker(cooperate=True)
    started = asyncio.Event()

    async def sink(event):
        started.set()

    task = asyncio.create_task(instance.run(event_sink=sink, request_handler=reply))
    await asyncio.wait_for(started.wait(), 5)
    instance.cancel()
    result = await task
    assert result.status == "cancelled"
    assert not result.forced
    assert_reaped(instance)


async def test_event_sink_failure_reaps_worker():
    instance = worker(hang_execute=True, ignore_term=True)

    async def sink(event):
        raise RuntimeError("sink failed")

    with pytest.raises(RuntimeError, match="sink failed"):
        await instance.run(event_sink=sink, request_handler=reply)
    assert_reaped(instance)


async def test_cleanup_crash_cannot_report_success():
    instance = worker(crash_close=True)
    result = await instance.run(event_sink=discard, request_handler=reply)
    assert result.status == "failed"
    assert instance.exitcode == 19
    assert_reaped(instance)


async def test_two_workers_have_isolated_snapshots_and_events():
    first = worker(value="first")
    second = worker(value="second")
    first_events = []
    second_events = []

    async def first_sink(event):
        first_events.append(event)

    async def second_sink(event):
        second_events.append(event)

    results = await asyncio.gather(
        first.run(event_sink=first_sink, request_handler=reply),
        second.run(event_sink=second_sink, request_handler=reply),
    )
    assert [result.value["value"] for result in results] == ["first", "second"]
    assert first.pid != second.pid
    assert first_events[0]["pid"] == first.pid
    assert second_events[0]["pid"] == second.pid
    assert_reaped(first)
    assert_reaped(second)

async def test_cancel_resistant_callback_cleanup_is_bounded():
    from aidev_agent_cli.runtime.worker import BACKGROUND_CALLBACKS

    instance = worker(hook=True, ignore_term=True)
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_hook(name, value):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release.wait()

    task = asyncio.create_task(instance.run(event_sink=discard, request_handler=blocked_hook))
    await asyncio.wait_for(started.wait(), 5)
    instance.cancel()
    try:
        result = await asyncio.wait_for(task, 2)
        assert result.status == "cancelled"
        assert cleanup_started.is_set()
        assert BACKGROUND_CALLBACKS
        assert_reaped(instance)
        assert (await worker().run(event_sink=discard, request_handler=reply)).status == "finished"
    finally:
        release.set()
        await asyncio.gather(*BACKGROUND_CALLBACKS, return_exceptions=True)
    assert not BACKGROUND_CALLBACKS


async def test_force_kill_cleans_dedicated_child_process_group():
    from pathlib import Path

    instance = worker(child_process=True, ignore_term=True)
    started = asyncio.Event()
    child_pid = None

    async def sink(event):
        nonlocal child_pid
        if event["type"] == "CHILD":
            child_pid = event["pid"]
            started.set()

    task = asyncio.create_task(instance.run(event_sink=sink, request_handler=reply))
    await asyncio.wait_for(started.wait(), 5)
    assert os.getpgid(instance.pid) == instance.pid
    assert os.getpgrp() != instance.pid
    instance.cancel()
    assert (await asyncio.wait_for(task, 3)).forced
    assert_reaped(instance)
    for _ in range(100):
        stat = Path(f"/proc/{child_pid}/stat")
        if not stat.exists():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("controlled descendant remains alive")


async def test_sdk_threads_serialize_ipc_requests_and_cancel_reads():
    instance = worker(threaded=True)
    result = await asyncio.wait_for(instance.run(event_sink=discard, request_handler=reply), 10)
    assert result.status == "finished"
    assert result.value == [1, 2, 3, 4]
    assert_reaped(instance)
