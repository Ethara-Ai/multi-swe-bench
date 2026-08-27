import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_TEST_CMD = 'go test -v -count=1 -gcflags="all=-l" ./...'
_TEST_DEPS = "github.com/agiledragon/gomonkey/v2@v2.13.0"


class K8sgptImageBase(Image):
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
        return "golang:1.23-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git"
                f" /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV CI=true
ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local
ENV GOFLAGS=-mod=mod

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

{code}

"""


class K8sgptImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return K8sgptImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
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
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout [[SHA]]
bash /home/check_git_changes.sh

go mod download || true

go get [[DEPS]] || true
git checkout -- go.mod go.sum

""".replace("[[REPO]]", repo)
                .replace("[[SHA]]", sha)
                .replace("[[DEPS]]", _TEST_DEPS),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd

[[TEST_CMD]]

""".replace("[[REPO]]", repo).replace("[[TEST_CMD]]", _TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

go get [[DEPS]] || true

[[TEST_CMD]]

""".replace("[[REPO]]", repo)
                .replace("[[DEPS]]", _TEST_DEPS)
                .replace("[[TEST_CMD]]", _TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

go mod download || true

[[TEST_CMD]]

""".replace("[[REPO]]", repo).replace("[[TEST_CMD]]", _TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("k8sgpt-ai", "k8sgpt")
class K8sgpt(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return K8sgptImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        status_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        package_re = re.compile(r"^(?:ok|FAIL)\s+(\S+)")
        module_re = re.compile(r"^[^/]+\.[^/]+/[^/]+/[^/]+/")

        pending: list[tuple[str, str]] = []

        def flush(package: str) -> None:
            for status, name in pending:
                qualified = f"{name}/{package}" if package else name
                if status == "PASS":
                    passed_tests.add(qualified)
                elif status == "FAIL":
                    failed_tests.add(qualified)
                else:
                    skipped_tests.add(qualified)
            pending.clear()

        for line in clean_log.splitlines():
            match = status_re.match(line)
            if match:
                pending.append((match.group(1), match.group(2)))
                continue

            match = package_re.match(line)
            if match:
                flush(module_re.sub("", match.group(1)))

        flush("")

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
