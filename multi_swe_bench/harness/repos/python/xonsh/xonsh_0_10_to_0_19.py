import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# xonsh MID era: xonsh 0.10.x - 0.19.x
#
# Discovered interactively in Docker (python:3.9-slim):
#   * xonsh 0.10-0.19 declares requires-python >=3.7 .. >=3.9; ubuntu:latest
#     now ships Python 3.14 (too new), so we pin python:3.9-slim which
#     satisfies the whole range.
#   * Test dependencies moved between releases:
#       - 0.10-0.12 ship requirements/tests.txt (no [test] extra)
#       - 0.13-0.19 ship a pyproject `[test]` optional-dependency group
#     prepare.sh picks whichever exists.
#   * Canonical test runner `xonsh run-tests.xsh test -- -v` works across
#     the whole range (xonsh console script is installed by `pip install -e .`).
#
# Verified PRs: 3909 (0.11.0), 4770 (0.12.2/0.12.3), 5665 (0.18.4)
#               -- all install & run with this recipe.
# ---------------------------------------------------------------------------


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

    def dependency(self) -> str:
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "mswebench"

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
                "prepare.sh",
                """cd /home/{pr.repo} && git reset --hard && git checkout {pr.base.sha}
###ACTION_DELIMITER###
pip install -e . || true
###ACTION_DELIMITER###
if [ -f requirements/tests.txt ]; then pip install -r requirements/tests.txt; else pip install -e ".[test]"; fi || true""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
xonsh run-tests.xsh test -- -v --continue-on-collection-errors --timeout=300

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
python3 - /home/test.patch /home/test.patch.nobin <<'PYEOF'
import sys, re
data = open(sys.argv[1], encoding="utf-8", errors="surrogateescape").read()
parts = re.split(r"(?m)(?=^diff --git )", data)
kept = [p for p in parts if "GIT binary patch" not in p and "Binary files " not in p]
open(sys.argv[2], "w", encoding="utf-8", errors="surrogateescape").write("".join(kept))
PYEOF
if ! git -C /home/{pr.repo} apply --whitespace=nowarn --binary /home/test.patch.nobin; then
    echo "Error: git apply failed" >&2
    exit 1
fi
xonsh run-tests.xsh test -- -v --continue-on-collection-errors --timeout=300

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
python3 - /home/test.patch /home/test.patch.nobin <<'PYEOF'
import sys, re
data = open(sys.argv[1], encoding="utf-8", errors="surrogateescape").read()
parts = re.split(r"(?m)(?=^diff --git )", data)
kept = [p for p in parts if "GIT binary patch" not in p and "Binary files " not in p]
open(sys.argv[2], "w", encoding="utf-8", errors="surrogateescape").write("".join(kept))
PYEOF
python3 - /home/fix.patch /home/fix.patch.nobin <<'PYEOF'
import sys, re
data = open(sys.argv[1], encoding="utf-8", errors="surrogateescape").read()
parts = re.split(r"(?m)(?=^diff --git )", data)
kept = [p for p in parts if "GIT binary patch" not in p and "Binary files " not in p]
open(sys.argv[2], "w", encoding="utf-8", errors="surrogateescape").write("".join(kept))
PYEOF
if ! git -C /home/{pr.repo} apply --whitespace=nowarn --binary  /home/test.patch.nobin /home/fix.patch.nobin; then
    echo "Error: git apply failed" >&2
    exit 1
fi
xonsh run-tests.xsh test -- -v --continue-on-collection-errors --timeout=300

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# xonsh 0.10.x-0.19.x declares requires-python >=3.7..>=3.9; pin
# python:3.9-slim because ubuntu:latest now ships Python 3.14 (too new).
FROM python:3.9-slim

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements (man-db: tests/test_man.py needs the `man` binary)
RUN apt-get update && apt-get install -y git man-db

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/xonsh/xonsh.git /home/xonsh

WORKDIR /home/xonsh
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("xonsh", "xonsh_0_10_to_0_19")
class XONSH_0_10_TO_0_19(Instance):
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
        # Parse pytest verbose output. Format captured in Docker is the
        # standard `tests/<path>::<id> STATUS` (and the summary form
        # `STATUS tests/<path>::<id>`). Test ids may contain '#', '.', '='
        # etc. from parametrization, so the id class is broadened to \\S+.
        passed_tests = set[str]()
        failed_tests = set[str]()
        skipped_tests = set[str]()
        import re

        # Strip ANSI escape codes first (xonsh run-tests.xsh colorizes output).
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # Line-anchored: pytest verbose emits `tests/<id> STATUS  [ nn%]`.
        # `.+?` is used (not \\S+) because xonsh parametrized ids contain
        # spaces, '#', quotes etc. e.g. test_expandvars[%foo% %a_bool%-bar True].
        # The reverse form covers the short summary `STATUS tests/<id> - reason`.
        status_re = re.compile(
            r"^(?P<test>tests/.+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XFAILED|XPASS|XPASSED|RERUN)\b"
            r"|^(?P<status2>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XFAILED|XPASS|XPASSED|RERUN)\s+"
            r"(?P<test2>tests/\S+)"
        )
        test_status = {}
        for raw in log.splitlines():
            match = status_re.match(raw.strip())
            if not match:
                continue
            if match.group("test"):
                test = match.group("test").strip()
                status = match.group("status")
            else:
                test = match.group("test2").strip()
                status = match.group("status2")
            if status != "RERUN":
                test_status[test] = status
        for test, status in test_status.items():
            if status in ("PASSED", "XPASS", "XPASSED"):
                passed_tests.add(test)
            elif status in ("FAILED", "ERROR", "XFAIL", "XFAILED"):
                failed_tests.add(test)
            elif status == "SKIPPED":
                skipped_tests.add(test)
        # Enforce TestResult invariants: the three sets must be pairwise
        # disjoint (failed takes precedence over passed, then skipped).
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
