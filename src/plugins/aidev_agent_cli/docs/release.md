# 发布记录

## 未发布：生产 runtime 进程隔离重构

- 三个 adapter（acp-dsh / agui / acp）统一经 RuntimeService 接纳，不再直接访问会话或调度管理器。
- Session 只保存跨轮配置、revision、历史、pending 与轮次。每个 Run 独立拥有快照、事件 sink、hook transport、取消信号和 ProcessWorker。
- 调度使用显式 active + FIFO queue；排队取消不启动 worker。completed cache 全进程最多 128 个句柄，不因下一次 start 清空。
- 真实 SDK 构建和 execute 都位于 spawned worker；协作取消后 terminate/kill + join，确认进程退出才释放会话槽位。不使用线程 fallback 或 Task.cancel 冒充硬停。
- SDK 接入集中在 runtime/sdk 包：factory、worker-local session、真实 BaseSessionWriter / AGUISessionWriter、hook 领域规则、中断恢复、workspace agent 与 checkpoint/saver。旧 SDK 模块路径不保留 shim。
- 核心不包含 NDJSON 行编码、默认事件/hook 编码、HookChannel/HookPendingRouter 或 HeadlessError。协议翻译留在 adapter。
- start 接纳 ChatInput / ContextInput；resume 只接纳 ResumeInterrupt。普通续轮调用 start。旧 executeKwargs.resume 在线上 AGUI adapter 归一为显式恢复请求。
- IdentitySnapshot 和 SettingsSnapshot 在接纳时固定 revision；Session 不订阅身份、不持有资源 manager。资源构造使用具体 ResourceOverrides，不反注入 Session。
- 本地历史、pending、hook memo 与 checkpoint 持久化；SQLite 连接由 worker 打开关闭。强杀不能撤销已经提交的外部副作用，也不声称历史和 checkpoint 跨库原子提交。
- close / close_all 使用共享 shielded cleanup task；closing 状态拒绝新接纳，worker 在退出前关闭自身存储，父进程确认全部 join 后才完成 runtime 收尾。并发关闭调用等待同一次收尾。
- 工作区固定为进程 cwd 的接纳快照；worker 注册 local filesystem backend 时强制 virtual_mode=True。
- 默认资源对三个协议一致（default 预设）；显式 injected 不要求平台 token，含 platform 的 ACP 启动时解析内存身份。

内部 API 和测试迁移说明见 .planning/CONTINUATION.md；协议文档见 acp-dsh.md、acp-integration.md。验证记录以 .planning/STATE.md 的实际运行结果为准，不将旧测试数量当作本次通过证据。

## 0.1.0

初始化 CLI 插件打包配置及 stdio 入口。旧入口不提供兼容 shim。
