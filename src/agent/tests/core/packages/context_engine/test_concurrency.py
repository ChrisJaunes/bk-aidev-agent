# -*- coding: utf-8 -*-
"""T-10 / REQ-11：多线程与协程并发写入、并发更新冲突。"""

from __future__ import annotations

import asyncio
import threading
import time

from aidev_agent.core.packages.context_engine import VersionConflictError

WORKERS = 8
PER_WORKER = 25


def test_concurrent_writes_are_all_loadable(make_store):
    store = make_store(sweep_interval=600)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for step in range(PER_WORKER):
                store.write(
                    scope="s1",
                    category="Execution Trace",
                    payload={"worker": index, "step": step},
                    source="tool_call",
                )
        except BaseException as exc:  # noqa: BLE001 - 任何异常都应让用例失败
            errors.append(exc)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(index,)) for index in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    total = WORKERS * PER_WORKER
    assert errors == []
    entries = store.load(scope="s1").entries
    assert len(entries) == total
    assert len({entry.id for entry in entries}) == total
    # 性能目标为参考值，仅打印实测吞吐，不做断言（plan 测试策略）
    print(f"[throughput] {total} writes in {elapsed:.3f}s -> {total / elapsed:.0f} entries/s")


async def test_async_concurrent_writes_are_all_loadable(make_store):
    store = make_store(sweep_interval=600)
    coroutines_per_worker = 10
    workers = 10

    async def worker(index: int) -> None:
        for step in range(coroutines_per_worker):
            store.write(
                scope="s1",
                category="Current User Input",
                payload={"worker": index, "step": step},
                source="user_request",
            )
            await asyncio.sleep(0)

    await asyncio.gather(*(worker(index) for index in range(workers)))

    assert len(store.load(scope="s1").entries) == workers * coroutines_per_worker


def test_concurrent_update_only_one_succeeds(make_store):
    store = make_store(sweep_interval=600)
    entry = store.write(scope="s1", category="Agent Definition", payload={"v": 0}, source="prompt")
    outcomes: list[str] = []

    def worker(index: int) -> None:
        try:
            store.update(scope="s1", entry_id=entry.id, version=entry.version, payload={"v": index})
        except VersionConflictError:
            outcomes.append("conflict")
        else:
            outcomes.append("ok")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == WORKERS - 1
    final = store.load(scope="s1").entries[0]
    assert final.version == entry.version + 1


def test_store_instances_do_not_share_entries(make_store):
    """条目只挂在实例属性上：两个实例互不可见（无模块级容器）。"""

    first = make_store()
    second = make_store()
    first.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")

    assert second.load(scope="s1").entries == ()
    assert second.stats().hot_entries == 0
