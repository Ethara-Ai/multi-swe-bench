# tjw-lint/jest-serializer-vue-tjw -- single-era config covering the whole
# dataset (one PR, #28, base commit 67459e3a from 2020-01).
#
# Two-tier image, built base-first:
#
#   ImageBase     node:13.6.0-buster + the cloned repo         tag base-pr-28
#   ImageDefault  ImageBase + patches + scripts + node_modules tag pr-28
#
# The split is not cosmetic.  build_dataset.build_image() populates the
# REPO_URL / BASE_COMMIT build args only when `dependency()` returns a str
# (build_dataset.py:623-629), and DockerfileEnhancer.enhance() likewise bails
# out with `if not isinstance(dep, str): return raw`.  So the base layer is the
# only one that receives the standardized repo fetch, the proxy/cert/OCI
# infrastructure and the history-hardening block; the PR layer is a plain
# Dockerfile stacked on top of it and must not expect any of that machinery.
#
# The Node pin (node:13.6.0-buster) is load-bearing.  The base commit locks
# @babel/preset-env 7.8.3, whose @babel/helper-compilation-targets ships
# "exports": false in package.json -- an opt-out written for the era when the
# exports field was still experimental.  Every Node that treats exports as
# stable (12.17+, 13.7+, 14, 16, 20, 22) reads false as "nothing is exported"
# and every test suite dies with ERR_PACKAGE_PATH_NOT_EXPORTED before a single
# test runs.  Verified against this commit: node:14-bullseye and node:12.22
# fail that way, node:13.6.0 and node:12.16.3 do not.  .travis.yml at this
# commit says node_js: "13", so 13.6.0 (released 2020-01-07, three weeks before
# the PR merged) is both the closest era match and a working one.
#
# node 13 and Debian buster are both EOL today.  That is unavoidable here --
# every supported Node breaks this commit -- but it carries one trap: buster's
# apt repositories have moved to archive.debian.org, and the harness helper
# that rewrites them (Image._is_deprecated_debian) matches only base images
# starting with "debian:buster", never "node:*-buster".  Neither image installs
# apt packages, and none should be added without first repointing sources.list
# at archive.debian.org by hand.
#
# Dependencies are installed with `npm ci`, not `npm install`.  package.json
# carries caret ranges that today resolve to releases years newer than the
# committed package-lock.json (lockfileVersion 1), which reintroduces the same
# babel breakage from the other direction.  The lock is honoured instead.
# fix.patch does edit package-lock.json, but only to add coveralls and bump
# eslint-plugin-jsdoc -- both lint/coverage-only, neither reachable from
# `npx jest` -- so the baked node_modules stays valid once the patch is applied
# and the graded runs need no reinstall and no network.
#
# Multi-arch: verified on linux/amd64 and linux/arm64.  node:13.6.0-buster
# publishes amd64, arm64/v8, arm/v7, ppc64le and s390x; neither image installs
# apt packages or downloads architecture-specific binaries; and the only native
# dependency in the lock is fsevents, which is darwin-only and optional.  All
# three stages were run under arm64 emulation and produced test counts
# identical to amd64 (13 / 17+10 / 27).
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Toolchain + cloned source. Built before the PR image."""

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
        return "node:13.6.0-buster"

    def image_prefix(self) -> str:
        return "envagent"

    # PR-scoped base tag ("base-pr-28"), per the harness Dockerfile QC contract.
    #
    # This is load-bearing, not cosmetic.  DockerfileEnhancer rewrites the clone
    # below into a checkout of ${BASE_COMMIT} followed by the hardening block,
    # which deletes every ref and prunes every object unreachable from that
    # commit.  Images are deduplicated on image_full_name(), so a bare "base"
    # tag would let a second PR silently reuse the first PR's base image -- an
    # image that physically cannot check out the second PR's commit.  Scoping
    # the tag to the PR number makes that collision impossible.
    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # The single `git clone` line is intentionally the only repo setup
        # here.  DockerfileEnhancer._standardize_repo_fetch() rewrites it into
        # the parameterized clone plus WORKDIR, `git reset --hard`,
        # `git checkout ${BASE_COMMIT}`, the hardening block and CMD.  Writing
        # our own WORKDIR/reset/checkout as well would duplicate all of it.
        # DEBIAN_FRONTEND, LANG, TZ, the proxy/cert ENV block and the OCI
        # labels are injected by the enhancer too and must not be repeated.
        #
        # No apt layer: node:13.6.0-buster already ships git, and buster's
        # package repositories are archived (see the module docstring).
        return """
FROM node:13.6.0-buster

WORKDIR /home/
RUN git clone https://github.com/tjw-lint/jest-serializer-vue-tjw.git /home/jest-serializer-vue-tjw
"""


class ImageDefault(Image):
    """Per-PR layer: patches, graded scripts, installed dependencies."""

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

    # Every graded script exports CI=true before invoking jest, and that is a
    # correctness requirement rather than a convention here.  Jest defaults its
    # `ci` option to the is-ci probe, and with CI unset a MISSING snapshot is
    # written to disk and the test reported as passing.  This repo is a
    # snapshot serializer whose test patch introduces 14 new snapshot entries,
    # so an unset CI turns "snapshot absent" into a fabricated PASS and
    # corrupts the f2p classification silently.  Verified directly: deleting
    # one entry from List.test.js.snap yields "1 snapshot written / 1 passed"
    # without CI, and "New snapshot was not written / 1 failed" with CI=true.
    #
    # --verbose is required too: with more than one suite in the run jest
    # collapses per-test output into a summary and parse_log would see zero
    # tests.  The 2>&1 is required because jest writes its whole reporter
    # stream to stderr.  Coverage is deliberately left off even though
    # fix.patch rewires `npm test` to add --coverage; jest is invoked directly
    # so all three scripts share one identical command, and coverage only adds
    # a table that parse_log would have to skip past.
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
            # Integrity guard invoked by prepare.sh on both sides of the
            # checkout.  Without it the reset and checkout still happen but
            # nothing verifies they took, so a stray edit or a half-applied
            # patch would leave a dirty tree and every graded run would
            # silently test modified code while still looking like a clean
            # pass/fail.
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
            # The checkout is a no-op against the hardened base image (HEAD is
            # already BASE_COMMIT) but is kept so this layer is correct on its
            # own terms rather than by relying on the base.  The clean-tree
            # assert runs on BOTH sides of it, per the QC contract.
            #
            # `npm ci || true` follows the harness convention that a failed
            # dependency install must not abort the image build: a partial
            # install surfaces downstream as an empty TestResult, which
            # Report.check() rejects on the fix_patch_result.all_count > 0
            # rule, rather than as an opaque build error.  It runs AFTER the
            # asserts so the untracked node_modules tree it creates can never
            # trip them (node_modules is gitignored, but ordering makes the
            # guarantee independent of .gitignore).
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

npm ci || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
npx jest --runInBand --verbose 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npx jest --runInBand --verbose 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npx jest --runInBand --verbose 2>&1

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # This layer is NOT run through DockerfileEnhancer (its dependency() is
        # an Image, not a str), so it must stand on its own: no ${BASE_COMMIT}
        # or ${REPO_URL} references, and no reliance on enhancer-injected ARGs.
        # The proxy/cert ENV and the checked-out tree are inherited from the
        # base image layer.
        return f"""FROM {name}:{tag}

{copy_commands}
# Bake node_modules into the image so the graded runs need no network.
RUN bash /home/prepare.sh
"""


@Instance.register("tjw-lint", "jest-serializer-vue-tjw")
class JestSerializerVueTjw(Instance):
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

        # jest --verbose prints an indentation tree per suite:
        #
        #   PASS tests/DataTestIds.test.js
        #     DataTestIds.vue                     <- describe(), indent 2
        #       * Only data-test removed (3ms)    <- test(), indent 4
        #
        # Tests are keyed as pytest-style node IDs:
        #
        #   tests/DataTestIds.test.js::DataTestIds.vue::Only data-test removed
        #   <suite file>            ::<describe chain>::<test name>
        #
        # jest's describe() occupies the same slot as pytest's class segment
        # (file.py::TestClass::test_method), so nested describes simply add
        # further "::" segments.  Keying on the leaf test name alone would be
        # wrong here regardless of format: leaf names repeat across suites in
        # this repo ("Snapshots unchanged" is used by both ObjectAttribute.vue
        # and Recursive.vue) and a set keyed on the leaf silently merges them.
        #
        # Including the suite file means the test/ -> tests/ rename carried by
        # the test patch gives every relocated test a new node ID, so it reads
        # as absent (NONE) in the baseline run rather than as the same test.
        # That is only an annotation on the `run` column: f2p and p2p are
        # derived from the test-patch and fix-patch stages, both of which use
        # the post-rename paths, so neither count is affected.
        ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        duration = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)$")
        status_marks = {
            "✓": passed_tests,  # check mark
            "✔": passed_tests,  # heavy check mark
            "✕": failed_tests,  # multiplication x
            "✗": failed_tests,  # ballot x
            "×": failed_tests,  # multiplication sign
            "○": skipped_tests,  # white circle
            "◯": skipped_tests,  # large circle
        }

        # Suite file from the most recent "PASS <path>" / "FAIL <path>" header.
        # jest appends a duration to that header for slow suites
        # ("PASS tests/Foo.test.js (5.123 s)"), which is stripped so the node
        # ID stays stable across stages.
        suite_file: Optional[str] = None
        # (indent, name) for each currently open describe level.
        describe_stack: list[tuple[int, str]] = []
        # Failure details for a suite are printed after its test list, indented
        # like describes and containing arbitrary snapshot-diff text.  Once the
        # first bullet of a suite is seen, stop feeding the describe stack until
        # the next PASS/FAIL header resets it.
        in_failure_detail = False

        for raw_line in log.split("\n"):
            line = ansi.sub("", raw_line).rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith(("PASS ", "FAIL ")):
                suite_file = duration.sub("", stripped[5:].strip()) or None
                describe_stack.clear()
                in_failure_detail = False
                continue

            if stripped.startswith("●"):  # black circle, failure heading
                in_failure_detail = True
                continue

            indent = len(line) - len(line.lstrip())
            mark = stripped[0]

            if mark in status_marks:
                name = duration.sub("", stripped[1:].strip())
                segments = [n for i, n in describe_stack if i < indent]
                segments.append(name)
                if suite_file:
                    segments.insert(0, suite_file)
                status_marks[mark].add("::".join(segments))
                continue

            if in_failure_detail:
                continue

            # Anything else inside a suite block is a describe() heading.
            while describe_stack and describe_stack[-1][0] >= indent:
                describe_stack.pop()
            describe_stack.append((indent, stripped))

        # TestResult.__post_init__ raises ValueError if the three sets overlap.
        # Nothing in the observed jest output produces an overlap, but a repeat
        # run of the same test name (jest.retryTimes, or two suites that share
        # both a describe and a test name) would, and a ValueError here aborts
        # the whole instance.  Resolve in favour of the worse outcome so a test
        # that ever failed is never recorded as passing.
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
