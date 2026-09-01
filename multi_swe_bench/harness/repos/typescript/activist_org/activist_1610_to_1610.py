"""activist-org/activist PR #1610 - Nuxt frontend, yarn berry + vitest.

Every value below was read off the repo at base commit c34b6913, not inferred:

  frontend/package.json   packageManager "yarn@4.10.3"; no `engines` block
                          scripts.test = "vitest run --run"
                          devDependencies vitest 3.2.4
                          a `postinstall` script exists, so `yarn install` runs Nuxt's prepare
  frontend/.yarnrc.yml    nodeLinker: node-modules
  frontend/yarn.lock      present (yarn berry, node-modules linker)
  GitHub primary language TypeScript 57.4%

Two things are worth knowing before changing anything here.

1. HALF THE TEST PATCH IS NOT GRADED, AND THAT IS ACCEPTED. The patch touches eight files in
   two different suites: four under frontend/test/ (vitest unit specs - IconEdit.spec.ts,
   IconDraggableEdit.spec.ts, useUser.spec.ts, setup.ts) and four under frontend/test-e2e/
   (Playwright specs and a shared actions helper). `yarn test` is `vitest run --run`, so only
   the first four execute. Running the Playwright half would need browser binaries plus a live
   app server inside the graded container, which is out of proportion to what it would add:
   the vitest half alone covers the composable and the two components the fix patch changes, so
   the instance still yields f2p. What it costs is coverage, not correctness - a regression
   that only the e2e specs would catch is invisible here. Do not "fix" this by pointing the
   test command at the e2e suite; that would trade an observable signal for an unobservable one.

2. Test identity comes from vitest's JSON reporter, not from its console tree. The previous
   revision matched `✓ <path>.spec.ts` and recorded results at FILE granularity, discarding
   every describe/it inside. Names were stable and collision-free, so nothing corrupted - but a
   file where one test flips FAIL->PASS while another flips PASS->FAIL reads as unchanged, and
   an n2p test added to an existing spec file cannot be told apart from its neighbours. The
   JSON reporter carries the suite chain explicitly, so the name is read rather than
   reconstructed. The console scraper is retained as a fallback for the case where vitest dies
   before writing the report.
"""

from __future__ import annotations

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Written outside the repo so it cannot be swept by `git clean` and cannot be mistaken for a
# tracked file.
_VITEST_JSON = "/tmp/vitest-report.json"

# `--reporter=default` keeps the human tree in the log for debugging AND feeds the fallback
# parser; `--reporter=json` alongside it writes the machine-readable report parse_log prefers.
# vitest accepts repeated --reporter flags and runs both.
#
# The project's own script is `vitest run --run`; `run` already disables watch mode, so the
# graded command is that plus the two reporters.
VITEST_CMD = (
    "yarn vitest run --run "
    f"--reporter=default --reporter=json --outputFile={_VITEST_JSON}"
)

# Exported by every script rather than declared as ENV in the base image, so the generated
# Dockerfile carries exactly one ENV instruction - the one DockerfileEnhancer injects.
#
#   CI                            Check 2C/3C baseline for JS, and vitest reads it: under CI a
#                                 missing snapshot is a failure instead of being written
#                                 silently, so the test and fix stages cannot manufacture their
#                                 own baseline.
#   FORCE_COLOR                   Turns colour off at the source. parse_log strips ANSI anyway,
#                                 but the fallback scraper measures indentation on the raw
#                                 column, so keeping colour out keeps the two paths agreeing.
#   YARN_ENABLE_IMMUTABLE_INSTALLS  yarn berry refuses to modify a lockfile in CI unless this is
#                                 set; required by Check 2B for a berry repo.
SHELL_ENV = """\
export CI=true
export FORCE_COLOR=0
export YARN_ENABLE_IMMUTABLE_INSTALLS=false"""


# Emitted after every graded vitest run and parsed by parse_log.
#
# vitest's JSON reporter uses the same shape as Jest's: a list of SUITES (`testResults`), each
# holding a list of CASES (`assertionResults`). A case carries `ancestorTitles` - the describe()
# chain - separately from `title`, which is exactly the context the console tree forces a parser
# to rebuild from indentation.
#
# The emitted id is `<repo-relative file>::<describe...> > <it>` - the same
# path-qualified shape the delivered instance reports use for pytest
# (`fiasco/tests/test_ion.py::test_x`). report.py reads both halves:
# `_test_name_matches_files` takes everything before the first `::` and compares it
# to test_patch_files, and `_candidate_identifiers` splits the tail on " > " to
# recover the `it()` title for attribution against the patch's added lines.
VITEST_REPORT_PY = r"""#!/usr/bin/env python3
import json
import os
import sys

# vitest's status vocabulary -> the three buckets TestResult accepts.
STATUS = {
    "passed": "PASS",
    "failed": "FAIL",
    "pending": "SKIP",
    "skipped": "SKIP",
    "todo": "SKIP",
}


def main():
    # Node ids must be REPO-relative, not frontend-relative: test_patch_files holds
    # `frontend/test/composables/useUser.spec.ts`, and report.py compares the path half.
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    report = sys.argv[2] if len(sys.argv) > 2 else "vitest-report.json"

    if not os.path.isfile(report):
        # vitest died before writing (config error, OOM, crash in a setup file). Emitting
        # nothing lets parse_log fall back to the console scraper instead of reporting an
        # empty-but-confident result.
        return

    try:
        with open(report) as handle:
            data = json.load(handle)
    except (ValueError, OSError):
        # A run killed mid-write leaves truncated JSON. Same reasoning.
        return

    for suite in data.get("testResults") or []:
        absolute = suite.get("name") or ""
        try:
            relative = os.path.relpath(absolute, root)
        except ValueError:
            relative = absolute
        relative = relative.replace(os.sep, "/")

        for case in suite.get("assertionResults") or []:
            title = (case.get("title") or "").strip()
            if not title:
                continue
            status = STATUS.get(case.get("status") or "")
            if status is None:
                continue
            chain = [
                str(a).strip()
                for a in (case.get("ancestorTitles") or [])
                if str(a).strip()
            ]
            chain.append(title)
            print(
                "VITEST_TESTCASE {0} {1}::{2}".format(
                    status, relative, " > ".join(chain)
                )
            )


if __name__ == "__main__":
    main()
"""


class ActivistFrontendImageBase(Image):
    """Base image for activist frontend (Node 22, yarn berry)."""

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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        # `base-pr-<N>` - the literal form the Dockerfile QC expects. Per-PR, not a bare
        # "base": dependency() returns a plain string, so DockerfileEnhancer always rewrites
        # this file, and _standardize_repo_fetch turns the clone line below into
        # `git clone ${REPO_URL}` + `git checkout ${BASE_COMMIT}` plus a hardening block that
        # detaches at that one commit. The base image's CONTENT is therefore PR-specific.
        #
        # No era qualifier is needed for uniqueness (Check 2F): a PR routes to exactly ONE era,
        # and the tag embeds that PR number, so this era and the sibling backend era
        # (activist_1613_to_1613, tag "base-backend") can never mint the same tag.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # python3 is required by vitest_report.py, not by the project. The node:*-bookworm
        # images derive from buildpack-deps, which carries git and a C toolchain but does not
        # guarantee a python3 interpreter, so it is requested explicitly rather than assumed.
        # corepack is what activates the yarn version pinned in packageManager.
        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates git python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

{code}

{self.clear_env}

"""


class ActivistFrontendImageDefault(Image):
    """PR-specific image for activist frontend."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return ActivistFrontendImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "vitest_report.py", VITEST_REPORT_PY),
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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

{shell_env}

cd /home/{repo}
git reset --hard
# Assert the reset actually produced a clean tree rather than assuming it did. A stray modified
# file would flow into all three graded stages and corrupt the comparison with nothing in the
# log to explain why.
bash /home/check_git_changes.sh

git checkout {base_sha}
bash /home/check_git_changes.sh

cd /home/{repo}/frontend
# Install into this image layer so the graded stages neither pay for the download nor depend on
# the network. Neither patch touches package.json or yarn.lock, so one pass covers all three
# stages; re-check that if a later PR of this repo is configured.
#
# `timeout 1800` is not belt-and-braces on top of `|| true`. `|| true` handles a command that
# FAILS; a command that HANGS never returns, so it never reaches the `||` at all - and Docker
# has no per-step timeout. yarn blocking on a half-dead registry is exactly that shape.
timeout 1800 yarn install || true

# Hard gate. Without it a failed install produces an image that builds clean and then reports
# 0/0/0 from all three stages - Report.check() rejects it at rule 1 with nothing in the log
# pointing at the install. Fail here instead, where the cause is on screen.
yarn vitest --version

# yarn berry writes .yarn/install-state.gz and may touch yarn.lock. Assert the tree is still
# pristine rather than assuming it - an unignored artefact here would break every later
# `git apply`.
cd /home/{repo}
bash /home/check_git_changes.sh
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha, shell_env=SHELL_ENV),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}/frontend
# Delete the previous stage's report before running. vitest overwrites it on a successful run,
# but a stage that dies before writing would otherwise leave the PREVIOUS stage's results on
# disk for the emitter to read - three stages would then report identical results and every
# f2p/n2p/p2p bucket would come out empty, a silently worthless instance that still reports
# valid.
rm -f {json}

vitest_exit=0
{vitest} 2>&1 || vitest_exit=$?
python3 /home/vitest_report.py /home/{repo} {json} || true
exit "$vitest_exit"
""".format(repo=self.pr.repo, shell_env=SHELL_ENV, vitest=VITEST_CMD, json=_VITEST_JSON),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
cd /home/{repo}/frontend
rm -f {json}

vitest_exit=0
{vitest} 2>&1 || vitest_exit=$?
python3 /home/vitest_report.py /home/{repo} {json} || true
exit "$vitest_exit"
""".format(repo=self.pr.repo, shell_env=SHELL_ENV, vitest=VITEST_CMD, json=_VITEST_JSON),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{repo}
# test.patch first, then fix.patch - separate invocations so a failure names which one failed.
# They touch disjoint files here (test.patch only frontend/test*/, fix.patch only
# frontend/app/), but the order is the graded contract regardless.
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
cd /home/{repo}/frontend
rm -f {json}

vitest_exit=0
{vitest} 2>&1 || vitest_exit=$?
python3 /home/vitest_report.py /home/{repo} {json} || true
exit "$vitest_exit"
""".format(repo=self.pr.repo, shell_env=SHELL_ENV, vitest=VITEST_CMD, json=_VITEST_JSON),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Generated from files() rather than hard-coded, so a file added there can never be
        # written into the build context yet left uncopied - which would surface at build time
        # as `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/{f.name}\n" for f in self.files())

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Emitted by vitest_report.py, one line per test CASE:
#
#   VITEST_TESTCASE PASS frontend/test/composables/useUser.spec.ts::useUser > returns the user
#
# The name half contains spaces, so it is captured to end-of-line.
_TESTCASE_RE = re.compile(
    r"^VITEST_TESTCASE\s+(?P<status>PASS|FAIL|SKIP)\s+(?P<name>\S.*?)\s*$"
)

# Fallback path only. vitest's default reporter prints one line per FILE:
#
#    ✓ test/composables/useColor.spec.ts (2 tests) 150ms
#    × test/components/form/Form.spec.ts (2 tests | 1 failed) 100ms
#    ↓ test/components/filter/pageFilter.spec.ts (1 test | 1 skipped)
#
# The capture is the path alone - the `(2 tests)` count and the duration both change between
# stages, and a name carrying either would make one file read as three (Check 4B).
_FILE_SPEC = r"(\S+\.(?:spec|test)\.(?:ts|tsx|mts|js|jsx))"
_FALLBACK_PASS_RE = re.compile(r"✓\s+" + _FILE_SPEC)
_FALLBACK_FAIL_RE = re.compile(r"(?:×|FAIL)\s+" + _FILE_SPEC)
_FALLBACK_SKIP_RE = re.compile(r"↓\s+" + _FILE_SPEC)

# The fallback runs from frontend/, so its paths are frontend-relative while the emitter's are
# repo-relative. Prefixing keeps the two paths producing comparable ids, which is what lets
# report.py match either against test_patch_files.
_FALLBACK_PREFIX = "frontend/"


def _parse_testcase_lines(lines: list[str]) -> TestResult | None:
    """Per-CASE results from vitest_report.py, or None if it did not run."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    seen = False

    for line in lines:
        match = _TESTCASE_RE.match(line)
        if not match:
            continue
        seen = True
        name = match.group("name")
        status = match.group("status")
        # Failure wins over any other verdict for the same name. vitest reports a retried test
        # more than once; treating the pair as passed would hide the failing attempt.
        if status == "FAIL":
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif name not in failed_tests:
            if status == "SKIP":
                if name not in passed_tests:
                    skipped_tests.add(name)
            else:
                skipped_tests.discard(name)
                passed_tests.add(name)

    if not seen:
        return None

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


def _parse_console_files(lines: list[str]) -> TestResult:
    """File-level results from the default reporter.

    Used only when no VITEST_TESTCASE line is present - vitest died before writing the JSON
    report, the emitter itself failed, or an older image without it is being re-graded. Coarser
    than the JSON path, but it keeps a stage reportable instead of silently empty, and an empty
    stage would manufacture transitions against the other two.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        match = _FALLBACK_FAIL_RE.search(line)
        if match:
            name = _FALLBACK_PREFIX + match.group(1)
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
            continue

        match = _FALLBACK_SKIP_RE.search(line)
        if match:
            name = _FALLBACK_PREFIX + match.group(1)
            if name not in failed_tests and name not in passed_tests:
                skipped_tests.add(name)
            continue

        match = _FALLBACK_PASS_RE.search(line)
        if match:
            name = _FALLBACK_PREFIX + match.group(1)
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


def parse_vitest_log(test_log: str) -> TestResult:
    """Parse a graded stage's output into per-CASE results.

    Two sources, in priority order: the JSON report emitted by vitest_report.py, then the
    console tree. See the module docstring for why the JSON report is preferred.
    """
    # Strip ANSI FIRST - without it the ✓/×/↓ patterns fail on colourised output and the stage
    # reports 0/0/0.
    lines = _ANSI_RE.sub("", test_log).splitlines()

    from_json = _parse_testcase_lines(lines)
    if from_json is not None:
        return from_json

    return _parse_console_files(lines)


@Instance.register("activist-org", "activist_1610_to_1610")
class ACTIVIST_1610_TO_1610(Instance):
    """Instance for activist PR 1610 (frontend, vitest)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ActivistFrontendImageDefault(self.pr, self._config)

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
        return parse_vitest_log(test_log)
