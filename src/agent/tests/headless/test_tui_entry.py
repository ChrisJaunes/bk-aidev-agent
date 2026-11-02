"""Entry-point and environment-order regression tests for the merged runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_package_import_does_not_initialize_sdk_or_logging():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, logging; import aidev_agent.headless; "
         "assert 'aidev_agent.config' not in sys.modules; "
         "assert 'aidev_agent.headless.cli' not in sys.modules; "
         "assert 'aidev_agent.headless.runtime' not in sys.modules; "
         "assert not logging.getLogger('aidev_agent.headless').handlers"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_tui_bootstraps_config_before_sdk_import_and_recovers_bad_input(tmp_path: Path):
    config = tmp_path / "bkai_tui.json"
    config.write_text(json.dumps({"env": {"BK_AIDEV_AGENT_APP_CODE": "headless-startup-test"}}))
    env = {**os.environ, "AGENT_TUI_TUI_CONFIG_FILE": str(config), "AGENT_TUI_OTEL": "0",
           "BKAI_TUI_LOG_PATH": str(tmp_path / "headless.log")}
    env.pop("BK_AIDEV_AGENT_APP_CODE", None)
    code = (
        "import asyncio; from aidev_agent.headless import runtime; "
        "asyncio.run(runtime.main()); from aidev_agent.config import settings; "
        "assert settings.APP_CODE == 'headless-startup-test'"
    )
    result = subprocess.run(
        [sys.executable, "-u", "-c", code], env=env,
        input='not-json\n{"id":"stop","method":"process.shutdown","params":{}}\n',
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert [frame["kind"] for frame in frames] == ["ready", "error", "result"]
    assert frames[1]["error"]["code"] == "PARSE_ERROR"
    assert frames[2]["id"] == "stop"
    import aidev_agent

    assert frames[0]["sdk_file"] == aidev_agent.__file__
