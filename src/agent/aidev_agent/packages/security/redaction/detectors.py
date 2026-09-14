# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.redaction.detectors

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

脱敏检测器（合并单模块）。

原 ``detectors/`` 子包的 12 个文件（``base`` / ``vendor`` / ``url`` / ``jwt`` / ``headers`` /
``dsn`` / ``pem`` / ``assignment`` / ``cookie`` / ``registered`` / ``entropy`` /
``__init__``）合并为单文件。合并**只做搬运**：正则字面量、优先级数值、
kind / rule_id / confidence 字符串、判定分支顺序、span 计算**逐字未改**。

合并消除的 intra-package import 边（原为跨模块引用，现为同文件引用）：
- ``vendor -> base``（``DetectorRule``）
- ``cookie -> assignment``（字段名归一化，现为模块级 ``_normalize_field_name`` 共享）
``detectors/__init__.py`` 的整体聚合转发被删除，其 ``__all__`` 语义由本模块继承。

**单模块内的分层（``260915-0dr`` 合并后）**：本模块自上而下分三层，
与合并前的模块依赖方向逐条对应 ——

1. **PEM 原语**（原 ``redaction/pem.py``，零依赖叶子）：``normalize_label`` /
   ``classify_pem_label`` / ``PemBlock`` / ``iter_pem_blocks``。
2. **共享统计工具**（原 ``redaction/entropy.py``，依赖上面的 PEM 原语）：
   ``shannon_entropy`` / ``char_class_count`` / ``classify_alphabet`` / 形状判据 /
   ``is_pem_non_secret_span`` / ``is_base64_container_span``。
3. **detector 类**（依赖上述两层）。

``classify_pem_label`` 仍是 label 分类的**唯一**判据 —— ``PemDetector``（产 finding 侧）
与 ``is_pem_non_secret_span``（熵侧排除）都在同文件内调用它。合并把原先「跨模块单向依赖」
变成「同文件定义」，故 **PemDetector 与熵侧共用同一实现是结构性保证**：
在类内重写一份 PEM 谓词会重现**缺陷 7/8 的 fail-open 明文泄露漂移**
（两份 PEM 分类实现分叉，CI 全绿也发现不了）。
合并使「跨模块实现分叉」在结构上**不可能发生**；代价是 4 个「跨模块零复制」
护栏（`is` 同一性 / import 面 / helper 与源实现比对）变成恒真式而被删除，
其判别力由 ``tests/packages/security/test_detectors.py::TestMergedModuleIntegrity``
**常驻接管**（CI 每次运行；经 11 组植入实证判别力）。

**模块级私有名（28 个）全部来自原 ``pem`` / ``entropy`` 的模块级定义**（pem 10 + entropy 18）
—— 它们是文件顶部三层的内部常量，模块级才是正确位置。
**本模块原有 detector 类的私有常量一律留在各自类体内（不得摊平到模块级）** ——
``_VAR_REF_RE`` / ``_HINT_RE`` / ``_is_variable_reference``
在 ``assignment`` / ``dsn`` / ``url`` 三个 detector 中同名但**字面量 / 实现不同**；
摊到模块级会静默遮蔽（晚绑定的模块全局），行为漂移且**无报错**。
类命名空间隔离是「合并前各文件已完成封装」（T-vq0）建立的不变量，合并**不得回退**。

**detector 类的定义顺序是硬约束**（``from __future__ import annotations`` 只延迟注解，
不延迟类体内的表达式求值）：
- ``DetectorRule`` 必须在 ``VendorTokenDetector`` 之前 ——
  后者的 ``_VENDOR_RULES`` 在**类体内立即调用** ``DetectorRule(...)``。
- ``RegisteredValue`` 必须在 ``RegisteredSecretDetector`` 之前
  （字段类型 + ``from_values`` 内 ``isinstance`` 检查）。

**三层的定义顺序（pem → entropy → detectors）是审查基线，不是 correctness 约束** ——
三层的跨模块引用**全部**位于函数体 / 方法体内（延迟绑定），
故任何排列都能 import 并正确运行（planning time 实测 6 种排列全部通过）。
固定此顺序只为可读性与便于逐行核对搬运完整性。

**依赖方向**：``packages.security`` 约定 —— 仅标准库 / pydantic / langchain_core /
``pydantic_models`` / 本包内模块；**禁止** ``core`` / ``services`` / ``api``。

**预筛选所有权（T-vq0，缺陷 3 根因防线）**：``could_match``
仍是 ``Detector`` 协议的一部分，但**调用方不再负责调用它** ——
每个 ``scan`` 的实现必须在自身开头调用一次自家的 ``could_match``
并在为 False 时立即返回 ``[]``。
``redaction.operations`` **不再显式调用** ``detector.could_match()``
（避免同一预筛选跑两遍，也让「新 detector 忘记接入预筛选」不可能因调用方遗漏而复现）。

公开接口：``Detector`` + 11 个 detector（见 ``__all__``）。``DetectorRule`` / ``RegisteredValue``
按名可导入（它们是原**子模块**公开面），但**不进** ``__all__`` ——
与原 ``detectors/__init__.py`` 的包公开面逐字相同（11 名），故 ``import *`` 可见面不变。

**其余安全理由（逐条随实现保留）**：缺陷 1（query string 值吞掉 ``&``）/
缺陷 2（变量引用误判致真实 secret 漏报）/ 缺陷 3（``_KEY_VALUE_RE`` O(n²) 与预筛选缺失）/
缺陷 4（PEM 非秘密块被裸熵二次遮掉）/ 缺陷 5（``\\b`` 锚点丢弃数字前缀键）/
缺陷 D9（DSN / cookie / PEM 的结构保真）/ 缺陷 D10（短 ``Bearer`` 与 ``Basic`` 凭据漏检）/
T-06-10 ~ T-06-24 系列（ReDoS 有界量词、JWT 整体替换、变量引用例外等）/
T-06-12（ReDoS 防护）/ T-06-13（绝不重新序列化 URL）/ T-06-14（负例排除三层防线）/
T-06-15（JWT 三段 + 五段）/ T-06-17（源码模板例外）/ T-06-21（候选有界量词）/
T-06-22（裸熵独立 rule_id 便于校准）/ T-06-23（裸熵最低优先级不覆盖结构化证据）/
T-06-24（不读环境变量，阈值 / 开关经构造参数注入）/ T-urf-02（已知值显式注入，无模块级态）/
T-urf-03（partial 掩码阈值由 settings 派生）/ D-08（``data:`` / ``;base64,`` 容器硬排除）。
各条理由的完整正文见对应实现处的 docstring 与注释。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, NamedTuple, Protocol

from aidev_agent.packages.security.redaction.findings import Finding

# ===== PEM 解析 / 分类（原 redaction/pem.py，260915-0dr 并入本文件）=====
#
# 本段是「什么是完整且 label 匹配的 PEM 块」与「某个 label 属于哪一类」的**唯一**真源，
# 被 detector 侧（``PemDetector``）与熵侧（``is_pem_non_secret_span``）共同依赖。
#
# **ReDoS 防护（T-06-12）**：跨行匹配用 ``re.DOTALL``，但 body 用**有界**量词（``{0,65536}``）；超过上限则不产出块（宁可漏也不让热路径失控）。

# LABEL 白名单（private key 类）—— 成员必须是**归一化后**的 label（见 normalize_label）
_PRIVATE_LABELS: tuple[str, ...] = (
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
    "PGP PRIVATE KEY BLOCK",
    "PRIVATE KEY",
)

# 非 private 的 label（显式排除，用于可读性与文档化）—— 同为归一化后形式。
# 仅这些**精确** label 才被视为非秘密；白名单匹配取代此前的正则字符串前缀匹配。
_NON_PRIVATE_LABELS: tuple[str, ...] = (
    "PUBLIC KEY",
    "RSA PUBLIC KEY",
    "EC PUBLIC KEY",
    "DSA PUBLIC KEY",
    "OPENSSH PUBLIC KEY",
    "CERTIFICATE",
    "X509 CERTIFICATE",
    "TRUSTED CERTIFICATE",
    "PGP PUBLIC KEY BLOCK",
    "SSH2 PUBLIC KEY",
    # CSR（PKCS#10）—— 含公钥与主体名，不含私钥material（RFC 2986）。
    # README 策略：「公钥、证书、CSR 不因 PEM 包装成为秘密，本测试保留它们」。
    # 此前缺失这两个 label ⇒ classify 落 `unknown` ⇒ fail-closed 当秘密
    # ⇒ ``EntropyDetector`` 逐行把 CSR body 当 base64 秘密脱敏（S037）。
    "CERTIFICATE REQUEST",
    "NEW CERTIFICATE REQUEST",
)

# body 有界量词上限（T-06-12：超限不命中，避免无界扫描）
_BODY_MAX = 65536

# BEGIN ... END 两段独立匹配（label 一致性由代码比对，不用 backreference）
_BEGIN_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+?)-----")
_END_RE = re.compile(r"-----END ([A-Z0-9 ]+?)-----")

# 4 短横 RFC 4716 形态（``---- BEGIN X ----``）；仅用于「非秘密块排除」识别，
# 与 5 短横 private key 匹配相互独立（见 iter_pem_blocks 的 dash 参数）。
_RFC4716_BEGIN_RE = re.compile(r"---- BEGIN ([A-Z0-9 ]+?) ----")
_RFC4716_END_RE = re.compile(r"---- END ([A-Z0-9 ]+?) ----")

# 归一化后的 label 集合（frozenset 便于 O(1) 判定）
_NON_SECRET_LABEL_SET = frozenset(_NON_PRIVATE_LABELS)
_PRIVATE_LABEL_SET = frozenset(_PRIVATE_LABELS)

# label 内部分隔符：PEM label 是 RFC 定义的固定词表，空白（空格 / 制表符等）
# 只有分隔语义、无区分语义，故一律折叠为单个空格并 strip。
_LABEL_WS_RE = re.compile(r"\s+")


def normalize_label(raw: str) -> str:
    """把 BEGIN/END 捕获到的原始 label 归一化为**规范形式**。

    这是「两个 label 是否相同」「label 是否属于某集合」的**唯一**规范来源
    （缺陷 8 / fix 轮 4）：``iter_pem_blocks`` 在解析时即归一化，
    故下游（``PemDetector`` 与 ``is_pem_non_secret_span``）看到的是同一个字符串，
    不可能再各自对原始空白做不同解释。

    规则：折叠所有内部空白（正则 ``\\s+``，含空格 / 制表符）为单个空格，并 strip 首尾。
    PEM label 是 RFC 定义的固定词表（``PUBLIC KEY`` / ``RSA PRIVATE KEY`` …），
    空白差异不是语义差异 ——
    ``-----BEGIN  PUBLIC KEY-----``（双空格）与 ``-----BEGIN PUBLIC KEY-----`` 指同一 label。

    **安全影响（fail-closed）**：仅当原始 label 折叠后**精确等于**
    验证白名单中的某个非秘密 label 时才会被排除。
    归一化使得「空白变体」与规范形等价，从而与 detector 判定一致
    （此前 ``' PUBLIC KEY'`` 被正则前缀匹配误判为非秘密、
    却不被 ``PemDetector`` 识别，**两边都不遮**而泄露）。
    近义但不精确的 label（``PUBLIC KEYX`` / ``X509`` 等）归一化后仍不在白名单
    → 不排除 → 遮（fail-closed）。

    Args:
        raw: ``iter_pem_blocks`` 正则捕获的原始 label（可能含前导 / 内部 / 尾随空白）。

    Returns:
        归一化 label（空白折叠为单空格 + strip）。
    """
    return _LABEL_WS_RE.sub(" ", raw).strip()


def classify_pem_label(label: str) -> str:
    """把（**已归一化**的）PEM label 分类为 ``"non_secret"`` / ``"secret"`` / ``"unknown"``。

    这是 label 分类的**唯一**判据（缺陷 8）：
    ``PemDetector`` 与 ``is_pem_non_secret_span`` 都只通过本函数决定「要不要保留」，
    二者因此不可能再对同一 label 给出不同结论。
    此前两端各用一套平行检查（私有集合成员 vs 正则前缀匹配），正是漂移来源。
    合并（``260915-0dr``）后二者同处本文件，共用同一实现是**结构性**保证。

    调用方**必须**传入 ``normalize_label()`` 的结果；
    本函数不重复归一化（避免「谁负责归一化」的二义性）。

    Args:
        label: 已归一化的 label。

    Returns:
        ``"non_secret"``（公钥 / 证书，可字节级保留）/ ``"secret"``（私钥类，必须遮）/
        ``"unknown"``（无法确证 —— 调用方按 fail-closed 处理）。
    """
    if label in _NON_SECRET_LABEL_SET:
        return "non_secret"
    if label in _PRIVATE_LABEL_SET:
        return "secret"
    return "unknown"


class PemBlock(NamedTuple):
    """一个**完整且 BEGIN/END label 一致**的 PEM 块在原文中的位置。

    Args:
        start: 块起点偏移（BEGIN 标记首字符，闭）。
        end: 块终点偏移（END 标记末字符之后，开）。
        label: 块 label（BEGIN 与 END 已确认一致）。
        body_start: body 起点偏移（BEGIN 标记末字符之后）——
            body 必须从此处**另起一行**才有意义；
            暴露给调用方用于「命中是否在 body 行内」的判定。
    """

    start: int
    end: int
    label: str
    body_start: int


def iter_pem_blocks(text: str, *, dash: int = 5) -> list[PemBlock]:
    """枚举文本中**完整且 BEGIN/END label 一致**的 PEM 块。

    这是「什么算一个完整 PEM 块」的**唯一**定义（缺陷 7 回归）：
    ``PemDetector`` 与熵侧的非秘密块排除都消费它，两端语义不可能再漂移。

    **fail-closed 语义**：只有 **BEGIN 之后存在一个 label 完全相同的 END**
    的块才会产出。END 缺失 / label 不一致 /
    本块被上一个未闭合块「吞掉」的块一律**不产出** ——
    调用方因此不会把未闭合或错配的块误判为「已识别 → 可排除」，
    未识别的 PEM-ish body 会继续走裸熵兜底被遮（宁遮不漏）。

    参数 ``dash`` 选择拼写：
      * ``5``：标准 ``-----BEGIN X-----`` / ``-----END X-----``（本 detector 使用）；
      * ``4``：RFC 4716 ``---- BEGIN X ----`` / ``---- END X ----``（非秘密排除使用）。

    匹配算法与旧 ``PemDetector.scan`` 逐字一致（分组捕获 + 代码比对 label，
    非 backreference；body 起点到 END 起点不超过 ``_BODY_MAX``，超限不产出），
    故对既有 5 短横行为是**零语义变更**的抽取。

    Args:
        text: 原始文本。
        dash: ``5`` 或 ``4``，选择短横拼写。

    Returns:
        ``PemBlock`` 列表（按 BEGIN 出现顺序）。
    """
    if not isinstance(text, str) or not text:
        return []
    if dash == 4:
        begin_re, end_re = _RFC4716_BEGIN_RE, _RFC4716_END_RE
    else:
        begin_re, end_re = _BEGIN_RE, _END_RE

    blocks: list[PemBlock] = []
    for begin in begin_re.finditer(text):
        # 归一化 **在解析时** 完成（缺陷 8 根因修复）：PemBlock.label 保证是规范形式，
        # 故 BEGIN/END 一致性比对与下游分类都建立在同一字符串上。
        # ``-----BEGIN  PUBLIC KEY-----``（双空格）→ ``'PUBLIC KEY'``。
        label = normalize_label(begin.group(1))
        end = end_re.search(text, begin.end())
        if end is None:
            continue
        # BEGIN/END label 必须一致（代码比对，非 backreference）—— 比对**归一化后**的 label：
        # ``BEGIN  PUBLIC KEY`` + ``END PUBLIC KEY``（空白不对称）视为同一 label，与 PEM 语义一致（空白不承载信息），
        # 并避免「空白变体」落到 unknown 而含糊。
        if normalize_label(end.group(1)) != label:
            continue
        # body 有界（T-06-12）：超限不产出
        if end.start() - begin.end() > _BODY_MAX:
            continue
        blocks.append(PemBlock(begin.start(), end.end(), label, begin.end()))
    return blocks


# PEM body 中的「非实质内容」字符：base64 折行产生的空白 + 教学省略号。
# 一个块若 BEGIN/END 之间**只**含这些字符，说明它是「空壳 / 教学省略占位」，
# 而非真实密钥 —— README 策略：
#   「单独的BEGIN标记和明确的教学省略占位符保持原样，**有实质内容的**截断私钥则遮罩」。
_PEM_NON_SUBSTANTIVE_RE = re.compile(r"^[\s.\u2026]*$")


def is_substantive_pem_body(text: str, block: PemBlock) -> bool:
    """判断 PEM 块的 body 是否含**实质内容**（真实 base64 材料）。

    判据：BEGIN 与 END 之间除空白 / 教学省略号（``...`` / ``…``）外，
    至少存在一个其他字符。用于区分：

    - **空壳 / 教学省略**：``-----BEGIN PRIVATE KEY-----``（无配对 END，或
      ``-----BEGIN X----- ... -----END X-----``）⇒ 不含实质内容 ⇒ 不该脱敏（S115/S116）；
    - **真实（含截断）私钥**：body 含 base64 字符 ⇒ 含实质内容 ⇒ 照常脱敏。

    Args:
        text: 原始文本（用于按偏移切片）。
        block: :func:`iter_pem_blocks` 产出的块。

    Returns:
        True 表示 body 含实质内容。
    """
    body = text[block.body_start : block.end]
    # 剥离 END 标记行：END 标记以 ``-----END`` 或 ``---- END`` 开头
    end_marker = body.rfind("-----END")
    if end_marker == -1:
        end_marker = body.rfind("---- END")
    if end_marker != -1:
        body = body[:end_marker]
    return not bool(_PEM_NON_SUBSTANTIVE_RE.match(body))


# ===== 共享统计工具（原 redaction/entropy.py，260915-0dr 并入本文件）=====
#
# 共享统计工具（Shannon 熵 / 字符类别 / 字符集分类 / 形状判据 / base64 容器排除）。
#
# PR3 把原先内联在 ``detectors/assignment.py`` 的 ``_shannon_entropy`` / ``_char_class_count`` /``_is_uuid_like`` / ``_is_fixed_hex`` **收口**到这里（公共名，去下划线），
# ``assignment.py`` 改为 import —— 消除跨模块重复实现，
# 也让裸熵 detector 与歧义字段评分共用同一份判据（DESIGN §3.5 / §3.6）。
#
# **判据是形状，不是熵**（DESIGN §3.5 明确）：长度 8 的 hex 空间小、长度 64 的 SHA-256 熵可达 4.0+，
# 故排除项一律走形状（纯 hex + 特定长度 / UUID / 连续序列 /重复串），熵只用于「是否达到阈值」的加分与触发判定。
#
# 纯统计工具段：无 I/O、无 registry 依赖、不读环境变量。

# 字符集分类标签（classify_alphabet 返回值）
_ALPHABET_ALNUM = "alnum"
_ALPHABET_BASE64 = "base64"
_ALPHABET_BASE64URL = "base64url"
_ALPHABET_OTHER = "other"

# 纯 hex 排除长度形状（GPG key ID / fingerprint / 常见 digest）—— 形状判据，不依赖熵。
#
# **64 已移出**（S057/S058/S076/S081 召回修复）：长度 64 是 SHA-256 / HMAC / AES-256
# 密钥的**标准宽度**，且其熵可达 3.6~4.0 —— 与「业务 digest」无法用形状区分，
# 一律豁免会把「hex 编码的真实密钥」整体漏掉。故 64 位交给熵/上下文判据决定：
# - 裸熵路径（``EntropyDetector``）熵 >= 4.2 才命中 ⇒ 本测试值的 3.675 仍不命中，
#   故「裸 64-hex digest 不脱敏」的既有行为**未变**（见 test_entropy 的 64-hex 负例）；
# - 赋值路径由字段名上下文决定（``secret`` 后缀 +2 等）。
# 8 / 16 / 32 / 40 保留豁免：固定宽度小 ID（GPG key ID、fingerprint、git short SHA）
# 在源码与日志里极常见，且熵本就低于阈值，豁免是净收益。
_HEX_EXCLUDE_LENGTHS = frozenset({8, 16, 32, 40})
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

# UUID 标准形（带连字符 36 位）
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# ULID（26 位 Crockford base32）/ KSUID（27 位 base62）
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_KSUID_RE = re.compile(r"^[0-9A-Za-z]{27}$")

# 字符集成员判定
_ALNUM_RE = re.compile(r"^[A-Za-z0-9]+$")
# 标准 Base64：含 + / 或 padding =，其余为 base64 字符
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_BASE64_CHARS_RE = re.compile(r"^[A-Za-z0-9+/]+$")
# Base64URL：含 - 或 _，其余为 base64url 字符
_BASE64URL_CHARS_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# base64 容器前缀（D-08 硬排除）：命中区间落在这些前缀之后则跳过
_BASE64_CONTAINERS: tuple[str, ...] = ("data:", ";base64,")
# 容器前缀与命中区间之间若出现「空白 / 引号」，
# 说明命中是独立 token 而非载荷（data URI 的 MIME 段 `text/plain;base64,` 里的 `;` `/` `,` 是 URI 语法，不算分隔）
_CONTAINER_GAP_RE = re.compile(r"[\s\"'`]")

# ---------------------------------------------------------------------------
# PEM 非秘密块硬排除（缺陷 4 / 缺陷 6 / 缺陷 7）
#
# PemDetector 已正确排除公钥 / 证书（LABEL 白名单），
# 但它**不产出 finding**，故裸熵兜底仍会把 ``-----BEGIN PUBLIC KEY-----`` 的 base64 body 当裸密文遮掉，
# 使「公钥不被触碰」的显式负例失效（误伤：破坏正常配置 / 信任链）。
#
# 此处以 **span 位置谓词**实现排除（与上面的 D-08 data: 容器排除同构，不做正则改写），
# 但谓词的**判定依据是「解析出的完整块」而非「孤立的 BEGIN 标记」**。
#
# 缺陷 7（fail-open 泄露回归）：早先的实现在命中之前找**最近的非秘密 BEGIN**，只看 BEGIN label、只要求「BEGIN 与命中之间无 END」，
# 于是「BEGIN PUBLIC KEY + END RSA PRIVATE KEY（错配）」「BEGIN PUBLIC KEY + 无 END（未闭合）」
# 这两类畸形块被熵侧判为「可排除」，同时 PemDetector 因 BEGIN != END 而拒绝动作 ——**两边都不遮**，body 原样泄露。
# 这是比误伤严重得多的 fail-open。
#
# 现在改为：委托 ``iter_pem_blocks``（与 PemDetector 共享的唯一解析源，定义在本文件上方）
# 枚举**完整且 BEGIN/END label 一致**的块，命中区间只有**落在某个已完整识别的非秘密块内**才排除。
# 无法正识别为「完整的非秘密块」→ **不排除** → 裸熵兜底遮掉（fail-closed）。
#
# 缺陷 8（fix 轮 4）：此前**分类本身**仍是两套平行检查 —— 本段用正则``[A-Z0-9 ]*PUBLIC KEY`` 做前缀匹配，
# PemDetector 用精确集合成员判定。二者对畸形 label 结论不一致：``iter_pem_blocks`` 曾保留 label 的原始空白，
# 于是``-----BEGIN  PUBLIC KEY-----``（双空格）产出 label ``' PUBLIC KEY'`` —— 被前者前缀匹配判为非秘密（排除），
# 却不在后者集合内（不产 finding）→ **两边都不遮**， body 原样泄露（fail-open）。现在：
# ``iter_pem_blocks`` **解析时即归一化** label（折叠内部空白 + strip），
# 本段与 PemDetector 都委托 ``classify_pem_label``（**唯一**分类判据）判定。
# ``non_secret`` 白名单为**精确** label 集合：
# ``PUBLIC KEY`` / ``RSA|EC|DSA|OPENSSH PUBLIC KEY`` / ``CERTIFICATE`` / ``X509 CERTIFICATE`` / ``TRUSTED CERTIFICATE`` /``PGP PUBLIC KEY BLOCK`` / RFC 4716 ``SSH2 PUBLIC KEY``；private key 块**不在**排除集内 —— PemDetector（priority 95）
# 负责整块替换；``unknown`` 一律不排除（fail-closed）。
# 4 短横 RFC 4716 形态（``---- BEGIN SSH2 PUBLIC KEY ----``）同样经共享解析器识别。
# ---------------------------------------------------------------------------

# 非秘密 label 分类**不再**用正则前缀匹配 —— 那是缺陷 8 的第二个漂移点：
# ``[A-Z0-9 ]*PUBLIC KEY`` 会匹配 ``' PUBLIC KEY'``（前导空白），而 PemDetector 按精确集合判定，
# 于是畸形 label 被熵侧放过、又被 detector 拒绝 → 两边都不遮而泄露。
# 现统一委托 ``classify_pem_label``（label 已在 iter_pem_blocks 内归一化），两端共用**同一**判据，不可能再漂移。

# BEGIN 与命中之间允许的内容：换行起头（PEM body 必须起于新行），其后可为空行 /前导空白 / body 字符。禁止在同一行内（未换行）
# 直接出现命中 —— 保证``CERTIFICATE AUTHORITY=...`` 这类「label 后接空格 + 普通赋值」的普通文本不放过。
_PEM_BODY_GAP_RE = re.compile(r"^\r?\n[\s\S]*$")

# 连续序列判据：单一相邻字符差值占比阈值
_SEQUENCE_STEP_RATIO = 0.9
# 重复串判据：去重后字符数不超过此值即视为重复
_REPETITION_UNIQUE_MAX = 2


def shannon_entropy(value: str) -> float:
    """计算 Shannon 熵（bit/字符，底为 2）。

    空串与单字符返回 ``0.0``（无不确定性）。

    Args:
        value: 待计算字符串。

    Returns:
        Shannon 熵值（bit/字符）。
    """
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def char_class_count(value: str) -> int:
    """统计字符类别数（upper / lower / digit / symbol 四类命中几类）。

    Args:
        value: 待统计字符串。

    Returns:
        命中类别数（0-4）。
    """
    classes = 0
    if any(c.isupper() for c in value):
        classes += 1
    if any(c.islower() for c in value):
        classes += 1
    if any(c.isdigit() for c in value):
        classes += 1
    if any(not c.isalnum() for c in value):
        classes += 1
    return classes


def classify_alphabet(value: str) -> str:
    """判断候选值的字符集归属。

    ``EntropyDetector`` 现自持本判据（T-vq0）；本定义保留以兼容 ``__all__``
    与既有导入面，二者由
    ``test_detectors.py::TestDetectorConstantEncapsulation::test_entropy_self_hosts_moved_helpers``
    与源实现钉住同结果。

    Args:
        value: 待分类字符串。

    Returns:
        ``"alnum"``（仅 ``[A-Za-z0-9]``）/ ``"base64"``（含 ``+`` ``/`` 或 padding ``=``）/
        ``"base64url"``（含 ``-`` 或 ``_``）/ ``"other"``（不符合上述任一）。
    """
    if not value:
        return _ALPHABET_OTHER
    if _ALNUM_RE.match(value):
        return _ALPHABET_ALNUM
    if _BASE64_RE.match(value) and ("+" in value or "/" in value or "=" in value):
        return _ALPHABET_BASE64
    if _BASE64URL_CHARS_RE.match(value) and ("-" in value or "_" in value):
        return _ALPHABET_BASE64URL
    return _ALPHABET_OTHER


def is_uuid_like(value: str) -> bool:
    """判断是否为 UUID / ULID / KSUID 形状。

    与 :func:`is_fixed_hex` 在「无连字符 32 位 hex」上有重叠 —— 两者可同时为 True，
    调用方按「任一为真即排除」处理，重叠无害。

    Args:
        value: 待判断字符串。

    Returns:
        True 表示形如标准 UUID（36 位带连字符）/ 无连字符 32 位 hex / ULID（26）/
        KSUID（27）；ULID / KSUID 要求至少含一个数字，避免误伤纯字母单词。
    """
    if _UUID_RE.match(value):
        return True
    if len(value) == 32 and _HEX_RE.match(value):
        return True
    if len(value) == 26 and _ULID_RE.match(value) and any(c.isdigit() for c in value):
        return True
    return len(value) == 27 and bool(_KSUID_RE.match(value)) and any(c.isdigit() for c in value)


def is_fixed_hex(value: str) -> bool:
    """判断是否为「纯 hex + 长度 ∈ {8,16,32,40}」形状（GPG key ID / fingerprint）。

    这是**形状判据**而非熵判据 —— 实测真实 fingerprint 熵约 3.4-3.8 本就低于阈值，
    长度 8 的 hex 空间小，故形状判据更稳。

    **64 不在集内**：64 位是 SHA-256 / AES-256 密钥的标准宽度，与业务 digest
    无法用形状区分，一律豁免会漏掉 hex 编码的真实密钥（S057 族）。
    故 64 位的「是否脱敏」由熵阈值与字段名上下文决定，而非形状。

    Args:
        value: 待判断字符串。

    Returns:
        True 表示纯 hex 且长度命中排除集。
    """
    return bool(_HEX_RE.match(value)) and len(value) in _HEX_EXCLUDE_LENGTHS


# 占位符形态：值的**本身**就是一个「此处应有值但未填」的标记，
# 而非指向别处的引用（后者由各 detector 的 ``_VAR_REF_RE`` 负责）。
#
# 与 `_VAR_REF_RE` 的分工（T-06-17 只覆盖了前者，本判据补齐后者）：
#   - 引用型：`${VAR}` / `$VAR` / `{var}` / `os.getenv(...)` —— 取值来自运行环境；
#   - 占位符型：`null` / `********` / `<REDACTED>` / `{{ jinja }}` / `!vault` /
#     `...` / `changeme` —— 取值**根本不存在或已被人工遮蔽**。
# 两者都不该被当作真实 secret 替换：前者替换会破坏配置模板，后者替换会污染
# 已经脱敏过的文本（二次脱敏应幂等 —— README「二次脱敏结果应不变」）。
_PLACEHOLDER_RE = re.compile(
    r"""(?ix)^(
       null|none|nil|undefined|empty            # 空值关键字
      |\*{3,}|\.{3,}|-{3,}|_{3,}                # 星号 / 省略 / 分隔线掩码
      |[<\[\(]\s?(redacted|hidden|removed|masked|omitted|secret)[^>\])]*\s?[>\]\)]  # 已脱敏标记
      |\{\{.*?\}\}                              # Jinja / Ansible Vault 模板
      |![a-z_]+                                 # YAML tag（`!vault` / `!secret`）
      |change\s?me|todo|placeholder|example|your[_-]?\w*
    )$""",
)


def is_placeholder_value(value: str) -> bool:
    """判断值本身是否为「占位符 / 已脱敏标记」而非真实凭据。

    Args:
        value: 待判断的值本体（不含 key 与分隔符）。

    Returns:
        True 表示该值是占位符，调用方应跳过脱敏。

    Note:
        与 :func:`is_fixed_hex` / :func:`is_uuid_like` 同为**形状判据**，不依赖熵。
        判据刻意保持精确字符串全等，不做「包含」匹配 ——
        否则 `my-password-example` 这类真实值会被误豁免。
    """
    if not value:
        return False
    return bool(_PLACEHOLDER_RE.match(value))


def is_monotonic_sequence(value: str) -> bool:
    """判断是否为连续递增 / 递减序列（``abcdefg…`` / ``123456…`` / 反向）。

    ``EntropyDetector`` 现自持本判据（T-vq0）；本定义保留以兼容 ``__all__``
    与既有导入面，二者由
    ``test_detectors.py::TestDetectorConstantEncapsulation::test_entropy_self_hosts_moved_helpers``
    与源实现钉住同结果。

    判据：对相邻字符差值取 ``Counter``，若单一差值占绝对多数（``>= len-2`` 次）
    即判为序列。长度 < 4 无意义，直接返回 False。

    Args:
        value: 待判断字符串。

    Returns:
        True 表示形如连续序列。
    """
    if len(value) < 4:
        return False
    steps = Counter(ord(b) - ord(a) for a, b in zip(value, value[1:]))
    return steps.most_common(1)[0][1] >= len(value) - 2


def is_repetition(value: str) -> bool:
    """判断是否为重复串（同一字符反复，或去重后字符极少）。

    ``EntropyDetector`` 现自持本判据（T-vq0）；本定义保留以兼容 ``__all__``
    与既有导入面，二者由
    ``test_detectors.py::TestDetectorConstantEncapsulation::test_entropy_self_hosts_moved_helpers``
    与源实现钉住同结果。

    Args:
        value: 待判断字符串。

    Returns:
        True 表示去重后字符数 <= 2（如 ``aaaa…`` / ``ababab…``）。
    """
    if len(value) < 4:
        return False
    return len(set(value)) <= _REPETITION_UNIQUE_MAX


# OpenSSH 公钥算法前缀（单行 ``<算法> <base64> [comment]`` 形态，无 PEM 包装）。
# 公钥不是秘密；裸熵兜底须据此前缀排除其 body（S035）。
# 只列**公钥**算法 —— ``OPENSSH PRIVATE KEY`` 是 PEM 包装的私钥，由 PemDetector 处理，
# 不在此列（否则会把私钥 body 误放过）。
_PUBLIC_KEY_PREFIXES: tuple[str, ...] = (
    "ssh-ed25519",
    "ssh-rsa",
    "ssh-dss",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)

# 公钥前缀与其 body 命中之间的允许内容：空白 + base64 字符（**含**被熵候选字母表
# 排除的 ``/`` 与 ``=`` 填充）。用于 :func:`is_public_key_span` 的回退扫描 ——
# body 因 ``/`` 被切碎时，命中起点落在中段，中间必然含有 base64 字符。
_PUBLIC_KEY_GAP_RE = re.compile(r"^(?=[\sA-Za-z0-9+/=]*$)[\sA-Za-z0-9+/=]*")

# 保留字段名（**公开**密码学参数，不是凭据）：`nonce=` / `salt=` / `iv=` 等。
# 含义与 assignment 的 `_EXCLUDED_FIELD_NAMES` 一致，但作用于**裸熵**路径 ——
# 该路径不看字段名，故须回看命中前的上下文自行判断。
# 归一化比较（去分隔符 + 小写），与 assignment 同一套归一化逻辑。
#
# 为什么必须做在熵侧：`nonce=<高熵串>` 与 `token=<高熵串>` 在取值上**完全同形**
# （S120 熵 4.88），assignment 的字段名排除拦不住熵兜底，实测 S120 会被
# `entropy.bare_alnum` 遮掉 —— 必须在此按「命中值属于哪个字段」再拦一次。
_NON_SECRET_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "nonce",
        "salt",
        "iv",
        "initializationvector",
        "initialisationvector",
    }
)

# 捕获 `<field>=` / `<field>: ` 中紧邻命中之前的那一段 key
_PRECEDING_KEY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*)\s*[:=]\s*$")


def _normalize_field_name(raw: str) -> str:
    """字段名归一化：去除非字母数字字符并小写（与 ``AssignmentDetector`` 同一口径）。"""
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def is_non_secret_field_value(text: str, start: int) -> bool:
    """判断裸熵命中是否属于「公开密码学参数」字段（`nonce=` / `salt=` / `iv=` 等）。

    判据是 **span 位置 + 紧邻字段名**（与 D-08 / 公钥前缀同构的位置谓词）：
    命中之前须紧邻 ``<field>=`` 或 ``<field>:``，且字段名归一化后落在保留集内。

    Args:
        text: 原始文本。
        start: 命中区间起始偏移（闭）。

    Returns:
        True 表示该命中应作为非秘密参数被保留。
    """
    prefix = text[:start]
    match = _PRECEDING_KEY_RE.search(prefix)
    if match is None:
        return False
    return _normalize_field_name(match.group(1)) in _NON_SECRET_FIELD_NAMES


def is_public_key_span(text: str, start: int, end: int) -> bool:
    """OpenSSH 公钥硬排除（S035）：命中区间是否为 ``<算法> <base64>`` 公钥行。

    OpenSSH 公钥是 ``authorized_keys`` / ``known_hosts`` 里的常见内容，
    body 是 base64 且熵天然高（实测 ``ssh-ed25519`` 的 body 熵 4.78 > 阈值 4.2），
    故裸熵兜底会把它整段遮掉 —— 但公钥不是秘密（README：
    「公钥、证书和 CSR 不因 PEM 包装成为秘密，本测试保留它们」）。

    PEM 侧的 ``is_pem_non_secret_span`` 管不到这种形态：OpenSSH 公钥
    **没有** PEM 的 ``-----BEGIN/END`` 包装，是单行 ``算法 base64 [comment]``。

    判据是 **span 位置谓词**（与 D-08 ``is_base64_container_span`` 同构）：
    命中区间之前须存在一个已知公钥算法前缀，且（前缀, 命中）之间**只含空白与
    base64 字符** —— 即该命中确是该前缀 body 的一部分。

    不能只检查「紧邻前缀」：``_CANDIDATE_RE`` 的字母表**不含** ``/``
    （该字符被保留给 DSN/URL 结构 detector），故含 ``/`` 的 base64 body 会在
    ``/`` 处被切碎，命中起点落在 body **中段**（S035 实测）——
    此时命中前紧邻的是 ``AAAAIIm/`` 而非 ``ssh-ed25519 ``。
    因此回退扫描时允许中间存在 base64 字符，只要最终能找到一个公钥前缀。

    这样 ``ssh-ed25519 AAAAC3...``（含 ``/`` 被切段）的任一片段都被排除，
    而 ``comment=QGBZ...`` 这类**另起 token** 的高熵值仍会被正常脱敏
    （前缀与它之间隔着空白 + 非 base64 字符，回退会在遇到 ``=`` 时终止）。

    Args:
        text: 原始文本。
        start: 命中区间起始偏移（闭）。
        end: 命中区间结束偏移（开）。

    Returns:
        True 表示该命中应为 OpenSSH 公钥 body 被硬排除。
    """
    for prefix in _PUBLIC_KEY_PREFIXES:
        idx = text.rfind(prefix, 0, start)
        if idx == -1:
            continue
        # 前缀须是 token 起点（前一字符不是字母/数字/下划线），避免命中 `myssh-ed25519`
        if idx > 0 and (text[idx - 1].isalnum() or text[idx - 1] == "_"):
            continue
        # 前缀与命中之间只允许空白 + base64 字符（含被字符类排除的 ``/``）
        gap = text[idx + len(prefix) : start]
        if _PUBLIC_KEY_GAP_RE.match(gap):
            return True
    return False


def is_pem_non_secret_span(text: str, start: int, end: int) -> bool:
    """PEM 非秘密块硬排除（缺陷 4 / 缺陷 6 / 缺陷 7）：命中区间是否落在公钥 / 证书块内。

    **fail-closed 判据**：只有命中区间落在某个**已完整识别**的 PEM 非秘密块之内，
    才返回 True（可排除）。
    任何「畸形 / 未闭合 / label 错配 / 含糊」的块一律返回 False → 交给裸熵兜底遮掉。
    **宁遮不漏**：多遮公钥是可接受的误伤，少遮私钥是不可逆泄露。

    判据分两步：

    1. 用 ``iter_pem_blocks``（与 ``PemDetector`` **共享的唯一解析源**）
       解析文本中**完整且 BEGIN/END label 一致**的块（5 短横与 RFC 4716
       4 短横两种拼写）。
       未闭合（无 END）/ label 错配的块**不会**被解析出来 ——
       这正是缺陷 7 的根因修复：旧实现只读 BEGIN label、
       只要求「BEGIN 与命中之间无 END」，于是畸形块被熵侧放过、
       又被 ``PemDetector``（要求 BEGIN == END）拒绝，**两边都不遮**而泄露。
    2. 命中区间须落在某块内（``block_start < start`` 且 ``end <= block_end``），
       且 ``classify_pem_label(block.label) == "non_secret"`` —— 与 ``PemDetector``
       **共用同一分类函数**（缺陷 8），只有精确命中非秘密白名单的 label 才排除。
       private key 家族不在排除集内 —— 其 body 由 ``PemDetector``（priority 95）
       整块高优先级替换，即便 PemDetector 因故未命中，裸熵兜底也会遮它。

    另加位置护栏：命中不得与 BEGIN 落在同一行（body 必须起于新行），
    使 ``CERTIFICATE AUTHORITY=...`` 这类「label 后接空格 + 普通赋值、无换行」
    的普通文本**不会**被误排除。

    Args:
        text: 原始文本。
        start: 命中区间起始偏移（闭）。
        end: 命中区间结束偏移（开）。

    Returns:
        True 表示该命中应作为 PEM 非秘密块 body 被硬排除。
    """
    # ``iter_pem_blocks`` / ``classify_pem_label`` 定义在本文件上方（合并前它们位于``redaction.pem``）——同文件直接调用，
    # 无循环依赖。
    for block in iter_pem_blocks(text, dash=5) + iter_pem_blocks(text, dash=4):
        # 命中须落在块内（块起点在命中之前、块终点不早于命中终点）
        if block.start >= start or block.end < end:
            continue
        label_kind = classify_pem_label(block.label)
        if label_kind == "secret":
            # 空 body 豁免（S115/S116）：无实质内容的「空壳 / 教学省略」块不是真实密钥，
            # 其 body 也不该被裸熵兜底遮掉 —— 与 PemDetector 的豁免判据**同一函数**，
            # 保证两侧不会一边放过、一边遮住（缺陷 7 的同类漂移）。
            if not is_substantive_pem_body(text, block):
                gap = text[block.body_start : start]
                if gap and _PEM_BODY_GAP_RE.match(gap):
                    return True
            continue
        if label_kind != "non_secret":
            # ``unknown``（含所有 malformed / 近义 label）不排除 → 遮。
            continue
        # 命中须起于 BEGIN 之后的新行（body 行），不得与 BEGIN 同行。
        # gap 从 BEGIN 标记之后取起，须以换行开头（PEM body 必须另起一行）。
        gap = text[block.body_start : start]
        if gap and _PEM_BODY_GAP_RE.match(gap):
            return True
    return False


def is_base64_container_span(text: str, start: int, end: int) -> bool:
    """D-08 硬排除：命中区间是否落在 ``data:`` / ``;base64,`` 容器载荷内。

    ``EntropyDetector`` 现自持本判据（T-vq0）；本定义保留以兼容 ``__all__``
    与既有导入面，二者由
    ``test_detectors.py::TestDetectorConstantEncapsulation::test_entropy_self_hosts_moved_helpers``
    与源实现钉住同结果。

    判据是 **span 位置关系**，不是「文本含 data:」——
    命中区间之前最近的容器前缀与命中起点之间若**不含**空格 / 引号 / 标点等分隔符，
    则命中确在 data URI 载荷里，返回 True。

    Args:
        text: 原始文本。
        start: 命中区间起始偏移（闭）。
        end: 命中区间结束偏移（开）。

    Returns:
        True 表示该命中应作为 base64 容器载荷被硬排除。
    """
    for marker in _BASE64_CONTAINERS:
        idx = text.rfind(marker, 0, start)
        if idx == -1:
            continue
        # 命中区间须在该前缀之后
        if idx + len(marker) > start:
            continue
        gap = text[idx + len(marker) : start]
        if not _CONTAINER_GAP_RE.search(gap):
            return True
    return False


# ===== 检测器（原 detectors.py 内容，260915-0dr 合并后紧随三层定义）=====


class Detector(Protocol):
    """检测器协议：廉价预筛选 + 原文 span 扫描。"""

    rule_id: str

    def could_match(self, text: str) -> bool:
        """廉价预筛选：文本是否可能命中（避免昂贵扫描）。

        实现约定：``scan`` 必须在自身开头调用本方法一次，为 False 时直接返回 ``[]``。
        """
        ...

    def scan(self, text: str) -> list[Finding]:
        """在原始文本上扫描，产出命中列表。"""
        ...


@dataclass(frozen=True)
class DetectorRule:
    """单条检测规则（正则 + 类型标签 + 优先级）。

    Args:
        rule_id: 规则标识（如 ``vendor.openai``）。
        kind: 凭据类型标签（用于 typed sentinel）。
        regex: 编译后的正则。
        priority: 优先级（区间重叠合并时保留最高者）。
    """

    rule_id: str
    kind: str
    regex: re.Pattern[str]
    priority: int


class VendorTokenDetector:
    """厂商前缀 Token 检测器（多规则，无单一 rule_id）。

    厂商 Token 前缀检测器（从 ``redact.py`` 内联迁移的 14 条规则）。

    每条规则带 ``kind``（用于 typed sentinel）与厂商前缀字面量（用于廉价预筛选）。
    规则只匹配高置信度的结构化 Token，避免误伤普通业务文本。
    """

    rule_id: str = ""

    # 厂商规则优先级（低于 registered 的 100，高于裸熵兜底）
    _VENDOR_PRIORITY: ClassVar[int] = 80

    _VENDOR_RULES: ClassVar[tuple[DetectorRule, ...]] = (
        DetectorRule(
            rule_id="vendor.openai",
            kind="openai_key",
            regex=re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}"),
            priority=_VENDOR_PRIORITY,
        ),  # OpenAI / 通用 sk-
        DetectorRule(
            rule_id="vendor.github",
            kind="github_token",
            regex=re.compile(r"ghp_[A-Za-z0-9]{36}"),
            priority=_VENDOR_PRIORITY,
        ),  # GitHub PAT (classic)
        DetectorRule(
            rule_id="vendor.github_fine_grained",
            kind="github_token",
            regex=re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
            priority=_VENDOR_PRIORITY,
        ),  # GitHub PAT (fine-grained)
        DetectorRule(
            rule_id="vendor.slack",
            kind="slack_token",
            regex=re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
            priority=_VENDOR_PRIORITY,
        ),  # Slack
        DetectorRule(
            rule_id="vendor.aws", kind="aws_key", regex=re.compile(r"AKIA[0-9A-Z]{16}"), priority=_VENDOR_PRIORITY
        ),  # AWS Access Key
        DetectorRule(
            rule_id="vendor.aws_temp", kind="aws_key", regex=re.compile(r"ASIA[0-9A-Z]{16}"), priority=_VENDOR_PRIORITY
        ),  # AWS Temporary Access Key
        DetectorRule(
            rule_id="vendor.gitlab",
            kind="gitlab_token",
            regex=re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
            priority=_VENDOR_PRIORITY,
        ),  # GitLab PAT
        DetectorRule(
            rule_id="vendor.xai", kind="xai_key", regex=re.compile(r"xai-[A-Za-z0-9]{20,}"), priority=_VENDOR_PRIORITY
        ),  # xAI
        DetectorRule(
            rule_id="vendor.google_api_key",
            kind="google_token",
            regex=re.compile(r"AIza[0-9A-Za-z_-]{35}"),
            priority=_VENDOR_PRIORITY,
        ),  # Google API Key
        DetectorRule(
            rule_id="vendor.google_oauth",
            kind="google_token",
            regex=re.compile(r"ya29\.[0-9A-Za-z_-]+"),
            priority=_VENDOR_PRIORITY,
        ),  # Google OAuth access token
        DetectorRule(
            rule_id="vendor.tencent",
            kind="tencent_secret_id",
            regex=re.compile(r"\bAKID[0-9A-Za-z]{13,}"),
            priority=_VENDOR_PRIORITY,
        ),  # 腾讯云 SecretId
        DetectorRule(
            rule_id="vendor.aliyun",
            kind="aliyun_access_key",
            regex=re.compile(r"\bLTAI[0-9A-Za-z]{17,}"),
            priority=_VENDOR_PRIORITY,
        ),  # 阿里云 AccessKeyId
        DetectorRule(
            rule_id="vendor.jwt",
            kind="jwt",
            regex=re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
            priority=_VENDOR_PRIORITY,
        ),  # JWT
        DetectorRule(
            rule_id="vendor.bearer",
            kind="bearer",
            regex=re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
            priority=_VENDOR_PRIORITY,
        ),  # Bearer Token
    )

    # 各规则的前缀字面量（廉价预筛选）：文本中不含任一前缀则直接跳过
    _PREFIX_HINTS: ClassVar[tuple[str, ...]] = (
        "sk-",
        "ghp_",
        "github_pat_",
        "xox",
        "AKIA",
        "ASIA",
        "glpat-",
        "xai-",
        "AIza",
        "ya29.",
        "AKID",
        "LTAI",
        "eyJ",
        "Bearer",
    )

    def could_match(self, text: str) -> bool:
        if not isinstance(text, str) or not text:
            return False
        return any(hint in text for hint in self._PREFIX_HINTS)

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []
        for rule in self._VENDOR_RULES:
            for match in rule.regex.finditer(text):
                findings.append(
                    Finding(
                        start=match.start(),
                        end=match.end(),
                        rule_id=rule.rule_id,
                        kind=rule.kind,
                        confidence="high",
                        priority=rule.priority,
                    )
                )
        return findings


class UrlDetector:
    """URL query / form-urlencoded 凭据检测器（只替换敏感值，不重新序列化）。

    URL query 与 form-urlencoded 凭据检测器（解 T-06-13）。

    **绝不重新序列化（T-06-13）**：本检测器**禁止**引入任何 URL 解析库 ——
    不拆解再 ``urlencode`` 重组。只用正则定位 span，替换走 ``api`` 的统一 span 机制，
    参数顺序 / 编码 / 非敏感参数**自然保真**。重新序列化会破坏 OAuth callback、
    magic link、预签名 URL（参数顺序与编码有签名语义）。

    两条路径：
    1. **URL query**：``[?&](key)=(value)``，value 终止于 ``&`` / ``#`` / 空白 /
       ``"`` / ``'`` / ``<`` / ``>`` / ``;``。
    2. **form-urlencoded**：先确认整体或局部是 ``k=v(&k=v)+`` 形状（**先判形状再拆**），
       再按 ``&`` 拆 pair —— 不把任意 ``a=b`` 都当 form。

    敏感 key 集（大小写不敏感）：``access_token`` / ``token`` / ``api_key`` / ``apikey`` /
    ``secret`` / ``client_secret`` / ``password`` / ``signature`` / ``code`` /
    ``x-amz-signature`` / ``x-amz-credential``。
    **``code`` 与 ``signature`` 只在此上下文敏感** —— ``status.code`` / ``error.code``
    不能当 OAuth code（ref.md §10.3）；HTTP 状态码文本 ``status code 200`` 不命中。

    ``kind`` = ``url_credential``，``confidence="high"``，``priority=85``。
    """

    rule_id: str = "url"

    # url 结构化证据强度：与 cookie 同级（85），弱于 header/dsn 的完整协议形态
    _URL_PRIORITY: ClassVar[int] = 85

    # URL query 敏感 key（大小写不敏感）
    _SENSITIVE_QUERY_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "access_token",
            "token",
            "api_key",
            "apikey",
            "secret",
            "client_secret",
            "password",
            "signature",
            "code",
            "x-amz-signature",
            "x-amz-credential",
        }
    )

    # query key 字面量（廉价预筛选用）
    _KEY_HINTS: ClassVar[tuple[str, ...]] = tuple(sorted(_SENSITIVE_QUERY_KEYS))

    # URL query：`?` 或 `&` 后的 key=value，value 终止于 `&` / `#` / 空白 / 引号 / `<>` / `;`
    _QUERY_RE: ClassVar[re.Pattern] = re.compile(r"[?&]([A-Za-z0-9_.\-]{1,64})=([^&#\s\"'<>;]{1,4096})")

    # form-urlencoded：整体为 `k=v` 且含至少一个 `&`（先判形状）
    _FORM_SHAPE_RE: ClassVar[re.Pattern] = re.compile(r"^[^\s]*[A-Za-z0-9_.\-]{1,64}=[^&\s]*&[^\s]+$")

    # form pair 拆解
    _FORM_PAIR_RE: ClassVar[re.Pattern] = re.compile(r"([A-Za-z0-9_.\-]{1,64})=([^&#\s]{1,4096})")

    # 变量引用值：`$X` / `${X}` / `{x}` —— 源码模板例外（T-06-17）
    _VAR_REF_RE: ClassVar[re.Pattern] = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$|^\{[A-Za-z_][A-Za-z0-9_]*\}$")

    @staticmethod
    def _is_variable_reference(value: str) -> bool:
        """判断 query 值是否为源码变量引用（配置模板例外，T-06-17）。"""
        return bool(UrlDetector._VAR_REF_RE.match(value))

    def could_match(self, text: str) -> bool:
        """廉价预筛选：含 ``=`` 且（含 ``?`` / ``&`` 或含任一敏感 key 字面量）。"""
        if not isinstance(text, str) or not text:
            return False
        if "=" not in text:
            return False
        if "?" in text or "&" in text:
            return True
        lowered = text.lower()
        return any(key in lowered for key in self._KEY_HINTS)

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []
        findings.extend(self._scan_url_query(text))

        # form-urlencoded：仅在「非 URL」
        # 且整体呈现 form 形状时才按 `&` 拆（URL 含 `?`/`://`，其 `&` 分隔的已经是 query，避免与 _scan_url_query 重复计数）
        if "?" not in text and "://" not in text and self._FORM_SHAPE_RE.match(text):
            findings.extend(self._scan_form(text))

        return findings

    def _scan_url_query(self, text: str) -> list[Finding]:
        """扫描 URL query 形态（``?k=v`` / ``&k=v``）。"""
        findings: list[Finding] = []
        for match in self._QUERY_RE.finditer(text):
            key, value = match.group(1), match.group(2)
            if key.lower() not in self._SENSITIVE_QUERY_KEYS:
                continue
            if not value or self._is_variable_reference(value):
                continue
            findings.append(
                Finding(
                    start=match.start(2),
                    end=match.end(2),
                    rule_id=self.rule_id,
                    kind="url_credential",
                    confidence="high",
                    priority=self._URL_PRIORITY,
                )
            )
        return findings

    def _scan_form(self, text: str) -> list[Finding]:
        """扫描 form-urlencoded 形态（已确认形状，按 ``&`` 拆 pair）。"""
        findings: list[Finding] = []
        for match in self._FORM_PAIR_RE.finditer(text):
            key, value = match.group(1), match.group(2)
            if key.lower() not in self._SENSITIVE_QUERY_KEYS:
                continue
            if not value or self._is_variable_reference(value):
                continue
            findings.append(
                Finding(
                    start=match.start(2),
                    end=match.end(2),
                    rule_id=f"{self.rule_id}.form",
                    kind="url_credential",
                    confidence="high",
                    priority=self._URL_PRIORITY,
                )
            )
        return findings


class JwtDetector:
    """JWT 检测器（三段 JWS 与五段 JWE，均整体替换）。

    JWT 检测器（三段 JWS + 五段 JWE，解 T-06-15）。

    **必须整体替换**（ref.md §10.8 点名的反模式）：只遮前三段等于泄露 ——
    三段 JWS 的签名段、五段 JWE 的密文段与认证标签段都是敏感内容。
    本 detector 显式枚举**三段**与**五段**两套模式，**两套都覆盖完整 token**。

    新增能力（对比 vendor 的 ``eyJ...{8,}.x.x`` 三段正则）：
    - 覆盖**五段 JWE**（vendor 规则完全没有覆盖）；
    - 段用**有界**量词（``{8,4096}``），不使用模糊量词「``\\.`` 出现 2 到 4 次」
      （会让六段以上误命中且语义不清）。

    段字符集 ``[A-Za-z0-9_-]``（base64url），段间**必须恰好**是 ``.``。

    负例：单个 ``eyJ...`` 片段（无后续 ``.seg.seg``）不命中；
    普通文本中出现的 ``eyJ`` 前缀不命中。

    ``kind`` = ``jwt``，``confidence="high"``，``priority=88``
    （**高于** vendor 的 80：本 detector 是结构化的、覆盖五段；
    vendor 的 JWT 规则只覆盖三段）。
    """

    rule_id: str = "jwt"

    # 结构化且覆盖五段 → 高于 vendor 的 JWT 规则（后者只有三段）
    _JWT_PRIORITY: ClassVar[int] = 88

    # 单段 base64url（有界量词，T-06-12）
    _SEGMENT: ClassVar[str] = r"[A-Za-z0-9_-]{8,4096}"

    # 三段 JWS：`eyJ<seg>.<seg>.<seg>`
    _JWS_RE: ClassVar[re.Pattern] = re.compile(r"eyJ" + _SEGMENT + r"\." + _SEGMENT + r"\." + _SEGMENT)

    # 五段 JWE：`eyJ<seg>.<seg>.<seg>.<seg>.<seg>`
    _JWE_RE: ClassVar[re.Pattern] = re.compile(
        r"eyJ" + _SEGMENT + r"\." + _SEGMENT + r"\." + _SEGMENT + r"\." + _SEGMENT + r"\." + _SEGMENT
    )

    def could_match(self, text: str) -> bool:
        """廉价预筛选：文本含 ``eyJ`` 即可能命中。"""
        if not isinstance(text, str) or not text:
            return False
        return "eyJ" in text

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        # 先扫五段（更长、更具体），再扫三段；两者覆盖范围由 merge_spans 处理
        findings: list[Finding] = []
        for match in self._JWE_RE.finditer(text):
            findings.append(
                Finding(
                    start=match.start(),
                    end=match.end(),
                    rule_id=f"{self.rule_id}.jwe",
                    kind="jwt",
                    confidence="high",
                    priority=self._JWT_PRIORITY,
                )
            )
        for match in self._JWS_RE.finditer(text):
            findings.append(
                Finding(
                    start=match.start(),
                    end=match.end(),
                    rule_id=f"{self.rule_id}.jws",
                    kind="jwt",
                    confidence="high",
                    priority=self._JWT_PRIORITY,
                )
            )
        return findings


class HeadersDetector:
    """敏感 header 值检测器（覆盖 Basic 凭据，Bearer 不设最小长度）。

    敏感 header 检测器（解 T-06-10 / 缺陷 D10）。

    **关键差异（对比旧 ``redact.py:45``）**：
    1. **不设最小长度** —— ``Bearer`` 规则不再要求至少 20 个字符（RFC 6750 允许短 token）。
       旧实现只匹配 20 字符以上的 ``Bearer`` 值，短 token 完全漏检。
    2. **Basic 凭据被替换** —— 旧实现对 ``Authorization: Basic <base64>`` 无规则，
       只替换了格式名而**凭据原样泄露**。本 detector 覆盖 value 本体。
    3. **span 只覆盖 value 本体**（若存在 scheme 如 ``Bearer`` / ``Basic`` / ``Digest``，
       span 从 scheme 之后开始），替换后 ``Authorization: Bearer [REDACTED:x]``
       保留 header 名与 scheme —— 既不泄露凭据，也不破坏协议可读性。

    ``kind`` 固定 ``authorization_header``，``priority=90`` —— **高于** vendor 的 80：
    header 结构（``Name: Scheme value``）是比厂商前缀更强的证据。
    """

    rule_id: str = "headers"

    # header 结构证据强于厂商前缀（vendor=80），低于注册值精确匹配（registered=100）
    _HEADERS_PRIORITY: ClassVar[int] = 90

    # 敏感 header 名（大小写不敏感，`|` 连接）
    _HEADER_NAMES: ClassVar[tuple[str, ...]] = (
        "Authorization",
        "Proxy-Authorization",
        "X-API-Key",
        "X-Goog-API-Key",
        "API-Key",
        "X-API-Token",
        "X-Auth-Token",
        "X-Access-Token",
    )

    # 已知 scheme（Bearer / Basic / Digest）—— 出现在 value 前时 span 从其后开始
    _SCHEME_RE: ClassVar[str] = r"(?:[A-Za-z][A-Za-z0-9_-]*\s+)?"

    # header 名 + 分隔符 + 可选 scheme + value 本体注意：从行首 / 分隔符边界起匹配（避免无界前缀回溯，T-06-12）
    _HEADER_RE: ClassVar[re.Pattern] = re.compile(
        r"(?i)(?:^|[\s;,:])\s*(" + "|".join(_HEADER_NAMES) + r")\s*:\s*" + _SCHEME_RE + r"([^\s\"'<>,;)]+)",
    )

    # 已知 scheme 集合：命中时从 span 起点排除（保留 scheme + 一个空格）
    _KNOWN_SCHEMES: ClassVar[tuple[str, ...]] = ("Bearer", "Basic", "Digest")

    # 廉价预筛选：含 `:` 且含任一 header 名片段
    _HINT_NAMES: ClassVar[tuple[str, ...]] = (
        "authorization",
        "api-key",
        "api_key",
        "x-api",
        "x-auth",
        "x-access",
        "x-goog",
    )

    def could_match(self, text: str) -> bool:
        """廉价预筛选：文本含 ``:`` 且含任一敏感 header 名片段。"""
        if not isinstance(text, str) or not text:
            return False
        if ":" not in text:
            return False
        lowered = text.lower()
        return any(hint in lowered for hint in self._HINT_NAMES)

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []
        for match in self._HEADER_RE.finditer(text):
            value = match.group(2)
            if not value:
                continue

            start = match.start(2)
            # 若 value 前是已知 scheme（如 `Bearer `），把 span 起点推到 scheme 之后
            raw = text[match.start() : match.end()]
            scheme_detected = False
            for scheme in self._KNOWN_SCHEMES:
                token = f"{scheme} "
                if token in raw:
                    start = match.end() - len(value)
                    scheme_detected = True
                    break

            # 无 value（如 `Authorization: Basic` 后即行尾）：`Basic` 被当作 value 捕获，
            # 但它其实是 scheme 名而非凭据 —— 不产出 finding
            if not scheme_detected and value.lower() in {s.lower() for s in self._KNOWN_SCHEMES}:
                continue

            findings.append(
                Finding(
                    start=start,
                    end=match.end(2),
                    rule_id=self.rule_id,
                    kind="authorization_header",
                    confidence="high",
                    priority=self._HEADERS_PRIORITY,
                )
            )
        return findings


class DsnDetector:
    """DSN 连接串 password 检测器（只替换密码段，连接串结构保真）。

    DSN 连接串检测器（解 T-06-11 / 缺陷 D9）。

    覆盖 ``scheme://[user[:password]@]host[:port][/db]`` 形态：
    ``postgresql`` / ``postgres`` / ``mysql`` / ``mariadb`` / ``mongodb`` / ``mongodb+srv`` /
    ``redis`` / ``rediss`` / ``amqp`` / ``amqps``。``redis://:pw@host/0`` 这种**无 user**
    的形态（userinfo 直接以冒号开头）也覆盖。

    finding span **只覆盖 password 段** —— scheme / user / host / port / db 全部保真，
    故替换后连接串仍可用（预签名 / 运维命令场景不被破坏）。

    **源码模板例外（T-06-17）**：password 为 ``${VAR}`` / ``$VAR`` / ``{var}`` 形状时不命中，
    避免把配置模板当成真实 secret 破坏。

    **JDBC 属性串不在本 detector**：``jdbc:sqlserver://h;Password=x`` 这类形态
    既无 ``user:pass@`` userinfo 也无受支持 scheme，由 :class:`JdbcDetector` 单独覆盖
    （须``jdbc:`` 前缀作为上下文，T-06-17 的同类要求）。

    **ReDoS 防护（T-06-12）**：正则从**强制** ``://`` 边界起匹配，
    password 段用**有界**量词（``[^@\\s/]{1,512}``），
    **禁止**惰性通配（lazy wildcard）与无界回溯 ——
    ref.md §4.6 记录过 320KB 连续字母数字输入下 55 秒的真实事故。

    ``kind`` = ``dsn_password``，``confidence="high"``，``priority=90``。
    """

    rule_id: str = "dsn"

    # DSN 结构证据强于厂商前缀（vendor=80）
    _DSN_PRIORITY: ClassVar[int] = 90

    # 支持的 scheme（强制出现在 `://` 之前）
    _SCHEMES: ClassVar[tuple[str, ...]] = (
        "postgresql",
        "postgres",
        "mysql",
        "mariadb",
        "mongodb+srv",
        "mongodb",
        "rediss",
        "redis",
        "amqps",
        "amqp",
    )

    # userinfo 形态：`user:password@` 或 `:password@`（无 user）。password 用有界量词（T-06-12）。
    # 注意：scheme 用 `re.escape` 保证 `mongodb+srv` 的 `+` 是字面量而非量词。
    _DSN_RE: ClassVar[re.Pattern] = re.compile(
        r"(?i)(?:^|[^\w])(" + "|".join(re.escape(s) for s in _SCHEMES) + r")://([^:@/\s]{0,128}):([^@\s/]{1,512})@",
    )

    # 变量引用形状：`${VAR}` / `$VAR` / `{var}`（源码模板例外）
    _VAR_REF_RE: ClassVar[re.Pattern] = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$|^\{[A-Za-z_][A-Za-z0-9_]*\}$")

    # 廉价预筛选：含 `://` 且含某个 scheme 字面量
    _HINT_RE: ClassVar[re.Pattern] = re.compile(r"://", re.IGNORECASE)

    # 已删除 `_JDBC_PASSWORD_RE` / `_JDBC_HINT_RE`（原 `(?i)\bPassword\s*=\s*([^\s;\"']{1,512})`）：
    # 该正则**不含任何 JDBC 上下文**（无 `jdbc:`、无分号属性串要求），
    # 实际等价于「`Password=` 后跟任意非空白」—— 与普通配置赋值完全同形，
    # 因而把 `password=null` / `********` / `<REDACTED>` / `!vault` 等占位符
    # 一并当密码脱敏（S106/S107/S109/S111）。且其 priority=90 高于 assignment 的 70，
    # 会**抢答**并把 kind 标成 `dsn_password`，让「DSN 检测在工作」成为假象。
    #
    # 删除依据（2026-09-15 实测）：全集 128 条中，本正则命中的 4 条正例
    # （S090/S092/S095/S128）**全部**是裸 `password=`，已被 ``AssignmentDetector``
    # 的 strict 路径覆盖 —— 删除后正例零损失；负例中 4 条输出变化（kind 回归
    # `credential`、S106 恢复原样）。真 JDBC 连接串（`jdbc:mysql://...`）仍由
    # 上方 ``_DSN_RE`` 按 scheme 覆盖。

    @staticmethod
    def _is_variable_reference(value: str) -> bool:
        """判断 DSN password 是否为源码变量引用（配置模板例外，T-06-17）。"""
        return bool(DsnDetector._VAR_REF_RE.match(value))

    def could_match(self, text: str) -> bool:
        """廉价预筛选：含 ``://`` 且含某个 scheme 字面量。

        `password=` 不再参与预筛选 —— 它属于 ``AssignmentDetector`` 的职责，
        本 detector 只认带 scheme 的 DSN 形态。
        """
        if not isinstance(text, str) or not text:
            return False
        lowered = text.lower()
        return bool(self._HINT_RE.search(text) and any(scheme in lowered for scheme in self._SCHEMES))

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []

        for match in self._DSN_RE.finditer(text):
            password = match.group(3)
            if not password or self._is_variable_reference(password) or is_placeholder_value(password):
                continue
            findings.append(
                Finding(
                    start=match.start(3),
                    end=match.end(3),
                    rule_id=self.rule_id,
                    kind="dsn_password",
                    confidence="high",
                    priority=self._DSN_PRIORITY,
                )
            )

        return findings


class JdbcDetector:
    """JDBC 属性串 password 检测器（**须** ``jdbc:`` 上下文）。

    覆盖 ``jdbc:<subprotocol>://<host>[...];Password=<value>`` 与
    ``jdbc:<subprotocol>:<dsn>;Password=<value>`` 形态的属性段。
    例：``jdbc:sqlserver://h;User Id=sa;Password=S3cr3tPw``。

    == 与已删除的 ``DsnDetector._JDBC_PASSWORD_RE`` 的关键区别 ==

    旧实现是 ``(?i)\\bPassword\\s*=\\s*([^\\s;\"']{1,512})`` —— **不含任何 JDBC 上下文**，
    实际等价于「``Password=`` 后跟任意非空白」，与普通配置赋值完全同形，
    因而把 ``password=null`` / ``********`` / ``<REDACTED>`` / ``!vault``
    一并当密码脱敏（S106/S107/S109/S111），且 priority=90 会**抢答**
    assignment 的 70 并把 kind 标成 ``dsn_password``，让「DSN 检测在工作」成为假象。

    本 detector 要求命中区间位于**同一个 JDBC 串**之内：必须能在命中之前找到
    ``jdbc:`` 标记，且（``jdbc:``, 命中）之间**不含空白**
    —— JDBC URL 是单个无空白 token，属性段之间以 ``;`` 连接。
    这样 ``password=null``（无 ``jdbc:``）不再命中，而真实 JDBC 串仍被覆盖。

    **源码模板 / 占位符例外（T-06-17 同类）**：值为 ``${VAR}`` / ``$VAR``
    或占位符（``null`` / ``<REDACTED>`` 等）时不命中。

    **ReDoS 防护（T-06-12）**：``jdbc:`` 为定长字面量前缀，
    password 段用**有界**量词（``{1,512}``），无惰性通配、无嵌套量词。

    ``kind`` = ``dsn_password``，``confidence="high"``，``priority=90``。
    """

    rule_id: str = "dsn.jdbc"

    # JDBC 属性串与 DSN 同级（90）：``jdbc:`` 是协议级结构证据
    _JDBC_PRIORITY: ClassVar[int] = 90

    # ``jdbc:`` 标记（唯一上下文来源）
    _JDBC_MARKER: ClassVar[str] = "jdbc:"

    # 属性段：`Password=<value>`，值为单 token（终止于 `;` / 空白 / 引号）
    _PASSWORD_ATTR_RE: ClassVar[re.Pattern] = re.compile(r"(?i)\bPassword\s*=\s*([^\s;\"']{1,512})")

    # 廉价预筛选：含 `jdbc:` 且含 `password=`
    _HINT_RE: ClassVar[re.Pattern] = re.compile(r"(?i)jdbc:")

    def could_match(self, text: str) -> bool:
        """廉价预筛选：文本含 ``jdbc:``（唯一上下文来源）。"""
        if not isinstance(text, str) or not text:
            return False
        return bool(self._HINT_RE.search(text))

    def _in_jdbc_context(self, text: str, start: int) -> bool:
        """判断位置 ``start`` 是否落在某个 JDBC 串内。

        判据：从命中回溯到最近的 ``jdbc:`` 标记，其间**不得出现行边界**
        （``\\n`` / ``\\r``）—— JDBC URL 是单行内容。

        不能用「无空白」作判据：真实 JDBC 属性串含**带空格的属性名**
        （``jdbc:sqlserver://h;User Id=sa;Password=x`` 里的 ``User Id``），
        要求无空白会把这种合法形态误拒（实测 S 系列回归）。
        行边界是更稳的判据：``password=null`` 这类普通配置行根本不含 ``jdbc:``，
        而含 ``jdbc:`` 的行内属性都属于同一连接串。

        Args:
            text: 原始文本。
            start: 命中起始偏移（闭）。

        Returns:
            True 表示命中在该 JDBC 串内。
        """
        idx = text.rfind(self._JDBC_MARKER, 0, start)
        if idx == -1:
            return False
        if idx > 0 and (text[idx - 1].isalnum() or text[idx - 1] == "_"):
            return False  # 避免匹配 `myjdbc:`
        return "\n" not in text[idx:start] and "\r" not in text[idx:start]

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：与其余 detector 同一约定。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []
        for match in self._PASSWORD_ATTR_RE.finditer(text):
            password = match.group(1)
            if not password or is_placeholder_value(password):
                continue
            if not self._in_jdbc_context(text, match.start(1)):
                continue
            findings.append(
                Finding(
                    start=match.start(1),
                    end=match.end(1),
                    rule_id=self.rule_id,
                    kind="dsn_password",
                    confidence="high",
                    priority=self._JDBC_PRIORITY,
                )
            )
        return findings


class PemDetector:
    """PEM / PGP private key block 检测器（整块替换，只认 private 类 label）。

    PEM / PGP private key block 检测器（解 T-06-11 / 缺陷 D9）。

    **整块替换**：finding span 覆盖 ``-----BEGIN ...-----`` 到 ``-----END ...-----``
    的完整 block（含 BEGIN/END 行）—— 只遮 key body 等于泄露 header 信息，
    只遮 header 等于泄露 key body。

    **LABEL 白名单**（必须命中）：``PRIVATE KEY`` / ``RSA PRIVATE KEY`` /
    ``EC PRIVATE KEY`` / ``OPENSSH PRIVATE KEY`` / ``ENCRYPTED PRIVATE KEY`` /
    ``PGP PRIVATE KEY BLOCK``。

    **负例显式排除**：``PUBLIC KEY`` / ``CERTIFICATE`` / ``PGP PUBLIC KEY BLOCK`` /
    ``SSH2 PUBLIC KEY`` —— 公钥与证书**不是秘密**，替换它们会破坏正常配置与信任链。
    实现方式是 **LABEL 白名单匹配**（而非「先匹配任意 BEGIN/END 再排除」），
    保证负例从一开始就不进入匹配。

    **BEGIN/END label 一致性**：用分组捕获 + **代码比对**实现，
    **不**使用正则 backreference（ref.md §14.2 明确禁止 backreference 进入正则）。
    label 不一致（``BEGIN RSA PRIVATE KEY`` + ``END EC PRIVATE KEY``）不命中。

    **解析 / 分类定义在本模块上方**（原 ``redaction.pem``）：``iter_pem_blocks`` /
    ``classify_pem_label`` / ``normalize_label`` 的定义已随 ``260915-0dr``
    并入本文件（原为 detector 与熵侧共同依赖的干净叶子，仅 stdlib）。
    历史上 ``entropy -> detectors.pem -> detectors/__init__ ->
    detectors.entropy -> entropy`` 的包级循环依赖已于 ``260914-urf`` 消除；
    ``260915-0dr`` 进一步把两模块并入本文件，循环依赖在结构上不再可能存在。

    ``kind`` = ``private_key``，``confidence="high"``，``priority=95``
    （最高 —— private key 是不可逆泄露）。
    """

    rule_id: str = "pem"

    # private key 是不可逆泄露 → 最高优先级（高于 registered=100？否：registered 是精确值 100，
    # 此处 95 用于与其它结构 detector 区分：pem(95) > headers/dsn(90) > jwt(88) > cookie/url(85) > assignment(70)）
    _PEM_PRIORITY: ClassVar[int] = 95

    def could_match(self, text: str) -> bool:
        """廉价预筛选：文本含 ``-----BEGIN `` 即可能命中。"""
        if not isinstance(text, str) or not text:
            return False
        return "-----BEGIN " in text

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []
        # 共享解析源（缺陷 7）：只认 5 短横 + 完整且 label 一致的块。 label 已在 iter_pem_blocks 内归一化，
        # 此处与熵侧共用 classify_pem_label 判定（缺陷 8）：只有 "secret" 才产 finding。
        # ``unknown`` 不产 finding —— 但**同样不会**被熵侧排除（熵侧只放过``"non_secret"``），故 unknown 块 body 仍由裸熵兜底遮掉（fail-closed）。
        for block in iter_pem_blocks(text, dash=5):
            if classify_pem_label(block.label) != "secret":
                # non_secret（PUBLIC KEY / CERTIFICATE / CSR）与 unknown 均不在此产出；
                # 前者由熵侧排除保留，后者由熵侧兜底遮掉。
                continue
            # 空 body 豁免：只有 BEGIN/END 而无实质内容的「空壳」或教学省略
            # （``BEGIN X----- ... -----END X-----``）不是真实密钥，不该脱敏（S115/S116）。
            # **有**实质 body 的截断私钥仍照常整块脱敏（README 明确要求）。
            if not is_substantive_pem_body(text, block):
                continue
            findings.append(
                Finding(
                    start=block.start,
                    end=block.end,
                    rule_id=self.rule_id,
                    kind="private_key",
                    confidence="high",
                    priority=self._PEM_PRIORITY,
                )
            )

        return findings


class AssignmentDetector:
    """``key=value`` 赋值检测器（严格字段 + 歧义字段评分）。

    两条路径共用同一份原始文本的一次 ``finditer``；span 一律只覆盖值本体。

    ``key=value`` 赋值检测器：严格凭据字段 + 歧义字段上下文评分。

    **路径 A —— 严格字段**（`_STRICT_FIELD_NAMES`，
    迁移自旧 ``redact.py`` 的 ``_HIGH_SIGNAL_TEXT_KEYS``）：命中即脱敏，不走评分。
    命中 span **只覆盖值本体**（不含 key 与分隔符、不含引号），
    替换后 ``password=`` 前缀保真，
    也让「同一值被 vendor detector 也命中」时的 span 合并自然正确。

    **路径 B —— 歧义字段**（字段名归一化后以 ``key`` / ``token`` / ``secret`` 结尾）：
    走**上下文评分**（DESIGN §3.5），``score >= 4`` 触发。

    == 与 §3.5 表格的两处有意偏离（均以本 PR 的 must_haves 为准）==

    1. **严格字段豁免变量引用**：§3.5 的 ``-3`` 分只作用于歧义路径（评分表行），
       严格字段本无评分流程。但本 PR 的 must_haves 明确要求
       ``password=$DB_PASSWORD`` / ``${DB_PASSWORD}`` / ``os.getenv(...)``
       **不被替换**（T-06-17：源码配置模板不可被当作真实 secret 破坏）。
       故在严格路径命中后追加一次变量引用短路 —— 这是 must_haves 对 §3.5 的细化。
    2. **优先使用形状判据而非熵**：
       §3.5 正文明确「排除项的判据是纯 hex + 特定长度这一**形状**，不是熵」。
       实测 16 位 hex 在本仓库样本上熵可达 4.000 > 无门限 4.2 判据时的残余，
       故纯 hex 定长形状一律跳过评分/严格判定。

    评分（DESIGN §3.5，分值逐条照抄）：
    +2 长度>=24 / +2 熵达阈值 / +1 字符类别>=3 / +2 字段名以 key|token|secret 结尾；
    -4 UUID-ULID-KSUID-常见 digest-trace ID 形状 / -4 纯 hex 且长度 8/16/32/40/64 /
    -3 变量引用（``$TOKEN`` / ``${TOKEN}`` / ``os.getenv(...)`` /
    ``process.env.X`` / f-string ``{x}``）。

    **性能（缺陷 3 / 缺陷 5）**：``_KEY_VALUE_RE`` 的 key 组锚定 ``(?<![A-Za-z_])``，
    且本 detector 的 ``scan`` 入口会先跑自家 ``could_match`` 预筛选
    （``packages.security.redaction.operations`` 不再重复调用）—— 两者共同保证在无 ``=`` / ``:`` 的大段文本
    （工具结果 / 日志）上不产生 O(n²) 重试。
    锚定**不用 ``\\b``**：``\\b`` 会丢弃数字前缀键（``1password=``，缺陷 5 回归）；
    左视负断言对 O(n²) 的抑制与 ``\\b`` 等价，却允许数字前缀。
    """

    rule_id: str = ""

    # 赋值检测优先级（低于 vendor 的 80、registered 的 100）：
    # vendor 是厂商格式强证据，assignment 是上下文推断
    _ASSIGNMENT_PRIORITY: ClassVar[int] = 70

    # 歧义字段触发阈值（DESIGN §3.5 / CONTEXT D-04）
    _SCORE_THRESHOLD: ClassVar[int] = 4

    # 变量引用上下文窗口：仅当值本身完全落在 window 内（即 window 未在值中途截断）
    # 时才把 window 残余部分拼回值做形状匹配 —— 避免超长真实值 + 64 字符外的无关文本被拼成一个假变量引用（缺陷 2 的根因之一）。
    _VAR_REF_CONTEXT: ClassVar[int] = 64

    # 路径 A —— 严格凭据字段名（迁移自 redact.py:75-87 的 _HIGH_SIGNAL_TEXT_KEYS）
    _STRICT_FIELD_NAMES: ClassVar[tuple[str, ...]] = (
        "password",
        "passwd",
        "apikey",
        "api_key",
        "access_key",
        "access_token",
        "secret",
        "secret_key",
        "app_secret",
        "authorization",
        "private_key",
    )

    # 路径 B —— 歧义字段后缀：归一化后以这些词结尾时参与评分（而非直接命中）
    _AMBIGUOUS_SUFFIXES: ClassVar[tuple[str, ...]] = ("key", "token", "secret")

    # 负例排除集（必须先于评分判定，命中即跳过该匹配）：
    # 含 key/token/secret 语义但属业务标识，非凭据 —— T-06-14 三层防线之一
    _EXCLUDED_FIELD_NAMES: ClassVar[frozenset[str]] = frozenset(
        re.sub(r"[^a-z0-9]", "", name)
        for name in (
            "token_count",
            "token_type",
            "tokenizer",
            "max_tokens",
            "secret_name",
            "secret_id",
            "secret_arn",
            "key_id",
            "public_key",
            "has_password",
            "authorization_url",
            # nonce / salt / IV 族：密码学意义上是**公开**参数（nonce 本就是
            # "number used once"，salt 与 IV 随密文一同存储），不是凭据。
            # 实测其值与真实 token 在熵/形状上完全同形（S120 nonce 熵 4.88、
            # S121 salt 熵 4.80），故检测器无法靠取值区分，只能靠字段名保留。
            # 用户 2026-09-15 明确批准加入此保留名单。
            "nonce",
            "salt",
            "iv",
            "initialization_vector",
            "initialisation_vector",
        )
    )

    # 严格字段名的归一化形式（去分隔符 + 小写）
    _NORMALIZED_STRICT: ClassVar[frozenset[str]] = frozenset(
        re.sub(r"[^a-z0-9]", "", name.lower()) for name in _STRICT_FIELD_NAMES
    )

    # key=value / key: value / "key": "value" 赋值形态；
    # value 终止于引号 / 空白 / 逗号 /分号 / 大括号 / ``&`` / ``#``。
    # - key 组**锚定** ``(?<![A-Za-z_])``（左视负断言）：无锚定时 finditer 会在每个字符位置重启尝试，
    #   而 key 组 ``[A-Za-z0-9_-]*`` 是贪心量词、
    #   可从段中间起跑并吞掉余下整段再回溯失败 —— 每 O(n) 个起点 × O(n) 回溯 = O(n²) ReDoS（缺陷 3）。锚定后：
    #   非边界位置的尝试在进入贪心类之前即 O(1) 失败，起点只能落在「前一个字符不是字母 / 下划线」处，重试次数退化为 O(词数)。
    #   **不能用 ``\b``**（缺陷 5 回归）：``\b`` 在「数字后接字母」
    #   处无边界（``1`` 与 ``p`` 同为 ``\w``），会静默丢弃 ``1password=`` / ``9api_key=`` /``2secret=`` 这类数字前缀键。
    #   左视负断言只禁止**字母 / 下划线**前缀（那样键才是更长标识符的一部分，如 ``abpassword``），**允许数字前缀**—— 数字不是合法标识符首字符，
    #   故 ``1`` 之后确实是键的起点。两者对 O(n²) 的抑制完全等价（实测 4 次倍增均线性），故替换无损线性。
    # - value 组排除 ``&`` / ``#``：query string 中它们才是真正的 pair 分隔符，
    #   否则 ``?access_token=x&next=/a`` 的值会吞掉 ``next=/a``（缺陷 1）
    #   —— assignment 的 span 与 url detector 命中重叠后按 priority(85>70) 取 url 元数据，
    #   整个 union 被当作一个 url_credential 替换，既破坏无关参数保真，
    #   又在 LOG/EXPORT 的 head6/tail4 掩码下泄露 ≥32 字符 secret 的首尾。
    # - value 组仍保留 ``{`` 终止符：
    #   ``${VAR}`` / ``os.getenv(...)`` 依赖它把值截断成``$`` / ``os.getenv(``，再由 _VAR_REF_RE 的后续上下文识别为变量引用（T-06-17）。
    _KEY_VALUE_RE: ClassVar[re.Pattern] = re.compile(
        r"""(?i)(?<![A-Za-z_])([A-Za-z_][A-Za-z0-9_-]*)\s*["']?\s*[:=]\s*["']?([^"'\s,;{}&#]+)["']?""",
    )

    # 纯 hex 排除长度形状由本模块提供（原 ``redaction.entropy``，PR3 收口，
    # ``260915-0dr`` 并入）——``_HEX_EXCLUDE_LENGTHS`` / ``_HEX_RE`` 见文件上方

    # UUID 标准形（带连字符）与无连字符 32 位 hex 形态由本模块提供（原 ``redaction.entropy``，``260915-0dr`` 并入）
    # —— 见 ``is_uuid_like``

    # 变量引用形状：$X / ${X} / os.getenv(...) / process.env.X / f-string {x}注意：
    # `${VAR}` / `os.getenv('X')` 这两类形态会被 _KEY_VALUE_RE 的值边界截断（`{` 是值终止符 → `$` / `os.getenv(`；引号也是值终止符 → `os.getenv(`），故变量引用检查必须能作用于「截断后的值 + 其后的原始文本」，
    # 见 _split_var_ref。
    #
    # **全部 alternative 必须锚定**（缺陷 2）：`os\.getenv\s*\(` / `process\.env\.` 原先未锚定，
    # 导致「值之后 64 字符内出现任意 process.env.」都会把前面的**真实 secret**误判为变量引用而跳过（静默漏报）。锚定到两串拼接起点后，
    # 只有当变量引用形状出现在值**起始处**（真实 `${X}` / `os.getenv(` 值）时才成立。
    # `os\.getenv\s*\(` 与 `process\.env\.` 均为定长前缀字面量 —— 无回溯、无 ReDoS。
    _VAR_REF_RE: ClassVar[re.Pattern] = re.compile(
        r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
        r"|^os\.getenv\s*\("
        r"|^process\.env\."
        r"|^\{[A-Za-z_][A-Za-z0-9_.]*\}"
    )

    # 廉价预筛选：文本中须出现赋值分隔符（`=` / `:` 是 assignment 的必要子串）
    _HINT_RE: ClassVar[re.Pattern] = re.compile(r"[:=]")

    # header scheme 名：作为「authorization」字段的值时说明这是 header 结构，
    # 应由 headers detector 处理而非 assignment（否则会把 header 名替换掉）
    _HEADER_SCHEMES: ClassVar[frozenset[str]] = frozenset({"basic", "bearer", "digest", "negotiate"})

    @staticmethod
    def _split_var_ref(value: str, context: str = "") -> str:
        """把「值 + 其后片段」拼成变量引用形状匹配所需的文本。

        两个约束（缺陷 2 回归防线）：

        1. **仅当值本身完整落在上下文窗口内**（``len(value) <= len(context)``）才拼接。
           否则说明值长于 64 字符窗口，窗口是「值的中途截断」，
           其残余文本与真实 secret 拼接会伪造变量引用形状 ——
           此时只对值本体匹配（真实 secret 不可能是 ``${X}`` / ``os.getenv(`` 形状，
           故不受影响）。
        2. 拼接口恒为值起点，配合 :data:`_VAR_REF_RE` 的 ``^`` 锚定，
           保证变量引用形态必须出现在**值起始处**，
           而不是「值之后某处的无关 token」。

        Args:
            value: 值本体（可能已被 `_KEY_VALUE_RE` 的边界截断）。
            context: ``value`` 在原文本中的后续片段（通常取 value 之后若干字符）。

        Returns:
            供 :data:`_VAR_REF_RE` 匹配的文本。
        """
        if context and len(value) <= len(context):
            return value + context
        return value

    @staticmethod
    def _is_variable_reference(value: str, context: str = "") -> bool:
        """判断值是否为源码变量引用（``$X`` / ``${X}`` / ``os.getenv(...)`` / ``process.env.X``）。

        Args:
            value: 值本体（可能已被 `_KEY_VALUE_RE` 的边界截断）。
            context: ``value`` 在原文本中的后续片段（通常取 value 之后若干字符）。
                ``${DB_PASSWORD}`` 会被截断成 ``$``、
                ``os.getenv('X')`` 会被截断成 ``os.getenv(``，
                故必须在「值 + 后续片段」拼接文本上做形状匹配。
        """
        return bool(AssignmentDetector._VAR_REF_RE.match(AssignmentDetector._split_var_ref(value, context)))

    @staticmethod
    def _score_ambiguous(key: str, value: str, context: str = "") -> int:
        """歧义字段的上下文评分（DESIGN §3.5，分值逐条照抄）。"""
        score = 0
        if len(value) >= 24:
            score += 2
        if shannon_entropy(value) >= 4.2:
            score += 2
        if char_class_count(value) >= 3:
            score += 1
        if _normalize_field_name(key).endswith(AssignmentDetector._AMBIGUOUS_SUFFIXES):
            score += 2

        # 减分项：形状判据（不依赖熵）
        if is_uuid_like(value) or is_fixed_hex(value):
            score -= 4
        if AssignmentDetector._is_variable_reference(value, context):
            score -= 3
        return score

    def could_match(self, text: str) -> bool:
        """廉价预筛选：文本含赋值分隔符 ``=`` 或 ``:`` 即可能命中。"""
        if not isinstance(text, str) or not text:
            return False
        return bool(self._HINT_RE.search(text))

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []
        for match in self._KEY_VALUE_RE.finditer(text):
            key, value = match.group(1), match.group(2)
            if not value:
                continue

            normalized = _normalize_field_name(key)
            # 值之后 64 字符作为变量引用判定上下文（应对 `${X}` / `os.getenv(` 被值边界截断的情形）；
            # 是否拼接由 _split_var_ref 按值长度裁决（缺陷 2）
            context = text[match.end(2) : match.end(2) + self._VAR_REF_CONTEXT]

            # 负例排除集先于评分短路（T-06-14）
            if normalized in self._EXCLUDED_FIELD_NAMES:
                continue

            # 占位符短路：值本身是「未填 / 已遮蔽」标记时，无论 key 多像凭据都不替换。
            # 放在 strict 判定**之前**，因为 strict 是「命中即脱敏」，不经过评分，
            # 故减分项（-3 变量引用）对路径 A 无效 —— 占位符必须在此显式豁免。
            # 单条覆盖：`password=null` / `password=<REDACTED>` / `password=!vault`
            # / `password={{ vault_db_password }}`（README 要求保留）。
            if is_placeholder_value(value):
                continue

            if normalized in self._NORMALIZED_STRICT:
                # 路径 A：严格字段，但变量引用与定长 hex 形状豁免（must_haves #8 / #4）
                if self._is_variable_reference(value, context) or is_fixed_hex(value):
                    continue
                # header 结构豁免：`Authorization: Basic ...` 的值是 scheme，由 headers detector 负责真正的凭据；
                # assignment 不应抢答（否则会把 header 名 `Authorization` 本身替换掉）
                if normalized in ("authorization", "proxyauthorization") and value.lower() in self._HEADER_SCHEMES:
                    continue
                kind = "credential"
            elif (
                normalized.endswith(self._AMBIGUOUS_SUFFIXES)
                and self._score_ambiguous(key, value, context) >= self._SCORE_THRESHOLD
            ):
                # 路径 B：歧义字段，评分达标才触发；kind 取归一化字段名（如 gpg_key）
                kind = normalized or "credential"
            else:
                continue

            findings.append(
                Finding(
                    start=match.start(2),
                    end=match.end(2),
                    rule_id=f"assignment.{normalized}",
                    kind=kind,
                    confidence="medium",
                    priority=self._ASSIGNMENT_PRIORITY,
                )
            )
        return findings


class CookieDetector:
    """Cookie / Set-Cookie 敏感名检测器（只替换敏感 cookie 的值）。

    Cookie / Set-Cookie 检测器（解 T-06-11 / 缺陷 D9）。

    匹配 ``Cookie:`` / ``Set-Cookie:`` 头（大小写不敏感），按 ``;`` 切 pair，
    **只对 cookie 名归一化后落在敏感集内的 pair** 替换其值；
    属性 pair（``Secure`` / ``HttpOnly`` / ``SameSite`` / ``Path`` / ``Domain`` /
    ``Max-Age`` / ``Expires``）一律不替换且保真。

    **关键：按 ``;`` 切分而非按 ``,``** ——
    ``Set-Cookie`` 的 ``Expires=Wed, 21 Oct 2026 07:28:00 GMT`` 里含**逗号**，
    若按 ``,`` 切会把它拆成两个半截 pair，破坏解析。
    故 ``Expires`` 必须作为完整的一个属性 pair 处理。

    finding span 只覆盖 cookie 值本体（不含名、不含 ``=``），
    替换后 ``session=[REDACTED:cookie]`` 保留 cookie 名。

    ``kind`` = ``cookie``，``confidence="high"``，``priority=85``。
    """

    rule_id: str = "cookie"

    # cookie 证据强度与 url 同级（85）：结构化但弱于 header/dsn 的完整协议形态
    _COOKIE_PRIORITY: ClassVar[int] = 85

    # 敏感 cookie 名（归一化后匹配）
    _SENSITIVE_NAMES: ClassVar[frozenset[str]] = frozenset(
        _normalize_field_name(name)
        for name in (
            "session",
            "sessionid",
            "session_id",
            "sid",
            "auth",
            "auth_token",
            "authtoken",
            "token",
            "access_token",
            "jwt",
            "csrf",
            "csrftoken",
            "xsrf",
            "xsrf_token",
        )
    )

    # 匹配 `Cookie:` / `Set-Cookie:` 到行尾（按 `;` 切分在代码中进行，
    # 避免 `Expires` 的逗号破坏解析）
    _COOKIE_HEADER_RE: ClassVar[re.Pattern] = re.compile(
        r"(?i)(?:^|[\s;])\s*Set-Cookie\s*:\s*([^\r\n]+)|(?:^|[\s;])\s*Cookie\s*:\s*([^\r\n]+)"
    )

    # 单个 pair：`name=value`
    _PAIR_RE: ClassVar[re.Pattern] = re.compile(r"^\s*([^=;\s]+)\s*=\s*([^;\s]*)\s*$")

    def could_match(self, text: str) -> bool:
        """廉价预筛选：文本含 ``cookie``（大小写不敏感）。"""
        if not isinstance(text, str) or not text:
            return False
        return "cookie" in text.lower()

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []
        for header in self._COOKIE_HEADER_RE.finditer(text):
            payload = header.group(1) if header.group(1) is not None else header.group(2)
            if not payload:
                continue
            base = header.start(1) if header.group(1) is not None else header.start(2)
            findings.extend(self._scan_pairs(payload, base))

        return findings

    def _scan_pairs(self, payload: str, base: int) -> list[Finding]:
        """按 ``;`` 切 pair，只对敏感 cookie 名的值产出 finding。

        Args:
            payload: header 值部分（``Cookie:`` 之后的整段）。
            base: ``payload`` 在原文本中的起始偏移。
        """
        findings: list[Finding] = []
        # 记录每个 pair 在 payload 内的偏移，用于精确 span
        offset = 0
        for segment in payload.split(";"):
            segment_start = offset
            offset += len(segment) + 1  # +1 为分号

            pair = self._PAIR_RE.match(segment)
            if pair is None:
                continue
            name, value = pair.group(1), pair.group(2)
            if not value:
                continue
            if _normalize_field_name(name) not in self._SENSITIVE_NAMES:
                continue

            value_start = base + segment_start + pair.start(2)
            findings.append(
                Finding(
                    start=value_start,
                    end=value_start + len(value),
                    rule_id=self.rule_id,
                    kind="cookie",
                    confidence="high",
                    priority=self._COOKIE_PRIORITY,
                )
            )
        return findings


@dataclass(frozen=True)
class RegisteredValue:
    """一个显式注入的已知敏感值（**带 kind 元数据**，供 ``registered`` detector 定 rule_id）。

    此前名为 ``KnownValue``，与 :func:`redaction.operations.redact_known_values` 的
    ``Sequence[str]`` 入参**同名不同物**：本类携带 ``kind`` / ``secret_id`` 并参与
    detector span 合并，而后者是「裸值 → 单一占位符」的精确替换。改名以区分二者。

    Args:
        value: **仅驻留内存**，``repr()`` 不含该字段（``field(repr=False)`` —— T-06-02）。
        kind: 凭据类型标签，决定 finding 的 ``rule_id``（``registered:{kind}``）。
        secret_id: 非敏感内部 ID（可选，保留信息以备调用方按 id 去重）。
    """

    value: str = field(repr=False)
    kind: str = "known"
    secret_id: str = ""


@dataclass(frozen=True)
class RegisteredSecretDetector:
    """已知敏感值精确值匹配（priority=100，高于一切模式规则）。

    构造入口是 :meth:`from_values` —— 值**显式注入**，无模块级全局态。

    已知敏感值的精确匹配检测器（``priority=100``，高于一切模式规则）。

    按值的首字符分组做短路预筛选：文本中不含任何 group 首字符时直接跳过。
    组内按值长度降序，长值优先命中，避免短值先命中把长值切碎。

    **显式注入（T-urf-02）**：本 detector **不再读取任何模块级全局注册表** ——
    已知值由调用方经 :meth:`RegisteredSecretDetector.from_values` **每次构造显式传入**。
    这消除了旧的 ``redaction.registry`` 模块级可变状态（跨请求串扰面）
    并绕开平台配置路径的问题：配置 / backend 的解析留在调用方（core），
    redaction 只接收纯值列表。

    ``RegisteredValue.value`` 使用 ``field(repr=False)`` ——
    **值本身即 secret，绝不进入 ``repr()`` / 日志**
    （T-06-02 回归护栏，迁移自 TestRegisteredSecretRepr）。
    """

    # 已知值精确匹配的优先级（高于一切模式规则）
    _REGISTERED_PRIORITY: ClassVar[int] = 100

    _grouped: dict[str, tuple[RegisteredValue, ...]] = field(repr=False, default_factory=dict)
    rule_id: str = "registered"

    @classmethod
    def from_values(
        cls,
        values: Sequence[RegisteredValue | str],
        *,
        default_kind: str = "known",
    ) -> "RegisteredSecretDetector":
        """按值首字符分组构造；组内按值长度降序（长值优先命中）。

        Args:
            values: ``RegisteredValue`` 或裸 ``str`` 序列；裸 ``str`` 按 ``default_kind`` 归类。
            default_kind: 裸 ``str`` 入参使用的 kind。

        Returns:
            注入后的 detector（值序列为空 ⇒ 空 detector，``could_match`` 恒 False）。
        """
        grouped: dict[str, list[RegisteredValue]] = {}
        for item in values or ():
            entry = item if isinstance(item, RegisteredValue) else RegisteredValue(value=item, kind=default_kind)
            # 空值无法作为分组键，也无法被 ``str.find`` 有意义地定位 → 跳过（防御）。
            if not entry.value:
                continue
            grouped.setdefault(entry.value[0], []).append(entry)
        return cls(
            _grouped={
                key: tuple(sorted(items, key=lambda s: len(s.value), reverse=True)) for key, items in grouped.items()
            }
        )

    def could_match(self, text: str) -> bool:
        """任一 group 的首字符出现在文本中即 True。"""
        if not isinstance(text, str) or not text:
            return False
        return any(char in text for char in self._grouped)

    def scan(self, text: str) -> list[Finding]:
        """对每个 group 做 ``str.find`` 循环推进，产出精确命中。"""
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []
        for items in self._grouped.values():
            for item in items:
                value = item.value
                offset = 0
                while (index := text.find(value, offset)) != -1:
                    findings.append(
                        Finding(
                            start=index,
                            end=index + len(value),
                            rule_id=f"registered:{item.kind}",
                            kind="registered_secret",
                            confidence="exact",
                            priority=self._REGISTERED_PRIORITY,
                        )
                    )
                    offset = index + len(value)
        return findings


@dataclass(frozen=True)
class EntropyDetector:
    """裸高熵兜底（最低优先级，仅在无结构化证据时启用）。

    Args:
        min_length: 候选最小长度（默认 32，来自 SecuritySettings）。
        alnum_threshold: alnum/Base64URL 阈值（默认 4.2）。
        base64_threshold: 标准 Base64 阈值（固定 4.5）。
        enabled: 门控开关（来自 SecuritySettings.redact_bare_entropy）。
    """

    # --- 类常量（ClassVar 强制：frozen dataclass 中「有注解无 ClassVar」会静默变成字段）
    # ---裸熵是最后兜底 —— 全 detector 最低优先级（registered 100 > pem 95 > ... > assignment 70）
    _ENTROPY_PRIORITY: ClassVar[int] = 50

    # 默认参数（与 SecuritySettings 默认值对齐；调用方可覆盖）
    _DEFAULT_MIN_LENGTH: ClassVar[int] = 32
    _DEFAULT_ALNUM_THRESHOLD: ClassVar[float] = 4.2
    # 标准 Base64 字符集更大（64 符号 vs 62），同长度下熵更高，故阈值更高（DESIGN D-09）
    _DEFAULT_BASE64_THRESHOLD: ClassVar[float] = 4.5

    # 候选提取：连续 base64/alnum/symbol 字符段，长度有界（T-06-21：无嵌套量词 / 无无界 * +）。
    # ``=`` 仅允许作为**尾部 padding**（base64 语义）—— 不允许出现在中部，
    # 否则``GPG_KEY=<value>`` 里的字段名会被 `=` 粘进候选，导致整个段落进 "other" 字符集而漏报。
    #
    # **扩展字符集（口令符号）**：原字符集仅 ``[A-Za-z0-9+/_-]``，导致含常见口令符号的
    # 真实口令被**切碎**成多个 < min_length 的片段 —— 每个片段都过不了长度判据，
    # 于是整条口令零命中（S054/S065 类）。密码学意义上「高熵」并不要求取值落在 base64 字母表内，
    # 把符号排除在外只是漏报来源，不是精度来源。
    #
    # 加入的是**口令常见且不与结构化文本冲突**的符号（``!@$%^&*()?~``` 等）；
    # 仍为**单一字符类**（无嵌套量词），O(n) 线性性质不变。
    #
    # 两条边界（各由一次回归钉住，勿再放宽）：
    #
    # 1. **必须白名单，不能用 ``[^\s…]`` 取反**（S124 回归）：取反会把 CJK 散文整段吞成候选
    #    —— 中文句子无空白、长度 > 32、字符类达标，于是「用户名林知遥，手机…」被判为
    #    bare_secret 整段替换。秘密值是 ASCII 编码产物，白名单天然把非 ASCII 排除在外，
    #    既修召回也不伤中文正文。
    # 2. **不得纳入 ``:`` ``/`` ``@`` ``#`` 等 URL/结构字符**（DSN 保真回归）：
    #    一旦纳入，``postgresql://app:S3cr3tPw@db.internal:5432/prod`` 会被熵检测器
    #    整串吞下，其 span 覆盖并**吞掉** ``dsn`` detector 的细粒度命中
    #    （合并后只留一个粗粒度 cluster），URL 的 host / port / dbname 全部丢失。
    #    结构性字符必须留给协议 detector 处理，熵兜底只认「值本体」。
    #
    # 尾部 ``=`` 仍只允许 padding（base64 语义）；中部 ``=`` 是分隔符，故不进字符类。
    # 引号 / 逗号 / 分号 / 花括号 / 尖括号 / 竖线 / 反斜杠 / 空白同样是结构分隔符，
    # 计入会让候选跨越多个无关 token，把「值」与「邻近文本」粘成假候选。
    _CANDIDATE_RE: ClassVar[re.Pattern] = re.compile(
        r"""[A-Za-z0-9!@$%^&*()_+\-\[\]?.~`]{32,4096}={0,2}""",
    )

    # 命中元数据
    _KIND: ClassVar[str] = "bare_secret"
    _CONFIDENCE: ClassVar[str] = "heuristic"
    # base64url 归入 base64 族（同一 rule_id 命名空间）
    _ALPHABET_BASE64_FAMILY: ClassVar[str] = "base64"

    # --- 以下 4 组私有常量自持（T-vq0）：原定义在 ``redaction.entropy``（该模块已于``260915-0dr`` 并入本文件），
    # 随本类独有的 4 个判据一并移入（值逐字复制）。
    # 共享统计 helper 的常量（_HEX_EXCLUDE_LENGTHS / _UUID_RE / _ULID_RE / _KSUID_RE）
    # 仍留在本文件上方的「共享统计工具」段。字符集分类标签（classify_alphabet 返回值）
    _ALPHABET_ALNUM: ClassVar[str] = "alnum"
    _ALPHABET_BASE64: ClassVar[str] = "base64"
    _ALPHABET_BASE64URL: ClassVar[str] = "base64url"
    # 口令符号族：候选含 base64 / base64url 字母表之外的符号（``!@#$%^&*`` 等）。
    # 归入 alnum 熵阈值（4.2）—— 其字母表大于 alnum 但小于标准 base64，
    # 且实测同长度下熵介于两者之间（S054 口令 ent=4.6757），4.2 判据即可覆盖。
    _ALPHABET_PASSWORD: ClassVar[str] = "password"
    _ALPHABET_OTHER: ClassVar[str] = "other"

    # 字符集成员判定
    _ALNUM_RE: ClassVar[re.Pattern] = re.compile(r"^[A-Za-z0-9]+$")
    # 标准 Base64：含 + / 或 padding =，其余为 base64 字符
    _BASE64_RE: ClassVar[re.Pattern] = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
    # Base64URL：含 - 或 _，其余为 base64url 字符
    _BASE64URL_CHARS_RE: ClassVar[re.Pattern] = re.compile(r"^[A-Za-z0-9_-]+$")
    # 口令符号族：至少含一个 alnum，且余下字符全部落在候选字母表内。
    # 要求含 alnum 是为了排除纯符号串（``!!!!…`` / ``-----`` 分隔线）——
    # 那不是口令，且其熵/字符类判据本就可疑。
    # 字母表与 :data:`_CANDIDATE_RE` 保持一致（同一次放宽的两个消费点，不可漂移）。
    _PASSWORD_CHARS_RE: ClassVar[re.Pattern] = re.compile(
        r"""^(?=.*[A-Za-z0-9])[A-Za-z0-9!@$%^&*()_+\-\[\]?.~`]+$""",
    )

    # base64 容器前缀（D-08 硬排除）：命中区间落在这些前缀之后则跳过
    _BASE64_CONTAINERS: ClassVar[tuple[str, ...]] = ("data:", ";base64,")
    # 容器前缀与命中区间之间若出现「空白 / 引号」，
    # 说明命中是独立 token 而非载荷（data URI 的 MIME 段 `text/plain;base64,` 里的 `;` `/` `,` 是 URI 语法，不算分隔）
    _CONTAINER_GAP_RE: ClassVar[re.Pattern] = re.compile(r"[\s\"'`]")

    # 重复串判据：去重后字符数不超过此值即视为重复
    _REPETITION_UNIQUE_MAX: ClassVar[int] = 2

    min_length: int = _DEFAULT_MIN_LENGTH
    alnum_threshold: float = _DEFAULT_ALNUM_THRESHOLD
    base64_threshold: float = _DEFAULT_BASE64_THRESHOLD
    enabled: bool = True
    rule_id: str = "entropy"

    @staticmethod
    def _classify_alphabet(value: str) -> str:
        """判断候选值的字符集归属。

        Args:
            value: 待分类字符串。

        Returns:
            ``"alnum"``（仅 ``[A-Za-z0-9]``）/ ``"base64"``（含 ``+`` ``/`` 或 padding ``=``）/
            ``"base64url"``（含 ``-`` 或 ``_``）/ ``"password"``（含上述字母表之外的符号，
            但至少含一个 alnum）/ ``"other"``（不符合上述任一，如纯符号 / 空串）。
        """
        if not value:
            return EntropyDetector._ALPHABET_OTHER
        if EntropyDetector._ALNUM_RE.match(value):
            return EntropyDetector._ALPHABET_ALNUM
        if EntropyDetector._BASE64_RE.match(value) and ("+" in value or "/" in value or "=" in value):
            return EntropyDetector._ALPHABET_BASE64
        if EntropyDetector._BASE64URL_CHARS_RE.match(value) and ("-" in value or "_" in value):
            return EntropyDetector._ALPHABET_BASE64URL
        if EntropyDetector._PASSWORD_CHARS_RE.match(value):
            return EntropyDetector._ALPHABET_PASSWORD
        return EntropyDetector._ALPHABET_OTHER

    @staticmethod
    def _is_monotonic_sequence(value: str) -> bool:
        """判断是否为连续递增 / 递减序列（``abcdefg…`` / ``123456…`` / 反向）。

        判据：对相邻字符差值取 ``Counter``，若单一差值占绝对多数（``>= len-2`` 次）
        即判为序列。长度 < 4 无意义，直接返回 False。

        Args:
            value: 待判断字符串。

        Returns:
            True 表示形如连续序列。
        """
        if len(value) < 4:
            return False
        steps = Counter(ord(b) - ord(a) for a, b in zip(value, value[1:]))
        return steps.most_common(1)[0][1] >= len(value) - 2

    @staticmethod
    def _is_repetition(value: str) -> bool:
        """判断是否为重复串（同一字符反复，或去重后字符极少）。

        Args:
            value: 待判断字符串。

        Returns:
            True 表示去重后字符数 <= 2（如 ``aaaa…`` / ``ababab…``）。
        """
        if len(value) < 4:
            return False
        return len(set(value)) <= EntropyDetector._REPETITION_UNIQUE_MAX

    @staticmethod
    def _is_base64_container_span(text: str, start: int, end: int) -> bool:
        """D-08 硬排除：命中区间是否落在 ``data:`` / ``;base64,`` 容器载荷内。

        判据是 **span 位置关系**，不是「文本含 data:」——
        命中区间之前最近的容器前缀与命中起点之间若**不含**空格 / 引号 / 标点等分隔符，
        则命中确在 data URI 载荷里，返回 True。

        Args:
            text: 原始文本。
            start: 命中区间起始偏移（闭）。
            end: 命中区间结束偏移（开）。

        Returns:
            True 表示该命中应作为 base64 容器载荷被硬排除。
        """
        for marker in EntropyDetector._BASE64_CONTAINERS:
            idx = text.rfind(marker, 0, start)
            if idx == -1:
                continue
            # 命中区间须在该前缀之后
            if idx + len(marker) > start:
                continue
            gap = text[idx + len(marker) : start]
            if not EntropyDetector._CONTAINER_GAP_RE.search(gap):
                return True
        return False

    def could_match(self, text: str) -> bool:
        """廉价预筛选：enabled 且文本含长度 >= min_length 的 alnum/base64 段。"""
        if not isinstance(text, str):
            return False
        return self.enabled and bool(self._CANDIDATE_RE.search(text))

    def scan(self, text: str) -> list[Finding]:
        # 预筛选所有权在本方法（T-vq0）：调用方无需（也不再）先行 could_match 判定，
        # 保证「新 detector 忘记接入预筛选」这一缺陷 3 的根因不再复现。
        if not self.could_match(text):
            return []

        findings: list[Finding] = []
        for match in self._CANDIDATE_RE.finditer(text):
            candidate = match.group(0)
            # 占位符短路：与 assignment 共用同一判据（单条定义，两处消费）。
            # 例：`changeme`×多字符 / `IMPORTANT_PLACEHOLDER` 这类长串字符类与熵都可能达标；
            # 值本身是「未填」标记时不该被当秘密（也保证二次脱敏幂等）。
            if is_placeholder_value(candidate):
                continue
            alphabet = self._classify_if_secret(candidate, text, match.start())
            if alphabet is None:
                continue
            rule_family = self._ALPHABET_BASE64_FAMILY if alphabet == self._ALPHABET_BASE64_FAMILY else alphabet
            findings.append(
                Finding(
                    start=match.start(),
                    end=match.end(),
                    rule_id=f"entropy.bare_{rule_family}",
                    kind=self._KIND,
                    confidence=self._CONFIDENCE,
                    priority=self._ENTROPY_PRIORITY,
                )
            )
        return findings

    def _classify_if_secret(self, candidate: str, text: str, start: int) -> str | None:
        """判据（短路顺序即判据顺序）；命中返回 alphabet，否则返回 None。"""
        # 1. 长度
        if len(candidate) < self.min_length:
            return None
        # 2. 字符集：候选正则已限定「非空白 + 非结构分隔符」，故此处恒为已知字母表之一。
        #    ``"other"`` 分支保留为防御（正则与分类表若日后失配，宁可漏报不误报）。
        alphabet = self._classify_alphabet(candidate)
        if alphabet == self._ALPHABET_OTHER:
            return None
        # 3. 形状排除（形状判据，不依赖熵）
        if (
            is_uuid_like(candidate)
            or is_fixed_hex(candidate)
            or self._is_monotonic_sequence(candidate)
            or self._is_repetition(candidate)
        ):
            return None
        # 4. 字符类别
        if char_class_count(candidate) < 3:
            return None
        # 5. 熵阈值（标准 Base64 更高）
        threshold = self.base64_threshold if alphabet == self._ALPHABET_BASE64_FAMILY else self.alnum_threshold
        if shannon_entropy(candidate) < threshold:
            return None
        # 6. data: / ;base64, 容器载荷硬排除（D-08）
        if self._is_base64_container_span(text, start, start + len(candidate)):
            return None
        # 7. PEM 非秘密块 body 硬排除（缺陷 4）：公钥 / 证书不是秘密。 PemDetector 已用 LABEL 白名单排除它们，但因其不产出 finding，
        #   裸熵兜底仍会遮掉 base64 body —— 此处以 span 位置谓词补齐（与 D-08 同构）。
        if is_pem_non_secret_span(text, start, start + len(candidate)):
            return None
        # 8. OpenSSH 公钥 body 硬排除（S035）：`ssh-ed25519 <base64>` 无 PEM 包装，
        #   第 7 步管不到，其 body 熵天然高（4.78）会被兜底遮掉 —— 公钥不是秘密。
        if is_public_key_span(text, start, start + len(candidate)):
            return None
        # 9. 公开密码学参数字段保留（S120/S121）：`nonce=` / `salt=` / `iv=` 的值
        #   与真实 token 在熵/形状上完全同形，只能靠紧邻字段名保留（用户已批准）。
        if is_non_secret_field_value(text, start):
            return None
        return alphabet


__all__ = [
    # detector 协议
    "Detector",
    # 结构化协议 detector（按证据强度降序：pem 95 > headers/dsn 90 > jwt 88 > cookie/url 85）
    "PemDetector",
    "HeadersDetector",
    "DsnDetector",
    "JdbcDetector",
    "JwtDetector",
    "CookieDetector",
    "UrlDetector",
    # 已注册值与厂商前缀
    "RegisteredSecretDetector",
    "VendorTokenDetector",
    # 上下文推断（严格字段 + 歧义字段评分）
    "AssignmentDetector",
    # 裸高熵兜底（最低优先级）
    "EntropyDetector",
]
