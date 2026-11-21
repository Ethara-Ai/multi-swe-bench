from __future__ import annotations

import html
import re
import shlex
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "pydantic"
_REPO = "pydantic-ai"
_ERA_MATCH = re.fullmatch(r"pydantic_ai_(\d+)_to_(\d+)", Path(__file__).stem)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like pydantic_ai_<hi>_to_<lo>.py")
_INTERVAL = f"{_REPO}_{_ERA_MATCH.group(1)}_to_{_ERA_MATCH.group(2)}"
_BASE_TAG = f"base-{_ERA_MATCH.group(1)}_to_{_ERA_MATCH.group(2)}"
_BASE_IMAGE = "python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df"
_UV_VERSION = "0.12.5"
_DEFAULT_SYNC_ARGS = "--frozen --all-extras --all-packages"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
_TEST_MODULE_RE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CDATA_RE = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_REPORT_RE = re.compile(r"^##PAI_REPORT## \S+[ \t]*$", re.M)
_CASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
_ATTR_RE = re.compile(r'\b(file|classname|name)="([^"]*)"')
_FAIL_RE = re.compile(r"<(?:failure|error)\b")
_SKIP_RE = re.compile(r"<skipped\b")


_NEW_FILE_RE = re.compile(r"^diff --git a/\S+ b/(\S+)\nnew file mode", re.M)


def _test_modules(test_patch: str) -> tuple[list[str], list[str]]:
    seen: dict[str, None] = {}
    for path in _DIFF_HEADER_RE.findall(test_patch or ""):
        if _TEST_MODULE_RE.search(path):
            seen[path] = None
    new = set(_NEW_FILE_RE.findall(test_patch or ""))
    return list(seen), [path for path in seen if path in new]


_ENV_SH = """export CI=true
export COLUMNS=150
export PYTHONDONTWRITEBYTECODE=1
export UV_FROZEN=1
export UV_PYTHON=3.12
export UV_PYTHON_PREFERENCE=only-system
export UV_LINK_MODE=copy
export BLOCKBUSTER_ENABLED=false
export PATH="/home/__REPO__/.venv/bin:$PATH"
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

_COLLECT_TARGETS_SH = """collect_targets() {
    local -A requested=()
    local -A expand=()
    local candidate dir sibling found depth
    for candidate in __TEST_MODULES__; do
        requested["${candidate}"]=1
    done
    for candidate in __NEW_MODULES__; do
        expand["${candidate}"]=1
    done
    for candidate in __TEST_MODULES__; do
        if [ -f "${candidate}" ]; then
            printf '%s\\n' "${candidate}"
        fi
        [ -n "${expand[${candidate}]:-}" ] || continue
        dir="$(dirname "${candidate}")"
        depth=0
        while [ "${dir}" != "." ] && [ "${dir}" != "tests" ] && [ "${depth}" -lt 2 ]; do
            found=0
            if [ -d "${dir}" ]; then
                for sibling in "${dir}"/test_*.py "${dir}"/*_test.py; do
                    [ -f "${sibling}" ] || continue
                    [ -z "${requested[${sibling}]:-}" ] || continue
                    printf '%s\\n' "${sibling}"
                    found=1
                done
            fi
            [ "${found}" -eq 0 ] || break
            dir="$(dirname "${dir}")"
            depth=$((depth + 1))
        done
    done | sort -u
}
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

sync_args="$(sed -n '/^install:/,/^[^[:space:]]/{s/^[[:space:]]*uv sync //p;}' Makefile | head -1)"
[ -n "$sync_args" ] || sync_args="__DEFAULT_SYNC_ARGS__"
printf '%s\\n' "$sync_args" > /home/uv_sync_args
echo "uv sync $sync_args"

for attempt in 1 2 3; do
    UV_NO_CACHE=1 uv sync $sync_args && break || true
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: uv sync failed 3 times"; exit 1; }
    echo "prepare.sh: uv sync attempt $attempt failed; retrying in 30s"
    sleep 30
done

__COLLECT_TARGETS__
mapfile -t TARGETS < <(collect_targets)

python -c "import pydantic_ai, pydantic_graph, pydantic_evals, pytest, xdist"
if [ "${#TARGETS[@]}" -gt 0 ]; then
    python -m pytest --collect-only -q -p no:cacheprovider "${TARGETS[@]}" > /tmp/pai-collect.log 2>&1 || { tail -50 /tmp/pai-collect.log; exit 1; }
fi
echo "DEPS_OK"
"""

_TEST_BLOCK = """__COLLECT_TARGETS__
mapfile -t TARGETS < <(collect_targets)
echo "Test targets: ${#TARGETS[@]}"
printf '  %s\\n' "${TARGETS[@]}"

if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "No test targets present at this stage"
    exit 0
fi

if ! git diff --quiet HEAD -- uv.lock '*pyproject.toml'; then
    uv sync $(cat /home/uv_sync_args) --inexact
fi

rm -f /tmp/pai-junit.xml
rc=0
python -m pytest -n 4 --dist=loadgroup -p no:cacheprovider --color=no \\
    -o junit_family=xunit1 --junitxml=/tmp/pai-junit.xml \\
    "${TARGETS[@]}" || rc=$?

echo "##PAI_RESULTS_BEGIN##"
if [ -s /tmp/pai-junit.xml ]; then
    echo "##PAI_REPORT## junit"
    sed '/<properties>/,/<\\/properties>/d' /tmp/pai-junit.xml
    echo
fi
echo "##PAI_RESULTS_END##"

if [ ! -s /tmp/pai-junit.xml ]; then
    echo "pytest exited with code $rc without writing /tmp/pai-junit.xml" >&2
    exit "$(( rc == 0 ? 1 : rc ))"
fi
"""

_RUN_HEADER_SH = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
"""


class PydanticAiImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "uv=={_UV_VERSION}"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class PydanticAiImageDefault(Image):
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
        return PydanticAiImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        modules, new_modules = _test_modules(self.pr.test_patch)
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__COLLECT_TARGETS__", _COLLECT_TARGETS_SH.rstrip("\n"))
            .replace("__ENV__", _ENV_SH)
            .replace("__TEST_MODULES__", " ".join(shlex.quote(path) for path in modules))
            .replace("__NEW_MODULES__", " ".join(shlex.quote(path) for path in new_modules))
            .replace("__DEFAULT_SYNC_ARGS__", _DEFAULT_SYNC_ARGS)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        header = self._script(_RUN_HEADER_SH)
        test_block = self._script(_TEST_BLOCK)
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{test_block}"),
            File(".", "test-run.sh", f"{header}\n{test_apply}\n\n{test_block}"),
            File(".", "fix-run.sh", f"{header}\n{fix_apply}\n\n{test_block}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        hardening = (
            Image._HARDENING_BLOCK.replace('"${BASE_COMMIT}"', self.pr.base.sha)
            .replace("git gc --prune=now --aggressive", "git gc --prune=now")
        )

        return f"""FROM {image.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    clean = _ANSI_RE.sub("", test_log)
    clean = _CDATA_RE.sub("", clean)

    statuses: dict[str, set[str]] = {}
    for body in _REPORT_RE.split(clean)[1:]:
        for case in _CASE_RE.finditer(body):
            attrs = {key: html.unescape(value) for key, value in _ATTR_RE.findall(case.group(1) or "")}
            file, classname, name = attrs.get("file"), attrs.get("classname", ""), attrs.get("name")
            if not file or not name:
                continue
            module = file[: -len(".py")].replace("/", ".") if file.endswith(".py") else file
            scope = classname[len(module) + 1 :] if classname.startswith(module + ".") else ""
            test_id = f"{file}::{scope.replace('.', '::')}::{name}" if scope else f"{file}::{name}"
            inner = case.group(3) or ""
            if _FAIL_RE.search(inner):
                status = "failed"
            elif _SKIP_RE.search(inner):
                status = "skipped"
            else:
                status = "passed"
            statuses.setdefault(test_id, set()).add(status)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    for test_id, seen in statuses.items():
        if "failed" in seen:
            failed_tests.add(test_id)
        elif "passed" in seen:
            passed_tests.add(test_id)
        else:
            skipped_tests.add(test_id)

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


@Instance.register(_ORG, _INTERVAL)
class PYDANTIC_AI(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PydanticAiImageDefault(self.pr, self._config)

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
