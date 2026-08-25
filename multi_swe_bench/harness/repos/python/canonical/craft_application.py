import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# canonical/craft-application#907 "feat(linter): add linter service framework" adds a linter
# service (craft_application.services.linter / craft_application.lint) + a `testcraft` fixture
# app that exercises it. Graded tests are the new linter tests. Without the fix the module
# doesn't exist, so at the test stage they error on import (n2p). We run pip install -e . (pulls
# Canonical's craft-* deps from PyPI) + pytest. One integration test (test_issue_then_ignore)
# needs a live snapd/LXD provider that isn't available in a container, so we deselect it.

_PIP_TEST_DEPS = "pytest pytest-mock pytest-check pytest-freezer pytest-cov pyfakefs"
_TEST_FILES = (
    "tests/unit/services/test_linter_service.py "
    "tests/unit/services/test_testcraft_linter.py "
    "tests/unit/commands/test_lint.py "
    "tests/integration/services/test_linter.py"
)
_DESELECT = "--deselect tests/integration/services/test_linter.py::test_issue_then_ignore"
_TEST_CMD = (
    f"python -m pytest {_TEST_FILES} {_DESELECT} "
    "-p no:cacheprovider -p no:randomly -v -rN"
)


class CraftApplicationImageBase(Image):
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
        return "python:3.12-slim"

    def image_prefix(self) -> str:
        return "envagent"

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
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git gcc && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
# pre-warm: install the package (pulls Canonical's craft-* deps from PyPI) + pytest stack
RUN pip install -e . || true
RUN pip install {_PIP_TEST_DEPS} || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class CraftApplicationImageDefault(Image):
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
        return CraftApplicationImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Bulletproof reset (the harness reuses one container across stages via docker-commit, so
        # a prior stage's applied patch can linger): hard-reset tracked files + remove untracked,
        # then apply. The patches apply cleanly on a clean base, so no --reject fallback is needed
        # (and a brace fallback would break here — apply_* is a .format() value, so its braces are
        # inserted literally, not collapsed like an f-string's).
        _reset = "git reset --hard HEAD >/dev/null 2>&1 || true; git clean -fdq >/dev/null 2>&1 || true"
        apply_test = f"{_reset}; git apply --whitespace=nowarn /home/test.patch"
        apply_fix = f"{_reset}; git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard >/dev/null 2>&1 || true
git checkout {pr.base.sha}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
{cmd}
""".format(pr=self.pr, cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
{apply}
pip install -e . >/dev/null 2>&1 || true
{cmd}
""".format(pr=self.pr, apply=apply_test, cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
{apply}
pip install -e . >/dev/null 2>&1 || true
{cmd}
""".format(pr=self.pr, apply=apply_fix, cmd=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("canonical", "craft-application")
class CraftApplication(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CraftApplicationImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        prog = re.compile(r"^(\S+::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", re.MULTILINE)
        summ = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)", re.MULTILINE)
        for name, status in prog.findall(clean):
            (passed if status in ("PASSED", "XPASS") else failed if status in ("FAILED", "ERROR") else skipped).add(name)
        for status, name in summ.findall(clean):
            (passed if status in ("PASSED", "XPASS") else failed if status in ("FAILED", "ERROR") else skipped).add(name)
        passed -= failed
        passed -= skipped
        skipped -= failed
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
