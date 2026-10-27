# 代码评审

## 最新结论

- 结论: 需要返工
- 评审版本: 96c0d2cfeb2fb968705c38becc8a1889e3c34a70

### 阻塞项

- **F-1 冷层备份过期会导致「更新已提交、之后被静默回滚」（数据正确性）**
  - 证据：
    - `src/agent/aidev_agent/core/packages/context_engine/store.py:480-504` 的 `_offload` 在冷层已有记录时**直接复用该记录的 blob**（`record[1] if record is not None else _serialize(entry.payload)`），不再以热层 payload 为准。
    - `store.py:548-552` 的 `_write_backup` 对冷层写入异常只记 warning：既不重试，也不把该条目标记为「备份不可信」。
    - 二者组合后，REQ-7 允许的「冷层写入异常下 update 仍成功」会留下「热层 = 新版本 / 冷层 = 旧版本」的不一致；冷层恢复后的下一次卸载把旧 blob 翻为 `offloaded` 并清空热层 payload（`store.py:500-501`），此后 `load` 回捞返回旧内容，最新已提交 payload 无告警丢失。
  - 复现（2026-10-27 在本轮 `reviewed_sha` 上实跑，注入「可关闭写失败」的内存冷层）：
    ```
    write  Conversation History {"v": 1}          -> cold 备份 {"v": 1}
    冷层 fail_writes=True；update 到 {"v": 2}      -> 热层 v2、version 2；冷层仍为 {"v": 1}（只有一条 warning）
    冷层恢复；clock +120s；sweep()                 -> MaintenanceReport(offloaded=1, failed=0)
    load()                                        -> payload {'v': 1}，status=active
    ```
    即一次成功提交的更新在 120 秒后变成旧值，且 `degraded=False`、无任何失败信号。
  - 影响范围：默认 `InMemoryColdBackend` 不会写失败，故现网内存形态不触发；但 REQ-7 明确要求「写入与更新路径在冷层不可用时仍成功」，且规格要求冷层后端可替换（REQ-8/plan D-3），换成外置后端后这是可达路径。违反规格「边界与非功能」中「正确性优先于吞吐：卸载/淘汰/回捞不得丢失 `active` 条目的 payload」，并使 AC-5「加载请求仍必须取回完整内容」在降级-恢复序列下失真。
  - 修复后可判定的验收标准：
    1. 新增一条用例覆盖「write v1 → 冷层写失败下 update 到 v2 → 冷层恢复 → 越过闲置阈值 `sweep()` → `load()`」，断言返回 payload 为 `{"v": 2}`、`status=active`、`degraded=False`；
    2. 同一条用例断言冷层中该条目的记录内容为最新 payload（`offloaded` 与恢复后的 `backup` 均不得为旧版本）；
    3. 可选但推荐：`_offload` 以热层 payload 重新序列化为准（或校验冷层 blob 与热层一致后才翻转），使「冷层备份过期」不再能污染卸载路径；
    4. 定向用例 `tests/core/packages/context_engine/` 仍全部通过（当前 117 条），零回归 7 组用例仍全部通过。

### 改进建议

- **W-1 `labels=None` 逃出校验层，抛原生 `TypeError`**
  `store.py:142` 先判 `isinstance(labels, str)`，再对 `labels` 做 `all(...)` 迭代；`labels=None` 时直接 `TypeError: 'NoneType' object is not iterable`，而 REQ-2 要求校验失败返回「可识别错误」。实测存储状态未变化（无副作用），故不阻塞；建议先归一化为 `tuple(labels or ())` 再校验，统一抛 `EntryValidationError`。

- **W-2 `sweep(scope=...)` 的容量口径跨 scope 不自洽**
  `store.py:337` 的 `overflow` 取全实例 `active` 数减 `hot_capacity`，但 `store.py:339-344` 的候选只从该 `scope` 内挑；多会话共享实例时按 scope 调用会少卸载，或误报「无法完全卸载」。`_maybe_maintain` 走的是全局 `sweep()`，故不阻塞；建议按 scope 子集计算 overflow，或在 docstring 中明确该参数只做「范围限定 + 全局容量判定」。

- **W-3 热层超限用「卸载」而非 REQ-8 字面的「淘汰（彻底移除）」**
  `store.py:345-349` 对热层溢出执行 `_offload`（可回捞），只有冷层溢出才 `_evict`。这与已审批的 `plan.md` D-4「热层 → 卸载；冷层 → 淘汰」及 AC-11 的判定口径一致，本轮不判阻塞；但 REQ-8 原文写的是「热层或冷层超出容量上限时……淘汰为彻底移除（不可还原）」，按原文复验会得出不同结论。建议在规格或 plan 的偏差表登记该口径，避免后续评估侧按字面取数。

- **W-4 失效条目只留空壳且不计入容量**
  `store.py:237-240` 的 `invalidate` 置 `evicted` 并清 payload，但条目仍留在 `_entries`；`stats()` 与容量计数都跳过 `evicted`，长生命周期实例（会话级 scope 反复失效 `Execution Trace`）会累积元数据空壳。REQ-4 要求「逻辑删除」，故不阻塞；建议在 plan 风险表登记该增长形态，或另立变更提供显式物理清理入口。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "REWORK",
  "change_name": "context-offloading-storage",
  "iteration": 1,
  "reviewed_sha": "96c0d2cfeb2fb968705c38becc8a1889e3c34a70",
  "blocking": [
    {
      "id": "F-1",
      "severity": "data",
      "requirement_ids": ["REQ-3", "REQ-6", "REQ-7", "REQ-8", "AC-5"],
      "evidence": "store.py:480-504 _offload 复用冷层已有记录 blob（record[1] if record is not None）而非热层 payload；store.py:548-552 _write_backup 对冷层写失败只 warning、不回写不降级。实测：write v1 → 冷层 fail_writes 下 update 到 v2（热层 v2、冷层 v1）→ 冷层恢复 → +120s sweep（offloaded=1）→ load 返回 {'v': 1}、degraded=False；已提交的最新 payload 静默丢失",
      "acceptance": "新增用例覆盖 write v1 → 冷层写失败下 update v2 → 冷层恢复 → 越过 idle_seconds 的 sweep → load 的序列，断言返回 payload 为 {'v': 2}、status=active、degraded=False，且冷层该条目记录内容为最新 payload；_offload 以热层 payload 为准或校验一致后再翻转；context_engine 目录 117 条与零回归 7 组用例仍全通过"
    }
  ],
  "warnings": [
    {
      "id": "W-1",
      "evidence": "store.py:142 labels=None 时 all(...) 抛原生 TypeError，未归一为 EntryValidationError（存储状态无变化）",
      "suggestion": "先 labels = tuple(labels or ()) 再校验，统一抛 EntryValidationError"
    },
    {
      "id": "W-2",
      "evidence": "store.py:337 overflow 取全实例 active 计数，store.py:339-344 候选只取传入 scope，多 scope 场景会少卸载或误报警告",
      "suggestion": "按 scope 子集计算 overflow，或在 docstring 明确 scope 只做范围限定"
    },
    {
      "id": "W-3",
      "evidence": "store.py:345-349 热层超限执行 _offload（可回捞），REQ-8 原文要求「淘汰为彻底移除」；plan D-4 与 AC-11 支持当前实现",
      "suggestion": "在规格或 plan 的偏差表登记该口径差异，避免按 REQ-8 原文复验判失败"
    },
    {
      "id": "W-4",
      "evidence": "store.py:237-240 invalidate 只置 evicted 并清 payload，空壳永久留在 _entries 且不计入 hot/offloaded 容量",
      "suggestion": "在 plan 风险表登记该增长形态，或另立变更提供显式物理清理入口"
    }
  ]
}
```

</details>

## 第 1 轮

### 评审范围

- 仓库: `bk-aidev-agent`（单仓，`spec.md` 声明无跨仓依赖、无跨仓执行顺序）
- 工作分支: `feature/context-offloading-storage`，评审开始 HEAD `96c0d2cfeb2fb968705c38becc8a1889e3c34a70`（`git ls-remote --heads` 与本地一致，评审 worktree 工作区 clean）
- 基线: `develop` `7a171a8af285c24733d75a16428ef31edfcbbd05`
- 累计差异: 26 个文件、+3624 / −0（`git diff 7a171a8a 96c0d2cf`）；其中实现 9 个文件、测试 11 个文件、规格 3 个文件
- 既有文件改动清单（`git diff --name-status` 中唯一的 `M`）：`src/agent/aidev_agent/config.py`（+36），其余全部为新增文件
- 冻结文档：`spec.md` 与已审批版本（`224d7403`）逐字节一致；`plan.md` 自审批提交 `6610296f` 起未改动；`tasks.md` 仅追加勾选与验证记录（+58 / −12），无需求或任务定义改写

### 需求核对（REQ-1 ~ REQ-16）

| 需求 | 实现落点 | 结论 |
| --- | --- | --- |
| REQ-1 数据模型 | `models.py:16-90`：七值 `Lifecycle`、三值 `EntryStatus`、`ContextEntry` 全字段 | 符合 |
| REQ-2 写入校验与零副作用 | `store.py:113-170`：校验全部前置于落库，`_serialize` 失败抛 `PayloadSerializationError` | 符合（W-1 例外：`labels=None`） |
| REQ-3 更新与版本冲突 | `store.py:172-205`、`errors.py:43-49`：冲突携带 `current_version`，无字段级部分覆盖 | 逻辑符合，但卸载后回滚见 F-1 |
| REQ-4 失效与幂等 | `store.py:207-243`：按 id/过滤器、返回本次新失效数、重复调用为 0 | 符合（W-4 空壳） |
| REQ-5 按过滤器加载 | `models.py:93-128`：维度内或、维度间与、标签交集、空结果不报错 | 符合 |
| REQ-6 卸载与回捞 | `store.py:310-376`、`store.py:506-536`：闲置阈值 `>=`、回捞透明并刷新 `last_accessed_at` | 符合（F-1 影响回捞内容） |
| REQ-7 冷层降级 | `store.py:264-270`、`store.py:548-552`：读失败 `degraded + skipped_ids`，写失败不阻断 | 符合（F-1 由「写失败不阻断」衍生） |
| REQ-8 淘汰顺序与永不淘汰 | `policies.py:28-95`、`store.py:357-373`：`(-priority, last_accessed_at, id)`、`never_evict` 跳过 | 符合（W-3 口径差异） |
| REQ-9 类别注册扩展 | `registry.py:50-95`、`registry.py:98-172`：十类以数据表声明，`store.py` 无类别名 | 符合 |
| REQ-10 到期清理 | `store.py:290-308`、`store.py:419-468`、`policies.py:97-112`：四个 `end_*`、引用延期、连续失败告警 | 符合 |
| REQ-11 隔离与并发 | `store.py:90-92`：实例级 `RLock` + `scope` 一级键，无模块级容器 | 符合 |
| REQ-12 配置项 | `config.py:116-141` 与 `:192-195`、`settings.py:14-46`：4 个 `BKAI_CONTEXT_STORE_*`，非法值回落 + warning | 符合 |
| REQ-13 十类样例与全链路 | `tests/.../conftest.py:114-188`、`test_builtin_categories_e2e.py:34-105` | 符合 |
| REQ-14 可观测 | `store.py:378-405`、日志前缀 `[ContextOffloading]` 含 id/类别/原因 | 符合 |
| REQ-15 测试覆盖 | 10 个测试文件 + 夹具，117 条 | 符合 |
| REQ-16 零回归 | `git diff 7a171a8a 96c0d2cf -- src/agent/aidev_agent/core/nodes src/agent/aidev_agent/core/graphs` 为空 | 符合 |

### 验证复核

按「默认不重跑、仅在记录可疑时复跑」的原则，先比对 `verification.md` 与实际改动：记录中的改动清单、命令与断言与代码一致，故只做一次抽样复核（用既有 venv 直跑本轮 SHA 的代码，`PYTHONPATH` 指向评审 worktree）：

| 复跑范围 | 结果 | 与记录对比 |
| --- | --- | --- |
| `tests/core/packages/context_engine/` | 117 passed | 与记录一致 |
| `tests/test_config.py` + `tests/core/nodes/model` + 三个 context_processor 用例 + `test_token_compression.py` + `test_model.py` | 234 passed / 1 xfailed | 与记录 119+3+8+12+40+37+15 一致 |

结论：验证记录真实、可复现，无「记录缺失或失败未解释」的情况；本轮 REWORK 不因验证失败，而因 F-1 的降级-恢复序列未被现有用例覆盖。

### 代码走查要点（除 F-1 外）

- 类别无分支：`store.py` 全文无任何类别名，优先级/生命周期/`never_evict`/token 预估全部经 `_registry`，符合 REQ-9 硬约束。
- 锁顺序：`store.py` 只在持有 `RLock` 时调用冷层，冷层用自身 `Lock`，无反向嵌套，未见死锁风险。
- 零副作用：`write` 全量校验后才构造条目并插入，12 组非法输入用例断言 `(hot, offloaded, tokens, cold.count)` 均不变，符合 AC-2 / AC-3。
- 到期语义：`end_turn` 只处理 `turn`，`platform`/`agent`/`user` 无入口，符合 REQ-10 与 AC-10。
- 规格偏差：仅 W-3（热层超限用卸载而非彻底淘汰），与已审批 `plan.md` D-4 一致，按本轮规则不判阻塞。

### 结论

存在 1 项数据正确性阻塞项（F-1：更新后冷层备份过期会导致 `load` 返回旧 payload 且无任何降级信号）。按固定验收规则「没有正确性、安全或数据方面的阻塞项」未满足，本轮判定 **需要返工**，返回「代码实现」节点（`790e2dbb-4758-4407-b48e-15bf27b341ca`）。四个改进建议 W-1 ~ W-4 不作为打回理由。
