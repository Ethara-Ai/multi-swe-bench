import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FnmOcamlImageBase(Image):
    """
    Base Docker image for OCaml/ReasonML era of fnm (PRs 13-225).

    Shared layer: node:16-bullseye + apt packages (git, m4, pkg-config) + repo clone.
    Per-PR images layer on top with commit checkout, esy build, and test patches.

    ARM64 limitation: esy npm package has no ARM64 binaries; x86_64 only.
    """

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
        return "node:16-bullseye"

    def image_tag(self) -> str:
        return "base-ocaml"

    def workdir(self) -> str:
        return "base-ocaml"

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
ENV FORCE_COLOR=false

WORKDIR /home/

RUN apt-get update && apt-get install -y git m4 pkg-config

{code}

{self.clear_env}

"""


class FnmOcamlImageDefault(Image):
    """
    Per-PR Docker image for OCaml/ReasonML era of fnm (PRs 13-225, v1.1 to v1.21).

    Build system: esy (opam-based package manager for ReasonML/OCaml)
    Test runner: @reason-native/rely (Jest-inspired native Reason test framework)
    Test command: esy x TestFnm.exe
    Base image: FnmOcamlImageBase (node:16-bullseye + apt packages + repo clone)

    Docker evidence:
      - PR#225 (sha=025e1e8da46f, v1.21.0): package.json has "test": "esy x TestFnm.exe"
      - 18 .re source files, 0 .rs files, no Cargo.toml
      - esy.json/package.json with opam dependencies: @opam/lwt, @reason-native/rely, etc.
      - test/ directory: TestFnm.re, SmokeTest.re, TestSemver.re, TestFs.re,
        TestUninstall.re, TestListRemote.re, TestFramework.re
      - feature_tests/ directory: 21 shell-based feature tests (not used for unit testing)
      - ARM64 limitation: esy npm package has no ARM64 binaries; x86_64 only
    """

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
        return FnmOcamlImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

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

export FORCE_COLOR=false

npm install -g esy@0.6.12
esy install
esy build

esy x TestFnm.exe || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export FORCE_COLOR=false
esy x TestFnm.exe

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export FORCE_COLOR=false
git apply --whitespace=nowarn /home/test.patch
esy build
esy x TestFnm.exe

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export FORCE_COLOR=false
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
esy build
esy x TestFnm.exe

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


@Instance.register("Schniz", "fnm_0_to_225")
class FNM_0_TO_225(Instance):
    """
    Instance for OCaml/ReasonML era of fnm (PRs 13-225).

    Test output format (@reason-native/rely):
      Pass summary: "Tests:       0 failed, N passed, N total"
      Fail summary: "Tests:       M failed, N passed, T total"
      Suite lines:  " PASS SuiteName" or " FAIL SuiteName"
      Individual failures shown with "SuiteName > TestName" format

    The rely framework output (with FORCE_COLOR=false) produces:
      - "Tests: <failed> failed, <passed> passed, <total> total" summary
      - "PASS <suite>" / "FAIL <suite>" per test suite
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FnmOcamlImageDefault(self.pr, self._config)

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
        """
        Parse rely test framework output.

        Rely output format (with FORCE_COLOR=false, ANSI stripped):
          Suite-level:  " PASS SuiteName" / " FAIL SuiteName"
          Summary:      "Tests:       1 failed, 5 passed, 6 total"
          Individual failure details: "  ● SuiteName › TestName"

        We parse both suite-level results and individual test failures.
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape sequences for reliable parsing
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", log)

        # Parse suite-level PASS/FAIL
        re_pass_suite = re.compile(r"^\s*PASS\s+(.+)$", re.MULTILINE)
        re_fail_suite = re.compile(r"^\s*FAIL\s+(.+)$", re.MULTILINE)

        for match in re_pass_suite.finditer(clean_log):
            passed_tests.add(match.group(1).strip())

        for match in re_fail_suite.finditer(clean_log):
            failed_tests.add(match.group(1).strip())

        # Parse individual test failure details: "  ● SuiteName › TestName"
        re_individual_fail = re.compile(
            r"^\s+[●•]\s+(.+?)\s+[›>]\s+(.+)$", re.MULTILINE
        )
        for match in re_individual_fail.finditer(clean_log):
            test_name = f"{match.group(1).strip()} > {match.group(2).strip()}"
            failed_tests.add(test_name)

        # Parse summary line: "Tests:       N failed, M passed, T total"
        re_summary = re.compile(
            r"Tests:\s+(\d+)\s+failed,\s+(\d+)\s+passed,\s+(\d+)\s+total"
        )
        summary_match = re_summary.search(clean_log)
        if summary_match:
            summary_failed = int(summary_match.group(1))
            summary_passed = int(summary_match.group(2))
            # Use summary counts if we didn't capture individual tests
            if not passed_tests and not failed_tests:
                for i in range(summary_passed):
                    passed_tests.add(f"test_{i + 1}")
                for i in range(summary_failed):
                    failed_tests.add(f"failed_test_{i + 1}")

        # Dedup: a test that appears in both passed and failed should be failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
