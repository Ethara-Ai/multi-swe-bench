"""commaai/flash harness config.

flash is comma.ai's browser flashing tool: a Next.js 13 (app router) app whose
unit suite runs under **vitest** and is driven by **pnpm**.  Upstream CI
(.github/workflows/main.yaml + .github/actions/setup-pnpm) pins Node 18 / pnpm 8
and runs `pnpm install` then `pnpm test`, so this harness reproduces that exact
toolchain.

Structure mirrors golang/cli (CliImageBase + CliImageDefault + Instance):

* ``FlashImageBase``  -- ``dependency()`` returns a *string* base image, so the
  pipeline-level ``DockerfileEnhancer`` owns the generated Dockerfile and emits
  the standard hardened form: BuildKit syntax directive, TARGETARCH/REPO_URL/
  BASE_COMMIT + proxy ARGs, the shared ENV block, OCI labels, the CA-cert
  symlink fan-out, ``git clone "${REPO_URL}"`` -> ``git checkout ${BASE_COMMIT}``
  -> the history-scrubbing hardening block -> ``CMD ["/bin/bash"]``.
* ``FlashImageDefault`` -- ``FROM`` the base image, ``COPY`` the patches and the
  eval scripts, ``RUN bash /home/prepare.sh``.  ``dependency()`` returns an
  ``Image`` here, so the enhancer passes this Dockerfile through untouched.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Upstream CI pins pnpm 8 (.github/actions/setup-pnpm defaults to
# pnpm-version: "8").  package.json carries no `packageManager` field, so
# corepack has nothing to honor -- pin the major explicitly instead.
_PNPM_VERSION = "8"

# --- external fixture pin ---------------------------------------------------
# src/config.js points `config.manifests.master` at a LIVE, MOVING target:
#   raw.githubusercontent.com/commaai/openpilot/master/system/hardware/tici/agnos.json
# src/utils/manifest.test.js fetches and JSON.parses that URL at COLLECTION time
# (top-level `await getManifest(...)` inside describe()), so whatever it returns
# decides whether the spec file loads at all.
#
# openpilot has since restructured (tici/ -> comma/) and left a symlink at the
# old path. raw.githubusercontent.com serves a symlink's *target* as plain text,
# so that URL now returns the literal string
#   ../../../openpilot/system/hardware/comma/agnos.json
# JSON.parse chokes on the leading "." -- "SyntaxError: Unexpected token . in
# JSON at position 0" at manifest.js:74 -- the whole spec fails to collect, and
# all three phases report an identical failed suite. Nothing can transition, so
# the instance is unresolvable through no fault of the PR. (The symlink's target
# path is itself a 404 on raw.githubusercontent.com, so following it is not a
# fix either.)
#
# Pinning the fixture to the openpilot revision contemporaneous with PR #29
# restores the manifest the PR was actually written against. 031e88f2 ("add size
# to system alt image manifest entry", 2024-01-25, three days before this PR
# merged) is the commit that introduced `alt.size` -- the very field this PR's
# fix reads -- so it is precisely the manifest schema the fix targets. Verified
# live: all 7 partition entries present, and both the .xz artifacts the fixed
# code resolves and the legacy .gz artifacts the base code synthesizes are still
# served by the CDN.
#
# This substitutes FIXTURE DATA only -- no product code, no test code -- and it
# is applied once in prepare.sh (image build time), so all three eval phases
# observe byte-identical state and no phase-asymmetry is introduced.
_OPENPILOT_PIN = "031e88f2c900aca67538ad040655001a7547156e"

_PIN_MANIFEST_FIXTURE = """# Pin the AGNOS manifest fixture -- see _OPENPILOT_PIN in the harness config.
sed -i 's#openpilot/master/system/hardware/tici/agnos.json#openpilot/{pin}/system/hardware/tici/agnos.json#' src/config.js
grep -q '{pin}' src/config.js || {{
  echo "ERROR: AGNOS manifest fixture pin did not apply to src/config.js" >&2
  exit 1
}}""".format(pin=_OPENPILOT_PIN)

# Env shared by prepare.sh and all three eval scripts.  It MUST be identical in
# every phase: the three runs are diffed against each other, so anything that
# changes how tests are collected or named in only one phase manufactures
# phantom NONE->PASS / FAIL->PASS transitions.
#
# NODE_OPTIONS: the manifest suite downloads whole AGNOS partition archives and
# buffers them through a Blob before unpacking, which overruns the default V8
# old-space on a container with modest RAM.  8 GiB keeps the download test from
# dying as an OOM instead of a real assertion.
_RUN_ENV = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export NEXT_TELEMETRY_DISABLED=1
export NODE_OPTIONS="--max-old-space-size=8192"
"""

# The `image and checksum` cases are excluded from the eval run. They are not
# unit tests: each one downloads a whole AGNOS partition archive from a
# third-party CDN and decompresses it in pure JS. For the `system` partition
# that is 840 MB (.xz, what the fixed code fetches) or 1.35 GB (.gz, what the
# base code synthesizes), expanding to ~2.4 GB that is then SHA-256'd by jsSHA.
# Measured in this image: a single phase's checksum tests ran >10 minutes
# without finishing.
#
# Excluding them is a correctness requirement here, not just a speed
# optimisation. Two ways they would sink the instance (see harness/report.py
# check()):
#
#   * Criterion 1 -- a phase that overruns --agent_timeout (default 1800 s) is
#     killed, fix_patch_result.all_count becomes 0, and the instance is thrown
#     out entirely. Three phases each downloading ~1 GB puts that well within
#     reach on any ordinary connection.
#   * Criterion 2 ("no new failures") -- the legacy .gz artifacts are STILL
#     live, so these tests PASS in the test phase. Any timeout, OOM or CDN blip
#     in the fix phase then reads as a pass->fail regression and invalidates a
#     perfectly correct fix.
#
# Nothing is lost from the graded signal: PR #29's contract is "archives come
# from the manifest URL and are .xz, not synthesized .gz", which the `xz
# archive` and `alt image` assertions cover exactly, and those transition
# FAIL->PASS cleanly. The filter is a negative lookahead so everything else --
# including src/app/App.test.jsx -- still runs, and it is applied identically in
# all three phases, so no phase-asymmetry is introduced. Excluded cases are
# reported by vitest as skipped, which its verbose reporter prints no line for,
# so they are simply absent from every phase's parse rather than being NONE in
# one and PASS in another.
_TEST_NAME_FILTER = "^(?!.*image and checksum).*$"

# `pnpm test` maps to a bare `vitest`, which is watch-mode outside CI; `run`
# makes the single-shot behaviour explicit and independent of CI detection.
#
# --reporter=verbose is what parse_log() consumes: vitest's verbose reporter on
# a non-TTY prints ONE line per test case, "<symbol> <file> > <suite> > <test>",
# whereas the default reporter only prints a per-file roll-up and no test names
# at all.
_TEST_CMD = f"pnpm exec vitest run --reporter=verbose -t '{_TEST_NAME_FILTER}'"

# Dependency install.  Two properties are shared by both variants below:
#
# 1. Output is redirected to a file.  pnpm writes its progress/summary table to
#    stdout at column 0; leaving it inline puts install chatter in the middle of
#    the test log, and (because run.sh installs nothing new while fix-run.sh
#    does) it would appear in some phases only.  Redirecting keeps stdout to
#    test output alone, so all three phases produce identical test ids.  A
#    failure still surfaces via the tail'd log rather than being swallowed.
#
# 2. --no-frozen-lockfile.  The reference fix for PR #29 edits BOTH package.json
#    and pnpm-lock.yaml, but a candidate patch may well touch only package.json.
#    A frozen install would reject that tree outright and score a plausible fix
#    as a build failure; the unfrozen install resolves whatever the patch left
#    behind.
#
# The two variants differ ONLY in whether a failed install aborts.

# prepare.sh (image build time) -- NON-FATAL.  This install is a cache warm-up,
# and partial failures here are routinely benign: the usual cause is an optional
# native addon (esbuild / @next/swc / rollup) failing to build or fetch on a
# non-amd64 host, which leaves a perfectly usable tree behind.  Letting that
# abort the image build would throw away the whole instance over a dependency
# the test suite never touches, so the block ends in `true` (the `|| true`
# contract) and the build continues.  Nothing is hidden: the failure is
# announced and the last 60 log lines are printed.  If the tree really is
# broken, the eval phases still fail loudly -- see _INSTALL_RUN.
_INSTALL_PREPARE = """pnpm install --no-frozen-lockfile > /tmp/pnpm-install.log 2>&1 || {
  echo "WARNING: pnpm install reported a failure during prepare; continuing." >&2
  echo "WARNING: a genuinely unusable tree will still fail loudly at eval time." >&2
  tail -60 /tmp/pnpm-install.log >&2
  true
}"""

# run.sh / test-run.sh / fix-run.sh (eval time) -- FATAL.  By this point the
# install is not a warm-up: fix.patch adds a dependency (xz-decompress), so a
# graded phase that cannot install cannot produce a meaningful verdict.  Aborting
# with a non-zero exit is the honest outcome -- it is reported as a phase error.
# Swallowing it would let vitest run against a stale/absent node_modules and emit
# a plausible-looking but wrong result.
#
# Note this is NOT `|| true` on a test command, which would be fatal to the
# dataset (an unstartable runner would yield an empty log, a 0/0/0 TestResult and
# an `all_count == 0` rejection with the cause buried).  The test command itself
# is never guarded; it remains the real gate.
_INSTALL_RUN = """pnpm install --no-frozen-lockfile > /tmp/pnpm-install.log 2>&1 || {
  echo "ERROR: pnpm install failed" >&2
  tail -60 /tmp/pnpm-install.log >&2
  exit 1
}"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


class FlashImageBase(Image):
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
        # package.json declares `engines: { node: ">=18.0.0" }` and upstream CI
        # runs Node 18; the xz-decompress package the fix pulls in needs >=16.
        # The official node:18 image is Debian-based and already ships git,
        # which both the clone below and the `android-fastboot` git dependency
        # need.
        return "node:18"

    # Instance-scoped, NOT a shared per-repo "base" tag. The base image bakes
    # `git checkout ${BASE_COMMIT}` and the D14 integrity asserts against one
    # specific commit, so a tag shared across PRs cannot honour that pin: the
    # first PR to build would win the tag, and every later PR with a different
    # base commit would silently inherit an image scrubbed and asserted against
    # somebody else's commit. Keying the tag on pr.number makes the base image
    # provably the one built for this instance.
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

        # Everything that must survive into the final image goes BEFORE `code`:
        # DockerfileEnhancer rewrites that clone/copy line into the standard
        # clone -> checkout -> harden -> CMD tail, so any directive placed after
        # it would land after CMD and never run.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN npm install -g pnpm@{_PNPM_VERSION}

{code}

{self.clear_env}

"""


class FlashImageDefault(Image):
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
        return FlashImageBase(self.pr, self._config)

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
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# Defensive no-op. The base image is instance-scoped (tag base-pr-{pr.number})
# and is hardened down to exactly this PR's base commit, so this guard always
# succeeds and the fetch never runs. It stays only as a belt-and-braces check
# against a stale/mistagged base image, which would otherwise fail the checkout
# below with an opaque "fatal: reference is not a tree".
if ! git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null; then
    git fetch --no-tags --depth 1 https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
fi
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{pin_fixture}

{run_env}
{install}

""".format(
                    pr=self.pr,
                    pin_fixture=_PIN_MANIFEST_FIXTURE,
                    run_env=_RUN_ENV,
                    install=_INSTALL_PREPARE,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{run_env}
{install}
{test_cmd}

""".format(
                    pr=self.pr,
                    run_env=_RUN_ENV,
                    install=_INSTALL_RUN,
                    test_cmd=_TEST_CMD,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply test.patch failed, retrying with --3way..." >&2
    if ! git apply --3way --whitespace=nowarn /home/test.patch; then
        echo "Error: git apply test.patch failed" >&2
        exit 1
    fi
fi
{run_env}
{install}
{test_cmd}

""".format(
                    pr=self.pr,
                    run_env=_RUN_ENV,
                    install=_INSTALL_RUN,
                    test_cmd=_TEST_CMD,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Warning: git apply test.patch failed, retrying with --3way..." >&2
    if ! git apply --3way --whitespace=nowarn /home/test.patch; then
        echo "Error: git apply test.patch failed" >&2
        exit 1
    fi
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Warning: git apply fix.patch failed, retrying with --3way..." >&2
    if ! git apply --3way --whitespace=nowarn /home/fix.patch; then
        echo "Error: git apply fix.patch failed" >&2
        exit 1
    fi
fi
{run_env}
{install}
{test_cmd}

""".format(
                    pr=self.pr,
                    run_env=_RUN_ENV,
                    install=_INSTALL_RUN,
                    test_cmd=_TEST_CMD,
                ),
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


@Instance.register("commaai", "flash")
class Flash(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FlashImageDefault(self.pr, self._config)

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

        # vitest's verbose reporter on a non-TTY (VerboseReporter.onTaskUpdate)
        # emits one line per test case, built from the task's full name -- the
        # spec file, then every enclosing describe(), then the test title, all
        # joined with " > ". Verified against vitest 0.34.2:
        #
        #     ✓ src/app/App.test.jsx > renders without crashing
        #     ✓ src/utils/manifest.test.js > master manifest > boot image > xz archive
        #     × src/utils/manifest.test.js > master manifest > system image > image and checksum
        #
        # That full name is used verbatim as the test id: bare titles collide
        # both across describe blocks (this suite generates one block per image,
        # each with identically-named cases) and across spec files.
        #
        # Anchoring on the leading spec path is what keeps this from matching
        # the reporter's own decorations -- the "  → expected 1 to be 2" error
        # echo under a failure, the "❯ src/utils/manifest.test.js:5:50" frame,
        # the "Unhandled Errors" banner and the "Test Files 1 failed | 1 passed"
        # summary all lack the "<spec> > <name>" shape.
        ansi_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]|\x00")
        spec_re = r"\S+\.(?:test|spec)\.[cm]?[jt]sx?"
        # 0.34 appends no duration to these lines (only an optional
        # "N MB heap used" under --logHeapUsage, which is off). Later vitest
        # majors do append "  12ms", so accept and discard a trailing duration
        # rather than silently mis-parsing the id if the pin ever moves.
        duration_re = r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s))?"
        case_re = re.compile(
            rf"^(?P<marker>[✓✔√×✕✖✗↓○])\s+"
            rf"(?P<name>{spec_re}\s+>\s+.*?)"
            rf"{duration_re}\s*$"
        )
        # The "Failed Tests" section repeats each failure as
        # " FAIL  src/utils/manifest.test.js > master manifest > ... ".
        # Same id as the marker line above, so the set dedupes it.
        fail_case_re = re.compile(rf"^FAIL\s+(?P<name>{spec_re}\s+>\s+.+?)\s*$")
        # A spec that throws while being collected reports no cases at all:
        # " FAIL  src/utils/manifest.test.js [ src/utils/manifest.test.js ]".
        # Record the file itself so a suite that never ran is not silently
        # indistinguishable from a suite with zero tests.
        fail_file_re = re.compile(
            rf"^FAIL\s+(?P<name>{spec_re})\s+\[\s*{spec_re}\s*\]\s*$"
        )

        pass_markers = {"✓", "✔", "√"}
        fail_markers = {"×", "✕", "✖", "✗"}
        # 0.34's verbose reporter skips tasks that have no `result`, so a
        # skipped/todo case prints no line at all and is only visible in the
        # "1 skipped" summary tally -- there is no name to recover. flash's
        # suite has no skipped cases, so nothing is lost today; these markers
        # are kept so a future vitest that does print them is bucketed as
        # skipped rather than falling through to the FAIL branches.
        skip_markers = {"↓", "○"}

        def normalize(name: str) -> str:
            # Collapse the whitespace around the " > " separators so an id is
            # byte-identical across phases regardless of reporter padding.
            return " > ".join(part.strip() for part in name.split(">"))

        for raw_line in test_log.splitlines():
            line = ansi_re.sub("", raw_line).strip()
            if not line:
                continue

            match = case_re.match(line)
            if match:
                name = normalize(match.group("name"))
                marker = match.group("marker")
                if marker in pass_markers:
                    passed_tests.add(name)
                elif marker in fail_markers:
                    failed_tests.add(name)
                elif marker in skip_markers:
                    skipped_tests.add(name)
                continue

            match = fail_file_re.match(line)
            if match:
                failed_tests.add(match.group("name").strip())
                continue

            match = fail_case_re.match(line)
            if match:
                failed_tests.add(normalize(match.group("name")))

        # A name can land in more than one bucket (a case reported by both the
        # marker line and the Failed Tests section, or two specs sharing a
        # title). Resolve with fail > skip > pass so a flaky test is never
        # credited as passing and TestResult's disjointness invariants hold.
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
