"""scikit-learn/scikit-learn -- single era, PR interval 16261 -> 11296 (0.23.dev0).

Era boundary. All five dataset PRs sit inside five weeks of master:

    pr-16076  4fe4d27eb1b8  2020-01-09      pr-15980  54a09dc86462  2020-02-02
    pr-11296  6b646da89c51  2020-01-31      pr-16261  10703551c301  2020-02-04
    pr-15436  d591d8c467b2  2020-02-13

At every one of those commits sklearn/__init__.py pins __version__ = '0.23.dev0',
there is no pyproject.toml (setup.py + numpy.distutils only), and setup.py
declares python_requires=">=3.5" with azure-pipelines exercising 3.5 through 3.8.
Nothing in the toolchain moves across the range, so ONE base image
(`base-16261-to-11296`) serves all five PRs and there is no era split to make.

Where the pin lives. The shared base only installs the toolchain and clones; it
performs no checkout and no history scrub. Both live in the per-PR image, where
${BASE_COMMIT} means exactly one commit - that PR's own base.sha - so each of the
five instances is pinned to its own commit and everything after it is pruned.
Doing it one layer down, in the shared base, would pin all five PRs to whichever
commit won image dedup and prune the other four out of existence (R10).

Registration. The dataset rows carry no `number_interval` and no `tag`, so
Instance.create() (instance.py:41-49) computes the plain key
`scikit-learn/scikit-learn`. Per HOW_TO_CREATE_REPO_CONFIG R26/#17.4 that makes
range-named files unreachable, which is why this is `scikit_learn.py` and not
`scikit_learn_16261_to_11296.py`.

Test selection. The graded command runs pytest over exactly the test files the
PR's own test.patch touches, discovered at run time from /home/test.patch, so
the three stages always run the same targets and a stage never pays for the
~20k-test full suite. Every target file already exists at its PR's base commit,
so the run stage produces a real baseline rather than an empty one.

Test ids. Names are emitted from the junit XML as repo-relative pytest node ids
(`sklearn/svm/tests/test_svm.py::test_x[param]`) because report.py's
_test_name_matches_files (report.py:385) splits a name on `::` and compares the
head against the test_patch file paths. A console-scraped id, or one rooted
anywhere but the repo, never matches and the n2p/cheating-guard classification
silently misfires (R20).
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The console -v text is NOT parsed. pytest's short summary prints
# "FAILED sklearn/x.py::test_y - ValueError: ..." and any regex over that
# captures the error message INTO the id, so the same test carries a different
# id at the test stage than at the fix stage and the transition is lost (R3).
# parse_log reads only the block between these markers, which is generated from
# --junitxml and therefore carries no timings, no worker ids and no messages.
BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="


class ImageBase(Image):
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
        # 0.23.dev0's azure-pipelines matrix tops out at Python 3.8, and the
        # Cython 0.29 sources here do not compile under 3.9+. bookworm rather
        # than the era-contemporary buster/bullseye: bookworm is still served
        # from deb.debian.org, so apt-get update needs no archive.debian.org
        # rewrite (R11).
        return "python:3.8-slim-bookworm"

    def image_tag(self) -> str:
        # ONE base for the whole range (R4: unique per era, and there is exactly
        # one era here). See dockerfile() for how the R10 mis-pinning trap is
        # avoided.
        return "base-16261-to-11296"

    def workdir(self) -> str:
        return "base-16261-to-11296"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # The clone and the WORKDIR live here, in the ONE shared base. The pin and
        # the history scrub deliberately do NOT - they are per-PR and live in
        # ImageDefault.dockerfile(), where ${BASE_COMMIT} means exactly one commit.
        #
        # The opt_out block below is a deliberate marker, not dead code. Read it
        # before deleting it. DockerfileEnhancer._inject_final_sanitize
        # (image.py:389) appends its OWN ${BASE_COMMIT}-pinned scrub to any base
        # Dockerfile whose text contains `git clone`, and skips doing so only when
        # the text already carries its sentinel assert. This base must not be
        # pinned: build_dataset.py:624 fills ${BASE_COMMIT} for a base image from
        # whichever PR's Image object survives image dedup - pr-11296 here, purely
        # because it is first in the JSONL - so an injected pin would fix all five
        # PRs at that one commit and prune the other four out of the object store.
        # The later PRs would then die in their own layer with
        # "fatal: reference is not a tree".
        opt_out = (
            "# Opt out of DockerfileEnhancer._inject_final_sanitize (image.py:389).\n"
            "# Its sentinel is the line quoted below; carrying it here suppresses the\n"
            "# injection of a ${BASE_COMMIT}-pinned scrub into this SHARED base, which\n"
            "# would pin all five PRs to whichever commit won image dedup. The real\n"
            "# checkout and the real scrub - with all four integrity asserts - run in\n"
            "# the per-PR layer, pinned to that PR's own base.sha:\n"
            '#   test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"'
        )

        # Do NOT set DEBIAN_FRONTEND or LANG here - DockerfileEnhancer injects
        # both into this (base) Dockerfile and duplicates would result.
        return f"""FROM {image_name}

{self.global_env}

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_ROOT_USER_ACTION=ignore
ENV OMP_NUM_THREADS=1
ENV OPENBLAS_NUM_THREADS=1
ENV MKL_NUM_THREADS=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

# setuptools is pinned below 60 because sklearn 0.23.dev0 builds through
# numpy.distutils, which setuptools 60's distutils hijack breaks; pip is pinned
# because this tree has no pyproject.toml and therefore needs pip's legacy
# `setup.py develop` editable path to still exist.
RUN python -m pip install --no-cache-dir "pip==23.0.1" "setuptools==59.8.0" "wheel==0.37.1"

# Scientific stack, pinned so that every package resolves to a prebuilt cp38
# wheel on BOTH linux/amd64 and linux/arm64 - nothing but scikit-learn itself is
# ever compiled from source. That constraint, not recency, drives these numbers:
# the era-contemporary trio (numpy 1.18.5 / scipy 1.4.1 / pandas 1.0.5, all early
# 2020) ships NO aarch64 wheel at all, so on the arm64 leg pip would fall back to
# a source build - and scipy 1.4.1 from source needs gfortran plus BLAS/LAPACK
# headers and does not survive gcc 12. These are the earliest releases carrying
# cp38 manylinux aarch64 wheels (verified against the PyPI JSON API):
#     numpy  1.19.5  (1.19.0 is the first with aarch64)
#     scipy  1.5.4   (1.5.0 has none)
#     pandas 1.1.5   (1.1.0 has none)
# All satisfy setup.py's floors for 0.23.dev0 (numpy>=1.11, scipy>=0.17).
# pandas and Pillow are runtime test dependencies, not build ones:
# sklearn/datasets/tests/test_base.py skips the as_frame cases without pandas and
# conftest.py skips the image doctests without Pillow, and a skipped gold test can
# never produce a FAIL->PASS.
RUN python -m pip install --no-cache-dir \\
    "numpy==1.19.5" "scipy==1.5.4" "Cython==0.29.21" "joblib==0.14.1" \\
    "pandas==1.1.5" "Pillow==7.2.0" "pytest==5.3.5"

{clone}

WORKDIR /home/{self.pr.repo}

{opt_out}

{self.clear_env}

CMD ["/bin/bash"]
"""


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        # ONE definition of the graded command, interpolated verbatim into all
        # three stage scripts below. This is what enforces R3: the stages cannot
        # drift apart, because run.sh, test-run.sh and fix-run.sh are generated
        # from this same string and differ ONLY in the patch application that
        # precedes it. Kept as a local rather than a staged run_tests.sh so no
        # extra file is COPY'd into the image.
        #
        # print_test_detail() emits one line per test case as a real repo-relative
        # pytest node id, wrapped in the markers parse_log() keys on. Reading the
        # junit XML rather than scraping the console keeps the id in a structured
        # field, so a pytest short-summary line ("FAILED <id> - ValueError: ...")
        # can never bleed an error message into a name and desync it between the
        # test and fix stages (R3/R4B). The `file` attribute is what pytest records
        # for the testcase, so the head of the id is exactly the path that appears
        # in test.patch, which is what report.py:385 matches on (R20).
        # <<'PY' is quoted so the shell expands nothing inside the body.
        test_body = """
print_test_detail() {{
    echo "{begin}"
    python - <<'PY'
import os
import xml.etree.ElementTree as ET

PATH = "/home/results.xml"

if os.path.exists(PATH):
    try:
        root = ET.parse(PATH).getroot()
    except ET.ParseError:
        root = None

    if root is not None:
        for tc in root.iter("testcase"):
            classname = tc.get("classname") or ""
            path = tc.get("file") or ""
            if not path:
                # No @file (older junit_family): rebuild the path from the
                # dotted classname, dropping a trailing CamelCase segment
                # because that is the test class, not a package.
                parts = classname.split(".")
                if len(parts) > 1 and parts[-1][:1].isupper():
                    parts = parts[:-1]
                path = "/".join(parts) + ".py"

            # Recover the class, when there is one, so two same-named methods
            # in different classes of one file stay distinct names.
            module = path[:-3].replace("/", ".") if path.endswith(".py") else ""
            cls = ""
            if module and classname.startswith(module + "."):
                cls = classname[len(module) + 1:]

            name = tc.get("name") or ""
            # pytest escapes newlines inside parametrized ids, but guarantee it:
            # a raw newline would split the TESTCASE line and corrupt the id.
            name = name.replace("\\r", " ").replace("\\n", " ")

            status = "PASSED"
            for child in tc:
                if child.tag in ("failure", "error"):
                    status = "FAILED"
                    break
                if child.tag == "skipped":
                    status = "SKIPPED"
                    break

            node_id = path + "::" + (cls + "::" if cls else "") + name
            print("TESTCASE " + node_id + " " + status)
PY
    echo "{end}"
}}

# never inherit the previous stage's results
rm -f /home/results.xml

# The graded targets are the test files this PR's own test.patch touches, read
# from the patch rather than hardcoded, so run/test/fix stages always agree.
set +e
TEST_FILES=$(grep -E '^\\+\\+\\+ b/' /home/test.patch \\
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \\
    | grep -E '(^|/)(test_[^/]*\\.py|[^/]*_test\\.py)$' \\
    | sort -u)
set -e

TEST_TARGETS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        TEST_TARGETS="$TEST_TARGETS $f"
    fi
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    print_test_detail
    exit 0
fi

echo "running pytest on:$TEST_TARGETS"

# --override-ini=addopts= : setup.cfg wires --doctest-modules and a set of
#     --ignore paths into addopts; doctest items would add ids that are not
#     pytest node ids and make collection depend on the installed numpy repr.
# -p no:cacheprovider     : .pytest_cache would dirty the work tree between
#     stages.
# --continue-on-collection-errors : one bad import must not zero a whole stage.
# -e is lifted only around the test call: at the test stage the suite is
# SUPPOSED to fail, and dying here would report zero tests and satisfy
# report.py's "fix something" check vacuously.
set +e
python -m pytest $TEST_TARGETS -v \\
    -p no:cacheprovider \\
    --continue-on-collection-errors \\
    --override-ini=addopts= \\
    --junitxml=/home/results.xml
RC=$?
set -e
echo "TEST_EXIT_CODE=$RC"

print_test_detail
""".format(begin=BEGIN_MARKER, end=END_MARKER)

        stage_header = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "\n"
            "export CI=true\n"
            "\n"
            f"cd /home/{repo}\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

python --version

# Compile the C/Cython extensions in PARALLEL before the editable install.
# 0.23.dev0's own n_jobs (sklearn/_build_utils/__init__.py:66) only parallelises
# the .pyx -> .c transpile; the C/C++ compile itself is numpy.distutils build_ext,
# which is strictly SERIAL unless handed -j. One serial compiler is tolerable on
# native amd64 (~10 min) but not under QEMU, where every arm64 instruction is
# software-translated - the base image measured that penalty at roughly 11x.
# `--parallel (-j)` is accepted by build_ext at these base commits (verified).
# Capped at 4 rather than $(nproc): buildx builds both platform legs
# concurrently, so this is really 8 compilers, and the emulated ones are memory
# hungry on a 7.7 GiB VM.
python setup.py build_ext --inplace -j 4

# No build isolation: this tree has no pyproject.toml, so the build must see
# the numpy/Cython already pinned in the base image rather than resolving a
# modern pair that cannot compile 0.23.dev0's .pyx sources. The extensions are
# already built by the step above, so this step is incremental and cheap.
python -m pip install --no-cache-dir --no-build-isolation -e /home/{repo}

# HARD GATE - not tolerant. If the base tree cannot import or collect, no stage
# can produce results, and that must fail HERE rather than surfacing as an
# unexplained empty report three stages later.
python -c "import sklearn; print('sklearn', sklearn.__version__)"
python -c "import numpy, scipy, pandas, pytest, joblib; print('deps ok')"

echo "DEPS_OK"
""".format(repo=repo, sha=sha),
            ),
            File(
                ".",
                "run.sh",
                stage_header + test_body,
            ),
            File(
                ".",
                "test-run.sh",
                stage_header
                + 'if ! git apply --whitespace=nowarn /home/test.patch; then\n'
                + '    echo "Error: git apply test.patch failed" >&2\n'
                + "    exit 1\n"
                + "fi\n"
                + test_body,
            ),
            File(
                ".",
                "fix-run.sh",
                stage_header
                + "if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then\n"
                + '    echo "Error: git apply test.patch+fix.patch failed" >&2\n'
                + "    exit 1\n"
                + "fi\n"
                + test_body,
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

        # The pin and the scrub live HERE, not in the shared base. One layer down
        # ${BASE_COMMIT} would be ambiguous across the five PRs; at this layer it is
        # exactly one PR's own base.sha, so every instance is pinned to its own
        # commit and every commit after it is pruned out of the object store.
        # Nothing from a later PR's future is reachable in this image.
        #
        # The clone and WORKDIR are NOT repeated here - both are inherited from the
        # shared base, which clones once and leaves WORKDIR at /home/<repo>.
        #
        # The ARG default is required, not decorative: build_dataset.py:622 only
        # passes REPO_URL/BASE_COMMIT as build-args when dependency() returns a
        # STRING. This image depends on an Image object, so it receives no
        # build-args at all, and DockerfileEnhancer.enhance (image.py:315) returns
        # the text verbatim for the same reason - it injects no ARG either.
        # Declaring it here is what makes ${BASE_COMMIT} below resolve.
        args = f'ARG BASE_COMMIT="{self.pr.base.sha}"'

        # `--aggressive` is dropped from the stock block: on a repo this size it
        # spends tens of minutes recomputing deltas and changes only compression.
        # `git gc --prune=now` plus `git repack -a -d -l` still removes every
        # unreachable object, which is the guarantee that matters.
        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{args}

{copy_commands}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("scikit-learn", "scikit-learn")
class SCIKIT_LEARN(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI before any matching.
        test_log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        # Greedy id capture with the status as the FINAL token: pytest
        # parametrized ids embed reprs that can contain spaces, so a non-greedy
        # or \S+ capture would silently truncate them.
        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in test_log.splitlines():
            stripped = line.strip()

            if stripped.startswith(BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(END_MARKER):
                in_detail = False
                continue
            # Everything outside the markers is raw pytest console output and is
            # ignored, so the "FAILED <id> - <error>" summary lines can never
            # pollute a test id.
            if not in_detail:
                continue

            match = case_re.match(stripped)
            if not match:
                continue

            name, status = match.group(1), match.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # Failure wins; each test lands in exactly one bucket (R2).
        passed_tests -= failed_tests
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
