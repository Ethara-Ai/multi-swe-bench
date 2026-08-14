import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_TESTLIB_SH = r"""
# Source this from run.sh / test-run.sh / fix-run.sh, from inside the repo root.
# Provides: z3_apply <patch> <label>
#           z3_restore_test_files <patch> <base_sha>
#           run_z3_tests <patch1> [patch2 ...]
#
# z3's unit tests live in src/test/<module>.cpp and are all driven by one
# `test-z3` binary: `./test-z3 <module>` runs a single module and prints "PASS"
# on stdout, plus a "(test <module> :time ... )" line that timeit writes to
# STDERR. Two consequences shape everything below:
#
#   1. A z3 unit test signals failure by ASSERTING, which aborts the process.
#      Running the whole suite in one process (`./test-z3 /a`) therefore lets
#      the first failing module suppress every module after it -- they report
#      as absent rather than failed. We run one process per module instead.
#   2. `/a` is worse than just coarse: main.cpp guards each module with
#      `for (i = 0; i < argc; ++i) if (test_all || ...)`, so with `/a` every
#      module runs once per argv entry -- the entire suite executes twice.
#
# We normalise everything to "PASS: <module>" / "FAIL: <module>" on stdout so
# parse_log never has to depend on cross-stream (stdout vs stderr) ordering.

set +e

Z3_REPO_DIR="$(pwd)"
Z3_BUILD_DIR="${Z3_REPO_DIR}/build"
Z3_TEST_BIN="${Z3_BUILD_DIR}/test-z3"
# Per-module wall-clock cap. z3 has a few genuinely slow modules (nlsat, rcf,
# polynomial); a module that exceeds this is reported FAIL, never left silent.
Z3_TEST_TIMEOUT="${Z3_TEST_TIMEOUT:-600}"

export LD_LIBRARY_PATH="${Z3_BUILD_DIR}:${LD_LIBRARY_PATH:-}"

# Drop the per-file sections of a patch that describe a BINARY file. Several
# gold patches in this dataset were produced without `git diff --binary`, so a
# changed PDF or image appears as a bare "Binary files ... differ" stanza with
# no payload and no full index line. git apply rejects the WHOLE patch over one
# of those, which otherwise loses every C++ hunk in the file. Emits the dropped
# paths on stdout so the log always says what was excluded.
z3_strip_binary_diffs() {
    python3 - "$1" "$2" <<'PY'
import re, sys

src, dst = sys.argv[1], sys.argv[2]
with open(src, "r", errors="replace", newline="") as fh:
    text = fh.read()

kept, dropped = [], []
for part in re.split(r"(?m)^(?=diff --git )", text):
    if not part.strip():
        continue
    if part.startswith("diff --git") and (
        "GIT binary patch" in part
        or re.search(r"(?m)^Binary files .* differ\s*$", part)
    ):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", part)
        dropped.append(m.group(2) if m else "<unknown>")
        continue
    kept.append(part)

with open(dst, "w", newline="") as fh:
    fh.write("".join(kept))

for d in dropped:
    print(d)
PY
}

# Apply a patch, preferring a clean application and falling back to a 3-way
# merge, then to the same patch with unappliable binary stanzas removed.
# Unlike `git apply --reject ... || true`, a total failure is reported to the
# caller instead of being swallowed, so a patch that does not apply becomes
# FAILs rather than an empty log.
z3_apply() {
    local patch="$1" label="$2" stripped dropped
    [ -s "$patch" ] || return 0

    if git apply --whitespace=nowarn "$patch" 2>/tmp/z3_apply_err; then
        return 0
    fi
    if git apply --whitespace=nowarn --3way "$patch" 2>>/tmp/z3_apply_err; then
        echo "z3-harness: ${label} applied via 3-way merge"
        return 0
    fi

    stripped="/tmp/$(basename "$patch").nobin"
    dropped=$(z3_strip_binary_diffs "$patch" "$stripped")
    if [ -n "$dropped" ]; then
        echo "z3-harness: ${label}: excluding binary file diffs git apply cannot apply:"
        printf 'z3-harness:   %s\n' $dropped
        if git apply --whitespace=nowarn "$stripped" 2>>/tmp/z3_apply_err \
           || git apply --whitespace=nowarn --3way "$stripped" 2>>/tmp/z3_apply_err; then
            echo "z3-harness: ${label} applied with binary diffs excluded"
            return 0
        fi
    fi

    echo "z3-harness: ${label} FAILED to apply"
    sed 's/^/z3-harness: /' /tmp/z3_apply_err
    return 1
}

# Anti-tamper: hard-restore the graded surface to its base-commit content before
# the gold test patch is applied, so a fix patch (gold, or model-authored at
# evaluation time) cannot reach the tests that decide the score.
#
# The whole src/test tree is reverted, not merely the files the gold test patch
# names. Restoring only the named files leaves a cheap hole: every module is
# dispatched by the TST macro in src/test/main.cpp, which prints the "PASS" this
# harness keys on, so a patch that edits main.cpp -- a file most gold test
# patches do not touch, and therefore invisible to both the grader-side
# MSB-REWARD-003 check and report.py's credited-test guard, since both compare
# only against the gold test patch's file list -- could make any module report
# PASS without running. No gold fix patch in this dataset touches src/test at
# all, so reverting it wholesale costs nothing legitimate.
#
# This is also the backstop for a malformed fix patch: both guards parse the
# patch with unidiff and fail open (returning "no tampering") when it does not
# parse. This restore does not read the fix patch at all, so it holds regardless.
z3_restore_test_files() {
    local patch="$1" base_sha="$2" f

    if git rev-parse --verify --quiet "${base_sha}:src/test" >/dev/null 2>&1; then
        git checkout "${base_sha}" -- src/test 2>/dev/null \
            || echo "z3-harness: could not restore src/test"
        # `git checkout` restores tracked files but leaves anything the fix patch
        # ADDED under src/test in place; drop those too.
        git clean -fdq -- src/test 2>/dev/null || true
    fi

    [ -s "$patch" ] || return 0

    while IFS= read -r f; do
        [ -z "$f" ] && continue
        # A file the test patch CREATES does not exist at base; nothing to
        # restore, and `git checkout` would fail on it.
        if git cat-file -e "${base_sha}:${f}" 2>/dev/null; then
            git checkout "${base_sha}" -- "$f" 2>/dev/null \
                || echo "z3-harness: could not restore ${f}"
        else
            rm -f "$f"
        fi
    done <<EOF
$(z3_patch_files "$patch")
EOF
}

z3_patch_files() {
    python3 - "$@" <<'PY'
import os, re, sys

for path in sys.argv[1:]:
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        continue
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(.+?) b/(.+?)\s*$", line)
            if m:
                print(m.group(2))
PY
}

# The canonical test-name set for this PR: every src/test/<module>.cpp the
# patches touch. Derived from the patch TEXT, not from the working tree, so all
# three stages (run / test-run / fix-run) report on exactly the same names --
# which is what lets report.py classify F2P vs P2P instead of seeing phantoms.
z3_patch_modules() {
    python3 - "$@" <<'PY'
import os, re, sys

mods = set()
for path in sys.argv[1:]:
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        continue
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(.+?) b/(.+?)\s*$", line)
            if not m:
                continue
            f = m.group(2)
            if f.startswith("src/test/") and f.endswith(".cpp"):
                mods.add(os.path.basename(f)[:-4])

# main.cpp is the driver that dispatches the modules, not a module itself.
mods.discard("main")
for m in sorted(mods):
    print(m)
PY
}

# Modules the built binary can actually dispatch without extra argv. `test-z3 /h`
# lists every registered module; the TST_ARGV ones (cnf_backbones, ddnf,
# expr_rand, sat_local_search, sat_lookahead, datalog_parser_file) are printed as
# "<name>(...)" because they demand arguments, and are unreachable for us -- they
# are excluded so they are never miscounted as failures.
z3_runnable_modules() {
    "$Z3_TEST_BIN" /h 2>/dev/null | sed -n 's/^    \([A-Za-z0-9_]\{1,\}\)$/\1/p'
}

# Configure + build libz3 and the test driver. No `-- -k` (keep-going) and no
# `|| true`: a build that fails must surface as a non-zero return so the caller
# can emit FAIL for every module, rather than silently running a stale binary.
z3_build() {
    mkdir -p "$Z3_BUILD_DIR" || return 1
    cd "$Z3_BUILD_DIR" || return 1

    cmake .. \
        -DCMAKE_BUILD_TYPE=Release \
        -DZ3_BUILD_TEST_EXECUTABLES=ON \
        -DZ3_BUILD_LIBZ3_SHARED=ON \
        -DZ3_BUILD_PYTHON_BINDINGS=OFF \
        -DZ3_BUILD_DOCUMENTATION=OFF \
        -DZ3_ENABLE_EXAMPLE_TARGETS=OFF || { cd "$Z3_REPO_DIR"; return 1; }

    # test-z3 is EXCLUDE_FROM_ALL in src/test/CMakeLists.txt, so the default
    # `all` target does NOT produce it -- it must be named explicitly.
    cmake --build . --target test-z3 -j"$(nproc)" || { cd "$Z3_REPO_DIR"; return 1; }

    cd "$Z3_REPO_DIR"
    test -x "$Z3_TEST_BIN"
}

_z3_fail_all() {
    local m
    while IFS= read -r m; do
        [ -n "$m" ] && echo "FAIL: $m"
    done <<EOF
$1
EOF
}

run_z3_tests() {
    local wanted runnable m

    wanted=$(z3_patch_modules "$@")
    if [ -z "$wanted" ]; then
        echo "z3-harness: no src/test modules touched by this PR's patches"
        return 0
    fi

    if ! z3_build; then
        # An un-buildable tree scores zero, not "no data". Without this, a patch
        # that breaks the compile produced an EMPTY log, which report.py reads as
        # TestStatus.NONE -- indistinguishable from a test that never existed.
        echo "z3-harness: build failed; reporting every candidate module as FAIL"
        _z3_fail_all "$wanted"
        return 0
    fi

    runnable=$(z3_runnable_modules)

    while IFS= read -r m; do
        [ -z "$m" ] && continue
        # Not dispatchable at this commit (new module not yet added, or TST_ARGV):
        # emit nothing so it reads as NONE in every stage rather than a fake FAIL.
        if ! printf '%s\n' "$runnable" | grep -qx -- "$m"; then
            continue
        fi

        # One process per module: these tests abort on failure, so a shared
        # process would hide every module after the first failing one.
        if (cd "$Z3_BUILD_DIR" && timeout "$Z3_TEST_TIMEOUT" "$Z3_TEST_BIN" "$m") \
                >/tmp/z3_mod_out 2>&1 && grep -qx 'PASS' /tmp/z3_mod_out; then
            echo "PASS: $m"
        else
            echo "FAIL: $m"
        fi
    done <<EOF
$wanted
EOF
}
"""


class Z3ImageBase(Image):
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
        return "gcc:13"

    def image_tag(self) -> str:
        # One shared toolchain base for every PR. The base no longer clones or
        # pins the repo, so nothing in it varies per PR and a single tag is
        # correct. Clone + checkout + hardening moved to Z3ImageDefault, which
        # still interpolates the LITERAL base.sha (never the BASE_COMMIT arg),
        # so "cannot be re-pinned at build time" holds exactly as before.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Base image: clone, pin to base.sha, then sanitise git history.

        The leading ``# syntax=docker/dockerfile:1.6`` is the documented opt-out
        -- ``DockerfileEnhancer.enhance()`` returns a Dockerfile verbatim once it
        sees that directive. The previous revision omitted it and relied on the
        enhancer's ``_standardize_repo_fetch`` regex to *retrofit* the checkout
        and the hardening block onto a hardcoded ``git clone`` line. That worked,
        but only for as long as the emitted line kept matching the enhancer's
        pattern; a stray flag or a quoted URL would silently drop the hardening
        and ship an image with the fix commit still reachable. Spelling it out
        here makes the guarantee local to this file and independent of pipeline
        regexes.

        This base is tagged per base.sha (``base-<sha8>``), so pinning it to one
        commit is correct rather than destructive -- each PR gets its own base.

        The clone is unconditional (the old ``config.need_clone`` branch that
        emitted ``COPY {repo} /home/{repo}`` is gone): ``build_dataset.build_image``
        only copies a local checkout when the dependency is NOT a string, so a
        base image like this one never receives one.

        The hardening interpolates the LITERAL base.sha rather than the
        ``${BASE_COMMIT}`` build arg: an ARG can be overridden at build time
        (``--build-arg BASE_COMMIT=...``) and would re-pin the image to another
        commit, which is exactly the guarantee this block exists to make
        unforgeable.
        """
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        base_sha = self.pr.base.sha

        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", base_sha)

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
# Declared but deliberately unused: build_dataset passes --build-arg BASE_COMMIT
# for every string-dependency image, and an undeclared arg is a build warning.
# The hardening below uses the literal sha so overriding this cannot re-pin it.
ARG BASE_COMMIT=""

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git cmake make python3 python3-pip \\
    ninja-build coreutils \\
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages --no-cache-dir setuptools

RUN git config --global --add safe.directory /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Z3ImageDefault(Image):
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
        return Z3ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        bootstrap_cmd = ""
        if self.pr.number <= 869:
            bootstrap_cmd = "python3 contrib/cmake/bootstrap.py create\n"

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

""",
            ),
            File(
                ".",
                "testlib.sh",
                _TESTLIB_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
{bootstrap_cmd}mkdir -p build

# Warm the build cache at base.sha so the three evaluation stages only have to
# recompile what their patches actually touch. Without this every stage pays a
# full from-scratch libz3 build, which is what pushed the previous revision's
# runs past their timeout and left all 27 instances with empty logs. Failure is
# tolerated here: this is purely a cache, and the stages rebuild anyway.
source /home/testlib.sh
z3_build || echo "prepare: warm build did not complete; stages will build from scratch"

""".format(pr=self.pr, bootstrap_cmd=bootstrap_cmd),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uo pipefail

cd /home/{pr.repo}
source /home/testlib.sh

# Baseline: neither patch applied. Establishes which of this PR's candidate
# modules already existed and passed before the PR.
run_z3_tests /home/test.patch /home/fix.patch
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uo pipefail

cd /home/{pr.repo}
source /home/testlib.sh

z3_restore_test_files /home/test.patch {pr.base.sha}
if ! z3_apply /home/test.patch "test.patch"; then
  echo "z3-harness: cannot evaluate without the gold tests"
fi

run_z3_tests /home/test.patch /home/fix.patch
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uo pipefail

cd /home/{pr.repo}
source /home/testlib.sh

# Order matters. The fix patch (gold during dataset generation, model-authored
# during evaluation, where run_evaluation.py bind-mounts it over /home/fix.patch)
# goes on FIRST; the gold test tree is then hard-restored from base.sha and the
# gold test patch applied on top. A fix patch therefore cannot influence the
# tests that grade it, whatever it contains.
z3_apply /home/fix.patch "fix.patch"
FIX_RC=$?

z3_restore_test_files /home/test.patch {pr.base.sha}
z3_apply /home/test.patch "test.patch"

if [ "$FIX_RC" -ne 0 ]; then
  echo "z3-harness: fix.patch did not apply; tests run against the unpatched tree"
fi

run_z3_tests /home/test.patch /home/fix.patch
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        """PR image: layered on the already-hardened, already-pinned base.

        ``dependency()`` returns an ``Image``, so ``DockerfileEnhancer.enhance()``
        returns this file verbatim and injects nothing -- the hardening has to be
        stated here or not at all. The base layer already sanitised history at
        this PR's base.sha and ``prepare.sh`` only resets and rebuilds, so this
        block is a cheap re-assertion (there is nothing left to collect) that
        keeps the guarantee true for the image that is actually shipped and run,
        rather than only for the layer beneath it.
        """
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = self.pr.repo
        base_sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", base_sha)

        org = self.pr.org

        return f"""FROM {name}:{tag}

# dependency() is an Image, so build_dataset passes no --build-arg for these;
# the literal defaults below are what apply. BASE_COMMIT is declared but NOT
# consumed by the hardening -- that uses the literal sha, so overriding this
# arg cannot re-pin the image.
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT="{base_sha}"

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git config --global --add safe.directory /home/{repo}

RUN git checkout {base_sha}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


def z3_number_interval(pr_numbers) -> str:
    """Format a bundle's PR numbers the way ``number_interval`` requires.

    An interval ENUMERATES the bundle; it is never a span. ``prs_in_bundle``
    ``[146, 147, 150, 155, 157]`` becomes ``"146-147-150-155-157"`` -- writing
    ``"146-157"`` would imply every PR from 146 through 157, including 148, 149,
    151-154 and 156, which are not in the bundle and must not be run.

    Matches ``build_lht_dataset.py`` exactly::

        sorted_pr_numbers = sorted(pr_numbers)
        pr_numbers_str = "-".join(str(n) for n in sorted_pr_numbers)
    """
    return "-".join(str(n) for n in sorted(int(n) for n in pr_numbers))


@Instance.register("Z3Prover", "z3")
class Z3(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Z3ImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # testlib.sh normalises every module to one of these two lines on stdout.
        #
        # It deliberately does NOT parse z3's native output. `test-z3` prints
        # "PASS" to stdout but the accompanying
        # "(test <module> :time .. :before-memory .. :after-memory ..)" line to
        # STDERR (timeit defaults to std::cerr), and the timeit destructor emits
        # it after the PASS. Pairing the two therefore depended on stdout and
        # stderr staying interleaved across Docker's two log streams -- and on
        # the test binary surviving, which a failing z3 test does not, since they
        # fail by aborting.
        re_result = re.compile(r"^(PASS|FAIL):\s+(\S+)$")

        for line in test_log.splitlines():
            match = re_result.match(line.strip())
            if not match:
                continue

            status, test_name = match.group(1), match.group(2)
            if status == "PASS":
                passed_tests.add(test_name)
            else:
                failed_tests.add(test_name)

        # A module that both failed and passed within one log (only reachable if
        # a stage were to run it twice) is counted as failed.
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval routing
#
# Instance.create() routes on f"{org}/{number_interval}" whenever a record
# carries a non-empty number_interval, and only falls back to f"{org}/{repo}"
# (or f"{org}/{repo}_{tag}") when it is empty. So every interval a record can
# carry MUST be registered here as well, or the run dies with
# "Instance 'Z3Prover/<interval>' is not registered" before a single image is
# built. This mirrors the _NUMBER_INTERVALS block in the fastfetch registry.
#
# The dataset currently ships number_interval="" for all 27 records, which routes
# to "Z3Prover/z3" and is why the pipeline works today. The registrations below
# make the interval form resolve to the same config, so populating the field
# cannot break routing.
#
# Note: number_interval only reaches the resolved jsonl by being READ OFF THE
# INPUT DATASET -- gen_report.py looks it up per task_id and copies it onto the
# ReportTask. A registry cannot synthesise it; it can only guarantee that a
# record carrying one still resolves to this config.
_PR_NUMBERS = [
    1630, 1878, 5155, 5348, 5721, 5756, 5923, 6026, 6188, 6209,
    6321, 6707, 7051, 7095, 7133, 7157, 7254, 7408, 7422, 7479,
    7645, 7681, 7695, 7790, 8011, 8543, 8591,
]

_NUMBER_INTERVALS = [
    # Whole-file bundle: every PR in Z3Prover__z3_dataset.jsonl, enumerated.
    z3_number_interval(_PR_NUMBERS),
    # Per-record bundles. Each record in this dataset has its own `number` AND
    # its own base.sha, so each is a bundle in its own right and its interval is
    # the single-element enumeration.
    *(str(_n) for _n in _PR_NUMBERS),
]

for _interval in _NUMBER_INTERVALS:
    Instance.register("Z3Prover", _interval)(Z3)
