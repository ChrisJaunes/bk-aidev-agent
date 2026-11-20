# -*- coding: utf-8 -*-
"""集成测试：worker 改用预装载 SDK 的 forkserver 启动（见 :mod:`tests.integration._prewarm`）。"""

from __future__ import annotations

import pytest

from aidev_agent_cli.runtime import worker as worker_module
from tests.integration import _prewarm


@pytest.fixture(scope="session")
def _forkserver_multiprocessing() -> _prewarm.ForkserverMultiprocessing:
    original = worker_module.multiprocessing
    shim = _prewarm.install()
    worker_module.multiprocessing = original
    return shim


@pytest.fixture(autouse=True)
def _prewarmed_worker_start(_forkserver_multiprocessing, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "multiprocessing", _forkserver_multiprocessing)
