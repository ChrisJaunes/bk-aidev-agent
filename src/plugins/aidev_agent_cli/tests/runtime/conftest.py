"""runtime 测试共用配置。"""

import pytest


@pytest.fixture(autouse=True)
def fast_worker_reaping(monkeypatch):
    """收紧合作取消宽限：被测故障进程不响应取消，生产默认值会让每次强杀空等 2 秒。

    join 宽限不收紧：正常退出的子进程需要它完成收尾，过短会被误判为强杀。
    """
    monkeypatch.setattr("aidev_agent_cli.runtime.worker.CANCEL_GRACE_SECONDS", 0.5)
