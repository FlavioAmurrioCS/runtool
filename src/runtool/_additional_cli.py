from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING
from typing import Sequence

if TYPE_CHECKING:
    from runtool._types import PipxList


from runtool import RUNTOOL_CONFIG
from runtool import CLIApp
from runtool import PipxInstallSource


class CommaFixer(CLIApp):
    """Fix commands in path."""

    COMMAND_NAME = "__comma-fixer"

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        _ = cls.parse_args(argv)
        path_dir = os.path.dirname(sys.argv[0])
        for file_name in os.listdir(path_dir):
            file_path = os.path.join(path_dir, file_name)
            if (
                file_name.startswith("-")
                and os.access(file_path, os.X_OK)
                and not os.path.isdir(file_path)
            ):
                shutil.move(file_path, os.path.join(path_dir, "," + file_name[1:]))
        print("Fixed!", file=sys.stderr)
        return 0


class ValidateConfig(CLIApp):
    """Validate config."""

    COMMAND_NAME = "__validate-config"

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        _ = cls.parse_args(argv)
        for tool in RUNTOOL_CONFIG.tools():
            executable_provider = RUNTOOL_CONFIG[tool]
            print(f"{executable_provider=}")
        return 0


class PipxConfigCLI(CLIApp):
    """Pipx config CLI."""

    COMMAND_NAME = "pipx-migrate"

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:  # noqa: ARG003
        result = subprocess.run(  # noqa: S603
            (PipxInstallSource.PIPX_EXECUTABLE_PROVIDER.get_executable(), "list", "--json"),
            check=True,
            capture_output=True,
        )
        pipx_list: PipxList = json.loads(result.stdout)
        ret: dict[str, dict[str, str]] = {}
        for _venv, v in pipx_list["venvs"].items():  # noqa: PERF102
            # print(venv)
            main_package = v["metadata"]["main_package"]

            package = main_package["package_or_url"]
            if package in main_package["apps"]:
                ret[package] = PipxInstallSource(  # noqa: SLF001
                    package=package, command=package
                )._mdict()
            else:
                ret[main_package["apps"][0]] = PipxInstallSource(  # noqa: SLF001
                    package=package, command=main_package["apps"][0]
                )._mdict()

            # for app in main_package["apps"]:
            #     ret[app] = PipxInstallSource(
            #         package=main_package["package_or_url"], command=app
            #     )._mdict()
            #     break

        print(json.dumps(ret, indent=2, default=str))
        return 0
