import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DotenvLabImageBase(Image):
    """Base image for motdotla/dotenv ERA 1 (lab framework, PRs 203-304).

    Uses node:20-bookworm. Lab test framework with JSON reporter.
    No package-lock.json in this era → npm install.
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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "lab-base"

    def workdir(self) -> str:
        return "lab-base"

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

RUN npm install || true

{self.clear_env}
"""


class DotenvLabImageDefault(Image):
    """Per-PR image for motdotla/dotenv ERA 1 (lab framework, PRs 203-304).

    Test command: lab test/* -r json
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
        return DotenvLabImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = "npx lab test/* -r json"

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
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
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

cd /home/{repo}
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --3way --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn /home/test.patch || true
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
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


@Instance.register("motdotla", "dotenv_304_to_203")
class DotenvLab(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DotenvLabImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Lab JSON reporter output: {"tests": {"experiment": [{"title": ..., "err": false|"msg", "duration": N}]}}
        # Lab JSON doesn't distinguish skipped from passed (both err=false) — safe because skips are code-defined.
        try:
            data = json.loads(clean_log)
            tests = data.get("tests", {})
            for experiment, cases in tests.items():
                for case in cases:
                    title = case.get("title", "")
                    name = f"{experiment} > {title}" if experiment else title
                    err = case.get("err", False)
                    if err and err is not False:
                        failed_tests.add(name)
                    else:
                        passed_tests.add(name)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

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
