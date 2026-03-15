import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AMReXImageBase(Image):
    """Base image for AMReX - builds AMReX from source"""

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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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

WORKDIR /home/

RUN apt-get update && apt-get install -y \\
    build-essential \\
    git \\
    cmake \\
    patch \\
    gfortran \\
    ca-certificates \\
    libhdf5-openmpi-dev \\
    openmpi-bin \\
    libfftw3-dev \\
    && rm -rf /var/lib/apt/lists/* \\
    && ln -sf /usr/bin/make /usr/bin/gmake || true

{code}

ENV AMREX_HOME=/home/amrex

{self.clear_env}

"""


class AMReXImageDefault(Image):
    """Instance-specific image for AMReX"""

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
        return AMReXImageBase(self.pr, self._config)

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
                "pr_info.txt",
                f"""pr_number:{self.pr.number}
title:{self.pr.title}
base_sha:{self.pr.base.sha}
""",
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/amrex
git reset --hard
bash /home/check_git_changes.sh
git fetch origin {pr.base.sha}
git checkout {pr.base.sha}
rm -rf build
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set +e

cd /home/amrex || exit 1

echo "Baseline test (no patches applied)"
echo "TEST_RESULT: PASS amrex_baseline"
exit 0
""",
            ),
            File(
                ".",
                "apply_patches.sh",
                """#!/bin/bash
# Apply patches with fallback to patch --fuzz=3
# Usage: bash /home/apply_patches.sh <patch_file> [patch_file2 ...]

set -e
cd /home/amrex

for PATCH_FILE in "$@"; do
    if [ ! -f "$PATCH_FILE" ]; then
        echo "Patch file not found: $PATCH_FILE"
        exit 1
    fi
    echo "Applying $PATCH_FILE..."
    git apply --ignore-space-change --ignore-whitespace "$PATCH_FILE" 2>&1 || {
        echo "git apply failed for $PATCH_FILE, retrying with patch --fuzz=3..."
        git checkout -- . 2>/dev/null || true
        patch --batch --fuzz=3 -p1 < "$PATCH_FILE" 2>&1 || {
            echo "ERROR: Patch failed to apply: $PATCH_FILE"
            exit 1
        }
    }
    echo "Applied $PATCH_FILE successfully"
done
""",
            ),
            File(
                ".",
                "build_and_test.sh",
                """#!/bin/bash
# Build AMReX with tests and run them using ctest
# Outputs structured test results for parse_log
# Usage: bash /home/build_and_test.sh

set -e
cd /home/amrex

NCPUS=$(nproc)

# Determine which test directories are present from the test patch
TEST_DIRS=$(grep "^diff --git a/Tests/" /home/test.patch 2>/dev/null | \\
    awk '{print $3}' | sed 's|^a/Tests/\\([^/]*\\).*|\\1|' | sort -u | \\
    grep -v "^CMakeLists.txt$" || true)

echo "=== AMReX Test Build ==="
echo "Test directories from patch: $TEST_DIRS"

# Strategy 1: Try CMake from repo root with tests enabled
echo "=== Strategy: CMake from repo root ==="
rm -rf build && mkdir -p build && cd build

CMAKE_RESULT=0
cmake .. \\
    -DCMAKE_BUILD_TYPE=Debug \\
    -DAMReX_SPACEDIM=3 \\
    -DAMReX_FORTRAN=OFF \\
    -DAMReX_MPI=OFF \\
    -DAMReX_OMP=OFF \\
    -DAMReX_PARTICLES=ON \\
    -DAMReX_ENABLE_TESTS=ON \\
    -DAMReX_BUILD_TUTORIALS=OFF 2>&1 || CMAKE_RESULT=$?

if [ $CMAKE_RESULT -ne 0 ]; then
    echo "CMake configuration failed, trying GNUmakefile fallback..."
    cd /home/amrex
    rm -rf build
    # Fall through to GNUmakefile strategy below
else
    BUILD_RESULT=0
    cmake --build . -j${NCPUS} 2>&1 || BUILD_RESULT=$?

    if [ $BUILD_RESULT -ne 0 ]; then
        echo "CMake build failed, trying GNUmakefile fallback..."
        cd /home/amrex
        rm -rf build
        # Fall through to GNUmakefile strategy below
    else
        # CMake build succeeded, run ctest
        echo "=== Running ctest ==="
        set +e
        CTEST_OUTPUT=$(ctest --output-on-failure --verbose 2>&1)
        CTEST_EXIT=$?
        set -e
        echo "$CTEST_OUTPUT"

        # Check if ctest actually found and ran tests (reuse output from above)
        if echo "$CTEST_OUTPUT" | grep -q "No tests were found"; then
            echo "ctest found no tests, trying GNUmakefile fallback..."
            cd /home/amrex
            rm -rf build
            # Fall through to GNUmakefile strategy below
        else
            echo "=== ctest completed with exit code: $CTEST_EXIT ==="
            exit $CTEST_EXIT
        fi
    fi
fi

# Strategy 2: GNUmakefile per-directory build
echo "=== Strategy: GNUmakefile per-directory ==="
cd /home/amrex

OVERALL_EXIT=0
TESTS_RUN=0

for dir in $TEST_DIRS; do
    TEST_PATH="/home/amrex/Tests/$dir"
    if [ ! -d "$TEST_PATH" ]; then
        continue
    fi

    # Find a buildable sub-path (could be the dir itself or a subdirectory)
    BUILD_PATH=""
    if [ -f "$TEST_PATH/GNUmakefile" ]; then
        BUILD_PATH="$TEST_PATH"
    else
        # Check immediate subdirectories for GNUmakefile
        for subdir in "$TEST_PATH"/*/; do
            if [ -f "$subdir/GNUmakefile" ]; then
                BUILD_PATH="$subdir"
                break
            fi
        done
    fi

    if [ -z "$BUILD_PATH" ]; then
        echo "TEST_RESULT: SKIP ${dir} (no GNUmakefile found)"
        continue
    fi

    echo "=== Building test: $dir ==="
    cd "$BUILD_PATH"

    set +e
    make -j${NCPUS} 2>&1
    MAKE_EXIT=$?
    set -e

    if [ $MAKE_EXIT -ne 0 ]; then
        echo "TEST_RESULT: FAIL ${dir}"
        OVERALL_EXIT=1
        TESTS_RUN=$((TESTS_RUN + 1))
        cd /home/amrex
        continue
    fi

    # Find executable
    TEST_EXE=$(find . -maxdepth 1 -type f -executable \\( -name "*.exe" -o -name "*.ex" \\) 2>/dev/null | head -1)
    if [ -z "$TEST_EXE" ]; then
        TEST_EXE=$(find . -maxdepth 1 -type f -executable ! -name "GNUmakefile" ! -name "*.H" ! -name "*.cpp" ! -name "*.o" ! -name "Makefile" 2>/dev/null | head -1)
    fi

    if [ -z "$TEST_EXE" ]; then
        echo "TEST_RESULT: FAIL ${dir} (no executable found)"
        OVERALL_EXIT=1
        TESTS_RUN=$((TESTS_RUN + 1))
        cd /home/amrex
        continue
    fi

    echo "Running: $TEST_EXE for test $dir"
    set +e
    # Try to find inputs file
    if [ -f inputs ]; then
        "$TEST_EXE" inputs 2>&1
    elif [ -f ../inputs ]; then
        "$TEST_EXE" ../inputs 2>&1
    else
        "$TEST_EXE" 2>&1
    fi
    RUN_EXIT=$?
    set -e

    if [ $RUN_EXIT -eq 0 ]; then
        echo "TEST_RESULT: PASS ${dir}"
    else
        echo "TEST_RESULT: FAIL ${dir}"
        OVERALL_EXIT=1
    fi
    TESTS_RUN=$((TESTS_RUN + 1))

    cd /home/amrex
done

if [ $TESTS_RUN -eq 0 ]; then
    echo "No tests could be built or run"
    echo "TEST_RESULT: FAIL amrex_no_tests"
    exit 1
fi

echo "=== GNUmakefile tests completed: $TESTS_RUN tests run ==="
exit $OVERALL_EXIT
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set +e

cd /home/amrex || { echo "TEST_RESULT: FAIL amrex_cd_failed"; exit 1; }

echo "Refreshing git index..."
git status > /dev/null 2>&1 || true

# Apply test patch only
bash /home/apply_patches.sh /home/test.patch
if [ $? -ne 0 ]; then
    echo "TEST_RESULT: FAIL amrex_test_patch_apply"
    exit 1
fi

# Build and run tests
bash /home/build_and_test.sh
exit $?
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set +e

cd /home/amrex || { echo "TEST_RESULT: FAIL amrex_cd_failed"; exit 1; }

echo "Refreshing git index..."
git status > /dev/null 2>&1 || true

# Apply test patch + fix patch
bash /home/apply_patches.sh /home/test.patch /home/fix.patch
if [ $? -ne 0 ]; then
    echo "TEST_RESULT: FAIL amrex_fix_patch_apply"
    exit 1
fi

# Build and run tests
bash /home/build_and_test.sh
exit $?
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = """RUN chmod +x /home/*.sh && \\
    bash /home/prepare.sh"""

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/amrex

{self.clear_env}

"""


@Instance.register("AMReX-Codes", "amrex")
class AMReX(Instance):
    """AMReX instance"""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return AMReXImageDefault(self.pr, self._config)

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
    def _normalize_test_name(name: str) -> str:
        """Normalize AMReX test names to match between ctest and GNUmakefile.

        ctest: ``Particles_ParticleMesh_3d`` -> ``Particles_ParticleMesh``
        GNUmakefile: ``Particles`` (directory name)
        We strip ``_1d``/``_2d``/``_3d`` suffixes so the dimension-agnostic
        name survives across both paths.
        """
        return re.sub(r"_[123]d$", "", name)

    @staticmethod
    def _directory_prefix(name: str) -> str:
        """Extract the top-level test directory from a ctest-style name.

        ``Particles_ParticleMesh`` -> ``Particles``
        ``CallNoinline`` -> ``CallNoinline``
        ``LinearSolvers_CurlCurl`` -> ``LinearSolvers``

        This matches the GNUmakefile convention where the test name is just
        the Tests/<dir> directory name.
        """
        # Known multi-word directory prefixes in AMReX Tests/
        _KNOWN_PREFIXES = [
            "LinearSolvers", "Advection_AmrLevel", "Advection_AmrCore",
            "Amr_Advection_AmrLevel", "Amr_Advection_AmrCore",
            "MultiBlock",
        ]
        for prefix in _KNOWN_PREFIXES:
            normalized_prefix = prefix.replace("/", "_")
            if name.startswith(normalized_prefix + "_") or name == normalized_prefix:
                return normalized_prefix
        parts = name.split("_")
        return parts[0] if parts else name

    def parse_log(self, test_log: str) -> TestResult:
        """Parse AMReX test output from ctest (``1/5 Test #1: name ... Passed``)
        and GNUmakefile scripts (``TEST_RESULT: PASS name``)."""
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Regex: ctest "1/5 Test #1: test_name ........ Passed/***Failed/***Not Run"
        re_ctest_pass = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed"
        )
        re_ctest_fail = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Failed"
        )
        re_ctest_skip = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Not Run"
        )

        # Regex: custom "TEST_RESULT: PASS/FAIL/SKIP [test_name]"
        re_custom_pass = re.compile(r"^TEST_RESULT:\s*PASS\s+(\S+)")
        re_custom_fail = re.compile(r"^TEST_RESULT:\s*FAIL\s+(\S+)")
        re_custom_skip = re.compile(r"^TEST_RESULT:\s*SKIP\s+(\S+)")
        re_bare_pass = re.compile(r"^TEST_RESULT:\s*PASS\s*$")
        re_bare_fail = re.compile(r"^TEST_RESULT:\s*FAIL\s*$")

        has_any_result = False

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for regex, target in [
                (re_ctest_pass, passed_tests),
                (re_ctest_fail, failed_tests),
                (re_ctest_skip, skipped_tests),
                (re_custom_pass, passed_tests),
                (re_custom_fail, failed_tests),
                (re_custom_skip, skipped_tests),
            ]:
                m = regex.match(line)
                if m:
                    normalized = self._normalize_test_name(m.group(1).strip())
                    target.add(normalized)
                    prefix = self._directory_prefix(normalized)
                    if prefix != normalized:
                        target.add(prefix)
                    has_any_result = True
                    break
            else:
                if re_bare_pass.match(line):
                    passed_tests.add("amrex_test")
                    has_any_result = True
                elif re_bare_fail.match(line):
                    failed_tests.add("amrex_test")
                    has_any_result = True

        if not has_any_result:
            _BUILD_ERROR_MARKERS = [
                "Build failed", "CMake configuration failed", "make: ***",
                "compilation terminated", "ERROR: No test executable found",
                "ERROR: Could not determine test directory",
                "ERROR: Patch failed to apply", "No tests could be built or run",
            ]
            _COMPILE_ERROR_PATTERNS = [
                "undefined reference", "was not declared", "no such file",
            ]
            has_build_error = False
            for line in test_log.splitlines():
                stripped = line.strip()
                if any(marker in stripped for marker in _BUILD_ERROR_MARKERS):
                    has_build_error = True
                    break
                low = stripped.lower()
                if "error:" in low and any(p in low for p in _COMPILE_ERROR_PATTERNS):
                    has_build_error = True
                    break

            failed_tests.add("amrex_build" if has_build_error else "amrex_unknown")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
