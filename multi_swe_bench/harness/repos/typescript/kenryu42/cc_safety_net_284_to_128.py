from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "cc_safety_net_284_to_128"
_BASE_IMAGE = "node:22.23.2-bookworm-slim"
_BASE_TAG = "base-284_to_128"
_BUN_VERSION = "1.4.2"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FILE_RE = re.compile(r"^(?P<file>[^\s()]+\.[cm]?[jt]sx?):$")
_RESULT_RE = re.compile(
    r"^(?:\((?P<word>pass|fail|skip|todo)\)|(?P<mark>[✓✔✗✘×»✎]))\s+(?P<name>.*?)(?:\s+\[\d+(?:\.\d+)?m?s\])?$"
)
_SUMMARY_RE = re.compile(r"^\d+ tests? [a-z]+:$")

_TEST_CMD = "bun test tests --timeout 30000 --path-ignore-patterns 'tests/e2e-live/**'\n"

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

export CI=true

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

bun install --frozen-lockfile

test "$(bun --version)" = "__BUN_VERSION__"
node --version
bun -e "await import('cc-safety-net'); await import('@opencode-ai/plugin'); await import('effect'); await import('./src/entries/index.ts'); console.log('DEPS_OK')"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test

cd /home/__REPO__
"""


class CcSafetyNet284To128ImageBase(Image):
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

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 \\
    && rm -rf /var/lib/apt/lists/*

{DockerfileEnhancer._MITM_MOUNT}

RUN npm install -g "bun@{_BUN_VERSION}"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class CcSafetyNet284To128ImageDefault(Image):
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
        return CcSafetyNet284To128ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__BUN_VERSION__", _BUN_VERSION)
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{_TEST_CMD}"),
            File(".", "test-run.sh", f"{header}\n{test_apply}\n\n{_TEST_CMD}"),
            File(".", "fix-run.sh", f"{header}\n{fix_apply}\n\n{_TEST_CMD}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        hardening = Image._HARDENING_BLOCK.replace('"${BASE_COMMIT}"', self.pr.base.sha)

        return f"""FROM {image.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    test_log = _ANSI_RE.sub("", test_log)
    current_file: str | None = None
    occurrences: dict[str, int] = {}
    for raw_line in test_log.splitlines():
        line = raw_line.strip()
        file_match = _FILE_RE.match(line)
        if file_match:
            current_file = file_match.group("file")
            continue
        if _SUMMARY_RE.match(line):
            current_file = None
            continue
        match = _RESULT_RE.match(line)
        if not match or current_file is None:
            continue
        rest = match.group("name").strip()
        if not rest:
            continue
        name = f"{current_file} > {rest}"
        occurrences[name] = occurrences.get(name, 0) + 1
        if occurrences[name] > 1:
            name = f"{name} #{occurrences[name]}"
        status = match.group("word") or match.group("mark")
        if status in ("pass", "✓", "✔"):
            passed_tests.add(name)
        elif status in ("fail", "✗", "✘", "×"):
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


@Instance.register("kenryu42", _INTERVAL)
class CC_SAFETY_NET_284_TO_128(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return CcSafetyNet284To128ImageDefault(self.pr, self._config)

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
