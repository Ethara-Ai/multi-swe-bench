import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PACKAGES = (
    "github.com/swaggo/swag "
    "github.com/swaggo/swag/cmd/swag "
    "github.com/swaggo/swag/gen"
)

_TEST_CMD = f"go test -v -count=1 {_PACKAGES}"

_APPLY_PATCH = """apply_patch() {
    pf="$1"
    if git apply --binary --whitespace=nowarn "$pf"; then
        return 0
    fi
    if git apply --binary --whitespace=nowarn --3way "$pf"; then
        return 0
    fi
    echo "ERROR: failed to apply $pf" >&2
    git apply --binary --whitespace=nowarn --check -v "$pf" >&2 || true
    exit 1
}"""


class SwagImageBase(Image):
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
        return "golang:1.17-bullseye"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

ENV CGO_ENABLED=0

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git make \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}

"""


class SwagImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return SwagImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

cd /home/{pr.repo}

git reset --hard
bash /home/check_git_changes.sh

git cat-file -e {pr.base.sha} 2>/dev/null || \\
    git fetch --quiet --no-tags https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

test -z "$(git remote)" || {{ echo "prepare: a git remote survived the base hardening" >&2; exit 1; }}

go mod download

go test -run "^$" -count=1 {packages} > /dev/null 2>&1 || true

""".format(pr=self.pr, packages=_PACKAGES),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

{test_cmd}

""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

{apply_patch}

apply_patch /home/test.patch

{test_cmd}

""".format(pr=self.pr, apply_patch=_APPLY_PATCH, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

{apply_patch}

apply_patch /home/test.patch
apply_patch /home/fix.patch

{test_cmd}

""".format(pr=self.pr, apply_patch=_APPLY_PATCH, test_cmd=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("swaggo", "swag")
class Swag(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SwagImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        re_pass = re.compile(r"^---\s+PASS:\s+(\S+)")
        re_fail = re.compile(r"^---\s+FAIL:\s+(\S+)")
        re_skip = re.compile(r"^---\s+SKIP:\s+(\S+)")
        re_build_fail = re.compile(r"FAIL\s+(\S+)\s+\[(?:build|setup) failed\]")

        for line in test_log.splitlines():
            line = ansi.sub("", line).strip()

            match = re_pass.match(line)
            if match:
                passed_tests.add(match.group(1))
                continue

            match = re_fail.match(line)
            if match:
                failed_tests.add(match.group(1))
                continue

            match = re_skip.match(line)
            if match:
                skipped_tests.add(match.group(1))
                continue

            match = re_build_fail.search(line)
            if match:
                failed_tests.add(f"BUILD-FAILED:{match.group(1)}")

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
