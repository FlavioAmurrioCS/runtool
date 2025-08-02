from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any
    from typing import Literal

    from typing_extensions import TypedDict

    InjectedPackages = dict[str, str]

    PathItem = dict[Literal["__Path__", "__type__"], str]

    class MainPackage(TypedDict):
        app_paths: list[PathItem]
        apps: list[str]
        include_apps: bool
        include_dependencies: bool
        man_pages: list[str]
        man_paths: list[PathItem]
        package: str
        package_or_url: str
        package_version: str
        pip_args: list[str]
        suffix: str
        app_paths_of_dependencies: dict[str, list[PathItem]]
        apps_of_dependencies: list[str]
        man_pages_of_dependencies: list[str]
        man_paths_of_dependencies: dict[str, list[PathItem]]

    class Metadata(TypedDict):
        injected_packages: InjectedPackages
        main_package: MainPackage
        pipx_metadata_version: str
        python_version: str
        venv_args: list[Any]

    class Venv(TypedDict):
        metadata: Metadata

    class PipxList(TypedDict):
        pipx_spec_version: str
        venvs: dict[str, Venv]
