import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class EverythingClaudeCodeImageBase(Image):
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
        return "node:20-bookworm"

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
ENV CI=true
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class EverythingClaudeCodeImageDefault(Image):
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
        return EverythingClaudeCodeImageBase(self.pr, self._config)

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

# Install npm deps if a package.json exists (early PRs in this dataset
# pre-date the package.json). `|| true` so optional deps can't block us.
if [ -f package.json ]; then
  timeout --kill-after=30 600 npm install --no-audit --no-fund || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
# Run the unit test suite. We invoke `node tests/run-all.js` DIRECTLY
# rather than `npm test` because the npm test pipeline chains validators
# that check documentation consistency, which legitimately fail on PRs
# that bump version numbers or add files without doc updates. Those
# validator failures abort the chain before any unit tests run, masking
# the actual test signal we care about.
#
# Output format from this custom runner: `  ✓ <name>` and `  ✗ <name>`.
set -e

cd /home/{pr.repo}
if [ -f tests/run-all.js ]; then
  timeout --kill-after=30 600 node tests/run-all.js || true
else
  echo "(no tests/run-all.js at this base.sha — pre-test era)"
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
bash /home/run_tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
bash /home/run_tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
bash /home/run_tests.sh
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


@Instance.register("affaan-m", "everything-claude-code")
class EverythingClaudeCode(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EverythingClaudeCodeImageDefault(self.pr, self._config)

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

        # Strip ANSI escapes FIRST so colored CI output still parses.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        # Custom runner's format (verified via docker smoke test):
        #   `  ✓ <test name>`     -> pass  (U+2713)
        #   `  ✗ <test name>`     -> fail  (U+2717)
        # Also accept ASCII fallbacks for robustness:
        #   `  ok <name>` / `  PASS <name>` / `  FAIL <name>`
        re_pass = [
            re.compile(r"^\s*[✓✔]\s+(.+?)\s*$"),  # ✓ ✔
            re.compile(r"^\s*ok\s+\d*\s*-?\s*(.+?)\s*$", re.IGNORECASE),
        ]
        re_fail = [
            re.compile(r"^\s*[✗✘❌✘]\s+(.+?)\s*$"),  # ✗ ✘ ❌
            re.compile(r"^\s*not\s+ok\s+\d*\s*-?\s*(.+?)\s*$", re.IGNORECASE),
        ]
        re_skip = [
            re.compile(r"^\s*[➖–○⚠]\s+(.+?)\s*$"),  # − – ○ ⚠
            re.compile(r"^\s*skip(?:ped)?\s+(.+?)\s*$", re.IGNORECASE),
        ]

        for line in clean.splitlines():
            for rx in re_pass:
                m = rx.match(line)
                if m:
                    passed_tests.add(m.group(1).strip())
                    break
            else:
                for rx in re_fail:
                    m = rx.match(line)
                    if m:
                        failed_tests.add(m.group(1).strip())
                        break
                else:
                    for rx in re_skip:
                        m = rx.match(line)
                        if m:
                            skipped_tests.add(m.group(1).strip())
                            break

        # Enforce disjoint sets: passed > failed > skipped.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
