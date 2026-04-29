
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _vitest_cmd_bash() -> str:
    """Return a conditional bash snippet that detects vitest config layout
    and runs the appropriate vitest command.

    Sub-phases in this era:
    - Early (#3950-#5810): vitest.config.ts, no workspace/projects
    - Mid (#5987-#6033): vitest.config.mts, no workspace/projects
    - Late-A (#6169-#6453): vitest.config.mts with projects (project name: unit)
    - Late-B (#6482-#6987): vitest.config.mts with projects (project names: unit:lib, unit:website, …)
    """
    return (
        "if grep -q 'unit:lib' vitest.config.mts 2>/dev/null; then\n"
        "  npx vitest run --reporter=verbose --config vitest.config.mts --project 'unit:lib'\n"
        "elif grep -q 'projects' vitest.config.mts 2>/dev/null; then\n"
        "  npx vitest run --reporter=verbose --config vitest.config.mts --project unit\n"
        "elif [ -f vitest.config.mts ]; then\n"
        "  npx vitest run --reporter=verbose --config vitest.config.mts\n"
        "else\n"
        "  npx vitest run --reporter=verbose --config vitest.config.ts\n"
        "fi"
    )


class RechartsVitestImageBase(Image):
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
        return "node:18"

    def image_tag(self) -> str:
        return "base-vitest"

    def workdir(self) -> str:
        return "base-vitest"

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
RUN apt update && apt install -y git

{code}

{self.clear_env}

"""


class RechartsVitestImageDefault(Image):
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
        return RechartsVitestImageBase(self.pr, self.config)

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
npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{vitest_cmd}

""".format(pr=self.pr, vitest_cmd=_vitest_cmd_bash()),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude '*.png' --whitespace=nowarn /home/test.patch || git apply --exclude package-lock.json --exclude '*.png' --exclude package.json --whitespace=nowarn /home/test.patch
npm install || true
{vitest_cmd}

""".format(pr=self.pr, vitest_cmd=_vitest_cmd_bash()),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --exclude package-lock.json --exclude '*.png' --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --exclude package-lock.json --exclude '*.png' --exclude package.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm install || true
{vitest_cmd}

""".format(pr=self.pr, vitest_cmd=_vitest_cmd_bash()),
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


@Instance.register("recharts", "recharts_3950_to_99999")
class RECHARTS_3950_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RechartsVitestImageDefault(self.pr, self._config)

    def _test_cmd(self) -> str:
        return _vitest_cmd_bash()

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

        # Strip ANSI escape codes for reliable matching
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        # Vitest output formats:
        # ✓ test/cartesian/Area.spec.tsx > <Area /> > Render customized label ...
        # ✓ |unit:lib| test/cartesian/Area.spec.tsx > getBaseValue > ...
        # × test/cartesian/Area.spec.tsx > <Area /> > some failing test
        # × |unit:lib| test/cartesian/Area.spec.tsx > some failing test
        # Also handles .test.ts, .spec.js etc.
        _file_pat = r"\S+\.(?:spec|test)\.(?:tsx?|jsx?)"
        re_pass = re.compile(
            rf"[✓]\s+(?:\|[^|]+\|\s+)?({_file_pat})\s+>"
        )
        re_fail = re.compile(
            rf"[❯×✗]\s+(?:\|[^|]+\|\s+)?({_file_pat})\s+>"
        )
        re_skip = re.compile(
            rf"[↓]\s+(?:\|[^|]+\|\s+)?({_file_pat})\s+>"
        )

        for line in test_log.splitlines():
            clean = ansi_re.sub("", line).strip()
            if not clean:
                continue

            pass_match = re_pass.search(clean)
            if pass_match:
                passed_tests.add(pass_match.group(1))
                continue

            fail_match = re_fail.search(clean)
            if fail_match:
                failed_tests.add(fail_match.group(1))
                continue

            skip_match = re_skip.search(clean)
            if skip_match:
                skipped_tests.add(skip_match.group(1))

        # Deduplicate: a file can have both passing and failing tests,
        # or both passing and skipped tests
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
