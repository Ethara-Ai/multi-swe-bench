from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.9-slim-bookworm"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TEST_ID = r"[^\s:]+\.(?:py|c)::\S+(?: < .*? >)?"
_VERBOSE_RE = re.compile(
    rf"^({_TEST_ID})\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[\s*\d+%\])?\s*$"
)
_SUMMARY_RE = re.compile(rf"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+({_TEST_ID})(?:\s+-\s.*)?$")

_PYTEST = "python -m pytest -v -rA --color=no -p no:cacheprovider"
_IGNORES = "--ignore=ast --ignore=printers --ignore=symbolic --ignore=ndarrays"

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true

pytest_group() {
  local rc=0
  "$@" || rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "pytest exited with status $rc: $*" >&2
    exit "$rc"
  fi
}"""

_TEST_CMDS = f"""cd /home/pyccel/tests
pytest_group {_PYTEST} -m "not parallel and c" {_IGNORES}
pytest_group {_PYTEST} -m "not parallel and not c and not python" {_IGNORES}
pytest_group {_PYTEST} -m "not parallel and python" {_IGNORES}
pytest_group {_PYTEST} ndarrays/"""


class PyccelImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/80-retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates gcc gfortran libblas-dev liblapack-dev \\
    libopenmpi-dev openmpi-bin libomp-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class PyccelImageDefault(Image):
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
        return PyccelImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

export PIP_DEFAULT_TIMEOUT=120
python -m pip install --no-cache-dir --no-deps -e .
python -m pip install --no-cache-dir setuptools==59.8.0 wheel==0.36.2 \\
  numpy==1.19.5 sympy==1.7.1 mpmath==1.1.0 termcolor==1.1.0 Arpeggio==1.10.1 textX==2.3.0 \\
  scipy==1.6.0 mpi4py==3.0.3 tblib==1.7.0 \\
  pytest==6.2.1 attrs==20.3.0 iniconfig==1.1.1 packaging==20.8 pyparsing==2.4.7 \\
  pluggy==0.13.1 py==1.10.0 toml==0.10.2
python -m pip check || {{ echo "prepare.sh: pip check found broken requirements"; exit 1; }}
python -c "import numpy, sympy, textx, scipy, tblib; from mpi4py import MPI" || {{ echo "prepare.sh: a python test dependency does not import"; exit 1; }}
python -m pytest --version || {{ echo "prepare.sh: pytest is not installed"; exit 1; }}
gfortran --version || {{ echo "prepare.sh: gfortran is missing"; exit 1; }}
python -c "import pyccel, os, sys; sys.exit(os.path.dirname(pyccel.__file__) != '/home/{self.pr.repo}/pyccel')" || {{ echo "prepare.sh: pyccel is not imported from the source tree"; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
{_RUN_HEADER}

{_TEST_CMDS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "test-run.sh: git apply failed" >&2; exit 1; }}
{_TEST_CMDS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "fix-run.sh: git apply failed" >&2; exit 1; }}
{_TEST_CMDS}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("PyccelImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("pyccel", "pyccel")
class PYCCEL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PyccelImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()
            name = status = None
            m = _VERBOSE_RE.match(line)
            if m:
                name, status = m.group(1), m.group(2)
            else:
                m = _SUMMARY_RE.match(line)
                if m:
                    status, name = m.group(1), m.group(2)
            if not name or not status:
                continue
            if not name.startswith("tests/"):
                name = f"tests/{name}"
            if status == "PASSED":
                passed.add(name)
            elif status in ("FAILED", "ERROR"):
                failed.add(name)
            else:
                skipped.add(name)

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
