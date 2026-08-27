import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        # python:3.8-slim, era-matched. The repo's own CI at this commit runs a
        # [3.6, 3.7] matrix plus a 3.8 job, and requirements.txt pins a 2021-era
        # scientific stack: numpy <1.21.3, scikit-image <0.18.0, scipy <1.7.2.
        # Those versions have no wheels for modern Pythons and would have to build
        # from source. 3.8 is the newest interpreter this commit was tested on and
        # the newest with wheels for every pin.
        #
        # Single layer, deliberately: docker_util._get_container_builder() routes any
        # build with a platform set through the docker-container buildx driver, which
        # cannot see images loaded into the local daemon, so a `FROM <our-own-base>`
        # split is unbuildable here. Returning a str also keeps DockerfileEnhancer
        # engaged, which performs the BASE_COMMIT checkout and history scrub.
        return "python:3.8-slim"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Mirrors the repo's CI line (`python -m pytest --ignore=tests/benchmarks`),
        # minus the coverage flags which add nothing here.
        #
        # -v is required, not cosmetic: the default dot output prints no test names at
        # all, and parse_log would see an empty suite and score it as "these tests do
        # not exist". Verbose gives `path::Class::test STATUS`, whose head before the
        # first "::" is the real file path - which is what report.py's
        # _test_name_matches_files needs to credit a newly added test.
        #
        # --tb=no keeps tracebacks out of the log. A traceback can contain a line that
        # looks like a test id, and it bloats the run log for no parsing benefit.
        #
        # -p no:cacheprovider stops pytest writing .pytest_cache into the tree.
        #
        # `|| true` so a non-zero exit (expected in the test stage) does not kill the
        # script before the log is captured. A genuinely broken environment cannot hide
        # behind it, because the image refuses to seal unless the build-time gate below
        # collected tests successfully.
        # --continue-on-collection-errors is load bearing. pytest ABORTS THE WHOLE
        # SESSION on a collection error ("Interrupted: N errors during collection"),
        # running nothing at all. That happens in the test stage here: the new tests do
        # `from histolab.exceptions import SlidePropertyError`, and that exception is
        # added by the FIX patch, so both test_slide.py modules fail to import. Measured
        # without the flag: run 630 passed / test 0 / fix 640 - the entire test stage
        # reported nothing, turning 600+ healthy tests into NONE. With it, only the two
        # un-importable modules drop out and the rest still report honestly.
        cmd = (
            "python -m pytest --ignore=tests/benchmarks "
            "-v --tb=no -p no:cacheprovider --continue-on-collection-errors || true"
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{cmd}
""".format(pr=self.pr, cmd=cmd),
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
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image}

{self.global_env}

# pip renders its progress bar with non-ASCII block characters. The harness decodes
# buildx output with the platform default codec (cp1252 on Windows), where those bytes
# are undefined and abort the build with "'charmap' codec can't decode byte ...".
ENV PIP_PROGRESS_BAR=off
ENV PIP_NO_COLOR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1

# DELIBERATELY NOT SETTING `CI`. tests/unitutil.py defines on_ci() as
# `"CI" in os.environ`, and tests/fixtures/__init__.py has:
#     if not os.path.exists(slide_path) and on_ci():
#         raise ValueError(f"no SVS fixture found at {{slide_path}}")
# The new integration test parametrises over EXTERNAL_SVS.LIVER_1, and pytest evaluates
# parametrize arguments at COLLECTION time - before skipif is considered. So with CI set,
# collecting tests/integration/test_slide.py raises and the whole module errors out, even
# though those cases are marked skipif(not on_ci()). With CI unset the loader returns a
# path without raising, the three external cases skip, and the one case backed by a
# repo-shipped fixture still runs.

# openslide-tools provides libopenslide, the C library openslide-python binds to. Without
# it `import openslide` fails at import time and EVERY test errors during collection -
# which reads downstream as "these tests do not exist" rather than as a broken image.
# gcc/g++ are kept as a fallback for any pinned dependency without a cp38 wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl openslide-tools gcc g++ \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

# DockerfileEnhancer rewrites the clone above and appends its own WORKDIR, reset --hard
# and checkout BASE_COMMIT, then the history-scrub block whose assertions fail the build
# unless HEAD is exactly BASE_COMMIT. Repeating any of that here would be dead code. The
# WORKDIR is kept so the install steps below do not depend on the enhancer's line order.
WORKDIR /home/{self.pr.repo}

# numpy is installed FIRST, on its own, purely so the next step can work on arm64.
# scikit-image 0.17.2 publishes NO aarch64 wheel (only manylinux1_x86_64), so on arm64 pip
# falls back to the 29.8 MB sdist, and that sdist's legacy setup.py does
# `openmp_build_ext()` which imports numpy at metadata-generation time. Under pip's default
# build isolation numpy is absent from the build env, so it dies with
# "ModuleNotFoundError: No module named 'numpy'" -> metadata-generation-failed, and the
# whole multi-arch build fails even though amd64 succeeded (amd64 gets a prebuilt wheel and
# never compiles anything).
#
# The version range mirrors requirements.txt so the pre-install cannot drift from what the
# project asks for. Only scikit-image needs this: scipy 1.7.1, Pillow and openslide-python
# all ship aarch64 wheels, so no gfortran/BLAS toolchain is required.
RUN pip install --no-cache-dir "numpy>=1.18.4,<1.21.3" wheel

# --no-build-isolation lets the scikit-image sdist build see the numpy installed above.
# Everything else resolves to a wheel on both architectures, so this changes nothing on
# amd64 - it only unblocks the one source build on arm64.
#
# Mirrors the repo's own CI (.github/workflows/tests.yml):
#     python -m pip install -e .[testing]
#     python -m pip install pooch==1.4.0
# Editable so `import histolab` resolves to the checked-out tree rather than a copy - the
# fix patch edits src/histolab/slide.py, and a non-editable install would leave the graded
# stages testing stale code. The [testing] extra supplies pytest and friends.
RUN pip install --no-cache-dir --no-build-isolation -e ".[testing]"

# pooch is NOT optional and NOT a linting tool, despite how it reads in the CI file.
# src/histolab/data/__init__.py does `from requests.exceptions import HTTPError`, and
# `requests` appears nowhere in requirements.txt - it arrives only as a pooch dependency.
# Without this line every test module that imports tests/fixtures fails at collection with
# ModuleNotFoundError: No module named 'requests'. Measured: 3 tests collected with 19
# collection errors without it, 644 tests collected and 0 errors with it.
RUN pip install --no-cache-dir pooch==1.4.0

# Refuse to seal an image whose graded stages could not report anything. Collection is
# the real risk here: a missing libopenslide, or an EXTERNAL_SVS fixture raising, makes
# every test error during collection while the command still exits in a way that looks
# survivable. --collect-only proves the suite is importable and non-empty at BASE_COMMIT.
RUN python -c "import openslide, histolab" \\
    && python -m pytest --ignore=tests/benchmarks --collect-only -q > /tmp/collect.txt 2>&1 \\
    && tail -3 /tmp/collect.txt \\
    && grep -qE "[0-9]+ tests? collected" /tmp/collect.txt

WORKDIR /home/

{copy_commands}
{self.clear_env}

"""


@Instance.register("histolab", "histolab")
class Histolab(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", log)

        # pytest -v prints one line per test:
        #   tests/unit/test_slide.py::Describe_Slide::it_knows_its_magnification PASSED [ 45%]
        #   tests/integration/test_slide.py::Describe_Slide::it_knows_its_magnification_factors[...] SKIPPED
        #
        # The id is kept whole, including any [param] suffix, because this suite is
        # heavily parametrised and two params of the same function genuinely are two
        # tests - collapsing them would let one result mask another.
        #
        # The status is NOT anchored at the end: pytest appends a progress percentage,
        # and SKIPPED/XFAIL often carry a trailing reason in parentheses.
        line_re = re.compile(
            r"^(\S+::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )

        for raw in log.split("\n"):
            m = line_re.match(raw.strip())
            if not m:
                continue
            test_id, status = m.group(1), m.group(2)

            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_id)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_id)
            else:
                # SKIPPED and XFAIL are both "did not run to a pass" without being a
                # failure. Reporting XFAIL as failed would invent a FAIL->PASS the
                # moment an expected failure starts passing.
                skipped_tests.add(test_id)

        # A rerun test can be reported twice; enforce one bucket each, or the stage
        # comparison double-counts and invents transitions.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
