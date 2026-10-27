# LLM 装配：Offloading 分层存储 技术方案

- 变更名: context-offloading-storage
- 仓库: git@github.com:ChrisJaunes/bk-aidev-agent.git
- 规格: specs/context-offloading-storage/spec.md

## 背景与目标

SDK 在 LLM 装配前需要把十类上下文（系统指令、Agent 定义、工具定义、技能、用户上下文、会话历史、当轮输入、执行轨迹、工作状态、环境与治理）交给装配策略。当前这些上下文的原始数据分散在各生产方，没有统一的承载结构与分层卸载能力，导致装配只能整体取舍，也无法为后续「Handoff 压缩」「工具延迟加载」提供落点。

本方案交付一个**与装配链路完全解耦**的存储底座：统一的 `ContextEntry` 数据模型、写入/更新/失效/加载四组同步原语、注册式类别体系、热/冷两层存储（闲置卸载、加载回捞、容量淘汰、到期清理）与只读统计。装配策略、Handoff、工具延迟加载等消费方在后续变更中接入，本期不接线。

目标：

1. 十类上下文（含子上下文标签）能以样例数据完成「写入 → 加载 → 卸载 → 还原 → 到期清理」全链路。
2. 存储核心无类别分支：类别、默认生命周期、典型来源、淘汰优先级、token 预估器全部由注册表声明。
3. 冷层不可用时加载降级为「热层数据 + 完整性标志」，写入与更新不受影响。
4. 现有装配链路（`ContextAssembly`、`PromptSlots`、`PromptMiddleware` 系列、`chat_history_assembly`、`token_compression`）零改动、零回归。

## 非目标

沿用规格的非目标，并在实现层明确以下不做项：

- 不改造任何真实上下文生产方，不把 system prompt / 工具定义 / 会话历史 / 执行轨迹的写入接到本存储。
- 不实现任何装配策略：不决定条目顺序、截断与格式化，`load` 的返回顺序不承载语义。
- 不定义 Handoff、工具延迟加载、工具加载模式、PTC 的写入契约；接口只保证对任意已注册类别通用。
- 不引入外置冷层（本地文件 / Redis / 数据库），不保证重启后还原、不保证多实例共享。
- 不新增 HTTP 字段、流式事件、span 属性、请求级参数。
- 不引入后台线程、定时器或进程级单例容器：所有维护动作同步执行，由调用方显式驱动（已确认，见 D-2）。
- 不做二级索引、布隆过滤器、分片等容量优化：千级条目下全扫描足够，避免过度设计。
- 不做鉴权与租户校验，隔离只依赖调用方传入的 `scope`。

## 现状

| 关注点 | 位置 | 现状结论 |
| --- | --- | --- |
| 上下文装配 | `src/agent/aidev_agent/core/nodes/model/context_assembly.py:70` `ContextAssembly` | 只有 tool / template / variable 三条 `MiddlewarePipeline`，中间件直接读写 `ProcessorContext`；没有「上下文条目」概念，也没有存储抽象 |
| 模板槽位 | `core/nodes/model/pydantic_models.py:42` `PromptSlots` | 仅 `system` / `human` 两个字符串槽位；装配产物是拼接后的模板，不是可检索条目 |
| 运行时上下文 | `core/nodes/model/pydantic_models.py:79` `ProcessorContext`（`:90` `store: BaseStore`） | 已有 LangGraph `store` 字段，但只在装配期由节点传入，生命周期与请求绑定；本存储**不依赖**它，避免与 checkpointer 语义耦合 |
| 配置注册 | `aidev_agent/config.py:109` `env = environs.Env()`、`:113` `existed_keys`、`:220` `settings.set_defaults` | 全部配置在 import 期解析并注册进 `settings`；`env.int`（如 `:133`）对非法值直接抛 `EnvValidationError`，**不满足** REQ-12 的「回落默认 + 告警」 |
| token 估算 | `core/nodes/model/token_compression.py:179` `_estimate_tokens` | 优先 `llm.get_num_tokens`，失败回退 `len(text) // 4`；存储层没有 LLM 句柄，沿用字符估算口径 |
| 内存存储先例 | `core/tools/runtime_tools/defer_manager.py:47` `RuntimeBackendDeferInMemoryStore` | 已有「Protocol 抽象 + 内存实现 + 锁 + daemon sweep + `atexit`」范式；本方案只复用其分层与锁风格，**不复用后台线程** |
| 测试 | `pyproject.toml:122` `asyncio_mode = "auto"`；`tests/test_config.py` | 环境变量用例通过 `runpy.run_path(config.__file__)` 重载模块断言，正好覆盖 AC-15；单测目录与源码目录对齐 |
| 规格落点 | `specs/context-offloading-storage/spec.md` | 仅规格，无 plan / tasks；本变更分支 HEAD `224d7403`，工作区干净 |

结论：仓库里没有可复用的「条目存储 + 分层卸载」实现，也没有与之冲突的同类抽象；新增独立包即可，无需改动既有模块（除 config 新增 4 个变量）。

## 方案对比

### 存储归属与承载

| 维度 | 方案 A（选定） | 方案 B | 方案 C |
| --- | --- | --- | --- |
| 做法 | 新增独立包 `aidev_agent/core/packages/context_engine/`，`ContextStore` 实例自持 `scope -> id -> entry` 两层字典 | 条目寄存在 LangGraph state / `ProcessorContext.store` | 模块级单例持有全部条目 |
| 装配链路影响 | 零（新增文件 + config 新增变量） | 需把 store 穿进 `ProcessorContext` 或图状态，等于改装配链路 | 零 |
| 会话隔离 | `scope` 一级键天然隔离，实例可跨会话共享 | 依附 thread_id，跨实例/跨进程语义受 checkpointer 约束 | 需要自行拼 scope，且全局可变 |
| 规格符合度 | 满足 REQ-11 / REQ-16 | 违反 REQ-16「零改动」 | 违反 REQ-11「禁止进程级全局可变状态作为条目唯一容器」 |
| 结论 | **采用** | 排除：零回归是硬约束 | 排除：并发与隔离都不达标 |

### 冷层承载

| 维度 | 方案 A（选定） | 方案 B |
| --- | --- | --- |
| 做法 | `ColdBackend` Protocol + `InMemoryColdBackend`（`(scope, id) -> (kind, blob)`） | 直接依赖 `langgraph.store.memory.InMemoryStore` |
| 卸载语义 | 只存序列化 blob，天然表达「payload 已外置」 | `InMemoryStore` 是 KV 文档存储，需自行约定 key 命名空间与状态 |
| 可替换性 | 换 Redis / 外置 KV 只实现 4 个方法 | 换介质需重写 langgraph store 适配 |
| 结论 | **采用**：与「冷层只承载序列化 payload」一致 | 排除：多一层框架语义，且对 AC-6 的「不可用」注入更绕 |

### 冷层写入时机（场景 2.5 的「异步同步冷层备份」）

- 已确认全同步、不开后台线程，因此「异步」降级为**写热层时同步写一份 blob 备份**：复用写入校验阶段已生成的 JSON 串，只多一次内存字典写入，不增加序列化成本。
- 卸载时把同一条冷层记录从 `backup` 翻转为 `offloaded`，回捞时翻回 `backup`；冷层容量只统计 `offloaded`，备份条目数由热层容量间接约束。
- 排除「不做备份、卸载时才写冷层」：REQ-7 明确「写入与更新路径在冷层不可用时仍成功（仅热层生效）」，隐含正常写入会触达冷层；去掉备份会让该条验收缺少可观测行为，且未来换外置后端时失去兜底副本。

### 维护动作的触发方式

- 已确认不引入后台线程，故采用**惰性节流 + 显式入口**：`write` / `update` / `invalidate` 入口按 `sweep_interval` 节流执行一次「闲置卸载 + 容量治理」；到期清理由 `end_turn` / `end_request` / `end_session` / `end_task` 显式触发；`sweep()` 供上层定时器或测试直接调用。
- 排除「每次调用都全量扫描」：写吞吐目标 ≥1000 条/秒，节流后可把扫描摊薄到秒级一次。
- 排除「读路径触发维护」：`load` 正在回捞条目，若同时卸载会把刚返回的数据再次外置，产生无意义抖动。

## 决策

### D-1 新增独立包 `aidev_agent/core/packages/context_engine/`

- 新增 `aidev_agent/core/packages/__init__.py`（空包声明）与包内 7 个模块，见「接口与数据」。
- 包内不 import 任何 `core.nodes` / `core.graphs`，只依赖标准库与 `aidev_agent.config`；保证后续接线时不会形成反向依赖。
- 本期不提供进程级默认实例：`ContextStore` 由调用方构造并持有。原因：当前无生产消费方，提前造单例会与 REQ-11 的「禁止模块级全局作为条目唯一容器」冲突。

### D-2 全同步语义，无后台线程

- 四组原语与 `sweep()`、`end_*()` 全部同步执行，不创建线程、不注册 `atexit`、不使用 `asyncio` 任务。
- 维护动作（闲置卸载、容量治理、到期清理）是普通方法，可由调用方在轮次钩子或自有定时器里调用；`sweep_interval` 只作为**惰性节流窗口**：距上次维护不足该秒数时，写/更新/失效入口跳过隐式维护。
- 代价（已知）：进程完全空闲时不会自我卸载；`load` 不触发维护。写入风险表 R-1。

### D-3 冷层协议与同步备份

- `ColdBackend` Protocol：`put(scope, entry_id, blob, kind)` / `get(scope, entry_id) -> tuple[str, str] | None` / `delete(scope, entry_id)` / `count(kind) -> int`。
- 默认 `InMemoryColdBackend` 用 `dict[(scope, entry_id)] = (kind, blob)` + `threading.Lock`；`kind ∈ {"backup", "offloaded"}`。
- `write` / `update` 成功后立即写 `backup`（best-effort）；`offload` 把记录翻为 `offloaded` 并清空热层 payload；`rehydrate` 反序列化回 payload 并翻回 `backup`；`evict` / `invalidate` 删除记录。
- 冷层异常一律捕获并记 `warning`，不向上抛：读取异常 → `LoadResult.degraded=True`；写入异常 → 写路径照常成功（REQ-7）。

### D-4 容量口径与淘汰顺序

- 热层计数 = 实例内所有 scope 中 `status=active` 且持有 payload 的条目数；冷层计数 = 所有 scope 中 `kind=offloaded` 的记录数。均为**实例级全局**，不做按会话配额（规格未要求，见 R-4）。
- 超限处理：热层 → 卸载；冷层 → 淘汰。候选排序键 `(-eviction_priority, last_accessed_at, id)`，即优先级高者先处理、同级最久未用先处理、再按 id 保证稳定。
- `never_evict=True` 的类别（本期仅 `System Instructions`）既不参与卸载也不参与淘汰；若热层被此类条目填满，记录 `warning` 并允许暂时超限（AC-11 要求其始终可加载）。
- 计数与索引在卸载/淘汰/失效时同步回收，`stats()` 立即反映（AC-16 不改变状态，仅读取）。

### D-5 注册式类别体系

- `CategorySpec`：`name`、`default_lifecycle`、`sources`（典型来源，说明性）、`eviction_priority`、`never_evict`（默认 `False`）、`token_estimator`（可选）。
- 十类内置类别以**数据表**形式在 `registry.py` 声明，核心 `store.py` 不出现任何类别名分支（REQ-8 / REQ-9）。
- `ContextStore.register_category(spec)` 委托注册表；重名抛 `DuplicateCategoryError`，未注册类别写入抛 `UnknownCategoryError`。
- `never_evict` 是对规格「默认淘汰优先级」的补充注册参数：仅靠数值无法表达「永不淘汰」，故单列布尔位，默认 `False`。

默认淘汰优先级与生命周期（高者先卸载/淘汰）：

| 类别 | 默认生命周期 | 淘汰优先级 | 可卸载 | 可淘汰 |
| --- | --- | --- | --- | --- |
| Execution Trace | 当前轮 `turn` | 100 | 是 | 是 |
| Current User Input | 当前轮 `turn` | 90 | 是 | 是 |
| Environment & Governance | 请求级 `request` | 80 | 是 | 是 |
| Working State | 任务级 `task` | 70 | 是 | 是 |
| Conversation History | 会话级 `session` | 60 | 是 | 是 |
| Skill Context | Agent 级 `agent` | 50 | 是 | 是 |
| User Context | 用户级 `user` | 40 | 是 | 是 |
| Tool Definitions | Agent 级 `agent` | 30 | 是 | 是 |
| Agent Definition | Agent 级 `agent` | 20 | 是 | 是 |
| System Instructions | 平台级 `platform` | 0 | 否 | 否 |

子上下文以 `sub_context` + `labels` 表达，不为每个子上下文建类别。内置标签约定：`Working State` → `task_context` / `planning_context` / `workflow_context` / `working_state_context` / `artifact_context`；`Environment & Governance` → `multi_agent_context` / `resource_context` / `event_context` / `environment_context` / `human_feedback_context` / `evaluation_context`。约定只用于夹具与文档，注册表不校验标签取值。

### D-6 写入校验与零副作用

- 校验顺序：`scope` 非空 → 类别已注册 → `source` 非空 → `lifecycle` 为合法枚举（缺省取该类别默认值）→ `labels` 均为字符串 → `json.dumps(payload, ensure_ascii=False)` 成功。
- 可序列化判据用 JSON 而非 pickle：冷层存的就是 JSON 文本，且 `ensure_ascii=False` 与 SDK 既有中文负载一致；`json.dumps` 抛 `TypeError` / `ValueError` 统一转 `PayloadSerializationError`。
- 校验全部通过后才构造条目并插入，任何失败点都不触碰 `_entries`、索引与计数器（AC-2 的「计数不变」由此天然成立）。

### D-7 并发安全

- 单个 `threading.RLock` 保护 `_entries`、计数器、`_last_sweep`；冷层用自己的锁，两者不嵌套获取（先热层后冷层的顺序固定，避免死锁）。
- 四组原语是同步短临界区，不 `await` 任何东西；asyncio 协程调用时等价于串行执行，满足 REQ-11 的「协程安全」。
- 版本冲突在锁内比较后抛出，条目不动（AC-9）。

### D-8 时间与可测试性

- 构造参数 `clock: Callable[[], datetime]`，默认 `lambda: datetime.now(timezone.utc)`；闲置判定、到期、时间戳、`_last_sweep` 全部走该时钟。
- 测试注入可控时钟后可用 `sweep()` 直接驱动，不依赖 `sleep`（规格「可测试性」条）。

### D-9 到期清理与引用延期

- 四个入口 `end_turn` / `end_request` / `end_session` / `end_task` 分别处理 `turn` / `request` / `session` / `task` 生命周期；`platform` / `agent` / `user` 无自动到期入口。
- 处理规则：`ref_count > 0` → 延期（不卸载不淘汰，计入报告 `deferred`）；`never_evict` → 跳过；`eviction_priority >= 80` → 直接淘汰；其余 → 卸载保留。
- 单条异常：捕获 + `warning` 日志 + 继续下一条；连续失败计数达 3 次输出 `error` 级告警并在下次成功后清零（REQ-10）。
- 到期阈值 80 是常量 `EXPIRY_EVICT_PRIORITY`，落在 `policies.py`，不散落在 `store.py`。

### D-10 配置在 `config.py` 内安全解析

- 在 `config.py` 的 `env = environs.Env()` 之后、`existed_keys` 快照之前新增私有助手 `_env_positive_int(name, default)`：读 `os.environ`，未设置或空串取默认；非整数或 `< 1` 记 `warning` 并回落默认；**不抛异常**。
- 4 个新常量放在既有 `BKAI_*` 配置段（`config.py:152` 附近），随 `settings.set_defaults` 注册，命名沿用 `BKAI_` 前缀：

| 变量 | 含义 | 默认 | 非法处理 |
| --- | --- | --- | --- |
| `BKAI_CONTEXT_STORE_HOT_CAPACITY` | 热层容量上限（条目数） | 1000 | 回落 1000 + warning |
| `BKAI_CONTEXT_STORE_COLD_CAPACITY` | 冷层容量上限（offloaded 条目数） | 5000 | 回落 5000 + warning |
| `BKAI_CONTEXT_STORE_IDLE_SECONDS` | 闲置卸载阈值（秒） | 900 | 回落 900 + warning |
| `BKAI_CONTEXT_STORE_SWEEP_INTERVAL` | 维护节流周期（秒） | 60 | 回落 60 + warning |

- `StoreConfig.from_settings()` 在 `ContextStore.__init__` 时读取一次（等价「随进程启动生效、不支持热更新」）；`StoreConfig(...)` 可直接构造以覆盖默认值，测试用它而不改动环境变量；AC-15 的「模块导入与初始化成功」由 `runpy.run_path(config.__file__)` + `ContextStore(StoreConfig.from_settings())` 断言。
- 未用 `env.int` 的原因：它在 import 期对非法值抛异常，会把配置错误升级为进程启动失败，与 REQ-12 直接冲突。这里保留「在 `config.py` 注册 + `settings` 可见」的既有形态，只替换解析实现。

### D-11 可观测

- `stats()` 返回不可变快照：热层条目数、`offloaded` 数、`backup` 数、按类别条目数、卸载/回捞/淘汰次数、token 估算总量；不修改任何状态、不触碰冷层记录内容（AC-16）。
- 日志统一 `logger = logging.getLogger(__name__)`，前缀 `[ContextOffloading]`，覆盖卸载、回捞、淘汰、降级、到期清理，字段含条目 id、类别、原因（REQ-14）。
- token 估算：条目写入/更新时用类别 `token_estimator`（缺省 `max(1, len(blob) // 4)`）算一次并随元数据保存，`stats()` 只做求和，避免每次统计重新序列化。

## 接口与数据

### 新增模块

| 文件 | 职责 |
| --- | --- |
| `aidev_agent/core/packages/__init__.py` | 包声明 |
| `aidev_agent/core/packages/context_engine/__init__.py` | 导出 `ContextStore`、`StoreConfig`、`ContextEntry`、`EntryFilter`、`Lifecycle`、`EntryStatus`、`CategorySpec`、`CategoryRegistry`、`ColdBackend`、`InMemoryColdBackend`、异常类 |
| `context_engine/models.py` | `Lifecycle`、`EntryStatus`、`ContextEntry`、`EntryFilter`、`LoadResult`、`MaintenanceReport`、`StoreStats` |
| `context_engine/errors.py` | 异常层次 |
| `context_engine/registry.py` | `CategorySpec`、`CategoryRegistry`、十类内置类别表 `build_builtin_registry()` |
| `context_engine/cold.py` | `ColdBackend` Protocol、`InMemoryColdBackend` |
| `context_engine/policies.py` | `EXPIRY_EVICT_PRIORITY`、候选排序键、淘汰候选筛选、到期动作判定（纯函数） |
| `context_engine/settings.py` | `StoreConfig`（含 `from_settings()`） |
| `context_engine/store.py` | `ContextStore`：四组原语 + `retain/release` + `end_*` + `sweep` + `stats` |

### 修改点

| 文件 | 改动 |
| --- | --- |
| `aidev_agent/config.py` | 新增 `_env_positive_int` 助手（`env` 声明后、`existed_keys` 前）与 4 个 `BKAI_CONTEXT_STORE_*` 常量 |

除此之外不改动任何既有文件；`ContextAssembly`、`PromptSlots`、`PromptMiddleware` 系列、`chat_history_assembly`、`token_compression` 零改动。

### 数据模型

`ContextEntry`（dataclass）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `str` | `uuid4().hex`，存储分配 |
| `category` | `str` | 已注册类别名 |
| `sub_context` | `str` | 子上下文标识，默认 `""` |
| `labels` | `tuple[str, ...]` | 标签，默认 `()` |
| `lifecycle` | `Lifecycle` | 七值枚举：`platform` / `agent` / `user` / `session` / `turn` / `task` / `request` |
| `source` | `str` | 来源标识，非空 |
| `payload` | `Any` | 原始数据；`offloaded` / `evicted` 时为 `None` |
| `status` | `EntryStatus` | `active` / `offloaded` / `evicted` |
| `version` | `int` | 写入从 1 起，每次更新 +1 |
| `created_at` / `updated_at` / `last_accessed_at` | `datetime` | UTC，来自注入时钟 |
| `token_estimate` | `int` | 内部元数据，供 `stats()` 求和 |
| `ref_count` | `int` | 内部元数据，`retain`/`release` 维护，`>0` 时到期延期 |

`EntryFilter`：`categories`、`lifecycles`、`labels`、`sources`（均可为空的元组）。**维度内为「或」、维度间为「与」**；`labels` 命中条件为与条目标签集合交集非空；`include_offloaded`（默认 `True`）为 `False` 时跳过 `offloaded` 条目。已 `evicted` 的条目永不返回。

### 关键签名

```python
class ContextStore:
    def __init__(
        self,
        *,
        config: StoreConfig | None = None,
        registry: CategoryRegistry | None = None,
        cold: ColdBackend | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None: ...

    def register_category(self, spec: CategorySpec) -> None: ...

    def write(
        self,
        *,
        scope: str,
        category: str,
        payload: Any,
        source: str,
        lifecycle: Lifecycle | None = None,
        sub_context: str = "",
        labels: Sequence[str] = (),
    ) -> ContextEntry: ...

    def update(self, *, scope: str, entry_id: str, version: int, payload: Any) -> ContextEntry: ...

    def invalidate(
        self,
        *,
        scope: str,
        entry_ids: Sequence[str] | None = None,
        entry_filter: EntryFilter | None = None,
    ) -> int: ...

    def load(self, *, scope: str, entry_filter: EntryFilter | None = None) -> LoadResult: ...

    def retain(self, *, scope: str, entry_ids: Sequence[str]) -> None: ...
    def release(self, *, scope: str, entry_ids: Sequence[str]) -> None: ...

    def end_turn(self, *, scope: str) -> MaintenanceReport: ...
    def end_request(self, *, scope: str) -> MaintenanceReport: ...
    def end_session(self, *, scope: str) -> MaintenanceReport: ...
    def end_task(self, *, scope: str) -> MaintenanceReport: ...

    def sweep(self, *, scope: str | None = None) -> MaintenanceReport: ...
    def stats(self, *, scope: str | None = None) -> StoreStats: ...
```

`LoadResult`：`entries: tuple[ContextEntry, ...]`、`degraded: bool`、`skipped_ids: tuple[str, ...]`。`MaintenanceReport`：`offloaded / evicted / expired / deferred / failed` 五个计数。

### 语义约定

| 操作 | 语义 |
| --- | --- |
| `write` | 校验 → 分配 id / `version=1` → 入热层 → 写冷层备份（best-effort）→ 节流维护 |
| `update` | `id` 不存在或已失效 → `EntryNotFoundError`；`version` 不一致 → `VersionConflictError(current_version)`；一致则回捞（若已卸载）→ 替换 payload → `version+1` → 刷新 `updated_at` / `last_accessed_at` → 重算 token → 重写备份 |
| `invalidate` | 按 `id` 或按过滤器；两者都缺 → `EntryValidationError`；命中且未失效的条目置 `evicted`、清 payload、删冷层记录；返回**本次新失效**条数（重复调用返回 0，幂等） |
| `load` | 遍历该 scope 条目 → 过滤 → `offloaded` 逐条回捞（失败跳过并置 `degraded`）→ 刷新命中条目 `last_accessed_at` → 返回元数据 + payload；命中为空返回空结果而非错误 |
| `retain` / `release` | 调整 `ref_count`（下限 0）；不影响其它字段与状态 |
| `sweep` | 闲置卸载（`now - last_accessed_at >= idle_seconds`）→ 热层容量治理 → 冷层容量治理；返回报告 |
| `stats` | 只读快照；不触发回捞、不修改 `last_accessed_at` |

### 异常

| 异常 | 触发 |
| --- | --- |
| `ContextStoreError` | 基类 |
| `UnknownCategoryError` | 写入/过滤使用未注册类别 |
| `DuplicateCategoryError` | 注册重名类别（AC-8） |
| `EntryValidationError` | 必填字段缺失、`scope`/`source` 为空、`lifecycle` 非法、`labels` 非字符串、`invalidate` 未给条件 |
| `PayloadSerializationError` | payload 无法 JSON 序列化（AC-3） |
| `VersionConflictError` | 更新版本不一致，携带 `current_version`（AC-9） |
| `EntryNotFoundError` | 更新不存在或已失效的条目 |

### 冷层协议

```python
class ColdBackend(Protocol):
    def put(self, scope: str, entry_id: str, blob: str, kind: str) -> None: ...
    def get(self, scope: str, entry_id: str) -> tuple[str, str] | None: ...
    def delete(self, scope: str, entry_id: str) -> None: ...
    def count(self, kind: str | None = None) -> int: ...
```

## 跨仓契约

- 单仓变更（`bk-aidev-agent`），无跨仓接口、无跨仓执行顺序、无平台侧配合，与规格「跨仓边界」一致。
- 与「Handoff：替换当前上下文的压缩方案」：本期不定义其写入契约；`write` 对任意已注册类别通用，Handoff 后续只需选定类别/标签即可接入。
- 与「工具的延迟加载」「工具加载模式」「工具 PTC」：不读取、不修改 `tool_lazy_loading` 相关代码，不定义工具定义/工具结果的写入形态。
- 与现有装配链路：零改动、零调用插入；`ContextStore` 在后续变更中由装配层持有并注入。

## 风险

| 编号 | 风险 | 处置 |
| --- | --- | --- |
| R-1 | 无后台线程：进程纯空闲时不会自动卸载，`load` 也不触发维护；REQ-6「超过闲置阈值必须被卸载」在无后续调用的场景下依赖上层显式 `sweep()` | 已由用户确认全同步（D-2）；`sweep()` + `sweep_interval` 提供可挂载的驱动点；风险写入交付说明，后续接线时由轮次钩子或上层定时器调用 |
| R-2 | 冷层同步备份使内存约为热层 payload + 冷层 blob 两份 | 复用写入校验阶段的 JSON 串，无额外序列化；冷层容量只计 `offloaded`，备份量受热层容量约束；换外置后端后备份即恢复其原始价值 |
| R-3 | 场景 2.5 写「异步同步冷层备份」，本方案为同步写入 | 与 D-2 一致，属已确认取舍；语义（写入后冷层有副本）不变，仅去掉异步性 |
| R-4 | 容量为实例级全局，多会话共享同一实例时互相挤占（先写入的会话可能被后写入的会话挤掉热层） | 规格未定义按会话配额，本期不引入；隔离只保证可见性（AC-13），不保证配额 |
| R-5 | `System Instructions` 不卸载也不淘汰，若其条目数本身超过热层容量，热层会暂时超限 | 记录 warning 并允许超限；AC-11 要求其始终可加载，正确性优先于容量硬约束 |
| R-6 | `invalidate` / 淘汰会物理删除冷层 blob，无回溯能力 | 与 REQ-4「逻辑删除、物理数据可删」一致；后续如需审计另立变更 |
| R-7 | 过滤为内存全扫描 O(n)，未建二级索引 | 千级条目规模满足 P95 < 50ms 参考目标；避免为未验证的规模过度设计，实测值记入交付说明 |
| R-8 | `make test` 依赖 `uv sync` 后的环境，新 worktree 无预置 `.venv` | 实现前先执行 `make init`；验证命令沿用仓库 `Makefile` 约定，不直接调 `pytest` |
| R-9 | 兄弟需求「接口约定」章节仍缺失，后续可能要求新增写入字段 | 接口按「任意已注册类别通用」设计，不预埋 Handoff / 工具专属字段；补齐后另立变更 |
| R-10 | `config.py` 新增私有助手若放在 `existed_keys` 之后，会被 `settings.set_defaults` 误注册成配置项 | 明确放在 `env` 声明之后、`existed_keys` 快照之前；单测断言 `settings` 中不出现该助手名 |

## 测试策略

新增用例目录 `src/agent/tests/core/packages/context_engine/`（与源码目录对齐，含两层 `__init__.py`；`tests/core` 本身无 `__init__.py`，沿用 `tests/core/nodes` 的做法）。共同夹具放 `conftest.py`：可拨动时钟、`make_store(**overrides)`、十类样例数据工厂。

| 文件 | 覆盖 |
| --- | --- |
| `test_registry.py` | 十类内置注册与优先级表、重名拒绝（AC-8）、未注册类别拒绝（AC-7 前置） |
| `test_write_path.py` | 必填字段/非法枚举/不可序列化拒绝且零副作用（AC-2 / AC-3）、版本递增与冲突（AC-9） |
| `test_invalidate.py` | 按 id / 过滤器失效、幂等（AC-12）、不影响未命中条目 |
| `test_load_filter.py` | 多维度过滤「维度内或、维度间与」（AC-4）、空结果非错误、`evicted` 不返回、跨 scope 隔离（AC-13） |
| `test_tiering.py` | 闲置卸载边界（AC-5）、回捞透明与 `last_accessed_at` 刷新、冷层不可用降级（AC-6）、淘汰顺序 + `System Instructions` 永不淘汰（AC-11）、冷层容量淘汰 |
| `test_expiry.py` | 轮次结束清理 `Execution Trace`（AC-10）、被引用条目延期、平台/Agent/用户级不自动到期、单条失败隔离与连续失败告警 |
| `test_observability.py` | `stats()` 字段齐全且只读（AC-16）、降级/卸载/回捞/淘汰日志含 id 与类别 |
| `test_settings.py` | 默认值、非法值（`0` / `-1` / `abc`）回落 + warning、`runpy.run_path(config.__file__)` 下导入成功（AC-15）、助手未被注册为配置项 |
| `test_concurrency.py` | 多线程 + asyncio 并发写入 N 条全部可加载且无未捕获异常（AC-14） |
| `test_builtin_categories_e2e.py` | 十类各写入一条（Working State / Environment & Governance 各 ≥2 个标签）→ 过滤加载 → 卸载 → 回捞 → 到期清理（AC-1），新注册类别跑通同一链路（AC-7） |

验证命令（仓库根目录，执行前先 `make init`）：

```bash
make test path=src/agent/tests/core/packages/context_engine/
make test path=src/agent/tests/test_config.py
# AC-17 零回归
make test path=src/agent/tests/core/nodes/model
make test path=src/agent/tests/core/nodes/test_context_processor_pipeline.py
make test path=src/agent/tests/core/nodes/test_context_processor_template_middleware.py
make test path=src/agent/tests/core/nodes/test_context_processor_variables_middleware.py
make test path=src/agent/tests/core/nodes/test_token_compression.py
make test path=src/agent/tests/core/nodes/test_model.py
```

性能目标（P95 < 50ms / 回捞 < 500ms / 写入 ≥ 1000 条/秒）为参考值，不设失败阈值；由 `test_concurrency.py` 顺带打印实测耗时写入交付说明，不做断言。
