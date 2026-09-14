# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.redaction.operations

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

脱敏操作层：本包**全部 ``redact_*`` 出口**集中于此。

模块按**替换策略**分为两节，二者的检测与替换机制**刻意不同**，不要试图统一：

1. **detector 管线**（``scan_text`` / ``redact_text`` / ``redact_payload`` /
   ``redact_for_export`` / ``count_sensitive_hits``）——
   所有 detector 扫描**同一份原始文本**，产出 ``Finding``；合并重叠区间后从后往前
   **一次性替换**（避免位移），替换与计数共享同一 ``ScanResult``。
2. **已知值精确替换**（``redact_known_values``）——
   逐值 ``str.replace``，不经正则、不做 span 合并，使用独立的 legacy 占位符。
   见下方「已知值两条路径的差异」。

**预筛选（缺陷 3 修复）**：每个 detector 的 ``scan()`` 自身在方法开头调用一次自家
``could_match()`` —— 这是 ``detectors.Detector`` 协议既有契约，此前无调用点（dead code）。
缺此守卫时 ``AssignmentDetector._KEY_VALUE_RE`` 在无 ``=`` / ``:`` 的长文本上
O(n²) 退化。**预筛选所有权现由 ``scan()`` 独有**（T-vq0）：
本模块**不再**显式调用 ``detector.could_match()``，避免同一预筛选跑两遍，
也让「新 detector 忘记接入预筛选」不可能因调用方遗漏而复现。

外层：本模块 ``_detectors_for_scan()`` ——
PR2 已接入 9 个 detector；PR3 追加的 ``EntropyDetector`` 在同一列表末尾并入。

**detector 清单（按证据强度降序）**：
``registered``（100）> ``pem``（95）> ``headers`` / ``dsn``（90）> ``jwt``（88）>
``cookie`` / ``url``（85）> ``vendor``（80）> ``assignment``（70）> ``entropy``（50）。
顺序不决定语义（所有 detector 扫同一份原始文本，最终由 ``merge_spans``
按 ``priority`` 决策），排列仅为可读性。

**配置注入（T-06-24）**：``scan_text`` / ``redact_text`` / ``redact_payload`` 接受
可选关键字参数 ``settings: SecuritySettings | None``，默认 ``None``
回落 ``SecuritySettings()``（env 默认工厂路径）。调用方（``security_wrapper``）
按需传平台下发实例 —— 叶节点不各自读环境变量，
配置唯一入口是 ``AgentConfig.security_settings``。

---

**已知值两条出口的差异（共享匹配机制，不共享占位符）**

``scan_text(settings=...)`` 与 ``redact_known_values()`` **都吃「已知敏感值」，
且现在共享同一匹配机制**（``RegisteredSecretDetector`` 精确匹配），差异只在出口：

============================  ===================================  ==================================
                              ``scan_text(settings=...)``          ``redact_known_values()``
============================  ===================================  ==================================
已知值来源                    ``settings.known_sensitive_values``  入参 ``Sequence[str]``（调用方解析）
匹配方式                      ``RegisteredSecretDetector`` 精确匹配 ``RegisteredSecretDetector`` 精确匹配
                              （+ 其余 detector / 正则 / 裸熵）
span 合并                     参与 ``merge_spans`` 优先级决策      ``merge_spans``（消解重叠）
其他 detector                 全部参与                             不参与（只做已知值）
输出占位符                    ``masking.REDACT_PLACEHOLDER``       ``KNOWN_VALUES_PLACEHOLDER``
出口语义                      LOG / MODEL_OUTPUT / EXPORT          沙箱工具返回值
============================  ===================================  ==================================

**唯一配置通道**：``scan_text`` 的已知值取自 ``settings``，**没有独立的
``known_values`` 参数** —— 此前那个参数是 6 跳透传链，且与 ``settings`` 语义重复：
调用方若只设了 ``settings.known_sensitive_values`` 而漏传 ``known_values``，
脱敏会**静默失效**。收敛为单通道后该缺陷不复现。

**共享匹配机制的原因**：此前 ``redact_known_values`` 是逐值 ``str.replace``，顺序即列表
顺序 —— 短值先替换会把长值切碎（``["abc", "abcdef"]`` 对 ``abcdef`` 只得到
``__REDACTED__def``）。该缺陷是「未复用 detector」的直接后果。现两者共用
``RegisteredSecretDetector``：组内按值长度降序，长值优先命中，顺序无关；
并经 ``merge_spans`` 消解重叠 span。

**为什么占位符不统一**：``KNOWN_VALUES_PLACEHOLDER`` 是
``"__BKAI_AGENT_REDACTED__"``，与 ``masking.REDACT_PLACEHOLDER``（``"[REDACTED]"``）
**刻意不同** —— 两者都是**用户可见文案**，且分属不同出口：

1. ``redact_known_values`` 的输出进入**沙箱工具返回值并直接回给模型 / 用户**，
   legacy 字符串是**行为契约**（被 ``test_provider.py`` / ``test_security.py`` /
   ``test_command_sec_enforce.py`` 多处钉住）。
2. 对齐 detector 的动机是**统一匹配机制**，不是统一占位符文案 —— 借重构之机改用户
   可见字符串会把「纯重构」变成「行为变更」。
3. ``masking`` 的 typed sentinel（``[REDACTED:{kind}]``）服务**模型上下文 / 日志 /
   导出**出口，本占位符服务**工具返回值**出口；出口语义不同，用同一常量是错误抽象。

**依赖方向**：``redact_known_values`` 复用本包内的 ``detectors`` /
``findings``（同包内引用，合法），但**禁止** import ``aidev_agent.config`` /
任何 backend 类型 / ``core`` / ``services`` / ``api`` —— 配置与 backend 的解析留在
**调用方（core）**，core 把结果以**纯 ``list[str]``** 传进来。

**与 ``command.command_security.redact_output`` 的关系**：本节的
``redact_known_values`` 是从那里收敛而来的唯一实现；
``command_security`` 只保留薄委托，core provider 侧以别名 ``redact_output`` 引用。

公开接口：``redact_text`` / ``redact_payload`` / ``scan_text`` /
``count_sensitive_hits`` / ``redact_for_export`` / ``redact_known_values``。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from aidev_agent.packages.security.redaction.detectors import (
    AssignmentDetector,
    CookieDetector,
    Detector,
    DsnDetector,
    EntropyDetector,
    HeadersDetector,
    JdbcDetector,
    JwtDetector,
    PemDetector,
    RegisteredSecretDetector,
    UrlDetector,
    VendorTokenDetector,
)
from aidev_agent.packages.security.redaction.findings import Finding, ScanResult, merge_spans
from aidev_agent.packages.security.redaction.masking import mask
from aidev_agent.packages.security.redaction.policy import RedactionPurpose, mask_style_for
from aidev_agent.pydantic_models import SecuritySettings

# 凭据字段名后缀（不区分大小写，忽略分隔符）——迁移自旧实现
_CREDENTIAL_FIELD_SUFFIXES: tuple[str, ...] = (
    "apikey",
    "api_key",
    "access_key",
    "access_token",
    "secret",
    "secret_key",
    "app_secret",
    "password",
    "passwd",
    "pwd",
    "token",
    "auth_token",
    "credential",
    "credentials",
    "private_key",
    "authorization",
)

_NORMALIZED_CREDENTIAL_SUFFIXES: tuple[str, ...] = tuple(
    re.sub(r"[^a-z0-9]", "", suffix.lower()) for suffix in _CREDENTIAL_FIELD_SUFFIXES
)


def _is_credential_field(key: str) -> bool:
    """判断字段名是否为凭据字段（忽略大小写与下划线 / 连字符等分隔符）。"""
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized.endswith(_NORMALIZED_CREDENTIAL_SUFFIXES)


def _split_known_sensitive_values(raw: str | None) -> list[str]:
    """把 ``SecuritySettings.known_sensitive_values``（逗号分隔原文）拆成 list。

    **刻意不做任何规范化**（不 lower / 不去尾点）：敏感值是大小写敏感的 secret，
    规范化会让脱敏静默失效。只做 ``strip`` + 过滤空串。
    """
    return [v.strip() for v in (raw or "").split(",") if v.strip()]


def _detectors_for_scan(*, settings: SecuritySettings) -> list[Detector]:
    """组装本次扫描的 detector 列表（新 detector 的接入点）。

    按证据强度降序排列（registered > pem > headers/dsn > jwt > cookie/url >
    vendor > assignment > entropy）；顺序不决定语义 —— 所有 detector
    扫描同一份原始文本，由 ``scan_text`` 统一 ``merge_spans`` 后按 ``priority`` 决策。

    已知敏感值取自 ``settings.known_sensitive_values``（逗号分隔原文）——
    **唯一配置通道是 ``settings``**：不再有独立的 ``known_values`` 参数，
    避免同一条配置数据出现两个入口（后者会被静默忽略）。空值 ⇒ 空 detector
    ⇒ ``could_match`` 返回 False ⇒ 零命中。本模块不读 env / backend
    （依赖方向硬约束，配置由调用方经 ``settings`` 传入）。

    裸熵兜底（``EntropyDetector``）排在**最后**，阈值与开关取自 ``settings``。
    """
    detectors: list[Detector] = [
        RegisteredSecretDetector.from_values(_split_known_sensitive_values(settings.known_sensitive_values)),
        PemDetector(),
        HeadersDetector(),
        DsnDetector(),
        JdbcDetector(),
        JwtDetector(),
        CookieDetector(),
        UrlDetector(),
        VendorTokenDetector(),
        AssignmentDetector(),
        # PR3 裸熵兜底（最低优先级 priority=50，阈值/开关经 SecuritySettings 下发）
        EntropyDetector(
            min_length=settings.redact_secrets_min_length,
            alnum_threshold=settings.redact_secrets_entropy_threshold,
            enabled=settings.redact_bare_entropy,
        ),
    ]
    return detectors


def _apply_replacements(
    text: str,
    findings: list[Finding],
    *,
    purpose: RedactionPurpose,
    settings: SecuritySettings,
) -> str:
    """从后往前一次性替换（避免位移），保持未命中区间字节级不变。

    partial 掩码阈值由 ``settings`` 派生并显式传给 :func:`mask`（T-urf-03）——
    阈值不再硬编码在 ``masking`` 模块中。
    """
    if not findings:
        return text

    style = mask_style_for(purpose)
    partial_min_len = settings.redact_partial_min_len
    partial_head = settings.redact_partial_head
    partial_tail = settings.redact_partial_tail
    result = text
    for finding in sorted(findings, key=lambda f: f.start, reverse=True):
        masked = mask(
            text[finding.start : finding.end],
            kind=finding.kind,
            style=style,
            partial_min_len=partial_min_len,
            partial_head=partial_head,
            partial_tail=partial_tail,
        )
        result = result[: finding.start] + masked + result[finding.end :]
    return result


def scan_text(
    text: str,
    *,
    purpose: RedactionPurpose = RedactionPurpose.LOG,
    settings: SecuritySettings | None = None,
) -> ScanResult:
    """扫描文本并按 purpose 脱敏，返回 ``ScanResult``。

    Args:
        text: 待扫描文本。
        purpose: 脱敏出口语义（决定掩码风格）。
        settings: 脱敏配置（阈值 / 开关 / 已知敏感值）。``None`` 时回落
            ``SecuritySettings()``（env 默认工厂路径，与不传参时行为对等）；
            调用方（``security_wrapper``）应按需传入平台下发实例。
            叶节点不直读 env（T-06-24）。**已知敏感值也经此传入** ——
            见 :func:`_detectors_for_scan`。

    Returns:
        ``ScanResult``（脱敏文本 + 合并后 cluster 数 + 合并前 rule_id 计数）。
        ``raw_rule_hits`` 含裸熵的独立键（``entropy.bare_alnum`` / ``entropy.bare_base64``）。
    """
    if not isinstance(text, str) or not text:
        return ScanResult(redacted_text=text, unique_findings=0, raw_rule_hits={})

    effective_settings = settings if settings is not None else SecuritySettings()
    raw: list[Finding] = []
    for detector in _detectors_for_scan(settings=effective_settings):
        # 预筛选已由 ``detector.scan()`` 自身承担（T-vq0）：
        # 该 detector 的 ``could_match`` 是「廉价预筛选」，此前由本循环调用；
        # 现移入 ``scan``，调用方不再重复判定
        # （缺陷 3 的守卫仍由各 detector 自助持有）。
        raw.extend(detector.scan(text))

    merged = merge_spans(raw)

    # 计数按合并前（raw）口径，同一 rule_id 出现几次计几次
    raw_rule_hits: dict[str, int] = {}
    for finding in raw:
        raw_rule_hits[finding.rule_id] = raw_rule_hits.get(finding.rule_id, 0) + 1

    return ScanResult(
        redacted_text=_apply_replacements(text, merged, purpose=purpose, settings=effective_settings),
        unique_findings=len(merged),
        raw_rule_hits=raw_rule_hits,
    )


def redact_text(
    text: str,
    *,
    purpose: RedactionPurpose = RedactionPurpose.LOG,
    settings: SecuritySettings | None = None,
) -> str:
    """对自由文本执行脱敏。

    Args:
        text: 待脱敏文本。
        purpose: 脱敏出口语义。
        settings: 脱敏配置（``None`` 回落 ``SecuritySettings()``）；见 :func:`scan_text`。

    Returns:
        脱敏后的文本。
    """
    return scan_text(text, purpose=purpose, settings=settings).redacted_text


def redact_payload(
    obj: Any,
    *,
    purpose: RedactionPurpose = RedactionPurpose.LOG,
    settings: SecuritySettings | None = None,
) -> Any:
    """递归脱敏结构化数据（dict / list / tuple）。

    对 dict 中字段名为凭据字段**且值不是容器**的项做整体掩码；容器值**递归进入**
    而非整体替换（解 D6）；str 走 :func:`redact_text`；其余原样返回。

    Args:
        obj: 任意嵌套的 dict / list / tuple / str / 其他。
        purpose: 脱敏出口语义。
        settings: 脱敏配置（``None`` 回落 ``SecuritySettings()``）；见 :func:`scan_text`。
            已知敏感值经此递归透传（``settings`` 即唯一配置通道）。

    Returns:
        脱敏后的同构数据结构。
    """
    if isinstance(obj, dict):
        # 单次回落：凭据字段整体掩码与递归调用统一使用同一份 ``effective_settings``
        # （此前只有递归调用才传 ``settings``，掩码分支直接用硬编码阈值）。
        effective_settings = settings if settings is not None else SecuritySettings()
        style = mask_style_for(purpose)
        result: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and _is_credential_field(key) and not isinstance(value, (dict, list, tuple)):
                result[key] = mask(
                    str(value),
                    kind="credential",
                    style=style,
                    partial_min_len=effective_settings.redact_partial_min_len,
                    partial_head=effective_settings.redact_partial_head,
                    partial_tail=effective_settings.redact_partial_tail,
                )
            else:
                result[key] = redact_payload(value, purpose=purpose, settings=effective_settings)
        return result
    if isinstance(obj, list):
        return [redact_payload(item, purpose=purpose, settings=settings) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact_payload(item, purpose=purpose, settings=settings) for item in obj)
    if isinstance(obj, str):
        return redact_text(obj, purpose=purpose, settings=settings)
    return obj


def count_sensitive_hits(text: str, *, settings: SecuritySettings | None = None) -> dict[str, int]:
    """统计文本中的敏感信息命中数（只计数、不落明文）。

    与 ``redact_text`` 共享同一 ``scan_text``，消除「脱敏口径」与「审计口径」漂移。
    保持旧的三键口径（``vendor_token`` / ``credential_key_value`` / ``total``）；
    ``credential_key_value`` 现由 assignment detector 提供**真实计数**
    （``raw_rule_hits`` 中所有 ``assignment.*`` 键之和）。

    ``total`` 以 vendor + credential_key_value 为口径 —— 裸熵（``entropy.bare_*``）
    是兜底命中的**独立命名空间**，其计数保留在 ``ScanResult.raw_rule_hits`` 中供
    误伤校准，不并入此处三键（避免与 vendor/assignment 同口径重叠导致重复计数）。

    Args:
        text: 待统计文本。
        settings: 脱敏配置（``None`` 回落 ``SecuritySettings()``）；见 :func:`scan_text`，
            已知敏感值同样经此传入。

    Returns:
        命中计数字典：``vendor_token`` / ``credential_key_value`` / ``total``。
    """
    result = scan_text(text, purpose=RedactionPurpose.LOG, settings=settings)
    vendor_token = sum(count for rule_id, count in result.raw_rule_hits.items() if rule_id.startswith("vendor."))
    credential_key_value = sum(
        count for rule_id, count in result.raw_rule_hits.items() if rule_id.startswith("assignment.")
    )
    return {
        "vendor_token": vendor_token,
        "credential_key_value": credential_key_value,
        "total": vendor_token + credential_key_value,
    }


def redact_for_export(obj: Any) -> Any:
    """导出 / 上报前的强制脱敏入口（等价于 ``purpose=EXPORT``）。"""
    if isinstance(obj, str):
        return redact_text(obj, purpose=RedactionPurpose.EXPORT)
    return redact_payload(obj, purpose=RedactionPurpose.EXPORT)


# ===== 已知值精确替换（工具返回值出口）=====
#
# 复用同包 ``RegisteredSecretDetector`` 做匹配 —— 见模块 docstring「共享匹配机制的原因」。
# **禁止** import ``aidev_agent.config`` / backend 类型 / ``core`` / ``services`` / ``api``：
# 配置解析留在调用方（core），以纯 ``list[str]`` 传入。

# 工具返回值脱敏占位符（唯一真源 —— legacy 值，用户可见文案，不可与 REDACT_PLACEHOLDER 统一）
KNOWN_VALUES_PLACEHOLDER = "__BKAI_AGENT_REDACTED__"


def redact_known_values(text: str, sensitive_values: Sequence[str]) -> str:
    """已知敏感值精确替换（工具返回值出口唯一实现）。

    匹配复用 :class:`~.detectors.RegisteredSecretDetector`（与 ``scan_text`` 的
    ``RegisteredSecretDetector`` **同一机制**）：组内按值长度降序，长值优先命中 ——
    避免短值先把长值切碎（``["abc", "abcdef"]`` 对 ``abcdef`` 不会只剩残段）。

    与 ``scan_text`` 的分工 / 占位符差异见模块 docstring。

    Args:
        text: 待脱敏文本。
        sensitive_values: 需要脱敏的敏感值列表（裸 ``str``，**不带 kind**）。

    Returns:
        脱敏后的文本（敏感值替换为 :data:`KNOWN_VALUES_PLACEHOLDER`）。
    """
    if not sensitive_values or not text:
        return text

    detector = RegisteredSecretDetector.from_values(sensitive_values)
    findings = merge_spans(detector.scan(text))
    if not findings:
        return text

    # 从后往前替换（避免位移），与 ``_apply_replacements`` 同策略；
    # 此处用固定占位符（非 ``mask()``）—— 出口文案契约见模块 docstring。
    # ``merge_spans`` 负责消解重叠：``["abc", "abcdef"]`` 对 ``abcdef`` 只产出一个
    # 覆盖全长的 span，而非「长值替换后被短值从内部再次替换」。
    result = text
    for finding in sorted(findings, key=lambda f: f.start, reverse=True):
        result = result[: finding.start] + KNOWN_VALUES_PLACEHOLDER + result[finding.end :]
    return result


__all__ = [
    # 文本脱敏 / 扫描
    "redact_text",
    "scan_text",
    # 结构化脱敏
    "redact_payload",
    "redact_for_export",
    # 命中统计
    "count_sensitive_hits",
    # 已知值精确替换（工具返回值出口）
    "redact_known_values",
    "KNOWN_VALUES_PLACEHOLDER",
]
