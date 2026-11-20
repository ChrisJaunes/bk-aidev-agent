# -*- coding: utf-8 -*-
"""真 SDK stdio 端到端：子进程 + 管道 + JSON-RPC（不是 fake peer）。

为什么要一份子进程测试
----------------------
``test_acp_adapter.py`` 用替身策略测协议翻译，测不到**启动顺序**与**进程级副作用**：
fd 1 的归属（guard / restore）、token 解析失败时的退出形态、EOF 收尾是否真的关掉
saver、以及「第二个会话不得改进程环境」。这些只有真的把 ``python -m aidev_agent_cli``
拉起来并说 JSON-RPC 才能证明。

驱动方式刻意用**管道**（``subprocess.PIPE``）而不是 ``subprocess.run(capture_output=True)``：
ACP 客户端就是管道；同时它也让我们能「先说一句话、再关 stdin」来观察 EOF 收尾。

平台的网络调用被两条手段挡在外面：
* ``session/new`` 不触达平台（只建会话后端）；
* ``session/prompt`` 在连不上平台时**快速失败**，而我们断言的是「失败被如实报成
  JSON-RPC error」——这恰好就是 B3 的线上证据。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration._paths import AGENT_ROOT, PLUGIN_ROOT, PYTHON

#: 一次往返的默认上限：握手应远快于此；慢的是平台重试（已被失败断言吃掉）。
RECV_TIMEOUT = 45.0


class AcpSession:
    """一个被拉起的 ACP 服务端子进程 + 最小的 JSON-RPC 客户端。"""

    def __init__(self, process: subprocess.Popen, log_path: Path) -> None:
        self.process = process
        self.log_path = log_path
        # stdout 用一个守护线程按行读进队列：阻塞式 ``readline()`` 无法被超时打断，
        # 直接读会让「没收到应答」表现为 60s 的 pytest 超时而不是一条可读的断言。
        import queue
        import threading

        self._lines: Any = queue.Queue()
        self._eof = object()
        threading.Thread(target=self._pump, name="acp-test-stdout", daemon=True).start()

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._lines.put(line)
        self._lines.put(self._eof)

    # ------------------------------------------------------------------ 发送

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, request_id: Any, method: str, params: dict[str, Any] | None = None) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ------------------------------------------------------------------ 接收

    def receive(self, timeout: float = RECV_TIMEOUT) -> dict[str, Any]:
        """读一条 JSON-RPC 消息（跳过空行）。"""
        import queue

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"在 {timeout}s 内没有收到应答")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError(f"在 {timeout}s 内没有收到应答") from None
            if line is self._eof:
                raise AssertionError("ACP 子进程在应答前关闭了 stdout")
            text = str(line).strip()
            if text:
                return json.loads(text)

    def call(self, request_id: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发请求并读**对应 id** 的响应（通知可能插在中间）。"""
        self.request(request_id, method, params)
        while True:
            message = self.receive()
            if message.get("id") == request_id:
                return message

    # ------------------------------------------------------------------ 收尾

    def close_stdin(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()

    def wait(self, timeout: float = 90.0) -> int:
        return self.process.wait(timeout=timeout)

    def stderr(self) -> str:
        if self.process.poll() is None:
            return ""
        assert self.process.stderr is not None

        return self.process.stderr.read()

    def logs(self) -> str:
        if not self.log_path.exists():
            return ""

        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)


def _env(tmp_path: Path, access_token: str = "", **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["MESSAGE_HANDLER_TYPE"] = "inmemory"
    env["PYTHONPATH"] = os.pathsep.join([str(PLUGIN_ROOT), str(AGENT_ROOT), env.get("PYTHONPATH", "")])
    env["AGENT_TUI_TUI_CONFIG_FILE"] = str(tmp_path / "bkai_tui.json")
    if access_token:
        (tmp_path / "bkai_tui.json").write_text(json.dumps({"auth": {"access_token": access_token}}), encoding="utf-8")
    env["BKAI_TUI_LOG_PATH"] = str(tmp_path / "worker.log")
    env["BKAI_TUI_CHECKPOINT_FILE"] = str(tmp_path / "ckpt.sqlite")
    # 不继承开发机的登录态 / 项目目录：用例必须自洽。
    for key in ("AGENT_TUI_CWD", "AGENT_TUI_AGENT_CODE"):
        env.pop(key, None)
    # 平台地址指向一个必然连不上的回环端口：**不触达真实平台**，且失败很快。
    env["BKPAAS_APP_CODE"] = "acp-test-app"
    env["BKPAAS_APP_SECRET"] = "acp-test-secret"
    env["BK_API_URL_TMPL"] = "http://127.0.0.1:1/{api_name}/"
    env["BKAPP_API_URL_TMPL"] = "http://127.0.0.1:1/{api_name}/"
    env.update(extra)

    return env


@pytest.fixture
def acp_server(tmp_path: Path) -> Iterator[Any]:
    """拉起 ACP 子进程的工厂；测试结束确保回收。"""
    started: list[AcpSession] = []

    def _start(**extra: str) -> AcpSession:
        process = subprocess.Popen(
            [PYTHON, "-u", "-m", "aidev_agent_cli", "--protocol", "acp", "--resources", "platform"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(AGENT_ROOT),
            env=_env(tmp_path, **extra),
        )
        session = AcpSession(process, tmp_path / "worker.log")
        started.append(session)

        return session

    yield _start

    for session in started:
        session.kill()
        for stream in (session.process.stdout, session.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def _initialize(server: AcpSession, request_id: Any = 1) -> dict[str, Any]:
    return server.call(
        request_id,
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "clientInfo": {"name": "handshake-test", "version": "0"},
        },
    )


# ============================================================================
# 1. 握手
# ============================================================================


@pytest.mark.slow
def test_handshake_advertises_protocol_one_and_no_unimplemented_capability(acp_server: Any) -> None:
    """``initialize`` 必须如实：protocolVersion 1，未实现能力全 false。"""
    server = acp_server(access_token="dummy-token")

    response = _initialize(server)

    assert "error" not in response, response
    result = response["result"]
    assert result["protocolVersion"] == 1
    capabilities = result["agentCapabilities"]
    assert capabilities.get("loadSession", False) is False
    prompt_caps = capabilities.get("promptCapabilities") or {}
    assert prompt_caps.get("image", False) is False
    assert prompt_caps.get("audio", False) is False
    assert prompt_caps.get("embeddedContext", False) is False


# ============================================================================
# 2. session/new：per-session 工作区隔离（D5）
# ============================================================================


@pytest.mark.slow
def test_two_sessions_keep_their_own_cwd_without_touching_the_process_env(fake_acp_server: Any, tmp_path: Path) -> None:
    """两个不同 cwd 的会话并存，sessionId 互不相同，cwd 仅作诊断记录。

    工作区根固定为进程 cwd：客户端 cwd 不再写入 ``SessionOptions`` / 进程环境变量，
    只会进日志。"""
    first = tmp_path / "workspace-one"
    second = tmp_path / "workspace-two"
    first.mkdir()
    second.mkdir()

    server = fake_acp_server(access_token="dummy-token")
    _initialize(server)

    one = server.call(2, "session/new", {"cwd": str(first), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})
    two = server.call(3, "session/new", {"cwd": str(second), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})

    assert "error" not in one, one
    assert "error" not in two, two
    session_one = one["result"]["sessionId"]
    session_two = two["result"]["sessionId"]
    assert session_one and session_two
    assert session_one != session_two

    server.close_stdin()
    assert server.wait() == 0
    logs = server.logs()
    assert str(first) in logs
    assert str(second) in logs


@pytest.mark.slow
def test_a_session_cwd_does_not_overwrite_the_process_default(fake_acp_server: Any, tmp_path: Path) -> None:
    """进程级初值是 one；第二个会话显式给 two。cwd 现在只记录、不写 env、不影响工作区。"""
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    server = fake_acp_server(access_token="dummy-token", AGENT_TUI_CWD=str(first))
    _initialize(server)
    one = server.call(2, "session/new", {"cwd": str(first), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})
    two = server.call(3, "session/new", {"cwd": str(second), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})

    assert "error" not in one and "error" not in two

    server.close_stdin()
    assert server.wait() == 0
    assert str(second) in server.logs()


# ============================================================================
# 3. prompt 失败路径（B3）
# ============================================================================


@pytest.mark.slow
def test_a_failed_build_is_a_json_rpc_error_and_the_session_is_unusable(acp_server: Any, tmp_path: Path) -> None:
    """**B3 回归（线上）**：无可用平台时失败必须如实报成 error，不能被说成成功。

    ``session/new`` 立即装配：装配失败回 ``agent.build_failed`` 并回滚，之后对该会话的
    ``session/prompt`` 是 ``session.not_found``，进程继续服务。
    """
    server = acp_server(access_token="dummy-token")
    _initialize(server)
    created = server.call(2, "session/new", {"cwd": str(tmp_path), "mcpServers": [], "_meta": {"aidev": {"sessionCode": "s-fail", "agent": "fake-entry-agent"}}})

    assert "result" not in created, created
    assert created["error"]["data"]["code"] == "agent.build_failed"

    response = server.call(3, "session/prompt", {"sessionId": "s-fail", "prompt": [{"type": "text", "text": "hi"}]})
    assert "result" not in response, "失败会话绝不能给出成功结果"
    assert response["error"]["data"]["code"] == "session.not_found"

    again = _initialize(server, request_id=99)
    assert "error" not in again, again


# ============================================================================
# 4. cancel：未知会话是 no-op；进程仍可服务后续请求
# ============================================================================


@pytest.mark.slow
def test_cancel_for_an_unknown_session_keeps_the_process_serving(fake_acp_server: Any, tmp_path: Path) -> None:
    """B1 的线上侧：取消未知会话是合法空操作，之后握手仍然正常。"""
    server = fake_acp_server(access_token="dummy-token")
    _initialize(server)
    session_id = server.call(2, "session/new", {"cwd": str(tmp_path), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})["result"]["sessionId"]

    server.request(3, "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "slow"}]})
    server.notify("session/cancel", {"sessionId": "no-such-session"})

    response = None
    while True:
        message = server.receive()
        if message.get("id") == 3:
            response = message
            break
    # 取消别的（未知）会话不得波及本轮：本轮照常结束。
    assert response["result"]["stopReason"] == "end_turn", response

    again = _initialize(server, request_id=99)
    assert "error" not in again, again


# ============================================================================
# 5. EOF：干净退出 + close_all + saver 已释放（B4）
# ============================================================================


@pytest.mark.slow
def test_eof_exits_zero_runs_close_all_and_releases_the_saver(fake_acp_server: Any, tmp_path: Path) -> None:
    """**B4 回归**：stdin 关闭后进程 0 退出、日志有 ``close_all``、sqlite 可再次打开。

    原入口 ``await run_agent(adapter)`` 之后直接 ``return 0``：不关 session、不关
    owned saver、不 flush。连接仍占着 sqlite 文件就是它的可观测形态。
    """
    checkpoint_file = tmp_path / "ckpt.sqlite"
    server = fake_acp_server(access_token="dummy-token")
    _initialize(server)
    server.call(2, "session/new", {"cwd": str(tmp_path), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})

    server.close_stdin()
    assert server.wait() == 0

    stderr = server.stderr()
    assert "Traceback" not in stderr, stderr[-1500:]
    assert "runtime closed" in server.logs()

    if checkpoint_file.exists():
        connection = sqlite3.connect(str(checkpoint_file), timeout=5)
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()


# ============================================================================
# 6. 启动失败不污染 fd 1
# ============================================================================


def test_the_adapter_does_not_take_a_single_shared_value_sink() -> None:
    """B2 的结构性约束：adapter 上没有可变的 runner writer / 共享解释器 / 第二份运行状态。

    断言的是**属性赋值**（AST），不是「源码里出现某个词」——docstring 提到旧实现是
    允许的（那是解释为什么这么做）。这条与 ``test_acp_adapter.py`` 的并发用例互补：
    那边证明行为，这边防止以后再把 sink 挂回 adapter 上。
    """
    import ast
    import inspect

    from aidev_agent_cli.adapters.acp import AcpAdapter

    tree = ast.parse(inspect.getsource(AcpAdapter))
    assigned = {
        node.targets[0].attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
    }

    # adapter 只持连接、共享 service 与默认会话；hook 是 service 的进程级依赖，不缓存工作区。
    assert assigned == {"_conn", "_service", "_profile", "_tool_factories", "_default_session"}, assigned
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ("_runner", "_agui", "_runs", "_cancelled", "_writers", "_workspace_roots"):
        assert forbidden not in attributes, forbidden

    # 也没有可变的 writer 属性（``self._runner.writer = ...`` 的载体）。
    assert not any(isinstance(node, ast.Attribute) and node.attr == "writer" for node in ast.walk(tree))


# ============================================================================
# 6. 取消：真 wire + 真 SDK 生命周期 + 本地确定性执行后端
# ============================================================================


@pytest.fixture
def fake_acp_server(tmp_path: Path) -> Iterator[Any]:
    """拉起 ACP 子进程，但执行后端是本地确定性的（``_fake_acp_agent``）。

    与 ``acp_server`` 共享同一个 :class:`AcpSession` 客户端（真管道、真 NDJSON、真 JSON-RPC）
    与同一条 ``--protocol acp`` 启动路径；区别只在进程入口：``-m tests.integration._fake_acp_agent``
    ——它把执行后端换成「慢轮会一直等、只有 cancel 能结束」的确定性实现。

    无外部凭据、无网络：替身不触达平台（见该模块 docstring）。
    """
    started: list[AcpSession] = []

    def _start(**extra: str) -> AcpSession:
        process = subprocess.Popen(
            [PYTHON, "-u", "-m", "tests.integration._fake_acp_agent"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(PLUGIN_ROOT),
            env=_env(tmp_path, **extra),
        )
        session = AcpSession(process, tmp_path / "worker.log")
        started.append(session)

        return session

    yield _start

    for session in started:
        session.kill()
        for stream in (session.process.stdout, session.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def _collect_response(server: AcpSession, request_id: Any, *, into: list, timeout: float = 90.0) -> dict[str, Any]:
    """读到 ``request_id`` 的响应为止；期间的通知追加进 ``into``。"""
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0.1, deadline - time.monotonic())
        message = server.receive(timeout=remaining)
        if message.get("method") == "session/update":
            into.append(message)
            continue
        if message.get("id") == request_id:
            return message


def _chunk_texts(updates: list[dict[str, Any]], session_id: str) -> list[str]:
    texts: list[str] = []
    for message in updates:
        params = message.get("params") or {}
        if params.get("sessionId") != session_id:
            continue
        update = params.get("update") or {}
        content = update.get("content") or {}
        text = content.get("text")
        if isinstance(text, str):
            texts.append(text)

    return texts


@pytest.mark.slow
def test_cancel_ends_a_waiting_prompt_as_cancelled_and_leaves_the_other_session_alone(
    fake_acp_server: Any, tmp_path: Path
) -> None:
    """B1 的承重证据：**只有**取消能让 A 结束，B 在同一时刻正常 ``end_turn``。

    为什么这条断言是单方向的：

    * A 的输入含 ``slow``，替身每步 sleep 50ms、共 400 步（20s）。若取消没送达，A 会一直
      跑到 20s 之后才 ``end_turn``——而测试在 90s 硬上限之前就会断言 ``cancelled``，届时 A
      仍是进行中，断言失败。
    * B 的输入不含 ``slow``，单步结束。若取消串到 B（原实现的 ``_live_runs_for`` 忽略
      ``sessionId``），B 会变成 ``cancelled`` 或 error，而不是 ``end_turn``。

    两条断言都不接受「cancelled 或 error」这种含糊结局：取消必须真的落成 ``cancelled``，
    而未被取消的会话必须真的落成 ``end_turn``。
    """
    session_a_dir = tmp_path / "a"
    session_b_dir = tmp_path / "b"
    session_a_dir.mkdir()
    session_b_dir.mkdir()

    server = fake_acp_server(access_token="dummy-token")

    _initialize(server)

    session_a = server.call(2, "session/new", {"cwd": str(session_a_dir), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})["result"]["sessionId"]
    session_b = server.call(3, "session/new", {"cwd": str(session_b_dir), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})["result"]["sessionId"]
    assert session_a != session_b

    # --- A：慢轮。先确认它真的产出了内容，再取消（否则可能在「已取消」之后才启动） ---
    server.request(10, "session/prompt", {"sessionId": session_a, "prompt": [{"type": "text", "text": "slow please"}]})
    a_updates: list[dict[str, Any]] = []
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        message = server.receive(timeout=45)
        if message.get("method") != "session/update":
            continue
        a_updates.append(message)
        if _chunk_texts([message], session_a):
            break
    assert _chunk_texts(a_updates, session_a), "慢轮必须真的开始产出（否则取消窗口不存在）"

    # --- B：同一时刻跑快轮 ---
    server.request(11, "session/prompt", {"sessionId": session_b, "prompt": [{"type": "text", "text": "fast"}]})

    # --- 取消只针对 A ---
    server.notify("session/cancel", {"sessionId": session_a})

    # 响应**可能不按请求顺序到达**（B 单步先完成时 id=11 会先出现），因此这里循环收件，
    # 两个 id 都拿到才停。A 的通知在取消**之前**已经读过一部分（上面的等待循环），
    # 所以继续追加进同一个列表，而不是重开一个空列表。
    responses: dict[Any, dict[str, Any]] = {}
    b_updates: list[dict[str, Any]] = []
    deadline = time.monotonic() + 90
    while len(responses) < 2:
        remaining = max(0.1, deadline - time.monotonic())
        message = server.receive(timeout=remaining)
        if message.get("method") == "session/update":
            params = message.get("params") or {}
            if params.get("sessionId") == session_b:
                b_updates.append(message)
            else:
                a_updates.append(message)
            continue
        if message.get("id") in (10, 11):
            responses[message["id"]] = message

    a_response = responses[10]
    b_response = responses[11]

    assert "error" not in a_response, a_response
    assert a_response["result"]["stopReason"] == "cancelled", a_response

    assert "error" not in b_response, b_response
    assert b_response["result"]["stopReason"] == "end_turn", b_response

    # 会话隔离的第二个角度：B 的通知里出现的是 B 的文本，A 的通知里没有 B 的文本。
    assert _chunk_texts(b_updates, session_b) == ["ok"]
    assert "ok" not in _chunk_texts(a_updates, session_a)

    # 进程仍可服务后续请求（取消没有杀掉连接）。
    assert "error" not in _initialize(server, request_id=99)

    server.close_stdin()
    assert server.wait() == 0


@pytest.mark.slow
def test_the_fake_backend_keeps_both_sessions_agent_codes_apart(fake_acp_server: Any, tmp_path: Path) -> None:
    """两个 ACP 会话各带自己的 cwd；进程级 ``AGENT_TUI_CWD`` 不被写。

    这条与 ``test_two_sessions_keep_their_own_cwd_without_touching_the_process_env`` 同一诉求，
    但走的是**确定性后端**：因此它验证的是 ``SessionOptions`` / ``Session`` 这一侧，
    而不依赖平台是否可达。cwd 现在只进日志，工作区根固定为进程 cwd。
    """
    first = tmp_path / "workspace-one"
    second = tmp_path / "workspace-two"
    first.mkdir()
    second.mkdir()

    server = fake_acp_server(access_token="dummy-token")

    _initialize(server)
    one = server.call(2, "session/new", {"cwd": str(first), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})
    two = server.call(3, "session/new", {"cwd": str(second), "mcpServers": [], "_meta": {"aidev": {"agent": "fake-entry-agent"}}})

    assert "error" not in one and "error" not in two
    assert one["result"]["sessionId"] != two["result"]["sessionId"]

    server.close_stdin()
    assert server.wait() == 0
    logs = server.logs()
    assert str(first) in logs
    assert str(second) in logs