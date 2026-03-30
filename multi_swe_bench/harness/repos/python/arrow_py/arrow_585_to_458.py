import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ArrowImageBase(Image):
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
        return "python:3.9-slim"

    def image_tag(self) -> str:
        return "base-nosetests-tox"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """\
FROM python:3.9-slim
{global_env}
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}
WORKDIR /home/{repo}
""".format(org=self.pr.org, repo=self.pr.repo, global_env=self.global_env)


class ArrowImageDefault(Image):
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
        return ArrowImageBase(self.pr, self.config)

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
git checkout {base_sha}
pip install nose chai mock || true
pip install python-dateutil || true
pip install . || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git checkout -- . 2>/dev/null || true
nosetests -v 2>&1
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
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch
nosetests -v 2>&1
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
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
nosetests -v 2>&1
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return """\
FROM {dep_image}:{dep_tag}
{global_env}
COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh
{clear_env}
""".format(
            dep_image=dep.image_name(),
            dep_tag=dep.image_tag(),
            global_env=self.global_env,
            clear_env=self.clear_env,
        )


@Instance.register("arrow-py", "arrow_585_to_458")
class ARROW_585_TO_458(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ArrowImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        nose_pass = re.compile(
            r"^(\S+)\s+\(([^)]+)\)\s+\.\.\.\s+ok\s*$", re.MULTILINE
        )
        nose_fail = re.compile(
            r"^(\S+)\s+\(([^)]+)\)\s+\.\.\.\s+(?:FAIL|ERROR)\s*$", re.MULTILINE
        )
        nose_skip = re.compile(
            r"^(\S+)\s+\(([^)]+)\)\s+\.\.\.\s+(?:skip|SKIP)\s*$", re.MULTILINE
        )

        for match in nose_pass.finditer(clean_log):
            passed_tests.add(f"{match.group(2)}.{match.group(1)}")

        for match in nose_fail.finditer(clean_log):
            failed_tests.add(f"{match.group(2)}.{match.group(1)}")

        for match in nose_skip.finditer(clean_log):
            skipped_tests.add(f"{match.group(2)}.{match.group(1)}")

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
