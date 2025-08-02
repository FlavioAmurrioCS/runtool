#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import dataclasses
import glob
import gzip
import itertools
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from abc import abstractmethod
from collections import Counter
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import asdict
from dataclasses import dataclass
from functools import cache
from functools import lru_cache
from textwrap import dedent
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple
from typing import Union
from typing import overload
from urllib.parse import urljoin
from urllib.parse import urlparse

if TYPE_CHECKING:
    from _collections_abc import dict_keys
    from collections.abc import Generator
    from collections.abc import Sequence
    from typing import Literal
    from typing import Protocol  # python3.8+

    import requests
    from requests import PreparedRequest
    from typing_extensions import Self
    from typing_extensions import TypeAlias

    JSON_TYPE: TypeAlias = Union[str, int, float, bool, None, list[Any], dict[str, Any]]
else:
    Protocol = object

logger = logging.getLogger(__name__)


def gron(obj: JSON_TYPE) -> list[str]:
    def _gron_helper(obj: JSON_TYPE, path: str = "json") -> Generator[tuple[str, str], None, None]:
        if isinstance(obj, dict):
            yield path, "{}"
            for key, value in obj.items():
                _key = f".{key}" if key.isalnum() else f'["{key}"]'
                yield from _gron_helper(value, f"{path}{_key}")
        elif isinstance(obj, list):
            yield path, "[]"
            for i, value in enumerate(obj):
                yield from _gron_helper(value, f"{path}[{i}]")
        elif isinstance(obj, bool):
            yield path, "true" if obj else "false"
        elif obj is None:
            yield path, "null"
        elif isinstance(obj, str):
            yield path, f'"{obj}"'
        else:
            yield path, str(obj)

    return sorted(f"{path} = {value};" for path, value in _gron_helper(obj))


class BColors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    RESET = "\033[0m"


def input_tty(prompt: str | None = None) -> str:
    with open("/dev/tty") as tty:
        if prompt:
            print(prompt, end="", file=sys.stderr)
        try:
            return tty.readline().strip()
        except KeyboardInterrupt:
            raise SystemExit from KeyboardInterrupt


def selection(options: list[str]) -> str | None:
    if len(options) == 1 or os.environ.get("CI"):
        return options[0]
    print(
        f"{BColors.OKCYAN}{'#' * 100}\nPlease select one of the following options:\n{'#' * 100}{BColors.RESET}",  # noqa: E501
        file=sys.stderr,
    )
    try:
        return options[
            int(
                input_tty(
                    "\n".join(f"{i}: {x}" for i, x in enumerate(options)) + "\nEnter Choice: "
                )
                or 0
            )
        ]
    except IndexError:
        return None


def rm_shim(shim: str) -> None:
    if os.path.islink(shim):
        os.unlink(shim)
    elif os.path.exists(shim):
        shutil.rmtree(shim, ignore_errors=True)


@lru_cache(maxsize=1)
def all_pythons() -> tuple[str, ...]:
    return tuple(
        x
        for x in (
            "python3",
            "python3.8",
            "python3.9",
            "python3.10",
            "python3.11",
            "python3.12",
            "python3.13",
        )
        if shutil.which(x)
    ) or (sys.executable,)


@cache
def domain_env_name(domain: str) -> str:
    domain = domain.upper()
    if domain in {"API.GITHUB.COM", "GITHUB.COM"}:
        domain = f"PUBLIC.{domain}"

    domain = f"TOKEN.{domain}"

    weight = {
        "GITHUB": 80,
        "TOKEN": 90,
    }
    parsed = domain.rsplit(".", maxsplit=1)[0].upper()
    tokens = sorted(
        (x for x in re.split(r"[.-]+", parsed) if x not in {"API", "CLOUD"}),
        key=lambda x: (weight.get(x, 0), x),
    )
    return "_".join(tokens)


@lru_cache(maxsize=1)
def default_session() -> requests.Session:
    import requests
    import requests.auth

    class MyAuth(requests.auth.AuthBase):
        def __call__(self, r: PreparedRequest) -> PreparedRequest:
            if r.url:
                from urllib.parse import urlparse

                env_name = domain_env_name(urlparse(r.url).netloc)
                if env_name in os.environ:
                    r.headers["Authorization"] = f"token {os.environ[env_name]}"
            return r

    ret = requests.Session()
    verify = os.environ.get("RUNTOOL_CA_BUNDLE", None)
    if verify and os.path.exists(verify):
        ret.verify = verify
    elif "RUNTOOL_VERIFY" in os.environ:
        ret.verify = int(os.environ["RUNTOOL_VERIFY"]) != 0
    ret.auth = MyAuth()
    return ret


# def m_requests(url: str, headers: dict[str, str] | None = None) -> _UrlopenRet:
#     import urllib.request

#     headers = headers or {}
#     if "github" in url and "GITHUB_TOKEN" in os.environ:
#         headers["Authorization"] = f'token {os.environ["GITHUB_TOKEN"]}'

#     req = urllib.request.Request(url, headers=headers or {})
#     return urllib.request.urlopen(req)


@lru_cache(maxsize=1)
def get_request(url: str) -> str:
    return default_session().get(url).text
    # with m_requests(url) as f:
    #     return f.read().decode("utf-8")


@contextmanager
def download_context(url: str) -> Generator[str, None, None]:
    logger.info("Downloading: %s", url)
    derive_name = os.path.basename(url)
    with tempfile.TemporaryDirectory() as tempdir:
        download_path = os.path.join(tempdir, derive_name)
        with open(download_path, "wb") as file, default_session().get(url, stream=True) as response:
            file.writelines(response.iter_content(chunk_size=4 * 1024))
        yield download_path


# region core


@dataclass
class ToolInstallerConfig:
    OPT_DIR: str
    BIN_DIR: str
    PACKAGE_DIR: str
    GIT_PROJECT_DIR: str
    PIPX_HOME: str

    def __init__(self) -> None:
        self.OPT_DIR = os.path.expanduser(os.environ.get("TOOL_INSTALLER_OPT_DIR", "~/opt/runtool"))
        self.BIN_DIR = os.path.expanduser(
            os.environ.get("TOOL_INSTALLER_BIN_DIR", os.path.join(self.OPT_DIR, "bin"))
        )
        self.PACKAGE_DIR = os.path.expanduser(
            os.environ.get("TOOL_INSTALLER_PACKAGE_DIR", os.path.join(self.OPT_DIR, "packages"))
        )
        self.GIT_PROJECT_DIR = os.path.expanduser(
            os.environ.get(
                "TOOL_INSTALLER_GIT_PROJECT_DIR", os.path.join(self.OPT_DIR, "git_projects")
            )
        )
        self.PIPX_HOME = os.path.expanduser(
            os.environ.get("TOOL_INSTALLER_PIPX_HOME", os.path.join(self.OPT_DIR, "pipx_home"))
        )


TOOL_INSTALLER_CONFIG = ToolInstallerConfig()


class ExecutableProvider(Protocol):
    def executable_path(self) -> str: ...

    def get_executable(self) -> str: ...

    def run(self, *args: str) -> subprocess.CompletedProcess[str]: ...

    def _mdict(self) -> dict[str, Any]: ...

    def uninstall(self) -> None: ...

    def reinstall(self) -> str: ...


class _ToolInstallerBase(Protocol):
    @staticmethod
    def make_executable(filename: str) -> str:
        os.chmod(filename, os.stat(filename).st_mode | stat.S_IEXEC)
        return filename

    @abstractmethod
    def get_executable(self) -> str: ...

    @abstractmethod
    def uninstall(self) -> None: ...

    def reinstall(self) -> str:
        self.uninstall()
        return self.get_executable()

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            (self.get_executable(), *args),
            text=True,
            errors="ignore",
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def _mdict(self) -> dict[str, Any]:
        class_name = self.__class__.__name__

        m_asdict: dict[str, str] = (
            asdict(self) if dataclasses.is_dataclass(self) else self._asdict()  # type:ignore[attr-defined]
        )

        with suppress(Exception):
            anno: dict[str, dataclasses.Field] = self.__class__.__dataclass_fields__  # type:ignore[attr-defined,type-arg]
            for k, v in anno.items():
                if m_asdict[k] == v.default:
                    del m_asdict[k]

        return {
            "class": class_name,
            **{
                key: value
                for key, value in m_asdict.items()
                if value is not None and not key.isupper()
            },
        }


class InternetInstaller(_ToolInstallerBase, Protocol):
    @staticmethod
    def uncompress(filename: str) -> zipfile.ZipFile | tarfile.TarFile | gzip.GzipFile:
        if filename.endswith(".zip"):
            return zipfile.ZipFile(filename)
        if filename.endswith(".gz") and not filename.endswith(".tar.gz"):
            return gzip.open(filename)
        return tarfile.open(filename)

    @staticmethod
    def find_executable(directory: str, executable_name: str) -> str | None:
        glob1 = glob.iglob(
            os.path.join(
                directory,
                "**",
                executable_name,
            ),
            recursive=True,
        )
        glob2 = glob.iglob(
            os.path.join(
                directory,
                "**",
                f"{executable_name}*",
            ),
            recursive=True,
        )
        return next(
            (
                x
                for x in itertools.chain(glob1, glob2)
                if (os.path.isfile(x)) and not os.path.islink(x)
            ),
            None,
        )

    @classmethod
    def executable_from_url(cls, url: str, rename: str | None = None) -> str:
        """Url must point to executable file."""
        rename = rename or os.path.basename(url)
        executable_path = os.path.join(TOOL_INSTALLER_CONFIG.BIN_DIR, rename)
        if not os.path.exists(executable_path):
            os.makedirs(TOOL_INSTALLER_CONFIG.BIN_DIR, exist_ok=True)
            with download_context(url) as download_file:
                shutil.move(download_file, executable_path)
        return cls.make_executable(executable_path)

    @classmethod
    def executable_from_package(
        cls,
        package_url: str,
        executable_name: str,
        package_name: str | None = None,
        rename: str | None = None,
    ) -> str:
        """
        Get the executable from a online package.

        package_url         points to zip/tar file.
        executable_name     file to looked for in package.
        package_name        what should the package be rename to.
        rename              The name of the file place in bin directory.
        """
        package_name = package_name or os.path.basename(package_url)
        package_path = os.path.join(TOOL_INSTALLER_CONFIG.PACKAGE_DIR, package_name)
        if (
            not os.path.exists(package_path)
            or cls.find_executable(package_path, executable_name) is None
        ):
            with (
                download_context(package_url) as tar_zip_file,
                tempfile.TemporaryDirectory() as tempdir,
            ):
                temp_extract_path = os.path.join(tempdir, "temp_package")
                with cls.uncompress(tar_zip_file) as untar_unzip_file:
                    if isinstance(untar_unzip_file, gzip.GzipFile):
                        os.makedirs(temp_extract_path, exist_ok=True)
                        with open(os.path.join(temp_extract_path, executable_name), "wb") as f:
                            f.write(untar_unzip_file.read())
                    else:
                        untar_unzip_file.extractall(temp_extract_path)  # noqa: S202
                os.makedirs(TOOL_INSTALLER_CONFIG.PACKAGE_DIR, exist_ok=True)
                shutil.move(temp_extract_path, package_path)

        result = cls.find_executable(package_path, executable_name)
        if not result:
            logger.error("%s not found in %s", executable_name, package_path)
            raise SystemExit(1)

        executable = cls.make_executable(result)
        rename = rename or executable_name
        os.makedirs(TOOL_INSTALLER_CONFIG.BIN_DIR, exist_ok=True)
        symlink_path = os.path.join(TOOL_INSTALLER_CONFIG.BIN_DIR, rename)
        if os.path.isfile(symlink_path):
            if not os.path.islink(symlink_path):
                logger.info(
                    "File is already in %s with name %s",
                    TOOL_INSTALLER_CONFIG.BIN_DIR,
                    os.path.basename(executable),
                )
                return executable
            if os.path.realpath(symlink_path) == os.path.realpath(executable):
                return symlink_path
            os.remove(symlink_path)

        os.symlink(executable, symlink_path, target_is_directory=False)
        return symlink_path


class BestLinkService(NamedTuple):
    uname: platform.uname_result = platform.uname()

    def pick(self, links: Sequence[str]) -> str | None:
        links = self.filter_links(links)
        return selection(links) or sorted(links, key=len)[-1]

    def filter_links(self, links: Sequence[str]) -> list[str]:
        """
        Will look at the urls and based on the information it has will try to pick the best one.

        links   links to consider.
        """
        if not links:
            return []
        if len(links) == 1:
            return [links[0]]

        links = self.filter_out_invalid(links)
        links = self.filter_system(links, self.uname.system)
        links = [x for x in links if not x.endswith(".rpm")] or links
        links = [x for x in links if not x.endswith(".deb")] or links
        links = self.filter_machine(links, self.uname.machine)
        links = [x for x in links if "musl" in x.lower()] or links
        links = [x for x in links if "armv7" not in x.lower()] or links
        links = [x for x in links if "32-bit" not in x.lower()] or links
        links = [x for x in links if ".pkg" not in x.lower()] or links
        links = [x for x in links if "manifest" not in x.lower()] or links
        links = [x for x in links if "full" in x.lower()] or links

        if len(links) == 2:  # noqa: PLR2004
            a, b = sorted(links, key=len)
            suffix = b.lower()[len(a) :] if b.lower().startswith(a.lower()) else b.lower()
            if (a + suffix).lower() == b.lower():
                return [a]
            if len(a) == len(b) and a.replace(".tar.gz", ".tar.xz") == b.replace(
                ".tar.gz", ".tar.xz"
            ):
                return [a]

        return sorted(links, key=len)

    def filter_system(self, links: list[str], system: str) -> list[str]:
        system_patterns = {
            "darwin": "darwin|apple|macos|osx",
            "linux": "linux|\\.deb",
            "windows": "windows|\\.exe",
        }

        system = system.lower()
        if system not in system_patterns or not links or len(links) == 1:
            return links

        pat = re.compile(system_patterns[system])
        filtered_links = [
            x
            for x in links
            if pat.search(
                os.path.basename(x).lower(),
            )
        ]
        return filtered_links or links

    def filter_machine(self, links: list[str], machine: str) -> list[str]:
        machine_patterns = {
            "x86_64": "x86_64|amd64|x86",
            "arm64": "arm64|arch64",
            "aarch64": "aarch64|armv7l|armv7|arm64",
        }

        if not links or len(links) == 1:
            return links

        machine = machine.lower()
        pat = re.compile(machine_patterns.get(machine, machine))
        filtered_links = [
            x
            for x in links
            if pat.search(
                os.path.basename(x).lower(),
            )
        ]

        return filtered_links or links

    def filter_out_invalid(self, links: Sequence[str]) -> list[str]:
        return [
            x
            for x in links
            if not re.search(
                "\\.txt|license|\\.md|\\.sha256|\\.sha256sum|checksums|\\.asc|\\.sig|src|\\.sbom",
                os.path.basename(x).lower(),
            )
        ]


_BEST_LINK_SERVICE = BestLinkService()


class LinkInstaller(InternetInstaller, Protocol):
    binary: str
    rename: str | None = None
    package_name: str | None = None

    def links(self) -> list[str]: ...

    def executable_path(self) -> str:
        return os.path.join(
            TOOL_INSTALLER_CONFIG.BIN_DIR,
            self.rename or self.binary,
        )

    def get_executable(self) -> str:
        executable_path = self.executable_path()
        if os.path.exists(executable_path):
            return executable_path

        return self.install_best(
            links=self.links(),
            binary=self.binary,
            rename=self.rename,
            package_name=self.package_name,
        )

    def uninstall(self) -> None:
        executable_path = self.executable_path()
        if os.path.exists(executable_path):
            if os.path.islink(executable_path):
                realpath = os.path.realpath(executable_path)
                package_dir = os.path.join(
                    TOOL_INSTALLER_CONFIG.PACKAGE_DIR,
                    os.path.dirname(realpath[len(TOOL_INSTALLER_CONFIG.PACKAGE_DIR) + 1 :]),
                )
                if not realpath.startswith(package_dir):
                    logger.error("Not able to uninstall %s", realpath)
                    raise SystemExit(1)
                rm_shim(package_dir)
            rm_shim(executable_path)

    def install_best(
        self,
        links: Sequence[str],
        binary: str,
        rename: str | None = None,
        package_name: str | None = None,
    ) -> str:
        rename = rename or binary
        download_url = _BEST_LINK_SERVICE.pick(links)
        if not download_url:
            logger.error("Could not choose appropiate download from %s", rename)
            raise SystemExit(1)
        basename = os.path.basename(download_url)
        if any(basename.endswith(x) for x in (".zip", ".tgz", ".tbz", ".gz")) or ".tar" in basename:
            return self.executable_from_package(
                package_url=download_url,
                executable_name=binary,
                package_name=package_name,
                rename=rename,
            )
        return self.executable_from_url(download_url, rename=rename)


@dataclass
class UrlInstallSource(LinkInstaller):
    url: str
    binary: str = ""
    rename: str | None = None

    def __post_init__(self) -> None:
        self.binary = self.binary or os.path.basename(self.url)

    def links(self) -> list[str]:
        return [self.url]


@dataclass
class _GitHubSource:
    hostname: str
    is_public_github: bool
    api_url: str
    owner: str
    repo: str
    tag: str
    project_url: str

    def __init__(self, url: str) -> None:
        urlparse_result = urlparse(url)
        self.hostname = urlparse_result.hostname or urlparse_result.netloc
        self.is_public_github = self.hostname in ("github.com", "www.github.com")
        self.api_url = (
            "https://api.github.com" if self.is_public_github else f"https://{self.hostname}/api/v3"
        )
        (
            _,
            self.owner,
            self.repo,
            *rest,
        ) = urlparse_result.path.split("/", maxsplit=3)
        self.repo = self.repo.split(".git", maxsplit=1)[0]
        self.project_url = f"https://{self.hostname}/{self.owner}/{self.repo}"
        self.tag = "latest"
        if rest and rest[0].startswith("releases/tag/"):
            _, _, self.tag, *_ = rest[0].split("/")
        self.tag = self.tag or "latest"

    @classmethod
    def _from_owner_repo(cls, owner: str, repo: str) -> _GitHubSource:
        return cls(f"https://github.com/{owner}/{repo}")

    def _links_from_html(self) -> list[str]:
        url = (
            f"{self.project_url}/releases/{'latest' if self.tag == 'latest' else f'tag/{self.tag}'}"
        )
        html = get_request(url)
        download_links: list[str] = []
        if not download_links:
            assets_urls = [
                self.project_url + "/" + link.split("/", maxsplit=3)[3]
                for link in re.findall(
                    f'/{self.owner}/{self.repo}/releases/expanded_assets/[^"]+', html
                )
            ]
            if assets_urls:
                html = get_request(assets_urls[0])
                download_links = [
                    self.project_url + "/" + link.split("/", maxsplit=3)[3]
                    for link in re.findall(
                        f'/{self.owner}/{self.repo}/releases/download/[^"]+', html
                    )
                ]
            else:
                logger.error("Not assets urls")
        return download_links

    def _links_from_api(self) -> list[str]:
        try:
            data = json.loads(
                get_request(f"{self.api_url}/repos/{self.owner}/{self.repo}/releases")
            )
            return [x["browser_download_url"] for x in data[0]["assets"]]
        except Exception:
            logger.exception("Not able to get releases from github api")
            return []

    def links(self) -> list[str]:
        if self.is_public_github:
            return self._links_from_html()
        return self._links_from_api()

    # https://docs.github.com/en/rest/repos/repos?apiVersion=2022-11-28#get-a-repository
    def _repo_info(self) -> dict[str, Any]:
        return json.loads(get_request(f"{self.api_url}/repos/{self.owner}/{self.repo}"))

    def _description_from_api(self) -> str | None:
        description = self._repo_info().get("description")
        return description or None

    def _description_from_html(self) -> str | None:
        html = get_request(self.project_url)
        description = re.search(rf"<title>GitHub - {self.owner}/{self.repo}: (.*)</title>", html)
        return description.group(1) if description else None

    def description(self) -> str | None:
        if self.is_public_github:
            return self._description_from_html()
        return self._description_from_api()


@dataclass
class GithubReleaseLinks(LinkInstaller):
    github_source: _GitHubSource
    binary: str
    rename: str | None = None

    def __init__(
        self,
        url: str,
        binary: str | None = None,
        rename: str | None = None,
    ) -> None:
        self.github_source = _GitHubSource(url=url)
        self.binary = binary or self.github_source.repo
        self.rename = rename
        self.package_name = f"{self.github_source.owner}_{self.github_source.repo}"

    def links(self) -> list[str]:
        return self.github_source.links()


@dataclass
class ShivInstallSource(_ToolInstallerBase):
    SHIV_EXECUTABLE_PROVIDER = UrlInstallSource(
        url="https://github.com/linkedin/shiv/releases/download/1.0.4/shiv", rename=",shiv"
    )
    package: str
    command: str | None = None

    def executable_path(self) -> str:
        return os.path.join(
            TOOL_INSTALLER_CONFIG.BIN_DIR,
            self.command or self.package,
        )

    def get_executable(self) -> str:
        command = self.command or self.package
        bin_path = self.executable_path()
        if not os.path.exists(bin_path):
            shiv_executable = self.SHIV_EXECUTABLE_PROVIDER.get_executable()
            subprocess.run(  # noqa: S603
                (
                    all_pythons()[0],
                    shiv_executable,
                    "-c",
                    command,
                    "-o",
                    bin_path,
                    self.package,
                ),
                check=True,
            )
        return self.make_executable(bin_path)

    def uninstall(self) -> None:
        rm_shim(self.executable_path())


FZF_EXECUTABLE_PROVIDER = GithubReleaseLinks(url="https://github.com/junegunn/fzf", rename=",fzf")

# endregion core


@dataclass
class GitProjectInstallSource(_ToolInstallerBase):
    git_url: str
    path: str
    tag: str = "master"
    pull: bool = False

    def git_project_location(self) -> str:
        return os.path.join(
            TOOL_INSTALLER_CONFIG.GIT_PROJECT_DIR,
            "_".join(self.git_url.split("/")[-1:]),
        )

    def executable_path(self) -> str:
        return os.path.join(
            self.git_project_location(),
            self.path,
        )

    def get_executable(self) -> str:
        git_project_location = self.git_project_location()
        git_bin = self.executable_path()
        if not os.path.exists(git_bin):
            subprocess.run(  # noqa: S603
                (
                    "git",
                    "clone",
                    "-b",
                    self.tag,
                    self.git_url,
                    git_project_location,
                ),
                check=True,
            )
        elif self.pull:
            subprocess.run(("git", "-C", git_project_location, "pull"), check=False)  # noqa: S603
        return self.make_executable(git_bin)

    def uninstall(self) -> None:
        rm_shim(self.git_project_location())


@dataclass
class ZipTarInstallSource(LinkInstaller):
    package_url: str
    binary: str
    package_name: str | None = None
    rename: str | None = None

    def links(self) -> list[str]:
        return [self.package_url]


def pipecmd(cmd: Sequence[str], input: str) -> str:  # noqa: A002
    return subprocess.run(  # noqa: S603
        cmd,
        input=input,
        check=True,
        stdout=subprocess.PIPE,
        encoding="utf-8",
    ).stdout.strip()


@dataclass
class GronInstaller(LinkInstaller):
    url: str
    gron_pattern: str
    binary: str
    package_name: str  # pyright: ignore [reportIncompatibleVariableOverride]
    rename: str | None = None

    def links(self) -> list[str]:
        response = get_request(self.url)
        pattern = re.compile(self.gron_pattern)
        gron_lines: list[str] = []
        gron_lines.extend(gron(json.loads(response)))

        ret = []

        base_url_path = urlparse(self.url)._replace(params=None, query=None, fragment=None).geturl()  # type:ignore[arg-type]

        for _, value in (
            line.rstrip(";").split(" = ", maxsplit=1) for line in gron_lines if pattern.search(line)
        ):
            _value = value[1:-1]
            if _value.startswith("http"):
                ret.append(_value)
            else:
                ret.append(urljoin(base_url_path, _value))

        return ret


@dataclass
class LinkScraperInstaller(LinkInstaller):
    url: str
    binary: str
    package_name: str  # pyright: ignore [reportIncompatibleVariableOverride]
    rename: str | None = None
    base_url: str | None = None
    link_contains: str | None = None

    def links(self) -> list[str]:
        response = get_request(self.url)
        href_pattern = re.compile(r'href="([^"]+)"')
        base_url = self.base_url or self.url

        ret = []
        for tag in re.findall(r"<a [^>]*>", response.replace("\n", " ")):
            href_match = href_pattern.search(tag)
            if href_match:
                url = href_match.group(1)
                if self.link_contains and self.link_contains not in url:
                    continue
                if url.startswith("http"):
                    ret.append(url)
                else:
                    ret.append(urljoin(base_url, url))
        return ret


# @dataclass
# class PipxInstallSource2(_ToolInstallerBase):
#     package: str
#     command: str | None = None

#     def get_executable(self) -> str:
#         command = self.command or self.package
#         bin_path = os.path.join(TOOL_INSTALLER_CONFIG.BIN_DIR, command)
#         if not os.path.exists(bin_path):
#             pipx_cmd = PIPX_EXECUTABLE_PROVIDER.get_executable()
#             env = {
#                 **os.environ,
#                 'PIPX_DEFAULT_PYTHON': latest_python(),
#                 'PIPX_BIN_DIR': TOOL_INSTALLER_CONFIG.BIN_DIR,
#                 'PIPX_HOME': TOOL_INSTALLER_CONFIG.PIPX_HOME,
#             }
#             subprocess.run(
#                 (
#                     pipx_cmd, 'install', '--force',
#                     self.package,
#                 ), check=True, env=env,
#             )
#         return bin_path


@dataclass
class PipxInstallSource(_ToolInstallerBase):
    PIPX_EXECUTABLE_PROVIDER = ShivInstallSource(package="pipx", command="pipx")
    package: str
    command: str | None = None

    def executable_path(self) -> str:
        return os.path.join(TOOL_INSTALLER_CONFIG.BIN_DIR, self.command or self.package)

    def get_executable(self) -> str:
        bin_path = self.executable_path()
        if not os.path.exists(bin_path):
            pipx_cmd = self.PIPX_EXECUTABLE_PROVIDER.get_executable()
            env = {
                **os.environ,
                "PIPX_DEFAULT_PYTHON": all_pythons()[0],
                "PIPX_BIN_DIR": TOOL_INSTALLER_CONFIG.BIN_DIR,
                "PIPX_HOME": TOOL_INSTALLER_CONFIG.PIPX_HOME,
            }
            subprocess.run(  # noqa: S603
                (
                    pipx_cmd,
                    "install",
                    "--force",
                    self.package,
                ),
                check=True,
                env=env,
            )
        return bin_path

    def uninstall(self) -> None:
        if os.path.exists(self.executable_path()):
            pipx_cmd = self.PIPX_EXECUTABLE_PROVIDER.get_executable()
            subprocess.run(  # noqa: S603
                (
                    pipx_cmd,
                    "uninstall",
                    self.package,
                ),
                check=True,
            )


# @dataclass
# class ScriptInstaller(InternetInstaller):
#     """
#     Download setup script
#     Source script
#     Add Environment variables
#     Command could be executable or bash function

#     """
#     scritp_url: str
#     command: str

#     def get_executable(self) -> str:
#         with download_context(self.scritp_url) as path:
#             self.make_executable(path)
#             subprocess.run([path, '--help'])

#         # return super().get_executable()


@dataclass
class GroupUrlInstallSource(LinkInstaller):
    _links: list[str]
    binary: str
    rename: str | None = None
    package_name: str | None = None

    def links(self) -> list[str]:
        return self._links


# 'rustup':
# 'sdk':


class _RunToolConfig:
    __INSTANCE__: _RunToolConfig | None = None
    _config: configparser.ConfigParser | None = None

    @property
    def config(self) -> configparser.ConfigParser:
        if self._config is None:
            self._config = configparser.ConfigParser()
            self._config.read(x for x in self.config_files() if os.path.exists(x))
        return self._config

    @classmethod
    @lru_cache(maxsize=1)
    def config_files(cls) -> list[str]:
        config_filename = "runtool.ini"
        foo = [
            os.path.realpath(config_filename),
            os.path.expanduser(f"~/.config/runtool/{config_filename}"),
            os.path.dirname(__file__) + f"/{config_filename}",
        ]
        if "RUNTOOL_CONFIG" in os.environ:
            path = os.path.expanduser(os.environ["RUNTOOL_CONFIG"])
            if os.path.exists(path):
                foo.insert(0, path)

        with suppress(Exception):
            import warnings

            warnings.simplefilter("ignore")
            from importlib.resources import path as importlib_path

            with importlib_path(__package__, config_filename) as ipath:  # pyright: ignore[reportArgumentType]
                foo.append(ipath.as_posix())
        return list(dict.fromkeys(foo).keys())

    @lru_cache(maxsize=1)  # noqa: B019
    def tools_descriptions(self) -> dict[str, str]:
        return {
            k: v.get("description", "") for k, v in sorted(self.config.items()) if k != "DEFAULT"
        }

    @lru_cache(maxsize=1)  # noqa: B019
    def tools(self) -> dict_keys[str, None]:
        return dict.fromkeys(sorted(self.config.sections())).keys()

    @cache  # noqa: B019
    def get_executable_provider(self, command: str) -> ExecutableProvider:
        obj = dict(self.config[command])
        class_name = obj.pop("class")
        obj.pop("description", None)
        return getattr(sys.modules[__name__], class_name)(**obj)

    def run(self, command: str, *args: str) -> subprocess.CompletedProcess[str]:
        return self.get_executable_provider(command).run(*args)

    def save(self) -> None:
        with open("/tmp/runtool.ini", "w") as f:  # noqa: S108
            self.config.write(f)

    def __getitem__(self, key: str) -> ExecutableProvider:
        return self.get_executable_provider(key)

    def __contains__(self, key: str) -> bool:
        return key in self.config

    @classmethod
    def get_instance(cls) -> _RunToolConfig:
        if not cls.__INSTANCE__:
            cls.__INSTANCE__ = cls()
        return cls.__INSTANCE__


RUNTOOL_CONFIG = _RunToolConfig.get_instance()

# region: cli


class CLIApp(Protocol):
    COMMAND_NAME: str
    ADD_HELP: bool = True

    @classmethod
    def _short_description(cls) -> str:
        return (cls.__doc__ or cls.__name__).splitlines()[0]

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description=cls._short_description(), add_help=cls.ADD_HELP
        )
        with suppress(Exception):
            if sys.argv[1] == cls.COMMAND_NAME:
                parser.prog = f"{parser.prog} {cls.COMMAND_NAME}"
        for field, _ztype in cls.__annotations__.items():
            if field in ("COMMAND_NAME",):
                continue
            ztype = str(_ztype)
            kwargs = {}

            field_arg = field.replace("_", "-")
            if ztype.startswith("list["):
                kwargs["nargs"] = "+"
            if hasattr(cls, field):
                kwargs["default"] = getattr(cls, field)
                field_arg = f"--{field.replace('_', '-')}"
            if "None" in ztype:
                field_arg = f"--{field.replace('_', '-')}"
            if "Literal" in ztype:
                kwargs["choices"] = eval(ztype.split("Literal")[1].split("[")[1].split("]")[0])  # noqa: S307
            parser.add_argument(field_arg, **kwargs)  # type: ignore[arg-type]
        return parser

    @overload
    @classmethod
    def parse_args(cls, argv: Sequence[str] | None) -> Self: ...

    @overload
    @classmethod
    def parse_args(
        cls, argv: Sequence[str] | None, *, allow_unknown_args: Literal[False]
    ) -> Self: ...

    @overload
    @classmethod
    def parse_args(
        cls, argv: Sequence[str] | None, *, allow_unknown_args: Literal[True]
    ) -> tuple[Self, list[str]]: ...

    @classmethod
    def parse_args(
        cls, argv: Sequence[str] | None = None, *, allow_unknown_args: bool = False
    ) -> tuple[Self, list[str]] | Self:
        return (
            cls.parser().parse_known_args(argv)  # type:ignore[return-value]
            if allow_unknown_args
            else cls.parser().parse_args(argv)
        )

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int: ...


class CLIRun(CLIApp):
    """Run tool."""

    COMMAND_NAME = "run"
    ADD_HELP = False
    tool: str

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description=cls._short_description(), add_help=cls.ADD_HELP
        )
        with suppress(Exception):
            if sys.argv[1] == cls.COMMAND_NAME:
                parser.prog = f"{parser.prog} {cls.COMMAND_NAME}"
        parser.add_argument("tool", choices=RUNTOOL_CONFIG.tools())
        return parser

    @classmethod
    def check_help(cls, argv: Sequence[str] | None = None) -> None:
        help_call = False
        if (argv is None and sys.argv[1] in ("--help", "-h")) or (
            argv is not None and argv[0] in ("--help", "-h")
        ):
            help_call = True

        if help_call:
            help_text = dedent(
                f"""\
                {cls.parser().prog} <tool> [args...]

                {cls._short_description()}

                Available tools:
                """
            ) + "\n".join(
                f"  {tool:30} {description[:100]}"
                for tool, description in RUNTOOL_CONFIG.tools_descriptions().items()
            )

            help_text += dedent(
                f"""\


                Environment variables:
                    TOOL_INSTALLER_OPT_DIR:         {TOOL_INSTALLER_CONFIG.OPT_DIR}
                    TOOL_INSTALLER_BIN_DIR:         {TOOL_INSTALLER_CONFIG.BIN_DIR}
                    TOOL_INSTALLER_PIPX_HOME:       {TOOL_INSTALLER_CONFIG.PIPX_HOME}
                    TOOL_INSTALLER_PACKAGE_DIR:     {TOOL_INSTALLER_CONFIG.PACKAGE_DIR}
                    TOOL_INSTALLER_GIT_PROJECT_DIR: {TOOL_INSTALLER_CONFIG.GIT_PROJECT_DIR}
                    RUNTOOL_CONFIG:                 {os.environ.get("RUNTOOL_CONFIG", "")}
                    """
            )

            help_text += (
                "\n\nConfig files:\n"
                + "\n".join(f"  {x}" for x in RUNTOOL_CONFIG.config_files())
                + "\n"
            )

            print(help_text)
            raise SystemExit(0)

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        cls.check_help(argv)
        args, rest = cls.parse_args(argv, allow_unknown_args=True)
        tool = RUNTOOL_CONFIG[args.tool].get_executable()
        cmd = (tool, *rest)
        os.execvp(cmd[0], cmd)  # noqa: S606


class CLIWhich(CLIRun, CLIApp):
    """Show executable file path."""

    COMMAND_NAME = "which"
    tool: str

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        cls.check_help(argv)
        args = cls.parse_args(argv)
        print(RUNTOOL_CONFIG[args.tool].get_executable())
        return 0


class CLIUninstall(CLIRun, CLIApp):
    """Uninstall tool."""

    COMMAND_NAME = "uninstall"
    tool: str

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        cls.check_help(argv)
        args = cls.parse_args(argv)
        RUNTOOL_CONFIG[args.tool].uninstall()
        return 0


class CLIReinstall(CLIRun, CLIApp):
    """Reinstall tool."""

    COMMAND_NAME = "reinstall"
    tool: str

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        cls.check_help(argv)
        args = cls.parse_args(argv)
        RUNTOOL_CONFIG[args.tool].reinstall()
        return 0


class CLIMultiInstaller(CLIApp):
    """Multi installer."""

    COMMAND_NAME = "multi-installer"

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        _ = cls.parse_args(argv)
        _fzf_executable = (
            shutil.which("fzf") or shutil.which(",fzf") or FZF_EXECUTABLE_PROVIDER.get_executable()
        )

        result = subprocess.run(  # noqa: S603
            (_fzf_executable or "fzf", "--multi"),
            input="\n".join(
                f"{tool:30} {description}"
                for tool, description in RUNTOOL_CONFIG.tools_descriptions().items()
            ),
            text=True,
            stdout=subprocess.PIPE,
            check=False,
        )
        for tool in (line.split(maxsplit=1)[0] for line in result.stdout.splitlines()):
            print("#" * 100)
            print(f" {tool} ".center(100))
            print("#" * 100)
            print(RUNTOOL_CONFIG[tool].get_executable())
        return 0


class CLIFilterLinks(CLIApp):
    """Filter links by system."""

    COMMAND_NAME = "filter-links"
    selector: Literal["filter", "pick"] = "pick"

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        stdin_lines = []
        if not sys.stdin.isatty():
            stdin_lines = [x.strip() for x in sys.stdin]

        args, rest = cls.parse_args(argv, allow_unknown_args=True)
        options = [*stdin_lines, *rest]
        if not options:
            return 1
        if len(options) == 1:
            print(options[0])
            return 0
        service = BestLinkService()
        if args.selector == "pick":
            result = service.pick(options)
            if not result:
                return 1
            print(result)
        elif args.selector == "filter":
            results = service.filter_links(options)
            if not results:
                return 1
            for line in results:
                print(line)
        return 0


class CLILinkInstaller(CLIApp):
    """Install from links."""

    COMMAND_NAME = "link-installer"
    links: list[str]
    binary: str | None = None
    rename: str | None = None
    package_name: str | None = None

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        args = cls.parse_args(argv)

        binary = args.binary
        if not binary:
            counter: Counter[str] = Counter()
            for link in args.links:
                for token in os.path.basename(link).split("-"):
                    counter[token] += 1
            binary = counter.most_common(1)[0][0]

        path = LinkInstaller.install_best(
            InternetInstaller,  # type: ignore[arg-type]
            links=args.links,
            binary=binary,
            rename=args.rename,
            package_name=args.package_name,
        )

        print(path)

        return 0


class GhLinks(CLIApp):
    """Show github release links."""

    COMMAND_NAME = "gh-links"
    url: str

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        args = cls.parse_args(argv)
        gh = _GitHubSource(
            url=args.url,
        )
        for link in gh.links():
            print(link)

        return 0


class GhInstall(CLIApp):
    """Install from github release."""

    COMMAND_NAME = "gh-install"
    url: str
    binary: str | None = None
    rename: str | None = None

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        args = cls.parse_args(argv)
        gh = GithubReleaseLinks(
            url=args.url,
            binary=args.binary,
            rename=args.rename,
        )

        print(gh.get_executable())
        return 0


class CLIFormatIni(CLIApp):
    """Format ini file."""

    COMMAND_NAME = "format-ini"
    file: list[str]
    output: str = "/dev/stdout"

    @classmethod
    def run(cls, argv: Sequence[str] | None = None) -> int:
        args = cls.parse_args(argv)

        config = configparser.ConfigParser()
        config.read(args.file)

        order_config = configparser.ConfigParser()
        dct = {
            k: config[k]
            for k in sorted(
                config.sections(),
                key=lambda x: (
                    config[x].get("class"),
                    config[x].get("url", ""),
                    config[x].get("package"),
                ),
            )
        }
        for v in dct.values():
            if v.get("description", "").strip():
                continue
            clz = v["class"]
            github = ""
            if clz == "PipxInstallSource":
                package = v["package"].split("[")[0]
                if "github" in package:
                    clz = "GithubReleaseLinks"
                    github = package
                else:
                    try:
                        pypi_info = json.loads(
                            get_request(f"https://www.pypi.org/pypi/{package}/json")
                        )
                        description = pypi_info["info"]["summary"]
                        if description:
                            v["description"] = description
                            continue
                        github = github or next(
                            (
                                x
                                for x in pypi_info["info"]["project_urls"].values()
                                if "github" in x
                            ),
                            "",
                        )
                    except Exception:  # noqa: BLE001
                        print(f"Could not get description for {package}", file=sys.stderr)
            if not github and clz in ("GithubScriptInstallSource", "GithubReleaseLinks"):
                github = (
                    v.get("base_url", "https://github.com") + "/" + v["user"] + "/" + v["project"]
                )
            github = github or next((x for x in v.values() if "github" in x), "")
            if github:
                d = _GitHubSource(url=github).description()
                if d:
                    v["description"] = d
                    continue
            else:
                print(f"Could not get description for {v['class']}", file=sys.stderr)

        order_config.read_dict(dct)
        with open(args.output, "w") as f:
            order_config.write(f)

        return 0


def main(argv: Sequence[str] | None = None) -> int:
    if "RUNTOOL_DEV" in os.environ:
        with suppress(ModuleNotFoundError):
            import runtool._additional_cli  # noqa: F401
    dct = {
        x.COMMAND_NAME: x
        for x in CLIApp.__subclasses__()
        # for x in sorted(CLIApp.__subclasses__(), key=lambda x: x.COMMAND_NAME)
    }

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", choices=dct.keys())
    help_text = dedent(
        f"""\
        {parser.prog} <command> [options] [args...]

        Available commands:
        """
    ) + "\n".join(f"  {k:20} {v._short_description()}" for k, v in dct.items())  # noqa: SLF001
    if sys.argv[1] in ("--help", "-h"):
        print(help_text)
        return 0
    args, rest = parser.parse_known_args(argv)
    raise SystemExit(dct[args.command].run(rest))


def test_placeholder() -> None:
    _: type[ExecutableProvider] = GithubReleaseLinks
    _: type[ExecutableProvider] = GronInstaller  # type: ignore[no-redef]
    _: type[ExecutableProvider] = LinkScraperInstaller  # type: ignore[no-redef]
    _: type[ExecutableProvider] = GroupUrlInstallSource  # type: ignore[no-redef]
    _: type[ExecutableProvider] = ZipTarInstallSource  # type: ignore[no-redef]
    _: type[ExecutableProvider] = UrlInstallSource  # type: ignore[no-redef]
    _: type[ExecutableProvider] = ShivInstallSource  # type: ignore[no-redef]
    _: type[ExecutableProvider] = GitProjectInstallSource  # type: ignore[no-redef]
    _: type[ExecutableProvider] = PipxInstallSource  # type: ignore[no-redef]


if __name__ == "__main__":
    raise SystemExit(main())

# endregion: cli
