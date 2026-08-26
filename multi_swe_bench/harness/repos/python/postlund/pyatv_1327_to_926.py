import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

TEST_CMD = "python -m pytest -p no:xdist -rA -v --timeout=120 --durations=10"

START_SERVICES = ""

KNOWN_FLAKY_TESTS = frozenset()

BASE_IMAGE = "python:3.9-slim"

TOOLCHAIN_SETUP = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
"""

CONSTRAINTS = """aiohttp==3.7.4.post0
async-timeout==3.0.1
bitarray==2.3.4
cryptography==3.4.8
mediafile==0.9.0
miniaudio==1.45
mutagen==1.45.1
netifaces==0.11.0
protobuf==3.17.3
srptools==0.2.0
yarl==1.6.3
zeroconf==0.36.2
"""


class PyatvImageBase(Image):
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
        return BASE_IMAGE

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return (
            f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

"""
            + TOOLCHAIN_SETUP
            + f"""
{code}

{self.clear_env}

"""
        )


class PyatvImageDefault(Image):
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
        return PyatvImageBase(self.pr, self.config)

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
            File(".", "constraints.txt", CONSTRAINTS),
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

grep -v '^codecov' requirements_test.txt > /home/requirements_test.pinned.txt

pip install --no-cache-dir -c /home/constraints.txt -r /home/requirements_test.pinned.txt
pip install --no-cache-dir -c /home/constraints.txt -e .

if [ "$(uname -m)" = "x86_64" ]; then
  {start_services}
  {test_cmd} || true
else
  echo "prepare.sh: $(uname -m) is not the grading architecture -- skipping the"
  echo "prepare.sh: test warm-up."
fi

""".format(pr=self.pr, start_services=START_SERVICES, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{start_services}
{test_cmd}

""".format(pr=self.pr, start_services=START_SERVICES, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{start_services}
{test_cmd}

""".format(pr=self.pr, start_services=START_SERVICES, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{start_services}
{test_cmd}

""".format(pr=self.pr, start_services=START_SERVICES, test_cmd=TEST_CMD),
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


@Instance.register("postlund", "pyatv_1327_to_926")
class PYATV_1327_TO_926(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PyatvImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_progress = re.compile(
            r"^(tests/.*?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$"
        )
        re_summary = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(tests/.*?)(?:\s+-\s.*)?$"
        )
        re_collect_error = re.compile(r"^ERROR\s+(tests/\S+\.py)\s*(?:-|$)")

        def record(status: str, test_id: str) -> None:
            if test_id in KNOWN_FLAKY_TESTS:
                return
            if status in ("PASSED", "XPASS"):
                if test_id in failed_tests:
                    return
                skipped_tests.discard(test_id)
                passed_tests.add(test_id)
            elif status in ("FAILED", "ERROR"):
                passed_tests.discard(test_id)
                skipped_tests.discard(test_id)
                failed_tests.add(test_id)
            elif status in ("SKIPPED", "XFAIL"):
                if test_id not in passed_tests and test_id not in failed_tests:
                    skipped_tests.add(test_id)

        for line in test_log.splitlines():
            line = line.rstrip()

            match = re_progress.match(line)
            if match:
                record(match.group(2), match.group(1))
                continue

            match = re_summary.match(line)
            if match:
                record(match.group(1), match.group(2))
                continue

            match = re_collect_error.match(line)
            if match:
                record("ERROR", match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("postlund", "pyatv")(PYATV_1327_TO_926)
