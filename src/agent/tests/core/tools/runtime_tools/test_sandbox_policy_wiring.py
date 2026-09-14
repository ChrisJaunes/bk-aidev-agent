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

from aidev_agent.core.tools.runtime_tools.bubblewrap import BubblewrapBackend, _select_backend
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.pydantic_models import SandboxMode, SandboxPolicy, SecuritySettings


def test_security_settings_from_mapping_parses_sandbox_policy_dict():
    """平台下发 sandbox_policy dict 时，from_mapping 自动解析为 SandboxPolicy。"""
    ss = SecuritySettings.from_mapping(
        {
            "sandbox_policy": {
                "mode": "readonly",
                "deny_paths": ["/etc/ssh", "/root/.ssh"],
                "allow_network": False,
            }
        }
    )
    assert isinstance(ss.sandbox_policy, SandboxPolicy)
    assert ss.sandbox_policy.mode is SandboxMode.READONLY
    assert ss.sandbox_policy.deny_paths == ["/etc/ssh", "/root/.ssh"]
    assert ss.sandbox_policy.allow_network is False


def test_security_settings_from_mapping_none_sandbox_policy():
    """未下发 sandbox_policy 时保持 None，回落 file_bwrap_* 字段。"""
    ss = SecuritySettings.from_mapping({})
    assert ss.sandbox_policy is None


def test_security_settings_from_mapping_keeps_other_fields():
    """下发 sandbox_policy 不干扰其他安全字段解析。"""
    ss = SecuritySettings.from_mapping({"file_bwrap": True, "sandbox_policy": {"mode": "full_access"}})
    assert ss.file_bwrap is True
    assert isinstance(ss.sandbox_policy, SandboxPolicy)
    assert ss.sandbox_policy.mode is SandboxMode.FULL_ACCESS


def test_filesystem_backend_accepts_sandbox_policy(tmp_path):
    """FilesystemBackend 接收 sandbox_policy，走 BubblewrapSandbox 新接口。"""
    policy = SandboxPolicy(readonly_paths=[], deny_paths=["/etc/ssh"])
    backend = FilesystemBackend(root_dir=str(tmp_path), sandbox_policy=policy)
    assert backend._bwrap is not None
    # model_copy 隔离：内容一致但非同一对象
    assert backend._bwrap._policy.readonly_paths == policy.readonly_paths
    assert backend._bwrap._policy.deny_paths == ["/etc/ssh"]
    assert backend._bwrap._policy is not policy


def test_filesystem_backend_policy_enables_bwrap_without_flag(tmp_path):
    """sandbox_policy 非 None 即启用沙箱，无需 bwrap_enabled=True。"""
    backend = FilesystemBackend(
        root_dir=str(tmp_path),
        bwrap_enabled=False,
        sandbox_policy=SandboxPolicy(),
    )
    assert backend._bwrap is not None


def test_filesystem_backend_no_policy_no_flag_disables_bwrap(tmp_path):
    """无 policy 且未启用 bwrap 时，不构造沙箱（fail-open 默认路径）。"""
    backend = FilesystemBackend(root_dir=str(tmp_path))
    assert backend._bwrap is None


def test_select_backend_returns_bubblewrap_on_linux():
    """当前平台（Linux）选择 BubblewrapBackend。"""
    assert isinstance(_select_backend("bwrap"), BubblewrapBackend)
