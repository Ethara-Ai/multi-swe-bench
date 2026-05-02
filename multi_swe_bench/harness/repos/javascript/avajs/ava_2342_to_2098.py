import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "node:12-buster"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        return f"""FROM node:12-buster

ENV DEBIAN_FRONTEND=noninteractive


WORKDIR /home/
RUN git clone https://github.com/avajs/ava.git /home/ava

WORKDIR /home/ava
RUN git reset --hard
RUN git checkout {self.pr.base.sha}
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo_name = self.pr.repo
        test_cmd = "npx nyc npm run test:tap"
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
                "prepare.sh",
                f"""#!/bin/bash
cd /home/{repo_name}
git reset --hard
git checkout {self.pr.base.sha}
npm install --ignore-scripts || true
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
npx nyc tap

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npm install --ignore-scripts || true
npx nyc tap

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npm install --ignore-scripts || true
npx nyc tap

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {self.dependency().image_name()}:{self.dependency().image_tag()}

WORKDIR /home/ava

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("avajs", "ava_2342_to_2098")
class AVA_2342_TO_2098(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        counter = 0

        for line in log.splitlines():
            line = line.strip()
            ok_match = re.match(r"^(not )?ok (\d+) - (.+)$", line)
            if ok_match:
                counter += 1
                is_not_ok = ok_match.group(1) is not None
                test_num = ok_match.group(2)
                test_desc = ok_match.group(3).strip()
                if "# SKIP" in test_desc:
                    test_name = re.sub(r"\s*#\s*SKIP.*$", "", test_desc).strip()
                    skipped_tests.add(f"{counter}:{test_num} - {test_name}")
                elif is_not_ok:
                    test_name = re.sub(r"\s*#\s*time=.*$", "", test_desc).strip()
                    failed_tests.add(f"{counter}:{test_num} - {test_name}")
                else:
                    test_name = re.sub(r"\s*#\s*time=.*$", "", test_desc).strip()
                    passed_tests.add(f"{counter}:{test_num} - {test_name}")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
