from __future__ import annotations

import os
import random
from typing import Generator
from unittest import mock

import pytest

import runtool
from runtool import RUNTOOL_CONFIG
from runtool import ExecutableProvider
from runtool import ToolInstallerConfig


@pytest.fixture(autouse=True)
def _mock_settings_env_vars() -> Generator[None, None, None]:
    tool_installer_opt_dir = "/tmp/test"  # noqa: S108
    with mock.patch.dict(
        os.environ,
        {
            "CI": "1",
            "TOOL_INSTALLER_OPT_DIR": tool_installer_opt_dir,
            "TOOL_INSTALLER_BIN_DIR": f"{tool_installer_opt_dir}/bin",
            "TOOL_INSTALLER_PIPX_HOME": f"{tool_installer_opt_dir}/pipx_home",
            "TOOL_INSTALLER_PACKAGE_DIR": f"{tool_installer_opt_dir}/packages",
            "TOOL_INSTALLER_GIT_PROJECT_DIR": f"{tool_installer_opt_dir}/git_projects",
            "PIPX_HOME": f"{tool_installer_opt_dir}/pipx_home",
            "PIPX_BIN_DIR": f"{tool_installer_opt_dir}/bin",
            "PATH": f"{tool_installer_opt_dir}/bin:/usr/bin:/bin",
            # "PATH": f"{tool_installer_opt_dir}/bin:{os.environ['PATH']}",
        },
    ):
        runtool.TOOL_INSTALLER_CONFIG = ToolInstallerConfig()
        yield


def non_pip_tools() -> Generator[ExecutableProvider, None, None]:
    for tool in RUNTOOL_CONFIG.tools():
        executable_provider = RUNTOOL_CONFIG.get_executable_provider(tool)
        if executable_provider.__class__.__name__ != "PipxInstallSource" and tool not in (
            ",miniconda-installer.sh",
            "watchman",
            "npm",
            "npx",
            "adb",  # Fails in CI
            "fastboot",  # Fails in CI
            "termscp",  # Fails in CI
            "duckdb",  # Fails in CI
        ):
            yield executable_provider


random.seed(42)  # Set seed for reproducibility


@pytest.mark.parametrize("tool_provider", random.sample([*(non_pip_tools())], 5))
def test_eval(tool_provider: ExecutableProvider) -> None:
    possible_commands = ("--help", "-h", "help", "--version", "-v")
    for command in possible_commands:
        result = tool_provider.run(command)
        if result.returncode == 0:
            return
        print(result.stdout)
        print(result.stderr)
    msg = f"Could not find help or version for {tool_provider} {tool_provider.get_executable()}"
    tool_provider.uninstall()
    raise AssertionError(msg)
