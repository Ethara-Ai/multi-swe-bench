from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "cloud_custodian_11259_to_10610"
_BASE_IMAGE = "python:3.12-slim-bookworm"
_BASE_TAG = "base-11259_to_10610"
_UV_VERSION = "0.13.9"

# uv workspace install used by upstream (Makefile `install`), minus the lint group.
_UV_SYNC = (
    "uv sync --frozen --all-packages --group dev --group addons "
    "--extra gcp --extra azure --python /usr/local/bin/python3"
)

_ENV_SH = """export CI=true
export UV_LINK_MODE=copy
export UV_PYTHON_DOWNLOADS=never
export UV_NO_PROGRESS=1
export PYTHONDONTWRITEBYTECODE=1"""

# amd64 only: turn uv.lock + workspace pyproject files into pip requirements (editable
# workspace members with the gcp/azure extras, dev/addons groups) and == constraints.
_LOCK_TO_PIP_PY = """import sys
import tomllib

from pip._vendor.packaging.markers import Marker
from pip._vendor.packaging.utils import canonicalize_name

EXTRAS = {"gcp", "azure"}
GROUPS = ["dev", "addons"]

root = tomllib.load(open("pyproject.toml", "rb"))
lock = tomllib.load(open("uv.lock", "rb"))



def group(pyproject, name):
    out = []
    for entry in pyproject.get("dependency-groups", {}).get(name, []):
        if isinstance(entry, dict):
            out += group(pyproject, entry["include-group"])
        else:
            out.append(entry)
    return out


# uv sync --all-packages --group X installs group X of the root and of every member.
members = {canonicalize_name(root["project"]["name"])}
requirements = ["-e ."]
for name in GROUPS:
    requirements += group(root, name)
for path in root["tool"]["uv"]["workspace"]["members"]:
    pyproject = tomllib.load(open(f"{path}/pyproject.toml", "rb"))
    project = pyproject["project"]
    members.add(canonicalize_name(project["name"]))
    extras = sorted(EXTRAS & set(project.get("optional-dependencies", {})))
    requirements.append(f"-e ./{path}" + (f"[{','.join(extras)}]" if extras else ""))
    for name in GROUPS:
        requirements += group(pyproject, name)

constraints = {}
for package in lock["package"]:
    name = canonicalize_name(package["name"])
    if name in members or "registry" not in package.get("source", {}):
        continue
    markers = package.get("resolution-markers")
    if markers and not any(Marker(m).evaluate() for m in markers):
        continue
    constraints[name] = package["version"]

open(sys.argv[1], "w").write("\\n".join(requirements) + "\\n")
open(sys.argv[2], "w").write("\\n".join(f"{n}=={v}" for n, v in sorted(constraints.items())) + "\\n")
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
set -e

__ENV__

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin "__BASE_SHA__"
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

if [ "$(uname -m)" = x86_64 ]; then
    # uv >= 0.5 segfaults under QEMU amd64 emulation (multi-arch builds on arm64 hosts).
    # Install the same uv.lock pins with pip instead; the arm64 path below is unchanged.
__PIP_PATCH_APPLY__
    python3 -m venv .venv
    python3 /home/lock_to_pip.py /tmp/requirements.txt /tmp/constraints.txt
    .venv/bin/pip install --no-cache-dir --no-compile -c /tmp/constraints.txt -r /tmp/requirements.txt
__PIP_PATCH_REVERT__
else
__SYNC__ || true
__PATCH_DEPS__
uv cache clean || true
fi

.venv/bin/python -c "import c7n, c7n_gcp, c7n_azure, pytest, xdist, placebo, vcr, vcr_unittest, freezegun; print('DEPS_OK')"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
"""

_BODY_SH = """
TARGETS=(__TARGETS__)
RUN_TARGETS=()
for target in "${TARGETS[@]}"; do
    if [ -f "$target" ]; then
        RUN_TARGETS+=("$target")
    fi
done
if [ ${#RUN_TARGETS[@]} -eq 0 ]; then
    echo "no target test files present"
    exit 0
fi

set -a
if [ -f test.env ]; then
    . ./test.env
fi
set +a
unset AWS_PROFILE

.venv/bin/python -m pytest -n 4 -p no:cacheprovider -p no:sugar -v -rA --tb=short "${RUN_TARGETS[@]}"
"""

_BINARY_NO_DATA_RE = re.compile(r"^Binary files (?:a/\S+|/dev/null) and b/(\S+) differ$", re.M)
_DIFF_FILE_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)

_RESULT_RE = re.compile(
    r"^(?:\[gw\d+\] \[\s*\d+%\] )?(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+?\.py::.+?)(?: - .*)?\s*$"
)
_RESULT_SUFFIX_RE = re.compile(r"^(\S+?\.py::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STATUS = {
    "PASSED": "pass",
    "XPASS": "pass",
    "FAILED": "fail",
    "ERROR": "fail",
    "SKIPPED": "skip",
    "XFAIL": "skip",
}


def _apply(pr: PullRequest) -> str:
    paths = sorted(
        set(_BINARY_NO_DATA_RE.findall(pr.test_patch or ""))
        | set(_BINARY_NO_DATA_RE.findall(pr.fix_patch or ""))
    )
    excludes = "".join(f" --exclude={shlex.quote(path)}" for path in paths)
    return f"git apply --whitespace=nowarn{excludes}"


def _apply_with_fallback(pr: PullRequest, patches: str) -> str:
    apply = _apply(pr)
    steps = "\n".join(
        f"    {apply} {p} || {{ git add -A && {apply} --3way {p}; }}" for p in patches.split()
    )
    return f"if ! {apply} {patches}; then\n    git checkout -- .\n    git clean -fdq\n{steps}\nfi"


# PRs whose test_patch only adds new test files have nothing to run before the test patch.
# Also run the existing tests of the source module the fix changes, in all three stages,
# so the baseline (run.sh) stage has tests too.
_RELATED_TESTS = {
    11229: ["tools/c7n_azure/tests_azure/tests_resources/test_storage.py"],
    11090: ["tests/test_cwa.py"],
    11053: [
        "tools/c7n_azure/tests_azure/tests_resources/test_dnszone.py",
        "tools/c7n_azure/tests_azure/tests_resources/test_record_set.py",
        "tools/c7n_azure/tests_azure/tests_resources/test_armresource.py",
    ],
}


def _targets(pr: PullRequest) -> str:
    targets = []
    for path in _DIFF_FILE_RE.findall(pr.test_patch or ""):
        name = path.rsplit("/", 1)[-1]
        if name.startswith("test_") and name.endswith(".py") and "/terraform/" not in path:
            if path not in targets:
                targets.append(path)
    for path in _RELATED_TESTS.get(pr.number, []):
        if path not in targets:
            targets.append(path)
    return " ".join(shlex.quote(t) for t in targets)


def _touches_lock(pr: PullRequest) -> bool:
    touched = _DIFF_FILE_RE.findall(pr.fix_patch or "") + _DIFF_FILE_RE.findall(pr.test_patch or "")
    return any(p.endswith("uv.lock") for p in touched)


def _pip_patch(pr: PullRequest, revert: bool) -> str:
    # amd64 pip path: install from the fix's uv.lock (patches applied), then restore the tree.
    if not _touches_lock(pr):
        return ""
    if revert:
        return "    git checkout -- .\n    git clean -fdq -e .venv"
    return f"    {_apply(pr)} /home/test.patch /home/fix.patch"


def _patch_deps(pr: PullRequest) -> str:
    # Fix patches that change uv.lock add packages; install them up front (inexact, so the
    # base environment is kept) so test runs need no network, then restore the work tree.
    touched = _DIFF_FILE_RE.findall(pr.fix_patch or "") + _DIFF_FILE_RE.findall(pr.test_patch or "")
    if not any(p.endswith("uv.lock") for p in touched):
        return ""
    apply = _apply(pr)
    return (
        f"{apply} /home/test.patch /home/fix.patch\n"
        f"{_UV_SYNC} --inexact || true\n"
        "git checkout -- .\n"
        "git clean -fdq -e .venv"
    )


class CloudCustodian11259To10610ImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    make \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv=={_UV_VERSION}"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class CloudCustodian11259To10610ImageDefault(Image):
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
        return CloudCustodian11259To10610ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__ENV__", _ENV_SH)
            .replace("__SYNC__", _UV_SYNC)
            .replace("__PATCH_DEPS__", _patch_deps(self.pr))
            .replace("__PIP_PATCH_APPLY__", _pip_patch(self.pr, revert=False))
            .replace("__PIP_PATCH_REVERT__", _pip_patch(self.pr, revert=True))
            .replace("__TARGETS__", _targets(self.pr))
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        body = self._script(_BODY_SH)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "lock_to_pip.py", _LOCK_TO_PIP_PY),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{body}"),
            File(".", "test-run.sh", f"{header}\n{_apply_with_fallback(self.pr, '/home/test.patch')}\n{body}"),
            File(
                ".",
                "fix-run.sh",
                f"{header}\n{_apply_with_fallback(self.pr, '/home/test.patch /home/fix.patch')}\n{body}",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {image.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    buckets = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}

    for line in _ANSI_RE.sub("", test_log).replace("\r", "\n").split("\n"):
        line = line.strip()
        match = _RESULT_RE.match(line)
        if match:
            buckets[_STATUS[match.group(1)]].add(match.group(2))
            continue
        match = _RESULT_SUFFIX_RE.match(line)
        if match:
            buckets[_STATUS[match.group(2)]].add(match.group(1))

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


@Instance.register("cloud-custodian", _INTERVAL)
class CLOUD_CUSTODIAN_11259_TO_10610(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return CloudCustodian11259To10610ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)
