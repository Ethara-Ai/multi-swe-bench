import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Shared per-era base: OS toolchain + third-party test deps + a FULL clone.

    Deliberately does NOT check out any PR's base.sha and does NOT run the
    anti-reward-hack hardening. Both are per-PR concerns: the hardening pins
    HEAD to one sha and runs `git gc --prune=now`, so doing it here would prune
    the shared clone down to a single PR's commit and break every other PR in
    the era with "reference is not a tree". The project itself is installed in
    the per-PR layer too, so the installed package matches that PR's base.sha
    rather than whatever the default branch happened to be when the base built.

    The `# syntax` directive opts this out of DockerfileEnhancer, which would
    otherwise inject exactly the checkout+prune that must not happen here.
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

    def dependency(self) -> str:
        return "ubuntu:latest"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-8496-to-7601"

    def workdir(self) -> str:
        return "base-8496-to-7601"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return """# syntax=docker/dockerfile:1.6
FROM ubuntu:latest

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} shared base (8496-to-7601)" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

# Toolchain and third-party test dependencies only -- shared by every PR in
# this era. The project install lives in the per-PR layer, after the checkout.
RUN apt-get update && apt-get install -y python3 python3-pip python3-venv python3.12-venv build-essential python3-dev
RUN python3 -m venv venv
RUN ./venv/bin/pip install cython
RUN ./venv/bin/pip install 'pytest>=7.0.0rc1,<8' pytest-xdist

CMD ["/bin/bash"]
""".format(org=org, repo=repo)


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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

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
                """ls
###ACTION_DELIMITER###
apt-get update
###ACTION_DELIMITER###
apt-get install -y python3 python3-pip build-essential python3-dev
###ACTION_DELIMITER###
pip install 'pytest>=7.0.0rc1,<8' pytest-xdist
###ACTION_DELIMITER###
python3 -m venv venv
###ACTION_DELIMITER###
apt-get install -y python3.12-venv
###ACTION_DELIMITER###
python3 -m venv venv
###ACTION_DELIMITER###
source venv/bin/activate
###ACTION_DELIMITER###
pip install cython
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
pip install 'pytest>=7.0.0rc1,<8' pytest-xdist
###ACTION_DELIMITER###
echo 'venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers test/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers -W ignore::DeprecationWarning:sqlalchemy.sql.sqltypes test/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
pip install -e ".[mypy]"
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers -n auto -m "not memory_intensive and not timing_intensive and not mypy" test/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers -n auto -m "not memory_intensive and not timing_intensive and not mypy" -W ignore::DeprecationWarning:sqlalchemy.sql.sqltypes test/' > test_commands.sh
###ACTION_DELIMITER###
echo 'export PYTHONWARNINGS="ignore::DeprecationWarning:sqlalchemy.sql.sqltypes"; venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers -n auto -m "not memory_intensive and not timing_intensive and not mypy" test/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'export PYTHONWARNINGS="ignore::DeprecationWarning:sqlalchemy.sql.sqltypes"; venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers -n auto -m "not memory_intensive and not timing_intensive and not mypy" -o "filterwarnings=default::DeprecationWarning:sqlalchemy" test/' > test_commands.sh
###ACTION_DELIMITER###
echo 'export PYTHONWARNINGS="ignore::DeprecationWarning:sqlalchemy.sql.sqltypes"; venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers -n auto -m "not memory_intensive and not timing_intensive and not mypy" -o "filterwarnings=ignore::DeprecationWarning:sqlalchemy" test/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
export PYTHONWARNINGS="ignore::DeprecationWarning:sqlalchemy.sql.sqltypes"; venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers -n auto -m "not memory_intensive and not timing_intensive and not mypy" -o "filterwarnings=ignore::DeprecationWarning:sqlalchemy" test/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
export PYTHONWARNINGS="ignore::DeprecationWarning:sqlalchemy.sql.sqltypes"; venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers -n auto -m "not memory_intensive and not timing_intensive and not mypy" -o "filterwarnings=ignore::DeprecationWarning:sqlalchemy" test/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
export PYTHONWARNINGS="ignore::DeprecationWarning:sqlalchemy.sql.sqltypes"; venv/bin/pytest -v --tb native -r sfxX --maxfail=250 -p warnings -p logging --strict-markers -n auto -m "not memory_intensive and not timing_intensive and not mypy" -o "filterwarnings=ignore::DeprecationWarning:sqlalchemy" test/

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Thin per-PR layer on top of the era's shared base. The base already
        # holds the toolchain, the test deps and a FULL clone, so all that is
        # left here is: pin to this PR's base.sha, install the project from
        # THAT source (never the base's default-branch tree), copy the patches
        # in, and harden.
        #
        # The hardening runs LAST, pinned to the literal base sha. It strips
        # remotes/refs and prunes unreachable objects; because Docker layers are
        # copy-on-write those deletions live in THIS layer only, so the shared
        # base keeps full history for the era's other PRs while this graded
        # image can no longer reach any post-fix commit.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return """# syntax=docker/dockerfile:1.6
FROM {base_image}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {sha}

# Project install AFTER checkout, so the installed package is this PR's
# base.sha and not the base image's default-branch tree.
RUN ./venv/bin/pip install -e .
RUN ./venv/bin/pip install -e ".[mypy]" || true

{copy_commands}

{hardening}

CMD ["/bin/bash"]
""".format(
            base_image=self.dependency().image_full_name(),
            repo=repo,
            sha=self.pr.base.sha,
            copy_commands=copy_commands,
            hardening=hardening,
        )


@Instance.register("sqlalchemy", "sqlalchemy_8496_to_7601")
class SQLALCHEMY_8496_TO_7601(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # pytest states the verdict in two different orders, and this suite
        # emits both. Matching only one order silently drops most results on
        # any era whose run.sh omits -rA:
        #   "test/foo.py::bar PASSED [ 12%]"        plain -v progress line
        #   "PASSED test/foo.py::bar"               -rA short summary
        #   "[gw3] [ 12%] PASSED test/foo.py::bar"  xdist worker line
        #   "test/foo.py::bar <- <string> PASSED"   location-indirection form,
        #                                           emitted for generated tests
        _status = r"PASSED|FAILED|SKIPPED"
        _verdict = re.compile(
            rf"(?:^|\s)(?P<s1>{_status})\s+(?P<t1>test/\S+)"
            rf"|(?:^|\s)(?P<t2>test/\S+)(?:\s+<-\s+\S+)?\s+(?P<s2>{_status})(?=\s|$)",
            re.MULTILINE,
        )
        for _m in _verdict.finditer(log):
            _status_hit = _m.group("s1") or _m.group("s2")
            _name = (_m.group("t1") or _m.group("t2")).strip()
            if _status_hit == "PASSED":
                passed_tests.add(_name)
            elif _status_hit == "FAILED":
                failed_tests.add(_name)
            else:
                skipped_tests.add(_name)

        # Skip reasons quote the id instead of listing it bare, e.g.
        #   SKIPPED [1] lib/.../config.py:420: 'test/x.py::Y::z (call)' : no cython
        for _name in re.findall(r"SKIPPED \[\d+\][^']*'(test/[^']+)'", log):
            skipped_tests.add(
                re.sub(r"\s*\((?:call|setup|teardown)\)$", "", _name.strip())
            )

        # One test id can carry different verdicts across backend variants
        # (PASSED on sqlite, SKIPPED where the backend is absent). TestResult
        # rejects ALL THREE pairwise overlaps, so resolve deterministically as
        # FAILED > SKIPPED > PASSED: never credit a pass for something that
        # also failed or was skipped somewhere. Without this the harness dies
        # with "Passed tests and skipped tests should not have common items".
        skipped_tests -= failed_tests
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
