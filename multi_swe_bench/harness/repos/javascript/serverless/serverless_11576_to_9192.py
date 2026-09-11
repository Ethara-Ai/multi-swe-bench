"""Repo config for serverless/serverless PRs 9192-11576 (Ethara round, 2026-09).

Registration
------------
This dataset's raw rows carry neither ``number_interval`` nor ``tag``, so
``Instance.create()`` can only ever compute the key ``serverless/serverless``.
That key is also claimed by ``.serverless.Serverless``.  ``Instance.register``
is a plain dict assignment, so the LAST import wins; this module is imported
last from ``repos/javascript/serverless/__init__.py`` and therefore owns the
key.  Nothing in the pre-existing configs is modified.  This is deliberate and
was approved for this round - it is recorded here so the override is visible to
a reviewer rather than being an invisible side effect of import order.

One config -> one base Dockerfile -> N PR Dockerfiles.  The base performs no
checkout and installs no project dependencies, so it is genuinely PR
independent: the rendered base Dockerfile hashes to a single value across all
20 PRs, and whichever PR builds it first produces an identical image.

Findings that shaped this file (each measured in a container, not inferred)
--------------------------------------------------------------------------
1. ``npm install`` alone yields ZERO tests on Node 14.  The repo ships no
   lockfile, so npm resolves ``simple-git`` (transitive, via
   @serverless/dashboard-plugin) to a current release that uses ES2022 class
   static blocks and cannot be parsed by Node 14.  ``--before=<commit date>``
   resolves every dependency as of the PR's own base commit and fixes this,
   while also reproducing the era faithfully.
2. Mocha flags placed BEFORE the spec glob yield ZERO tests on mid-range
   commits: serverless' own mocha require-hooks parse ``process.argv``, see an
   option ahead of any command, and at those commits raise a hard
   ``ServerlessError``.  The glob is therefore passed first, and
   ``SLS_DEPRECATION_DISABLE`` is exported as a second, independent guard.
3. ``--parallel`` was evaluated and REJECTED.  It is ~4x faster and survives a
   spec-file load error, but it is non-deterministic on the later commits: two
   consecutive runs at PR 11558's base produced plans of 1..2786 and 1..2056,
   i.e. 730 tests silently missing from one of them, against a serial plan of
   1..2787.  A run that loses a different set of tests each time produces
   phantom PASS->FAIL transitions and invalidates the report under
   Report.check.  Serial mocha measured deterministic and complete at every
   commit smoke-tested, so it is used.  The one load error in this dataset
   (PR 9662) is handled instead by the placeholder step described on
   _fix_created_modules_needed_by_tests, which lets those tests run and fail at
   the test stage and so be credited as f2p rather than n2p.
4. Baseline noise: ``FORCE_COLOR=1`` fixes assertions that expect chalk colour
   codes (CI gets these from wrapping the command in ``script -e -c``); ruby and
   a ``python`` alias fix ``invokeLocalRuby`` / ``invokeLocalPython``.  Together
   these took baseline failures from 11 to 4 at the oldest commit.  The
   remaining 4 are stable across all three stages and therefore neutral.
5. TAP markers can be glued to preceding test stdout (``testtestok 1460 ...``)
   and mocha emits marker numbers out of order under async.  ``parse_log``
   therefore matches permissively and never assumes sequence.
6. ``.serverless.serverless`` installs two global shims when imported: it wraps
   ``PullRequest.from_json`` (to derive ``number_interval`` from
   ``prs_in_bundle``) and ``Instance.create`` (a ValueError fallback).  Both are
   benign here and were verified as such: our rows carry neither
   ``prs_in_bundle`` nor ``number_interval``, so the first only logs
   "no number_interval and no prs_in_bundle" per row and leaves the field empty,
   and the second never fires because our key resolves on the first attempt.
   Those log lines are expected noise in the build output, not errors.
"""

from __future__ import annotations

import posixpath
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "node:14-bullseye"

# git + ca-certificates are mandated by the Dockerfile spec.  python3 /
# python-is-python3 / ruby back serverless' invoke-local tests, which the CI
# runner gets for free from ubuntu-latest and which are absent from its apt line.
APT_PACKAGES = "git ca-certificates python3 python-is-python3 ruby"

# Distinct from the sibling configs' base tags ("base" in .serverless,
# "base-sls1x", "base-sls2x", "base-sls-7780-8561").  Image dedup keys on
# image_full_name(), so a shared tag means only ONE base is ever built and a
# foreign or stale :base would be silently reused instead of ours - the tag is
# scoped to this PR range to make that impossible.  QC check 2F.
# Shape follows the Dockerfile-QC P1/A1 shared-base form base-<lo>_to_<hi>.
BASE_TAG = "base-9192_to_11576"

# THE single source of truth for the graded command.  Interpolated verbatim into
# run.sh / test-run.sh / fix-run.sh, which makes QC check P7 true by
# construction.  The glob MUST precede the flags - see finding 2 above.
#
# The --ignore is deliberate and scoped.  test/unit/scripts/serverless.test.js
# spawns the real CLI as a child process; PR 10317's fix adds an S3
# bucket-existence probe to that path, which has no network in the grading
# container and so runs to mocha's 30s timeout.  Measured deterministic: three
# consecutive fix-stage runs failed it identically, so it is an environment
# artifact rather than a regression, and it produced a PASS->FAIL that
# Report.check rule 2 rejects.  Verified safe to exclude globally: the file is
# not a graded test file for ANY of the 20 PRs in this dataset, so no PR loses
# signal.  Excluding it in all three stages keeps the command byte-identical.
TEST_COMMAND = (
    './node_modules/.bin/mocha "test/unit/**/*.test.js" --reporter tap'
    ' --ignore "test/unit/scripts/serverless.test.js"'
)

# Exported by all three run scripts.  Kept out of TEST_COMMAND so the graded
# command string stays byte-identical.
RUN_ENV = (
    "export CI=true\n"
    "export FORCE_COLOR=1\n"
    'export SLS_DEPRECATION_DISABLE="*"\n'
)

# Written into the tree by test-run.sh for modules that fix_patch creates and
# test_patch imports.  Kept as a constant so the shell line stays readable and
# so a reviewer can see exactly what is placed on disk.
PLACEHOLDER_JS = (
    '"use strict";\n'
    "// Placeholder written by test-run.sh; the real module arrives with fix.patch.\n"
    "module.exports = () => { throw new Error("
    '"placeholder: module is introduced by the fix patch"); };\n'
)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# Permissive on purpose: the marker can be glued to preceding stdout
# ("testtestok 1460 ...") and mocha emits marker numbers out of order for async
# tests even serially, so neither line-start anchoring nor sequence is assumed.
_TAP_RE = re.compile(r"(not ok|ok) (\d+) ")
_SKIP_RE = re.compile(r"\s*#\s*(?:SKIP|skip|pending)\b.*$")

CHECK_GIT_CHANGES = """#!/bin/bash
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


class ServerlessImageBase(Image):
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
        return BASE_IMAGE

    # Constant tag: the base does no checkout and installs no project
    # dependencies, so it is identical for every PR in this dataset.
    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # The syntax directive makes DockerfileEnhancer.enhance() return this
        # file unchanged, so no hardening is injected into the base and no
        # ${BASE_COMMIT} checkout is added - both belong to the PR layer.  The
        # infrastructure block is sourced from the enhancer rather than pasted,
        # so proxy ARGs / CA symlinks / OCI labels cannot drift on a harness bump.
        infra = DockerfileEnhancer._infrastructure_block(self, BASE_IMAGE, True)
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {BASE_IMAGE}

{infra}
WORKDIR /home/

RUN apt-get -o Acquire::Check-Valid-Until=false update && \\
    apt-get install -y --no-install-recommends {APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class ServerlessImageDefault(Image):
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
        return ServerlessImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}\n"),
            File(".", "test.patch", f"{self.pr.test_patch}\n"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._stage_sh(patches=[])),
            File(".", "test-run.sh", self._stage_sh(patches=["test.patch"])),
            File(".", "fix-run.sh", self._stage_sh(patches=["test.patch", "fix.patch"])),
        ]

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""#!/bin/bash
set -e

export CI=true
export npm_config_audit=false
export npm_config_fund=false

cd /home/{repo}

git reset --hard
bash /home/check_git_changes.sh

git checkout {sha}
bash /home/check_git_changes.sh

# The repo ships no lockfile, so npm resolves semver ranges live and would pull
# present-day releases that no longer support Node 14.  Resolving as of this
# commit's own date reproduces the dependency set the PR actually had.
COMMIT_DATE="$(git show -s --format=%cI HEAD)"
echo "prepare: resolving dependencies as of ${{COMMIT_DATE}}"

npm install --before="${{COMMIT_DATE}}" --no-audit --no-fund --loglevel=error || true
test -x ./node_modules/.bin/mocha || npm install --no-audit --no-fund --loglevel=error || true

# ---- HARD VERIFICATION - no error suppression below this line ----
# Every install line above ends in "|| true", so a CDN 403 or a resolution
# failure is silent.  This block is what stops a hollow image shipping green.
test -x ./node_modules/.bin/mocha
./node_modules/.bin/mocha --version
test -d test/unit
test "$(ls -1 node_modules | wc -l)" -gt 400
node -e "require('./package.json')"
node -e "require.resolve('mocha'); require.resolve('chai'); require.resolve('sinon'); require.resolve('@serverless/test/setup/log'); console.log('DEPS_OK')"

bash /home/check_git_changes.sh
"""

    def _fix_created_modules_needed_by_tests(self) -> list[str]:
        """Paths that fix_patch CREATES and test_patch IMPORTS.

        Mocha requires every spec file before running any test, so at the
        test-only stage such an import cannot resolve and the entire run aborts
        at load time: no plan line, no test ever named, and the PR's own new
        tests cannot be credited as f2p even though they demonstrably cannot
        pass without the fix.  Mocha has no equivalent of pytest's
        --continue-on-collection-errors (the full 8.4.0 option set was checked),
        and --parallel only yields one anonymous "Uncaught error outside test
        suite" because the file never registered its it() blocks.

        Writing an empty placeholder at that path lets the spec file load; the
        tests then run and fail, which is the truthful pre-fix outcome.  The
        results still come from a real test run - no name is ever synthesised
        from patch text.

        Detection is derived from the two patches, never hardcoded: a path
        qualifies only if fix_patch marks it "new file mode", an added require()
        in test_patch resolves to it, and test_patch does not itself create it.
        Across this dataset exactly one PR qualifies - 9662, whose test_patch is
        a unit test of get-aws.js, the module fix_patch introduces.
        """
        fix_patch = self.pr.fix_patch or ""
        test_patch = self.pr.test_patch or ""

        created, current = set(), None
        for line in fix_patch.splitlines():
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                current = m.group(2)
                continue
            if line.startswith("new file mode") and current:
                created.add(current)

        test_files = {
            b for _, b in re.findall(r"^diff --git a/(\S+) b/(\S+)", test_patch, re.M)
        }

        needed, current = [], None
        for line in test_patch.splitlines():
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                current = m.group(2)
                continue
            if not line.startswith("+") or line.startswith("+++") or not current:
                continue
            for req in re.findall(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", line[1:]):
                if not req.startswith("."):
                    continue
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(current), req)
                )
                for cand in (target, target + ".js", target + "/index.js"):
                    if cand in created and cand not in test_files and cand not in needed:
                        needed.append(cand)
        return sorted(needed)

    def _stage_sh(self, patches: list[str]) -> str:
        repo = self.pr.repo
        # One atomic git apply with both patches, in order: git validates every
        # patch before touching the tree.  Safe here because 3.4 verified that no
        # PR has a file appearing in both fix_patch and test_patch.
        #
        # --3way is required by PR 11558, whose fix_patch was generated against a
        # tree that is not its recorded base.sha: the patch declares blob
        # 333adf0aa19 for scripts/serverless.js while base.sha carries 092f9acd0b0,
        # so a plain apply fails outright and the fix stage captures nothing.  The
        # three-way merge reconstructs the pre-image from the blob ids the patch
        # itself records and applies it cleanly.  Measured harmless everywhere
        # else: for PRs whose patches already apply, git applies them directly and
        # only falls back on failure - checked on 9192/9895/11487/11558, all
        # rc=0 with zero conflict markers and zero unmerged index entries.
        # Test stage only.  Every stage runs in a FRESH container from the image
        # (docker_util.run -> containers.run), so nothing written here can reach
        # the fix stage or collide with fix_patch creating the real file.
        stubs = ""
        if patches == ["test.patch"]:
            for mod in self._fix_created_modules_needed_by_tests():
                stubs += 'mkdir -p "$(dirname %s)"\n' % mod
                stubs += "cat > %s <<'PLACEHOLDER_EOF'\n%sPLACEHOLDER_EOF\n" % (
                    mod,
                    PLACEHOLDER_JS,
                )

        applies = (
            "git apply --3way --whitespace=nowarn "
            + " ".join(f"/home/{p}" for p in patches)
            + "\n"
            if patches
            else ""
        )
        return f"""#!/bin/bash
set -eo pipefail

{RUN_ENV}
cd /home/{repo}
{applies}{stubs}
{TEST_COMMAND}
"""

    def dockerfile(self) -> str:
        # Order matters: prepare.sh runs FIRST and establishes the commit plus the
        # dependency tree, then the prune block verifies HEAD rather than
        # re-establishing it (Dockerfile-QC P11).  The block opens with a
        # commit-scoped "git checkout --detach <sha>", which is a same-tree no-op
        # that preserves anything prepare.sh modified - P12 explicitly allows it
        # and prohibits reset/clean/path-scoped checkout here.
        #
        # PR layers receive no build args (build_dataset.py passes them only when
        # dependency() is a str), so ${{BASE_COMMIT}} would expand to empty and
        # every assert would pass vacuously.  The literal SHA is substituted into
        # the harness' own hardening block so the four integrity asserts cannot
        # drift from the harness definition.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)
        base = self.dependency().image_full_name()
        return f"""FROM {base}

COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}"""


@Instance.register("serverless", "serverless")
class Serverless(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ServerlessImageDefault(self.pr, self._config)

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
        log = _ANSI_RE.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in log.split("\n"):
            markers = list(_TAP_RE.finditer(line))
            if not markers:
                continue
            for i, m in enumerate(markers):
                end = markers[i + 1].start() if i + 1 < len(markers) else len(line)
                # Only the test name is captured - never the marker number, which
                # is not stable across stages, and never timings.
                title = line[m.end() : end].strip()
                if not title:
                    continue
                if _SKIP_RE.search(title):
                    skipped_tests.add(_SKIP_RE.sub("", title).strip())
                elif m.group(1) == "ok":
                    passed_tests.add(title)
                else:
                    failed_tests.add(title)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
