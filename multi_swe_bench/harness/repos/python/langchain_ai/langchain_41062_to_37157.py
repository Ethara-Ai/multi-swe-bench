from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "langchain_41062_to_37157"
_BASE_IMAGE = "python:3.12-slim-bookworm"
_BASE_TAG = "base-41062_to_37157"
_PYTHON_VERSION = "3.12"
_UV_VERSION = "0.12.14"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_BINARY_STUB_RE = re.compile(r"^Binary files .* differ$", re.M)
_UNIT_TEST_RE = re.compile(
    r"^(?P<package>libs/(?:partners/)?[^/]+)/tests/unit_tests/(?:.*/)?test_[^/]*\.py$"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STATUS = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
_ROOT_RE = re.compile(r"^MSB_ROOT (?P<root>\S+)$")
_VERBOSE_RE = re.compile(
    rf"^(?P<name>\S+\.py::.+?)\s+(?P<status>{_STATUS})(?:\s+\(.*\))?(?:\s+\[\s*\d+%\])?$"
)
_SUMMARY_RE = re.compile(
    rf"^(?P<status>{_STATUS})\s+(?P<name>\S+\.py(?:::.+?)?)(?:\s+-\s.*)?$"
)


def _binary_stub_paths(*patches: str) -> list[str]:
    paths: list[str] = []
    for patch in patches:
        text = patch or ""
        headers = list(_DIFF_HEADER_RE.finditer(text))
        for index, header in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            body = text[header.end():end]
            if _BINARY_STUB_RE.search(body) and "GIT binary patch" not in body:
                for path in (header.group(1), header.group(2)):
                    if path not in paths:
                        paths.append(path)
    return paths


def _apply_command(*patches: tuple[str, str]) -> str:
    excludes = _binary_stub_paths(*(content for _, content in patches))
    parts = ["git apply --whitespace=nowarn"]
    parts.extend(f"--exclude={shlex.quote(path)}" for path in excludes)
    parts.extend(f"/home/{name}" for name, _ in patches)
    return " ".join(parts)


def _packages(pr: PullRequest) -> list[str]:
    packages: list[str] = []
    for header in _DIFF_HEADER_RE.finditer(pr.test_patch or ""):
        match = _UNIT_TEST_RE.match(header.group(2))
        if match and match.group("package") not in packages:
            packages.append(match.group("package"))
    return sorted(packages)


def _test_body(pr: PullRequest) -> str:
    blocks = ["rc=0"]
    for package in _packages(pr):
        blocks.append(
            f"""echo "MSB_ROOT {package}"
(
    cd {package}
    .venv/bin/python -m pytest -p msb_offline_dns tests/unit_tests -v -rA --tb=no --color=no -p no:cacheprovider --continue-on-collection-errors --disable-socket --allow-unix-socket
) || rc=$?"""
        )
    blocks.append('exit "$rc"')
    return "\n\n".join(blocks) + "\n"


_OFFLINE_DNS_PLUGIN = """import ipaddress
import socket

_real_getaddrinfo = socket.getaddrinfo
_real_gethostbyname = socket.gethostbyname
_real_gethostbyname_ex = socket.gethostbyname_ex
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", socket.gethostname().lower()}


def _is_local(host):
    if host is None:
        return True
    name = host.decode() if isinstance(host, bytes) else str(host)
    name = name.strip("[]").split("%")[0].rstrip(".").lower()
    if name in _LOCAL_NAMES or name == "":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _blocked():
    return socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")


def _getaddrinfo(host, *args, **kwargs):
    if _is_local(host):
        return _real_getaddrinfo(host, *args, **kwargs)
    raise _blocked()


def _gethostbyname(host):
    if _is_local(host):
        return _real_gethostbyname(host)
    raise _blocked()


def _gethostbyname_ex(host):
    if _is_local(host):
        return _real_gethostbyname_ex(host)
    raise _blocked()


def pytest_configure(config):
    socket.getaddrinfo = _getaddrinfo
    socket.gethostbyname = _gethostbyname
    socket.gethostbyname_ex = _gethostbyname_ex
"""


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

export CI=true
export UV_LINK_MODE=copy
export UV_PYTHON=__PYTHON_VERSION__

for package in __PACKAGES__; do
    (
        cd "/home/__REPO__/$package"
        uv sync --group test --frozen
    )
done

uv cache clean

mkdir -p /home/msb-tiktoken-cache
curl -fsSL --retry 5 -o /home/msb-tiktoken-cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4 https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
curl -fsSL --retry 5 -o /home/msb-tiktoken-cache/fb374d419588a4632f3f557e76b4b70aebbca790 https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken

for package in __PACKAGES__; do
    (
        cd "/home/__REPO__/$package"
        module="$(.venv/bin/python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['name'].replace('-', '_'))")"
        .venv/bin/python -c "import pytest, pytest_socket, $module"
        PYTHONPATH=/home/msb_pytest_plugins .venv/bin/python -c "import msb_offline_dns"
        .venv/bin/python -m pytest --version
    )
done
test -s /home/msb-tiktoken-cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4
test -s /home/msb-tiktoken-cache/fb374d419588a4632f3f557e76b4b70aebbca790

echo "DEPS_OK"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1
export TIKTOKEN_CACHE_DIR=/home/msb-tiktoken-cache
export HF_HUB_OFFLINE=1
export PYTHONPATH=/home/msb_pytest_plugins

cd /home/__REPO__
"""


class Langchain41062To37157ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} {_INTERVAL} instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{DockerfileEnhancer._MITM_MOUNT}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv=={_UV_VERSION}" && uv --version

{code}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Langchain41062To37157ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return Langchain41062To37157ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__PYTHON_VERSION__", _PYTHON_VERSION)
            .replace("__PACKAGES__", " ".join(shlex.quote(p) for p in _packages(self.pr)))
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        body = _test_body(self.pr)
        test_apply = _apply_command(("test.patch", self.pr.test_patch))
        fix_apply = _apply_command(
            ("test.patch", self.pr.test_patch), ("fix.patch", self.pr.fix_patch)
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{body}"),
            File(".", "test-run.sh", f"{header}\n{test_apply}\n\n{body}"),
            File(".", "fix-run.sh", f"{header}\n{fix_apply}\n\n{body}"),
            File("msb_pytest_plugins", "msb_offline_dns.py", _OFFLINE_DNS_PLUGIN),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = ""
        for file in self.files():
            if file.dir == ".":
                copy_commands += f"COPY {file.name} /home/\n"
            else:
                copy_commands += f"COPY {file.dir}/{file.name} /home/{file.dir}/\n"
        hardening = Image._HARDENING_BLOCK.replace('"${BASE_COMMIT}"', self.pr.base.sha)

        return f"""FROM {image.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}
"""


def _qualified(root: str, name: str) -> str:
    return name if root == "." else f"{root}/{name}"


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    root = "."

    for raw_line in _ANSI_RE.sub("", test_log).replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        marker = _ROOT_RE.match(line)
        if marker:
            root = marker.group("root")
            continue
        match = _VERBOSE_RE.match(line)
        if not match:
            match = _SUMMARY_RE.match(line)
            if match and match.group("name").count("[") > match.group("name").count("]"):
                continue
        if not match:
            continue
        name = match.group("name")
        if "::" not in name:
            name = f"{name}::<collection error>"
        name = _qualified(root, name)
        status = match.group("status")
        if status in ("PASSED", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    skipped_tests -= passed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("langchain-ai", _INTERVAL)
class LANGCHAIN_41062_TO_37157(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Langchain41062To37157ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)
