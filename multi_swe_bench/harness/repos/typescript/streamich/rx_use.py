"""streamich/rx-use harness config — Jest + ts-jest, Yarn Classic, TypeScript."""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RxUseImageBase(Image):
   
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
        return "node:18"

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
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class RxUseImageDefault(Image):
    
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
        return RxUseImageBase(self.pr, self._config)

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


yarn install --frozen-lockfile --non-interactive || yarn install --non-interactive || true

bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
npx jest --ci --runInBand --forceExit --verbose --no-coverage

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
npx jest --ci --runInBand --forceExit --verbose --no-coverage

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npx jest --ci --runInBand --forceExit --verbose --no-coverage

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


@Instance.register("streamich", "rx-use")
class RxUse(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return RxUseImageDefault(self.pr, self._config)

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
        """Parse Jest verbose output into pass/fail/skip sets.

        Jest verbose output looks like::

            PASS src/__tests__/raf.spec.ts
              ✓ is an operator function (13ms)
              ✕ throttles second value (2ms)
              ○ skipped does not emit

            FAIL src/__tests__/matchMedia$.spec.ts
              ● Test suite failed to run
                ... TS2307: Cannot find module '../matchMedia$'.

        Two granularities are recorded: the suite (its path) and each
        assertion (`<path>::<name>`).

        The suite-level entry is what carries the FAIL→PASS signal here. A
        suite that fails to compile prints no ✓/✕ lines at all, so the new
        tests have no per-assertion identity in the test stage; without the
        suite entry the transition would read NONE→NONE→PASS and the "tests
        fail before the fix" evidence would be lost. With it: NONE→FAIL→PASS.
        The per-assertion entries then ride along as n2p.

        Assertions are prefixed with their file because bare Jest names are
        not unique in this repo — `is server`, `can subscribe` and
        `attaches 1 listener` each appear in several suites, and the two names
        the test patch adds (`is server`, `holds "false" on server`) collide
        with the existing `*.server.spec.ts` suites. The prefix also makes the
        names resolvable by `report.py`'s `_test_name_matches_files`, which
        splits on `::` and compares the head to the patched file (R20).
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # `(1ms)` / `(8.703s)` timings are stripped: they differ run to run and
        # a name that carries one is not comparable across stages (R3).
        suite_passed_re = re.compile(r"^PASS\s+(\S+)(?:\s+\([\d.]+\s*m?s\))?$")
        suite_failed_re = re.compile(r"^FAIL\s+(\S+)(?:\s+\([\d.]+\s*m?s\))?$")
        test_passed_re = re.compile(r"^\s+[✓✔]\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$")
        test_failed_re = re.compile(r"^\s+[✕✗×]\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$")
        test_skipped_re = re.compile(
            r"^\s+○\s+skipped\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$"
        )

        current_suite = ""

        occurrences: dict[str, int] = {}

        def assertion_key(suite: str, name: str) -> str:
            base = f"{suite}::{name}"
            occurrences[base] = occurrences.get(base, 0) + 1
            nth = occurrences[base]
            return base if nth == 1 else f"{base} #{nth}"

        for line in clean_log.splitlines():
            line = line.rstrip()

            m = suite_passed_re.match(line)
            if m:
                current_suite = m.group(1)
                passed_tests.add(current_suite)
                continue

            m = suite_failed_re.match(line)
            if m:
                current_suite = m.group(1)
                failed_tests.add(current_suite)
                continue

            # Jest indents assertions under the suite header that precedes
            # them, and --runInBand keeps the blocks strictly sequential, so
            # the last header seen is the owning file.
            if not current_suite:
                continue

            m = test_passed_re.match(line)
            if m:
                passed_tests.add(assertion_key(current_suite, m.group(1)))
                continue

            m = test_failed_re.match(line)
            if m:
                failed_tests.add(assertion_key(current_suite, m.group(1)))
                continue

            m = test_skipped_re.match(line)
            if m:
                skipped_tests.add(assertion_key(current_suite, m.group(1)))

        # R2 — the three sets must be disjoint or TestResult raises. A name
        # reported both ways is a failure.
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
