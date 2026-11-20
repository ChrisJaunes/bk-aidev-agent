import tomllib
from pathlib import Path

import aidev_agent_cli


def test_package_version_matches_project():
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_file.open("rb") as file:
        project = tomllib.load(file)
    assert aidev_agent_cli.__version__ == project["project"]["version"]
    assert project["project"]["name"] == "aidev-agent-cli"
