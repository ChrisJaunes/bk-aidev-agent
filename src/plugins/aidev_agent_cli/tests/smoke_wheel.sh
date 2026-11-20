#!/usr/bin/env bash
# wheel 独立安装 smoke：本地构建 + 干净 venv 安装 + 两个协议各跑一遍。
#
# 关键约束（避免用现有开发环境“假绿”）：
#   * 全程清空 PYTHONPATH，并在与源码树无关的临时目录执行；
#   * 只装 CLI wheel 的 [checkpoint] extra，不手工补 langgraph-checkpoint-sqlite；
#   * 校验 pip check、SDK/CLI 的解析来源（必须是 site-packages，不能是源码树）；
#   * 不发布，只本地 build/install。
#
# 用法：bash src/plugins/aidev_agent_cli/tests/smoke_wheel.sh
set -euo pipefail

unset PYTHONPATH PYTHONHOME || true

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_ROOT="$(cd "${PLUGIN_ROOT}/../../agent" && pwd)"
SMOKE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aidev-cli-smoke.XXXXXX")"
VENV_DIR="${SMOKE_DIR}/venv"
DIST_DIR="${SMOKE_DIR}/dist"
PY="${VENV_DIR}/bin/python"

cleanup() { rm -rf "${SMOKE_DIR}"; }
trap cleanup EXIT

echo "== 1/10 build wheels locally (no publish) =="
cd "${PLUGIN_ROOT}"
uv build --wheel --out-dir "${DIST_DIR}"
cd "${AGENT_ROOT}"
uv build --wheel --out-dir "${DIST_DIR}"

SDK_WHEEL="$(ls "${DIST_DIR}"/aidev_agent-*.whl)"
CLI_WHEEL="$(ls "${DIST_DIR}"/aidev_agent_cli-*.whl)"
echo "sdk: ${SDK_WHEEL}"
echo "cli: ${CLI_WHEEL}"

echo "== 2/10 SDK wheel must not contain headless artifacts =="
if unzip -l "${SDK_WHEEL}" | grep -q "headless"; then
    echo "FAIL: SDK wheel 仍含 headless 产物" >&2
    unzip -l "${SDK_WHEEL}" | grep headless >&2
    exit 1
fi
echo "ok: SDK wheel 无 headless"

echo "== 3/10 clean venv, install CLI wheel with [checkpoint,acp] extras =="
python -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
# 只装 extra：checkpoint 需要的 sqlite saver 必须由 extra 自己带进来。
#
# 先装本地构建的 SDK wheel，再装 CLI：若让 pip 自己解析 aidev-agent>=3.0.0，
# 它会命中 PyPI 上同版本的发布产物——那一份不含本仓新增的 llm_retry 等模块，
# 验证的就变成“旧 SDK + 新 CLI”，正是要避免的假绿。
"${VENV_DIR}/bin/pip" install --quiet --no-deps --force-reinstall "${SDK_WHEEL}"
"${VENV_DIR}/bin/pip" install --quiet "${CLI_WHEEL}[checkpoint,acp]"

echo "== 4/10 pip check =="
"${VENV_DIR}/bin/pip" check

echo "== 5/10 declared constraints honored =="
"${PY}" - <<'PY'
import importlib.metadata as md
import sys

def ver(name):
    return md.version(name)

openai = ver("openai")
major, minor = (int(x) for x in openai.split(".")[:2])
assert (major, minor) >= (2, 54), f"openai {openai} 不满足 SDK 声明的 >=2.54.0"

# [checkpoint] extra 必须自己把 saver 带进来，而不是靠外部手工安装
md.distribution("langgraph-checkpoint-sqlite")
import langgraph.checkpoint.sqlite  # noqa: F401

# [acp] extra 必须带真实的 agent-client-protocol（ACP 用例与 smoke 都依赖它）
import acp

assert acp.PROTOCOL_VERSION == 1, acp.PROTOCOL_VERSION

print("python :", sys.version.split()[0])
print("openai :", openai)
print("saver  :", ver("langgraph-checkpoint-sqlite"))
print("acp    :", ver("agent-client-protocol"))
PY

echo "== 6/10 import origins must be site-packages =="
cd "${SMOKE_DIR}"
"${PY}" - <<'PY'
import aidev_agent
import aidev_agent_cli

for mod in (aidev_agent, aidev_agent_cli):
    assert "site-packages" in mod.__file__, f"{mod.__name__} 未从 site-packages 解析: {mod.__file__}"
print("aidev_agent    :", aidev_agent.__file__)
print("aidev_agent_cli:", aidev_agent_cli.__file__)
PY

echo "== 7/10 embedded protocol =="
EMBED="${SMOKE_DIR}/embedded.out"
# create 后 close：两条请求走同一条 stdin，close 才会命中已存在的会话
printf '%s\n%s\n' \
    '{"id":"1","method":"create","params":{"session_code":"s","agent":{"agent_code":"a"},"llm":{"base_url":"http://127.0.0.1:1","model":"m"}}}' \
    '{"id":"2","method":"close","params":{"session_code":"s"}}' \
    | "${PY}" -m aidev_agent_cli > "${EMBED}" 2>"${SMOKE_DIR}/embedded.err"
"${PY}" - "${EMBED}" <<'PY'
import json
import sys

lines = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
responses = {line.get("id"): line for line in lines if "ok" in line}
assert responses["1"]["ok"] is True, responses.get("1")
assert responses["2"]["ok"] is True, responses.get("2")
print("ok: embedded 响应", responses["1"]["result"], responses["2"]["result"])
PY

echo "== 8/10 tui protocol: ready then result =="
TUI="${SMOKE_DIR}/tui.out"
echo '{"id":"s","method":"process.shutdown","params":{}}' \
    | "${PY}" -u -m aidev_agent_cli --protocol tui > "${TUI}"
"${PY}" - "${TUI}" <<'PY'
import json
import sys

lines = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
kinds = [line["kind"] for line in lines]
assert kinds[:2] == ["ready", "result"], kinds
print("ok: tui 帧序列", kinds)
PY

if "${PY}" -m aidev_agent.headless </dev/null >/dev/null 2>&1; then
    echo "FAIL: 旧入口 python -m aidev_agent.headless 仍可用" >&2
    exit 1
fi
echo "ok: 旧入口已消失"

# ---------------------------------------------------------------- ACP
# 握手路径**不得**触达平台 HTTP：initialize 在任何平台调用之前完成，因此这里
# 只给一个假 token、不配置任何真实凭据或平台地址。
export BKAI_TUI_ACCESS_TOKEN=dummy-token
export AGENT_TUI_TUI_CONFIG_FILE="${SMOKE_DIR}/bkai_tui.json"
export BKAI_TUI_LOG_PATH="${SMOKE_DIR}/worker.log"

echo "== 9/10 acp handshake: initialize -> protocolVersion 1, no ready frame =="
ACP_INIT="${SMOKE_DIR}/acp_init.out"
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{"fs":{"readTextFile":false,"writeTextFile":false}},"clientInfo":{"name":"smoke","version":"0"}}}'     | "${PY}" -u -m aidev_agent_cli --protocol acp --resources platform         > "${ACP_INIT}" 2>"${SMOKE_DIR}/acp_init.err"
"${PY}" - "${ACP_INIT}" <<'PY'
import json
import sys

lines = [line for line in open(sys.argv[1]) if line.strip()]
assert lines, "acp 握手没有产生任何响应"
response = json.loads(lines[0])
assert response["id"] == 1, response
assert response["result"]["protocolVersion"] == 1, response
caps = response["result"]["agentCapabilities"]
assert caps.get("loadSession", False) is False, caps
assert '"kind":"ready"' not in open(sys.argv[1]).read(), "acp 模式不得出现 agui 的 ready 帧"
print("ok: acp 握手 protocolVersion=1")
PY

echo "== 10/10 acp eof: stdin closed -> exit 0, no traceback =="
ACP_EOF="${SMOKE_DIR}/acp_eof.err"
set +e
"${PY}" -u -m aidev_agent_cli --protocol acp --resources platform     < /dev/null > "${SMOKE_DIR}/acp_eof.out" 2> "${ACP_EOF}"
ACP_STATUS=$?
set -e
if [ "${ACP_STATUS}" -ne 0 ]; then
    echo "FAIL: acp EOF 退出码 ${ACP_STATUS}" >&2
    tail -20 "${ACP_EOF}" >&2
    exit 1
fi
if grep -q "Traceback" "${ACP_EOF}"; then
    echo "FAIL: acp EOF 出现 traceback" >&2
    tail -20 "${ACP_EOF}" >&2
    exit 1
fi
if [ -s "${SMOKE_DIR}/acp_eof.out" ]; then
    echo "FAIL: 无请求的 acp EOF 写脏了 stdout" >&2
    cat "${SMOKE_DIR}/acp_eof.out" >&2
    exit 1
fi
echo "ok: acp EOF 干净退出"

echo "WHEEL SMOKE PASSED"