# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making
蓝鲸智云 - AIDev (BlueKing - AIDev) available.
Copyright (C) 2025 THL A29 Limited,
a Tencent company. All rights reserved.
Licensed under the MIT License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the
specific language governing permissions and limitations under the License.
We undertake not to change the open source license (MIT license) applicable
to the current version of the project delivered to anyone in the future.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from aidev_agent.core.tools.runtime_tools.bubblewrap import BwrapResult
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend


def test_bwrap_disabled_by_default():
    """默认不启用 bwrap：_bwrap 为 None，走同进程路径。"""
    backend = FilesystemBackend(root_dir="/tmp/ws")
    assert backend._bwrap is None
    assert backend._bwrap_ready() is False


def test_bwrap_enabled_but_unavailable_fail_open(tmp_path):
    """bwrap 启用但二进制缺失：fail-open 降级回同进程读写。"""
    backend = FilesystemBackend(root_dir=str(tmp_path), bwrap_enabled=True, bwrap_path="/nonexistent/bwrap-xyz")
    assert backend._bwrap is not None
    assert backend._bwrap_ready() is False
    # 写文件仍走同进程 os.open，真实落盘
    result = backend.write("foo.txt", "hello")
    assert result.error is None
    assert (tmp_path / "foo.txt").read_text() == "hello"


def test_read_routes_to_bwrap_when_available(tmp_path):
    """bwrap 可用时 read 走 bwrap 子进程。"""
    backend = FilesystemBackend(root_dir=str(tmp_path), bwrap_enabled=True, bwrap_path="/nonexistent")
    fake = MagicMock()
    fake.is_available.return_value = True
    fake.read_text.return_value = "line-one\nline-two\n"
    backend._bwrap = fake

    result = backend.read("foo.txt", offset=0, limit=10)
    assert "line-one" in result
    fake.read_text.assert_called_once()


def test_write_routes_to_bwrap_when_available(tmp_path):
    """bwrap 可用时 write 走 bwrap 子进程（不落宿主机）。"""
    backend = FilesystemBackend(root_dir=str(tmp_path), bwrap_enabled=True, bwrap_path="/nonexistent")
    fake = MagicMock()
    fake.is_available.return_value = True
    fake.write_text.return_value = True
    backend._bwrap = fake

    result = backend.write("bar.txt", "payload")
    assert result.error is None
    fake.write_text.assert_called_once()
    # 内容经 bwrap 写入，宿主机不应直接落盘（此处为 mock，故不存在）
    assert not (tmp_path / "bar.txt").exists()


def test_execute_routes_to_bwrap_when_available(tmp_path):
    """bwrap 可用时 execute 走 bwrap 子进程。"""
    backend = FilesystemBackend(root_dir=str(tmp_path), bwrap_enabled=True, bwrap_path="/nonexistent")
    fake = MagicMock()
    fake.is_available.return_value = True
    fake.run_shell.return_value = BwrapResult(output="ok", exit_code=0)
    backend._bwrap = fake

    result = backend.execute("echo hi", timeout=10, max_output_size=1000)
    assert result.output == "ok"
    assert result.exit_code == 0
    fake.run_shell.assert_called_once_with("echo hi", timeout=10)


def test_ls_routes_to_bwrap_when_available(tmp_path):
    """bwrap 可用时 ls_info 走 bwrap 子进程。"""
    backend = FilesystemBackend(root_dir=str(tmp_path), bwrap_enabled=True, bwrap_path="/nonexistent")
    fake = MagicMock()
    fake.is_available.return_value = True
    fake.list_entries.return_value = [
        {"path": str(tmp_path / "a.py"), "is_dir": False, "size": 12, "modified_at": "2026-01-01T00:00:00"},
        {"path": str(tmp_path / "sub"), "is_dir": True, "size": 0, "modified_at": "2026-01-01T00:00:00"},
    ]
    backend._bwrap = fake

    result = backend.ls_info("/")
    assert len(result) == 2
    # 目录路径带 '/' 后缀
    paths = {r["path"] for r in result}
    assert any(p.endswith("/") for p in paths)
    fake.list_entries.assert_called_once()


def test_upload_routes_to_bwrap_when_available(tmp_path):
    """bwrap 可用时 upload_files 走 bwrap 子进程（bytes 经 stdin）。"""
    backend = FilesystemBackend(root_dir=str(tmp_path), bwrap_enabled=True, bwrap_path="/nonexistent")
    fake = MagicMock()
    fake.is_available.return_value = True
    fake.write_bytes.return_value = True
    backend._bwrap = fake

    result = backend.upload_files([("foo.bin", b"\x00\x01\x02")])
    assert result[0]["error"] is None
    fake.write_bytes.assert_called_once()
    assert not (tmp_path / "foo.bin").exists()  # 内容经 bwrap 写入，宿主机不直接落盘


def test_download_routes_to_bwrap_when_available(tmp_path):
    """bwrap 可用时 download_files 走 bwrap 子进程（bytes 二进制）。"""
    backend = FilesystemBackend(root_dir=str(tmp_path), bwrap_enabled=True, bwrap_path="/nonexistent")
    fake = MagicMock()
    fake.is_available.return_value = True
    fake.read_bytes.return_value = b"\x00\x01\x02"
    backend._bwrap = fake

    result = backend.download_files(["foo.bin"])
    assert result[0]["content"] == b"\x00\x01\x02"
    assert result[0]["error"] is None
    fake.read_bytes.assert_called_once()
