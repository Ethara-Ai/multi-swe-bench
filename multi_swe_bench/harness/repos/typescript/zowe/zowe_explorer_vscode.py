import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


JEST_PATTERN = r'".*__tests__.*\.unit\.test\.ts"'

JEST_CMD = (
    f"npx jest {JEST_PATTERN} --verbose --ci --reporters=default --colors=false"
)


class ZoweExplorerVscodeImageBase(Image):

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
        return "node:12-buster"

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

WORKDIR /home/

{code}

{self.clear_env}

"""


class ZoweExplorerVscodeImageDefault(Image):

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
        return ZoweExplorerVscodeImageBase(self.pr, self._config)

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
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

if [ -f resources/testProfileData.example.ts ] && [ ! -f resources/testProfileData.ts ]; then
    cp resources/testProfileData.example.ts resources/testProfileData.ts
fi

bash /home/check_git_changes.sh

npm ci --ignore-scripts --no-audit --no-fund \\
  || npm install --ignore-scripts --no-audit --no-fund \\
  || true

test -d node_modules/jest || {{ echo "FATAL: jest not installed" >&2; exit 1; }}
test -d node_modules/ts-jest || {{ echo "FATAL: ts-jest not installed" >&2; exit 1; }}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard

{jest}
""".format(pr=self.pr, jest=JEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard

git apply --whitespace=nowarn /home/test.patch \\
  || git apply --3way --whitespace=nowarn /home/test.patch

{jest}
""".format(pr=self.pr, jest=JEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard

git apply --whitespace=nowarn /home/test.patch /home/fix.patch \\
  || git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch

{jest}
""".format(pr=self.pr, jest=JEST_CMD),
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


@Instance.register("zowe", "zowe-explorer-vscode")
class ZoweExplorerVscode(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ZoweExplorerVscodeImageDefault(self.pr, self._config)

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
        return parse_jest_log(test_log)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_SUITE_PASS_RE = re.compile(r"^PASS\s+(\S+)")
_SUITE_FAIL_RE = re.compile(r"^FAIL\s+(\S+)")

_TEST_PASS_RE = re.compile(r"^[✓✔]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
_TEST_FAIL_RE = re.compile(r"^[✕✗✘×]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
_TEST_SKIP_RE = re.compile(r"^○\s+(?:skipped\s+)?(.*?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")


_SLUG_RE = re.compile(r"\s+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("_", name.strip())


def parse_jest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    current_suite: Optional[str] = None

    for raw_line in test_log.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        m = _SUITE_PASS_RE.match(stripped) or _SUITE_FAIL_RE.match(stripped)
        if m:
            current_suite = m.group(1)
            continue

        for regex, bucket in (
            (_TEST_PASS_RE, passed_tests),
            (_TEST_FAIL_RE, failed_tests),
            (_TEST_SKIP_RE, skipped_tests),
        ):
            m = regex.match(stripped)
            if m:
                name = _slugify(m.group(1))
                if not name:
                    break
                bucket.add(f"{current_suite}::{name}" if current_suite else name)
                break

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
