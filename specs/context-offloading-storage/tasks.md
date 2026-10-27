# LLM 装配：Offloading 分层存储 任务清单

- 变更名: context-offloading-storage
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/context-offloading-storage/spec.md
- 方案: specs/context-offloading-storage/plan.md

## 需求覆盖

| 需求 | 任务 |
| --- | --- |
| REQ-1 数据模型与必填字段 | T-2、T-4 |
| REQ-2 写入路径与校验 | T-4 |
| REQ-3 更新与并发冲突 | T-4 |
| REQ-4 失效（invalidate） | T-4 |
| REQ-5 按过滤器加载 | T-5 |
| REQ-6 卸载与按需回捞 | T-6 |
| REQ-7 冷层不可用时降级 | T-3、T-6 |
| REQ-8 淘汰顺序与永不淘汰约束 | T-7 |
| REQ-9 类别注册扩展 | T-2 |
| REQ-10 生命周期到期清理 | T-8 |
| REQ-11 作用域隔离与并发安全 | T-5、T-10 |
| REQ-12 配置项 | T-1 |
| REQ-13 十类样例数据与全链路可测 | T-11 |
| REQ-14 可观测 | T-9 |
| REQ-15 测试覆盖 | T-1 ~ T-12 |
| REQ-16 现有装配链路零回归 | T-12 |

## T-1 配置安全解析与 StoreConfig

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/config.py`、`src/agent/aidev_agent/core/packages/__init__.py`、`src/agent/aidev_agent/core/packages/context_engine/__init__.py`、`src/agent/aidev_agent/core/packages/context_engine/settings.py`、`src/agent/tests/core/packages/__init__.py`、`src/agent/tests/core/packages/context_engine/__init__.py`、`src/agent/tests/core/packages/context_engine/test_settings.py`
- 需求: REQ-12
- 依赖: 无
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_settings.py`
- [ ] 完成标准：`_env_positive_int` 位于 `env` 声明之后、`existed_keys` 之前；4 个 `BKAI_CONTEXT_STORE_*` 常量可经 `settings` 读到；未设置时返回 `1000 / 5000 / 900 / 60`；取值 `0`、`-1`、`abc`、空串时回落默认并产生一条 warning，进程不抛异常；`settings` 中不出现 `_env_positive_int`；`StoreConfig.from_settings()` 返回与常量一致的四个字段，且 `StoreConfig(...)` 可直接覆盖。

## T-2 数据模型、异常与类别注册表

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/packages/context_engine/models.py`、`src/agent/aidev_agent/core/packages/context_engine/errors.py`、`src/agent/aidev_agent/core/packages/context_engine/registry.py`、`src/agent/tests/core/packages/context_engine/test_registry.py`
- 需求: REQ-1、REQ-9
- 依赖: 无
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_registry.py`
- [ ] 完成标准：`Lifecycle` 七值、`EntryStatus` 三值与 `ContextEntry` / `EntryFilter` / `LoadResult` / `MaintenanceReport` / `StoreStats` 定义齐备；`build_builtin_registry()` 产出十类，名称、默认生命周期、淘汰优先级与 `plan.md` 表格逐项一致，仅 `System Instructions` 的 `never_evict=True`；重复注册抛 `DuplicateCategoryError` 且既有类别行为不变（AC-8）；查询未注册类别抛 `UnknownCategoryError`；`store.py` 内不出现任何类别名。

## T-3 冷层协议与内存后端

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/packages/context_engine/cold.py`、`src/agent/tests/core/packages/context_engine/test_tiering.py`
- 需求: REQ-7
- 依赖: 无
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_tiering.py args="-k cold_backend"`
- [ ] 完成标准：`ColdBackend` Protocol 含 `put/get/delete/count` 四方法；`InMemoryColdBackend` 以 `(scope, entry_id)` 为键存 `(kind, blob)`，`count(kind=None)` 支持按 `kind` 过滤；同 `(scope, id)` 再次 `put` 覆盖旧记录；并发 `put/get` 无异常；测试内可注入一个「读写均抛异常」的假后端（不进入生产代码）。

## T-4 写入、更新与失效原语

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/packages/context_engine/store.py`、`src/agent/tests/core/packages/context_engine/test_write_path.py`、`src/agent/tests/core/packages/context_engine/test_invalidate.py`
- 需求: REQ-1、REQ-2、REQ-3、REQ-4
- 依赖: T-1、T-2、T-3
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_write_path.py`、`make test path=src/agent/tests/core/packages/context_engine/test_invalidate.py`
- [ ] 完成标准：`write` 分配 `id` 与 `version=1` 并写入热层 + 冷层备份；缺字段、空 `scope`/`source`、非法 `lifecycle`、未注册类别、不可序列化 payload 分别抛对应异常，且条目总数、`stats()` 计数与冷层键数均不变（AC-2 / AC-3）；`lifecycle` 缺省取类别默认值；`update` 版本一致时 `version+1` 且 `updated_at` 前进、不一致抛 `VersionConflictError` 并携带当前版本、条目内容不变（AC-9）；`invalidate` 支持按 id 与按过滤器，重复调用返回 `0` 且条目持续不可见（AC-12），未命中条目不受影响，两者都缺条件抛 `EntryValidationError`。

## T-5 过滤加载与作用域隔离

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/packages/context_engine/store.py`、`src/agent/tests/core/packages/context_engine/test_load_filter.py`
- 需求: REQ-5、REQ-11
- 依赖: T-4
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_load_filter.py`
- [ ] 完成标准：`load` 支持类别、生命周期、标签、来源四维过滤，维度内为「或」、维度间为「与」，`labels` 采用交集非空判定；「仅当前轮 + 会话级」与「含 Agent 级 Tool Definitions」两个过滤器分别返回正确子集，无遗漏无多余（AC-4）；命中为空返回空 `entries` 而非异常；`evicted` 条目不出现在结果中；两个 scope 使用同一实例时互相不可见（AC-13）；返回条目顺序不被文档化为语义顺序。

## T-6 闲置卸载、按需回捞与降级

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/packages/context_engine/policies.py`、`src/agent/aidev_agent/core/packages/context_engine/store.py`、`src/agent/tests/core/packages/context_engine/test_tiering.py`
- 需求: REQ-6、REQ-7
- 依赖: T-4、T-5
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_tiering.py`
- [ ] 完成标准：注入时钟推进超过 `idle_seconds` 后调用 `sweep()`，条目 `status=offloaded`、热层 `payload is None`、热层计数下降、冷层 `offloaded` 计数上升（AC-5）；随后 `load` 自动回捞，返回完整 payload、`status=active`、`last_accessed_at` 刷新，调用方无需额外操作；冷层读异常时 `load` 正常返回热层条目并置 `degraded=True`、失败条目进 `skipped_ids`；冷层写异常时 `write`/`update` 仍成功且仅热层生效（AC-6）；卸载与回捞各写一条含 id、类别、原因的日志。

## T-7 容量淘汰与永不淘汰约束

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/packages/context_engine/policies.py`、`src/agent/aidev_agent/core/packages/context_engine/store.py`、`src/agent/tests/core/packages/context_engine/test_tiering.py`
- 需求: REQ-8
- 依赖: T-6
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_tiering.py args="-k eviction"`
- [ ] 完成标准：热层超 `hot_capacity` 时按 `(-eviction_priority, last_accessed_at, id)` 顺序卸载，卸载后计数回到上限内；冷层 `offloaded` 超 `cold_capacity` 时按同序彻底淘汰并回收索引与计数；`System Instructions` 在满容量压力场景下始终可加载且从未被卸载或淘汰（AC-11）；热层被 `System Instructions` 填满时允许超限并输出 warning；排序键与候选筛选以纯函数形式落在 `policies.py`，可单测。

## T-8 生命周期到期清理与引用延期

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/packages/context_engine/store.py`、`src/agent/aidev_agent/core/packages/context_engine/policies.py`、`src/agent/tests/core/packages/context_engine/test_expiry.py`
- 需求: REQ-10
- 依赖: T-6
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_expiry.py`
- [ ] 完成标准：`end_turn` / `end_request` / `end_session` / `end_task` 分别处理对应生命周期；`turn` 的 `Execution Trace` 到期按「优先级 ≥ 80 直接淘汰」处理（AC-10）；`ref_count > 0` 的条目进 `deferred` 且不卸载不淘汰，`release` 后下次清理生效；`platform`/`agent`/`user` 条目在任何 `end_*` 下都不被清理；单条异常被捕获并继续处理其余条目，连续 3 次失败输出 error 级告警并在成功一次后清零。

## T-9 可观测与惰性维护节流

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/packages/context_engine/store.py`、`src/agent/tests/core/packages/context_engine/test_observability.py`
- 需求: REQ-14
- 依赖: T-6、T-7、T-8
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_observability.py`
- [ ] 完成标准：`stats()` 返回热层条目数、`offloaded` 数、`backup` 数、按类别条目数、卸载/回捞/淘汰次数与 token 估算总量，调用前后所有条目状态、`last_accessed_at` 与冷层记录不变（AC-16）；token 估算在写入时算一次并随元数据保存；`write`/`update`/`invalidate` 入口按 `sweep_interval` 节流触发维护，注入时钟未推进时不重复扫描；日志前缀统一 `[ContextOffloading]`，卸载/回捞/淘汰/降级/到期清理各有一条含 id 与类别的记录。

## T-10 并发安全

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/aidev_agent/core/packages/context_engine/store.py`、`src/agent/tests/core/packages/context_engine/test_concurrency.py`
- 需求: REQ-11
- 依赖: T-5
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_concurrency.py`
- [ ] 完成标准：多线程并发写入 N 条后 N 条全部可加载且无未捕获异常、无丢失（AC-14）；asyncio 协程并发写入同样全部可加载；并发 `update` 同一 id 只有一个成功，其余收到 `VersionConflictError`；`store.py` 与 `cold.py` 不存在承载条目的模块级可变容器（代码评审确认，条目只挂在实例属性上）；并发用例顺带打印写入吞吐实测值。

## T-11 十类样例数据全链路

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: `src/agent/tests/core/packages/context_engine/conftest.py`、`src/agent/tests/core/packages/context_engine/test_builtin_categories_e2e.py`
- 需求: REQ-13
- 依赖: T-5、T-6、T-7、T-8
- 验证: `make test path=src/agent/tests/core/packages/context_engine/test_builtin_categories_e2e.py`
- [ ] 完成标准：夹具为十类各写入至少一条样例，`Working State` 与 `Environment & Governance` 各覆盖 ≥2 个子上下文标签；全链路「写入 → 过滤加载 → 卸载 → 回捞 → 到期清理」跑通，读回的 payload、类别、生命周期、来源与提交一致（AC-1）；注册一个全新类别后以同一夹具跑通同一条链路且核心代码零修改（AC-7）；夹具位于测试目录内，不进入生产代码路径。

## T-12 零回归验收

- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 文件: 无源码改动（仅执行既有用例）
- 需求: REQ-16
- 依赖: T-9、T-10、T-11
- 验证: `make test path=src/agent/tests/core/nodes/model`、`make test path=src/agent/tests/core/nodes/test_context_processor_pipeline.py`、`make test path=src/agent/tests/core/nodes/test_context_processor_template_middleware.py`、`make test path=src/agent/tests/core/nodes/test_context_processor_variables_middleware.py`、`make test path=src/agent/tests/core/nodes/test_token_compression.py`、`make test path=src/agent/tests/core/nodes/test_model.py`、`make test path=src/agent/tests/test_config.py`
- [ ] 完成标准：上述用例全部通过；`git diff develop -- src/agent/aidev_agent` 只显示 `config.py` 的新增行与新包文件，涉及 `ContextAssembly`、`PromptSlots`、`PromptMiddleware` 系列、`chat_history_assembly`、`token_compression` 的文件 diff 为空（AC-17）；相同输入下 prompt 装配结果与变更前逐项一致（由既有用例断言）。
