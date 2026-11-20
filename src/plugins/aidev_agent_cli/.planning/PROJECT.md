# Runtime 进程隔离重构

## What This Is
已有Python CLI专用重构里程碑。用户已批准全部设计，授权自主推进，不重复采访。

## Core Value
卡死构建/执行必须真实进程强制终止并join后继续下一轮。

## Requirements
### Validated
现有三协议与同步生成器SDK存在，重构验收尚未完成。
### Active
RuntimeService唯一异步入口；Session/Run隔离；spawn进程边界；FIFO调度；typed输入；SDK集中接入；历史与checkpoint持久恢复；identity/config快照；三个adapter迁移与全套验收。
### Out of Scope
旧接口兼容、push、共享系统变更与无关修改。

## Context
初始HEAD c9100bc9d，工作树干净。GSD init误报has_git=false，实际git根在上级，不创建嵌套仓库。SDK execute是同步生成器；stop仅request_cancel/release_resources；历史预处理要求BaseSessionWriter。

## Constraints
Python3.11；spawn与显式IPC；worker自开关SQLite/客户端/loop；禁止线程fallback；强杀不能撤销外部副作用。

## Key Decisions
| Decision | Rationale | Outcome |
|---|---|---|
|先验证worker再接调度SDK协议|强终止是结构性要求|Pending|
|设计自动批准|本轮明确授权|Accepted|

## Evolution
每阶段按真实代码测试更新状态，最终审计后归档。
