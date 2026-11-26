# PTC 串行执行

PTC 将模型生成的 Python 放入独立 PaaS 沙箱执行。Python 中的
`agent_tools.call(name, arguments)` 通过本地 Unix Socket 产生工具事件，
当前 Celery Worker 的 Runtime 校验并执行真实工具，再经下一次 HTTP 请求回填。

## 启用

默认关闭。平台 `agent_info.ptc_settings` 或 SDK `AgentConfig.ptc_settings` 示例：

```json
{
  "enabled": true,
  "snapshot": "由宿主配置的无业务凭证沙箱镜像",
  "tool_codecs": {
    "lookup_record": "json",
    "read_document": "text"
  }
}
```

`tool_codecs` 是宿主对数据工具的显式开放：名称必须是装配、重名处理和请求过滤后的
实际名称。`json` 用于 JSON 对象或数组等结果，`text` 用于纯文本，`content` 保留
LangChain content blocks 的数据形态。工具仍需存在于本轮已授权的外部工具集合。
审批、内置运行时工具、递归代理和 return_direct 工具不会开放给 PTC。

只登记无副作用的数据工具。此版本不提供跨 Worker 的持久结果账本，不能用于需要
Exactly Once 保证的写操作。代码和工具输入均有字节限制；工具调用默认最多 100 次。

PTC 使用专用 `PaasSandboxBackend`，不复用 file-kit 的后端实例、不调用技能凭证
注入、不挂载技能 PV、不降级到 Worker 本地执行。镜像和平台网络策略须由宿主配置为
不含业务凭证并禁止沙箱出站访问。Python 代码进程只继承最小环境变量。
PTC 要求已知凭证脱敏启用，不能靠关闭模型展示脱敏来关闭沙箱数据边界的净化。

## 模型调用

模型工具为 `execute_python(code, inputs)`。目标沙箱、执行身份、调用 ID 和预算由
服务端决定，模型不能指定它们。使用现有工具 Schema 或 `tool_describe` 查询参数。

```python
from agent_tools import call, ToolCallError

values = []
for record_id in inputs["ids"]:
    record = call("lookup_record", {"record_id": record_id})
    values.append(record["amount"])

result = {"count": len(values), "sum": sum(values)}
```

赋值给 `result` 的有限 JSON 值作为最终结果。工具失败会抛出带 `code` 属性的
`ToolCallError`；代码可以捕获业务错误。stdout/stderr 独立排空并限长，不混入协议。
程序工具结果在 Runtime 净化后保留原类型；模型和 AG-UI 只收到外层执行结果，内部
临时消息不会写入外层对话历史。

## 执行和重试

每个 Runtime 按需创建一个专用沙箱，每次代码执行创建独立 Supervisor 和代码进程。
Supervisor 缓存事件；代码进程在工具调用处阻塞，保持同一 PID、局部变量和调用栈。
初版采用每执行独立控制进程以隔离终态重放；后续可在测量开销后再做控制进程复用。

沙箱和资产就绪、串行 N 次工具调用、每段计算在等待窗口内完成时，执行路径为
一次 start 加 N 次 resume，即 N+1 次执行请求。超过窗口返回 running，Runtime
随后 attach；创建、部署、重试及大载荷上传额外计数。

同一执行 ID 的 start 不重复启动；同一 call_id 的工具结果在 Runtime 记录；同一
call_id 和相同结果的 resume 只注入一次。重复请求重放原事件，不推进下一个调用。
代码和结果的冲突提交会被拒绝。

活动执行固定原 sandbox_id，直接使用独立 stdout 的 HTTP 命令 API。404 返回
EXECUTION_LOST，不走普通后端的自动重建逻辑。小载荷使用受控 base64 参数；大载荷
通过文件上传，避免超过 Linux 单个参数长度限制。所有真实 HTTP 请求均有超时。

## 生命周期与首版边界

同一 Runtime 同时只允许一个 PTC 执行，默认累计最多 16 次，已完成调用的重放不消耗
新执行预算。Runtime 的结果缓存默认最多 16 MiB。每段代码默认执行上限 300 秒、单工具
等待上限 60 秒；代码完成后回收进程组，Supervisor 保留终态 300 秒供重放。
Runtime 释放时销毁专用沙箱，平台 TTL 兜底异常退出后的资源回收。

本版支持当前 Runtime 内 HTTP 重连和结果回填幂等；不支持人机审批挂起、跨 Worker
接管、跨代码段 Python 全局变量或任意图 Command。遇到恢复请求明确拒绝，遇到
丢失或中断失败后不在同一 Runtime 内重跑原代码。审批工具仍可使用原普通工具链。
新的 Runtime 重试属于新的执行，不能把进程内账本当作跨进程副作用保证。

SDK 直接使用 `ReActAgentBuilder` 时需提供 `ptc_settings` 和当前 Runtime 独占的
`ptc_session_manager`，并在 Runtime 结束后调用 manager.close()；
`ChatCompletionAgent` 的标准构建与释放链已处理此生命周期。

## 验证

```bash
cd src/agent
make test path=tests/core/ptc
```

测试以真实 Unix Socket 和后台子进程模拟独立 HTTP 命令，覆盖执行栈保留、N+1、
断线重试、重复提交、取消、崩溃、凭证环境隔离、Runtime 工具执行、结构化结果和
数据脱敏。PaaS 客户端使用 mock 验证固定实例和 HTTP 超时；平台真实网络策略与
镜像兼容性仍需在目标 PaaS 环境运行验收。
