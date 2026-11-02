# 代码评审

## 最新结论

- 结论: 通过，但有改进建议
- 评审版本: f8c4594a666aac857849f362195b1d5d9a7fb1dc
- 变更名: tool-ptc-code-mode（`specs/tool-ptc-code-mode/`）
- 轮次: 第 1 轮

### 阻塞项

无。REQ-1 ~ REQ-13 在代码与用例上均可对上，ACC-1 ~ ACC-14、ACC-16 ~ ACC-19 有可离线判定
的证据；ACC-15 的 E2B / paas 真实端到端按规格「测试约束」记为未取证（缺环境），
不构成阻塞。定向验证重跑全绿，未发现正确性、安全或数据方面的阻塞项。

### 改进建议

以下均为一般改进建议，不构成返工理由。

- W-1 `CodeModeBridge._list_requests` 在事件循环线程上直接调用同步 `ls_info`
  （`core/tools/code_mode/bridge.py:247`、`:288`），而同处的 `download_files` / `upload_files`
  都走了 `asyncio.to_thread`。轮询间隔 0.1s，在 e2b / paas 这类远端后端上等于把事件循环
  按轮询节奏持续占住，单次执行最长 120s。建议同样交给 `to_thread`。
- W-2 `PaasSandboxBackend.scratch_dir` 返回字面量 `$STORAGE_PATH/.aidev_code_mode/<name>`
  （`runtime_tools/paas_backend.py:884-886`），`upload_files` / `download_files` / `ls_info`
  会经 `_resolve_path` 展开，但 `execute` 是把整串拼进 `bash -c`（`bridge.py:232`）依赖沙箱
  shell 里存在同名环境变量。两条路径口径不一致，建议 `scratch_dir` 直接返回展开后的绝对路径。
- W-3 `test_chat_x_has_no_diff`（`tests/core/graphs/react/test_code_mode_contract.py:54-66`）
  比对的是「工作区 vs HEAD」，不是「本分支 vs 基线」；改动即使真的碰了前端，只要已提交就不会
  失败。本次评审改为人工核对 `git diff 29487419..HEAD -- src/frontend` 为空，结论成立。
  建议把断言改成与基线比较。
- W-4 ACC-1 / ACC-2 要求的「与基线逐字节一致的事件序列」没有 golden 事件比对用例，
  现以「`function_call` 分支代码零改动 + 工具名集合一致」间接保证（`test_code_mode_switch.py:56-63`）。
  建议补一条固定 fake 模型输入下的事件序列快照比对。
- W-5 缺少整图集成用例：现有用例都直接 `tool.ainvoke({"code": ...})`，没有覆盖
  「fake 模型 → 模型节点 `bind_tools` → ToolNode 注入 `InjectedState` → `code_execute`」这条
  真实路径。装配层与工具层分别有断言，但两者衔接处（`state` 注入、`ToolMessage` 回填）无覆盖。
- W-6 `CodeModeSession.lock` 惰性创建只避免了构造期绑定（`core/tools/code_mode/session.py:36-40`），
  若同一已构建图在不同事件循环里复用（例如多次同步 invoke），第二次会命中
  「bound to a different event loop」。建议按运行循环存放锁，或改用不绑定循环的实现。
- W-7 远端后端的执行目录是硬编码假设：e2b 固定 `/tmp/.aidev_code_mode`、paas 固定
  `$STORAGE_PATH`。`ensure_code_mode_capable` 只检查方法是否被重写，不检查目录可写；
  目录不可用时只能落到执行期 `infra_error`。建议装配期做一次目录探测或在文档标注该前提。
- W-8 `core/tools/code_mode/errors.py:21` 的 `FAILURE_STATUSES` 定义后未被使用（失败判定
  走的是 `status != "ok"`），建议删除或在 `tool.py` 里真正用它分类。

<details>
<summary>自动校验信息</summary>

```spec_verdict
{
  "verdict": "PASS_WITH_WARNING",
  "change_name": "tool-ptc-code-mode",
  "iteration": 1,
  "reviewed_sha": "f8c4594a666aac857849f362195b1d5d9a7fb1dc",
  "blocking": [],
  "warnings": [
    {"id": "W-1", "evidence": "core/tools/code_mode/bridge.py:247/288 在事件循环线程同步调用 ls_info，其余 IO 走了 to_thread", "suggestion": "ls_info 也放进 asyncio.to_thread，避免远端后端下阻塞事件循环"},
    {"id": "W-2", "evidence": "runtime_tools/paas_backend.py:884-886 返回字面量 $STORAGE_PATH 路径，execute 命令依赖沙箱 shell 展开", "suggestion": "scratch_dir 返回展开后的绝对路径，与 _resolve_path 口径统一"},
    {"id": "W-3", "evidence": "tests/core/graphs/react/test_code_mode_contract.py:54-66 只比对工作区与 HEAD", "suggestion": "改为与基线 commit 比较前端目录 diff"},
    {"id": "W-4", "evidence": "test_code_mode_switch.py:56-63 仅比较工具名集合，无事件序列快照", "suggestion": "补一条固定 fake 模型下的事件序列 golden 比对"},
    {"id": "W-5", "evidence": "code mode 相关用例均为直接 tool.ainvoke，无模型节点 + ToolNode 的整图用例", "suggestion": "补一条 fake 模型驱动的整图 code mode 用例"},
    {"id": "W-6", "evidence": "core/tools/code_mode/session.py:36-40 惰性锁仍绑定首次使用的事件循环", "suggestion": "按循环保存锁，或改用不绑定循环的实现"},
    {"id": "W-7", "evidence": "e2b 固定 /tmp、paas 固定 $STORAGE_PATH，能力探测不校验目录可写", "suggestion": "装配期探测目录可写，或在文档标注该前提"},
    {"id": "W-8", "evidence": "core/tools/code_mode/errors.py:21 FAILURE_STATUSES 未被引用", "suggestion": "删除或在使用处接入该集合"}
  ]
}
```

</details>

## 第 1 轮

### 评审范围

- 仓库：`git@github.com:ChrisJaunes/bk-aidev-agent.git`，单仓。
- 工作分支：`feature/tool-ptc-code-mode`（评审开始与结束两次 `ls-remote` 一致，
  远端 SHA = 本地 HEAD = `f8c4594a666aac857849f362195b1d5d9a7fb1dc`）。
- 基线：`origin/develop` = `29487419ff821900fa209d195b7ca947688cf5f2`。
- 累计差异：`29487419..f8c4594a`，34 个文件 / +3460 −5；`specs/tool-ptc-code-mode/{spec,plan,tasks}.md`
  与已审批附件逐字节一致（`diff` 无输出），未发现实现阶段改写冻结文档。
- 前端 `src/frontend/ai-blueking/packages/chat-x` 相对基线零 diff。

### 需求核对

| 需求 | 落地位置 | 判定 |
| :--- | :--- | :--- |
| REQ-1 开关与默认值 | `pydantic_models.py:63-69`（`Literal` + 默认 `function_call`）、`graphs/react/code_mode.py:39-45`（装配期兜底校验） | 满足 |
| REQ-2 默认路径零回归 | `graph.py:989-995` 仅 code_mode 分支改动；`function_call` 原样返回 `assembled` | 满足 |
| REQ-3 模型可见工具面 | `graph.py:997-1053`（只返回 `code_execute`）、`manifest.py:194-234`（清单）、`tool.py:26-32`（无审批元数据） | 满足 |
| REQ-4 代码环境工具函数 | `manifest.py:163-191`（桩）、`prelude.py:77-96`（桥调用）、`invoker.py:93-135`（复用内层 ToolNode） | 满足 |
| REQ-5 中断类排除 / 命令审批 fail-closed | `code_mode.py:63-77`（强制 `dontInterrupt` 过滤 + allow/deny）、`graph.py:789-799`（code mode 下 review 档降 `block`） | 满足 |
| REQ-6 上下文回填 | 内层调用结果只经 `prelude` 回给代码、不进 state（`invoker.py:119-135`）；`bridge.py:381-411` 只拼执行输出 | 满足 |
| REQ-7 事件与展示 | `code_mode.py:101-112` 复用 `ManuallyEmitToolCall`，`core/ag_ui/agent.py:858-882` 转三元组，未新增事件类型 | 满足 |
| REQ-8 异常与重试 | `errors.py:13-30`、`session.py:57-64`、`tool.py:85-94`（`GraphBubbleUp` 子类终止本轮） | 满足 |
| REQ-9 运行时覆盖 | `types.py:321-347` + 四类 backend 的 `scratch_dir`（`local_backend.py:766-770`、`bubblewrap_backend.py:586-590`、`e2b_backend.py:656-660`、`paas_backend.py:883-886`） | 代码层满足；E2B/paas 真实端到端未取证（按 ACC-15 记录） |
| REQ-10 安全约束 | 代码只在沙箱执行（`bridge.py:214-259`）、prelude 不新增 env 注入、名单双校验（`prelude.py:79-80` + `invoker.py:104-105`）、结果经既有脱敏 wrapper | 满足 |
| REQ-11 A2A 继承 | `a2a_tools/utils.py:108-112` 走 `model_copy()`，`tool_use_mode` 自动继承 | 满足 |
| REQ-12 测算方案 | `src/agent/docs/code-mode-benefit-measurement.md` 五节齐全，无评测代码/实测数据 | 满足 |
| REQ-13 契约兼容 | 新增可选字段；`test_code_mode_contract.py:34-51` 覆盖流式/非流式/resume 路径一致 | 满足 |

### 验证复核

默认不重跑；因「ACC-15 的 local 真实端到端」「重试终止」等关键结论仅由用例承担，重跑了
同一条定向命令，结果与 `tasks.md` 验证记录一致：

- `tests/core/tools/code_mode` + `test_code_mode_{switch,assembly,contract}.py` +
  `test_code_mode_events.py` + `test_code_mode_inheritance.py`：**74 passed, 1 skipped**。
- `tests/core/tools/code_mode/test_local_e2e.py`：`local` 真实后端通过（含循环、分支、聚合，
  `total= 8`）；`bubblewrap` 因容器缺 `bwrap` 跳过，与记录一致。
- 回归：`tests/core/graphs`：**225 passed, 1 deselected**（`function_call` 路径无新增失败）。
- 未重跑 `tests/core` 全量（记录为 2263 passed）；抽样回归未见异常。

### 结论

REQ-1 ~ REQ-13 全部落地，ACC-1 ~ ACC-14、ACC-16 ~ ACC-19 有可复现的离线证据，
ACC-15 按规格记为未取证，无阻塞项。记录 8 条改进建议，均不构成返工理由。
本仓结论：**通过，但有改进建议**。
