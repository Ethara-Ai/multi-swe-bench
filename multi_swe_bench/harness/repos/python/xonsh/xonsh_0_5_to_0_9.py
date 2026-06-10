import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# xonsh OLD era: xonsh 0.5.x - 0.9.x
#
# Discovered interactively in Docker (python:3.6-slim), validated against
# the FULL tests/ suite (not just isolated files):
#   * xonsh 0.5-0.9 targets Python 3.4-3.6; ubuntu:latest now ships Python
#     3.14 which cannot import these releases, so we pin python:3.6-slim.
#   * `pip install ply==3.11` is required (the pinned ply==3.8 in
#     requirements-tests.txt is broken on modern setuptools).
#   * pygments is imported by xonsh.pyghooks but is NOT a hard dependency
#     (`pip install -e .` does not pull it), so it must be installed
#     explicitly or pyghooks-importing test modules error at collection.
#   * pytest/ptk are selected by a RUNTIME PROBE, not version numbers
#     (early/late 0.9.x point releases are incompatible):
#       - if xonsh/pytest_plugin.py uses `from_parent` (modernised plugin,
#         late 0.9.x) it needs pytest>=5.4 AND ptk 3.x
#         -> prompt-toolkit>=3.0 + pytest==6.2.5
#       - otherwise (0.5-0.8, early/mid 0.9.x) the legacy `[pytest]`
#         setup.cfg section forbids pytest>=4 -> pytest==3.10.1, with
#         ptk 1.x for xonsh<0.8 (tools.py imports ptk at collection) and
#         ptk 2.x for >=0.8
#   * --continue-on-collection-errors so a single env-specific bad test
#     file (e.g. test_ptk_shell on a given point release) cannot zero the
#     whole session.
#
# Verified (full tests/ run, replaying this exact prepare.sh probe):
#   0.5.2=2773, 0.7.0, 0.8.0=3050, 0.9.3=3262, 0.9.16=3986,
#   0.9.23=4166, 0.9.27 passed -- the whole 0.5-0.9 span runs.
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
        return "python:3.6-slim"

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
pip install ply==3.11 || true
###ACTION_DELIMITER###
pip install -e . || true
###ACTION_DELIMITER###
pip install "pygments>=2.2" || true
###ACTION_DELIMITER###
cd /home/{pr.repo}; if grep -q "from_parent" xonsh/pytest_plugin.py 2>/dev/null; then pip install "prompt-toolkit>=3.0" "pytest==6.2.5" "pytest-timeout==1.4.2"; else python -c "import xonsh,sys;v=xonsh.__version__.split('.');sys.exit(0 if (int(v[0]),int(v[1]))<(0,8) else 1)" && PTK="prompt-toolkit==1.0.15" || PTK="prompt-toolkit>=2.0,<3.0"; pip install "$PTK" "pytest==3.10.1" "pytest-timeout==1.3.4"; fi || true""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
pytest -v -p no:flake8 -p no:cacheprovider --timeout=120 --continue-on-collection-errors tests/

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
pytest -v -p no:flake8 -p no:cacheprovider --timeout=120 --continue-on-collection-errors tests/

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
pytest -v -p no:flake8 -p no:cacheprovider --timeout=120 --continue-on-collection-errors tests/

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# xonsh 0.5.x-0.9.x targets Python 3.4-3.6; pin python:3.6-slim because
# ubuntu:latest now ships Python 3.14 which cannot import these releases.
FROM python:3.6-slim

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


@Instance.register("xonsh", "xonsh_0_5_to_0_9")
class XONSH_0_5_TO_0_9(Instance):
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
