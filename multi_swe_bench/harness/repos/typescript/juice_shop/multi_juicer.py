import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MultiJuicerImageBase(Image):
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
        return "golang:1.25"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \\
    && curl -fsSL https://deb.nodesource.com/setup_26.x | bash - \\
    && apt-get install -y --no-install-recommends nodejs \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class MultiJuicerImageDefault(Image):
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
        return MultiJuicerImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export CI=true

cd /home/{pr.repo}/balancer/ui
npm ci --ignore-scripts || true
npm run build

cd /home/{pr.repo}/balancer
go mod download

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}/balancer/ui
npm run build

cd /home/{pr.repo}/balancer
go test -v -json -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

cd /home/{pr.repo}/balancer/ui
npm run build

cd /home/{pr.repo}/balancer

set +e
go test -v -json -count=1 ./... | tee /tmp/go_test_output.json
GO_TEST_STATUS=${{PIPESTATUS[0]}}
set -e

FAILED_PKGS=$(grep -h '\\[build failed\\]' /tmp/go_test_output.json | grep -oP '"Package":"\\K[^"]+' | sort -u || true)

if [ -n "$FAILED_PKGS" ]; then
  TOUCHED_FILES=$(grep -oP '^\\+\\+\\+ b/\\K.*_test\\.go$' /home/test.patch || true)
  for file in $TOUCHED_FILES; do
    rel="${{file#balancer/}}"
    [ "$rel" = "$file" ] && continue
    dir=$(dirname "$rel")
    pkg=$(go list "./$dir" 2>/dev/null || true)
    [ -z "$pkg" ] && continue
    if echo "$FAILED_PKGS" | grep -qxF "$pkg"; then
      grep -oP '^func \\KTest\\w+' "/home/{pr.repo}/$file" | while read -r testname; do
        echo '{{"Action":"fail","Package":"'"$pkg"'","Test":"'"$testname"'"}}'
      done
    fi
  done
fi

exit $GO_TEST_STATUS

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

cd /home/{pr.repo}/balancer/ui
npm run build

cd /home/{pr.repo}/balancer
go test -v -json -count=1 ./...

""".format(pr=self.pr),
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


@Instance.register("juice-shop", "multi-juicer")
class MultiJuicer(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MultiJuicerImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", test_log)

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            test_name = event.get("Test")
            if not test_name:
                continue

            full_name = f"{event.get('Package', '')}::{test_name}"
            action = event.get("Action")

            if action == "pass":
                failed_tests.discard(full_name)
                skipped_tests.discard(full_name)
                passed_tests.add(full_name)
            elif action == "fail":
                passed_tests.discard(full_name)
                skipped_tests.discard(full_name)
                failed_tests.add(full_name)
            elif action == "skip":
                if full_name not in passed_tests and full_name not in failed_tests:
                    skipped_tests.add(full_name)

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
