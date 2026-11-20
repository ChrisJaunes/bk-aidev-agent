# -*- coding: utf-8 -*-
"""``utils.terminal`` 的字典形状必须与 SDK 真序列化器逐键相同。

为什么需要这一条
----------------
父进程（``runner``）在 worker 收尾后补 ``RUN_ERROR`` / ``RUN_FINISHED``，而父进程不得装载
SDK——于是形状被固化成纯 stdlib 的字典（:mod:`aidev_agent_cli.utils.terminal`）。那条
"不得装载 SDK"的约束由 ``test_layer_graph`` 守住；这里守住**另一面**：字典与 SDK 的
``model_dump(mode="json", by_alias=True, exclude_none=True)`` 完全一致，否则父进程补的事件
会在协议层与 worker 发出的事件不同形。

SDK 侧 ``sdk.events`` 现在反过来调用同一份形状定义（``RunErrorEvent(**dict)``），因此本文件
同时验证"两处同源"。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from aidev_agent_cli.utils import terminal

ROOT = Path(__file__).resolve().parents[2]


def _sdk_dump(code: str) -> dict:
    """在子进程里用 SDK 构造同类事件并 dump（隔离 SDK 的导入副作用）。"""
    return json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), timeout=120, check=True).stdout)


def test_cancel_pair_matches_the_sdk_serialization() -> None:
    error_event, finished_event = terminal.build_cancel_terminal_events("thread-1")

    expected_error = _sdk_dump(
        "import json\n"
        "from ag_ui.core import EventType, RunErrorEvent\n"
        "from aidev_agent.utils.event import RunId, stamp_round_end_event\n"
        "e = RunErrorEvent(type=EventType.RUN_ERROR, message=RunId.CANCELLED_MESSAGE)\n"
        "stamp_round_end_event(e)\n"
        "print(json.dumps(e.model_dump(mode='json', by_alias=True, exclude_none=True)))\n"
    )
    expected_finished = _sdk_dump(
        "import json\n"
        "from ag_ui.core import EventType, RunFinishedEvent\n"
        "from aidev_agent.core.ag_ui.types import RunFinishedSuccessOutcome, serialize_run_finished_outcome\n"
        "from aidev_agent.utils.event import RunId, stamp_round_end_event\n"
        "e = RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id='thread-1', run_id=RunId.CANCELLED,\n"
        "                     outcome=serialize_run_finished_outcome(RunFinishedSuccessOutcome()))\n"
        "stamp_round_end_event(e)\n"
        "print(json.dumps(e.model_dump(mode='json', by_alias=True, exclude_none=True)))\n"
    )

    assert error_event.keys() == expected_error.keys(), (error_event, expected_error)
    assert finished_event.keys() == expected_finished.keys(), (finished_event, expected_finished)
    assert error_event["message"] == expected_error["message"]
    assert finished_event["threadId"] == expected_finished["threadId"]
    assert finished_event["runId"] == expected_finished["runId"]
    assert finished_event["outcome"] == expected_finished["outcome"]
    assert isinstance(error_event["timestamp"], int)
    assert isinstance(finished_event["timestamp"], int)


def test_finished_and_error_builders_match_the_sdk_serialization() -> None:
    expected_finished = _sdk_dump(
        "import json\n"
        "from ag_ui.core import EventType, RunFinishedEvent\n"
        "from aidev_agent.core.ag_ui.types import RunFinishedSuccessOutcome, serialize_run_finished_outcome\n"
        "from aidev_agent.utils.event import stamp_round_end_event\n"
        "e = RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id='t', run_id='run-9',\n"
        "                     outcome=serialize_run_finished_outcome(RunFinishedSuccessOutcome()))\n"
        "stamp_round_end_event(e)\n"
        "print(json.dumps(e.model_dump(mode='json', by_alias=True, exclude_none=True)))\n"
    )
    expected_error = _sdk_dump(
        "import json\n"
        "from ag_ui.core import EventType, RunErrorEvent\n"
        "from aidev_agent.utils.event import stamp_round_end_event\n"
        "e = RunErrorEvent(type=EventType.RUN_ERROR, message='boom')\n"
        "stamp_round_end_event(e)\n"
        "print(json.dumps(e.model_dump(mode='json', by_alias=True, exclude_none=True)))\n"
    )

    finished = terminal.build_finished_event("t", "run-9")
    error = terminal.build_error_event("boom")

    assert finished.keys() == expected_finished.keys()
    assert error.keys() == expected_error.keys()
    assert finished["outcome"] == expected_finished["outcome"]
    assert error["message"] == expected_error["message"]


def test_sdk_events_reuses_the_same_shape() -> None:
    """worker 侧的 builder 只是把同一份字典转成 SDK 模型——形状没有第二份事实来源。"""
    from aidev_agent_cli.sdk import events

    error_event, finished_event = events.build_cancel_terminal_events("thread-2")
    dumped_error = error_event.model_dump(mode="json", by_alias=True, exclude_none=True)
    dumped_finished = finished_event.model_dump(mode="json", by_alias=True, exclude_none=True)
    dict_error, dict_finished = terminal.build_cancel_terminal_events("thread-2")

    assert dumped_error.keys() == dict_error.keys()
    assert dumped_finished.keys() == dict_finished.keys()
    assert dumped_finished["runId"] == dict_finished["runId"]
    assert dumped_finished["outcome"] == dict_finished["outcome"]


def test_terminal_module_is_stdlib_only() -> None:
    """形状定义处自身零第三方依赖（由 §4 的分层断言兜底，这里点明意图）。"""
    source = (ROOT / "aidev_agent_cli" / "utils" / "terminal.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.names[0].name for node in tree.body if isinstance(node, ast.Import) and node.names}
    from_imported = {node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module}

    assert imported <= {"time"}, sorted(imported)
    assert from_imported <= {"__future__", "typing"}, sorted(from_imported)
