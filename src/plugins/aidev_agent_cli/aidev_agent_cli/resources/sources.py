# -*- coding: utf-8 -*-
"""--resources 的来源名、预设展开与校验。**只依赖标准库**：入口在装 stdout guard 之前就要用它。

这里认**名字**与**预设字符串**：命中 :data:`PRESETS` 的取值先展开成一组来源名，再
校验取值合法、去重、检查底座位置。预设展开与去重都在本模块完成，入口只需在省略
``--resources`` 时给出预设名。本模块不出现「协议」概念。

约束：底座（platform / injected）至多一个且必须排在第一位；重复项只保留第一次出现
（去重发生在预设展开之后，避免展开后再重复加载同一来源）。
"""

from __future__ import annotations

from typing import Iterable

#: 底座来源：提供 agent 配置的基础 dict。
BASE_SOURCES = ("platform", "injected")

#: 叠加在底座之上的来源（默认顺序即推荐的优先级顺序）。
OVERLAY_SOURCES = ("approval", "local", "mcp", "knowledge", "workspace", "runtime")

SOURCES = BASE_SOURCES + OVERLAY_SOURCES

#: 平台底座的默认叠加：``default`` 预设展开成它，也是省略 ``--resources`` 时的取值。
PLATFORM_SOURCES = ("platform", *OVERLAY_SOURCES)

#: 预设字符串 → 展开后的来源名序列。目前只有一个预设。
PRESETS: dict[str, tuple[str, ...]] = {"default": PLATFORM_SOURCES}


def parse_resources(names: Iterable[str]) -> tuple[str, ...]:
    """展开预设、去重、校验来源名；非法时抛 ValueError（信息可直接给用户看）。

    ``names`` 里的每一项要么是一个预设字符串（命中 :data:`PRESETS` 则展开成一组来源名），
    要么是一个普通来源名。展开完成后再去重（保序），然后校验每个名字合法性、底座位置。
    ``platform`` / ``injected`` 都是普通来源名，不是预设。
    """
    expanded: list[str] = []
    for raw in names:
        item = str(raw).strip()
        if not item:
            continue
        if item in PRESETS:
            expanded.extend(PRESETS[item])
        else:
            expanded.append(item)

    # 去重发生在预设展开之后：例如 ("default", "platform") 不会重复加载 platform。
    items = list(dict.fromkeys(expanded))

    if not items:
        raise ValueError("--resources 不能为空")

    for item in items:
        if item not in SOURCES:
            raise ValueError(f"未知的资源来源：{item}（可选：{', '.join(SOURCES)}）")

    bases = [name for name in items if name in BASE_SOURCES]
    if len(bases) > 1:
        raise ValueError(f"底座来源至多一个，收到：{', '.join(bases)}")
    if bases and items[0] != bases[0]:
        raise ValueError(f"底座来源 {bases[0]} 必须排在第一位")

    return tuple(items)


def uses_platform(names: Iterable[str]) -> bool:
    return "platform" in tuple(names)
