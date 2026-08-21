import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DotPropImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "str | Image":
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

        repo = self.pr.repo

        if self.config.need_clone:
            code = f"""\
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}
RUN npm install --force || true

{Image._HARDENING_BLOCK}
CMD ["/bin/bash"]"""
        else:
            code = f"""\
COPY {repo} /home/{repo}

WORKDIR /home/{repo}
RUN npm install --force || true"""

        return f"""\
FROM {image_name}

{self.global_env}
ENV LC_ALL=C.UTF-8 \\
    CI=true

WORKDIR /home/
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class DotPropImageDefault(Image):
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
        return DotPropImageBase(self.pr, self._config)

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
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
                '  echo "check_git_changes: Not inside a git repository"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                'if [[ -n $(git status --porcelain) ]]; then\n'
                '  echo "check_git_changes: Uncommitted changes"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                'echo "check_git_changes: No uncommitted changes"\n'
                "exit 0\n",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

rm -rf node_modules
npm install --force || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
npx ava --tap --concurrency=1 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary --exclude="*.snap" /home/test.patch
npm install --force || true
npx ava --tap --concurrency=1 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary --exclude="*.snap" /home/test.patch /home/fix.patch
npm install --force || true
npx ava --tap --concurrency=1 2>&1
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY check_git_changes.sh /home/check_git_changes.sh
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
WORKDIR /home/{self.pr.repo}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("sindresorhus", "dot-prop")
class DotProp(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DotPropImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        test_pattern = re.compile(r"^(ok|not ok) (\d+) (.*?)(?: #.*)?$")
        lines = test_log.split("\n")
        tap_started = False
        current_test_name = None
        test_file_id = f"{self.pr.repo}/test.js"

        for line in lines:
            line = line.strip()

            if line.startswith("TAP version 13"):
                tap_started = True
                continue

            if not tap_started:
                continue

            if line.startswith("1.."):
                break

            if line.startswith("# "):
                current_test_name = line[2:].strip()

            elif line.startswith(("ok", "not ok")):
                match = test_pattern.match(line)
                if match:
                    status, _test_num, line_test_name = match.groups()
                    line_test_name = line_test_name.strip()
                    if line_test_name.startswith("- "):
                        line_test_name = line_test_name[2:].strip()

                    if "# SKIP" in line:
                        test_name = (
                            f"{test_file_id}::{current_test_name}: {line_test_name}"
                            if current_test_name
                            else f"{test_file_id}::{line_test_name}"
                        )
                        skipped_tests.add(test_name)
                        continue

                    test_name = (
                        f"{test_file_id}::{current_test_name}: {line_test_name}"
                        if current_test_name
                        else f"{test_file_id}::{line_test_name}"
                    )

                    if status == "ok":
                        passed_tests.add(test_name)
                    elif status == "not ok":
                        failed_tests.add(test_name)

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
