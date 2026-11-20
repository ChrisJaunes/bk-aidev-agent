# ACP 接入指南

本包提供 ACP v1（JSON-RPC over stdio），将 AG-UI 事件翻译为 session/update。

## 1. 启动与资源

```bash
python -u -m aidev_agent_cli --protocol acp --resources platform
```

资源和协议正交。省略 --resources 时三个协议都使用 default 预设；显式 injected 也可以配合 acp，不需要平台 token。含 platform 的 ACP 启动必须有身份凭据。stdout 只承载协议，诊断走 stderr / 日志文件。

安装：pip install "aidev-agent-cli[checkpoint,acp]"。源码部署时 PYTHONPATH 指向插件根；wheel 部署无需设置。

## 2. 身份规则

含 platform 时按环境变量 BKAI_TUI_ACCESS_TOKEN、用户配置 auth.access_token 的顺序解析 token。两者都没有时在连接绑定前非零退出，不污染 stdout。token 不接受 argv，不进入日志或响应。

ACP 启动取得的身份只在内存生效，不覆盖交互式登录态。RuntimeService 在接纳时捕获 IdentitySnapshot；已接纳轮次不因后续登录改变身份，下一次接纳使用新的 revision。Session 不订阅身份变化，也不缓存资源 manager。

## 3. 能力

- initialize：protocolVersion=1，最小能力集和 _meta.aidev 扩展声明。
- session/new：返回 sessionId；_meta.aidev 接受 agent / llm / username / hooks。
- session/prompt：文本轮次；事件通过 session/update 发送。
- session/cancel：只取消目标会话，未知或已结束会话是 no-op。
- _aidev/resume：中断恢复；响应带 status / turnId / interrupts 元数据。
- _aidev/hook 与 _aidev/hook_degraded：宿主裁决往返与降级通知。

不宣告 loadSession、多媒体输入、客户端 MCP、ACP 文件服务或独立模型列表。审批/问答中断以文本和元数据报告，不把中断当成功完成；ITSM 审批不能通过宿主恢复绕过。

扩展字段和裁决形状见 acp-dsh.md。普通续轮由 adapter 接到 start(ChatInput)；核心 resume 只接 ResumeInterrupt。

## 4. 工作区

session/new 的 cwd 仅作协议诊断，不是会话工作区，不进入 SessionOptions，也不改进程环境。工作区取进程实际 cwd，在接纳时快照给 worker；本地 filesystem backend 强制 virtual_mode=True，不能用绝对路径或 .. 逃逸根目录。

## 5. 执行与关闭

每轮独立 sink、解释器和 hook transport；不同会话可并发，同会话 ACP busy 时拒绝新轮。真实 SDK 构建与 execute 都在 spawned ProcessWorker，不使用线程 fallback。

活动取消先协作等待，再 terminate/kill + join；进程退出后才释放槽位并允许下一轮。强杀不能撤销已经提交的外部副作用。异常或 RUN_ERROR 必须映射为 JSON-RPC error，不得返回虚假的 end_turn。

EOF 先在有界窗口内等待协议响应写出，随后 RuntimeService.close_all 进入 closing gate、取消所有 worker，由 worker 关闭其存储后退出；父进程确认 join，最后关闭协议输出。并发 shutdown 调用共享同一次 shielded 收尾。

## 6. 内部 API 迁移

RuntimeService 是 adapter 唯一运行入口，new_session/start/resume/prepare/资源查询/cancel/close/close_all 均为异步。SessionRegistry 存具体 Session，session_info 返回不可变信息投影；没有 public sessions/runs manager、Dispatcher.state、旧同步 Session 执行或传输参数。

SDK 接入集中到 runtime.sdk 包（factory、session、events、hooks、interrupts、workspace、checkpoints/checkpointer），旧模块路径不提供转发 shim。测试迁移签名见 .planning/CONTINUATION.md。
