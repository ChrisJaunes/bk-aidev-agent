# 插件自己的 uv 环境（./.venv）是唯一执行环境：make init 装它，make test 也用它。
# SDK 以 path source 装进这个环境（见 pyproject.toml [tool.uv.sources]），
# 因此跑的是当前源码而不是某个已发布的同名版本；SDK 不反向依赖本插件。
ROOT_DIR?=$(shell git rev-parse --show-toplevel)/src/plugins/aidev_agent_cli
# 子进程仍需以 src/agent 为 cwd（.env 加载、AGENT_TUI_CWD 相对路径、~/.bkai 都依赖它）；
# 把插件根放在 PYTHONPATH 最前，保证 python -m aidev_agent_cli 解析到当前源码。
PYTHONPATH_VALUE=${ROOT_DIR}:${PYTHONPATH}
pytest = cd ${ROOT_DIR} && PYTHONPATH=${PYTHONPATH_VALUE} uv run --no-sync pytest -c ${ROOT_DIR}/pyproject.toml

.PHONY: ALL init uv-install lock requirements test build

ALL: init

uv.lock: pyproject.toml
	uv lock
	uv lock --check

requirements: requirements.txt

requirements.txt: pyproject.toml
	uv export --no-hashes --no-annotate --no-dev --no-editable --no-header --format requirements-txt > requirements.txt

init: uv-install
	@echo "Project initialization complete."

# --inexact 保留环境里 uv 不认识的包（例如手工安装的工具），不会顺手删掉。
# dev 组已含 checkpoint / 追踪相关依赖，init 之后即可跑全部用例。
uv-install:
	uv --version
	uv sync --inexact

# 相对路径按插件根归一：子目录（如 tests/integration）是相对插件根，而不是相对子进程 cwd。
test:
	$(eval path ?= ${ROOT_DIR}/tests)
	$(eval maxfail ?= --maxfail=2)
	$(eval warnings ?= --disable-pytest-warnings)
	$(eval markers ?= not slow)
	$(eval resolved_path = $(if $(filter /%,$(path)),$(path),${ROOT_DIR}/$(path)))
	${pytest} $(resolved_path) \
		-m "$(markers)" \
		${maxfail} \
		${warnings} \
		--durations=20 \
		${args}

build:
	uv build
