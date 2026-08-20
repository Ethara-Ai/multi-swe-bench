import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PolicyEngineImageBase(Image):
    """Repo-level base (`images/base/`, tag `:base`).

    Deliberately does NOT override `dockerfile()`: the default in
    `Image.dockerfile()` (harness/image.py:200) emits the canonical
    FROM + apt + `git clone ${REPO_URL}` + `git checkout ${BASE_COMMIT}` +
    hardening sequence, and `DockerfileEnhancer` injects TARGETARCH /
    proxy args / cert symlinks / multi-arch labels on top. Overriding
    here bypasses that and breaks multi-arch buildx / OCI export.
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
        # policyengine-us at this era targets Python 3.10+ (setup.py python_requires
        # >=3.10). 3.11-slim gives us manylinux wheels for numpy/scipy/pandas/
        # matplotlib on both amd64 and arm64, so no BLAS compilation is needed.
        return "python:3.11-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def extra_packages(self) -> list:
        # Defaults from Image.dockerfile() already include build-essential (which
        # provides gcc/g++/make), git, curl, wget, python3, sudo, ca-certificates,
        # gnupg. We only add:
        #   python3-dev  - Python C headers for any wheel-less C-extension deps.
        #   libhdf5-dev  - required at import for `tables`/`h5py`, transitive dep
        #                  of taxcalc's HDF5 data loaders.
        return ["python3-dev", "libhdf5-dev"]

    def files(self) -> list:
        return []


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
        return PolicyEngineImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
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
                """#!/bin/bash
set -e

cd /home/policyengine-us

pip install --upgrade pip setuptools wheel
# Install torch CPU-only FIRST to pre-satisfy the transitive dep from
# survey-enhance (a required dep of policyengine-us at this base commit).
# Without this pin the default resolver picks torch 2.13.0 which unbundles CUDA
# into separate wheels (nvidia-cudnn-cu13, nvidia-cusparselt-cu13, cuda-toolkit,
# triton, ...) and pulls ~5 GB per arch on aarch64 alone, blowing out the build.
# --index-url is scoped to this one command so subsequent `pip install` calls
# still hit PyPI. torch+cpu (~200 MB) satisfies survey-enhance without CUDA.
pip install --index-url https://download.pytorch.org/whl/cpu torch
# `.[dev]` pulls the canonical PolicyEngine dev extras (pytest, black, etc.)
# via setup.py, which at this base commit pins policyengine-core==2.23.1.
# The follow-up install adds behresp/coverage/matplotlib/taxcalc which are
# runtime deps of the YAML tests under policyengine_us/tests/policy/ that
# aren't declared in .[dev].
pip install -e .[dev]
# Post-install pin: policyengine-core 2.23.1 imports nptyping 1.4.4 which uses
# `np.compat.unicode` — that attribute was REMOVED in numpy 2.0, so any numpy
# >=2 crashes on `from policyengine_core.simulations import Microsimulation`
# with `AttributeError: module 'numpy' has no attribute 'compat'`. That import
# is on the path of `policyengine_core.scripts.policyengine_command test`, so
# every YAML stage returns (0,0,0) — the exact failure Phase B just hit.
# matplotlib 3.10+ also floors numpy at >=1.25, so pin the intersection:
# `>=1.25,<2.0` resolves to numpy 1.26.4 which satisfies matplotlib/contourpy
# AND avoids the np.compat removal. Do this AFTER `-e .[dev]` (which resolves
# numpy to 2.4.6 unbounded) so this line downgrades it in place.
pip install 'numpy>=1.25,<2.0' behresp coverage pytest matplotlib taxcalc

# The YAML tests are the primary regression signal for parameter/variable
# changes: `policyengine_core.scripts.policyengine_command test` walks the
# tests/policy/ tree, resolves each YAML fixture against the policyengine_us
# country model, and emits progress lines like
#     policyengine_us/tests/.../va_reduced_itemized_deductions.yaml ......
# plus `FAILED policyengine_us/tests/.../foo.yaml::...` on mismatch, which
# parse_log() below anchors on. `-c policyengine_us` selects the country.
cat > /home/policyengine-us/test_commands.sh <<'RUNNER'
#!/bin/bash
cd /home/policyengine-us
coverage run -a --branch -m policyengine_core.scripts.policyengine_command test policyengine_us/tests/policy/ -c policyengine_us
coverage xml -i
pytest policyengine_us/tests/ --maxfail=0 -v -rA --no-header
RUNNER
chmod +x /home/policyengine-us/test_commands.sh
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/policyengine-us
bash /home/policyengine-us/test_commands.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/policyengine-us
if ! git -C /home/policyengine-us apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/policyengine-us/test_commands.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/policyengine-us
if ! git -C /home/policyengine-us apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/policyengine-us/test_commands.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("PolicyEngine", "policyengine_us_4841_to_4482")
class POLICYENGINE_US_4841_TO_4482(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # YAML tests emit file-level progress lines from policyengine_core's
        # test runner: "policyengine_us/tests/.../foo.yaml ......" (one line
        # per yaml fixture, dots for each in-file assertion). Anchoring on
        # the trailing space is what separates the path from the dots so we
        # don't accidentally match strings embedded in tracebacks.
        progress_pattern = re.compile(r"(policyengine_us/tests/\S+?\.yaml) ")
        all_tests = set(progress_pattern.findall(log))

        # Failures print as `FAILED policyengine_us/tests/.../foo.yaml[::case]`.
        # The optional `::` handles pytest-style yaml sub-node IDs; rstrip(":")
        # normalizes back to the bare path so it set-diffs against progress lines.
        failed_pattern = re.compile(r"FAILED (policyengine_us/tests/\S+?\.yaml::?)")
        failed_tests.update(t.rstrip(":") for t in failed_pattern.findall(log))

        # File-granularity means: a test file is "passed" iff it appeared in
        # progress output AND is not in the failed set. Skipped never emitted
        # by policyengine_core's runner, so keep the set empty rather than
        # invent false skip signal.
        passed_tests = all_tests - failed_tests - skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
