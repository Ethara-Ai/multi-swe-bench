import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MkdocsDrawioExporterImageBase(Image):
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
        return "python:3.11-slim"

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

RUN DEBIAN_FRONTEND=noninteractive apt-get update \\
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
       git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class MkdocsDrawioExporterImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return MkdocsDrawioExporterImageBase(self.pr, self.config)

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

pip install --upgrade pip || true
pip install -e . || true
pip install pytest || true

pytest -v --tb=short -o "python_files=exporter.py" mkdocs_drawio_exporter/tests/ || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
pytest -v --tb=short -o "python_files=exporter.py" mkdocs_drawio_exporter/tests/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
  echo "ERROR: test.patch failed to apply" >&2
  exit 1
fi
pytest -v --tb=short -o "python_files=exporter.py" mkdocs_drawio_exporter/tests/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
  echo "ERROR: test.patch failed to apply" >&2
  exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
  echo "ERROR: fix.patch failed to apply" >&2
  exit 1
fi
pytest -v --tb=short -o "python_files=exporter.py" mkdocs_drawio_exporter/tests/

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


@Instance.register("LukeCarrier", "mkdocs-drawio-exporter")
class MkdocsDrawioExporter(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MkdocsDrawioExporterImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"^PASSED\s+(\S+)"),
            re.compile(r"^(\S+)\s+PASSED"),
            re.compile(r"^(\S+::\S+)\s+PASSED"),
        ]
        re_fail_tests = [
            re.compile(r"^FAILED\s+(\S+)"),
            re.compile(r"^(\S+)\s+FAILED"),
            re.compile(r"^ERROR\s+(\S+)"),
            re.compile(r"^(\S+)\s+ERROR"),
        ]
        re_skip_tests = [
            re.compile(r"^SKIPPED\s+(\S+)"),
            re.compile(r"^(\S+)\s+SKIPPED"),
        ]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    name = pass_match.group(1)
                    if name and not name[0].isdigit():
                        passed_tests.add(name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    name = fail_match.group(1)
                    if name and not name[0].isdigit():
                        failed_tests.add(name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    name = skip_match.group(1)
                    if name and not name[0].isdigit():
                        skipped_tests.add(name)

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
