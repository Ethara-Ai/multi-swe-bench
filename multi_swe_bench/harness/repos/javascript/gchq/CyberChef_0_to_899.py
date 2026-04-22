from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class CyberChefOldImageBase(Image):
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
        return "node:10"

    def image_tag(self) -> str:
        return "base-grunt"

    def workdir(self) -> str:
        return "base-grunt"

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

WORKDIR /home/
RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}
"""


class CyberChefOldImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return CyberChefOldImageBase(self.pr, self._config)

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
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
sed -i 's/}}, 10 \\* 1000);/}}, 600 \\* 1000);/' test/index.js test/index.mjs 2>/dev/null || true; sed -i 's/}}, 60 \\* 1000);/}}, 600 \\* 1000);/' tests/operations/index.mjs 2>/dev/null || true; sed -i 's/}}, 60 \\* 1000);/}}, 600 \\* 1000);/' tests/lib/utils.mjs 2>/dev/null || true
npx grunt test
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='package-lock.json' --exclude='*.ico' --exclude='*.xlsx' /home/test.patch
npm install || true
sed -i 's/}}, 10 \\* 1000);/}}, 600 \\* 1000);/' test/index.js test/index.mjs 2>/dev/null || true; sed -i 's/}}, 60 \\* 1000);/}}, 600 \\* 1000);/' tests/operations/index.mjs 2>/dev/null || true; sed -i 's/}}, 60 \\* 1000);/}}, 600 \\* 1000);/' tests/lib/utils.mjs 2>/dev/null || true
npx grunt test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='package-lock.json' --exclude='*.ico' --exclude='*.xlsx' /home/test.patch /home/fix.patch
npm install || true
sed -i 's/}}, 10 \\* 1000);/}}, 600 \\* 1000);/' test/index.js test/index.mjs 2>/dev/null || true; sed -i 's/}}, 60 \\* 1000);/}}, 600 \\* 1000);/' tests/operations/index.mjs 2>/dev/null || true; sed -i 's/}}, 60 \\* 1000);/}}, 600 \\* 1000);/' tests/lib/utils.mjs 2>/dev/null || true
npx grunt test

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


@Instance.register("gchq", "CyberChef_0_to_899")
class CYBERCHEF_0_TO_899(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CyberChefOldImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        # CyberChef custom test runner output format (early grunt era):
        # Passing: ✔️️ Test Name
        # Failing: ❌ Test Name (followed by indented output)
        # Erroring: 🔥 Test Name (followed by indented output)
        # Summary: PASSING 256 / FAILING 3 / TOTAL 259
        re_pass = re.compile(r"^[✔✓]️?️?\s+(.+)$")
        re_fail = re.compile(r"^❌\s+(.+)$")
        re_error = re.compile(r"^🔥\s+(.+)$")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            pass_match = re_pass.match(line)
            if pass_match:
                passed_tests.add(pass_match.group(1).strip())
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                failed_tests.add(fail_match.group(1).strip())
                continue

            error_match = re_error.match(line)
            if error_match:
                failed_tests.add(error_match.group(1).strip())
                continue

        # If no individual test lines found, fall back to summary parsing
        # Summary format: PASSING\t256 or PASSING 256
        if not passed_tests and not failed_tests:
            re_summary_pass = re.compile(r"^PASSING\s+(\d+)$")
            re_summary_fail = re.compile(r"^FAILING\s+(\d+)$")
            re_summary_error = re.compile(r"^ERRORING\s+(\d+)$")

            total_passing = 0
            total_failing = 0

            for line in test_log.splitlines():
                line = ansi_escape.sub("", line).strip()
                m = re_summary_pass.match(line)
                if m:
                    total_passing += int(m.group(1))
                m = re_summary_fail.match(line)
                if m:
                    total_failing += int(m.group(1))
                m = re_summary_error.match(line)
                if m:
                    total_failing += int(m.group(1))

            for i in range(1, total_passing + 1):
                passed_tests.add(f"Test_{i}")
            for i in range(1, total_failing + 1):
                failed_tests.add(f"FailedTest_{i}")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
