import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class LodashImageBase(Image):
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
        return "node:14"

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

        return f"""\
FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

RUN npm ci || npm install || true

{self.clear_env}
"""


class LodashImageDefault(Image):
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
        return LodashImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {sha}

npm install || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
sed -i 's/hidepassed = true/hidepassed = false/' test/test.js test/test-fp.js
npm test
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
sed -i 's/hidepassed = true/hidepassed = false/' test/test.js test/test-fp.js
git apply --whitespace=nowarn --exclude=package-lock.json /home/test.patch
npm test
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
sed -i 's/hidepassed = true/hidepassed = false/' test/test.js test/test-fp.js
git apply --whitespace=nowarn --exclude=package-lock.json /home/test.patch /home/fix.patch
npm test
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        name = dep.image_name()
        tag = dep.image_tag()

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


@Instance.register("lodash", "lodash")
class Lodash(Instance):
    """Lodash utility library (lodash/lodash). Custom test runner with PASS/FAIL output."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LodashImageDefault(self.pr, self._config)

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
        """Parse lodash custom test runner output.

        Format:
            PASS - test description
            FAIL - test description
            PASS: N  FAIL: N  TOTAL: N
        """
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()

        for line in clean_log.splitlines():
            stripped = line.strip()

            # Match " PASS - test name"
            pass_match = re.match(r"^\s*PASS\s+-\s+(.+)$", stripped)
            if pass_match:
                passed_tests.add(pass_match.group(1).strip())
                continue

            # Match " FAIL - test name"
            fail_match = re.match(r"^\s*FAIL\s+-\s+(.+)$", stripped)
            if fail_match:
                failed_tests.add(fail_match.group(1).strip())
                continue

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=0,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=set(),
        )
