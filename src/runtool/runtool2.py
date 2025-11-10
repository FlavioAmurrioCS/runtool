from __future__ import annotations

import argparse
import atexit
import functools
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from datetime import timedelta
from functools import cached_property
from glob import glob
from textwrap import dedent
from typing import TYPE_CHECKING
from typing import NamedTuple
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Generator
    from collections.abc import Iterable
    from collections.abc import Mapping
    from http.client import HTTPResponse
    from typing import Any
    from typing import TypeVar
    from typing import Union

    from typing_extensions import Literal
    from typing_extensions import NotRequired
    from typing_extensions import ParamSpec
    from typing_extensions import Protocol
    from typing_extensions import TypedDict
    from typing_extensions import Unpack

    # FileContent = Union[IO[bytes], bytes, str]
    # _FileSpec = Union[
    #     FileContent,
    #     tuple[Optional[str], FileContent],
    # ]
    _Params = Union[dict[str, Any], tuple[tuple[str, Any], ...], list[tuple[str, Any]], None]

    HTTP_METHOD = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"]

    JSON = Union[None, bool, int, float, str, list["JSON"], dict[str, "JSON"]]

    class _CompleteRequestArgs(TypedDict):
        # url: str
        # method: HTTP_METHOD
        # auth: NotRequired[tuple[str, str] | None]
        # cookies: NotRequired[dict[str, str] | None]
        data: NotRequired[Mapping[str, Any] | None]
        # files: NotRequired[Mapping[str, _FileSpec]]
        verify: NotRequired[bool | str]
        headers: NotRequired[Mapping[str, Any] | None]
        json: NotRequired[Any | None]
        params: NotRequired[_Params]
        timeout: NotRequired[float | None]

    T = TypeVar("T")
    R = TypeVar("R")
    P = ParamSpec("P")

    class CacheEntry(TypedDict):
        timestampt: float
        data: object

    class TimeDeltaArg(TypedDict, total=False):
        days: float
        seconds: float
        microseconds: float
        milliseconds: float
        minutes: float
        hours: float
        weeks: float

    class InstallSource(Protocol):
        def links(self) -> list[str]: ...

    class Subcommand(Protocol):
        @staticmethod
        def arg_parser(
            parser: argparse.ArgumentParser | None = None,
        ) -> argparse.ArgumentParser: ...

        @staticmethod
        def run(args: Any, others: list[str] | None = None) -> int:  # noqa: ANN401
            ...

    Argv = Union[list[str], tuple[str, ...], None]

    class InstallationMetadata(TypedDict):
        link: str
        bin_files: list[str]


logger = logging.getLogger("runtool")


################################################################################
# region: Utilities
################################################################################
@contextmanager
def timing_ctx(name: str) -> Generator[None]:
    """Context manager to time a block of code."""
    t0 = time.monotonic_ns()
    try:
        yield
    finally:
        t1 = time.monotonic_ns()
        logger.debug("%s: %d ms", name, (t1 - t0) // 1_000_000)


@dataclass
class RuntoolCache:
    cache_file: str | None = None
    _data: defaultdict[str, dict[str, CacheEntry]] = field(
        init=False, default_factory=lambda: defaultdict(dict), repr=False
    )

    @cached_property
    def data(self) -> dict[str, dict[str, CacheEntry]]:
        logger.debug("Accessing cache data")
        if self.cache_file and os.path.isfile(self.cache_file):
            logger.debug("Loading cache from %s", self.cache_file)
            with timing_ctx("Load cache"), open(self.cache_file) as f:
                self._data.update(json.load(f))
                logger.debug("Loaded cache from %s", self.cache_file)

        atexit.register(self.save)

        return self._data

    def save(self) -> None:
        if self.cache_file:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(self._data, f)

            logger.debug("Saved cache to %s", self.cache_file)

    def __call__(self, **delta: Unpack[TimeDeltaArg]) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Decorator to cache function results to disk for a given time delta."""
        _delta = timedelta(**delta)

        def innner(func: Callable[P, R]) -> Callable[P, R]:
            func_name = func.__qualname__

            @functools.wraps(func)
            def inner2(*args: P.args, **kwargs: P.kwargs) -> R:
                with timing_ctx(f"Cache check for {func_name}"):
                    if int(os.getenv("NO_CACHE", "0")) == 1:
                        logger.debug("NO_CACHE is set, skipping cache.")
                        return func(*args, **kwargs)
                    key = f"{args} {kwargs}"
                    if (
                        int(os.getenv("RE_CACHE", "0")) == 1
                        or func_name not in self.data
                        or key not in self.data[func_name]
                        or (self.data[func_name][key]["timestampt"] + _delta.total_seconds())
                        < time.time()
                    ):
                        logger.debug("Cache miss or expired for %s %s", func_name, key)
                        result: R = func(*args, **kwargs)
                        entry: CacheEntry = {"data": result, "timestampt": time.time()}
                        self.data[func_name][key] = entry
                        return result
                    logger.debug("Cache hit for %s %s", func_name, key)
                    return self.data[func_name][key]["data"]  # type:ignore[return-value]

            return inner2

        return innner


DEFAULT_ROOT = os.path.expanduser("~/opt/runtool")
runtool_cache = RuntoolCache(os.path.join(DEFAULT_ROOT, "runtool_cache.json"))


def request(  # noqa: C901, PLR0912
    url: str, *, method: HTTP_METHOD = "GET", **kwargs: Unpack[_CompleteRequestArgs]
) -> HTTPResponse:
    import urllib.parse
    from collections.abc import Mapping

    final_url = url
    params = kwargs.get("params")
    if params:
        parts = urllib.parse.urlsplit(url)
        base_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)

        if isinstance(params, Mapping):
            extra_pairs: list[tuple[str, str]] = []
            for k, v in params.items():
                if isinstance(v, (list, tuple)):
                    extra_pairs.extend((k, "" if item is None else str(item)) for item in v)
                else:
                    extra_pairs.append((k, "" if v is None else str(v)))
        else:
            extra_pairs = [(k, "" if v is None else str(v)) for k, v in params]

        new_query = urllib.parse.urlencode(
            base_pairs + extra_pairs, doseq=True, encoding="utf-8", errors="strict"
        )
        final_url = urllib.parse.urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                new_query,
                parts.fragment,
            )
        )

    http_method = method.upper()
    import urllib.request

    headers = {k.title(): v for k, v in (kwargs.get("headers") or {}).items()}

    if kwargs.get("data") and kwargs.get("json"):
        msg = "Cannot set both 'data' and 'json'"
        raise ValueError(msg)

    data = kwargs.get("data")

    json_content = kwargs.get("json")
    if json_content is not None:
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        data = json.dumps(json_content).encode("utf-8")  # type: ignore[assignment]

    verify = kwargs.get("verify")
    context: ssl.SSLContext | None

    if verify is None:
        context = None
    elif isinstance(verify, (str, os.PathLike)):
        verify_str = str(verify)
        if os.path.isdir(verify_str):
            context = ssl.create_default_context(capath=verify_str)
        else:
            context = ssl.create_default_context(cafile=verify_str)
    else:
        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(  # noqa: S310
        url=final_url,
        data=data,  # type: ignore[arg-type]
        headers=headers,
        # origin_req_host=None,
        # unverifiable=self.unverifiable,
        method=http_method,
    )
    response: HTTPResponse = urllib.request.urlopen(  # noqa: S310
        url=req,
        timeout=kwargs.get("timeout"),
        # cafile=None, # Deprecated
        # capath=None, # Deprecated
        # cadefault=False, # Deprecated
        context=context,
    )

    return response


################################################################################
# region: Utilities
################################################################################
CONTENT_DISPOTION_PATTERN = re.compile(r'filename\*?=(?:UTF-8\'\')?\"?([^"]+)\"?')


@contextmanager
def download_file(url: str) -> Generator[str]:
    """Download a file from a URL to a temporary location."""
    response = request(url)
    filename: str = next(
        iter(CONTENT_DISPOTION_PATTERN.findall(response.headers["content-disposition"] or "")), None
    ) or os.path.basename(urlparse(url).path)
    with tempfile.TemporaryDirectory(suffix=filename) as tmp_dir:
        full_path = os.path.join(tmp_dir, filename)
        with open(full_path, "wb") as f:
            f.write(response.read())
        yield full_path


@contextmanager
def download_and_extract(url: str) -> Generator[str]:
    """
    Download and extract an archive from a URL.
    Yields the path to the extracted directory.
    """
    with download_file(url) as downloaded_file_path:
        extraction_temp_dir = os.path.dirname(downloaded_file_path)
        opener = (
            zipfile.ZipFile
            if zipfile.is_zipfile(downloaded_file_path)
            else (tarfile.open if tarfile.is_tarfile(downloaded_file_path) else None)
        )
        if not opener:
            # downloaded file is not an archive
            yield extraction_temp_dir
            return
        _basename, e = os.path.splitext(downloaded_file_path)
        if not e:
            # Assume no extension means this is an executable, mainly to handle shiv apps
            yield extraction_temp_dir
            return
        with tempfile.TemporaryDirectory() as extraction_temp_directory:
            try:
                with opener(downloaded_file_path) as f:
                    f.extractall(extraction_temp_directory)  # noqa: S202
            except ValueError:
                yield extraction_temp_dir
                return
            files = os.listdir(extraction_temp_directory)
            if len(files) == 1:
                # if only one folder is extracted, yield that
                fl = os.path.join(extraction_temp_directory, files[0])
                if os.path.isdir(fl):
                    yield fl
                    return
            # since multiple files/folders were extracted or a single non-directory file, yield the temp dir  # noqa: E501
            yield extraction_temp_directory


def ensure_executable(filename: str) -> None:
    """Ensure the given file is executable."""
    os.chmod(filename, os.stat(filename).st_mode | stat.S_IEXEC)


def test_file_executable(filename: str) -> bool:
    """Return True if the given file is an executable binary."""
    original_mode = os.stat(filename).st_mode
    os.chmod(filename, original_mode | stat.S_IEXEC)
    try:
        _result = subprocess.run((filename, "--help"), check=False, capture_output=True)  # noqa: S603
    except OSError:
        return False
    finally:
        os.chmod(filename, original_mode)
    return True


def filter_nonempty(func: Callable[[T], object], iterable: Iterable[T]) -> list[T]:
    """
    Filter an iterable with a function, returning the original iterable if the result is empty.
    """
    original: list[T] = list(iterable)
    ret: list[T] = [x for x in original if func(x)]
    return ret or original


def classify_file(path: str) -> Literal["text", "binary", "ascii"]:
    """
    Return 'ascii', 'text', or 'binary' using a simple content heuristic.
    """
    with open(path, "rb") as f:
        chunk = f.read(65536)  # read a sample
    if not chunk:
        return "text"  # empty files treated as text
    if b"\x00" in chunk:
        return "binary"
    try:
        chunk.decode("ascii")
        return "ascii"  # noqa: TRY300
    except UnicodeDecodeError:
        pass
    try:
        chunk.decode("utf-8")
        return "text"  # noqa: TRY300
    except UnicodeDecodeError:
        pass
    non_printable = sum(b < 9 or (13 < b < 32) for b in chunk)  # allow \t\n\r  # noqa: PLR2004
    return "binary" if non_printable / len(chunk) > 0.30 else "text"  # noqa: PLR2004


@contextmanager
def link_installer_helper(link: str) -> Generator[tuple[str, list[str]]]:
    with download_and_extract(link) as downloaded_directory:
        if not os.path.isdir(downloaded_directory):
            logger.error("Downloaded file is not a directory!")
            raise SystemExit(1)
        bin_files = glob(os.path.join(os.path.join(downloaded_directory, "bin", "*"))) or glob(
            os.path.join(os.path.join(downloaded_directory, "*"))
        )
        bin_files = filter_nonempty(os.path.isfile, bin_files)
        bin_files = filter_nonempty(lambda x: not x.endswith(".1"), bin_files)  # man pages
        bin_files = filter_nonempty(lambda x: "page" not in x, bin_files)
        bin_files = filter_nonempty(lambda x: not x.endswith(".sh"), bin_files)
        bin_files = filter_nonempty(lambda x: classify_file(x) == "binary", bin_files)
        bin_files = list(filter(test_file_executable, bin_files))
        yield downloaded_directory, bin_files


def link_installer(link: str, package_dir: str) -> InstallationMetadata:
    link_hash = hashlib.md5(link.encode("utf-8"), usedforsecurity=False).hexdigest()
    installed_package_directory = os.path.join(package_dir, link_hash)
    package_metadata_file = os.path.join(installed_package_directory, "package.json")
    if os.path.isdir(installed_package_directory) and os.path.isfile(package_metadata_file):
        with open(package_metadata_file) as f:
            return json.load(f)

    with link_installer_helper(link) as (downloaded_package, bin_files):
        if not bin_files:
            msg = "No binary files found in the downloaded package."
            raise RuntimeError(msg)
        os.makedirs(package_dir, exist_ok=True)
        shutil.move(downloaded_package, installed_package_directory)
        _bin_files = [x.replace(downloaded_package, installed_package_directory) for x in bin_files]
        metadata: InstallationMetadata = {
            "link": link,
            "bin_files": _bin_files,
        }
        with open(package_metadata_file, "w") as f:
            json.dump(metadata, f)
        return metadata
        # os.makedirs(BIN_DIR, exist_ok=True)
        # for bin_file in bin_files:
        #     symlink_name = os.path.join(
        #         BIN_DIR, os.path.basename(bin_file).split("_", maxsplit=1)[0]
        #     )
        #     if os.path.exists(symlink_name):
        #         print(f"{symlink_name} already exist")
        #         continue
        #     ensure_executable(bin_file)
        #     os.symlink(bin_file, symlink_name)


class GithubRelease(NamedTuple):
    base_url: str
    owner: str
    repo: str
    tag: str

    @classmethod
    def from_url(cls, url: str) -> GithubRelease:
        resultz = urlparse(url)
        pattern = re.compile(
            r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)(?:/releases/(?:tag/(?P<tag>[^/]+)|latest))?/?$"
        )
        result = pattern.match(resultz.path)
        if not result:
            msg = f"Invalid GitHub URL: {url}"
            raise ValueError(msg)
        dct = result.groupdict()
        return cls(
            base_url=resultz._replace(path="", params="", query="", fragment="").geturl(),
            owner=dct["owner"],
            repo=dct["repo"],
            tag=dct.get("tag") or "",
        )

    @staticmethod
    @runtool_cache(days=1)
    def gh_get_versions(base_url: str, owner: str, repo: str) -> list[str]:
        html_content = request(f"{base_url}/{owner}/{repo}/releases").read().decode()
        release_tags = re.findall(rf"/{owner}/{repo}/releases/tag/(?P<tag>[^'\"]+)", html_content)
        return [*dict.fromkeys(release_tags).keys()]

    @runtool_cache(days=1)
    def links(self) -> list[str]:
        tag = self.tag
        if not tag:
            logger.warning("Tag is empty, fetching latest tag.")
            versions = self.gh_get_versions(self.base_url, self.owner, self.repo)
            if not versions:
                msg = "No versions found."
                raise ValueError(msg)
            tag = versions[0]
        release_assets_html = (
            request(f"{self.base_url}/{self.owner}/{self.repo}/releases/expanded_assets/{tag}")
            .read()
            .decode()
        )
        retrieve_download_links = re.findall(
            rf'(?P<lnk>/{self.owner}/{self.repo}/releases/download/{tag}/[^"<]+)',
            release_assets_html,
        )
        return sorted({f"{self.base_url}{x}" for x in retrieve_download_links})


def filter_links(links: Iterable[str], system: str, machine: str) -> list[str]:
    """Filter links based on system and machine."""
    dct = [(os.path.basename(x).lower(), x) for x in links]
    systems = {
        "darwin": ["darwin", "macos", "apple", "osx"],
    }.get(system.lower(), [])

    dct = filter_nonempty(lambda x: any((i in x[0]) for i in systems), dct)
    machines = {
        "arm64": ["arm64", "aarch64", "universal"],
    }.get(machine.lower(), [])
    dct = filter_nonempty(lambda x: any((i in x[0]) for i in machines), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__(".sha"), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__(".json"), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__(".sbom"), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__(".provenance"), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__(".whl"), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__("32-bit"), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__("lib"), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__("no-web"), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__("denort"), dct)
    dct = filter_nonempty(lambda x: not x[0].__contains__("static"), dct)
    dct = filter_nonempty(lambda x: x[0].__contains__(".tgz"), dct)
    dct = filter_nonempty(lambda x: x[0].__contains__(".tar"), dct)
    dct = filter_nonempty(lambda x: x[0].__contains__(".zip"), dct)
    return [x[1] for x in dct]


class Runtool(NamedTuple):
    packages_dir: str = os.environ.get(
        "RUNTOOL_PACKAGES_DIR", os.path.join(DEFAULT_ROOT, "packages")
    )
    bin_dir: str = os.environ.get("RUNTOOL_BIN_DIR", os.path.join(DEFAULT_ROOT, "bin"))
    system: str = platform.system()
    machine: str = platform.machine()

    def gh_get_bin(self, base_url: str, owner: str, repo: str, tag: str | None = None) -> list[str]:
        gh = GithubRelease(
            base_url=base_url,
            owner=owner,
            repo=repo,
            tag=tag or "",
        )
        return self.install_best_link(gh.links())

    def install_best_link(self, links: list[str]) -> list[str]:
        filtered_links = filter_links(links, self.system, self.machine)
        if not filtered_links:
            return []
        best_link = filtered_links[0]
        return self.install_link(best_link)

    def install_link(self, link: str) -> list[str]:
        return link_installer(link, package_dir=self.packages_dir)["bin_files"]


_runtool = Runtool()
################################################################################
# region: Commands
################################################################################
__PROG__ = None


class FilterLinks(NamedTuple):
    links: list[str]
    machine: str
    system: str

    @staticmethod
    def arg_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
        parser = parser or argparse.ArgumentParser()
        parser.description = "Filter links based on system and machine."
        parser.formatter_class = argparse.RawTextHelpFormatter
        parser.epilog = dedent("""\
        Example:
          %(prog)s <link1> <link2> ...
          cat links.txt | %(prog)s
        """)
        parser.add_argument("links", nargs="*", help="List of links, can be provided via stdin.")
        uname = platform.uname()
        parser.add_argument(
            "--machine",
            default=uname.machine,
            help="Machine architecture. (default: %(default)s)",
        )
        parser.add_argument(
            "--system", default=uname.system, help="Operating system. (default: %(default)s)"
        )
        return parser

    @staticmethod
    def run(args: FilterLinks, others: list[str] | None = None) -> int:  # noqa: ARG004
        for link in filter_links(
            args.links or sys.stdin.readlines(), system=args.system, machine=args.machine
        ):
            print(link)
        return 0


class LinkInstaller(NamedTuple):
    link: str

    @staticmethod
    def arg_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
        parser = parser or argparse.ArgumentParser()
        parser.description = "Install from direct link."
        parser.formatter_class = argparse.RawTextHelpFormatter
        parser.epilog = dedent("""\
        Example:
          %(prog)s <direct download URL>
        """)
        parser.add_argument("link", help="Direct download URL.")
        return parser

    @staticmethod
    def run(args: LinkInstaller, others: list[str] | None = None) -> int:  # noqa: ARG004
        _runtool.install_link(args.link)
        return 0


class GHInstall(NamedTuple):
    link: str

    @staticmethod
    def arg_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
        parser = parser or argparse.ArgumentParser()
        parser.description = "Install from GitHub release."
        parser.formatter_class = argparse.RawTextHelpFormatter
        parser.epilog = dedent("""\
        Example:
          %(prog)s <GitHub release URL>
        """)
        parser.add_argument("link", help="GitHub release URL.")
        return parser

    @staticmethod
    def run(args: GHInstall, others: list[str] | None = None) -> int:  # noqa: ARG004
        gh = GithubRelease.from_url(args.link)
        _runtool.install_best_link(gh.links())
        # Create symlinks in BIN_DIR
        return 0


class Sample(NamedTuple):
    @staticmethod
    def arg_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
        parser = parser or argparse.ArgumentParser()
        parser.description = "<PLACEHOLDER_DESCRIPTION>"
        parser.formatter_class = argparse.RawTextHelpFormatter
        parser.epilog = dedent("""\
        Example:
          %(prog)s <PLACEHOLDER_EXAMPLE>
        """)
        return parser

    @staticmethod
    def run(args: Sample, others: list[str] | None = None) -> int:  # noqa: ARG004
        return 0


CMDS: Mapping[str, str] = {
    "act": "https://github.com/nektos/act",
    "bat": "https://github.com/sharkdp/bat",
    "btop": "https://github.com/aristocratos/btop",
    "charm": "https://github.com/charmbracelet/charm",
    "cli": "https://github.com/cli/cli",
    "code-server": "https://github.com/coder/code-server",
    "compose": "https://github.com/docker/compose",
    "delta": "https://github.com/dandavison/delta",
    "deno": "https://github.com/denoland/deno",
    "dive": "https://github.com/wagoodman/dive",
    "duckdb": "https://github.com/duckdb/duckdb",
    "exa": "https://github.com/ogham/exa",
    "fd": "https://github.com/sharkdp/fd",
    "fzf": "https://github.com/junegunn/fzf",
    "gdu": "https://github.com/dundee/gdu",
    "geckodriver": "https://github.com/mozilla/geckodriver",
    "gron": "https://github.com/tomnomnom/gron",
    "grype": "https://github.com/anchore/grype",
    "gum": "https://github.com/charmbracelet/gum",
    "hadolint": "https://github.com/hadolint/hadolint",
    "helix": "https://github.com/helix-editor/helix",
    "htmlq": "https://github.com/mgdm/htmlq",
    "hyperfine": "https://github.com/sharkdp/hyperfine",
    "jq": "https://github.com/jqlang/jq",
    "k6": "https://github.com/grafana/k6",
    "lazydocker": "https://github.com/jesseduffield/lazydocker",
    "lazygit": "https://github.com/jesseduffield/lazygit",
    "lazynpm": "https://github.com/jesseduffield/lazynpm",
    "miller": "https://github.com/johnkerl/miller",
    "nvim": "https://github.com/neovim/neovim",
    "ollama": "https://github.com/ollama/ollama",
    "rclone": "https://github.com/rclone/rclone",
    "ripgrep": "https://github.com/BurntSushi/ripgrep",
    "ruff": "https://github.com/astral-sh/ruff",
    "sh": "https://github.com/mvdan/sh",
    "shellcheck": "https://github.com/koalaman/shellcheck",
    "shiv": "https://github.com/linkedin/shiv",
    "skate": "https://github.com/charmbracelet/skate",
    "soft-serve": "https://github.com/charmbracelet/soft-serve",
    "taplo": "https://github.com/tamasfe/taplo",
    "termscp": "https://github.com/veeso/termscp",
    "tldr": "https://github.com/isacikgoz/tldr",
    "uv": "https://github.com/astral-sh/uv",
    "vhs": "https://github.com/charmbracelet/vhs",
    "wasmer": "https://github.com/wasmerio/wasmer",
    "watchman": "https://github.com/facebook/watchman",
    "xq": "https://github.com/sibprogrammer/xq",
    "yq": "https://github.com/mikefarah/yq",
    "zellij": "https://github.com/zellij-org/zellij",
}


class Run(NamedTuple):
    cmd: str

    @staticmethod
    def arg_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
        parser = parser or argparse.ArgumentParser(add_help=False)
        parser.description = "Run Command"
        parser.formatter_class = argparse.RawTextHelpFormatter
        parser.epilog = dedent("""\
        Example:
          %(prog)s fzf --help
        """)
        parser.add_argument("cmd", help="Command to run.", choices=list(CMDS.keys()))
        return parser

    @staticmethod
    def run(args: Run, others: list[str] | None = None) -> int:
        link = CMDS[args.cmd]
        gh = GithubRelease.from_url(link)
        links = _runtool.gh_get_bin(base_url=gh.base_url, owner=gh.owner, repo=gh.repo, tag=gh.tag)

        links = filter_nonempty(lambda x: x.split("_")[0] == args.cmd, links)
        bin_path = links[0]
        runtool_cache.save()  # Save cache before exec
        logger.debug("Executing %s", bin_path)
        os.execvp(bin_path, [bin_path] + (others or []))  # noqa: S606


SUBCOMMANDS: dict[str, Subcommand] = {
    "run": Run,
    "gh-install": GHInstall,
    "filter-links": FilterLinks,
    "link-installer": LinkInstaller,
}


class Main(NamedTuple):
    verbose: bool
    command: str

    @staticmethod
    def arg_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
        parser = parser or argparse.ArgumentParser(prog=__PROG__)
        parser.description = "Runtool - A tool to manage command line tools installation."
        parser.formatter_class = argparse.RawTextHelpFormatter
        # parser.epilog = dedent("""\
        # Examples:
        #   %(prog)s gh-install <GitHub release URL>
        # """)

        parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")

        subparsers = parser.add_subparsers(
            dest="command",
            required=True,
            description="Available subcommands",
        )
        for name, cmd in SUBCOMMANDS.items():
            _parser = subparsers.add_parser(name, add_help=cmd not in (Run,))
            cmd.arg_parser(_parser)
        return parser

    @staticmethod
    def run(args: Main, others: list[str] | None = None) -> int:
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        cmd_class = SUBCOMMANDS[args.command or next(iter(SUBCOMMANDS.keys()))]
        return cmd_class.run(args, others)


def runner(cls: type[Subcommand], argv: Argv = None) -> int:
    parser = cls.arg_parser()
    args, others = parser.parse_known_args(argv)
    return cls.run(args, others)


################################################################################
# endregion: Commands
################################################################################
def main(argv: Argv = None) -> int:
    return runner(Main, argv)


if __name__ == "__main__":
    __PROG__ = "python3 -m runtool.runtool2"
    raise SystemExit(main())
