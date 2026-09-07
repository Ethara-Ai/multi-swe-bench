import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# openforcefield/openff-interchange#995 "Make `Interchange.topology` a required field"
# (base 956fc872eba69e9bdbd79d0723f100098dc74e5f, merged 2024-06-21, post-v0.3.27 main).
#
# Environment rationale - openff-interchange is a conda-forge-only scientific stack, so the
# base image is condaforge/miniforge3 (multi-arch: linux/amd64 + linux/arm64, mamba built in):
#   openff-toolkit-base=0.16.0  the June-2024 toolkit line; 0.16 dropped its own pydantic
#                               dependency, which is what lets Interchange run on pydantic v2
#   pydantic>=2                 this sha is past the Pydantic-v2 migration (the fix patch
#                               touches `_AnnotatedTopology`, a v2 Annotated validator type)
#   rdkit                       cheminformatics backend; the `-base` toolkit ships without one
#                               and every graded test calls `Molecule.from_smiles(...)`
#   openmm                      required by the `interop/openmm` tests and the `sage` fixtures
# ambertools/gromacs/lammps/foyer/mbuild are deliberately omitted: no graded test needs them
# and the suite skips those paths via `openff.utilities.has_package` guards.
#
# The test command is scoped to the three files the PR's test patch touches so the run does not
# depend on the full (~30 min) suite or on the engine binaries above. The identical command is
# used in all three stages so the f2p comparison stays meaningful.

_ENV = "interchange"
_CONDA_SH = "/opt/conda/etc/profile.d/conda.sh"
_ENV_CREATE = (
    "mamba create -y -n interchange -c conda-forge "
    'python=3.11 "openff-toolkit-base=0.16.0" openff-units openff-utilities '
    'openff-forcefields openmm rdkit "pydantic>=2" numpy pyyaml packaging pytest'
)
_TEST_FILES = (
    "openff/interchange/_tests/unit_tests/components/test_interchange.py "
    "openff/interchange/_tests/unit_tests/interop/openmm/_import/test_import.py "
    "openff/interchange/_tests/unit_tests/operations/test_combine.py"
)
_TEST_CMD = (
    f"python -m pytest -v -rA --no-header --tb=short -p no:cacheprovider {_TEST_FILES}"
)


class OpenFFInterchangeImageBase(Image):
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
        return "condaforge/miniforge3:24.3.0-0"

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

        org = self.pr.org
        repo = self.pr.repo

        # The `# syntax` directive opts this Dockerfile out of DockerfileEnhancer
        # auto-injection, so the canonical scaffolding it would otherwise add
        # (ARG block, proxy/cert ENV, LABEL, cert symlinks, clone -> reset ->
        # checkout ${BASE_COMMIT}, history hardening) is written out by hand
        # here, verbatim from the same constants the enhancer uses.
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

{self.global_env}
# DEBIAN_FRONTEND / LANG are already set by the ENV block above; only the vars
# it does not cover are declared here.
ENV LC_ALL=C.UTF-8
ENV PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'

# validated minimal conda env (see module comment for the pin rationale)
RUN {_ENV_CREATE} && conda clean -y --all

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}

# Editable install + import smoke test run after the hardening block: they only
# touch the conda env, never git state, so the pinned history stays intact.
RUN . {_CONDA_SH} && conda activate {_ENV} && pip install --no-deps -e .
RUN . {_CONDA_SH} && conda activate {_ENV} \\
    && python -c "import openmm, rdkit, openff.toolkit, openff.interchange; print('imports ok')"

{self.clear_env}

CMD ["/bin/bash"]
"""


class OpenFFInterchangeImageDefault(Image):
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
        return OpenFFInterchangeImageBase(self.pr, self._config)

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
cd /home/{pr.repo}
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: /home/{pr.repo} is not a git repository" >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "check_git_changes: working tree is dirty" >&2
  git status --porcelain >&2
  exit 1
fi
echo "check_git_changes: working tree is clean"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
. {conda_sh}
conda activate {env}
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
pip install --no-deps -e . || true
""".format(conda_sh=_CONDA_SH, env=_ENV, pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
. {conda_sh}
conda activate {env}
cd /home/{pr.repo}
{test_cmd}
""".format(conda_sh=_CONDA_SH, env=_ENV, pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
. {conda_sh}
conda activate {env}
cd /home/{pr.repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(conda_sh=_CONDA_SH, env=_ENV, pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
. {conda_sh}
conda activate {env}
cd /home/{pr.repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(conda_sh=_CONDA_SH, env=_ENV, pr=self.pr, test_cmd=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/


RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("openforcefield", "openff-interchange")
class OpenFFInterchange(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenFFInterchangeImageDefault(self.pr, self._config)

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

        # pytest -v progress lines:  path::Class::test[param] PASSED [ 42%]
        progress = re.compile(
            r"^(\S+::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b",
            re.MULTILINE,
        )
        # pytest -rA short-summary lines:  PASSED path::Class::test[param]
        summary = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)",
            re.MULTILINE,
        )

        def record(name: str, status: str) -> None:
            if status in ("PASSED", "XPASS"):
                passed.add(name)
            elif status in ("FAILED", "ERROR"):
                failed.add(name)
            else:
                skipped.add(name)

        for name, status in progress.findall(clean):
            record(name, status)
        for status, name in summary.findall(clean):
            record(name, status)

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
