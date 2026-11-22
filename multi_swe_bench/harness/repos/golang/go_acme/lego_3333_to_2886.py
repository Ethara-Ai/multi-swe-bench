import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_MODULE_STEM = re.split(r"[\\/]", __file__)[-1].removesuffix(".py")
_ERA_SLUG = re.search(r"\d+_to_\d+$", _MODULE_STEM).group(0)
_REPO_SLUG = _MODULE_STEM[: _MODULE_STEM.index(_ERA_SLUG)].rstrip("_")
_INTERVAL_NAME = _MODULE_STEM
_BASE_TAG = f"base-{_ERA_SLUG}"

_GO_IMAGE = "golang:1.26.11-bookworm"

_MEMCACHED_CMD = "memcached -d -u nobody -p 11211 -m 64"
_TEST_CMD = "go test -json -count=1 -vet=off -mod=mod -timeout 20m ./..."
_BUILD_CMD = "go build -buildvcs=false ./..."
_COMPILE_CMD = "go test -p 2 -count=1 -vet=off -run '^$' ./..."

_BINARY_EXCLUDES_AWK = (
    "awk '/^diff --git /{p=$NF} /^Binary files /"
    "{print \"--exclude=\" substr(p,3)}'"
)


_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
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

go mod download

__BUILD_CMD__
__COMPILE_CMD__
echo DEPS_OK
"""


_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true

__MEMCACHED_CMD__

cd /home/__REPO__
__TEST_CMD__
"""


_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true

__MEMCACHED_CMD__

cd /home/__REPO__
test_excludes=$(__BINARY_EXCLUDES_AWK__ /home/test.patch | tr '\\n' ' ')
git apply --whitespace=nowarn $test_excludes /home/test.patch
__TEST_CMD__
"""


_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true

__MEMCACHED_CMD__

cd /home/__REPO__
test_excludes=$(__BINARY_EXCLUDES_AWK__ /home/test.patch | tr '\\n' ' ')
git apply --whitespace=nowarn $test_excludes /home/test.patch
fix_excludes=$(__BINARY_EXCLUDES_AWK__ /home/fix.patch | tr '\\n' ' ')
git apply --whitespace=nowarn $fix_excludes /home/fix.patch
__TEST_CMD__
"""


_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

__PROXY_ARGS__

__ENV_BLOCK__

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

__CERT_SYMLINKS__

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    build-essential \\
    pkg-config \\
    memcached \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


_PRUNE = """RUN set -eux; \\
    git checkout --detach "__BASE_SHA__"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_MODULE_PREFIX_RE = re.compile(r"^[^/\s]+\.[^/\s]+/[^/\s]+/[^/\s]+(?:/v\d+)?/")
_TEXT_RESULT_RE = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
_PACKAGE_MARKER = "PACKAGE_BUILD"


def _short_package(package: str) -> str:
    short = _MODULE_PREFIX_RE.sub("", package, count=1)
    return short if short else package


def _test_id(package: str, test: str) -> str:
    if not package:
        return test
    return f"{_short_package(package)}::{test}"


def lego_parse_log(test_log: str) -> TestResult:
    log = _ANSI_RE.sub("", test_log or "")

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    package_actions: dict[str, str] = {}
    packages_with_tests: set[str] = set()
    json_events = 0

    for line in log.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{") or '"Action"' not in stripped:
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue

        action = event.get("Action") or ""
        package = event.get("Package") or event.get("ImportPath") or ""
        package = package.split(" ", 1)[0]
        test = event.get("Test") or ""
        json_events += 1

        if test:
            name = _test_id(package, test)
            if package:
                packages_with_tests.add(package)
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            elif action == "skip":
                skipped_tests.add(name)
            continue

        if package and action in ("pass", "fail", "skip", "build-fail"):
            package_actions[package] = action

    if json_events == 0:
        for line in log.splitlines():
            match = _TEXT_RESULT_RE.match(line)
            if not match:
                continue
            status = match.group(1)
            name = match.group(2)
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

    for package, action in package_actions.items():
        if package in packages_with_tests:
            continue
        if action in ("fail", "build-fail"):
            failed_tests.add(_test_id(package, _PACKAGE_MARKER))

    skipped_tests -= failed_tests
    passed_tests -= failed_tests
    passed_tests -= skipped_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class LegoEraImageBase3333To2886(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return _GO_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def _merged_env_block(self) -> str:
        assignments = [
            line[len("ENV ") :]
            for line in self.global_env.splitlines()
            if line.startswith("ENV ")
        ]
        assignments.extend(
            (
                "CGO_ENABLED=0",
                "GOTOOLCHAIN=auto",
                "GOPATH=/root/go",
                "CI=true",
            )
        )
        return DockerfileEnhancer._ENV_BLOCK + "".join(
            " \\\n    " + assignment for assignment in assignments
        )

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", self._merged_env_block())
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class LegoEraImageDefault3333To2886(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return LegoEraImageBase3333To2886(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__BINARY_EXCLUDES_AWK__", _BINARY_EXCLUDES_AWK)
            .replace("__MEMCACHED_CMD__", _MEMCACHED_CMD)
            .replace("__TEST_CMD__", _TEST_CMD)
            .replace("__BUILD_CMD__", _BUILD_CMD)
            .replace("__COMPILE_CMD__", _COMPILE_CMD)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("The dependency of the default image must be an image.")

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        sections = [f"FROM {image.image_full_name()}"]
        for part in (
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard",
            f"RUN git checkout {self.pr.base.sha}",
            copy_commands,
            "RUN bash /home/prepare.sh",
            self._render(_PRUNE),
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("go-acme", _INTERVAL_NAME)
class LEGO_3333_TO_2886(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LegoEraImageDefault3333To2886(self.pr, self._config)

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
        return lego_parse_log(test_log)


Instance.register("go-acme", _REPO_SLUG)(LEGO_3333_TO_2886)
