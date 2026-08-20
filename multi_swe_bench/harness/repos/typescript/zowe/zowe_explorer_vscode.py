import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ============================================================================
# zowe/zowe-explorer-vscode  --  TypeScript VS Code extension, Jest + ts-jest
#
# DATASET (zowe__zowe-explorer-vscode_raw_dataset.jsonl, 1 entry)
#   PR 851 "Issue #311: Add/remove favorites with star button"
#   base master @ 998e3dc4850e95aa0c451f260d8316e6e2dfe060, merged 2020-06-17
#   number_interval and tag are both null, so Instance.create keys on the bare
#   f"{org}/{repo}" -- the single registration below is what it routes through.
#   ONE entry means no PR-number split is possible or needed; no range alias is
#   invented here.
#
# THE SIGNAL IS A SNAPSHOT, NOT A NEW TEST CASE.
# test_patch touches exactly one file:
#     __tests__/__unit__/__snapshots__/extension.unit.test.ts.snap
# It adds no describe/it blocks. The snapshot records the `menus` array that
# extension.unit.test.ts reads out of package.json, and fix_patch is what edits
# package.json. So the three stages resolve as:
#     run   (no patch)      package.json old + snapshot old -> match  -> PASS
#     test  (test.patch)    package.json old + snapshot NEW -> differ -> FAIL
#     fix   (both patches)  package.json NEW + snapshot NEW -> match  -> PASS
# That is a genuine fail->pass. Two consequences drive the scripts below:
#   * --ci is passed and -u/--updateSnapshot is NEVER passed. Letting Jest
#     rewrite the snapshot would turn the `test` stage green and erase the only
#     f2p this dataset has.
#   * parse_log records suite-level PASS/FAIL as well as per-test lines, so the
#     transition is captured whether Jest attributes the mismatch to the
#     individual `it` or aborts the suite.
#
# TOOLCHAIN IS PINNED TO 2020, NOT TO TODAY.
#   jest ^25.1.0, ts-jest ^25.3.0, typescript ^3.7.2, @types/node ^7.0.66
# node:12-buster is chosen for that: Node 12 was current LTS at the base commit
# and ships npm 6, which is the lockfileVersion 1 format this package-lock.json
# is written in. A modern node:20/22 base (the registry's most common choice)
# ships npm 9/10 and would rewrite or reject that lockfile, and jest 25 is not
# supported on Node 20.
#
# postinstall IS DELIBERATELY SUPPRESSED.
#   "postinstall": "node ./node_modules/vscode/bin/install"
# The legacy `vscode` package's postinstall downloads a VS Code binary and the
# vscode.d.ts typings over the network. It is slow, flaky, and unnecessary here:
# the unit suite mocks vscode (__mocks__/vscode.ts) and package.json sets
# ts-jest `diagnostics: false`, so the real typings are never needed to compile
# or run these tests. --ignore-scripts keeps the image build hermetic.
# ============================================================================

# Only the unit suite. package.json's own jest block already excludes
# __tests__/__integration__/ and __tests__/__theia__/ via modulePathIgnorePatterns,
# but those suites additionally need a live z/OS host and a VS Code binary, so
# the pattern below keeps them out even if that config changes.
JEST_PATTERN = r'".*__tests__.*\.unit\.test\.ts"'

# reporters=default OVERRIDES package.json's ["default","jest-junit",
# "jest-stare","jest-html-reporter"]. Those three write report files and are the
# usual cause of a Jest 25 crash when one of them fails to resolve -- a crash
# would produce zero parseable lines and be indistinguishable from "every test
# vanished". `default` is the reporter whose PASS/FAIL and check/cross output
# parse_log actually reads.
JEST_CMD = (
    f"npx jest {JEST_PATTERN} --verbose --ci --reporters=default --colors=false"
)


class ZoweExplorerVscodeImageBase(Image):
    """Base image: clone only. Shared by every PR image for this repo."""

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
        return "node:12-buster"

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

        # NO apt-get HERE, DELIBERATELY.
        # Two independent reasons:
        #   * node:12-buster is built on buildpack-deps:buster, which already
        #     ships git and ca-certificates. An install would add nothing.
        #   * Debian buster has been retired from the main mirrors --
        #     deb.debian.org/debian/dists/buster/Release now 404s (only
        #     archive.debian.org still serves it). `apt-get update` therefore
        #     exits non-zero and fails the build. Image._is_deprecated_debian()
        #     would rewrite the sources to archive.debian.org, but it matches on
        #     "debian:buster" (not "node:12-buster") and is only consulted by the
        #     default Image.dockerfile(), which this class overrides.
        # DEBIAN_FRONTEND/TZ are also omitted: DockerfileEnhancer sets both.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class ZoweExplorerVscodeImageDefault(Image):
    """Per-PR image: checks out base.sha and installs the dependency tree."""

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
        return ZoweExplorerVscodeImageBase(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
# Checked once, AFTER checkout. Running it before as well bought nothing and
# turned any stray porcelain output (line-ending or filemode noise) into a hard
# image-build failure under `set -e`.
bash /home/check_git_changes.sh

# REPO SETUP STEP, performed here because no clone can carry it.
# __tests__/__unit__/utils/profileLink.unit.test.ts does a top-level
#     import * as testConst from "../../../resources/testProfileData";
# but the repo ships only resources/testProfileData.example.ts -- the real file
# is listed in .gitignore (line 6 at this base.sha) because it holds live z/OS
# host/user/password values that must never be committed. Every developer copies
# the template by hand; nothing in a fresh clone does it.
#
# Left undone, Jest cannot resolve the module, the import throws before any
# it() runs, and the whole file reports "Test suite failed to run" -- a
# permanent suite-level failure identical in all three stages.
#
# Safe to create: the path is gitignored AT base.sha, so `git status
# --porcelain` stays empty and check_git_changes.sh still passes; and the
# `git reset --hard` at the top of each run script does not delete untracked
# files, so this one copy survives into all three stages.
if [ -f resources/testProfileData.example.ts ] && [ ! -f resources/testProfileData.ts ]; then
    cp resources/testProfileData.example.ts resources/testProfileData.ts
fi

# The copy above must not have dirtied the tree. If a future base commit drops
# that .gitignore entry the file becomes untracked-and-visible, `git apply`
# starts failing, and this catches it at build time instead of leaving three
# stages to fail confusingly.
bash /home/check_git_changes.sh

# --ignore-scripts suppresses "postinstall": "node ./node_modules/vscode/bin/install",
# which downloads a VS Code binary over the network. The unit suite mocks vscode
# and ts-jest runs with diagnostics disabled, so nothing here needs it.
# npm ci first: it honours the committed lockfileVersion 1 package-lock.json
# exactly. npm install is the fallback for a tree npm ci refuses.
npm ci --ignore-scripts --no-audit --no-fund \\
  || npm install --ignore-scripts --no-audit --no-fund \\
  || true

# PRECONDITION GATE, not an install step. Both commands above end in `|| true`,
# so without this an image whose install silently failed would ship and every
# stage would report 0/0/0 -- indistinguishable from "the suite found no tests".
# Failing loudly here is far better than a silently empty run.
test -d node_modules/jest || {{ echo "FATAL: jest not installed" >&2; exit 1; }}
test -d node_modules/ts-jest || {{ echo "FATAL: ts-jest not installed" >&2; exit 1; }}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# Stage 1 (baseline): no patches. Expected GREEN -- package.json and the
# snapshot are both at base.sha and therefore agree.
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard

{jest}
""".format(pr=self.pr, jest=JEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
# Stage 2: test.patch only. Expected RED -- the snapshot now describes menus
# that package.json does not yet declare.
#
# `set -e` matters most here: if BOTH apply attempts fail, the script must die.
# Without it the run would fall through to Jest against unpatched code, the
# stage would go GREEN, and the only fail->pass signal in this dataset would
# vanish with nothing in the log to say patching was the cause.
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard

git apply --whitespace=nowarn /home/test.patch \\
  || git apply --3way --whitespace=nowarn /home/test.patch

{jest}
""".format(pr=self.pr, jest=JEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
# Stage 3: test.patch + fix.patch. Expected GREEN again -- fix.patch brings
# package.json in line with the new snapshot.
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard

git apply --whitespace=nowarn /home/test.patch /home/fix.patch \\
  || git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch

{jest}
""".format(pr=self.pr, jest=JEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Built by looping files() so a file added there can never be missing a
        # COPY, or vice versa.
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


@Instance.register("zowe", "zowe-explorer-vscode")
class ZoweExplorerVscode(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ZoweExplorerVscodeImageDefault(self.pr, self._config)

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
        return parse_jest_log(test_log)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Suite headers: "PASS __tests__/__unit__/extension.unit.test.ts (12.34 s)"
_SUITE_PASS_RE = re.compile(r"^PASS\s+(\S+)")
_SUITE_FAIL_RE = re.compile(r"^FAIL\s+(\S+)")

# Per-test lines from Jest's `default` reporter under --verbose.
_TEST_PASS_RE = re.compile(r"^[✓✔]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
_TEST_FAIL_RE = re.compile(r"^[✕✗✘×]\s+(.*?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
# `○` only -- NOT `●`. Jest uses ○ for a skipped test but ● as the bullet on
# each failure-detail block in its summary ("● Suite › test name"). Matching ●
# here would file every failure under `skipped` as well, inventing a name that
# exists in the failing stage and not in the passing ones -- a stage-to-stage
# name mismatch, which is the usual source of a Report.check() anomaly.
_TEST_SKIP_RE = re.compile(r"^○\s+(?:skipped\s+)?(.*?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")


_SLUG_RE = re.compile(r"\s+")


def _slugify(name: str) -> str:
    """Collapse a Jest test description into an identifier-shaped token.

    Jest takes its test name as a free-text string -- ``it("should not change
    the existing context menus", ...)`` -- so the raw name is an English
    sentence, where pytest's is a function identifier. This maps the former onto
    the latter's shape so both report in one style.

    Whitespace-only substitution, deliberately: punctuation, case and word order
    are all preserved, so the result stays reversible by eye and still greps
    back to the source `it(...)`. Anything more aggressive (lowercasing,
    stripping punctuation) would start merging distinct tests into one name.

    Applied in ONE place, inside the per-test branch of parse_jest_log, so all
    three stages are guaranteed to derive identical names -- a stage-to-stage
    mismatch here is what produces a phantom f2p.
    """
    return _SLUG_RE.sub("_", name.strip())


def parse_jest_log(test_log: str) -> TestResult:
    """Parse Jest `default`-reporter output into a TestResult.

    NAME FORMAT: ``<repo-relative file>::<test name>``, matching the convention
    the rest of the benchmark reports in (``fiasco/tests/test_ion.py::test_x``).
    Jest's ``PASS``/``FAIL`` header supplies the path and the ``✓``/``✕`` lines
    supply the test, so both halves come straight from the log.

    Qualifying by file is not cosmetic: identically-named ``it`` blocks in
    different suites are common in this repo, and an unqualified name would
    merge them into one identity whose status flips arbitrarily between stages.

    The ``PASS``/``FAIL <file>`` headers are read for that prefix ONLY -- the
    bare path is never itself recorded as a test, since a file is not a test
    identity under this convention. The cost is that a suite which dies before
    any ``it`` runs (a ts-jest transform error, a missing import -- what
    ``profileLink.unit.test.ts`` did before prepare.sh started creating
    resources/testProfileData.ts) emits no per-test lines and so registers as
    its tests silently vanishing rather than as a failure. That cannot corrupt
    this instance's f2p, which is a per-test line, but it is the reason the
    prepare.sh fix matters rather than being optional tidying.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    current_suite: Optional[str] = None

    for raw_line in test_log.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        # Header lines set the prefix for the tests that follow. They are NOT
        # recorded as tests themselves -- see the docstring.
        m = _SUITE_PASS_RE.match(stripped) or _SUITE_FAIL_RE.match(stripped)
        if m:
            current_suite = m.group(1)
            continue

        for regex, bucket in (
            (_TEST_PASS_RE, passed_tests),
            (_TEST_FAIL_RE, failed_tests),
            (_TEST_SKIP_RE, skipped_tests),
        ):
            m = regex.match(stripped)
            if m:
                name = _slugify(m.group(1))
                if not name:
                    break
                # `::` separator, per the benchmark's file::test convention.
                bucket.add(f"{current_suite}::{name}" if current_suite else name)
                break

    # A name seen failing anywhere is failing: Jest prints a per-test line and
    # then repeats the name in its failure summary, and a suite can be reported
    # PASS on a retry after an earlier FAIL.
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
