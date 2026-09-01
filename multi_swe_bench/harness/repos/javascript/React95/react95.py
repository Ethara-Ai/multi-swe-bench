from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# React95/React95 #147 ("useClippy") — lerna + yarn-workspaces monorepo, jest 25.
#
# Three things about this PR shape the config; each was measured in-container
# against base 49f75626 (2020-05-26) before being written here.
#
# 1. THE FIX PATCH CREATES A NEW WORKSPACE.
#    packages/clippy/ does not exist at base — fix.patch adds the whole package
#    (package.json, src/, index.js). The graded tests live in
#    packages/clippy/tests/, so they cannot even resolve until that package is
#    linked into node_modules. A prepare-time `yarn install` is NOT enough: at
#    image-build time the package does not exist yet. Every run script therefore
#    re-runs `yarn install` AFTER applying patches, which is what makes
#    `import ... from '@react95/clippy'` resolvable in the fix stage. Without it
#    the fix stage fails with:
#        Cannot find module '@react95/clippy' from 'useClippy.test.js'
#    and the instance would look broken when it is actually fine.
#
# 2. THE FIX PATCH ALSO MOVES THE JEST CONFIG.
#    At base, only packages/core/jest.config.js exists and there is no root
#    `test` script. fix.patch DELETES that file and introduces
#    jest/config/config.js plus a root `test` script. So no single fixed
#    --config path is valid in all three stages:
#        run  : packages/core/jest.config.js   (jest/config/* absent)
#        test : packages/core/jest.config.js   (same)
#        fix  : jest/config/config.js          (packages/core/... deleted)
#    The command below picks whichever exists. The three run scripts share ONE
#    identical command string, so QC P7 holds — the branch is inside the command,
#    not between the scripts.
#
# 3. THE PATCHES CONTAIN A BINARY FILE.
#    fix.patch carries packages/clippy/Clippy.gif, and git refuses it:
#        cannot apply binary patch to '...Clippy.gif' without full index line
#    That aborts the WHOLE apply (git apply is atomic), so the fix stage would
#    never run. filter_binary drops binary sections before applying — the same
#    approach the open-policy-agent config uses, and what QC check 3B advises.
#    The .gif is an asset; no test reads it.
#
# MEASURED BASELINE (in-container, per stage):
#    run   63 passed / 8 failed        (18 core suites; clippy absent)
#    test  71 passed core + clippy 2 suites FAIL  (module not found)
#    fix   75 passed / 0 failed        (20 suites = 18 core + 2 clippy)
# The 8 run-stage failures are stale snapshots that test.patch refreshes; they
# are FAIL->PASS between run and test, so they land in p2p, not in the gating
# set. The 4 gating tests are the clippy ones.
# ---------------------------------------------------------------------------

REPO_DIR = "/home/React95"

# Shared by prepare.sh and all three run scripts so the binary-stripping logic
# cannot drift between them.
_FILTER_BINARY = r"""filter_binary() {
  awk '
    function flush() { if (section != "" && !isbin) printf "%s", section }
    /^diff --git / { flush(); section=""; isbin=0 }
    /^GIT binary patch$/ { isbin=1 }
    /^Binary files / { isbin=1 }
    { section = section $0 "\n" }
    END { flush() }
  ' "$1"
}"""

# One command string, interpolated verbatim into run.sh / test-run.sh /
# fix-run.sh, so the graded command is identical across stages (QC P7).
# --verbose is REQUIRED, not cosmetic. Without it jest prints per-test "✓ name"
# lines ONLY for failing suites, so a fully green stage yields nothing for
# parse_log to match and the stage reports 0/0/0. Measured on this repo: 15
# per-test lines without it, 84 with it — which is why the first build came back
# run=(0,0,0) fix=(0,0,0) and was rejected as an invalid report.
TEST_CMD = r"""# Relink workspaces: fix.patch adds packages/clippy as a NEW workspace, and
# jest cannot resolve '@react95/clippy' until yarn links it. Harmless no-op in
# the run/test stages where the package does not exist.
yarn install --ignore-scripts >/dev/null 2>&1

JEST=node_modules/.bin/jest
if [ -f jest/config/config.js ]; then
    # Post-fix layout: one config covering core + clippy.
    "$JEST" --config jest/config/config.js --ci --verbose 2>&1
else
    # Base layout: core owns the only jest config. Run clippy separately when
    # test.patch has put tests there but fix.patch has not yet added the config.
    "$JEST" --config packages/core/jest.config.js --rootDir packages/core --ci --verbose 2>&1
    if [ -d packages/clippy ]; then
        "$JEST" --rootDir packages/clippy --ci --verbose 2>&1
    fi
fi"""


class React95ImageBase(Image):
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
        # node 14 is the era-appropriate runtime for a 2020-05 tree on jest 25;
        # package.json declares no `engines`. Verified in-container: yarn
        # install and the full suite both succeed on node:14-bullseye.
        return "node:14-bullseye"

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

        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV CI=true
ENV NODE_OPTIONS=--max-old-space-size=4096

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class React95ImageDefault(Image):
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
        return React95ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

cd {repo_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Warm the module cache. `|| true` because this is a warm-up, never a graded
# command: a resolution failure must surface in the graded stage where it is
# recorded as a result, not kill the image build.
yarn install --frozen-lockfile --ignore-scripts || true
""".format(repo_dir=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

{test_cmd}
""".format(repo_dir=REPO_DIR, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

{filter}

filter_binary /home/test.patch > /home/test.filtered.patch
if ! git apply --whitespace=nowarn /home/test.filtered.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

{test_cmd}
""".format(repo_dir=REPO_DIR, filter=_FILTER_BINARY, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

{filter}

filter_binary /home/test.patch > /home/test.filtered.patch
filter_binary /home/fix.patch > /home/fix.filtered.patch
if ! git apply --whitespace=nowarn /home/test.filtered.patch /home/fix.filtered.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi

{test_cmd}
""".format(repo_dir=REPO_DIR, filter=_FILTER_BINARY, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # prepare.sh reads only itself and its helper, so those are copied
        # first; the patches and run scripts land AFTER `RUN prepare.sh` in a
        # layer that costs nothing to rebuild.
        prepare_inputs = {"prepare.sh", "check_git_changes.sh"}
        pre, post = "", ""
        for file in self.files():
            line = f"COPY {file.name} /home/\n"
            if file.name in prepare_inputs:
                pre += line
            else:
                post += line

        return f"""FROM {name}:{tag}

{self.global_env}

{pre}
RUN bash /home/prepare.sh

{post}
{self.clear_env}

"""


@Instance.register("React95", "React95")
class REACT95(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return React95ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # ANSI first — jest colourises the ✓/✕ markers and PASS/FAIL words.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # IDs are "<suite file> > <test name>". The file prefix is essential:
        # this is a component library where the same `it()` text recurs across
        # suites, and a bare name would merge distinct tests into one id — which
        # both loses p2p entries and can force a passing test to be recorded as
        # failed once the disjoint-set dedup runs.
        # `(?:\S+\s+)?` absorbs jest's optional displayName. packages/core's
        # jest.config.js sets `displayName: 'core'`, so the real output is
        #     PASS core packages/core/components/Icon/Icon.test.jsx (23.1s)
        # not `PASS <path>`. Without this the path never matches, every test id
        # loses its file prefix, and the stage parses as empty.
        suite_re = re.compile(r"^(PASS|FAIL)\s+(?:\S+\s+)?(\S+\.[jt]sx?)\b")
        case_re = re.compile(
            r"^\s+(?P<mark>[✓✔✕×✗○])\s+(?:skipped\s+)?(?P<name>.+?)"
            r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$"
        )

        current = ""
        for line in log.split("\n"):
            sm = suite_re.match(line)
            if sm:
                current = sm.group(2)
                # A suite that cannot even load (missing module, transform
                # error) prints FAIL with no per-test lines beneath it. Record
                # the FILE so the stage still carries that signal instead of
                # appearing empty — this is exactly the test stage here, where
                # the clippy suites fail on `Cannot find module`.
                if sm.group(1) == "FAIL":
                    failed_tests.add(current)
                continue

            cm = case_re.match(line)
            if not cm:
                continue
            name = cm.group("name").strip()
            tid = f"{current} > {name}" if current else name
            mark = cm.group("mark")
            if mark in "✓✔":
                passed_tests.add(tid)
            elif mark in "✕×✗":
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

        # TestResult.__post_init__ rejects overlapping sets; failure wins over a
        # retry that later passed, then skip.
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
