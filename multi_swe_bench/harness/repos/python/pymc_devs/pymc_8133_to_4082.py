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
        return "python:3.10-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        return [
            "g++",
            "gfortran",
            "libopenblas-dev",
            "liblapack-dev",
            "pkg-config",
        ]

    def extra_setup(self) -> str:
        return r"""RUN pip install --no-cache-dir --upgrade pip "setuptools<70" wheel

# ── Era detection ──────────────────────────────────────────────────────
# This handler spans PRs 4082-8133 which cross three backend eras:
#   1. Theano   (pymc3/, requirements.txt lists theano/theano-pymc)
#   2. Aesara   (pymc3/ or pymc/, requirements.txt lists aesara, no pytensor)
#      We always install aesara>=2.5,<2.8 and patch old API names in source.
#      This avoids scipy<1.8 issues with old aesara's _bvalfromboundary dependency.
#   3. PyTensor (pymc/, requirements.txt lists pytensor)
# We also detect the aeppl Simplex→SimplexTransform rename (0.0.28 boundary).
# ───────────────────────────────────────────────────────────────────────

# Detect era from requirements.txt and set PKG_DIR for grepping source
RUN if grep -qi 'pytensor' requirements.txt 2>/dev/null; then \
        echo "pytensor" > /tmp/pymc_era; \
    elif grep -qi 'aesara' requirements.txt 2>/dev/null; then \
        echo "aesara" > /tmp/pymc_era; \
    elif grep -qi 'theano' requirements.txt 2>/dev/null; then \
        echo "theano" > /tmp/pymc_era; \
    else \
        echo "unknown" > /tmp/pymc_era; \
    fi && \
    if [ -d pymc ]; then echo "pymc" > /tmp/pymc_pkg; \
    elif [ -d pymc3 ]; then echo "pymc3" > /tmp/pymc_pkg; \
    else echo "pymc" > /tmp/pymc_pkg; \
    fi && \
    echo "Detected era=$(cat /tmp/pymc_era) pkg=$(cat /tmp/pymc_pkg)"

# ── Source patching (aesara era) ──────────────────────────────────────
# MUST run BEFORE dependency installation so that the source matches
# the aesara>=2.5 API we're about to install.
# Patches: A_structure→assume_a, CType→Type, default_shape→default_supp_shape
# ───────────────────────────────────────────────────────────────────────
RUN ERA=$(cat /tmp/pymc_era) && PKG=$(cat /tmp/pymc_pkg) && \
    if [ "$ERA" = "aesara" ]; then \
        find "$PKG" -name '*.py' -exec grep -l 'A_structure' {} \; 2>/dev/null | while read pyfile; do \
            sed -i 's/Solve(A_structure="lower_triangular")/Solve(assume_a="gen", lower=True)/g' "$pyfile"; \
            sed -i 's/Solve(A_structure="upper_triangular")/Solve(assume_a="gen", lower=False)/g' "$pyfile"; \
            sed -i "s/Solve(A_structure='lower_triangular')/Solve(assume_a='gen', lower=True)/g" "$pyfile"; \
            sed -i "s/Solve(A_structure='upper_triangular')/Solve(assume_a='gen', lower=False)/g" "$pyfile"; \
            sed -i 's/Solve(A_structure="general")/Solve(assume_a="gen")/g' "$pyfile"; \
            sed -i "s/Solve(A_structure='general')/Solve(assume_a='gen')/g" "$pyfile"; \
        done; \
        find "$PKG" -name '*.py' -exec grep -l 'from aesara.graph.type import CType' {} \; 2>/dev/null | while read pyfile; do \
            sed -i 's/from aesara.graph.type import CType/from aesara.graph.type import Type as CType/g' "$pyfile"; \
        done; \
        find "$PKG" -name '*.py' -exec grep -l 'default_shape_from_params' {} \; 2>/dev/null | while read pyfile; do \
            sed -i 's/default_shape_from_params/default_supp_shape_from_params/g' "$pyfile"; \
        done; \
    elif [ "$ERA" = "theano" ]; then \
        sed -i 's/theano\.config\.gcc\.cxxflags/theano.config.gcc__cxxflags/g' "$PKG/__init__.py" 2>/dev/null || true; \
    fi

# ── Dependency installation ───────────────────────────────────────────
# Aesara era: always aesara>=2.5,<2.8 (source already patched above).
# aeppl boundary: SimplexTransform in transforms.py → aeppl>=0.0.28
#                 otherwise → aeppl>=0.0.6,<0.0.28
# ───────────────────────────────────────────────────────────────────────

RUN ERA=$(cat /tmp/pymc_era) && PKG=$(cat /tmp/pymc_pkg) && \
    if [ "$ERA" = "pytensor" ]; then \
        pip install --no-cache-dir "numpy<2" "scipy<1.14" && \
        printf 'numpy<2\nscipy<1.14\n' > /tmp/constraints.txt; \
    elif [ "$ERA" = "aesara" ]; then \
        AESARA_SPEC="aesara>=2.5.0,<2.8" && \
        SCIPY_SPEC="scipy<1.12" && \
        XARRAY_SPEC="xarray<2024.1" && \
        pip install --no-cache-dir "numpy<1.24" "$SCIPY_SPEC" && \
        TRANSFORMS="$PKG/distributions/transforms.py" && \
        if grep -q 'SimplexTransform' "$TRANSFORMS" 2>/dev/null; then \
            pip install --no-cache-dir "aeppl>=0.0.28,<0.1" "$AESARA_SPEC" "$XARRAY_SPEC"; \
            printf "aeppl>=0.0.28,<0.1\n$AESARA_SPEC\nnumpy<1.24\n$SCIPY_SPEC\n$XARRAY_SPEC\n" > /tmp/constraints.txt; \
        elif [ -f "$TRANSFORMS" ]; then \
            pip install --no-cache-dir "aeppl>=0.0.6,<0.0.28" "$AESARA_SPEC" "$XARRAY_SPEC"; \
            printf "aeppl>=0.0.6,<0.0.28\n$AESARA_SPEC\nnumpy<1.24\n$SCIPY_SPEC\n$XARRAY_SPEC\n" > /tmp/constraints.txt; \
        else \
            pip install --no-cache-dir "$AESARA_SPEC" "$XARRAY_SPEC" 2>/dev/null || true; \
            printf "$AESARA_SPEC\nnumpy<1.24\n$SCIPY_SPEC\n$XARRAY_SPEC\n" > /tmp/constraints.txt; \
        fi; \
    elif [ "$ERA" = "theano" ]; then \
        pip install --no-cache-dir "numpy<1.22" "scipy>=1.4.1,<1.8" "xarray<2022.6" "arviz<0.12" "matplotlib<3.6" "pandas<1.5" && \
        pip install --no-cache-dir --no-build-isolation "theano-pymc==1.0.12" && \
        printf "numpy<1.22\nscipy<1.8\nxarray<2022.6\narviz<0.12\nmatplotlib<3.6\npandas<1.5\n" > /tmp/constraints.txt; \
    else \
        pip install --no-cache-dir "numpy<1.24" "scipy<1.12" && \
        printf 'numpy<1.24\nscipy<1.12\n' > /tmp/constraints.txt; \
    fi

# Set BLAS flags so aesara/theano skips numpy.distutils detection
ENV AESARA_FLAGS="blas__ldflags=-lopenblas"
ENV THEANO_FLAGS="blas__ldflags=-lopenblas"
ENV PYTENSOR_FLAGS="blas__ldflags=-lopenblas"

# Install the project
RUN if [ -f pyproject.toml ]; then \
        pip install --no-cache-dir -c /tmp/constraints.txt -e ".[dev]" 2>/dev/null || \
        pip install --no-cache-dir -c /tmp/constraints.txt -e . 2>/dev/null || \
        (pip install --no-cache-dir -c /tmp/constraints.txt arviz cachetools cloudpickle fastprogress pandas typing-extensions && \
         pip install --no-cache-dir -c /tmp/constraints.txt -e .); \
    elif [ -f setup.py ]; then \
        pip install --no-cache-dir -c /tmp/constraints.txt -r requirements.txt 2>/dev/null; \
        pip install --no-cache-dir -c /tmp/constraints.txt -e . 2>/dev/null || \
        pip install --no-cache-dir -c /tmp/constraints.txt .; \
    fi

# ── Post-install compatibility fixes ─────────────────────────────────
# Theano: force-reinstall theano-pymc (requirements.txt may overwrite with original theano)
# Aesara: inject CType=Type alias into installed package (covers all import forms)
RUN ERA=$(cat /tmp/pymc_era) && \
    if [ "$ERA" = "theano" ]; then \
        pip uninstall -y theano theano-pymc 2>/dev/null || true && \
        THEANO_DIR=$(python -c "import site; print(site.getsitepackages()[0])")/theano && \
        rm -rf "$THEANO_DIR" && \
        pip install --no-cache-dir --no-deps --no-build-isolation "theano-pymc==1.0.12" && \
        ln -s "$THEANO_DIR/scan" "$THEANO_DIR/scan_module"; \
    elif [ "$ERA" = "aesara" ]; then \
        python -c "from aesara.graph.type import CType" 2>/dev/null || \
        (AESARA_TYPE=$(python -c "import aesara.graph.type as t; print(t.__file__)") && \
         echo "CType = Type  # compatibility alias" >> "$AESARA_TYPE" && \
         echo "Added CType alias to aesara.graph.type"); \
    fi

RUN pip install --no-cache-dir -c /tmp/constraints.txt pytest pytest-xdist pytest-cov ipython 2>/dev/null || true"""

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
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

if [ -d "tests" ]; then
    TEST_DIR="tests"
elif [ -d "pymc/tests" ]; then
    TEST_DIR="pymc/tests"
elif [ -d "pymc3/tests" ]; then
    TEST_DIR="pymc3/tests"
else
    TEST_DIR="."
fi

pytest --no-header -rA --tb=no -p no:cacheprovider "$TEST_DIR" --continue-on-collection-errors 2>&1 || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    patch --batch --fuzz=5 -p1 -i /home/test.patch || true
fi

if [ -d "tests" ]; then
    TEST_DIR="tests"
elif [ -d "pymc/tests" ]; then
    TEST_DIR="pymc/tests"
elif [ -d "pymc3/tests" ]; then
    TEST_DIR="pymc3/tests"
else
    TEST_DIR="."
fi

pytest --no-header -rA --tb=no -p no:cacheprovider "$TEST_DIR" --continue-on-collection-errors 2>&1 || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    patch --batch --fuzz=5 -p1 -i /home/test.patch || true
    patch --batch --fuzz=5 -p1 -i /home/fix.patch || true
fi

if [ -d "tests" ]; then
    TEST_DIR="tests"
elif [ -d "pymc/tests" ]; then
    TEST_DIR="pymc/tests"
elif [ -d "pymc3/tests" ]; then
    TEST_DIR="pymc3/tests"
else
    TEST_DIR="."
fi

pytest --no-header -rA --tb=no -p no:cacheprovider "$TEST_DIR" --continue-on-collection-errors 2>&1 || true
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        base = super().dockerfile()
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return base.replace(
            'CMD ["/bin/bash"]',
            f'{copy_commands}\n\nCMD ["/bin/bash"]',
        )


@Instance.register("pymc-devs", "pymc_8133_to_4082")
class PYMC_8133_TO_4082(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        test_results: dict[str, str] = {}

        clean_log = self._strip_ansi(test_log)
        pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED(?: \[[\d]+\])?|XFAIL)\s+(\S+)"
        )
        for line in clean_log.splitlines():
            match = pattern.search(line)
            if match:
                status, test_name = match.group(1), match.group(2)
                test_name = test_name.strip()
                if "FAIL" in status or "ERROR" in status:
                    test_results[test_name] = "failed"
                elif "SKIP" in status or "XFAIL" in status:
                    if test_results.get(test_name) != "failed":
                        test_results[test_name] = "skipped"
                elif "PASS" in status:
                    if test_results.get(test_name) not in ["failed", "skipped"]:
                        test_results[test_name] = "passed"

        for test_name, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_name)
            elif status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
