# Requirements: Runtime

## v1 Requirements
- [x] **PROC-01**: spawn构建/execute进程隔离；合作取消后terminate/kill/join；受控事件/请求回复IPC，无泄漏
- [x] **CORE-01**: RuntimeService唯一异步入口、RUNNING/CLOSING/CLOSED gate与bootstrap
- [x] **CORE-02**: Session跨轮状态/Run运行快照/具体SessionRegistry；删除重复状态与策略
- [x] **SCHED-01**: active+FIFO reject/enqueue；queued cancel不构建；phase、有界完成缓存、shield共享future
- [x] **INPUT-01**: typed ChatInput/ContextInput/ResumeInterrupt；执行中拒配置更新
- [x] **PROC-02**: 序列化工具工厂；worker独立SQLite/SDK/客户端，不线程fallback
- [x] **SDK-01**: runtime/sdk集中装配、BaseSessionWriter身份、AGUI事件与唯一终态finalizer
- [x] **DATA-01**: 独立SQLite历史表/平台HistoryStore；失败取消轮次状态；不伪装checkpoint原子性
- [x] **DATA-02**: 持久checkpoint所属session/reason/tool匹配；重启memo/continuation；owned/borrowed生命周期
- [x] **CONFIG-01**: IdentitySnapshot与ProcessConfig/SettingsStore分离，捕获cwd/virtual_mode安全边界
- [x] **ADAPT-01**: 三adapter/resources/entry生产迁移统一sink/hook、每Run bridge和旧docs清理已完成；tests迁移完成，完整门禁通过
- [x] **VERIFY-01**: 两会话/3轮FIFO/queued cancel/构建执行卡死kill后成功/关闭无泄漏/future超时/closing拒新/RUN_ERROR/唯一终态/重启恢复/存储关闭顺序/默认解析/import/三协议及全套测试

## Traceability
| Requirement | Phase | Status |
|---|---|---|
| PROC-01 | Phase 1 | Production verified |
| CORE-01 | Phase 2 | Production verified |
| CORE-02 | Phase 2 | Production verified |
| SCHED-01 | Phase 2 | Production verified |
| INPUT-01 | Phase 2 | Production verified |
| PROC-02 | Phase 3 | Production verified |
| SDK-01 | Phase 3 | Production verified |
| DATA-01 | Phase 3 | Production verified |
| DATA-02 | Phase 3 | Production verified |
| CONFIG-01 | Phase 3 | Production verified |
| ADAPT-01 | Phase 4 | Complete; full suite passed |
| VERIFY-01 | Phase 5 | Complete; independent full suite 734 passed |

Coverage: 12 mapped, 0 unmapped.
Actual evidence and scope are recorded in STATE.md. All requirements have passed the migrated full suite (734 passed, 8 warnings), Ruff, compileall and diff checks. Independent review findings have regression coverage. Final implementation verified and ready for local commit; no push.