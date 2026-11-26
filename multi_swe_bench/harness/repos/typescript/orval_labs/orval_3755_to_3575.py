from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "orval_3755_to_3575"
_BASE_IMAGE = "node:22.23.4-bookworm-slim"
_BASE_TAG = "base-3755_to_3575"
_BUN_VERSION = "1.3.14"

_RESULT_RE = re.compile(
    r"^(?P<mark>[✓×↓]) +(?:\|(?P<piped>[^|]+)\||(?P<badge>\S+)) +(?P<rest>.+?)(?: \d+(?:\.\d+)?m?s)?$"
)

def _project_dir(project: str) -> str | None:
    if project.startswith("@orval/"):
        return "packages/" + project.split("/", 1)[1]
    if project == "orval":
        return "packages/orval"
    if project == "orval-tests":
        return "tests"
    return None


class Orval3755To3575ImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{DockerfileEnhancer._MITM_MOUNT}

RUN npm install -g "bun@{_BUN_VERSION}"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Orval3755To3575ImageDefault(Image):
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
        return Orval3755To3575ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_sha = self.pr.base.sha
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
bash /home/check_git_changes.sh

bun install --frozen-lockfile

node_modules/.bin/vp run --no-cache -F './packages/*' build:release

node_modules/.bin/vitest --version
tests/node_modules/.bin/vitest --version
tests/node_modules/.bin/run-p --version
tests/node_modules/.bin/orval --version
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}

status=0
node_modules/.bin/vp run --no-cache -F './packages/*' build:release || status=$?
node_modules/.bin/vitest run --reporter=verbose || status=$?
bun run --filter orval-tests generate-api || status=$?
(cd tests && node_modules/.bin/vitest run --config vitest.snapshots.ts --reporter=verbose) || status=$?
exit $status
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

status=0
node_modules/.bin/vp run --no-cache -F './packages/*' build:release || status=$?
node_modules/.bin/vitest run --reporter=verbose || status=$?
bun run --filter orval-tests generate-api || status=$?
(cd tests && node_modules/.bin/vitest run --config vitest.snapshots.ts --reporter=verbose) || status=$?
exit $status
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

status=0
node_modules/.bin/vp run --no-cache -F './packages/*' build:release || status=$?
node_modules/.bin/vitest run --reporter=verbose || status=$?
bun run --filter orval-tests generate-api || status=$?
(cd tests && node_modules/.bin/vitest run --config vitest.snapshots.ts --reporter=verbose) || status=$?
exit $status
""",
            ),
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

    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    occurrences: dict[str, int] = {}
    for line in test_log.splitlines():
        match = _RESULT_RE.match(line.strip())
        if not match:
            continue
        project = match.group("piped") or match.group("badge")
        directory = _project_dir(project)
        rest = match.group("rest")
        name = f"{directory}/{rest}" if directory else f"|{project}| {rest}"
        occurrences[name] = occurrences.get(name, 0) + 1
        if occurrences[name] > 1:
            name = f"{name} #{occurrences[name]}"
        mark = match.group("mark")
        if mark == "✓":
            passed_tests.add(name)
        elif mark == "×":
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


@Instance.register("orval-labs", _INTERVAL)
class ORVAL_3755_TO_3575(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Orval3755To3575ImageDefault(self.pr, self._config)

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
