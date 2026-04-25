import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# --------------------------------------------------------------------------- #
# Era boundaries (by PR number)
#
#   Era A  PR 18         – setup.py, flat cattr/ layout, Python 3.6
#   Era B  PR 45–137     – setup.py, src/cattr/ layout, Python 3.7
#   Era C  PR 139–312    – poetry-core build, src/cattr(s)/ layout, Python 3.7
#   Era D  PR 371–688    – hatchling or poetry-core, Python 3.12
#                          (PR 371 needs typing.Final → requires Python 3.8+)
# --------------------------------------------------------------------------- #

# PR numbers per era (sorted)
_ERA_A = {18}
_ERA_B = {45, 86, 107, 115, 123, 132, 137}
_ERA_C = {139, 144, 167, 198, 207, 247, 312}
_ERA_D = {371, 431, 461, 543, 653, 660, 688}


def _era(pr_number: int) -> str:
    if pr_number in _ERA_A:
        return "A"
    if pr_number in _ERA_B:
        return "B"
    if pr_number in _ERA_C:
        return "C"
    if pr_number in _ERA_D:
        return "D"
    raise ValueError(f"PR #{pr_number} not mapped to any era")


# ---- shared shell helpers ------------------------------------------------- #

_FILTER_PATCH_PY = (
    "import re, sys\n"
    "\n"
    "if len(sys.argv) < 2:\n"
    "    sys.exit(1)\n"
    "\n"
    "try:\n"
    "    content = open(sys.argv[1]).read()\n"
    "except (IOError, OSError):\n"
    "    sys.exit(1)\n"
    "\n"
    "if not content.strip():\n"
    "    sys.exit(1)\n"
    "\n"
    "parts = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)\n"
    "filtered = [p for p in parts if p.strip() and 'Binary files' not in p]\n"
    "result = ''.join(filtered)\n"
    "\n"
    "if result.strip():\n"
    "    sys.stdout.write(result)\n"
    "else:\n"
    "    sys.exit(1)\n"
)


def _run_sh(repo: str, test_cmd: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
{test_cmd}
"""


def _test_run_sh(repo: str, test_cmd: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
if [ -s /home/test.patch ]; then
    python3 /home/filter_patch.py /home/test.patch > /tmp/filtered_test.patch 2>/dev/null && \\
        git apply --whitespace=nowarn /tmp/filtered_test.patch || true
fi
{test_cmd}
"""


def _fix_run_sh(repo: str, test_cmd: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
if [ -s /home/test.patch ]; then
    python3 /home/filter_patch.py /home/test.patch > /tmp/filtered_test.patch 2>/dev/null && \\
        git apply --whitespace=nowarn /tmp/filtered_test.patch || true
fi
if [ -s /home/fix.patch ]; then
    python3 /home/filter_patch.py /home/fix.patch > /tmp/filtered_fix.patch 2>/dev/null && \\
        git apply --whitespace=nowarn /tmp/filtered_fix.patch || true
fi
{test_cmd}
"""


# --------------------------------------------------------------------------- #
#  Era A – PR 18 only (Python 3.6, setup.py, flat cattr/)
# --------------------------------------------------------------------------- #
class ImageEraA(Image):
    """Python 3.6 + setup.py, flat cattr/ layout."""

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
        test_cmd = "python -m pytest tests/ -x --tb=short -v"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "filter_patch.py", _FILTER_PATCH_PY),
            File(".", "run.sh", _run_sh(self.pr.repo, test_cmd)),
            File(".", "test-run.sh", _test_run_sh(self.pr.repo, test_cmd)),
            File(".", "fix-run.sh", _fix_run_sh(self.pr.repo, test_cmd)),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """FROM python:3.6-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {base_sha}

# Pinned deps from requirements_dev.txt for this era
RUN pip install --no-cache-dir pytest==3.0.4 hypothesis==3.36.0 coverage==4.2
RUN pip install --no-cache-dir -e .

{copy_commands}

""".format(
            org=self.pr.org,
            repo=self.pr.repo,
            base_sha=self.pr.base.sha,
            copy_commands=copy_commands,
        )


# --------------------------------------------------------------------------- #
#  Era B – PR 45-137 (Python 3.7, setup.py, src/cattr/)
# --------------------------------------------------------------------------- #
class ImageEraB(Image):
    """Python 3.7 + setup.py, src/cattr/ layout."""

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
        return "python:3.7-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = 'python -m pytest tests/ -x --tb=short -v -o "addopts=-l"'
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "filter_patch.py", _FILTER_PATCH_PY),
            File(".", "run.sh", _run_sh(self.pr.repo, test_cmd)),
            File(".", "test-run.sh", _test_run_sh(self.pr.repo, test_cmd)),
            File(".", "fix-run.sh", _fix_run_sh(self.pr.repo, test_cmd)),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """FROM python:3.7-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {base_sha}

# Install test deps then the package
RUN pip install --no-cache-dir "pytest<8" "hypothesis<6.31" attrs
RUN pip install --no-cache-dir pymongo immutables || true
RUN pip install --no-cache-dir -e .

{copy_commands}

""".format(
            org=self.pr.org,
            repo=self.pr.repo,
            base_sha=self.pr.base.sha,
            copy_commands=copy_commands,
        )


# --------------------------------------------------------------------------- #
#  Era C – PR 139-312 (Python 3.7, poetry-core, src/cattr(s)/)
# --------------------------------------------------------------------------- #
class ImageEraC(Image):
    """Python 3.7 + poetry-core build system."""

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
        return "python:3.7-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = 'python -m pytest tests/ -x --tb=short -v -o "addopts=-l"'
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "filter_patch.py", _FILTER_PATCH_PY),
            File(".", "run.sh", _run_sh(self.pr.repo, test_cmd)),
            File(".", "test-run.sh", _test_run_sh(self.pr.repo, test_cmd)),
            File(".", "fix-run.sh", _fix_run_sh(self.pr.repo, test_cmd)),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """FROM python:3.7-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential \\
    && rm -rf /var/lib/apt/lists/*

# poetry-core needed to build from pyproject.toml
RUN pip install --no-cache-dir poetry-core

WORKDIR /home/

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {base_sha}

# Install test deps and the package via pip (using PEP 517 from pyproject.toml)
RUN pip install --no-cache-dir "pytest<8" "hypothesis<6.31" attrs
RUN pip install --no-cache-dir pymongo immutables || true
RUN pip install --no-cache-dir -e .

{copy_commands}

""".format(
            org=self.pr.org,
            repo=self.pr.repo,
            base_sha=self.pr.base.sha,
            copy_commands=copy_commands,
        )


# --------------------------------------------------------------------------- #
#  Era D – PR 371-688 (Python 3.12, hatchling or poetry-core, src/cattrs/)
# --------------------------------------------------------------------------- #
class ImageEraD(Image):
    """Python 3.12 + hatchling/hatch-vcs or poetry-core build system."""

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
        return "python:3.12-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = 'python -m pytest tests/ -x --tb=short -v -o "addopts=-l"'
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "filter_patch.py", _FILTER_PATCH_PY),
            File(".", "run.sh", _run_sh(self.pr.repo, test_cmd)),
            File(".", "test-run.sh", _test_run_sh(self.pr.repo, test_cmd)),
            File(".", "fix-run.sh", _fix_run_sh(self.pr.repo, test_cmd)),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential \\
    && rm -rf /var/lib/apt/lists/*

# hatchling + hatch-vcs + poetry-core for PEP 517 build (covers both eras)
RUN pip install --no-cache-dir hatchling hatch-vcs poetry-core

WORKDIR /home/

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {base_sha}

# Install test dependencies and the package
RUN pip install --no-cache-dir pytest hypothesis attrs typing-extensions
RUN pip install --no-cache-dir -e .
# Optional test deps for preconf tests
RUN pip install --no-cache-dir ujson orjson msgpack pyyaml tomlkit cbor2 pymongo msgspec immutables || true

{copy_commands}

""".format(
            org=self.pr.org,
            repo=self.pr.repo,
            base_sha=self.pr.base.sha,
            copy_commands=copy_commands,
        )


# --------------------------------------------------------------------------- #
#  Instance – single registration for all 22 PRs
# --------------------------------------------------------------------------- #
@Instance.register("python-attrs", "cattrs")
class Cattrs(Instance):
    """Evaluation instance for python-attrs/cattrs.

    Handles all 22 LHT instances across versions v0.6.0 through v26.1.0.
    Routes here when both number_interval and tag are empty.
    Dispatches to the appropriate era Image class based on PR number.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        era = _era(self.pr.number)
        if era == "A":
            return ImageEraA(self.pr, self._config)
        if era == "B":
            return ImageEraB(self.pr, self._config)
        if era == "C":
            return ImageEraC(self.pr, self._config)
        if era == "D":
            return ImageEraD(self.pr, self._config)
        raise ValueError(f"Unknown era {era!r} for PR #{self.pr.number}")

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
        """Parse pytest output.

        Handles standard pytest verbose format:
            tests/test_foo.py::test_bar PASSED
        Also handles the reversed format and ANSI color codes.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        log_clean = re.sub(r"\x1b\[.*?m", "", log)

        # Match: test_path STATUS  or  STATUS test_path
        pattern = re.compile(
            r"(tests/[\w/._-]+\.py::[\w\[\]._-]+)\s+(PASSED|FAILED|SKIPPED|ERROR)"
            r"|(PASSED|FAILED|SKIPPED|ERROR)\s+(tests/[\w/._-]+\.py::[\w\[\]._-]+)"
        )

        for match in pattern.finditer(log_clean):
            if match.group(1):
                test_name = match.group(1).strip()
                status = match.group(2)
            elif match.group(3):
                status = match.group(3)
                test_name = match.group(4).strip()
            else:
                continue

            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
