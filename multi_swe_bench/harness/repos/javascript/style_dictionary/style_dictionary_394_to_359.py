import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "style-dictionary"


class ImageBase394To359(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return "base-394"

    def workdir(self) -> str:
        return "base_394_to_359"

    def files(self) -> list[File]:
        return []

    def repo_dir(self) -> str:
        return REPO_DIR

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{REPO_DIR}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {REPO_DIR} /home/{REPO_DIR}"

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git python2 g++ make && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}
"""


class ImageDefault394To359(Image):
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
        return ImageBase394To359(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def repo_dir(self) -> str:
        return REPO_DIR

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
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install
""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}
npx jest --runInBand --verbose
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}
git apply --exclude package-lock.json --exclude '*/package-lock.json' --whitespace=nowarn /home/test.patch
npm install || true
npx jest --runInBand --verbose
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}
git apply --exclude package-lock.json --exclude '*/package-lock.json' --whitespace=nowarn /home/test.patch /home/fix.patch
npm install || true
npx jest --runInBand --verbose
""".format(repo_dir=REPO_DIR),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""\
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("style-dictionary", "style_dictionary_394_to_359")
class StyleDictionary394To359(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault394To359(self.pr, self._config)

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

        current_suite = None

        re_pass_suite = re.compile(r"^PASS (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")
        re_pass_test = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d*\.?\d+\s*\w+\))?$")

        re_fail_suite = re.compile(r"^FAIL (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")
        re_fail_test = re.compile(r"^\s*[✕×]\s+(.+?)(?:\s+\(\d*\.?\d+\s*\w+\))?$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass_suite.match(line)
            if pass_match:
                current_suite = pass_match.group(1)
                passed_tests.add(current_suite)
                continue

            fail_match = re_fail_suite.match(line)
            if fail_match:
                current_suite = fail_match.group(1)
                failed_tests.add(current_suite)
                continue

            pass_test_match = re_pass_test.match(line)
            if pass_test_match:
                if current_suite is None:
                    continue

                test = f"{current_suite}:{pass_test_match.group(1)}"
                passed_tests.add(test)
                continue

            fail_test_match = re_fail_test.match(line)
            if fail_test_match:
                if current_suite is None:
                    continue

                test = f"{current_suite}:{fail_test_match.group(1)}"
                failed_tests.add(test)
                continue

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
