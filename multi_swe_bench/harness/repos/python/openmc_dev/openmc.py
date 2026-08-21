import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# OpenMC is a Python API over a compiled C++ core (libopenmc). The target test
# `tests/unit_tests/test_source.py::test_rejection` drives `openmc.lib`
# (ctypes bindings to libopenmc), and the fix patch modifies C++ sources
# (src/source.cpp, include/openmc/source.h). Therefore the fix stage MUST
# rebuild + reinstall the C++ library after applying fix.patch, otherwise the
# already-compiled libopenmc still has the old behaviour and the test can't pass.
#
# Nuclear data (cross sections + ENDF) is required at run time; tools/ci/download-xs.sh
# fetches it to $HOME (/root here) and we point OPENMC_CROSS_SECTIONS / OPENMC_ENDF_DATA
# at it in every run script.
_OPENMC_ENV = (
    "export OPENMC_CROSS_SECTIONS=/root/nndc_hdf5/cross_sections.xml\n"
    "export OPENMC_ENDF_DATA=/root/endf-b-vii.1/"
)

# Same pytest invocation in all three run scripts (only the applied patches differ),
# scoped to the unit-test tree that contains the target test — gives broad p2p
# coverage without the heavy/slow regression suite.
# `test_deplete_activation.py::test_activation` is a stochastic Monte-Carlo
# depletion test (reaction rates compared to a 1% tolerance) — it flip-flops
# nondeterministically across runs (observed run=FAIL/test=PASS/fix=FAIL), which
# trips the report's "no PASS->FAIL between test and fix" guard and invalidates
# an otherwise-clean instance. Deselect it (identically in all three scripts so
# the f2p comparison stays honest).
_PYTEST = (
    "pytest -v --no-header -rA --tb=no -p no:cacheprovider "
    "--ignore=tests/unit_tests/test_deplete_activation.py "
    "tests/unit_tests"
)


class OpenmcImageBase(Image):
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
        # openmc-dev/openmc @ 1d140e34 (Sept 2022, ref develop) declares
        # python_requires>=3.6 and builds a C++ core via CMake/HDF5.
        # ubuntu:22.04 ships Python 3.10 + a modern gcc/CMake — matches the
        # existing openmc_dev era config's base.
        return "ubuntu:22.04"

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

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential g++ cmake \\
    libhdf5-dev libopenblas-dev \\
    python3 python3-pip python3-dev \\
    wget xz-utils \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class OpenmcImageDefault(Image):
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
        return OpenmcImageBase(self.pr, self.config)

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

# Pin build deps to the PR's era (Sept 2022): Cython <3 and NumPy <1.24 keep
# the C-extension + Python API buildable against openmc @ this commit.
pip3 install --no-cache-dir --upgrade pip
pip3 install --no-cache-dir "cython<3.0" "numpy<1.24" "scipy<1.10"

# Build + install the C++ core (libopenmc) so openmc.lib can load it.
mkdir -p build && cd build && cmake -DCMAKE_BUILD_TYPE=Release .. && make -j"$(nproc)" && make install && cd ..
ldconfig

# Install the Python package + test extras (pytest/pytest-cov/colorama).
pip3 install --no-cache-dir -e .[test]

# Fetch nuclear data to $HOME. The repo's tools/ci/download-xs.sh streams via
# `wget -O - | tar` which corrupts the whole extraction on any mid-stream hiccup
# (Box.com throttles/truncates). Download to a file with retries + resume instead.
if [ ! -e /root/nndc_hdf5/cross_sections.xml ]; then
    wget --tries=5 --retry-connrefused --waitretry=15 --read-timeout=180 --continue -O /root/nndc_hdf5.tar.xz https://anl.box.com/shared/static/teaup95cqv8s9nn56hfn7ku8mmelr95p.xz
    tar -C /root -xJf /root/nndc_hdf5.tar.xz
    rm -f /root/nndc_hdf5.tar.xz
fi
if [ ! -d /root/endf-b-vii.1/neutrons ]; then
    wget --tries=5 --retry-connrefused --waitretry=15 --read-timeout=180 --continue -O /root/endf-b-vii.1.tar.xz https://anl.box.com/shared/static/4kd2gxnf4gtk4w1c8eua5fsua22kvgjb.xz
    tar -C /root -xJf /root/endf-b-vii.1.tar.xz
    rm -f /root/endf-b-vii.1.tar.xz
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{env}
{pytest}

""".format(pr=self.pr, env=_OPENMC_ENV, pytest=_PYTEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{env}
{pytest}

""".format(pr=self.pr, env=_OPENMC_ENV, pytest=_PYTEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
# The fix modifies C++ sources (src/source.cpp, include/openmc/source.h), so the
# compiled core must be rebuilt + reinstalled before the test can observe it.
cd build && make -j"$(nproc)" && make install && cd ..
ldconfig
{env}
{pytest}

""".format(pr=self.pr, env=_OPENMC_ENV, pytest=_PYTEST),
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


@Instance.register("openmc-dev", "openmc")
class Openmc(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenmcImageDefault(self.pr, self._config)

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
        # Strip ANSI colour codes first so matching is robust against pytest's
        # coloured output.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest `-rA` renders lines as either "<nodeid> PASSED" or
        # "PASSED <nodeid>"; capture both. nodeids start with "tests/".
        pattern1 = re.compile(r"(tests/[^ \t]+)\s+(PASSED|FAILED|SKIPPED|ERROR)\b")
        pattern2 = re.compile(r"\b(PASSED|FAILED|SKIPPED|ERROR)\s+(tests/[^ \t]+)")
        for line in log.splitlines():
            m = pattern1.search(line)
            if m:
                test_name, status = m.group(1).strip(), m.group(2)
            else:
                m = pattern2.search(line)
                if not m:
                    continue
                status, test_name = m.group(1), m.group(2).strip()

            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Enforce TestResult invariants: the three sets must be disjoint.
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
