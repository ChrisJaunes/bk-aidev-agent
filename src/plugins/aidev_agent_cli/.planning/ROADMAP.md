# Roadmap: Runtime

- [x] **Phase 1: 可强终止进程边界** — spawn IPC、cancel/terminate/kill/join、脚本worker验收。
- [x] **Phase 2: 应用模型与调度** — Session/Run/Registry、RuntimeService gate、typed输入与FIFO。
- [x] **Phase 3: SDK与持久事实** — SDK worker装配、历史/checkpoint、hook、identity/config。
- [x] **Phase 4: 协议迁移与清理** — 三 adapter/resources/entry/tests/docs 已迁移，旧策略和传输耦合已移除。
- [x] **Phase 5: 全套验收审计** — 独立审查发现的收尾及恢复组合缺陷已修复，新增回归覆盖；最终全套 734 passed，Ruff/compileall/diff 检查通过。

## Phase Details
### Phase 1: 可强终止进程边界
Goal: 构建执行卡死可真实终止，退出后继续运行。
Requirements: PROC-01
Success Criteria: spawn/受控IPC；cancel合作后terminate/kill/join；无Task.cancel假硬停；无子进程泄漏。
Plans: 1 plan
### Phase 2: 应用模型与调度
Depends on: Phase 1
Requirements: CORE-01, CORE-02, SCHED-01, INPUT-01
### Phase 3: SDK与持久事实
Depends on: Phase 2
Requirements: PROC-02, SDK-01, DATA-01, DATA-02, CONFIG-01
### Phase 4: 协议迁移与清理
Depends on: Phase 3
Requirements: ADAPT-01
### Phase 5: 全套验收审计
Depends on: Phase 4
Requirements: VERIFY-01
## Actual production completion (2026-11-09)

生产范围全部实现并自审：Session/Run 边界、具体 Registry、typed inputs、active+queue FIFO/completed bound、唯一 RuntimeService closing gate、身份/环境 revision 快照、SDK 集中包真实 writer、worker 自持存储与强杀 join、三 adapter/resource/entry 收敛。主会话最终独立验收 734 passed, 8 warnings；完整证据见 STATE.md。五阶段实现和测试迁移完成，未推送。
